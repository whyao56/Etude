"""存储层：按实体切开的 SQLite 读写。

模块划分不是按「代码量」切的，是按**谁改谁**切的：

- ``schema``    —— 连接、事务、表结构、迁移。只有它碰 sqlite3 连接。
- ``lessons``   —— 训练包与例句（含「一条例句 → 五张卡」的铺开逻辑）。
- ``readiness`` —— 「现在到底能不能练」，真的去磁盘核对文件。
- ``plans``     —— 学习计划与大纲。
- ``cards``     —— 复习调度、评分、撤销、批量调控。
- ``attempts``  —— 跟读录音与发音评分（0.1.0 新增）。

对外只暴露这个包的 ``__all__``。``app/db.py`` 是它的门面，历史名字全在那边，
这样 ``pipeline.py``、路由和 14 个测试文件都不必知道内部拆成了几块。
"""

from __future__ import annotations

from .attempts import (
    attempt_summary,
    attempts_in_repository,
    delete_attempts_for_card,
    list_attempts,
    record_attempt,
)
from .cards import (
    card_payload,
    due_cards,
    grade_card,
    last_review,
    list_cards,
    push_cards,
    set_card_srs,
    set_cards_srs,
    undo_grade,
)
from .lessons import (
    add_example,
    create_lesson,
    delete_example,
    delete_lesson,
    example_by_id,
    example_rows,
    fail_lesson,
    finish_lesson,
    get_lesson,
    lesson_detail,
    list_lessons,
    rebuild_cards,
    swap_rows,
    update_example,
)
from .plans import (
    create_plan,
    delete_plan,
    get_plan,
    list_plans,
    plan_lesson_progress,
    set_plan_outline,
    set_plan_status,
)
from .readiness import images_expected, readiness, stats
from .schema import (
    CHANNELS,
    backup_to,
    close,
    connect,
    execute,
    now,
    one,
    parse_ts,
    query,
)

__all__ = [
    "CHANNELS",
    "add_example",
    "attempt_summary",
    "attempts_in_repository",
    "backup_to",
    "card_payload",
    "close",
    "connect",
    "create_lesson",
    "create_plan",
    "delete_attempts_for_card",
    "delete_example",
    "delete_lesson",
    "delete_plan",
    "due_cards",
    "example_by_id",
    "example_rows",
    "execute",
    "fail_lesson",
    "finish_lesson",
    "get_lesson",
    "get_plan",
    "grade_card",
    "images_expected",
    "last_review",
    "lesson_detail",
    "list_attempts",
    "list_cards",
    "list_lessons",
    "list_plans",
    "now",
    "one",
    "parse_ts",
    "plan_lesson_progress",
    "push_cards",
    "query",
    "readiness",
    "rebuild_cards",
    "record_attempt",
    "set_card_srs",
    "set_cards_srs",
    "set_plan_outline",
    "set_plan_status",
    "stats",
    "swap_rows",
    "undo_grade",
    "update_example",
]
