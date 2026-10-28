"""造一份「像用过几天」的演示数据。

为什么需要它：空库跑起来的界面，看不出进度页、连续天数、柱状图是什么样子；
而手工点几十张卡太慢。这个脚本干三件事：

1. 把大模型指到本机的假模型（`scripts/openai_stub.py`），批量建几个训练包；
2. 在若干张卡上打分（含「没想起来」，制造到期卡）；
3. **把一部分评分记录和跟读记录倒填到过去 N 天**
   —— 这一步要直接改 sqlite，因为接口只认「现在」。

只用于开发/演示，不进产品流程。用法：

    python scripts/seed_demo.py --base http://127.0.0.1:8977 \\
        --db %LOCALAPPDATA%\\Etude\\etude.sqlite3 --days 21

参数默认值按本机开发环境写死（8977 + 假模型的 18999）。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TARGETS = [
    "appreciate",
    "run out of",
    "It depends on how you look at it.",
    "get the hang of",
    "straightforward",
    "keep an eye on",
    "reluctant",
    "come up with",
]


def call(base: str, method: str, path: str, payload=None, raw: bytes | None = None,
         headers: dict | None = None):
    url = base.rstrip("/") + path
    data = None
    hdrs = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if raw is not None:
        data = raw
        hdrs["Content-Type"] = "audio/wav"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            if not body:
                return None
            if resp.headers.get("Content-Type", "").startswith("audio/"):
                return body
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"{method} {path} -> {exc.code}\n{detail}")


def wav(seconds: float = 1.6, rate: int = 16000, freq: float = 180.0) -> bytes:
    """一段合成的元音样噪声。假麦克风也就这个水平 —— 评分只关心响度与停顿。"""
    frames = int(rate * seconds)
    pcm = bytearray()
    for i in range(frames):
        t = i / rate
        # 基频 + 两次谐波 + 一点点包络，免得纯正弦的峰值统计太理想
        env = math.sin(math.pi * min(1.0, i / (0.08 * rate))) if i < 0.08 * rate else 1.0
        env *= math.sin(math.pi * min(1.0, (frames - i) / (0.1 * rate))) if frames - i < 0.1 * rate else 1.0
        value = 0.32 * env * (
            math.sin(2 * math.pi * freq * t)
            + 0.45 * math.sin(2 * math.pi * freq * 2 * t)
            + 0.2 * math.sin(2 * math.pi * freq * 3 * t)
        )
        value = max(-0.98, min(0.98, value))
        pcm += struct.pack("<h", int(value * 32767))
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + bytes(pcm)


def wait_ready(base: str, lesson_id: int, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = call(base, "GET", f"/api/lessons/{lesson_id}/job")
        if job["status"] in ("ready", "failed"):
            return job
        time.sleep(0.6)
    raise SystemExit(f"训练包 {lesson_id} 两分钟还没生成完")


def main() -> int:
    parser = argparse.ArgumentParser(description="给开发环境造演示数据")
    parser.add_argument("--base", default="http://127.0.0.1:8977")
    parser.add_argument("--db", default="", help="数据目录里的 etude.sqlite3，用于倒填历史")
    parser.add_argument("--stub", default="http://127.0.0.1:18999/v1")
    parser.add_argument("--days", type=int, default=21, help="把记录摊到过去几天（0 = 不倒填）")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--no-llm", action="store_true", help="不动大模型配置（已经配好了）")
    args = parser.parse_args()
    rnd = random.Random(args.seed)

    if not args.no_llm:
        call(args.base, "POST", "/api/config", {
            "llm": {"preset": "custom", "base_url": args.stub, "model": "stub-model", "api_key": "demo-key"},
            "images": {"source": "off"},
            "tts": {"engine": "edge"},
            "study": {"examples_per_lesson": 8, "new_per_day": 10},
            "pronounce": {"enabled": True, "mode": "acoustic", "save_recordings": True},
        })
        print("大模型指向", args.stub)

    existing = {item["target"] for item in call(args.base, "GET", "/api/lessons")["lessons"]}
    lesson_ids = []
    for target in TARGETS:
        if target in existing:
            print("已有训练包", target)
            continue
        created = call(args.base, "POST", "/api/lessons", {"target": target})
        lesson_id = created.get("lesson_id") or created["lesson"]["id"]
        job = wait_ready(args.base, lesson_id)
        print(f"建包 {target} -> {job['status']}" + ("（复用没生成完的）" if created.get("reused") else ""))
        lesson_ids.append(lesson_id)

    lessons = call(args.base, "GET", "/api/lessons")["lessons"]
    if not lessons:
        raise SystemExit("一条训练包都没有，先确认服务在跑、假模型通不通。")

    # 评分：让「已练」和「到期」都有一些
    graded = 0
    for round_no in range(3):
        queue = call(args.base, "GET", "/api/queue?limit=30")["cards"]
        if not queue:
            break
        for card in queue:
            grade = rnd.choices([0, 1, 2, 3], weights=[1, 1, 4, 2])[0]
            call(args.base, "POST", f"/api/cards/{card['id']}/grade",
                 {"grade": grade, "seconds": rnd.randint(2, 14)})
            graded += 1
        print(f"第 {round_no + 1} 轮评分 {len(queue)} 张")

    # 跟读：给几种通道各留几条记录（页面上的「比第一次进步几分」才有东西看）
    attempts = 0
    detail = call(args.base, "GET", f"/api/lessons/{lessons[0]['id']}")
    for example in detail["examples"][:3]:
        for _ in range(4):
            score = call(args.base, "POST",
                         f"/api/examples/{example['id']}/attempt?ref_seconds=2.4",
                         raw=wav(seconds=rnd.uniform(1.3, 2.6)))
            attempts += 1
    print(f"跟读 {attempts} 次")

    if args.days and args.db:
        db = Path(args.db)
        if not db.is_file():
            raise SystemExit(f"数据库不在这儿：{db}")
        spread_history(db, args.days, rnd)
        print(f"历史摊到过去 {args.days} 天")

    stats = call(args.base, "GET", "/api/stats")
    print("统计：", json.dumps(stats, ensure_ascii=False))
    print(f"评分 {graded} 次 / 跟读 {attempts} 次 / 训练包 {len(lessons)} 个")
    return 0


def spread_history(db: Path, days: int, rnd: random.Random) -> None:
    """把 reviews / attempts 的时间挪到过去 N 天，并且删掉一部分只留稀疏分布。

    直接改库是因为接口只认「现在」—— 演示数据要的是「过去三周哪天练过」。
    """
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT id FROM reviews ORDER BY id").fetchall()
        if rows:
            keep = rows
            # 只留 55% 左右，免得每天都是满的
            keep = [row for row in rows if rnd.random() < 0.55]
            drop = [row[0] for row in rows if row not in keep]
            if drop:
                conn.executemany("DELETE FROM reviews WHERE id = ?", [(i,) for i in drop])
            for (review_id,) in keep:
                offset = rnd.randint(0, days - 1)
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                      time.gmtime(time.time() - offset * 86400))
                conn.execute("UPDATE reviews SET at = ? WHERE id = ?", (stamp, review_id))
                conn.execute(
                    "UPDATE cards SET last_at = ? WHERE id = "
                    "(SELECT card_id FROM reviews WHERE id = ?)",
                    (stamp, review_id),
                )
        attempt_rows = conn.execute("SELECT id FROM attempts ORDER BY id").fetchall()
        for (attempt_id,) in attempt_rows:
            offset = rnd.randint(0, days - 1)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                  time.gmtime(time.time() - offset * 86400))
            conn.execute("UPDATE attempts SET at = ? WHERE id = ?", (stamp, attempt_id))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
