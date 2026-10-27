"""学习计划：一份大纲 + 一批训练包。

大纲是有顺序的，所以列表接口要按 outline 的顺序排，不能按 id 排。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..paths import audio_dir, db_path, images_dir, writable_root
from .lessons import delete_lesson

from .schema import (  # noqa: F401 - 每个模块都可能用到，统一引进来
    CHANNELS,
    _CONN,
    _LOCK,
    _loads,
    backup_to,
    close,
    connect,
    execute,
    now,
    one,
    parse_ts,
    query,
)


def create_plan(
    *,
    language: str,
    goal_kind: str,
    goal_label: str,
    goal_detail: str,
    domains: list[str],
    target_count: int,
) -> int:
    ts = now()
    return execute(
        """
        INSERT INTO plans
            (language, goal_kind, goal_label, goal_detail, domains,
             target_count, status, outline, created_at, updated_at)
        VALUES (?,?,?,?,?,?, 'draft', '[]', ?, ?)
        """,
        (
            language,
            goal_kind,
            goal_label,
            goal_detail,
            json.dumps(domains, ensure_ascii=False),
            int(target_count),
            ts,
            ts,
        ),
    )


def get_plan(plan_id: int) -> dict | None:
    row = one("SELECT * FROM plans WHERE id = ?", (plan_id,))
    return _plan_out(row) if row else None


def list_plans() -> list[dict]:
    rows = query(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM lessons l WHERE l.plan_id = p.id) AS lesson_count,
               (SELECT COUNT(*) FROM lessons l
                 WHERE l.plan_id = p.id AND l.status = 'ready')      AS ready_count
          FROM plans p
      ORDER BY p.id DESC
        """
    )
    return [_plan_out(r) for r in rows]


def _plan_out(row: dict) -> dict:
    row = dict(row)
    row["domains"] = _loads(row.get("domains"), [])
    row["outline"] = _loads(row.get("outline"), [])
    return row


def set_plan_outline(plan_id: int, outline: list[dict]) -> None:
    execute(
        "UPDATE plans SET outline = ?, updated_at = ? WHERE id = ?",
        (json.dumps(outline, ensure_ascii=False), now(), plan_id),
    )


def set_plan_status(plan_id: int, status: str, error: str = "") -> None:
    execute(
        "UPDATE plans SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error[:2000], now(), plan_id),
    )


def delete_plan(plan_id: int, *, with_lessons: bool = True) -> int:
    """删计划。默认把它下面的训练包一起删 —— 留着的话它们会
    以「不属于任何计划」的样子出现在列表里，用户会以为删除没生效。"""
    removed = 0
    if with_lessons:
        for row in query("SELECT id FROM lessons WHERE plan_id = ?", (plan_id,)):
            delete_lesson(row["id"])
            removed += 1
    else:
        execute("UPDATE lessons SET plan_id = 0 WHERE plan_id = ?", (plan_id,))
    execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    with _LOCK:
        connect().commit()
    return removed


def plan_lesson_progress(plan_id: int) -> dict:
    row = one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'ready'    THEN 1 ELSE 0 END) AS ready,
               SUM(CASE WHEN status = 'failed'   THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN status = 'generating' THEN 1 ELSE 0 END) AS running
          FROM lessons WHERE plan_id = ?
        """,
        (plan_id,),
    ) or {}
    return {
        "total": row.get("total") or 0,
        "ready": row.get("ready") or 0,
        "failed": row.get("failed") or 0,
        "running": row.get("running") or 0,
    }
