"""进度与统计。

只做「把库里已有的事实按天摆好」这件事 —— 不新增表、不记埋点。
所有时间桶都按**本机时区**切：库里存的是 UTC，一个中国用户晚上十一点的
复习会落到第二天的 UTC 日期里，按 UTC 分组的话「连续 7 天」会在深夜断掉。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .schema import parse_ts, query

CHANNEL_ORDER = ("read", "listen", "speak", "type", "build")


def _local_date(stamp: str) -> date | None:
    try:
        return parse_ts(stamp).astimezone().date()
    except (ValueError, TypeError):
        return None


def _buckets(days: int) -> list[date]:
    today = datetime.now().astimezone().date()
    return [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def _streak(active_days: set[date]) -> dict:
    """连续天数。今天还没练不算断 —— 昨天练过就还接着数。"""
    today = datetime.now().astimezone().date()
    anchor = today if today in active_days else today - timedelta(days=1)
    current = 0
    cursor = anchor
    while cursor in active_days:
        current += 1
        cursor -= timedelta(days=1)
    best = 0
    run = 0
    previous: date | None = None
    for day in sorted(active_days):
        run = run + 1 if previous and (day - previous).days == 1 else 1
        best = max(best, run)
        previous = day
    return {"current": current, "best": best, "today_done": today in active_days}


def progress(days: int = 14) -> dict:
    days = max(7, min(int(days or 14), 120))
    buckets = _buckets(days)
    first = buckets[0]

    review_rows = query(
        "SELECT r.at AS at, r.grade AS grade, r.channel AS channel, "
        "       r.seconds AS seconds, r.card_id AS card_id "
        "FROM reviews r ORDER BY r.id"
    )
    attempt_rows = query("SELECT a.at AS at, a.score AS score FROM attempts a ORDER BY a.id")

    per_day: dict[date, dict] = {
        day: {"date": day.isoformat(), "reviews": 0, "attempts": 0, "grades": [0, 0, 0, 0]}
        for day in buckets
    }
    active_days: set[date] = set()
    total_reviews = 0
    for row in review_rows:
        day = _local_date(row["at"])
        if day is None:
            continue
        active_days.add(day)
        total_reviews += 1
        bucket = per_day.get(day)
        if bucket is not None:
            bucket["reviews"] += 1
            grade = row["grade"]
            if isinstance(grade, int) and 0 <= grade < 4:
                bucket["grades"][grade] += 1

    total_attempts = 0
    scores: list[float] = []
    for row in attempt_rows:
        day = _local_date(row["at"])
        if day is None:
            continue
        active_days.add(day)
        total_attempts += 1
        if isinstance(row["score"], (int, float)):
            scores.append(float(row["score"]))
        bucket = per_day.get(day)
        if bucket is not None:
            bucket["attempts"] += 1

    series = [per_day[day] for day in buckets]
    today = datetime.now().astimezone().date()
    today_row = per_day.get(today, {"reviews": 0, "attempts": 0})

    # 五通道：各有几张卡、练过几张、平均分
    channel_rows = query(
        "SELECT channel, COUNT(*) AS cards, "
        "       SUM(CASE WHEN introduced = 1 THEN 1 ELSE 0 END) AS introduced, "
        "       SUM(CASE WHEN suspended = 1 THEN 1 ELSE 0 END) AS suspended, "
        "       AVG(CASE WHEN best_score > 0 THEN best_score END) AS best_score "
        "FROM cards GROUP BY channel"
    )
    by_channel = {row["channel"]: row for row in channel_rows}
    review_by_channel = {
        row["channel"]: row
        for row in query(
            "SELECT channel, COUNT(*) AS reviews, AVG(grade) AS avg_grade FROM reviews GROUP BY channel"
        )
    }
    channels = []
    for channel in CHANNEL_ORDER:
        stats = by_channel.get(channel, {})
        reviews = review_by_channel.get(channel, {})
        channels.append({
            "channel": channel,
            "cards": int(stats.get("cards") or 0),
            "introduced": int(stats.get("introduced") or 0),
            "suspended": int(stats.get("suspended") or 0),
            "reviews": int(reviews.get("reviews") or 0),
            "avg_grade": round(float(reviews["avg_grade"]), 2) if reviews.get("avg_grade") is not None else None,
            "best_score": round(float(stats["best_score"]), 1) if stats.get("best_score") is not None else None,
        })

    grades = [0, 0, 0, 0]
    for row in query("SELECT grade, COUNT(*) AS n FROM reviews GROUP BY grade"):
        if isinstance(row["grade"], int) and 0 <= row["grade"] < 4:
            grades[row["grade"]] = int(row["n"])

    weak = query(
        "SELECT c.id AS card_id, c.channel AS channel, c.lapses AS lapses, "
        "       c.interval AS interval, c.best_score AS best_score, "
        "       e.sentence AS sentence, l.target AS target "
        "FROM cards c JOIN lessons l ON l.id = c.lesson_id "
        "LEFT JOIN examples e ON e.id = c.example_id "
        "WHERE c.lapses > 0 ORDER BY c.lapses DESC, c.interval ASC LIMIT 6"
    )

    from .schema import now as _now

    totals = query(
        "SELECT (SELECT COUNT(*) FROM lessons) AS lessons, "
        "       (SELECT COUNT(*) FROM examples) AS examples, "
        "       (SELECT COUNT(*) FROM cards) AS cards, "
        "       (SELECT COUNT(*) FROM cards WHERE introduced = 1) AS introduced, "
        "       (SELECT COUNT(*) FROM plans) AS plans"
    )[0]

    stale = query(
        "SELECT COUNT(*) AS n FROM cards WHERE introduced = 1 AND suspended = 0 AND due_at <= ?",
        (_now(),),
    )[0]["n"]

    return {
        "days": days,
        "from": first.isoformat(),
        "to": today.isoformat(),
        "series": series,
        "today": {
            "date": today.isoformat(),
            "reviews": int(today_row["reviews"]),
            "attempts": int(today_row["attempts"]),
        },
        "streak": _streak(active_days),
        "totals": {
            "lessons": int(totals["lessons"]),
            "examples": int(totals["examples"]),
            "cards": int(totals["cards"]),
            "introduced": int(totals["introduced"]),
            "plans": int(totals["plans"]),
            "reviews": total_reviews,
            "attempts": total_attempts,
            "due_now": int(stale or 0),
        },
        "grades": grades,
        "channels": channels,
        "pronounce": {
            "count": len(scores),
            "average": round(sum(scores) / len(scores), 1) if scores else None,
            "best": round(max(scores), 1) if scores else None,
            "recent": [round(value, 1) for value in scores[-8:]],
        },
        "weak": weak,
    }
