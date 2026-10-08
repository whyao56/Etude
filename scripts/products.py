"""打包产物的位置 —— 三个脚本（build_exe / make_release / e2e_check）共用这一份判断。

为什么需要这个模块，而不是各自写一句 ``ROOT / "dist" / "Etude"``：

**PyInstaller 在 COLLECT 阶段会先把已存在的输出目录整个删掉再重建**
（``PyInstaller/building/api.py``：``_rmtree(self.name)``，注释写着
「will prompt for confirmation if --noconfirm is not given」）。

本机有一层批量删除保护：一次删除超过 50 个文件的目录需要人工确认，
而脚本里的确认传不进去 —— 于是**第二次打包必定失败**，
且失败信息是 PyInstaller 报的一句很模糊的「PyInstaller 失败了」。

所以这里的做法是：**不去删旧目录，换一个空位写新产物。**
不删任何东西，也就不会碰上那层保护。

同时这个选择带来一个额外的好处：上一份产物还在，
体积对比、行为对比、出问题时回滚都不用重新打包。

规则：
    dist/Etude              第一次的产物（规范位置，和文档里写的一致）
    dist/build2/Etude       第二次
    dist/build3/Etude       第三次
    …                       依次往后找第一个空位

要清理时自己删 ``dist/`` 下的旧目录 —— **脚本永远不会替你删**。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# PyInstaller 的 COLLECT 名字写死在 build/etude.spec 里（``name="Etude"``），
# 传 .spec 时 --name 会被忽略。所以**目录名没法加版本号**，
# 只能换父目录。这是这个模块存在的直接原因。
LEAF = "Etude"

EXE_NAME = "Etude.exe"


def next_output() -> tuple[Path, Path, str]:
    """挑一个空的输出位置。

    返回 ``(distpath, 产物目录, 说明)``：
    - ``distpath`` 是传给 PyInstaller ``--distpath`` 的父目录；
    - 产物目录 = ``distpath / "Etude"``；
    - 说明是给日志用的一句话（第一次是空串）。
    """
    first = DIST / LEAF
    if not first.exists():
        return DIST, first, ""

    index = 2
    while (DIST / f"build{index}" / LEAF).exists():
        index += 1
    parent = DIST / f"build{index}"
    note = (
        f"{first} 已存在，本次写到 {parent / LEAF}"
        "（不覆盖、不删除任何东西）"
    )
    return parent, parent / LEAF, note


def newest_output() -> Path | None:
    """找**最近一次**打包的产物目录。

    按产物里 ``Etude.exe`` 的修改时间排序，而不是按目录名 ——
    目录名（``build2`` / ``build10``）按字符串比大小会得到错的顺序，
    而时间戳是这件事的真实判据。
    """
    candidates = []
    for parent in DIST.iterdir() if DIST.is_dir() else []:
        if not parent.is_dir():
            continue
        exe = parent / LEAF / EXE_NAME
        if exe.is_file():
            candidates.append((exe.stat().st_mtime, parent / LEAF))
    if not candidates:
        return None
    return max(candidates)[1]


def describe_dist() -> str:
    """给报错信息用：现在 dist/ 下到底有什么。"""
    found = newest_output()
    if found is None:
        return f"{DIST} 下没有找到任何产物，先跑 scripts/build_exe.py。"
    listing = sorted(
        p.name for p in DIST.iterdir() if p.is_dir()
    )
    return f"最近一份产物在 {found}；dist/ 下有：{', '.join(listing)}"
