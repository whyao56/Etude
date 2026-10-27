"""跟读记录（0.1.0）。

一张卡可以有很多次尝试，这个模块只管「记下来」和「翻出来看」。
评分本身在 ``app/pronounce.py``，这里不掺业务判断。

两条设计取舍写在这里，因为它们决定了表长什么样：

1. **存每一次，不存最好那次。** 用户想看的是「上礼拜和今天的我」；
   只有留着原始记录才比得出来。而一次录音的分数受麦克风、环境噪声、
   当天嗓子影响很大 —— 只展示最高分会让人误判自己在进步。
2. **音频按内容寻址单独存文件。** 表里只留文件名，和语音、配图一个口径；
   换数据目录、做备份时不会留一堆死链。
"""

from __future__ import annotations

import json
from typing import Any

from .schema import _loads, execute, one, query


def _attempt_out(row: dict) -> dict:
    out = dict(row)
    out["detail"] = _loads(out.get("detail"), {})
    return out


def record_attempt(
    *,
    card_id: int = 0,
    lesson_id: int = 0,
    example_id: int = 0,
    channel: str = "speak",
    mode: str = "acoustic",
    score: float = 0.0,
    seconds: float = 0.0,
    audio: str = "",
    detail: dict | None = None,
) -> dict:
    """记一次跟读。返回刚写进去的那一行（前端直接拿它去画结果）。"""
    from .schema import now

    at = now()
    attempt_id = execute(
        """
        INSERT INTO attempts
            (card_id, lesson_id, example_id, channel, mode, score, seconds, audio, detail, at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(card_id),
            int(lesson_id),
            int(example_id),
            str(channel or "speak"),
            str(mode or "acoustic"),
            float(score),
            float(seconds),
            str(audio or ""),
            json.dumps(detail or {}, ensure_ascii=False),
            at,
        ),
    )
    if card_id:
        # 卡片上的「最高分」只做展示与自身对比，**不参与 SRS 调度**：
        # 发音好不好和「该不该现在复习」是两件事，混在一起会让
        # 一个读不准音的人永远被安排重练同一张卡（卡在 10 分钟档出不来）。
        execute(
            "UPDATE cards SET best_score = MAX(best_score, ?) WHERE id = ?",
            (float(score), int(card_id)),
        )
    row = one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
    return _attempt_out(row) if row else {}


def list_attempts(
    *, card_id: int = 0, example_id: int = 0, limit: int = 20
) -> list[dict]:
    """最近的 N 次尝试，新的在前。

    两个条件都给 0 时返回全局最近记录 —— 设置页要显示「一共录了多少次」。
    """
    limit = max(1, min(200, int(limit)))
    where: list[str] = []
    params: list[Any] = []
    if card_id:
        where.append("card_id = ?")
        params.append(int(card_id))
    if example_id:
        where.append("example_id = ?")
        params.append(int(example_id))
    sql = "SELECT * FROM attempts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_attempt_out(row) for row in query(sql, tuple(params))]


def attempt_summary(*, card_id: int = 0, example_id: int = 0) -> dict:
    """汇总：练了几次、最高多少、最近多少、第一次多少。

    ``delta`` 是「最近一次 − 第一次」。它是这个功能真正想说的那句话 ——
    「你比刚开始的时候好了多少」，而不是「你现在几分」。
    """
    rows = list_attempts(card_id=card_id, example_id=example_id, limit=200)
    if not rows:
        return {"count": 0, "best": 0.0, "latest": 0.0, "first": 0.0,
                "average": 0.0, "delta": 0.0, "last_at": ""}
    scores = [float(row.get("score") or 0.0) for row in rows]
    latest = scores[0]
    first = scores[-1]
    return {
        "count": len(scores),
        "best": round(max(scores), 1),
        "latest": round(latest, 1),
        "first": round(first, 1),
        "average": round(sum(scores) / len(scores), 1),
        "delta": round(latest - first, 1),
        "last_at": rows[0].get("at", ""),
    }


def delete_attempts_for_card(card_id: int) -> int:
    """删掉一张卡上的全部尝试。只给「清空这张卡的跟读历史」用。"""
    rows = query("SELECT id FROM attempts WHERE card_id = ?", (int(card_id),))
    execute("DELETE FROM attempts WHERE card_id = ?", (int(card_id),))
    return len(rows)


def attempts_in_repository() -> int:
    """一共存了多少次录音。自检与设置页都要显示这个数。"""
    row = one("SELECT COUNT(*) AS n FROM attempts", ())
    return int(row["n"]) if row else 0
