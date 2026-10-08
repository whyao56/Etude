"""把 Etude 打成 Windows 可执行目录。

为什么要有这个脚本而不是直接敲 pyinstaller：
打包之前和之后各有几件**很容易漏**的事，脚本把它们变成默认行为。

打包前：
  · 跑一遍前端静态守卫（零构建的项目没有编译器帮忙）
  · 检查前端版本常量和后端 __version__ 是否一致
打包后：
  · 用 PyInstaller 自己的归档读取器核对关键模块**真的在包里**
    （不用 grep 二进制 —— 那个方法两个方向都会给假结论）
  · 量体积并和上次对比，涨得离谱就报警
  · 跑一次 ``Etude.exe --check``，确认冻结态能起来

用法：
    python scripts/build_exe.py
    python scripts/build_exe.py --no-webview     # 不装原生窗口，包更小
    python scripts/build_exe.py --skip-checks    # 赶时间时跳过前置检查
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import products  # noqa: E402  （要先把 scripts/ 放进 sys.path）

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
BUILD = ROOT / "build"
SPEC = BUILD / "etude.spec"
SIZE_BASELINE = BUILD / "size-baseline.json"

# 体积报警线。超了只**报警**，不让构建失败 ——
# 用体积判断「有没有出问题」会有误报，而一个会误报的就别让它拦住发布。
SIZE_WARN_BYTES = 260 * 1024 * 1024


def python_exe() -> str:
    return sys.executable


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(cmd[:4]) + (" …" if len(cmd) > 4 else ""))
    return subprocess.run(cmd, cwd=str(ROOT), text=True, **kwargs)


def version() -> str:
    text = (BACKEND / "app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("backend/app/__init__.py 里找不到 __version__")
    return match.group(1)


# ----------------------------------------------------------------- 前置检查


def preflight() -> None:
    print("[1/5] 前置检查")

    result = run([python_exe(), str(ROOT / "scripts" / "check_js.py")],
                 capture_output=True)
    if result.returncode != 0:
        print(result.stdout or "", result.stderr or "")
        raise SystemExit("前端静态守卫没过，先修好再打包。")
    print("      " + (result.stdout or "").strip())

    backend_version = version()
    html = (BACKEND / "frontend" / "index.html").read_text(encoding="utf-8")
    match = re.search(r'const\s+APP_VERSION\s*=\s*"([^"]+)"', html)
    if not match or match.group(1) != backend_version:
        raise SystemExit(
            f"版本号不一致：后端 {backend_version}，"
            f"前端 {match.group(1) if match else '读不到'}。两处要一起改。"
        )
    print(f"      版本号一致：{backend_version}")


# ------------------------------------------------------------------- 打包


def build(distpath: Path, out_dir: Path, with_webview: bool) -> None:
    print("[2/5] 打包（1-3 分钟）")

    # 为什么换输出目录而不是清空原来的：见 scripts/products.py 的模块注释 ——
    # PyInstaller 在 COLLECT 阶段会把已存在的输出目录整个删掉再重建，
    # 而本机的批量删除保护不让脚本删这么大的目录。
    # 所以这里只往**不存在**的位置写，一个文件都不删。
    env = dict(os.environ)
    env["ETUDE_WITH_WEBVIEW"] = "1" if with_webview else "0"
    env.pop("NODE_OPTIONS", None)

    started = time.time()
    result = run(
        [
            python_exe(), "-m", "PyInstaller",
            str(SPEC),
            "--noconfirm",
            "--distpath", str(distpath),
            "--workpath", str(BUILD / "_pyinstaller"),
        ],
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit("PyInstaller 失败了。")

    exe = out_dir / products.EXE_NAME
    if not exe.is_file():
        raise SystemExit(f"打包结束但找不到 {exe}")
    print(f"      用时 {time.time() - started:.0f} 秒")


# --------------------------------------------------------------- 归档核对


def verify_archive(out_dir: Path) -> list[str]:
    """用 PyInstaller 自己的归档读取器核对关键模块在不在包里。

    **不用 grep 二进制判断。** 上个项目在这上面踩过两次：
    在 exe 里 grep 到模块名（3 次）差点当成证据，可那几次命中来自文档字符串；
    反过来，模块真被打进去时内容是压缩的，grep 又什么都搜不到。
    **两个方向都会给假结论。**

    另外：「静态分析看不见」只对**模块名是运行时拼出来的**成立
    （比如 importlib.import_module(name)）。不要推广到函数内写的
    from X import Y —— 字节码分析照样能追到。
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    exe = out_dir / products.EXE_NAME
    reader = CArchiveReader(str(exe))
    pyz_bytes = reader.extract("PYZ.pyz")
    tmp = BUILD / "_pyz.pyz"
    tmp.write_bytes(pyz_bytes)
    names = set(ZlibArchiveReader(str(tmp)).toc)

    required = [
        "fastapi",
        "starlette",
        "uvicorn.protocols.http.h11_impl",
        "edge_tts",
        "httpx",
        "app.api",
        "app.pipeline",
        "app.providers.llm",
        "app.providers.tts",
        "app.selfcheck",
    ]
    missing = [name for name in required if name not in names]

    print(f"      归档里有 {len(names)} 个模块")
    for name in required:
        mark = "OK  " if name in names else "缺！"
        print(f"        {mark} {name}")
    return missing


