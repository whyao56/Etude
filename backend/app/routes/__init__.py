"""路由包。

装配层（``app/api.py``）只认这里的一个元组 ``ROUTERS``，不 import 具体模块 ——
这样增减资源只改这一个文件，也不会出现「api.py 和路由模块互相 import」。

同一个模块内部的路由顺序有意义（FastAPI 按声明顺序匹配，字面路径必须排在
``{id}`` 之前）；模块之间互不冲突，所以这里的顺序纯粹按界面的使用频率排。
"""

from __future__ import annotations

from . import batch, cards, config, data, lessons, media, meta, plans, pronounce, progress

ROUTERS = (
    meta.router,
    config.router,
    plans.router,
    # 批量建包的两个字面路径（/api/lessons/batch…）必须排在 lessons 之前：
    # FastAPI 按声明顺序匹配，虽然方法不同不会真的撞车，
    # 但「靠方法不一样侥幸不冲突」不是能长期指望的东西。
    batch.router,
    lessons.router,
    cards.router,
    pronounce.router,
    progress.router,
    data.router,
    media.router,
)

__all__ = ["ROUTERS"]
