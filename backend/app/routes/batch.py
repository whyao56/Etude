"""批量建包（0.1.0）。

为什么不复用「学习计划」：计划要的是**一份跨越几周的路线**（有目标、有大纲、
有先后顺序、一次几十个包），而这里要的是**一堆今天就能练的目标** ——
用户手里往往是一张从别处抄来的单子，或者一句「我想把和厨房有关的都说利索」。

两件事刻意做在一起：
- ``/api/lessons/batch`` 收一串现成的目标，直接建包；
- ``/api/lessons/batch/draft`` 只让模型**出清单**，不建包 ——
  让人先看见「它打算建什么」再决定。生成了 20 个包才发现跑题，
  比先花 5 秒看一眼贵太多。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from .. import config, db, prompts
from ..providers import llm
from ..schemas import BatchIn, ThemeIn
from ._jobs import submit
from .lessons import _run_generation

router = APIRouter()

# 一次最多建 40 个。再多的话，等它生成完要一个多小时，
# 而用户在第五个包就开始练了 —— 后面的其实可以分几批来。
MAX_BATCH = 40

_SPLIT = re.compile(r"[\n\r,，;；、]+")
# 粘贴过来的单子常带项目符号和序号（"1. " "- " "· "）。
_BULLET = re.compile(r"^\s*(?:[-*·•]|\d+[.)、])\s*")


def parse_targets(text: str, extra: list[str]) -> list[str]:
    """把一坨文本拆成目标清单。去重、去空、去序号，保留原大小写。

    去重按小写比：``Kitchen`` 和 ``kitchen`` 是同一个目标，
    建成两个包只会让人在两个几乎一样的包之间反复横跳。
    """
    raw: list[str] = []
    if text:
        raw.extend(_SPLIT.split(text))
    raw.extend(extra or [])

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        cleaned = _BULLET.sub("", str(item)).strip().strip("\"'“”")
        if not cleaned or len(cleaned) > 300:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out[:MAX_BATCH]


@router.post("/api/lessons/batch")
def create_batch(payload: BatchIn) -> dict:
    targets = parse_targets(payload.text, payload.targets)
    if not targets:
        raise HTTPException(
            400, "没解析出任何目标。一行一个词或一句话，别贴一整段文章。"
        )

    profile = prompts.language_profile(payload.language)
    if not profile["supported"]:
        raise HTTPException(
            400,
            f"{profile['label']}还没开放。{profile['note']}现在可以先用英语。",
        )

    if payload.examples:
        # 显式带了数量就顺手记住 —— 用户下次批量建包不用再改一遍设置。
        config.save({"study": {"examples_per_lesson": int(payload.examples)}})

    created: list[dict] = []
    retried: list[dict] = []
    skipped: list[dict] = []

    for target in targets:
        existing = db.one(
            "SELECT id, status FROM lessons WHERE lower(target) = lower(?) "
            "ORDER BY id DESC",
            (target,),
        )
        if existing and existing["status"] == "failed":
            # 上一次生成失败的，原地重试而不是再开一个 ——
            # 否则同一个词会在列表里堆成三四个「失败」。
            db.execute(
                "UPDATE lessons SET status = 'generating', error = '', updated_at = ? "
                "WHERE id = ?",
                (db.now(), existing["id"]),
            )
            submit(_run_generation, existing["id"])
            retried.append({"target": target, "lesson_id": existing["id"]})
            continue
        if existing and payload.skip_existing:
            skipped.append(
                {"target": target, "lesson_id": existing["id"], "why": "已经有了"}
            )
            continue

        lesson_id = db.create_lesson(
            target, prompts.classify_target(target), payload.language, plan_id=0
        )
        submit(_run_generation, lesson_id)
        created.append({"target": target, "lesson_id": lesson_id})

    return {
        "created": created,
        "retried": retried,
        "skipped": skipped,
        "total": len(targets),
        "queued": len(created) + len(retried),
    }


@router.post("/api/lessons/batch/draft")
def draft_batch(payload: ThemeIn) -> dict:
    """只出清单，不建包。让人先看再动手。"""
    profile = prompts.language_profile(payload.language)
    if not profile["supported"]:
        raise HTTPException(
            400,
            f"{profile['label']}还没开放。{profile['note']}现在可以先用英语。",
        )

    cfg = config.load()
    known = [
        row["target"]
        for row in db.query("SELECT target FROM lessons ORDER BY id DESC LIMIT 400")
    ]
    try:
        messages = prompts.build_theme_messages(
            payload.theme,
            payload.count,
            language=payload.language,
            exclude=known,
        )
        raw = llm.chat(
            base_url=cfg["llm"]["base_url"],
            api_key=cfg["llm"]["api_key"],
            model=cfg["llm"]["model"],
            messages=messages,
            temperature=float(cfg["llm"].get("temperature", 0.9)),
            max_tokens=int(cfg["llm"].get("max_tokens", 4096)),
            timeout=float(cfg["llm"].get("timeout", 120.0)),
        )
        from .. import pipeline

        return pipeline.normalize_targets(llm.extract_json(raw), payload.count)
    except llm.LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
