from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .. import __version__, db, runtime
from ..paths import DEFAULT_PORT, frontend_dir, is_frozen, writable_root
from ..providers import images as images_mod
from ..providers import llm, tts

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    return {
        "app": "Etude",
        "version": __version__,
        "pid": os.getpid(),
        "started_at": datetime.fromtimestamp(runtime.STARTED_AT, timezone.utc).isoformat(
            timespec="seconds"
        ),
        "uptime_seconds": round(time.time() - runtime.STARTED_AT, 1),
        "mode": "exe" if is_frozen() else "source",
        "data_dir": str(writable_root()),
        "port": DEFAULT_PORT,
    }


@router.get("/api/selfcheck")
def selfcheck() -> dict:
    from ..selfcheck import run

    return run()


@router.get("/api/catalog")
def catalog() -> dict:
    from .. import prompts

    return {
        "llm_presets": llm.PRESETS,
        "tts_voices": tts.voice_catalog(),
        "image_sources": images_mod.IMAGE_SOURCES,
        "image_presets": images_mod.IMAGE_PRESETS,
        "languages": prompts.LANGUAGES,
        "goal_kinds": prompts.GOAL_KINDS,
        "exam_presets": prompts.EXAM_PRESETS,
        "domain_presets": prompts.DOMAIN_PRESETS,
        "channels": [
            {"id": "read", "label": "阅读", "from": "英文句子", "to": "场景",
             "what": "看英文句子，在脑子里出现场景。不说话、不翻译。"},
            {"id": "listen", "label": "听力", "from": "声音", "to": "场景",
             "what": "只听声音，直接进入场景。中间不经过图片和中文。"},
            {"id": "speak", "label": "口语", "from": "场景", "to": "说出口",
             "what": "看场景，直接把英文说出来，说完再看原文。"},
            {"id": "type", "label": "打字", "from": "场景", "to": "打出来",
             "what": "看场景，把英文打出来，自动判分。"},
            {"id": "build", "label": "造句", "from": "原句", "to": "换词新句",
             "what": "换掉主语/谓语/宾语，造出一个新句子。"},
        ],
    }


@router.get("/api/stats")
def get_stats() -> dict:
    return db.stats()


_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}

# 0.1.0 起界面是原生 ES 模块：浏览器自己按 import 去取 js/css，
# 所以静态资源必须有路由。三种做法里选了这个（而不是 StaticFiles 挂载）：
# 因为这里要统一发 no-store —— 换个版本的 exe 之后，缓存里的旧脚本配上
# 新后端正是「标签对、内容错」那一类最难查的问题。
_ASSET_MIME = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}


@router.get("/static/{path:path}")
def static(path: str) -> FileResponse:
    root = frontend_dir().resolve()
    target = (root / path).resolve()
    # 解析完再比前缀。只比字符串的话，`js/../../config.json` 会通过。
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "没有这个文件。")
    mime = _ASSET_MIME.get(target.suffix.lower())
    if mime is None:
        raise HTTPException(404, "没有这个文件。")
    return FileResponse(target, media_type=mime, headers=_NO_STORE)


@router.get("/")
def index() -> FileResponse:
    path = frontend_dir() / "index.html"
    if not path.exists():
        return JSONResponse(
            status_code=500,
            content={
                "error": "找不到界面文件。",
                "hint": f"期望它在 {path}。这是打包时漏了资源，"
                "重试修不好，请重新安装完整发行包。",
            },
        )
    return FileResponse(path, media_type="text/html; charset=utf-8", headers=_NO_STORE)
