"""原生窗口。

Windows 上靠系统自带的 WebView2 运行时（Win10 1803+ / Win11 默认就有）。
没有 WebView2 或没装 pywebview 时**自动退回浏览器**，而不是报错退出 ——
「能不能用」比「用哪种窗口」重要。
"""

from __future__ import annotations

import sys
import webbrowser

# 支持 `python -m app.desktop` 这个入口时会出现模块分裂：
# 文件以 __main__ 的名字执行，而别的模块又 `from . import desktop`
# 把它按模块名再导入一次，于是同一份模块级状态存了两份。
# 症状会**像功能坏了而不像状态串了**（窗口开着，接口说「没有窗口」）。
# 这一行必须在任何可能触发其它导入的代码之前。
if __name__ == "__main__":
    sys.modules.setdefault("app.desktop", sys.modules[__name__])


WINDOW_TITLE = "Etude · 句子五通道语言训练器"
MIN_SIZE = (960, 640)


def open_browser(url: str) -> None:
    webbrowser.open(url)


def run_window(url: str, server, *, on_closed=None) -> bool:
    """开原生窗口并阻塞到窗口关闭。返回是否真的开出来了。

    ``server`` 只用于在窗口关闭时请求停机。**关闭回调里绝不做同步等待**：
    回调跑在 UI 线程上，而 UI 线程一旦卡在等另一个线程收工，两边就互等，
    永远解不开（这是上个项目「点 × 就卡死」的根因）。
    所以这里只置一个标志、立刻返回，剩下的交给解释器退出时的收尾。
    """
    try:
        import webview
    except ImportError:
        return False

    def _on_closed() -> None:
        # 只置标志，不 join、不 sleep、不等事件。
        try:
            server.ask_stop()
        except Exception:  # noqa: BLE001 - 收尾失败不该让关闭流程卡住
            pass
        if on_closed is not None:
            try:
                on_closed()
            except Exception:  # noqa: BLE001
                pass

    try:
        window = webview.create_window(
            WINDOW_TITLE,
            url,
            width=1180,
            height=820,
            min_size=MIN_SIZE,
            text_select=True,
        )
        window.events.closed += _on_closed
        # gui=None 让它自己挑；Windows 上通常是 edgechromium（WebView2）。
        webview.start()
        return True
    except Exception:  # noqa: BLE001
        # WebView2 缺失、GUI 后端起不来等等 —— 退回浏览器，不要抛给用户。
        return False
