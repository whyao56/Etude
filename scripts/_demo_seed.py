"""临时演示数据脚本（不属于项目，用完即删）。

走真实的 HTTP 接口生成几个训练包，好让界面截图里是真实的界面，
而不是空壳。
"""

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8977"
TARGETS = [
    "appreciate",
    "in the long run",
    "I would rather stay home tonight.",
]


def post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    for target in TARGETS:
        print("  生成:", target, "->", post("/api/lessons", {"target": target}))
        time.sleep(3)

    for _ in range(30):
        time.sleep(2)
        lessons = get("/api/lessons")["lessons"]
        if all(l["status"] != "generating" for l in lessons):
            break

    for l in lessons:
        print(f"  #{l['id']} {l['target'][:36]:38} {l['status']:9} "
              f"例句={l['example_count']} 卡={l['card_count']}")
    print("  统计:", get("/api/stats"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
