"""存储层的地基：连接、事务、schema 与迁移。

这个模块是唯一碰 sqlite3 连接的地方。上层（lessons/plans/cards）只调
query/one/execute，不自己开连接 —— 单连接 + RLock 的并发模型全靠这一条
不变式，破了就会出现「database is locked」这种只在用户机器上复现的错。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..paths import db_path, writable_root


_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None


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
    -- 1 = 暂时不练这张卡。Anki 里叫 suspend，是「这张卡我暂时不想看到」
    -- 的出口 —— 素材没错，只是现在不该占复习时间。
    suspended   INTEGER NOT NULL DEFAULT 0,
    -- 0.1.0：这张卡上「跟读」拿到过的最高分（0~100）。发音不是必修，
    -- 所以它只做展示与自身对比（同一个人的进步），不参与 SRS 调度。
    best_score  REAL    NOT NULL DEFAULT 0,
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
    -- 评分**之前**这张卡的 SRS 状态（JSON）。撤销要用。
    -- 存快照而不是靠 formula 倒推：间隔和难度都是乘法的累积结果，
    -- 反推要精确复现当时的浮点数，倒推错一点点就是「撤销后进度变了」。
    prev_state  TEXT    NOT NULL DEFAULT '',
    at          TEXT    NOT NULL
);

-- 跟读录音的**每一次**尝试（0.1.0）。
--
-- 为什么存每一次而不是「最好那次」：用户真正想看的是「上礼拜和今天的我」，
-- 只有留着原始记录才比得出来。而一次录音的分数受麦克风、环境噪声、
-- 当天嗓子影响很大，只看最高分会让人误判自己是不是在进步。
--
-- 音频文件按内容寻址存在 recordings/ 下，这里只存文件名 ——
-- 和语音、配图一个口径，换数据目录时不会留一堆死链。
CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 0 = 这次跟读不在某张卡上（比如训练包详情里的「试一试」）。
    card_id     INTEGER NOT NULL DEFAULT 0,
    lesson_id   INTEGER NOT NULL DEFAULT 0,
    example_id  INTEGER NOT NULL DEFAULT 0,
    channel     TEXT    NOT NULL DEFAULT 'speak',
    -- acoustic（只用声学指标）/ asr（词级对比）/ mixed（两者都有）
    mode        TEXT    NOT NULL DEFAULT 'acoustic',
    score       REAL    NOT NULL DEFAULT 0,
    seconds     REAL    NOT NULL DEFAULT 0,
    audio       TEXT    NOT NULL DEFAULT '',
    -- 每一项指标的实测值（JSON）：分数只给结论，明细要能说清「为什么是这个分」。
    detail      TEXT    NOT NULL DEFAULT '{}',
    at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_due ON cards (due_at, introduced);
CREATE INDEX IF NOT EXISTS idx_cards_lesson ON cards (lesson_id);
CREATE INDEX IF NOT EXISTS idx_examples_lesson ON examples (lesson_id, ord);
CREATE INDEX IF NOT EXISTS idx_reviews_at ON reviews (at);
CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews (card_id);
CREATE INDEX IF NOT EXISTS idx_lessons_plan ON lessons (plan_id);
CREATE INDEX IF NOT EXISTS idx_attempts_card ON attempts (card_id, id);
CREATE INDEX IF NOT EXISTS idx_attempts_example ON attempts (example_id, id);
"""


# 补列清单。``CREATE TABLE IF NOT EXISTS`` 对**已经存在**的表什么都不做 ——
# 于是新加的列在老库上永远不存在，症状是升级后第一次写入报「no such column」，
# 而且只在那条路径上炸。所以每次加列都要在这里登记一次。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("lessons", "plan_id", "INTEGER NOT NULL DEFAULT 0"),
    ("examples", "scene_image", "TEXT NOT NULL DEFAULT ''"),
    ("examples", "image_query", "TEXT NOT NULL DEFAULT ''"),
    # 0.0.3：卡片暂停（Anki 的 suspend）与撤销评分用的状态快照。
    ("cards", "suspended", "INTEGER NOT NULL DEFAULT 0"),
    ("reviews", "prev_state", "TEXT NOT NULL DEFAULT ''"),
    # 0.1.0：发音训练。attempts 是新表（走 SCHEMA），这里只补老表上的新列。
    ("cards", "best_score", "REAL NOT NULL DEFAULT 0"),
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


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback
