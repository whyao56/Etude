from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from .. import db, storage
from ..paths import backups_dir
from ..schemas import BackupIn, DataLocationIn

router = APIRouter()


@router.get("/api/data-location")
def get_data_location() -> dict:
    info = storage.describe()
    info["locked_by_env"] = storage.env_locked()
    return info


@router.post("/api/data-location")
def post_data_location(payload: DataLocationIn) -> dict:
    """改数据目录。**只写指针，重启后生效。**

    运行中换目录必然出错：数据库连接、配置缓存、音频路径都是进程级的，
    换了之后一半请求读旧库一半读新库。所以这里明确返回
    ``restart_required``，让界面把话说清楚 —— 否则用户会以为点了没反应。
    """
    if storage.env_locked():
        raise HTTPException(
            400,
            "数据目录被环境变量 ETUDE_DATA_DIR 钉住了。"
            "要先去掉那个环境变量，这里的设置才会生效。",
        )
    if not payload.path.strip():
        result = storage.reset_to_default()
    else:
        result = storage.relocate(payload.path, copy=payload.copy_files)
    if not result.get("ok"):
        return JSONResponse(
            status_code=400,
            content={"error": result["error"], "hint": result.get("hint", "")},
        )
    return result


@router.post("/api/backup")
def backup(payload: BackupIn) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = (payload.name or f"etude-backup-{stamp}").strip()
    if not re.match(r"^[\w.\-]+$", name):
        raise HTTPException(400, "备份名只能用字母、数字、点、下划线和横线。")
    if not name.endswith(".sqlite3"):
        name += ".sqlite3"
    dest = backups_dir() / name
    try:
        db.backup_to(dest)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"备份失败：{exc}") from exc
    return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size}
