"""生成流水线：目标 → 例句素材 → 音频 → 配图 → 可练的训练包。

两件必须做对的事：

**一、逐条失败不能拖垮整体。** 语音合成和图片下载都要联网，
8 条里失败 2 条是常事。如果任何一条失败就整体回滚，用户会得到
「什么都没生成」—— 而他本可以先练那 6 条。所以音频和配图的失败是
**记录**，不是**中断**。

**二、进度要能看见。** 生成一个包要几十秒（开了配图更久）。
没有进度的话，用户面对的是一个不动的界面，然后他会怀疑是卡死了
（上一个项目里，「点了没反应」是最难查的一类反馈）。
所以这里维护一张作业表，界面轮询它。

0.0.2 还多了一条流水线：**学习计划**（``generate_plan``）。
它先让模型出一份大纲（一次调用），再逐个把大纲里的条目变成训练包。
两个流水线共用同一套作业表，所以界面只有一套进度代码。
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any

from . import config, db
from .paths import log_dir
from .providers import images as images_mod
from .providers import llm, tts

# 作业表。键是字符串：``lesson:<id>`` 或 ``plan:<id>``。
#
# 用带前缀的字符串而不是两张表，是为了让界面只有一套「查进度」的代码。
# 顺带也避开了「lesson 3 和 plan 3 抢同一个 key」这种将来一定会踩的坑。
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.RLock()


def job_key_lesson(lesson_id: int) -> str:
    return f"lesson:{lesson_id}"


def job_key_plan(plan_id: int) -> str:
    return f"plan:{plan_id}"


def job_status(lesson_id: int) -> dict | None:
    """按训练包 id 查进度。保留这个签名 —— 接口层一直在用它。"""
    with _JOBS_LOCK:
        job = _JOBS.get(job_key_lesson(lesson_id))
        return dict(job) if job else None


def job_status_of(key: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        return dict(job) if job else None


def _set_job(key: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        job = _JOBS.setdefault(key, {})
        job.update(fields)
        job["updated_at"] = time.time()


def recover_stale() -> int:
    """把上次崩溃/关窗留下的「生成中」标成失败。

    不做这一步的话，那些包会永远停在「生成中」——
    界面一直转圈，而实际上没有任何线程在干活（**进程没了，状态还在**）。
    计划也是同样的道理。
    """
    rows = db.query("SELECT id FROM lessons WHERE status = 'generating'")
    for row in rows:
        db.fail_lesson(row["id"], "上一次生成没有跑完（程序被关闭了）。重新生成即可。")

    plans = db.query("SELECT id FROM plans WHERE status = 'generating'")
    for row in plans:
        db.set_plan_status(
            row["id"], "failed", "上一次生成没有跑完（程序被关闭了）。重新开一次即可。"
        )
    return len(rows) + len(plans)


# --------------------------------------------------------------- 规范化


def normalize(payload: dict, target: str, want_examples: int) -> dict:
    """把模型输出整理成能落库的形态。

    这一层是防「不报错但结果不对」的：
    模型可能返回 7 条而不是 8 条、可能某条少了 scene_zh、
    可能把同一条重复两遍。这些都不会抛异常，只会安静地产生一个残缺的训练包。
    """
    if not isinstance(payload, dict):
        raise llm.LLMError("模型返回的不是一个 JSON 对象。", "点一次重试。")

    raw_examples = payload.get("examples")
    if not isinstance(raw_examples, list):
        raise llm.LLMError(
            "模型返回里没有 examples 列表。", "点一次重试；反复如此就换个模型。"
        )

    examples: list[dict] = []
    seen: set[str] = set()
    for item in raw_examples:
        if not isinstance(item, dict):
            continue
        sentence = str(item.get("sentence") or "").strip()
        if not sentence or sentence.lower() in seen:
            # 重复的例句几乎没用 —— 理论要的是「不同例子的重复」。
            continue
        seen.add(sentence.lower())

        scene_zh = str(item.get("scene_zh") or "").strip()
        variants = item.get("zh_variants")
        if not isinstance(variants, list):
            variants = []
        variants = [str(v).strip() for v in variants if str(v).strip()]
        if not variants and scene_zh:
            variants = [scene_zh]

        examples.append(
            {
                "sentence": sentence,
                "scene_en": str(item.get("scene_en") or "").strip(),
                "scene_zh": scene_zh,
                "zh_variants": variants,
                "register": str(item.get("register") or "").strip(),
            }
        )

    if not examples:
        raise llm.LLMError(
            "模型返回的例句全都是空的或重复的。", "点一次重试，或换一个更强的模型。"
        )

    swaps = []
    raw_swaps = payload.get("swaps")
    if isinstance(raw_swaps, list):
        for item in raw_swaps:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            if not role:
                continue
            swaps.append(
                {
                    "role": role,
                    "original": str(item.get("original") or "").strip(),
                    "candidates": _str_list(item.get("candidates")),
                    "samples": _str_list(item.get("samples")),
                }
            )

    return {
        "kind": str(payload.get("kind") or "").strip() or None,
        "ipa": str(payload.get("ipa") or "").strip(),
        "gloss": str(payload.get("gloss") or "").strip(),
        "note": str(payload.get("note") or "").strip(),
        "examples": examples[: max(want_examples, 1) * 2],
        "swaps": swaps,
    }


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


# --------------------------------------------------------------- 主流程


def generate(lesson_id: int) -> dict:
    """同步跑完整条流水线。调用方负责放到线程里。"""
    cfg = config.load()
    lesson = db.get_lesson(lesson_id)
    if lesson is None:
        raise KeyError(f"没有这个训练包：{lesson_id}")

    key = job_key_lesson(lesson_id)
    started = time.time()
    _set_job(key, status="running", step="正在让模型生成素材", done=0, total=0,
             lesson_id=lesson_id)

    try:
        want = int(cfg["study"].get("examples_per_lesson", 8))
        messages = prompts_build(cfg, lesson, want)

        _set_job(key, step="正在等模型返回例句")
        raw = llm.chat(
            base_url=cfg["llm"]["base_url"],
            api_key=cfg["llm"]["api_key"],
            model=cfg["llm"]["model"],
            messages=messages,
            temperature=float(cfg["llm"].get("temperature", 0.9)),
            max_tokens=int(cfg["llm"].get("max_tokens", 4096)),
            timeout=float(cfg["llm"].get("timeout", 120.0)),
        )
        payload = normalize(
            llm.extract_json(raw), lesson["target"], want
        )

        # 模型对 kind 的判断通常比本地猜的准，采信它的。
        if payload["kind"] in ("word", "phrase", "sentence"):
            db.execute(
                "UPDATE lessons SET kind = ? WHERE id = ?",
                (payload["kind"], lesson_id),
            )

        failures: list[str] = []
        total = len(payload["examples"])

        audio = _make_audio(key, payload["examples"], cfg["tts"], failures)
        images = _make_images(key, payload["examples"], cfg.get("images", {}), failures)

        db.finish_lesson(lesson_id, payload, audio, images)

        if failures:
            _write_log(lesson_id, failures)

        elapsed = round(time.time() - started, 1)
        _set_job(
            key,
            status="done",
            step=f"完成，用时 {elapsed} 秒",
            done=total,
            total=total,
            audio_failures=len(failures),
        )
        return {
            "lesson_id": lesson_id,
            "examples": total,
            "audio": len(audio),
            "images": len(images),
            "audio_failures": len(failures),
            "seconds": elapsed,
        }

    except Exception as exc:  # noqa: BLE001 - 要落库，所以必须全接住
        hint = getattr(exc, "hint", "")
        message = f"{exc}" + (f"（{hint}）" if hint else "")
        db.fail_lesson(lesson_id, message)
        _set_job(key, status="failed", step=message)
        _write_log(lesson_id, [message, traceback.format_exc()])
        raise


def _make_audio(
    key: str, examples: list[dict], tts_cfg: dict, failures: list[str]
) -> dict[int, str]:
    """逐条合成语音。**失败只记不抛** —— 另外几条还能练。"""
    audio: dict[int, str] = {}
    total = len(examples)
    for i, ex in enumerate(examples):
        _set_job(key, step=f"正在合成第 {i + 1}/{total} 条语音", done=i, total=total)
        try:
            audio[i] = tts.synthesize(
                ex["sentence"],
                engine=tts_cfg.get("engine", "edge"),
                voice=tts_cfg.get("voice", "en-US-AriaNeural"),
                rate=tts_cfg.get("rate", "+0%"),
                volume=tts_cfg.get("volume", "+0%"),
                openai_cfg=tts_cfg.get("openai", {}),
            )
            ex["audio_voice"] = tts_cfg.get("voice", "")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"语音 · {ex['sentence'][:40]}…：{exc}")
    return audio


def _make_images(
    key: str, examples: list[dict], img_cfg: dict, failures: list[str]
) -> dict[int, str]:
    """逐条配场景图。**失败只记不抛**，理由和音频完全一样。

    图片不是训练的必需品：阅读、听力、口语、打字、造句五条通道
    少了图都还能练。所以这里绝不允许它把一个包搞成「生成失败」。
    """
    source = (img_cfg.get("source") or "off").strip()
    out: dict[int, str] = {}
    if source in ("off", "none", ""):
        return out

    limit = int(img_cfg.get("max_per_lesson", 8) or 0)
    budget = len(examples) if limit <= 0 else min(limit, len(examples))
    gen_cfg = dict(img_cfg.get("llm") or {})
    timeout = float(img_cfg.get("timeout", 45.0))

    for i, ex in enumerate(examples[:budget]):
        scene_en = ex.get("scene_en", "")
        query = images_mod.build_query(scene_en, ex.get("sentence", ""))
        prompt = images_mod.build_prompt(scene_en, ex.get("sentence", ""))
        ex["image_query"] = query
        if not query and not prompt:
            continue
        _set_job(
            key,
            step=f"正在配第 {i + 1}/{budget} 张场景图"
                 + ("（模型生成，较慢）" if source == "llm" else ""),
            done=i,
            total=budget,
        )
        try:
            out[i] = images_mod.provide(
                query=query, prompt=prompt, source=source,
                gen_cfg=gen_cfg, timeout=timeout,
            )
        except images_mod.ImageError as exc:
            failures.append(f"配图 · {ex.get('sentence', '')[:40]}…：{exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"配图 · {ex.get('sentence', '')[:40]}…：{exc}")
    return out


# --------------------------------------------------------------- 学习计划


def normalize_outline(payload: dict, want: int) -> dict:
    """整理模型给的大纲。

    和 ``normalize`` 一样，这一层防的是「不报错但结果不对」：
    模型可能少给几条、可能把同一个词列两遍、可能把 target 写成一句
    语法说明（「一般现在时」）—— 那种条目后面没法当训练目标用，
    因为提示词会去给「一般现在时」找例句。
    """
    if not isinstance(payload, dict):
        raise llm.LLMError("模型返回的不是一个 JSON 对象。", "点一次重试。")

    raw = payload.get("outline")
    if not isinstance(raw, list):
        raise llm.LLMError(
            "模型返回里没有 outline 列表。", "点一次重试；反复如此就换个模型。"
        )

    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target") or "").strip()
        if not target or target.lower() in seen:
            continue
        seen.add(target.lower())

        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in ("word", "phrase", "sentence"):
            from . import prompts

            kind = prompts.classify_target(target)

        items.append({
            "target": target,
            "kind": kind,
            "domain": str(entry.get("domain") or "").strip(),
            "band": str(entry.get("band") or "").strip(),
            "why": str(entry.get("why") or "").strip(),
        })

    if not items:
        raise llm.LLMError(
            "模型给的大纲是空的。", "点一次重试，或换一个更强的模型。"
        )

    return {
        "note": str(payload.get("note") or "").strip(),
        "outline": items[: max(want, 1)],
    }


def draft_plan(plan_id: int) -> dict:
    """只出大纲，不动任何素材。让用户能先看到「要学什么」再决定开不开工。

    这一步存在的理由很实在：批量生成 20 个包要十几分钟。
    如果大纲是错的（跑题、太难、太简单），用户希望**在花掉那十几分钟之前**
    就发现，而不是生成了 20 个包之后再一个个删。
    """
    cfg = config.load()
    plan = db.get_plan(plan_id)
    if plan is None:
        raise KeyError(f"没有这个计划：{plan_id}")

    from . import prompts

    messages = prompts.build_syllabus_messages(
        language=plan["language"],
        goal_kind=plan["goal_kind"],
        goal_label=plan["goal_label"],
        goal_detail=plan["goal_detail"],
        domains=plan["domains"],
        count=int(plan["target_count"]),
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
    outline = normalize_outline(llm.extract_json(raw), int(plan["target_count"]))
    db.set_plan_outline(plan_id, outline["outline"])
    db.set_plan_status(plan_id, "draft")
    return {"plan_id": plan_id, **outline}


def generate_plan(plan_id: int) -> dict:
    """把计划里的大纲逐个变成训练包，并立刻开始生成素材。

    串行而不是并发：并发会**同时**打三个大模型请求，
    在「每天有额度」的免费档上很容易一起被限流 ——
    然后三个都失败，而串行只会慢一点。
    """
    cfg = config.load()
    plan = db.get_plan(plan_id)
    if plan is None:
        raise KeyError(f"没有这个计划：{plan_id}")

    key = job_key_plan(plan_id)
    started = time.time()
    _set_job(key, status="running", step="正在准备大纲", done=0, total=0, plan_id=plan_id)

    try:
        outline = plan["outline"]
        if not outline:
            _set_job(key, step="正在生成大纲")
            result = draft_plan(plan_id)
            outline = result["outline"]

        db.set_plan_status(plan_id, "generating")
        lesson_ids: list[int] = []
        total = len(outline)
        for i, entry in enumerate(outline):
            _set_job(
                key,
                step=f"正在建第 {i + 1}/{total} 个训练包：{entry['target']}",
                done=i,
                total=total,
            )
            lesson_id = db.create_lesson(
                entry["target"], entry.get("kind") or "word",
                plan["language"], plan_id=plan_id,
            )
            lesson_ids.append(lesson_id)
            try:
                generate(lesson_id)
            except Exception as exc:  # noqa: BLE001
                # 一个包失败不该把整份计划废掉 —— 其余的照样能用。
                _write_log(lesson_id, [f"计划 #{plan_id} 里的这一条失败：{exc}"])

        db.set_plan_status(plan_id, "ready")
        elapsed = round(time.time() - started, 1)
        _set_job(
            key, status="done", step=f"完成，用时 {elapsed} 秒", done=total, total=total
        )
        return {
            "plan_id": plan_id,
            "lessons": len(lesson_ids),
            "seconds": elapsed,
        }

    except Exception as exc:  # noqa: BLE001
        hint = getattr(exc, "hint", "")
        message = f"{exc}" + (f"（{hint}）" if hint else "")
        db.set_plan_status(plan_id, "failed", message)
        _set_job(key, status="failed", step=message)
        _append_log(f"plan-{plan_id}", [message, traceback.format_exc()])
        raise


def prompts_build(cfg: dict, lesson: dict, want: int) -> list[dict]:
    from . import prompts

    return prompts.build_messages(
        lesson["target"],
        kind=lesson.get("kind") or prompts.classify_target(lesson["target"]),
        language=lesson.get("language", "en"),
        examples=want,
        zh_variants=int(cfg["study"].get("zh_variants_per_example", 3)),
    )


def regenerate_audio(lesson_id: int) -> dict:
    """只补音频，不动例句。给「上次语音合成失败」留的补救入口。"""
    cfg = config.load()
    tts_cfg = cfg["tts"]
    examples = db.example_rows(lesson_id)
    key = job_key_lesson(lesson_id)
    _set_job(key, status="running", step="正在补语音", done=0, total=len(examples))

    ok, failed = 0, []
    for i, ex in enumerate(examples):
        _set_job(key, step=f"正在补第 {i + 1}/{len(examples)} 条", done=i)
        try:
            name = tts.synthesize(
                ex["sentence"],
                engine=tts_cfg.get("engine", "edge"),
                voice=tts_cfg.get("voice", "en-US-AriaNeural"),
                rate=tts_cfg.get("rate", "+0%"),
                volume=tts_cfg.get("volume", "+0%"),
                openai_cfg=tts_cfg.get("openai", {}),
            )
            db.execute(
                "UPDATE examples SET audio_path = ?, audio_voice = ? WHERE id = ?",
                (name, tts_cfg.get("voice", ""), ex["id"]),
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(f"语音 · {ex['sentence'][:40]}…：{exc}")

    _set_job(
        key,
        status="done",
        step=f"补了 {ok} 条语音，失败 {len(failed)} 条",
        done=len(examples),
    )
    return {"ok": ok, "failed": failed}


def regenerate_images(lesson_id: int) -> dict:
    """只补场景图，不动例句和语音。"""
    cfg = config.load()
    img_cfg = cfg.get("images", {})
    source = (img_cfg.get("source") or "off").strip()
    key = job_key_lesson(lesson_id)

    if source in ("off", "none", ""):
        return {
            "ok": 0,
            "failed": [],
            "skipped": "设置里关掉了场景配图。",
        }

    examples = db.example_rows(lesson_id)
    _set_job(key, status="running", step="正在补场景图", done=0, total=len(examples))

    ok, failed = 0, []
    for i, ex in enumerate(examples):
        _set_job(key, step=f"正在补第 {i + 1}/{len(examples)} 张图", done=i)
        scene_en = ex.get("scene_en", "")
        query = images_mod.build_query(scene_en, ex.get("sentence", ""))
        prompt = images_mod.build_prompt(scene_en, ex.get("sentence", ""))
        try:
            name = images_mod.provide(
                query=query, prompt=prompt, source=source,
                gen_cfg=dict(img_cfg.get("llm") or {}),
                timeout=float(img_cfg.get("timeout", 45.0)),
            )
            db.execute(
                "UPDATE examples SET scene_image = ?, image_query = ? WHERE id = ?",
                (name, query, ex["id"]),
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(f"配图 · {ex.get('sentence', '')[:40]}…：{exc}")

    _set_job(
        key,
        status="done",
        step=f"补了 {ok} 张场景图，失败 {len(failed)} 张",
        done=len(examples),
    )
    return {"ok": ok, "failed": failed}


def _write_log(lesson_id: int, lines: list[str]) -> None:
    """留一份排错用的流水。日志写不进去不该影响主流程。"""
    _append_log(f"lesson-{lesson_id}", lines)


def _append_log(name: str, lines: list[str]) -> None:
    try:
        path = log_dir() / f"{name}.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
