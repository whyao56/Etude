"""间隔重复调度（简化版 SM-2）。

为什么需要它：理论上说「遗忘对学习很重要」——遗忘会阻止大脑只记住个别情况。
所以复习的节奏不应该是「今天练到记住」，而是**在快要忘的时候再遇到一次，
而且每次遇到的是不同的例子**。

这个模块刻意做成**纯函数**：输入当前状态 + 评分，输出新状态，
不碰数据库、不读时钟。这样它能被直接测，也能被界面拿去算「四个按钮
分别会把这张卡推到什么时候」。
"""

from __future__ import annotations

# 评分档位。和界面上四个按钮一一对应。
AGAIN, HARD, GOOD, EASY = 0, 1, 2, 3
GRADE_LABELS = {AGAIN: "没想起来", HARD: "吃力", GOOD: "正常", EASY: "秒答"}

MINUTES_PER_DAY = 1440.0

# 首次见到这张卡时，各评分对应的间隔（天）。
FIRST_INTERVAL = {AGAIN: 0.0, HARD: 1.0, GOOD: 1.0, EASY: 4.0}

# 「没想起来」的重来间隔（分钟）。不直接推到明天 ——
# 当次会话里就该再遇到一次，否则这张卡今天等于没练。
RELEARN_MINUTES = 10.0

EASE_MIN = 1.3
EASE_MAX = 3.0
MAX_INTERVAL_DAYS = 365.0


def schedule(
    interval: float,
    ease: float,
    reps: int,
    lapses: int,
    grade: int,
) -> tuple[float, float, int, int, float]:
    """算下一张卡的排期。

    返回 ``(interval_days, ease, reps, lapses, delay_minutes)``。

    ``delay_minutes`` 才是真正决定「什么时候再来」的那个数 ——
    它和 ``interval_days`` 分开返回，是因为「没想起来」这种情况的推迟
    是分钟级的，而间隔是天的刻度；混用一个字段会让 10 分钟变成 0.007 天，
    然后在某处被四舍五入成 0。
    """
    if grade not in (AGAIN, HARD, GOOD, EASY):
        raise ValueError(f"不认识的评分：{grade!r}")

    ease = float(ease)
    interval = float(interval)
    reps = int(reps)
    lapses = int(lapses)

    if grade == AGAIN:
        ease = max(EASE_MIN, ease - 0.2)
        return 0.0, ease, 0, lapses + 1, RELEARN_MINUTES

    reps += 1

    if grade == EASY:
        ease = min(EASE_MAX, ease + 0.15)
    elif grade == HARD:
        ease = max(EASE_MIN, ease - 0.15)

    if interval <= 0:
        new_interval = FIRST_INTERVAL[grade]
    elif grade == HARD:
        # 吃力：比上次长一点，但远不到一整倍 —— 否则「吃力」会变成
        # 一种比「正常」更划算的选择（间隔涨得一样，还少花力气）。
        new_interval = max(1.0, interval * 1.2)
    elif grade == GOOD:
        new_interval = max(1.0, interval * ease)
    else:  # EASY
        new_interval = max(1.0, interval * ease * 1.3)

    new_interval = min(MAX_INTERVAL_DAYS, round(new_interval, 4))
    return new_interval, ease, reps, lapses, new_interval * MINUTES_PER_DAY


def preview(interval: float, ease: float, reps: int, lapses: int) -> dict[int, dict]:
    """给界面用：四个按钮各自会把这张卡推到什么时候。"""
    out: dict[int, dict] = {}
    for grade in (AGAIN, HARD, GOOD, EASY):
        days, _ease, _reps, _lapses, minutes = schedule(
            interval, ease, reps, lapses, grade
        )
        out[grade] = {
            "label": GRADE_LABELS[grade],
            "days": days,
            "minutes": minutes,
            "human": human_delay(minutes),
        }
    return out


def human_delay(minutes: float) -> str:
    """把分钟数说成人话。界面直接显示，避免出现「0.007 天」这种。"""
    if minutes < 1:
        return "马上"
    if minutes < 60:
        return f"{minutes:.0f} 分钟后"
    if minutes < MINUTES_PER_DAY:
        return f"{minutes / 60:.0f} 小时后"
    days = minutes / MINUTES_PER_DAY
    if days < 30:
        return f"{days:.1f} 天后"
    if days < 365:
        return f"{days / 30:.1f} 个月后"
    return f"{days / 365:.1f} 年后"
