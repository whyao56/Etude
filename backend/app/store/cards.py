"""复习调度：卡片、评分、撤销、批量调控。

调度算法在 srs.py（纯函数），这里只负责把结果写进 cards 和 reviews，
并在写之前留一份 prev_state 快照 —— 没有快照的评分不可撤销。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..paths import audio_dir, db_path, images_dir, writable_root
from .lessons import get_lesson, swap_rows

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


def due_cards(limit: int = 40, new_per_day: int = 20) -> list[dict]:
    """今日队列：先到期卡，再按配额放新卡。

    刻意分成两段查询而不是一个 ORDER BY —— 「到期」和「新卡」用的是
    两种完全不同的界定（时间 / 配额），混在一起的排序键迟早会写错。
    """
    started = (
        one(
            "SELECT COUNT(*) AS n FROM reviews WHERE substr(at,1,10) = ?",
            (datetime.now(timezone.utc).date().isoformat(),),
        )
        or {}
    ).get("n", 0)

    introduced = query(
        """
        SELECT c.*, l.target, l.gloss FROM cards c
          JOIN lessons l ON l.id = c.lesson_id
         WHERE c.introduced = 1 AND c.due_at <= ? AND c.suspended = 0
      ORDER BY c.due_at ASC
         LIMIT ?
        """,
        (now(), limit),
    )
    room = max(0, limit - len(introduced))
    fresh_quota = max(0, new_per_day - started)
    fresh: list[dict] = []
    if room and fresh_quota:
        fresh = query(
            """
            SELECT c.*, l.target, l.gloss FROM cards c
              JOIN lessons l ON l.id = c.lesson_id
             WHERE c.introduced = 0 AND c.suspended = 0
          ORDER BY c.lesson_id ASC, c.example_id ASC,
                   CASE c.channel WHEN 'read' THEN 0 WHEN 'listen' THEN 1
                                  WHEN 'speak' THEN 2 WHEN 'type' THEN 3
                                  ELSE 4 END
             LIMIT ?
            """,
            (min(room, fresh_quota),),
        )
    return introduced + fresh


def card_payload(card_id: int) -> dict | None:
    card = one("SELECT * FROM cards WHERE id = ?", (card_id,))
    if card is None:
        return None
    lesson = get_lesson(card["lesson_id"]) or {}
    out: dict[str, Any] = {"card": card, "lesson": lesson, "example": None, "swaps": []}
    if card["example_id"]:
        ex = one("SELECT * FROM examples WHERE id = ?", (card["example_id"],))
        if ex:
            ex["zh_variants"] = _loads(ex.get("zh_variants"), [])
            out["example"] = ex
    out["swaps"] = swap_rows(card["lesson_id"])
    return out


def grade_card(card_id: int, grade: int, seconds: float = 0.0) -> dict:
    """记一次复习结果并排下一张的到期时间。"""
    from ..srs import schedule

    card = one("SELECT * FROM cards WHERE id = ?", (card_id,))
    if card is None:
        raise KeyError(f"没有这张卡：{card_id}")

    interval, ease, reps, lapses, delay_min = schedule(
        interval=card["interval"],
        ease=card["ease"],
        reps=card["reps"],
        lapses=card["lapses"],
        grade=grade,
    )
    due = datetime.now(timezone.utc) + timedelta(minutes=delay_min)
    ts = now()

    with _LOCK:
        conn = connect()
        conn.execute(
            """
            UPDATE cards
               SET interval = ?, ease = ?, reps = ?, lapses = ?,
                   due_at = ?, introduced = 1, last_grade = ?, last_at = ?
             WHERE id = ?
            """,
            (interval, ease, reps, lapses, due.isoformat(timespec="seconds"),
             grade, ts, card_id),
        )
        conn.execute(
            """
            INSERT INTO reviews
                (card_id, lesson_id, channel, grade, seconds, prev_state, at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (card_id, card["lesson_id"], card["channel"], grade, float(seconds),
             json.dumps(_srs_snapshot(card), ensure_ascii=False), ts),
        )
        conn.commit()

    return {
        "card_id": card_id,
        "interval_days": interval,
        "due_at": due.isoformat(timespec="seconds"),
        "next_in_minutes": delay_min,
    }