def measure_size(out_dir: Path) -> tuple[int, int]:
    total = 0
    count = 0
    for path in out_dir.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
            count += 1
    return total, count


def check_size(total: int, count: int) -> None:
    print(f"[4/5] 体积 {total / 1048576:.1f} MB，{count} 个文件")
    previous = None
    if SIZE_BASELINE.exists():
        try:
            previous = json.loads(SIZE_BASELINE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None

    if previous:
        delta = total - previous.get("bytes", 0)
        pct = delta / max(previous.get("bytes", 1), 1) * 100
        sign = "+" if delta >= 0 else "-"
        print(f"      上次 {previous.get('bytes', 0) / 1048576:.1f} MB，"
              f"变化 {sign}{abs(delta) / 1048576:.1f} MB（{pct:+.1f}%）")
        if abs(delta) > 30 * 1024 * 1024:
            print("      ⚠ 体积变化超过 30 MB。如果这不是有意为之，"
                  "去 spec 的 excludes 里找找谁被间接拉进来了。")

    if total > SIZE_WARN_BYTES:
        print(f"      ⚠ 超过报警线 {SIZE_WARN_BYTES / 1048576:.0f} MB。"
              "（只报警，不失败 —— 体积的临时波动不该拦住发布。）")

    # newline="\n"：默认的 write_text 在 Windows 上会把 \n 翻成 \r\n，
    # 而 .gitattributes 声明这个文件是 LF，于是每次打包后
    # git 都会把它标成「已修改」（其实只是行尾变了）。
    # 一个每次构建都自己变脏的受版本控制的文件，会让「工作区干净吗」
    # 这个判断彻底失效 —— 而那正是发布前唯一想确认的事。
    with SIZE_BASELINE.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump({"bytes": total, "files": count, "version": version()},
                  fh, indent=2)
        fh.write("\n")


# ------------------------------------------------------------- 冻结态冒烟


def smoke(out_dir: Path, tmp_dir: Path) -> None:
    print("[5/5] 冻结态冒烟：Etude.exe --check")
    exe = out_dir / products.EXE_NAME
    env = dict(os.environ)
    # 数据目录指到临时目录，**完全不碰真实数据**。
    env["ETUDE_DATA_DIR"] = str(tmp_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [str(exe), "--check"],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=180,
    )
    print()
    print(result.stdout or result.stderr)
    if result.returncode not in (0, 1):
        raise SystemExit(f"自检没能正常退出（退出码 {result.returncode}）。")
    if "fail" in (result.stdout or "").lower():
        raise SystemExit("自检报了 fail，先看上面哪一项。")
    print("自检通过。")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-webview", action="store_true",
                        help="不打包原生窗口（体积更小，走浏览器模式）")
    parser.add_argument("--skip-checks", action="store_true",
                        help="跳过前置检查和归档核对（只在你清楚后果时用）")
    parser.add_argument("--out", default=None,
                        help="指定产物目录（默认自动挑一个空的，见 scripts/products.py）")
    args = parser.parse_args()

    if args.out:
        out_dir = Path(args.out).resolve()
        distpath = out_dir.parent
        if out_dir.exists():
            raise SystemExit(
                f"{out_dir} 已存在。PyInstaller 会先把它整个删掉，"
                "本机不允许脚本这么删。换个空目录，或先自己手动删掉它。"
            )
        note = ""
    else:
        distpath, out_dir, note = products.next_output()
    if note:
        print(f"      注意：{note}")

    if not args.skip_checks:
        preflight()
    build(distpath, out_dir, with_webview=not args.no_webview)

    if not args.skip_checks:
        print("[3/5] 归档核对")
        missing = verify_archive(out_dir)
        if missing:
            raise SystemExit(f"这些模块没进包：{missing}")

    total, count = measure_size(out_dir)
    check_size(total, count)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="etude-smoke-") as tmp:
        smoke(out_dir, Path(tmp))

    print(f"\n产物：{out_dir / products.EXE_NAME}")
    print("按发行说明里的步骤打包成 zip 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
