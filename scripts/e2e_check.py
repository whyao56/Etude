"""端到端检查：对着**打包好的 exe** 打真实 HTTP。

三条要求（来自上个项目的教训）：
  ① 打真实 HTTP，不是调函数；
  ② 数据目录指向临时目录，**完全不碰真实数据**；
  ③ 断言写成可见的数字（`12/12`），失败时一眼看得出缺什么。

另外刻意**换一个没人用的端口**，并且核对响应里能唯一标识这个进程的字段
（pid / 模式 / 数据目录）——
上个项目里 curl 打到了残留的旧进程，看到版本和数据目录都不对，
差点以为打包版是坏的。

用法：
    python scripts/e2e_check.py                        # 只查服务本身
    python scripts/e2e_check.py --llm-base http://127.0.0.1:18999/v1
                                                       # 再走一遍完整生成
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import products  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(ok), label, detail))
    mark = "  OK  " if ok else " FAIL "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def open_no_proxy(url: str, data: bytes | None = None, timeout: float = 60.0):
    """发请求，且**明确不走代理**。

    代理会把对 127.0.0.1 的请求改写成绝对地址，于是本地服务收到一个 404
    而不是「连不上」—— 症状像功能坏了，其实是代理插了一脚。
    """
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def get_json(url: str, timeout: float = 30.0):
    with open_no_proxy(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, timeout: float = 120.0):
    with open_no_proxy(url, json.dumps(payload).encode("utf-8"), timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-base", default=None,
                        help="假模型服务的 base_url；给了就跑一遍完整生成")
    parser.add_argument("--exe", default=None,
                        help="打包版的 Etude.exe（默认取最近一次打包的那份）")
    args = parser.parse_args()

    if args.exe:
        exe = Path(args.exe)
    else:
        dist = products.newest_output()
        if dist is None:
            print(products.describe_dist())
            return 2
        exe = dist / products.EXE_NAME
        print(f"用最近一次的产物：{exe}")
    if not exe.is_file():
        print(f"找不到 {exe}。先跑 scripts/build_exe.py。")
        return 2

    port = free_port()
    print(f"用端口 {port}（刻意避开常用端口，免得撞上残留的旧进程）")

    with tempfile.TemporaryDirectory(prefix="etude-e2e-") as data_dir:
        env = {
            **{k: v for k, v in __import__("os").environ.items()},
            "ETUDE_DATA_DIR": data_dir,
            "PYTHONIOENCODING": "utf-8",
        }
        proc = subprocess.Popen(
            [str(exe), "--port", str(port), "--no-window"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        base = f"http://127.0.0.1:{port}"
        try:
            if not wait_ready(base):
                print("服务没起来。它的输出是：")
                print(proc.stdout.read() if proc.stdout else "（没有输出）")
                return 2

            print("\n=== 进程自述 ===")
            health = get_json(base + "/api/health")
            check(health.get("app") == "Etude", "是 Etude 在答话")
            check(health.get("mode") == "exe", "运行形态是打包版", str(health.get("mode")))
            check(
                Path(health["data_dir"]).resolve() == Path(data_dir).resolve(),
                "数据目录正是我们指定的临时目录",
                health["data_dir"],
            )
            check(health.get("pid") == proc.pid, "应答的就是我们起的那个进程",
                  f"{health.get('pid')} == {proc.pid}")

            print("\n=== 界面 ===")
            with open_no_proxy(base + "/") as resp:
                html = resp.read().decode("utf-8")
                headers = {k.lower(): v for k, v in resp.headers.items()}
            check(len(html) > 20000, "界面文件在包里并且取得到", f"{len(html)} 字节")
            check("Etude" in html and "APP_VERSION" in html, "界面内容看起来是对的")
            check("no-store" in headers.get("cache-control", ""),
                  "界面响应带了 no-store（否则版本检测会失效）")

            print("\n=== 自检 ===")
            report = get_json(base + "/api/selfcheck")
            fails = [i for i in report["items"] if i["level"] == "fail"]
            check(not fails, "自检没有 fail 项",
                  f"总体 {report['overall']}，{len(report['items'])} 项")

            print("\n=== 静态资源与安全 ===")
            for name in ("..%2F..%2Fconfig.json", "config.json", "a.mp3"):
                try:
                    open_no_proxy(base + "/api/audio/" + name, timeout=10)
                    ok = False
                    detail = "被放行了！"
                except urllib.error.HTTPError as exc:
                    ok = exc.code in (400, 404)
                    detail = f"HTTP {exc.code}"
                check(ok, f"挡住越权文件名 {name}", detail)

            if args.llm_base:
                print("\n=== 完整生成（对着假模型）===")
                post_json(base + "/api/config", {
                    "llm": {"base_url": args.llm_base, "api_key": "e2e", "model": "stub"},
                    "tts": {"engine": "openai", "openai": {
                        "base_url": args.llm_base, "api_key": "e2e",
                        "model": "tts-1", "voice": "alloy"}},
                    # 场景配图**必须**在这里指明来源。默认是 search ——
                    # 去开放图库联网检索。那会让这个检查依赖外网：断网、
                    # 代理、图库抽风，症状都是「训练包一直停在生成中」，
                    # 而失败原因看起来像生成的 bug，排查方向全错。
                    # 指到假模型还顺带把「文生图」这条链路走了一遍。
                    "images": {
                        "source": "llm",
                        "max_per_lesson": 8,
                        "llm": {"base_url": args.llm_base, "api_key": "e2e",
                                "model": "stub"},
                    },
                })
                created = post_json(base + "/api/lessons", {"target": "appreciate"})
                lesson_id = created["lesson_id"]

                deadline = time.time() + 120
                lesson = {}
                while time.time() < deadline:
                    lesson = get_json(f"{base}/api/lessons/{lesson_id}")
                    if lesson["status"] != "generating":
                        break
                    time.sleep(1)

                check(lesson.get("status") == "ready", "训练包生成成功",
                      str(lesson.get("status")))
                ex_count = len(lesson.get("examples", []))
                check(ex_count >= 3, "生成了多条例句", f"{ex_count} 条")
                check(len(lesson.get("cards", [])) == ex_count * 5,
                      "每条例句铺了 5 张卡",
                      f"{len(lesson.get('cards', []))} 张")

                name = (lesson.get("examples") or [{}])[0].get("audio_path")
                check(bool(name), "第一条有语音文件")
                if name:
                    try:
                        with open_no_proxy(f"{base}/api/audio/{name}", timeout=30) as r:
                            blob = r.read()
                        check(len(blob) > 0, "语音能真的取回来", f"{len(blob)} 字节")
                    except urllib.error.HTTPError as exc:
                        check(False, "语音能真的取回来", f"HTTP {exc.code}")

                # 场景配图。这里断言的是**文件头**而不是「文件非空」：
                # 图床出错时爱返回一个 200 + 一页 HTML，按后缀或按
                # Content-Type 存下来就是一张永远加载不出来的假图，
                # 而「非空」这个断言会一路放行。
                pic = (lesson.get("examples") or [{}])[0].get("scene_image")
                check(bool(pic), "第一条有场景配图")
                if pic:
                    try:
                        with open_no_proxy(f"{base}/api/images/{pic}", timeout=30) as r:
                            blob = r.read()
                        head = blob[:4]
                        check(head == b"\x89PNG", "配图取回来真的是图片",
                              f"{len(blob)} 字节，头部 {head!r}")
                    except urllib.error.HTTPError as exc:
                        check(False, "配图取回来真的是图片", f"HTTP {exc.code}")

                queue = get_json(base + "/api/queue?limit=40")
                check(len(queue["cards"]) > 0, "复习队列里有卡",
                      f"{len(queue['cards'])} 张")

                card = get_json(f"{base}/api/cards/{queue['cards'][0]['id']}")
                graded = post_json(base + f"/api/cards/{card['card']['id']}/grade",
                                   {"grade": 2, "seconds": 3.0})
                check(graded.get("interval_days", 0) > 0, "评分能落库并排下一次",
                      f"下次 {graded.get('next_in_minutes', 0):.0f} 分钟后")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    passed = sum(1 for ok, _l, _d in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} 项通过")
    if passed != total:
        print("失败的项：")
        for ok, label, detail in _results:
            if not ok:
                print(f"  · {label} {detail}")
    return 0 if passed == total else 1


def wait_ready(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            get_json(base + "/api/health", timeout=3)
            return True
        except Exception:  # noqa: BLE001 - 起服务期间各种连不上都算「还没好」
            time.sleep(0.4)
    return False


if __name__ == "__main__":
    sys.exit(main())
