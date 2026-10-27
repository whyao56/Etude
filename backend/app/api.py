"""HTTP 装配层。

两条贯穿整个接口的原则（写在装配层，因为它是所有人的入口）：

1. **每个响应都能自证身份。** ``/api/health`` 返回版本、启动时刻、运行形态、
   数据目录和进程号。上一个项目里踩过：端口上留着一个旧进程在答话，
   前端每次从磁盘现读（所以是新的），服务却是一直在跑的那个（所以是旧的），
   用户看到「检查更新显示 0.3.1，但界面是 0.4.0」。有了这个接口，
   旧进程一眼可辨。

2. **失败要说清方向。** 所有可能失败的操作都返回 ``hint``，
   而不是把 Python 的异常字符串直接甩给用户。

0.1.0 起这个文件只剩三件事：建线程池、装路由、兜住 500。
具体接口按**资源**搬进了 ``app/routes/``（meta/config/lessons/plans/cards/
data/media），请求体模型在 ``app/schemas.py``。
拆的理由不是「文件太大不好看」——是原来那 870 行里同时住着六个资源的
路由、十三个请求模型和线程池，改一处要通读全篇才能确定没撞别的路由。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .routes import ROUTERS
from .runtime import STARTED_AT  # noqa: F401 - 老名字留着，外部还在读它

# 生成是 I/O 等待型（等模型、等语音、等图片），线程数不需要大。
# 但也不能只有 1 —— 用户在等第一个包的时候往往想再丢一个进去。
#
# 上限 3 是刻意的：一份学习计划会串行生成几十个包，
# 如果池子开得再大，多个计划并行时会**同时**打十几个模型请求，
# 在按天限额的免费档上会被整体限流（然后所有包一起失败）。
_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="etude-gen")


def submit(fn: Callable[..., Any], *args: Any):
    """把作业丢进池子。

    函数体里读模块级 ``_POOL``，不是把它当默认参数抄一份 ——
    测试要把提交换成同步执行时 patch 的就是这个模块属性
    （``tests/conftest.py`` 的 ``sync_pool`` 夹具）。
    抄一份的写法会让 patch 静默失效，测试全绿但生产异步，
    那是这类重构最容易留下的假绿灯。
    """
    return _POOL.submit(fn, *args)


app = FastAPI(title="Etude", version=__version__, docs_url=None, redoc_url=None)

for _router in ROUTERS:
    app.include_router(_router)


@app.exception_handler(500)
async def on_500(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": f"程序内部出错：{exc}",
            "hint": "这多半是程序的问题。把这段连同数据目录里的 logs 一起反馈给作者。",
            "path": str(request.url.path),
        },
    )


__all__ = ["app", "submit"]
