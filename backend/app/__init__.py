"""Etude —— 句子五通道语言训练器。

这个包最外层的 __init__ 只做两件事，而且刻意**不做别的事**：
把线程环境变量限住、把版本号定在这里。

为什么线程限流必须放在这里、而且必须早于任何子模块 import：
OpenBLAS 是在 ``import numpy`` 那一刻就把线程池和每个线程的工作缓冲区
建好的，之后再设环境变量等于没设。所以位置只能是「最外层、最早」。
Etude 自己不用 numpy，但依赖树将来可能带进来（比如换 TTS 后端），
这里先限住，代价是零。
"""

from __future__ import annotations

import os as _os

# 按逻辑核数开线程池的 BLAS 实现会给每个线程预留一大块工作内存，
# 在 24 核机器上实测能差出 39 倍。本项目只做文本和音频文件搬运，
# 一点并行度都用不上，所以限成单线程。
for _var in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    # setdefault 而不是硬赋值：用户自己设过就听用户的。
    _os.environ.setdefault(_var, "1")
del _var, _os

__version__ = "0.0.3"
__all__ = ["__version__"]
