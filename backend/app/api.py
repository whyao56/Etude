"""HTTP 接口。

两条贯穿全文件的原则：

1. **每个响应都能自证身份。** ``/api/health`` 返回版本、启动时刻、运行形态、
   数据目录和进程号。上一个项目里踩过：端口上留着一个旧进程在答话，
   前端每次从磁盘现读（所以是新的），服务却是一直在跑的那个（所以是旧的），
   用户看到「检查更新显示 0.3.1，但界面是 0.4.0」。有了这个接口，
   旧进程一眼可辨。

2. **失败要说清方向。** 所有可能失败的操作都返回 ``hint``，
   而不是把 Python 的异常字符串直接甩给用户。
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, config, db, pipeline, srs, storage
from .paths import (
    DEFAULT_PORT,
    audio_dir,
    backups_dir,
    frontend_dir,
    images_dir,
    is_frozen,
    writable_root,
)
from .providers import images as images_mod
from .providers import llm, tts

STARTED_AT = time.time()

# 生成是 I/O 等待型（等模型、等语音、等图片），线程数不需要大。
# 但也不能只有 1 —— 用户在等第一个包的时候往往想再丢一个进去。
#
# 上限 3 是刻意的：一份学习计划会串行生成几十个包，
# 如果池子开得再大，多个计划并行时会**同时**打十几个模型请求，
# 在按天限额的免费档上会被整体限流（然后所有包一起失败）。
_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="etude-gen")

app = FastAPI(title="Etude", version=__version__, docs_url=None, redoc_url=None)


# ------------------------------------------------------------------ 模型


class LessonIn(BaseModel):
    target: str = Field(min_length=1, max_length=300)
    kind: str | None = None
    language: str = "en"
    plan_id: int = 0


class GradeIn(BaseModel):
    grade: int = Field(ge=0, le=3)
    seconds: float = Field(default=0.0, ge=0.0, le=3600.0)


class ConfigIn(BaseModel):
    llm: dict | None = None
    tts: dict | None = None
    images: dict | None = None
    study: dict | None = None
    channels: dict | None = None
    server: dict | None = None


class PresetIn(BaseModel):
    preset: str


class BackupIn(BaseModel):
    name: str = ""


class PlanIn(BaseModel):
    language: str = "en"
    goal_kind: str = "fluency"
    goal_label: str = ""
    goal_detail: str = Field(default="", max_length=2000)
    domains: list[str] = Field(default_factory=list)
    target_count: int = Field(default=20, ge=1, le=200)


class OutlineIn(BaseModel):
    outline: list[dict] = Field(default_factory=list)


class DataLocationIn(BaseModel):
    """改数据目录的请求体。

    字段对外叫 ``copy``，但 Python 属性名必须叫别的 —— ``copy`` 会盖住
    ``BaseModel.copy``，pydantic 每次校验都会为此发一条警告。
    接口形状没变，只是内部属性换了个名字。
    """

    model_config = ConfigDict(populate_by_name=True)

    path: str = ""
    copy_files: bool = Field(default=True, alias="copy")


# ------------------------------------------------------------------ 自述


@app.get("/api/health")
def health() -> dict:
    return {
        "app": "Etude",
        "version": __version__,
        "pid": os.getpid(),
        "started_at": datetime.fromtimestamp(STARTED_AT, timezone.utc).isoformat(
            timespec="seconds"
        ),
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "mode": "exe" if is_frozen() else "source",
        "data_dir": str(writable_root()),
        "port": DEFAULT_PORT,
    }


@app.get("/api/selfcheck")
def selfcheck() -> dict:
    from .selfcheck import run

    return run()


# ------------------------------------------------------------------ 配置


@app.get("/api/config")
def get_config() -> dict:
    return config.public_view()


@app.post("/api/config")
def post_config(payload: ConfigIn) -> dict:
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    merged = config.save(patch)
    return {"ok": True, "config": config.public_view(merged)}


@app.post("/api/config/preset")
def post_preset(payload: PresetIn) -> dict:
    if payload.preset not in llm.PRESETS:
        raise HTTPException(400, f"没有这个厂商：{payload.preset}")
    merged = config.save(llm.apply_preset(config.load(), payload.preset))
    return {"ok": True, "config": config.public_view(merged)}


@app.post("/api/config/test-image")
def test_image() -> dict:
    cfg = config.load().get("images", {})
    try:
        return images_mod.check(
            source=cfg.get("source", "search"),
            gen_cfg=dict(cfg.get("llm") or {}),
        )
    except images_mod.ImageError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": str(exc), "hint": exc.hint, "kind": exc.kind},
        )


@app.get("/api/catalog")
def catalog() -> dict:
    from . import prompts

    return {
        "llm_presets": llm.PRESETS,
        "tts_voices": tts.voice_catalog(),
        "image_sources": images_mod.IMAGE_SOURCES,
        "image_presets": images_mod.IMAGE_PRESETS,
        "languages": prompts.LANGUAGES,
        "goal_kinds": prompts.GOAL_KINDS,
        "exam_presets": prompts.EXAM_PRESETS,
        "domain_presets": prompts.DOMAIN_PRESETS,
        "channels": [
            {"id": "read", "label": "阅读", "from": "英文句子", "to": "场景",
             "what": "看英文句子，在脑子里出现场景。不说话、不翻译。"},
            {"id": "listen", "label": "听力", "from": "声音", "to": "场景",
             "what": "只听声音，直接进入场景。中间不经过图片和中文。"},
            {"id": "speak", "label": "口语", "from": "场景", "to": "说出口",
             "what": "看场景，直接把英文说出来，说完再看原文。"},
            {"id": "type", "label": "打字", "from": "场景", "to": "打出来",
             "what": "看场景，把英文打出来，自动判分。"},
            {"id": "build", "label": "造句", "from": "原句", "to": "换词新句",
             "what": "换掉主语/谓语/宾语，造出一个新句子。"},
        ],
    }


@app.post("/api/config/test-llm")
def test_llm() -> dict:
    cfg = config.load()["llm"]
    try:
        return llm.check(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            timeout=min(float(cfg.get("timeout", 120.0)), 30.0),
        )
    except llm.LLMError as exc:
        return JSONResponse(status_code=400, content=exc.as_dict())


@app.post("/api/config/test-tts")
def test_tts() -> dict:
    cfg = config.load()["tts"]
    try:
        return tts.check(
            engine=cfg.get("engine", "edge"),
            voice=cfg.get("voice", "en-US-AriaNeural"),
            openai_cfg=cfg.get("openai", {}),
        )
    except tts.TTSError as exc:
        return JSONResponse(
            status_code=400, content={"error": str(exc), "hint": exc.hint}
        )


# ------------------------------------------------------------------ 训练包


@app.get("/api/lessons")
def list_lessons() -> dict:
    return {"lessons": db.list_lessons(), "stats": db.stats()}


@app.post("/api/lessons")
def create_lesson(payload: LessonIn) -> dict:
    from . import prompts

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
    _POOL.submit(_run_generation, lesson_id)
    return {"lesson_id": lesson_id, "reused": False}


def _run_generation(lesson_id: int) -> None:
    """跑在线程池里。异常已经在 pipeline 内部落库，这里只防止它冒到线程外。"""
    try:
        pipeline.generate(lesson_id)
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/lessons/{lesson_id}")
def get_lesson(lesson_id: int) -> dict:
    lesson = db.lesson_detail(lesson_id)
    if lesson is None:
        raise HTTPException(404, "没有这个训练包。")
    lesson["job"] = pipeline.job_status(lesson_id)
    lesson["srs_preview"] = srs.preview(0.0, 2.5, 0, 0)
    return lesson


@app.get("/api/lessons/{lesson_id}/job")
def get_job(lesson_id: int) -> dict:
    lesson = db.get_lesson(lesson_id)
    if lesson is None:
        raise HTTPException(404, "没有这个训练包。")
    return {
        "status": lesson["status"],
        "error": lesson["error"],
        "job": pipeline.job_status(lesson_id),
    }


@app.delete("/api/lessons/{lesson_id}")
def delete_lesson(lesson_id: int) -> dict:
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    db.delete_lesson(lesson_id)
    return {"ok": True}


@app.post("/api/lessons/{lesson_id}/regenerate")
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
    _POOL.submit(_run_generation, lesson_id)
    return {"ok": True, "lesson_id": lesson_id}


@app.post("/api/lessons/{lesson_id}/audio")
def redo_audio(lesson_id: int) -> dict:
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    return _sync_audio(lesson_id)


def _sync_audio(lesson_id: int) -> dict:
    try:
        return pipeline.regenerate_audio(lesson_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"补语音时出错：{exc}") from exc


@app.post("/api/lessons/{lesson_id}/images")
def redo_images(lesson_id: int) -> dict:
    """只补场景图。和补语音分开，因为它们是两种完全不同的失败
    （一个是网络合成，一个是图库检索/文生图），补救动作也不一样。"""
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    try:
        return pipeline.regenerate_images(lesson_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"补场景图时出错：{exc}") from exc


# ------------------------------------------------------------------ 计划
# 顺序有讲究：``/api/plans/draft`` 必须排在 ``/api/plans/{plan_id}`` 前面。
# FastAPI 按声明顺序匹配，反过来写的话 draft 会被当成 plan_id 去转 int，
# 于是返回一个 422 —— 而用户看到的是「点生成大纲报参数错误」。


@app.get("/api/plans")
def list_plans() -> dict:
    plans = db.list_plans()
    for plan in plans:
        plan["progress"] = db.plan_lesson_progress(plan["id"])
    return {"plans": plans}


@app.post("/api/plans")
def create_plan(payload: PlanIn) -> dict:
    """建计划并**立刻出大纲**。

    大纲只花一次模型调用（几秒），却决定了后面十几分钟生成什么。
    先看到再开工，比生成了 20 个包之后发现跑题要便宜得多。
    """
    from . import prompts

    profile = prompts.language_profile(payload.language)
    if not profile["supported"]:
        raise HTTPException(
            400,
            f"{profile['label']}还没开放。{profile['note']}"
            "可以先建一份英语计划 —— 换语言不需要重建数据。",
        )
    if payload.goal_kind not in {g["id"] for g in prompts.GOAL_KINDS}:
        raise HTTPException(400, f"不认识的目标类型：{payload.goal_kind}")

    label = payload.goal_label.strip()
    detail = payload.goal_detail.strip()
    if payload.goal_kind == "custom" and not (label or detail):
        raise HTTPException(400, "自己描述目标的话，至少要写一句话。")

    plan_id = db.create_plan(
        language=payload.language,
        goal_kind=payload.goal_kind,
        goal_label=label,
        goal_detail=detail,
        domains=payload.domains,
        target_count=payload.target_count,
    )
    try:
        result = pipeline.draft_plan(plan_id)
    except Exception as exc:  # noqa: BLE001
        db.set_plan_status(plan_id, "failed", str(exc))
        raise HTTPException(400, str(exc)) from exc
    plan = db.get_plan(plan_id)
    return {"ok": True, "plan": plan, "note": result.get("note", "")}


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: int) -> dict:
    plan = db.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "没有这个计划。")
    plan["progress"] = db.plan_lesson_progress(plan_id)
    plan["job"] = pipeline.job_status_of(pipeline.job_key_plan(plan_id))
    # 按**大纲的顺序**排，不是按 id。
    # ``list_lessons`` 是「新的在前」，直接拿来用的话，计划详情里的顺序正好
    # 被倒过来 —— 而大纲本身是有先后的（从马上能用到的排到靠后的），
    # 倒着显示会把「最后才该学的那条」摆在最上面。
    # 用 target 去对照，而不是靠 id 递增：中间删掉一条再补生成一条之后，
    # id 的顺序就不再等于大纲的顺序了。
    order = {
        (item.get("target") or "").lower(): i
        for i, item in enumerate(plan.get("outline") or [])
    }
    plan["lessons"] = sorted(
        (row for row in db.list_lessons() if row.get("plan_id") == plan_id),
        key=lambda row: (order.get((row.get("target") or "").lower(), len(order)), row["id"]),
    )
    return plan


@app.put("/api/plans/{plan_id}/outline")
def edit_outline(plan_id: int, payload: OutlineIn) -> dict:
    """改大纲。用户可以先删掉不想要的条目再开工 ——
    这是「在花十几分钟之前发现问题」的另一半。"""
    if db.get_plan(plan_id) is None:
        raise HTTPException(404, "没有这个计划。")
    cleaned = pipeline.normalize_outline({"outline": payload.outline}, len(payload.outline))
    db.set_plan_outline(plan_id, cleaned["outline"])
    return {"ok": True, "outline": cleaned["outline"]}


@app.post("/api/plans/{plan_id}/start")
def start_plan(plan_id: int) -> dict:
    plan = db.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "没有这个计划。")
    if not plan["outline"]:
        raise HTTPException(400, "这个计划还没有大纲，先点「生成大纲」。")
    if plan["status"] == "generating":
        return {"ok": True, "plan_id": plan_id, "already_running": True}
    db.set_plan_status(plan_id, "generating")
    _POOL.submit(_run_plan, plan_id)
    return {"ok": True, "plan_id": plan_id}


def _run_plan(plan_id: int) -> None:
    try:
        pipeline.generate_plan(plan_id)
    except Exception:  # noqa: BLE001 - 失败已落库，这里只防止冒到线程外
        pass


@app.get("/api/plans/{plan_id}/job")
def get_plan_job(plan_id: int) -> dict:
    plan = db.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "没有这个计划。")
    return {
        "status": plan["status"],
        "error": plan["error"],
        "job": pipeline.job_status_of(pipeline.job_key_plan(plan_id)),
        "progress": db.plan_lesson_progress(plan_id),
    }


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int, keep_lessons: bool = False) -> dict:
    if db.get_plan(plan_id) is None:
        raise HTTPException(404, "没有这个计划。")
    removed = db.delete_plan(plan_id, with_lessons=not keep_lessons)
    return {"ok": True, "lessons_removed": removed}


# ------------------------------------------------------------------ 数据位置


@app.get("/api/data-location")
def get_data_location() -> dict:
    info = storage.describe()
    info["locked_by_env"] = storage.env_locked()
    return info


@app.post("/api/data-location")
def post_data_location(payload: DataLocationIn) -> dict:
    """改数据目录。**只写指针，重启后生效。**

    运行中换目录必然出错：数据库连接、配置缓存、音频路径都是进程级的，
    换了之后一半请求读旧库一半读新库。所以这里明确返回
    ``restart_required``，让界面把话说清楚 —— 否则用户会以为点了没反应。
    """
    if storage.env_locked():
        raise HTTPException(
            400,
            "数据目录被环境变量 ETUDE_DATA_DIR 钉住了。"
            "要先去掉那个环境变量，这里的设置才会生效。",
        )
    if not payload.path.strip():
        result = storage.reset_to_default()
    else:
        result = storage.relocate(payload.path, copy=payload.copy_files)
    if not result.get("ok"):
        return JSONResponse(
            status_code=400,
            content={"error": result["error"], "hint": result.get("hint", "")},
        )
    return result


# ------------------------------------------------------------------ 音频
# 只允许取「内容寻址」形态的文件名（`<engine>-<20位十六进制>.mp3`）。
# 不做这个校验的话，`/api/audio/..%2F..%2Fconfig.json` 就能读到配置文件里的密钥。
_AUDIO_NAME = re.compile(r"^[a-z]+-[0-9a-f]{20}\.mp3$")


@app.get("/api/audio/{name}")
def get_audio(name: str) -> FileResponse:
    if not _AUDIO_NAME.match(name):
        raise HTTPException(400, "文件名不合法。")
    path = audio_dir() / name
    if not path.exists():
        # 界面会画出一个播放按钮却点了没反应 —— 明确报 404，
        # 让前端能把它标成「这条缺语音」而不是静默失败。
        raise HTTPException(404, "这条语音还没生成。")
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "max-age=31536000"})


# 图片来源有 search / llm 两种，文件扩展名跟着图库给什么走。
# 白名单校验的理由和音频完全一样：不校验就能读到 config.json。
_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@app.get("/api/images/{name}")
def get_image(name: str) -> FileResponse:
    if not images_mod.valid_name(name):
        raise HTTPException(400, "文件名不合法。")
    path = images_dir() / name
    if not path.exists():
        raise HTTPException(404, "这张场景图还没有生成。")
    mime = _MIME_BY_EXT.get(path.suffix.lower(), "image/jpeg")
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "max-age=31536000"})


# ------------------------------------------------------------------ 复习


@app.get("/api/queue")
def queue(limit: int = 40) -> dict:
    cfg = config.load()
    limit = max(1, min(200, limit))
    cards = db.due_cards(limit=limit, new_per_day=int(cfg["study"].get("new_per_day", 20)))
    return {"cards": [_decorate(c) for c in cards], "stats": db.stats()}


def _decorate(card: dict) -> dict:
    """给卡片行补上界面要用的字段（目标词、有没有语音）。"""
    return {
        "id": card["id"],
        "lesson_id": card["lesson_id"],
        "example_id": card["example_id"],
        "channel": card["channel"],
        "target": card.get("target", ""),
        "introduced": card["introduced"],
    }


@app.get("/api/cards/{card_id}")
def get_card(card_id: int) -> dict:
    payload = db.card_payload(card_id)
    if payload is None:
        raise HTTPException(404, "没有这张卡。")
    card = payload["card"]
    payload["srs_preview"] = srs.preview(
        card["interval"], card["ease"], card["reps"], card["lapses"]
    )
    return payload


@app.post("/api/cards/{card_id}/grade")
def grade_card(card_id: int, payload: GradeIn) -> dict:
    try:
        return db.grade_card(card_id, payload.grade, payload.seconds)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/stats")
def get_stats() -> dict:
    return db.stats()


# ------------------------------------------------------------------ 备份


@app.post("/api/backup")
def backup(payload: BackupIn) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = (payload.name or f"etude-backup-{stamp}").strip()
    if not re.match(r"^[\w.\-]+$", name):
        raise HTTPException(400, "备份名只能用字母、数字、点、下划线和横线。")
    if not name.endswith(".sqlite3"):
        name += ".sqlite3"
    dest = backups_dir() / name
    try:
        db.backup_to(dest)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"备份失败：{exc}") from exc
    return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size}

# ------------------------------------------------------------------ 界面

# 前端自带一个版本号常量，和 /api/health 比对。不一致时页面顶部弹红条。
# 所以 HTML 绝不能被缓存 —— 否则用户拿到的还是旧前端，检测也就失效了。
_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


@app.get("/")
def index() -> FileResponse:
    path = frontend_dir() / "index.html"
    if not path.exists():
        return JSONResponse(
            status_code=500,
            content={
                "error": "找不到界面文件。",
                "hint": f"期望它在 {path}。这是打包时漏了资源，"
                "重试修不好，请重新安装完整发行包。",
            },
        )
    return FileResponse(path, media_type="text/html; charset=utf-8", headers=_NO_STORE)


@app.exception_handler(500)
async def on_500(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": f"程序内部出错：{exc}",
            "hint": "这多半是程序的问题。把这段连同数据目录里的 logs 一起反馈给作者。",
            "path": str(request.url.path),
        },
    )
