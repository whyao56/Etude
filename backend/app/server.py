"""启动 HTTP 服务。

这里处理一个本机最常见的干扰源：**端口被占**。

上个项目的实测教训是：端口上留着一个早前会话的旧进程，新版本「为了单实例」
静默复用了它，于是手工验证时看到的数据目录和版本都不对，差点以为包坏了。

所以这里的策略是**分类处理，而不是一律让路**：

- 端口被占 + 对面是 Etude      → 真的已经在跑，直接用那个，不再起第二个；
- 端口被占 + 对面不是 Etude    → 换个空闲端口起我们自己的；
- 端口空闲                     → 正常启动。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import uvicorn

from .paths import DEFAULT_PORT


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def probe_etude(port: int, timeout: float = 1.5) -> dict | None:
    """问一下这个端口上是不是 Etude。是就返回它的自述。"""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        # 明确不走代理：代理会把 127.0.0.1 的请求改写成绝对地址，
        # 那样拿到的会是一个 404，看起来像「对面不是 Etude」，判断就错了。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("app") == "Etude":
            return data
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return None


def choose_port(preferred: int = DEFAULT_PORT, tries: int = 20) -> tuple[int, dict | None]:
    """返回 ``(端口, 已在跑的那个实例的自述或 None)``。"""
    running = probe_etude(preferred)
    if running is not None:
        return preferred, running
    for candidate in range(preferred, preferred + tries):
        if port_free(candidate):
            return candidate, None
    raise RuntimeError(
        f"{preferred} 起的 {tries} 个端口全被占用了。"
        "关掉一些程序，或者在设置里换一个端口号。"
    )


class Server:
    """在后台线程里跑 uvicorn，可被 ask_stop() 停掉。

    为什么不用 ``uvicorn.run()``：它会接管主线程，而界面窗口需要在主线程上。
    """

    def __init__(self, port: int):
        from .api import app

        self.port = port
        self._config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            # 本机工具，不需要每次请求都做一遍 DNS 反查。
            server_header=False,
            date_header=False,
        )
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self, wait: float = 20.0) -> None:
        self._thread = threading.Thread(
            target=self._server.run, name="etude-http", daemon=True
        )
        self._thread.start()

        deadline = time.time() + wait
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return
            if not self._thread.is_alive():
                raise RuntimeError("HTTP 服务线程提前退出了，通常是端口被占用。")
            time.sleep(0.05)
        raise RuntimeError(f"HTTP 服务在 {wait:.0f} 秒内没有起来。")

    def ask_stop(self) -> None:
        """请求停机。

        只置标志、不做同步等待 —— 这个函数会在窗口的关闭回调里被调用，
        而关闭回调跑在 UI 线程上。在 UI 线程里同步等另一个线程收工，
        正是上一个项目「点 × 就卡死」的根因（两边互等，没有超时）。
        """
        self._server.should_exit = True

    def join(self, timeout: float = 5.0) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()
