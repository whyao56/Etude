"""路径判定 —— 全项目**唯一**判断「自己是不是在 exe 里」的地方。

三条规则（经验来自上一个项目的教训：等打包时才发现程序目录只读，
就得把「数据写哪」这条链路全部改一遍）：

1. **可写的**进用户目录（默认 ``%LOCALAPPDATA%\\Etude``）；
2. **只读的**进包内资源（``resource_dir()``）；
3. **判断只在这一处做** —— 其它模块调 ``audio_dir()`` / ``db_path()``，
   永远不需要知道自己在源码态还是冻结态。

## 数据目录可以被搬走（0.0.2 新增）

数据会长到几个 GB（音频 + 场景图片），而 C 盘常常是最紧的那个盘。
所以允许把数据整个搬到别处，做法是**在一个永远不动的地方放一个指针**：

    默认位置/bootstrap.json   →   {"data_dir": "D:/Etude/data"}

读取顺序：``ETUDE_DATA_DIR`` 环境变量 → bootstrap.json → 平台默认位置。

指针**必须留在默认位置**、不能跟着数据一起搬走 —— 否则程序就再也
找不到数据在哪了。这是这个设计里唯一一条不能违反的约束。

## 关于备份

数据库开了 WAL，**单独 copy 主文件会丢掉还没落盘的那部分**。
所以备份走 SQLite 自己的 ``VACUUM INTO``（见 ``db.backup_to``），
而不是在这里提供「连边车文件一起 cp」的工具函数。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "Etude"

# 允许用环境变量把数据目录整个挪走。
# 端到端检查靠它把数据写到临时目录，完全不碰真实数据。
DATA_DIR_ENV = "ETUDE_DATA_DIR"

# 端口。刻意避开了上一个项目用过的 8787 —— 那台机器上留着一堆旧进程，
# 手工验证时 curl 打到旧进程上，看到的版本和数据目录都不对。
DEFAULT_PORT = 8977


def is_frozen() -> bool:
    """是否跑在 PyInstaller 打出来的包里。"""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """源码态的仓库根目录（etude/）。冻结态返回 exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """只读资源根目录。界面文件从这里读。

    onedir 和 onefile 在 PyInstaller 6 里 ``sys._MEIPASS`` 都指向内部目录
    （onedir 是 ``_internal/``），所以两种形态共用一条路径。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    # 源码态：资源放在 backend/ 下，和 spec 里的目标路径保持一致，
    # 这样「前端在哪」在两种形态下都是同一句 <root>/frontend/index.html。
    return Path(__file__).resolve().parents[1]


def frontend_dir() -> Path:
    return resource_dir() / "frontend"


def default_root() -> Path:
    """平台默认的数据目录。**bootstrap.json 永远住在这里，不被搬走。**"""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_NAME
    # 非 Windows 或环境变量缺失时的兜底。
    return Path.home() / f".{APP_NAME.lower()}"


def bootstrap_path() -> Path:
    return default_root() / "bootstrap.json"


def read_bootstrap() -> dict:
    """读指针文件。坏掉/不存在都当成「没有指针」，不抛异常。

    配置坏掉不该让程序起不来 —— 起不来比「用默认位置」严重得多。
    """
    path = bootstrap_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_bootstrap(data_dir: str | None) -> Path:
    """写指针文件。``None`` 表示恢复成平台默认位置。

    先写临时文件再 ``os.replace``：中途崩溃不会留下半个 JSON，
    否则下一次启动读到坏文件就会**静默退回默认目录**，
    而用户以为自己的数据还在新位置上。
    """
    path = bootstrap_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"data_dir": str(data_dir)} if data_dir else {}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def data_dir_override() -> str:
    """指针文件里存的目标目录（空串表示没设过）。"""
    value = read_bootstrap().get("data_dir")
    return str(value) if value else ""


def writable_root() -> Path:
    """可写数据根目录。打包后程序目录可能只读，所以数据一律不落在这里。"""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    pointed = data_dir_override()
    if pointed:
        return Path(pointed).expanduser().resolve()
    return default_root()


def writable_root_source() -> str:
    """数据目录是**怎么**定下来的。界面要显示这个 —— 「我的数据到底在哪、
    为什么会在这里」被打包后是最常被问的一件事。"""
    if os.environ.get(DATA_DIR_ENV):
        return DATA_DIR_ENV
    if data_dir_override():
        return "bootstrap"
    return "default"


def ensure_writable_root() -> Path:
    root = writable_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return writable_root() / "etude.sqlite3"


def audio_dir() -> Path:
    d = writable_root() / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def images_dir() -> Path:
    """场景图片缓存。和音频一样按内容寻址，同一张图不会存两份。"""
    d = writable_root() / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    d = writable_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backups_dir() -> Path:
    d = writable_root() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return writable_root() / "config.json"