def _srs_snapshot(card: dict) -> dict:
    """评分**之前**的 SRS 状态。撤销时原样写回去。"""
    return {
        "interval": card["interval"],
        "ease": card["ease"],
        "reps": card["reps"],
        "lapses": card["lapses"],
        "due_at": card["due_at"],
        "introduced": card["introduced"],
        "last_grade": card["last_grade"],
        "last_at": card["last_at"],
    }


def last_review(card_id: int) -> dict | None:
    return one(
        "SELECT * FROM reviews WHERE card_id = ? ORDER BY id DESC LIMIT 1", (card_id,)
    )


def undo_grade(card_id: int) -> dict:
    """撤销这张卡最近一次评分，把 SRS 状态和复习记录一起退回去。

    为什么要连 ``reviews`` 那一行也删掉：那张表是「今天已经练了多少张」
    的依据（``due_cards`` 会数它来决定还放多少新卡）。评分撤了但记录留着，
    用户就会莫名其妙地发现「今天新卡配额被吃掉了」。

    快照缺失时**拒绝执行**而不是退化成「清零」—— 一条 0.0.2 之前
    留下的老记录没有快照，此时把卡清零等于凭空毁掉用户的复习进度。
    """
    row = last_review(card_id)
    if row is None:
        raise KeyError("这张卡还没有评分记录，没什么可撤的。")
    prev = _loads(row.get("prev_state"), None)
    if not isinstance(prev, dict) or "interval" not in prev:
        raise KeyError(
            "这条评分记录来自更早的版本，没有留下可回退的状态。"
            "（不是坏了 —— 只是当时还没存快照。）"
        )

    with _LOCK:
        conn = connect()
        conn.execute(
            """
            UPDATE cards
               SET interval = ?, ease = ?, reps = ?, lapses = ?, due_at = ?,
                   introduced = ?, last_grade = ?, last_at = ?
             WHERE id = ?
            """,
            (
                prev["interval"], prev["ease"], prev["reps"], prev["lapses"],
                prev["due_at"], prev["introduced"], prev["last_grade"],
                prev["last_at"], card_id,
            ),
        )
        conn.execute("DELETE FROM reviews WHERE id = ?", (row["id"],))
        conn.commit()
    return {"card_id": card_id, "undone_review_id": row["id"], "restored": prev}


def set_card_srs(card_id: int, patch: dict) -> dict:
    """老手直接改这张卡的复习参数。

    这是「Anki 的灵活度」里最容易被忽略、但实际最常用的一块：
    学习者知道某张卡自己已经烂熟了（或者根本不该现在练），
    宁可手动把它推到两周后，也不想靠连点四次「秒答」去逼近。

    可改：``interval``（天）、``due_at``（ISO 时间）、``ease``、
    ``suspended``。**只改传进来的字段**，其余原样不动 ——
    一个只勾了「暂停」的请求不该顺手把间隔重置。
    """
    card = one("SELECT * FROM cards WHERE id = ?", (card_id,))
    if card is None:
        raise KeyError(f"没有这张卡：{card_id}")

    sets: list[str] = []
    params: list[Any] = []
    if "interval" in patch and patch["interval"] is not None:
        days = max(0.0, float(patch["interval"]))
        sets.append("interval = ?")
        params.append(days)
        # 改间隔同时把到期时间一起算出来。两者分开改的话，
        # 界面上会出现「间隔 30 天，但明天到期」这种自相矛盾的卡。
        sets.append("due_at = ?")
        params.append(
            (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
        )
    if "due_at" in patch and patch["due_at"]:
        sets.append("due_at = ?")
        params.append(str(patch["due_at"]))
    if "ease" in patch and patch["ease"] is not None:
        # 难度夹在 1.3~3.5：超出去之后间隔会指数爆炸或永远推不动。
        sets.append("ease = ?")
        params.append(min(3.5, max(1.3, float(patch["ease"]))))
    if "suspended" in patch and patch["suspended"] is not None:
        sets.append("suspended = ?")
        params.append(1 if patch["suspended"] else 0)
    if "introduced" in patch and patch["introduced"] is not None:
        sets.append("introduced = ?")
        params.append(1 if patch["introduced"] else 0)
        if not patch["introduced"]:
            # 打回新卡必须把 stats 也归零，否则它在队列里排到「新卡」
            # 那一段时会带着旧的间隔和难度，第一眼就是自相矛盾的。
            sets.extend(["interval = ?", "reps = ?", "lapses = ?", "last_grade = ?"])
            params.extend([0.0, 0, 0, None])

    if not sets:
        raise ValueError("没有给出要改的字段。")

    params.append(card_id)
    execute(f"UPDATE cards SET {', '.join(sets)} WHERE id = ?", params)
    return one("SELECT * FROM cards WHERE id = ?", (card_id,)) or {}


def push_cards(card_ids: list[int], days: float) -> dict:
    """把一批卡整体往后推 ``days`` 天。

    「今天不想看这一批」比「逐张改日期」常见得多，所以单独给一个动作。
    从**各自当前的到期时间**往后推，而不是统一设成 now+days ——
    后者会把一张压了三周的卡和一张今天到期的卡抹平成同一天。
    """
    days = max(0.0, float(days))
    changed = 0
    with _LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN")
            for card_id in card_ids:
                row = conn.execute(
                    "SELECT due_at, interval FROM cards WHERE id = ?", (int(card_id),)
                ).fetchone()
                if row is None:
                    continue
                try:
                    base = parse_ts(row[0])
                except (TypeError, ValueError):
                    base = datetime.now(timezone.utc)
                due = base + timedelta(days=days)
                conn.execute(
                    "UPDATE cards SET due_at = ?, interval = ? WHERE id = ?",
                    (due.isoformat(timespec="seconds"), float(row[1] or 0) + days, int(card_id)),
                )
                changed += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"changed": changed, "days": days}


