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
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import db_path, writable_root

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

# 每条例句默认配几种通道训练。造句是对「整句」的操作，但它同样按例句铺开
# —— 理论要的是「不同例子的重复」，所以每一个例句都值得单独造句一次。
CHANNELS = ("read", "listen", "speak", "type", "build")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 学习计划：以目标为导向的批量素材储备。
-- 注意它存的是**目标**，不是一个难度标签 —— 见 prompts.SYLLABUS_SYSTEM。
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    language      TEXT    NOT NULL DEFAULT 'en',
    -- exam（应付某个考试）/ fluency（真正学会听说读写认）/ custom（自己写的目标）
    goal_kind     TEXT    NOT NULL DEFAULT 'fluency',
    goal_label    TEXT    NOT NULL DEFAULT '',
    goal_detail   TEXT    NOT NULL DEFAULT '',
    -- 用到的领域，JSON 数组：日常 / 职场 / 学术 / 旅行 …
    domains       TEXT    NOT NULL DEFAULT '[]',
    target_count  INTEGER NOT NULL DEFAULT 20,
    -- draft（大纲已生成，等着确认）/ generating / ready / failed
    status        TEXT    NOT NULL DEFAULT 'draft',
    error         TEXT    NOT NULL DEFAULT '',
    outline       TEXT    NOT NULL DEFAULT '[]',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

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
    -- 属于哪个学习计划。0 = 用户手动建的单个训练包。
    plan_id     INTEGER NOT NULL DEFAULT 0,
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
    -- 场景配图（相对 images 目录的文件名，内容寻址）＋ 当时用的检索/生成词。
    -- 存相对路径：存绝对路径的话，用户一搬数据目录就全成死链了。
    scene_image TEXT    NOT NULL DEFAULT '',
    image_query TEXT    NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_lessons_plan ON lessons (plan_id);
