# -*- mode: python ; coding: utf-8 -*-
"""Etude 打包配置。

**用 onedir 而不是 onefile。**
onefile 每次启动都要把自己解压到一个随机临时目录、退出即删 ——
用户第二次打开会以为「我导入的东西全没了」。onedir 启动是秒级的，
东西都看得见，排错也直观。代价是分发一个文件夹，在说明里写清楚就行。

判断打包形态的第一问是「**数据在哪、会不会丢**」，不是「文件数越少越好」。
Etude 的数据全在 %LOCALAPPDATA%\\Etude，和程序目录无关 ——
所以换个地方解压、甚至直接删掉程序目录，学习记录都不会丢。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()          # noqa: F821  (PyInstaller 注入)
PROJECT_ROOT = SPEC_DIR.parent
BACKEND = PROJECT_ROOT / "backend"

# 原生窗口（pywebview）。关掉它 exe 会直接走浏览器模式。
# 它不是必需的：WebView2 缺失时程序本来就会退回浏览器，
# 所以这条开关只是给「想要更小的包 / 排错」留的。
WITH_WEBVIEW = os.environ.get("ETUDE_WITH_WEBVIEW", "1") == "1"

# ---------------------------------------------------------------- 静态资源

datas = [
    # 界面。整个目录进去，因为将来可能拆成多个文件。
    (str(BACKEND / "frontend"), "frontend"),
]

# 文档带进包里，用户在「设置 → 自检」旁边能直接翻。
# **刻意不带 docs/releases/**：发行说明里印着这个包自己的 SHA256，
# 而一个文件不可能包含自己的哈希 —— 打进去的那份必然是旧的（或占位符）。
# 发行说明的读者在下载页上，不在程序内部。
_docs = PROJECT_ROOT / "docs"
for path in sorted(_docs.glob("*.md")):
    datas.append((str(path), "docs"))
if (_docs / "images").is_dir():
    datas.append((str(_docs / "images"), "docs/images"))

for extra in ("README.md", "CHANGELOG.md", "LICENSE"):
    path = PROJECT_ROOT / extra
    if path.exists():
        datas.append((str(path), "."))

# ---------------------------------------------------------------- 隐藏导入

hiddenimports = [
    # uvicorn 的协议和事件循环实现是按字符串在运行时挑的，静态分析看不见。
    # 不显式列出来就会「源码能跑、exe 一启动就 ImportError」。
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 语音合成。edge_tts 内部按名字挑实现，同样看不见。
    "edge_tts",
    "edge_tts.communicate",
    "edge_tts.constants",
    "edge_tts.drm",
    "edge_tts.exceptions",
    "edge_tts.models",
    "edge_tts.submaker",
    "aiohttp",
    "certifi",
]

# 收集 edge_tts 的全部子模块 —— 它内部结构将来变了也不会漏。
hiddenimports += collect_submodules("edge_tts")

if WITH_WEBVIEW:
    hiddenimports += [
        "webview",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr_loader",
        "pythonnet",
    ]

# ---------------------------------------------------------------- 排除

# 这些是在同一台机器的共享虚拟环境里、**本项目完全不用**的重型依赖。
# 不排掉的话，PyInstaller 的依赖图有可能顺着某个间接引用把它们整棵拉进来，
# 包会凭空胖几百 MB，而且这种膨胀不会有任何功能测试报警 ——
# 只有量体积才发现得了。所以这里显式切断，并且 CI 里盯体积。
excludes = [
    "numpy",
    "scipy",
    "pandas",
    "matplotlib",
    "onnxruntime",
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "huggingface_hub",
    "hf_xet",
    "av",
    "soundcard",
    "PIL",
    "pymupdf",
    "fitz",
    "tkinter",
    "PyQt5",
    "PySide2",
    "PySide6",
    "IPython",
    "pytest",
    "setuptools",
]

# ---------------------------------------------------------------- 主程序

a = Analysis(  # noqa: F821
    [str(BACKEND / "run_etude.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Etude",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 保留控制台：--check 自检要把结果打到终端上，这是「双击没反应」
    # 时的第一站。关掉它等于把唯一的排错出口堵了。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Etude",
)
