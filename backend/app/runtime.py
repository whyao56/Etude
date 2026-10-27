"""进程级的两个时间点。

单独拎出来，是因为 ``/api/health`` 要用「启动时刻」算运行时长，
而它住的路由模块和装配层不是一个文件。写在这里之后：

- 路由里读 ``runtime.STARTED_AT``（**属性访问，不是 import 时取值**），
  这样测试替换 ``runtime.STARTED_AT`` 立刻生效；
- 装配层 ``api.STARTED_AT`` 仍然可读（老代码/老测试还在引用它）。

0.0.1 之所以把它写死在 ``api.py`` 里，是因为那时候全后端只有那一个文件。
拆包的时候最容易出的错就是「import 时把值抄了一份」——
抄完之后 patch 原名字就再也影响不了任何东西了。
"""

from __future__ import annotations

import time

STARTED_AT: float = time.time()

__all__ = ["STARTED_AT"]
