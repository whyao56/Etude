from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import db, pipeline
from ..schemas import OutlineIn, PlanIn
from ._jobs import submit

router = APIRouter()


@router.get("/api/plans")
def list_plans() -> dict:
    plans = db.list_plans()
    for plan in plans:
        plan["progress"] = db.plan_lesson_progress(plan["id"])
    return {"plans": plans}


@router.post("/api/plans")
def create_plan(payload: PlanIn) -> dict:
    """建计划并**立刻出大纲**。

    大纲只花一次模型调用（几秒），却决定了后面十几分钟生成什么。
    先看到再开工，比生成了 20 个包之后发现跑题要便宜得多。
    """
    from .. import prompts

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


@router.get("/api/plans/{plan_id}")
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


@router.put("/api/plans/{plan_id}/outline")
def edit_outline(plan_id: int, payload: OutlineIn) -> dict:
    """改大纲。用户可以先删掉不想要的条目再开工 ——
    这是「在花十几分钟之前发现问题」的另一半。"""
    if db.get_plan(plan_id) is None:
        raise HTTPException(404, "没有这个计划。")
    cleaned = pipeline.normalize_outline({"outline": payload.outline}, len(payload.outline))
    db.set_plan_outline(plan_id, cleaned["outline"])
    return {"ok": True, "outline": cleaned["outline"]}


@router.post("/api/plans/{plan_id}/start")
def start_plan(plan_id: int) -> dict:
    plan = db.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "没有这个计划。")
    if not plan["outline"]:
        raise HTTPException(400, "这个计划还没有大纲，先点「生成大纲」。")
    if plan["status"] == "generating":
        return {"ok": True, "plan_id": plan_id, "already_running": True}
    db.set_plan_status(plan_id, "generating")
    submit(_run_plan, plan_id)
    return {"ok": True, "plan_id": plan_id}


def _run_plan(plan_id: int) -> None:
    try:
        pipeline.generate_plan(plan_id)
    except Exception:  # noqa: BLE001 - 失败已落库，这里只防止冒到线程外
        pass


@router.get("/api/plans/{plan_id}/job")
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


@router.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int, keep_lessons: bool = False) -> dict:
    if db.get_plan(plan_id) is None:
        raise HTTPException(404, "没有这个计划。")
    removed = db.delete_plan(plan_id, with_lessons=not keep_lessons)
    return {"ok": True, "lessons_removed": removed}
