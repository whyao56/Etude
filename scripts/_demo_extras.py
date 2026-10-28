"""临时：给演示数据补一个计划 + 一个带配图的训练包。用完就删。

用法：.venv\\Scripts\\python.exe scripts\\_demo_extras.py
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8977"


def call(path: str, data=None, method: str | None = None):
    req = urllib.request.Request(
        BASE + path, method=method or ("POST" if data is not None else "GET")
    )
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, body) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        print("  !", path, exc.code, exc.read().decode("utf-8")[:400])
        raise


def main() -> None:
    # 1. 配图走假模型（它回一张渐变 PNG），这样截图里能看到图片位。
    call(
        "/api/config",
        {
            "images": {
                "source": "llm",
                "llm": {
                    "base_url": "http://127.0.0.1:18999/v1",
                    "model": "stub-image",
                    "api_key": "stub",
                },
            }
        },
    )
    print("images:", call("/api/lessons/1/images", {}))

    # 2. 一个计划，跑 6 个包，好让「目标」页有详情可看。
    plan = call(
        "/api/plans",
        {
            "language": "en",
            "goal_kind": "fluency",
            "goal_label": "真正学会这门语言",
            "goal_detail": "下个月去冰岛自驾：问路、订房、在咖啡馆和人聊天",
            "domains": ["日常", "旅行"],
            "target_count": 6,
        },
    )
    print("plan:", json.dumps(plan, ensure_ascii=False)[:120])
    # POST /api/plans 回的是 {"ok": true, "plan": {...}} —— 别按 plan_id 取。
    plan_id = (plan.get("plan") or {}).get("id") or plan.get("plan_id")
    call(f"/api/plans/{plan_id}/start", {})

    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(3)
        info = call(f"/api/plans/{plan_id}")
        print("  progress:", info.get("progress"), "status:", info.get("status"))
        if info.get("status") in ("ready", "failed"):
            break


if __name__ == "__main__":
    main()
