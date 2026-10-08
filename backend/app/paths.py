"""路径判定 —— 全项目**唯一**判断「自己是不是在 exe 里」的地方。

三条规则（经验来自上一个项目的教训：等打包时才发现程序目录只读，
就得把「数据写哪」这条链路全部改一遍）：

1. **可写的**进用户目录（``%LOCALAPPDATA%\\Etude``）；
2. **只读的**进包内资源（``resource_dir()``）；
3. **判断只在这一处做** —— 其它模块调 ``audio_dir()`` / ``db_path()``，
   永远不需要知道自己在源码态还是冻结态。

关于备份：数据库开了 WAL，**单独 copy 主文件会丢掉还没落盘的那部分**。
所以备份走 SQLite 自己的 ``VACUUM INTO``（见 ``db.backup_to``），
而不是在这里提供「连边车文件一起 cp」的工具函数。
"""

from __future__ import annotations

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


def writable_root() -> Path:
    """可写数据根目录。打包后程序目录可能只读，所以数据一律不落在这里。"""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_NAME
    # 非 Windows 或环境变量缺失时的兜底。
    return Path.home() / f".{APP_NAME.lower()}"


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


def log_dir() -> Path:
    d = writable_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return writable_root() / "config.json"
