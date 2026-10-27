"""训练包与例句。

「一条例句 × 五个通道 = 五张卡」这个主键结构在这里落地：add_example 铺卡、
delete_example 手写级联删卡、rebuild_cards 只补缺不动进度。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..paths import audio_dir, db_path, images_dir, writable_root
from .schema import (  # noqa: F401 - 每个模块都可能用到，统一引进来
    CHANNELS,
    _CONN,
    _LOCK,
    _loads,
    backup_to,
    close,
    connect,
    execute,
    now,
    one,
    parse_ts,
    query,
)


def get_lesson(lesson_id: int) -> dict | None:
    return one("SELECT * FROM lessons WHERE id = ?", (lesson_id,))


def list_lessons() -> list[dict]:
    return query(
        """
        SELECT l.*,
               (SELECT COUNT(*) FROM examples e WHERE e.lesson_id = l.id) AS example_count,
               (SELECT COUNT(*) FROM cards c WHERE c.lesson_id = l.id)    AS card_count,
               (SELECT COUNT(*) FROM cards c
                 WHERE c.lesson_id = l.id AND c.introduced = 1)          AS introduced_count
          FROM lessons l
      ORDER BY l.id DESC
        """
    )


def example_rows(lesson_id: int) -> list[dict]:
    rows = query(
        "SELECT * FROM examples WHERE lesson_id = ? ORDER BY ord, id", (lesson_id,)
    )
    for r in rows:
        r["zh_variants"] = _loads(r.get("zh_variants"), [])
    return rows


def swap_rows(lesson_id: int) -> list[dict]:
    rows = query("SELECT * FROM swaps WHERE lesson_id = ? ORDER BY id", (lesson_id,))
    for r in rows:
        r["candidates"] = _loads(r.get("candidates"), [])
        r["samples"] = _loads(r.get("samples"), [])
    return rows


_EXAMPLE_FIELDS = ("sentence", "scene_en", "scene_zh", "register", "scene_image", "image_query")


def example_by_id(example_id: int) -> dict | None:
    row = one("SELECT * FROM examples WHERE id = ?", (example_id,))
    if row:
        row["zh_variants"] = _loads(row.get("zh_variants"), [])
    return row


def update_example(example_id: int, patch: dict) -> dict:
    """改一条例句。只认白名单里的字段，其余忽略。

    ``zh_variants`` 单独处理：它进库是一段 JSON 文本，直接写字符串
    会把整个数组写成一个元素。
    """
    row = example_by_id(example_id)
    if row is None:
        raise KeyError(f"没有这条例句：{example_id}")

    sets: list[str] = []
    params: list[Any] = []
    for field in _EXAMPLE_FIELDS:
        if field in patch and patch[field] is not None:
            sets.append(f"{field} = ?")
            params.append(str(patch[field]))
    if "zh_variants" in patch and patch["zh_variants"] is not None:
        variants = [str(v).strip() for v in patch["zh_variants"] if str(v).strip()]
        sets.append("zh_variants = ?")
        params.append(json.dumps(variants, ensure_ascii=False))

    if not sets:
        raise ValueError("没有给出要改的字段。")

    params.append(example_id)
    execute(f"UPDATE examples SET {', '.join(sets)} WHERE id = ?", params)
    return example_by_id(example_id) or {}


def add_example(lesson_id: int, sentence: str, **fields: Any) -> int:
    """往训练包里手加一条例句，并把五个通道的卡片一起铺好。

    **卡片必须一起铺。** 只插例句不插卡片的话，界面上会多出一条
    「看得见但练不到」的例句 —— 它出现在详情页里，却永远不会进队列。
    这类「看着配好了、用起来没反应」是本项目最忌讳的状态。

    新卡的排位放在最后（``ord`` 取当前最大值 +1），
    所以手工加的内容不会插到已有进度前面去。
    """
    sentence = (sentence or "").strip()
    if not sentence:
        raise ValueError("例句不能是空的。")

    ts = now()
    with _LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN")
            nxt = (
                conn.execute(
                    "SELECT COALESCE(MAX(ord), -1) + 1 FROM examples WHERE lesson_id = ?",
                    (lesson_id,),
                ).fetchone() or [0]
            )[0]
            cur = conn.execute(
                """
                INSERT INTO examples
                    (lesson_id, ord, sentence, scene_en, scene_zh, zh_variants,
                     register, audio_path, audio_voice, scene_image, image_query,
                     created_at)
                VALUES (?,?,?,?,?,?,?,'','','','',?)
                """,
                (
                    lesson_id, int(nxt), sentence,
                    str(fields.get("scene_en") or ""),
                    str(fields.get("scene_zh") or ""),
                    json.dumps(
                        [str(v) for v in (fields.get("zh_variants") or []) if str(v).strip()],
                        ensure_ascii=False,
                    ),
                    str(fields.get("register") or ""),
                    ts,
                ),
            )
            example_id = int(cur.lastrowid)
            for channel in CHANNELS:
                conn.execute(
                    """
                    INSERT INTO cards
                        (lesson_id, example_id, channel, due_at, interval, ease,
                         reps, lapses, introduced)
                    VALUES (?,?,?,?,0,2.5,0,0,0)
                    """,
                    (lesson_id, example_id, channel, ts),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return example_id


def delete_example(example_id: int) -> dict:
    """删掉一条例句，连带它的卡片。

    ``cards`` 表没有对 examples 的外键（``cards.example_id`` 允许是 0，
    表示「作用于整个训练包」），所以**必须手写级联** ——
    靠外键的话这里会静默留下五张指向不存在例句的卡，练习时直接空屏。
    """
    row = example_by_id(example_id)
    if row is None:
        raise KeyError(f"没有这条例句：{example_id}")
    with _LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN")
            n = conn.execute(
                "DELETE FROM cards WHERE example_id = ?", (example_id,)
            ).rowcount
            conn.execute("DELETE FROM examples WHERE id = ?", (example_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"deleted_example": example_id, "deleted_cards": n}


def rebuild_cards(lesson_id: int) -> dict:
    """保证「每条例句 × 每个通道」都有卡，且**不碰**已存在的卡。

    给两种场景用：手工加过例句之后对不上账；或者某次生成中途失败
    留下了「有例句没卡」的半成品。已存在的卡一律不动 ——
    重建的语义是「补缺」，不是「重置进度」。
    """
    ts = now()
    created = 0
    with _LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN")
            ex_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM examples WHERE lesson_id = ?", (lesson_id,)
                )
            ]
            have = {
                (r[0], r[1])
                for r in conn.execute(
                    "SELECT example_id, channel FROM cards WHERE lesson_id = ?",
                    (lesson_id,),
                )
            }
            for ex_id in ex_ids:
                for channel in CHANNELS:
                    if (ex_id, channel) in have:
                        continue
                    conn.execute(
                        """
                        INSERT INTO cards
                            (lesson_id, example_id, channel, due_at, interval, ease,
                             reps, lapses, introduced)
                        VALUES (?,?,?,?,0,2.5,0,0,0)
                        """,
                        (lesson_id, ex_id, channel, ts),
                    )
                    created += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"created": created}


def lesson_detail(lesson_id: int) -> dict | None:
    lesson = get_lesson(lesson_id)
    if lesson is None:
        return None
    lesson["examples"] = example_rows(lesson_id)
    lesson["swaps"] = swap_rows(lesson_id)
    # 卡片清单也要给出来：界面上的「自由练」需要按 (例句 × 通道) 找到卡片 id，
    # 让它自己去推算 id 是错的 —— 推算依赖「插入顺序 = 卡片 id 连续」，
    # 而删掉重生成一次这个前提就不成立了（**不报错，只是练到别人身上**）。
    lesson["cards"] = query(
        "SELECT id, example_id, channel, introduced, due_at FROM cards "
        "WHERE lesson_id = ? ORDER BY id",
        (lesson_id,),
    )
    # 局部导入，理由同 readiness()：两个模块互相需要，顶层导入会成环。
    from .readiness import readiness

    lesson["readiness"] = readiness(lesson_id)
    return lesson


def create_lesson(
    target: str, kind: str = "word", language: str = "en", plan_id: int = 0
) -> int:
    ts = now()
    return execute(
        """
        INSERT INTO lessons
            (target, kind, language, plan_id, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'generating', ?, ?)
        """,
        (target.strip(), kind, language, int(plan_id or 0), ts, ts),
    )


def finish_lesson(
    lesson_id: int,
    payload: dict,
    audio: dict[int, str],
    images: dict[int, str] | None = None,
) -> None:
    """把生成结果一次性写进去，并铺开卡片。

    整体一个事务：中途失败不留半个训练包
    —— 「例句子集 + 卡片全集」这种半成品，比彻底失败更难查。
    """
    images = images or {}
    ts = now()
    with _LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                UPDATE lessons
                   SET gloss = ?, ipa = ?, note = ?,
                       status = 'ready', error = '', updated_at = ?
                 WHERE id = ?
                """,
                (
                    payload.get("gloss", ""),
                    payload.get("ipa", ""),
                    payload.get("note", ""),
                    ts,
                    lesson_id,
                ),
            )
            conn.execute("DELETE FROM examples WHERE lesson_id = ?", (lesson_id,))
            conn.execute("DELETE FROM swaps WHERE lesson_id = ?", (lesson_id,))
            conn.execute("DELETE FROM cards WHERE lesson_id = ?", (lesson_id,))

            example_ids: list[int] = []
            for i, ex in enumerate(payload.get("examples", [])):
                cur = conn.execute(
                    """
                    INSERT INTO examples
                        (lesson_id, ord, sentence, scene_en, scene_zh,
                         zh_variants, register, audio_path, audio_voice,
                         scene_image, image_query, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        lesson_id,
                        i,
                        ex.get("sentence", "").strip(),
                        ex.get("scene_en", ""),
                        ex.get("scene_zh", ""),
                        json.dumps(ex.get("zh_variants", []), ensure_ascii=False),
                        ex.get("register", ""),
                        audio.get(i, ""),
                        ex.get("audio_voice", ""),
                        images.get(i, ""),
                        ex.get("image_query", ""),
                        ts,
                    ),
                )
                example_ids.append(int(cur.lastrowid))

            for sw in payload.get("swaps", []):
                conn.execute(
                    """
                    INSERT INTO swaps (lesson_id, role, original, candidates, samples)
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        lesson_id,
                        sw.get("role", ""),
                        sw.get("original", ""),
                        json.dumps(sw.get("candidates", []), ensure_ascii=False),
                        json.dumps(sw.get("samples", []), ensure_ascii=False),
                    ),
                )

            for example_id in example_ids:
                for channel in CHANNELS:
                    conn.execute(
                        """
                        INSERT INTO cards
                            (lesson_id, example_id, channel, due_at, interval, ease,
                             reps, lapses, introduced)
                        VALUES (?,?,?,?,0,2.5,0,0,0)
                        """,
                        (lesson_id, example_id, channel, ts),
                    )

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def fail_lesson(lesson_id: int, message: str) -> None:
    execute(
        "UPDATE lessons SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
        (message[:2000], now(), lesson_id),
    )


def delete_lesson(lesson_id: int) -> None:
    execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
    execute("DELETE FROM cards WHERE lesson_id = ?", (lesson_id,))
    with _LOCK:
        connect().commit()
