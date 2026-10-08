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
    -- 1 = 暂时不练这张卡。Anki 里叫 suspend，是「这张卡我暂时不想看到」
    -- 的出口 —— 素材没错，只是现在不该占复习时间。
    suspended   INTEGER NOT NULL DEFAULT 0,
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

CREATE INDEX IF NOT EXISTS idx_cards_due ON cards (due_at, introduced);
CREATE INDEX IF NOT EXISTS idx_cards_lesson ON cards (lesson_id);
CREATE INDEX IF NOT EXISTS idx_examples_lesson ON examples (lesson_id, ord);
CREATE INDEX IF NOT EXISTS idx_reviews_at ON reviews (at);
CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews (card_id);
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
    # 0.0.3：卡片暂停（Anki 的 suspend）与撤销评分用的状态快照。
    ("cards", "suspended", "INTEGER NOT NULL DEFAULT 0"),
    ("reviews", "prev_state", "TEXT NOT NULL DEFAULT ''"),
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


# ------------------------------------------------------- 例句的手工编辑

# 允许手工改的字段白名单。
#
# 为什么要有白名单而不是「把 payload 里所有 key 都写进去」：
# 那个写法会让一个手滑的请求把 ``id`` 或 ``lesson_id`` 改掉 ——
# 例句换了主人，卡片却还挂在原训练包上。这正是「不报错但结果不对」。
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
                "SELECT COUNT(*) AS n FROM cards "
                "WHERE introduced = 1 AND suspended = 0 AND due_at <= ?",
                (now(),),
            )
            or {}
        ).get("n", 0),
        # 暂停的卡单独报一个数。不报的话，用户暂停了一批卡之后会发现
        # 「总数对不上」却找不到那部分去哪了 —— 被藏起来的数字最让人不安。
        "suspended": (
            one("SELECT COUNT(*) AS n FROM cards WHERE suspended = 1") or {}
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
         WHERE c.introduced = 1 AND c.due_at <= ? AND c.suspended = 0
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
             WHERE c.introduced = 0 AND c.suspended = 0
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
            INSERT INTO reviews
                (card_id, lesson_id, channel, grade, seconds, prev_state, at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (card_id, card["lesson_id"], card["channel"], grade, float(seconds),
             json.dumps(_srs_snapshot(card), ensure_ascii=False), ts),
        )
        conn.commit()

    return {
        "card_id": card_id,
        "interval_days": interval,
        "due_at": due.isoformat(timespec="seconds"),
        "next_in_minutes": delay_min,
    }


def _srs_snapshot(card: dict) -> dict:
    """评分**之前**的 SRS 状态。撤销时原样写回去。"""
    return {
        "interval": card["interval"],
        "ease": card["ease"],
        "reps": card["reps"],
        "lapses": card["lapses"],
        "due_at": card["due_at"],
        "introduced": card["introduced"],
        "last_grade": card["last_grade"],
        "last_at": card["last_at"],
    }


def last_review(card_id: int) -> dict | None:
    return one(
        "SELECT * FROM reviews WHERE card_id = ? ORDER BY id DESC LIMIT 1", (card_id,)
    )


def undo_grade(card_id: int) -> dict:
    """撤销这张卡最近一次评分，把 SRS 状态和复习记录一起退回去。

    为什么要连 ``reviews`` 那一行也删掉：那张表是「今天已经练了多少张」
    的依据（``due_cards`` 会数它来决定还放多少新卡）。评分撤了但记录留着，
    用户就会莫名其妙地发现「今天新卡配额被吃掉了」。

    快照缺失时**拒绝执行**而不是退化成「清零」—— 一条 0.0.2 之前
    留下的老记录没有快照，此时把卡清零等于凭空毁掉用户的复习进度。
    """
    row = last_review(card_id)
    if row is None:
        raise KeyError("这张卡还没有评分记录，没什么可撤的。")
    prev = _loads(row.get("prev_state"), None)
    if not isinstance(prev, dict) or "interval" not in prev:
        raise KeyError(
            "这条评分记录来自更早的版本，没有留下可回退的状态。"
            "（不是坏了 —— 只是当时还没存快照。）"
        )

    with _LOCK:
        conn = connect()
        conn.execute(
            """
            UPDATE cards
               SET interval = ?, ease = ?, reps = ?, lapses = ?, due_at = ?,
                   introduced = ?, last_grade = ?, last_at = ?
             WHERE id = ?
            """,
            (
                prev["interval"], prev["ease"], prev["reps"], prev["lapses"],
                prev["due_at"], prev["introduced"], prev["last_grade"],
                prev["last_at"], card_id,
            ),
        )
        conn.execute("DELETE FROM reviews WHERE id = ?", (row["id"],))
        conn.commit()
    return {"card_id": card_id, "undone_review_id": row["id"], "restored": prev}


def set_card_srs(card_id: int, patch: dict) -> dict:
    """老手直接改这张卡的复习参数。

    这是「Anki 的灵活度」里最容易被忽略、但实际最常用的一块：
    学习者知道某张卡自己已经烂熟了（或者根本不该现在练），
    宁可手动把它推到两周后，也不想靠连点四次「秒答」去逼近。

    可改：``interval``（天）、``due_at``（ISO 时间）、``ease``、
    ``suspended``。**只改传进来的字段**，其余原样不动 ——
    一个只勾了「暂停」的请求不该顺手把间隔重置。
    """
    card = one("SELECT * FROM cards WHERE id = ?", (card_id,))
    if card is None:
        raise KeyError(f"没有这张卡：{card_id}")

    sets: list[str] = []
    params: list[Any] = []
    if "interval" in patch and patch["interval"] is not None:
        days = max(0.0, float(patch["interval"]))
        sets.append("interval = ?")
        params.append(days)
        # 改间隔同时把到期时间一起算出来。两者分开改的话，
        # 界面上会出现「间隔 30 天，但明天到期」这种自相矛盾的卡。
        sets.append("due_at = ?")
        params.append(
            (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
        )
    if "due_at" in patch and patch["due_at"]:
        sets.append("due_at = ?")
        params.append(str(patch["due_at"]))
    if "ease" in patch and patch["ease"] is not None:
        # 难度夹在 1.3~3.5：超出去之后间隔会指数爆炸或永远推不动。
        sets.append("ease = ?")
        params.append(min(3.5, max(1.3, float(patch["ease"]))))
    if "suspended" in patch and patch["suspended"] is not None:
        sets.append("suspended = ?")
        params.append(1 if patch["suspended"] else 0)
    if "introduced" in patch and patch["introduced"] is not None:
        sets.append("introduced = ?")
        params.append(1 if patch["introduced"] else 0)
        if not patch["introduced"]:
            # 打回新卡必须把 stats 也归零，否则它在队列里排到「新卡」
            # 那一段时会带着旧的间隔和难度，第一眼就是自相矛盾的。
            sets.extend(["interval = ?", "reps = ?", "lapses = ?", "last_grade = ?"])
            params.extend([0.0, 0, 0, None])

    if not sets:
        raise ValueError("没有给出要改的字段。")

    params.append(card_id)
    execute(f"UPDATE cards SET {', '.join(sets)} WHERE id = ?", params)
    return one("SELECT * FROM cards WHERE id = ?", (card_id,)) or {}


def push_cards(card_ids: list[int], days: float) -> dict:
    """把一批卡整体往后推 ``days`` 天。

    「今天不想看这一批」比「逐张改日期」常见得多，所以单独给一个动作。
    从**各自当前的到期时间**往后推，而不是统一设成 now+days ——
    后者会把一张压了三周的卡和一张今天到期的卡抹平成同一天。
    """
    days = max(0.0, float(days))
    changed = 0
    with _LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN")
            for card_id in card_ids:
                row = conn.execute(
                    "SELECT due_at, interval FROM cards WHERE id = ?", (int(card_id),)
                ).fetchone()
                if row is None:
                    continue
                try:
                    base = parse_ts(row[0])
                except (TypeError, ValueError):
                    base = datetime.now(timezone.utc)
                due = base + timedelta(days=days)
                conn.execute(
                    "UPDATE cards SET due_at = ?, interval = ? WHERE id = ?",
                    (due.isoformat(timespec="seconds"), float(row[1] or 0) + days, int(card_id)),
                )
                changed += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"changed": changed, "days": days}


def set_cards_srs(card_ids: list[int], patch: dict) -> dict:
    """批量改卡。逐张走 ``set_card_srs``，**任一张失败就整体报错**。

    不吞掉单张的失败：批量操作里「改了一半」是最难查的状态，
    用户以为都改了，实际只有前 30 张生效。
    """
    done = 0
    for card_id in card_ids:
        set_card_srs(int(card_id), patch)
        done += 1
    return {"changed": done}


def list_cards(
    *,
    lesson_id: int = 0,
    channel: str = "",
    state: str = "all",
    search: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """卡片浏览器的数据源。

    「老手调控」没有这一层就只是嘴上说说 —— 一个只能看到「下一张该练什么」
    的界面，是没法做批量整理的。这里给的是**能筛、能翻页**的清单。
    """
    where: list[str] = ["1=1"]
    params: list[Any] = []
    if lesson_id:
        where.append("c.lesson_id = ?")
        params.append(int(lesson_id))
    if channel:
        where.append("c.channel = ?")
        params.append(channel)
    if state == "due":
        where.append("c.introduced = 1 AND c.suspended = 0 AND c.due_at <= ?")
        params.append(now())
    elif state == "new":
        where.append("c.introduced = 0 AND c.suspended = 0")
    elif state == "suspended":
        where.append("c.suspended = 1")
    elif state == "learning":
        where.append("c.introduced = 1 AND c.suspended = 0")
    if search.strip():
        where.append("(e.sentence LIKE ? OR l.target LIKE ?)")
        like = f"%{search.strip()}%"
        params.extend([like, like])

    clause = " AND ".join(where)
    total = (
        one(
            f"""SELECT COUNT(*) AS n FROM cards c
                  JOIN lessons l ON l.id = c.lesson_id
             LEFT JOIN examples e ON e.id = c.example_id
                 WHERE {clause}""",
            params,
        )
        or {}
    ).get("n", 0)

    rows = query(
        f"""
        SELECT c.id, c.lesson_id, c.example_id, c.channel, c.due_at, c.interval,
               c.ease, c.reps, c.lapses, c.introduced, c.suspended,
               c.last_grade, c.last_at,
               l.target, e.sentence
          FROM cards c
          JOIN lessons l ON l.id = c.lesson_id
     LEFT JOIN examples e ON e.id = c.example_id
         WHERE {clause}
      ORDER BY c.due_at ASC, c.id ASC
         LIMIT ? OFFSET ?
        """,
        params + [max(1, min(1000, int(limit))), max(0, int(offset))],
    )
    return {"cards": rows, "total": total}


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback
