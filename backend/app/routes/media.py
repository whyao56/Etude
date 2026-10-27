from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..paths import audio_dir, images_dir
from ..providers import images as images_mod

router = APIRouter()


_AUDIO_NAME = re.compile(r"^[a-z]+-[0-9a-f]{20}\.mp3$")


@router.get("/api/audio/{name}")
def get_audio(name: str) -> FileResponse:
    if not _AUDIO_NAME.match(name):
        raise HTTPException(400, "文件名不合法。")
    path = audio_dir() / name
    if not path.exists():
        # 界面会画出一个播放按钮却点了没反应 —— 明确报 404，
        # 让前端能把它标成「这条缺语音」而不是静默失败。
        raise HTTPException(404, "这条语音还没生成。")
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "max-age=31536000"})


_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@router.get("/api/images/{name}")
def get_image(name: str) -> FileResponse:
    if not images_mod.valid_name(name):
        raise HTTPException(400, "文件名不合法。")
    path = images_dir() / name
    if not path.exists():
        raise HTTPException(404, "这张场景图还没有生成。")
    mime = _MIME_BY_EXT.get(path.suffix.lower(), "image/jpeg")
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "max-age=31536000"})
