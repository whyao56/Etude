"""SQLite 存储层（门面）。

设计上刻意用**扁平字典**出入，不引 ORM：
- 这些数据最终都要原样变成 JSON 给前端，字典省掉一次转换；
- 没有编译器的项目里，ORM 的字段名拼错只会在运行时炸。

一条重要的区分（来自上一个项目最贵的一类 bug）：
    ``status='ready'``      —— 生成任务跑完了，行写进去了
    ``readiness.ready``     —— **现在真的能练**
这两者的差集，就是「看着配好了、用起来没反应」的全部来源。
所以完整度一律从**实际内容**算（有没有例句、有没有音频、音频文件还在不在），
不信 status 字段。

0.1.0 起实现按实体搬进了 ``app/store/``（schema / lessons / readiness / plans /
cards / attempts）。这个模块只剩转发：历史名字一个不少，所以 ``pipeline.py``、
路由和 14 个测试文件都不用知道内部拆成了几块 —— 拆包不该让调用方跟着改。
"""

from __future__ import annotations

from .store import *  # noqa: F401,F403 - 门面的全部公开名字
from .store import __all__ as _STORE_ALL
from .store.cards import _srs_snapshot
from .store.lessons import _EXAMPLE_FIELDS
from .store.schema import SCHEMA, _CONN, _LOCK, _MIGRATIONS, _loads, _migrate

__all__ = list(_STORE_ALL)
