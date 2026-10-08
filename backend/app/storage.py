"""数据目录的搬迁。

数据会长大：音频每条约 20–60 KB，场景图每张 50–300 KB，一个训练包
40 张卡就是几 MB。攒几百个包之后 C 盘会告急。所以允许把数据整个搬走。

## 三条必须做对的

1. **指针留在原地。** 真正被搬走的是 ``etude.sqlite3`` / ``audio`` /
   ``images`` / ``logs`` / ``config.json``；``bootstrap.json`` 永远在
   平台默认目录里 —— 它跟着搬走的话，程序就再也找不到数据了。

2. **先确认目标可用，再动任何东西。** 目标不可写、是同一个目录、
   或者被塞在当前数据目录里面（会递归复制自己），这三种情况必须在
   **复制开始之前**就报出来。复制到一半才发现，用户面对的是
   两个都不完整的数据目录。

3. **不删旧数据。** 复制成功之后，旧目录原样留着，把路径告诉用户让他自己删。
   自动删除是这类操作里唯一不可逆的一步，而这一步没有任何好处
   —— 多占一会儿磁盘远比丢数据便宜。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import db, paths

# 数据目录里「属于数据」的东西。白名单而不是黑名单：
# 黑名单（排除 bootstrap.json）在将来加了新文件时会被悄悄绕过。
_ITEMS = ("etude.sqlite3", "etude.sqlite3-wal", "etude.sqlite3-shm",
          "config.json", "audio", "images", "logs", "backups")


def measure(path: Path) -> dict:
    """目录里有多少东西。用来回答「搬过去要占多大」和「搬完了没」。"""
    path = Path(path)
    total = 0
    files = 0
    if path.exists():
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
                    files += 1
            except OSError:
                continue
    return {"bytes": total, "files": files}


def free_space(path: Path) -> int:
    """目标盘还剩多少。拿不到就返回 -1（界面显示成「未知」而不是 0）。"""
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(str(probe)).free
    except OSError:
        return -1


def describe() -> dict:
    current = paths.writable_root()
    info = measure(current)
    return {
        "current": str(current),
        "source": paths.writable_root_source(),
        "default": str(paths.default_root()),
        "bootstrap": str(paths.bootstrap_path()),
        "moved": bool(paths.data_dir_override()),
        "bytes": info["bytes"],
        "files": info["files"],
        "free_bytes": free_space(current),
    }


def _validate(target: Path, current: Path) -> str:
    """返回错误说明（空串 = 可以搬）。

    这一层刻意**只判断、不动手** —— 搬迁失败最贵的不是失败本身，
    而是「搬了一半」。
    """
    if not str(target).strip():
        return "请填写一个目录。"
    if target == current:
        return "这就是当前的数据目录，不用搬。"
    # 塞在当前目录里面 → 复制自己，会无限递归，而且中途磁盘写满。
    try:
        target.relative_to(current)
        return "新位置不能放在当前数据目录里面（会把自己复制一遍）。"
    except ValueError:
        pass
    if target.exists() and not target.is_dir():
        return f"{target} 已经存在，而且不是一个目录。"
    return ""


def _can_write(target: Path) -> str:
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".etude-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"这个目录写不进去：{exc}"
    return ""


def relocate(raw_path: str, *, copy: bool = True) -> dict:
    """把数据搬到新目录。

    ``copy=True`` 会先把现有数据复制过去（推荐）；``False`` 只改指针，
    新位置从空数据开始 —— 老数据仍在原地，随时可以改回来。

    返回里带 ``restart_required``：数据库连接和配置缓存都是进程级的，
    运行中换目录必然出错（一半请求读旧库、一半读新库）。所以这里
    **只写指针**，让用户重启。这一点必须在界面上说清楚，
    否则用户会以为「点了没反应」。
    """
    # 空白路径必须在 ``resolve()`` **之前**就挡掉。
    # ``Path("   ").resolve()`` 会变成当前工作目录，于是「什么都没填」
    # 会被当成「搬到当前工作目录」—— 然后真的在那里建一堆目录出来。
    # 界面上留空表示「改回默认位置」，那是由调用方翻译的，不该落到这里。
    raw = (raw_path or "").strip()
    if not raw:
        return {
            "ok": False,
            "error": "请填写一个目录。",
            "hint": "留空表示改回平台默认位置，那条路径在别处处理。",
        }

    current = paths.writable_root()
    target = Path(raw).expanduser()
    try:
        target = target.resolve()
    except OSError as exc:
        return {"ok": False, "error": f"这个路径用不了：{exc}"}

    error = _validate(target, current)
    if error:
        return {"ok": False, "error": error}

    error = _can_write(target)
    if error:
        return {"ok": False, "error": error}

    # 先把数据库的 WAL 收干净，再整目录复制 —— 不然复制出来的库里
    # 会缺着还没 checkpoint 的写。VACUUM INTO 由 SQLite 保证一致性。
    copied = 0
    if copy:
        try:
            db.close()  # 让下一次连接重新打开，避免文件被占用
            for name in _ITEMS:
                src = current / name
                if not src.exists():
                    continue
                dst = target / name
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
                copied += 1
        except OSError as exc:
            return {
                "ok": False,
                "error": f"复制数据时出错：{exc}",
                "hint": f"新位置 {target} 可能是个残留的半成品，"
                        "建议删掉它重来一次。当前数据没有被动过。",
            }

    # 复制成功了才写指针。顺序反过来的话，复制失败会留下一个
    # 「指向空目录」的指针 —— 用户重启后看到的是「我的学习记录没了」。
    paths.write_bootstrap(str(target))
    return {
        "ok": True,
        "moved_to": str(target),
        "copied": copied,
        "left_behind": str(current) if copy else "",
        "restart_required": True,
    }


def reset_to_default() -> dict:
    """把指针改回平台默认位置。**不动任何数据。**"""
    paths.write_bootstrap(None)
    return {
        "ok": True,
        "moved_to": str(paths.default_root()),
        "copied": 0,
        "left_behind": "",
        "restart_required": True,
    }


def is_writable(path: Path) -> bool:
    return not _can_write(Path(path))


def env_locked() -> bool:
    """数据目录被环境变量钉住了 —— 这时改指针是没用的，必须告诉用户。"""
    return bool(os.environ.get(paths.DATA_DIR_ENV))