"""

# 0.0.1 → 0.0.2 需要补的列。
#
# 为什么要有这个而不是「让用户删库重建」：用户的复习进度就在这个库里。
# 升级一次就把进度清空，等于告诉他「别升级」。
#
# 为什么是 ALTER TABLE 而不是 CREATE TABLE IF NOT EXISTS：
# 后者对**已经存在**的表什么都不做 —— 于是新加的列在老库上永远不存在，
# 症状是升级后第一次写入报「no such column」，而且只在那条路径上炸。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("lessons", "plan_id", "INTEGER NOT NULL DEFAULT 0"),
    ("examples", "scene_image", "TEXT NOT NULL DEFAULT ''"),
    ("examples", "image_query", "TEXT NOT NULL DEFAULT ''"),
)


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
            # 顺序不能反：**先补列，再跑建表脚本**。
            #
            # 建表脚本里带着新加的索引（idx_lessons_plans 建在 lessons.plan_id 上）。
            # 对一个 0.0.1 留下的老库来说，`CREATE TABLE IF NOT EXISTS lessons`
            # 是空操作（表已经在了、但没有新列），紧接着那条建索引就报
            # `no such column: plan_id` —— 程序在**拿到第一个连接时就崩**，
            # 也就是每次启动都起不来。而开发机上永远是全新库，永远不出现。
            #
            # 反过来的顺序是安全的：老库先 ALTER 补列，再跑建表脚本；
            # 全新库里表还不存在，_migrate 会跳过（表结构由建表脚本负责）。
            _migrate(conn)
            conn.executescript(SCHEMA)
            conn.commit()
            _CONN = conn
        return _CONN


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """给老版本留下的库补上新列。返回补了哪些，方便打日志。

    只在 ``connect()`` 里跑一次。用 ``PRAGMA table_info`` 查实际结构，
    不去猜版本号 —— 版本号会说谎（用户可能从任意一个中间状态升上来），
    表结构不会。
    """
    applied: list[str] = []
    for table, column, decl in _MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            # 表还不存在（全新库），建表时已经带上这一列了。
            continue
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        applied.append(f"{table}.{column}")
    return applied


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


def images_expected() -> bool:
    """这个安装到底期不期待场景配图。

    ``images.source = "off"`` 是用户明确的选择（比如长期离线，或者就是
    不想要图）。这时候把「缺场景图」当成问题报出来是错的 —— 训练包详情页
    会永远挂着一句「素材还不齐」，还配一个按了也没用的「补场景图」按钮。
    报一个用户无法消除的警告，等于教会用户无视所有警告。
    """
    try:
        from .config import load as _load_config

        source = ((_load_config().get("images") or {}).get("source") or "").strip()
    except Exception:  # noqa: BLE001 —— 配置读不出来不该让 readiness 崩掉
        return True
    return source != "off"


def readiness(lesson_id: int, expect_images: bool | None = None) -> dict:
    """这个训练包「现在能不能真的练」。从实际内容算，不信 status。

    刻意分成两个字段，对应两种不同的界面行为：

    - ``ready``    —— 现在拿起来就能练（至少有例句）。有它才允许进入训练。
    - ``complete`` —— 素材齐全（音频在、配图也在）。它只控制**要不要给提示条**，
      不控制能不能练。

    把这两件事合成一个布尔值，就会出现「语音挂了所以整个包不能练」——
    而实际上读写和造句完全不受影响。

    ``expect_images`` 默认按当前配置推断：配图关了就不把「缺图」当问题。
    """
    from .paths import audio_dir, images_dir

    want_images = images_expected() if expect_images is None else bool(expect_images)

    examples = query(
        "SELECT id, sentence, scene_zh, audio_path, scene_image "
        "FROM examples WHERE lesson_id = ?",
        (lesson_id,),
    )
    missing_audio = []
    missing_image = []
    for row in examples:
        rel = (row.get("audio_path") or "").strip()
        # 「记录里有路径、磁盘上没有」是最阴的一种：界面会照常画出播放按钮/
        # 图片框，点了/加载了没反应。所以这里两个都真的去磁盘核对。
        if not rel or not (audio_dir() / rel).exists():
            missing_audio.append(row["id"])

        img = (row.get("scene_image") or "").strip()
        if not img or not (images_dir() / img).exists():
            missing_image.append(row["id"])

    lesson = get_lesson(lesson_id) or {}
    problems: list[str] = []
    if not (lesson.get("gloss") or "").strip():
        problems.append("缺少英英释义")
    if not examples:
        problems.append("没有例句")
    if missing_audio:
        problems.append(f"{len(missing_audio)} 条例句缺语音")
    if want_images and missing_image:
        problems.append(f"{len(missing_image)} 条例句缺场景图")

    return {
        "has_gloss": bool((lesson.get("gloss") or "").strip()),
        "example_count": len(examples),
        "audio_ready": len(examples) - len(missing_audio),
        "image_ready": len(examples) - len(missing_image),
        "missing_audio_ids": missing_audio,
        "missing_image_ids": missing_image,
        "images_expected": want_images,
        "ready": bool(examples),
        "complete": bool(examples) and not problems,
        "problems": problems,
    }


def stats() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "lessons": (one("SELECT COUNT(*) AS n FROM lessons") or {}).get("n", 0),
        "plans": (one("SELECT COUNT(*) AS n FROM plans") or {}).get("n", 0),
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


# ------------------------------------------------------------------- 计划
# 计划 = 一次「以目标为导向」的素材储备。它是**多份并存**的：
# 用户可能同时准备「六级」和「口语能聊」两套，各自独立推进。


def create_plan(
    *,
    language: str,
    goal_kind: str,
    goal_label: str,
    goal_detail: str,
    domains: list[str],
    target_count: int,
) -> int:
    ts = now()
    return execute(
        """
        INSERT INTO plans
            (language, goal_kind, goal_label, goal_detail, domains,
             target_count, status, outline, created_at, updated_at)
        VALUES (?,?,?,?,?,?, 'draft', '[]', ?, ?)
        """,
        (
            language,
            goal_kind,
            goal_label,
            goal_detail,
            json.dumps(domains, ensure_ascii=False),
            int(target_count),
            ts,
            ts,
        ),
    )


def get_plan(plan_id: int) -> dict | None:
    row = one("SELECT * FROM plans WHERE id = ?", (plan_id,))
    return _plan_out(row) if row else None


def list_plans() -> list[dict]:
    rows = query(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM lessons l WHERE l.plan_id = p.id) AS lesson_count,
               (SELECT COUNT(*) FROM lessons l
                 WHERE l.plan_id = p.id AND l.status = 'ready')      AS ready_count
          FROM plans p
      ORDER BY p.id DESC
        """
    )
    return [_plan_out(r) for r in rows]


def _plan_out(row: dict) -> dict:
    row = dict(row)
    row["domains"] = _loads(row.get("domains"), [])
    row["outline"] = _loads(row.get("outline"), [])
    return row


def set_plan_outline(plan_id: int, outline: list[dict]) -> None:
    execute(
        "UPDATE plans SET outline = ?, updated_at = ? WHERE id = ?",
        (json.dumps(outline, ensure_ascii=False), now(), plan_id),
    )


def set_plan_status(plan_id: int, status: str, error: str = "") -> None:
    execute(
        "UPDATE plans SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error[:2000], now(), plan_id),
    )


def delete_plan(plan_id: int, *, with_lessons: bool = True) -> int:
    """删计划。默认把它下面的训练包一起删 —— 留着的话它们会
    以「不属于任何计划」的样子出现在列表里，用户会以为删除没生效。"""
    removed = 0
    if with_lessons:
        for row in query("SELECT id FROM lessons WHERE plan_id = ?", (plan_id,)):
            delete_lesson(row["id"])
            removed += 1
    else:
        execute("UPDATE lessons SET plan_id = 0 WHERE plan_id = ?", (plan_id,))
    execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    with _LOCK:
        connect().commit()
    return removed


def plan_lesson_progress(plan_id: int) -> dict:
    row = one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'ready'    THEN 1 ELSE 0 END) AS ready,
               SUM(CASE WHEN status = 'failed'   THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN status = 'generating' THEN 1 ELSE 0 END) AS running
          FROM lessons WHERE plan_id = ?
        """,
        (plan_id,),
    ) or {}
    return {
        "total": row.get("total") or 0,
        "ready": row.get("ready") or 0,
        "failed": row.get("failed") or 0,
        "running": row.get("running") or 0,
    }


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
