from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import config, db, srs
from ..schemas import CardBatchIn, CardPushIn, CardSrsIn, GradeIn

router = APIRouter()


@router.get("/api/queue")
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


@router.get("/api/cards/{card_id}")
def get_card(card_id: int) -> dict:
    payload = db.card_payload(card_id)
    if payload is None:
        raise HTTPException(404, "没有这张卡。")
    card = payload["card"]
    payload["srs_preview"] = srs.preview(
        card["interval"], card["ease"], card["reps"], card["lapses"]
    )
    # 「能不能撤销」由后端说了算，前端不去猜 —— 猜的那天就会有一个
    # 点了没反应的撤销按钮（0.0.2 修评分键修的正是这种事）。
    last = db.last_review(card_id)
    snapshot = (db._loads(last.get("prev_state"), None) if last else None)
    payload["can_undo"] = isinstance(snapshot, dict) and "interval" in snapshot
    return payload


@router.post("/api/cards/{card_id}/grade")
def grade_card(card_id: int, payload: GradeIn) -> dict:
    try:
        return db.grade_card(card_id, payload.grade, payload.seconds)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/cards/{card_id}/undo")
def undo_grade(card_id: int) -> dict:
    """撤销这张卡最近一次评分。"""
    try:
        return db.undo_grade(card_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.patch("/api/cards/{card_id}")
def patch_card(card_id: int, payload: CardSrsIn) -> dict:
    try:
        card = db.set_card_srs(card_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"card": card}


@router.get("/api/cards")
def list_cards(
    lesson_id: int = 0,
    channel: str = "",
    state: str = "all",
    q: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """卡片浏览器。筛选 + 分页，给「老手调控」用。"""
    return db.list_cards(
        lesson_id=lesson_id, channel=channel, state=state,
        search=q, limit=limit, offset=offset,
    )


@router.post("/api/cards/batch")
def batch_cards(payload: CardBatchIn) -> dict:
    patch = payload.patch.model_dump(exclude_none=True)
    try:
        result = db.set_cards_srs(payload.ids, patch)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@router.post("/api/cards/push")
def push_cards(payload: CardPushIn) -> dict:
    return db.push_cards(payload.ids, payload.days)