def set_cards_srs(card_ids: list[int], patch: dict) -> dict:
    """批量改卡。逐张走 ``set_card_srs``，**任一张失败就整体报错**。

    不吞掉单张的失败：批量操作里「改了一半」是最难查的状态，
    用户以为都改了，实际只有前 30 张生效。
    """
    done = 0
    for card_id in card_ids:
        set_card_srs(int(card_id), patch)
        done += 1
    return {"changed": done}


def list_cards(
    *,
    lesson_id: int = 0,
    channel: str = "",
    state: str = "all",
    search: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """卡片浏览器的数据源。

    「老手调控」没有这一层就只是嘴上说说 —— 一个只能看到「下一张该练什么」
    的界面，是没法做批量整理的。这里给的是**能筛、能翻页**的清单。
    """
    where: list[str] = ["1=1"]
    params: list[Any] = []
    if lesson_id:
        where.append("c.lesson_id = ?")
        params.append(int(lesson_id))
    if channel:
        where.append("c.channel = ?")
        params.append(channel)
    if state == "due":
        where.append("c.introduced = 1 AND c.suspended = 0 AND c.due_at <= ?")
        params.append(now())
    elif state == "new":
        where.append("c.introduced = 0 AND c.suspended = 0")
    elif state == "suspended":
        where.append("c.suspended = 1")
    elif state == "learning":
        where.append("c.introduced = 1 AND c.suspended = 0")
    if search.strip():
        where.append("(e.sentence LIKE ? OR l.target LIKE ?)")
        like = f"%{search.strip()}%"
        params.extend([like, like])

    clause = " AND ".join(where)
    total = (
        one(
            f"""SELECT COUNT(*) AS n FROM cards c
                  JOIN lessons l ON l.id = c.lesson_id
             LEFT JOIN examples e ON e.id = c.example_id
                 WHERE {clause}""",
            params,
        )
        or {}
    ).get("n", 0)

    rows = query(
        f"""
        SELECT c.id, c.lesson_id, c.example_id, c.channel, c.due_at, c.interval,
               c.ease, c.reps, c.lapses, c.introduced, c.suspended,
               c.last_grade, c.last_at,
               l.target, e.sentence
          FROM cards c
          JOIN lessons l ON l.id = c.lesson_id
     LEFT JOIN examples e ON e.id = c.example_id
         WHERE {clause}
      ORDER BY c.due_at ASC, c.id ASC
         LIMIT ? OFFSET ?
        """,
        params + [max(1, min(1000, int(limit))), max(0, int(offset))],
    )
    return {"cards": rows, "total": total}
