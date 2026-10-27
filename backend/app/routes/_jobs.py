"""后台作业的提交口。

生成是 I/O 等待型（等模型、等语音、等图），线程数不需要大，但也不能是 1
—— 用户在等第一个包的时候往往想再丢一个进去。池子本身住在 ``app.api``。
它必须住在那里，理由很土：**测试要 patch 它**。

``tests/conftest.py`` 里的 ``sync_pool`` 夹具做的是
``monkeypatch.setattr(api, "_POOL", pool)``，让提交变成同步调用，
这样「生成到底跑没跑」可以断言。所以：

- 这个模块**不能**在顶层 ``from .. import api`` —— 装配层要 include 路由，
  路由又要 import 这里，顶层互相导入就是循环；
- 每次提交都重新取一次 ``api.submit``，函数体里读模块级 ``_POOL``，
  patch 之后才真的换得掉。
"""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any, Callable


def submit(fn: Callable[..., Any], *args: Any) -> "Future[Any]":
    from .. import api

    return api.submit(fn, *args)


__all__ = ["submit"]
