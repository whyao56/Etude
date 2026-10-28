from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from .. import db, storage
from ..paths import backups_dir, db_path, log_dir, recordings_dir, writable_root
from ..schemas import BackupIn, DataLocationIn, OpenFolderIn, RestoreIn

router = APIRouter()

_BACKUP_NAME = re.compile(r"^[\w.\-]+\.sqlite3$")
_FOLDERS = {
    "data": lambda: writable_root(),
    "logs": log_dir,
    "recordings": recordings_dir,
    "backups": backups_dir,
}


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


@router.get("/api/backups")
def list_backups() -> dict:
    """列备份。按时间倒序，最新的在最前面。

    顺带把「当前库有多少行」带上 —— 恢复是个不可撤销的动作，
    界面需要让用户看见「你要回到的是什么时候、多大的一份数据」。
    """
    items = []
    for path in sorted(backups_dir().glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        items.append({
            "name": path.name,
            "bytes": stat.st_size,
            "at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        })
    return {"backups": items, "current": db.stats(), "default_name": _default_name()}


def _default_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"etude-backup-{stamp}.sqlite3"


@router.post("/api/backups/restore")
def restore_backup(payload: RestoreIn) -> dict:
    """用备份覆盖当前库。

    三步都不能省：
    1. 先把**现在的库**自动备份一份（`before-restore-*.sqlite3`）——
       恢复本身也要能后悔；
    2. `db.close()`，并删掉 `-wal` / `-shm`：留着的话 sqlite 会把旧日志
       重放到新主文件上，得到一个「一半新一半旧」的库；
    3. 恢复后重新 `connect()`，迁移在 connect 里跑，老备份也能直接用。
    """
    name = (payload.name or "").strip()
    if not _BACKUP_NAME.match(name):
        raise HTTPException(400, "备份名不合法。")
    source = backups_dir() / name
    if not source.is_file():
        raise HTTPException(404, f"找不到备份 {name}。")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safety = backups_dir() / f"before-restore-{stamp}.sqlite3"
    try:
        db.backup_to(safety)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"恢复前的自动备份失败，为安全起见没有继续：{exc}") from exc

    target = db_path()
    db.close()
    for suffix in ("-wal", "-shm"):
        extra = target.with_name(target.name + suffix)
        if extra.exists():
            extra.unlink()
    shutil.copyfile(source, target)
    stats = db.stats()
    return {"ok": True, "restored": name, "safety_copy": safety.name, "stats": stats}


@router.post("/api/open-folder")
def open_folder(payload: OpenFolderIn) -> dict:
    """在文件管理器里打开一个数据目录。

    只认四个写死的名字，不接受任意路径 —— 这个接口对浏览器的意义
    是「帮用户少找一次目录」，不是「给网页一个执行任意程序的口子」。
    """
    picker = _FOLDERS.get((payload.which or "data").strip().lower())
    if picker is None:
        raise HTTPException(400, "只支持 data / logs / recordings / backups 四个目录。")
    folder = picker()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"打不开文件夹：{exc}") from exc
    return {"ok": True, "path": str(folder)}
