"""SQLite 存储层。

设计上刻意用**扁平字典**出入，不引 ORM：
- 这些数据最终都要原样变成 JSON 给前端，字典省掉一次转换；
- 没有编译器的项目里，ORM 的字段名拼错只会在运行时炸。

一条重要的区分（来自上一个项目最贵的一类 bug）：
    ``status='ready'``      —— 生成任务跑完了，行写进去了
    ``readiness.ready``     —— **现在真的能练**
这两者的差集，就是「看着配好了、用起来没反应」的全部来源。
所以完整度一律从**实际内容**算（有没有例句、有没有音频、音频文件还在不在），
不信 status 字段。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import db_path

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

# 每条例句默认配几种通道训练。造句是对「整句」的操作，但它同样按例句铺开
# —— 理论要的是「不同例子的重复」，所以每一个例句都值得单独造句一次。
CHANNELS = ("read", "listen", "speak", "type", "build")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'word',
    language    TEXT    NOT NULL DEFAULT 'en',
    gloss       TEXT    NOT NULL DEFAULT '',
    ipa         TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'generating',
    error       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS examples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id   INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL DEFAULT 0,
    sentence    TEXT    NOT NULL,
    -- 场景描述：英文那份用于「想完之后核对」，中文那份是口语化的，不逐字对译。
    scene_en    TEXT    NOT NULL DEFAULT '',
    scene_zh    TEXT    NOT NULL DEFAULT '',
    -- 同一句话的多种中文说法（JSON 数组）。界面每次随机取一种 ——
    -- 这是「输出始终一致」的逆向应用：让大脑没法形成英文↔某个固定中文的关联。
    zh_variants TEXT    NOT NULL DEFAULT '[]',
    register    TEXT    NOT NULL DEFAULT '',
    audio_path  TEXT    NOT NULL DEFAULT '',
    audio_voice TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS swaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id   INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL,
    original    TEXT    NOT NULL DEFAULT '',
    candidates  TEXT    NOT NULL DEFAULT '[]',
    samples     TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id   INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    -- 0 表示这张卡作用于整个训练包而不是某一条例句。
    example_id  INTEGER NOT NULL DEFAULT 0,
    channel     TEXT    NOT NULL,
    due_at      TEXT    NOT NULL,
    interval    REAL    NOT NULL DEFAULT 0,
    ease        REAL    NOT NULL DEFAULT 2.5,
    reps        INTEGER NOT NULL DEFAULT 0,
    lapses      INTEGER NOT NULL DEFAULT 0,
    introduced  INTEGER NOT NULL DEFAULT 0,
    last_grade  INTEGER,
    last_at     TEXT    NOT NULL DEFAULT '',
    UNIQUE (lesson_id, example_id, channel)
);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     INTEGER NOT NULL,
    lesson_id   INTEGER NOT NULL,
    channel     TEXT    NOT NULL DEFAULT '',
    grade       INTEGER NOT NULL,
    seconds     REAL    NOT NULL DEFAULT 0,
    at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_due ON cards (due_at, introduced);
CREATE INDEX IF NOT EXISTS idx_cards_lesson ON cards (lesson_id);
CREATE INDEX IF NOT EXISTS idx_examples_lesson ON examples (lesson_id, ord);
CREATE INDEX IF NOT EXISTS idx_reviews_at ON reviews (at);
"""


def now() -> str:
    """统一的时间戳口径：UTC ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def connect() -> sqlite3.Connection:
    """拿连接。单进程单连接 + 一把锁 —— 本项目没有并发压力，
    用连接池反而多一类「连到旧文件」的坑。"""
    global _CONN
    with _LOCK:
        if _CONN is None:
            path = db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            conn.commit()
            _CONN = conn
        return _CONN


def close() -> None:
    """只给测试用：把连接丢掉，让下一次 connect 重新指向新的数据目录。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except sqlite3.Error:
                pass
            _CONN = None


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _LOCK:
        cur = connect().execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _LOCK:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.lastrowid or 0


