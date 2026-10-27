from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db, pipeline, srs
from ..schemas import ExampleIn, LessonIn, NewExampleIn
from ._jobs import submit

router = APIRouter()


@router.get("/api/lessons")
def list_lessons() -> dict:
    return {"lessons": db.list_lessons(), "stats": db.stats()}


@router.post("/api/lessons")
def create_lesson(payload: LessonIn) -> dict:
    from .. import prompts

    target = payload.target.strip()
    if not target:
        raise HTTPException(400, "输入是空的。")

    profile = prompts.language_profile(payload.language)
    if not profile["supported"]:
        # 明确说「还没开放」，而不是默默用英语的规则去生成别的语言。
        raise HTTPException(
            400,
            f"{profile['label']}还没开放。{profile['note']}"
            "现在可以先用英语 —— 换语言只需要一次版本更新，数据不用重建。",
        )

    kind = payload.kind or prompts.classify_target(target)
    if kind not in ("word", "phrase", "sentence"):
        kind = "word"

    existing = db.one(
        "SELECT id, status FROM lessons WHERE lower(target) = lower(?) ORDER BY id DESC",
        (target,),
    )
    if existing and existing["status"] == "generating":
        # 同一个目标正在生成，直接复用，不要开第二份。
        return {"lesson_id": existing["id"], "reused": True}

    lesson_id = db.create_lesson(
        target, kind, language=payload.language, plan_id=payload.plan_id
    )
    submit(_run_generation, lesson_id)
    return {"lesson_id": lesson_id, "reused": False}


def _run_generation(lesson_id: int) -> None:
    """跑在线程池里。异常已经在 pipeline 内部落库，这里只防止它冒到线程外。"""
    try:
        pipeline.generate(lesson_id)
    except Exception:  # noqa: BLE001
        pass


@router.get("/api/lessons/{lesson_id}")
def get_lesson(lesson_id: int) -> dict:
    lesson = db.lesson_detail(lesson_id)
    if lesson is None:
        raise HTTPException(404, "没有这个训练包。")
    lesson["job"] = pipeline.job_status(lesson_id)
    lesson["srs_preview"] = srs.preview(0.0, 2.5, 0, 0)
    return lesson


@router.get("/api/lessons/{lesson_id}/job")
def get_job(lesson_id: int) -> dict:
    lesson = db.get_lesson(lesson_id)
    if lesson is None:
        raise HTTPException(404, "没有这个训练包。")
    return {
        "status": lesson["status"],
        "error": lesson["error"],
        "job": pipeline.job_status(lesson_id),
    }


@router.delete("/api/lessons/{lesson_id}")
def delete_lesson(lesson_id: int) -> dict:
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    db.delete_lesson(lesson_id)
    return {"ok": True}


@router.post("/api/lessons/{lesson_id}/regenerate")
def regenerate_lesson(lesson_id: int) -> dict:
    """重新生成整个训练包（例句 + 语音）。

    给「生成失败」和「对素材不满意」两种情况用。会**先清空旧素材**，
    所以复习进度也会一起没掉 —— 这一点在界面上要问清楚，不能默默做。
    """
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    db.execute(
        "UPDATE lessons SET status = 'generating', error = '', updated_at = ? WHERE id = ?",
        (db.now(), lesson_id),
    )
    submit(_run_generation, lesson_id)
    return {"ok": True, "lesson_id": lesson_id}


@router.post("/api/lessons/{lesson_id}/audio")
def redo_audio(lesson_id: int) -> dict:
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    return _sync_audio(lesson_id)


def _sync_audio(lesson_id: int) -> dict:
    try:
        return pipeline.regenerate_audio(lesson_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"补语音时出错：{exc}") from exc


@router.post("/api/lessons/{lesson_id}/images")
def redo_images(lesson_id: int) -> dict:
    """只补场景图。和补语音分开，因为它们是两种完全不同的失败
    （一个是网络合成，一个是图库检索/文生图），补救动作也不一样。"""
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    try:
        return pipeline.regenerate_images(lesson_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"补场景图时出错：{exc}") from exc


@router.patch("/api/examples/{example_id}")
def patch_example(example_id: int, payload: ExampleIn) -> dict:
    # by_alias=True 把 register_text 换回 register —— 数据库里的列就叫 register。
    patch = payload.model_dump(exclude_none=True, by_alias=True)
    try:
        return {"example": db.update_example(example_id, patch)}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/examples/{example_id}")
def remove_example(example_id: int) -> dict:
    try:
        return db.delete_example(example_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/lessons/{lesson_id}/examples")
def add_example(lesson_id: int, payload: NewExampleIn) -> dict:
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    try:
        example_id = db.add_example(
            lesson_id, payload.sentence,
            scene_en=payload.scene_en, scene_zh=payload.scene_zh,
            register=payload.register_text, zh_variants=payload.zh_variants,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    failures: list[str] = []
    if payload.make_media:
        # 手工加的句子也必须能听。只插一行数据的话，用户会在听力卡上
        # 遇到一条永远没有声音的句子，而且不知道去哪补。
        from .. import pipeline

        audio = pipeline.make_example_audio(example_id, failures)
        image = pipeline.make_example_image(example_id, failures)
        db.execute(
            "UPDATE examples SET audio_path = ?, scene_image = ? WHERE id = ?",
            (audio or "", image or "", example_id),
        )
    return {"example": db.example_by_id(example_id), "failures": failures}


@router.post("/api/examples/{example_id}/audio")
def redo_example_audio(example_id: int) -> dict:
    from .. import pipeline

    if db.example_by_id(example_id) is None:
        raise HTTPException(404, "没有这条例句。")
    failures: list[str] = []
    name = pipeline.make_example_audio(example_id, failures, force=True)
    if not name:
        raise HTTPException(400, failures[0] if failures else "语音合成失败。")
    db.execute("UPDATE examples SET audio_path = ? WHERE id = ?", (name, example_id))
    return {"audio_path": name}


@router.post("/api/examples/{example_id}/image")
def redo_example_image(example_id: int) -> dict:
    from .. import pipeline

    if db.example_by_id(example_id) is None:
        raise HTTPException(404, "没有这条例句。")
    failures: list[str] = []
    name = pipeline.make_example_image(example_id, failures, force=True)
    if not name:
        raise HTTPException(400, failures[0] if failures else "配图失败。")
    db.execute("UPDATE examples SET scene_image = ? WHERE id = ?", (name, example_id))
    return {"scene_image": name}


@router.post("/api/lessons/{lesson_id}/rebuild-cards")
def rebuild_cards(lesson_id: int) -> dict:
    """补齐「有例句但没有卡」的缺口。**不重置任何已有进度。**"""
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    return db.rebuild_cards(lesson_id)
