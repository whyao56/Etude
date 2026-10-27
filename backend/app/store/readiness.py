"""「这个包现在能不能练」的判断。

ready（能练）与 complete（素材全齐）必须分开：语音抽风时读写造句仍然可用，
如果合成一个字段，用户会被整包封死。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..paths import audio_dir, db_path, images_dir, writable_root
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


def images_expected() -> bool:
    """这个安装到底期不期待场景配图。

    ``images.source = "off"`` 是用户明确的选择（比如长期离线，或者就是
    不想要图）。这时候把「缺场景图」当成问题报出来是错的 —— 训练包详情页
    会永远挂着一句「素材还不齐」，还配一个按了也没用的「补场景图」按钮。
    报一个用户无法消除的警告，等于教会用户无视所有警告。
    """
    try:
        from ..config import load as _load_config

        source = ((_load_config().get("images") or {}).get("source") or "").strip()
    except Exception:  # noqa: BLE001 —— 配置读不出来不该让 readiness 崩掉
        return True
    return source != "off"


def readiness(lesson_id: int, expect_images: bool | None = None) -> dict:
    """这个训练包「现在能不能真的练」。从实际内容算，不信 status。

    刻意分成两个字段，对应两种不同的界面行为：

    - ``ready``    —— 现在拿起来就能练（至少有例句）。有它才允许进入训练。
    - ``complete`` —— 素材齐全（音频在、配图也在）。它只控制**要不要给提示条**，
      不控制能不能练。

    把这两件事合成一个布尔值，就会出现「语音挂了所以整个包不能练」——
    而实际上读写和造句完全不受影响。

    ``expect_images`` 默认按当前配置推断：配图关了就不把「缺图」当问题。
    """
    from ..paths import audio_dir, images_dir

    want_images = images_expected() if expect_images is None else bool(expect_images)

    examples = query(
        "SELECT id, sentence, scene_zh, audio_path, scene_image "
        "FROM examples WHERE lesson_id = ?",
        (lesson_id,),
    )
    missing_audio = []
    missing_image = []
    for row in examples:
        rel = (row.get("audio_path") or "").strip()
        # 「记录里有路径、磁盘上没有」是最阴的一种：界面会照常画出播放按钮/
        # 图片框，点了/加载了没反应。所以这里两个都真的去磁盘核对。
        if not rel or not (audio_dir() / rel).exists():
            missing_audio.append(row["id"])

        img = (row.get("scene_image") or "").strip()
        if not img or not (images_dir() / img).exists():
            missing_image.append(row["id"])

    # 局部导入：lessons.lesson_detail() 会反向调用这里的 readiness()，
    # 顶层互相 import 会成环。写在函数里，调用时才解析，环就断在这行。
    from .lessons import get_lesson

    lesson = get_lesson(lesson_id) or {}
    problems: list[str] = []
    if not (lesson.get("gloss") or "").strip():
        problems.append("缺少英英释义")
    if not examples:
        problems.append("没有例句")
    if missing_audio:
        problems.append(f"{len(missing_audio)} 条例句缺语音")
    if want_images and missing_image:
        problems.append(f"{len(missing_image)} 条例句缺场景图")

    return {
        "has_gloss": bool((lesson.get("gloss") or "").strip()),
        "example_count": len(examples),
        "audio_ready": len(examples) - len(missing_audio),
        "image_ready": len(examples) - len(missing_image),
        "missing_audio_ids": missing_audio,
        "missing_image_ids": missing_image,
        "images_expected": want_images,
        "ready": bool(examples),
        "complete": bool(examples) and not problems,
        "problems": problems,
    }


def stats() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "lessons": (one("SELECT COUNT(*) AS n FROM lessons") or {}).get("n", 0),
        "plans": (one("SELECT COUNT(*) AS n FROM plans") or {}).get("n", 0),
        "examples": (one("SELECT COUNT(*) AS n FROM examples") or {}).get("n", 0),
        "cards": (one("SELECT COUNT(*) AS n FROM cards") or {}).get("n", 0),
        "introduced": (
            one("SELECT COUNT(*) AS n FROM cards WHERE introduced = 1") or {}
        ).get("n", 0),
        "due_now": (
            one(
                "SELECT COUNT(*) AS n FROM cards "
                "WHERE introduced = 1 AND suspended = 0 AND due_at <= ?",
                (now(),),
            )
            or {}
        ).get("n", 0),
        # 暂停的卡单独报一个数。不报的话，用户暂停了一批卡之后会发现
        # 「总数对不上」却找不到那部分去哪了 —— 被藏起来的数字最让人不安。
        "suspended": (
            one("SELECT COUNT(*) AS n FROM cards WHERE suspended = 1") or {}
        ).get("n", 0),
        "reviews_today": (
            one(
                "SELECT COUNT(*) AS n FROM reviews WHERE substr(at, 1, 10) = ?",
                (today,),
            )
            or {}
        ).get("n", 0),
    }