def backup_to(dest: Path) -> Path:
    """把数据库导成**一个自洽的文件**。

    不用 ``shutil.copy``：库开着 WAL，主文件里可能缺着还没 checkpoint 的写。
    复制出来的那个文件打开后看起来是好的、内容却是旧的 —— 正是
    「不报错但结果不对」那一类。``VACUUM INTO`` 由 SQLite 自己保证一致性。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with _LOCK:
        # VACUUM INTO 的参数不能绑变量，只能拼；用 replace 转义单引号。
        safe = str(dest).replace("'", "''")
        connect().execute(f"VACUUM INTO '{safe}'")
        connect().commit()
    return dest


# --------------------------------------------------------------------- 读


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
    lesson["readiness"] = readiness(lesson_id)
    return lesson


def readiness(lesson_id: int) -> dict:
    """这个训练包「现在能不能真的练」。从实际内容算，不信 status。

    刻意分成两个字段，对应两种不同的界面行为：

    - ``ready``    —— 现在拿起来就能练（至少有例句）。有它才允许进入训练。
    - ``complete`` —— 素材齐全（音频也在）。它只控制**要不要给提示条**，
      不控制能不能练。

    把这两件事合成一个布尔值，就会出现「语音挂了所以整个包不能练」——
    而实际上读写和造句完全不受影响。
    """
    from .paths import audio_dir

    examples = query(
        "SELECT id, sentence, scene_zh, audio_path FROM examples WHERE lesson_id = ?",
        (lesson_id,),
    )
    missing_audio = []
    for row in examples:
        rel = (row.get("audio_path") or "").strip()
        if not rel:
            missing_audio.append(row["id"])
            continue
        if not (audio_dir() / rel).exists():
            # 记录里有音频路径、磁盘上没有 —— 这是最阴的一种：
            # 界面会照常画出一个播放按钮，点了没反应。
            missing_audio.append(row["id"])

    lesson = get_lesson(lesson_id) or {}
    problems: list[str] = []
    if not (lesson.get("gloss") or "").strip():
        problems.append("缺少英英释义")
    if not examples:
        problems.append("没有例句")
    if missing_audio:
        problems.append(f"{len(missing_audio)} 条例句缺语音")

    return {
        "has_gloss": bool((lesson.get("gloss") or "").strip()),
        "example_count": len(examples),
        "audio_ready": len(examples) - len(missing_audio),
        "missing_audio_ids": missing_audio,
        "ready": bool(examples),
        "complete": bool(examples) and not problems,
        "problems": problems,
    }


def stats() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "lessons": (one("SELECT COUNT(*) AS n FROM lessons") or {}).get("n", 0),
        "examples": (one("SELECT COUNT(*) AS n FROM examples") or {}).get("n", 0),
        "cards": (one("SELECT COUNT(*) AS n FROM cards") or {}).get("n", 0),
        "introduced": (
            one("SELECT COUNT(*) AS n FROM cards WHERE introduced = 1") or {}
        ).get("n", 0),
        "due_now": (
            one(
                "SELECT COUNT(*) AS n FROM cards WHERE introduced = 1 AND due_at <= ?",
                (now(),),
            )
            or {}
        ).get("n", 0),
        "reviews_today": (
            one(
                "SELECT COUNT(*) AS n FROM reviews WHERE substr(at, 1, 10) = ?",
                (today,),
            )
            or {}
        ).get("n", 0),
    }


# --------------------------------------------------------------------- 写


def create_lesson(
    target: str, kind: str = "word", language: str = "en"
) -> int:
    ts = now()
    return execute(
        """
        INSERT INTO lessons (target, kind, language, status, created_at, updated_at)
        VALUES (?, ?, ?, 'generating', ?, ?)
        """,
        (target.strip(), kind, language, ts, ts),
    )


def finish_lesson(lesson_id: int, payload: dict, audio: dict[int, str]) -> None:
    """把生成结果一次性写进去，并铺开卡片。

    整体一个事务：中途失败不留半个训练包
    —— 「例句子集 + 卡片全集」这种半成品，比彻底失败更难查。
    """
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
                         zh_variants, register, audio_path, audio_voice, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
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


# ------------------------------------------------------------------- 复习


def due_cards(limit: int = 40, new_per_day: int = 20) -> list[dict]:
    """今日队列：先到期卡，再按配额放新卡。

    刻意分成两段查询而不是一个 ORDER BY —— 「到期」和「新卡」用的是
    两种完全不同的界定（时间 / 配额），混在一起的排序键迟早会写错。
    """
    started = (
        one(
            "SELECT COUNT(*) AS n FROM reviews WHERE substr(at,1,10) = ?",
            (datetime.now(timezone.utc).date().isoformat(),),
        )
        or {}
    ).get("n", 0)

    introduced = query(
        """
        SELECT c.*, l.target, l.gloss FROM cards c
          JOIN lessons l ON l.id = c.lesson_id
         WHERE c.introduced = 1 AND c.due_at <= ?
      ORDER BY c.due_at ASC
         LIMIT ?
        """,
        (now(), limit),
    )
    room = max(0, limit - len(introduced))
    fresh_quota = max(0, new_per_day - started)
    fresh: list[dict] = []
    if room and fresh_quota:
        fresh = query(
            """
            SELECT c.*, l.target, l.gloss FROM cards c
              JOIN lessons l ON l.id = c.lesson_id
             WHERE c.introduced = 0
          ORDER BY c.lesson_id ASC, c.example_id ASC,
                   CASE c.channel WHEN 'read' THEN 0 WHEN 'listen' THEN 1
                                  WHEN 'speak' THEN 2 WHEN 'type' THEN 3
                                  ELSE 4 END
             LIMIT ?
            """,
            (min(room, fresh_quota),),
        )
    return introduced + fresh


def card_payload(card_id: int) -> dict | None:
    card = one("SELECT * FROM cards WHERE id = ?", (card_id,))
    if card is None:
        return None
    lesson = get_lesson(card["lesson_id"]) or {}
    out: dict[str, Any] = {"card": card, "lesson": lesson, "example": None, "swaps": []}
    if card["example_id"]:
        ex = one("SELECT * FROM examples WHERE id = ?", (card["example_id"],))
        if ex:
            ex["zh_variants"] = _loads(ex.get("zh_variants"), [])
            out["example"] = ex
    out["swaps"] = swap_rows(card["lesson_id"])
    return out


def grade_card(card_id: int, grade: int, seconds: float = 0.0) -> dict:
    """记一次复习结果并排下一张的到期时间。"""
    from .srs import schedule

    card = one("SELECT * FROM cards WHERE id = ?", (card_id,))
    if card is None:
        raise KeyError(f"没有这张卡：{card_id}")

    interval, ease, reps, lapses, delay_min = schedule(
        interval=card["interval"],
        ease=card["ease"],
        reps=card["reps"],
        lapses=card["lapses"],
        grade=grade,
    )
    due = datetime.now(timezone.utc) + timedelta(minutes=delay_min)
    ts = now()

    with _LOCK:
        conn = connect()
        conn.execute(
            """
            UPDATE cards
               SET interval = ?, ease = ?, reps = ?, lapses = ?,
                   due_at = ?, introduced = 1, last_grade = ?, last_at = ?
             WHERE id = ?
            """,
            (interval, ease, reps, lapses, due.isoformat(timespec="seconds"),
             grade, ts, card_id),
        )
        conn.execute(
            """
            INSERT INTO reviews (card_id, lesson_id, channel, grade, seconds, at)
            VALUES (?,?,?,?,?,?)
            """,
            (card_id, card["lesson_id"], card["channel"], grade, float(seconds), ts),
        )
        conn.commit()

    return {
        "card_id": card_id,
        "interval_days": interval,
        "due_at": due.isoformat(timespec="seconds"),
        "next_in_minutes": delay_min,
    }


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback
