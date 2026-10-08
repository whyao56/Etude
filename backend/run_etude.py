"""Etude 的启动入口。

源码态跑：``python backend/run_etude.py``
打包后跑：``Etude.exe``（同一个文件就是 PyInstaller 的入口）

命令行开关：

    --check            跑一遍自检并退出（打包后「双击没反应」时的第一站）
    --check-deep       自检并真的连一次模型（慢，但能确认密钥是通的）
    --port N           指定端口
    --no-window        不开原生窗口，只在终端里跑服务
    --data-dir PATH    把数据目录指到别处（也认环境变量 ETUDE_DATA_DIR）
    --version          打印版本号
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 冻结态下 PyInstaller 已经把包放好了；源码态下要让 `app` 这个包能被找到。
# 必须在 import app 之前做。
_BACKEND = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(_BACKEND))


def _fix_console_encoding() -> None:
    """打包后标准输出会掉回 GBK，中文全变乱码。

    这台机器上实测过：源码态正常、exe 里乱码。显式改掉最省事。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="etude", description="Etude —— 句子五通道语言训练器"
    )
    parser.add_argument("--check", action="store_true", help="跑自检并退出")
    parser.add_argument(
        "--check-deep", action="store_true", help="自检并真的连一次模型"
    )
    parser.add_argument("--port", type=int, default=None, help="指定端口")
    parser.add_argument("--no-window", action="store_true", help="不开原生窗口")
    parser.add_argument("--data-dir", default=None, help="数据目录")
    parser.add_argument("--version", action="store_true", help="打印版本号")
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    args = build_parser().parse_args(argv)

    # 数据目录必须在 import app 之前定下来 ——
    # 所有可写路径都是懒取的，但配置和数据库一旦被打开就会锁在旧目录上。
    if args.data_dir:
        os.environ["ETUDE_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())

    from app import __version__, db, pipeline, selfcheck
    from app.paths import DEFAULT_PORT
    from app.server import Server, choose_port, probe_etude

    if args.version:
        print(f"Etude {__version__}")
        return 0

    if args.check or args.check_deep:
        report = selfcheck.run(deep=args.check_deep)
        print(selfcheck.format_text(report))
        return 0 if report["overall"] != "fail" else 1

    # 收拾上一次没跑完的生成任务。不做这一步，那些包会永远停在「生成中」——
    # 界面一直转圈，而实际上没有任何线程在干活。
    recovered = pipeline.recover_stale()
    if recovered:
        print(f"清理了 {recovered} 个上次没跑完的生成任务。")

    preferred = args.port or DEFAULT_PORT
    try:
        port, already = choose_port(preferred)
    except RuntimeError as exc:
        print(f"起不来：{exc}", file=sys.stderr)
        return 2

    if already is not None:
        # 已经在跑了。再开一个进程只会让「数据在哪、状态归谁」变复杂，
        # 所以直接把窗口指向那个实例。
        url = f"http://127.0.0.1:{already['port']}/"
        print(
            f"Etude {already['version']} 已经在运行"
            f"（pid {already['pid']}，数据目录 {already['data_dir']}）。"
        )
        print(f"打开：{url}")
        if not args.no_window:
            from app.desktop import open_browser, run_window

            if not run_window(url, _NullServer()):
                open_browser(url)
        return 0

    # 启动时的自检结果只写日志，不打断启动 ——
    # 「密钥还没填」这种事属于 warn，用户本来就该在界面里看到它。
    try:
        report = selfcheck.run(deep=False)
        if report["overall"] != "ok":
            print(f"启动自检：{report['overall'].upper()}（详情见 logs/selfcheck.txt）")
    except Exception as exc:  # noqa: BLE001
        print(f"自检本身出错了（不影响使用）：{exc}")

    server = Server(port)
    try:
        server.start()
    except RuntimeError as exc:
        print(f"起不来：{exc}", file=sys.stderr)
        return 2

    print(f"Etude {__version__} 就绪 → {server.url}")
    print("关掉这个窗口（或按 Ctrl+C）即可退出。")

    if args.no_window:
        try:
            while True:
                import time

                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        server.ask_stop()
        server.join()
        return 0

    from app.desktop import open_browser, run_window

    if run_window(server.url, server):
        # 窗口关了。不等太久 —— 用户已经在关程序了，多等一秒都是难受。
        server.join(timeout=5.0)
        return 0

    # 开不出原生窗口，退回浏览器。服务留在前台，Ctrl+C 退出。
    print("没有可用的原生窗口（可能缺 WebView2），改用浏览器打开。")
    open_browser(server.url)
    try:
        while True:
            import time

            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    server.ask_stop()
    server.join()
    return 0


class _NullServer:
    """已经在跑的那个实例不属于本进程，没有东西可以停。"""

    def ask_stop(self) -> None:
        return None


if __name__ == "__main__":
    sys.exit(main())
