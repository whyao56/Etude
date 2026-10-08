"""生成流水线：目标 → 例句素材 → 音频 → 可练的训练包。

两件必须做对的事：

**一、逐条失败不能拖垮整体。** 语音合成要联网，8 条里失败 2 条是常事。
如果任何一条失败就整体回滚，用户会得到「什么都没生成」——
而他本可以先练那 6 条。所以音频的失败是**记录**，不是**中断**。

**二、进度要能看见。** 生成一个包要几十秒。没有进度的话，用户面对的是
一个不动的界面，然后他会怀疑是卡死了（上一个项目里，「点了没反应」
是最难查的一类反馈）。所以这里维护一张作业表，界面轮询它。
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any

from . import config, db
from .paths import log_dir
from .providers import llm, tts

# 作业表。键是 lesson_id。进程内的，重启即清空 —— 这正是我们要的：
# 重启后那些 generating 状态的包会由 recover_stale() 收拾。
_JOBS: dict[int, dict] = {}
_JOBS_LOCK = threading.RLock()


def job_status(lesson_id: int) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(lesson_id)
        return dict(job) if job else None


def _set_job(lesson_id: int, **fields: Any) -> None:
    with _JOBS_LOCK:
        job = _JOBS.setdefault(lesson_id, {})
        job.update(fields)
        job["updated_at"] = time.time()


def recover_stale() -> int:
    """把上次崩溃/关窗留下的「生成中」标成失败。

    不做这一步的话，那些包会永远停在「生成中」——
    界面一直转圈，而实际上没有任何线程在干活（**进程没了，状态还在**）。
    """
    rows = db.query("SELECT id FROM lessons WHERE status = 'generating'")
    for row in rows:
        db.fail_lesson(row["id"], "上一次生成没有跑完（程序被关闭了）。重新生成即可。")
    return len(rows)


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

    started = time.time()
    _set_job(lesson_id, status="running", step="正在让模型生成素材", done=0, total=0)

    try:
        want = int(cfg["study"].get("examples_per_lesson", 8))
        messages = prompts_build(cfg, lesson, want)

        _set_job(lesson_id, step="正在等模型返回例句")
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

        audio: dict[int, str] = {}
        tts_cfg = cfg["tts"]
        total = len(payload["examples"])
        failures: list[str] = []
        for i, ex in enumerate(payload["examples"]):
            _set_job(
                lesson_id,
                step=f"正在合成第 {i + 1}/{total} 条语音",
                done=i,
                total=total,
            )
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
            except tts.TTSError as exc:
                # 记下而不是中断 —— 另外几条还能练。
                failures.append(f"{ex['sentence'][:40]}…：{exc}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{ex['sentence'][:40]}…：{exc}")

        db.finish_lesson(lesson_id, payload, audio)

        if failures:
            _write_log(lesson_id, failures)

        elapsed = round(time.time() - started, 1)
        _set_job(
            lesson_id,
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
            "audio_failures": len(failures),
            "seconds": elapsed,
        }

    except Exception as exc:  # noqa: BLE001 - 要落库，所以必须全接住
        hint = getattr(exc, "hint", "")
        message = f"{exc}" + (f"（{hint}）" if hint else "")
        db.fail_lesson(lesson_id, message)
        _set_job(lesson_id, status="failed", step=message)
        _write_log(lesson_id, [message, traceback.format_exc()])
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
    _set_job(lesson_id, status="running", step="正在补语音", done=0, total=len(examples))

    ok, failed = 0, []
    for i, ex in enumerate(examples):
        _set_job(lesson_id, step=f"正在补第 {i + 1}/{len(examples)} 条", done=i)
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
            failed.append(f"{ex['sentence'][:40]}…：{exc}")

    _set_job(
        lesson_id,
        status="done",
        step=f"补了 {ok} 条，失败 {len(failed)} 条",
        done=len(examples),
    )
    return {"ok": ok, "failed": failed}


def _write_log(lesson_id: int, lines: list[str]) -> None:
    """留一份排错用的流水。日志写不进去不该影响主流程。"""
    try:
        path = log_dir() / f"lesson-{lesson_id}.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
