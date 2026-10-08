"""从 0.0.1 升上来。

0.0.2 改了库结构：``lessons`` 多一列 ``plan_id``，``examples`` 多两列
``scene_image`` / ``image_query``，另外多一张 ``plans`` 表。

0.0.1 的用户升级时会带着一个**已经在用的库**——里面有他的复习进度
（每张卡的间隔、难度系数、到期时间、历史评分）。这些东西丢了是不可接受的，
而且丢了之后不会有任何报错：界面照常，只是所有卡从零开始。

迁移的做法是「查实际表结构，缺哪列补哪列」，**不猜版本号**：
用户可能从任意一个中间状态升上来，版本号会说谎，表结构不会。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

# 0.0.1 的建表语句（逐字抄自 tag/commit 73bb380 的 app/db.py）。
#
# 刻意**不**从 git 现取：一条测试如果需要网络或仓库历史才能跑，
# 它在别人的机器上就会变成一条「跳过的测试」——而跳过的测试等于没有。
# 抄一份的成本是几十行，换来的是它永远能跑。
SCHEMA_0_0_1 = """
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
    scene_en    TEXT    NOT NULL DEFAULT '',
    scene_zh    TEXT    NOT NULL DEFAULT '',
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

OLD_TS = "2026-01-02T03:04:05+00:00"
FUTURE_TS = "2099-01-01T00:00:00+00:00"


@pytest.fixture
def old_database(isolated_data):
    """造一个「0.0.1 用了一阵子」的库。

    里面刻意留着一份**非默认**的复习进度：间隔 17.5 天、难度系数 2.31、
    已复习 6 次、忘过 1 次。迁移之后这几个数字必须原封不动 ——
    这才是「平滑升级」的真实含义。
    """
    path = isolated_data / "etude.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SCHEMA_0_0_1)
        conn.execute(
            "INSERT INTO lessons (id, target, kind, language, gloss, ipa, note,"
            " status, created_at, updated_at) VALUES"
            " (1, 'appreciate', 'word', 'en', 'to enjoy', '', '', 'ready', ?, ?)",
            (OLD_TS, OLD_TS),
        )
        for i, sentence in enumerate(["I appreciate it.", "She appreciates that."]):
            conn.execute(
                "INSERT INTO examples (lesson_id, ord, sentence, scene_en, scene_zh,"
                " zh_variants, register, audio_path, created_at)"
                " VALUES (1, ?, ?, 'A scene.', '一个场景。', ?, '日常口语', ?, ?)",
                (i, sentence, json.dumps(["一个场景。", "（短）场景"], ensure_ascii=False),
                 f"edge-{'%020d' % i}.mp3", OLD_TS),
            )
        # 两张卡：一张已经练过（有进度），一张还没引入。
        conn.execute(
            "INSERT INTO cards (lesson_id, example_id, channel, due_at, interval, ease,"
            " reps, lapses, introduced, last_grade, last_at)"
            " VALUES (1, 1, 'read', ?, 17.5, 2.31, 6, 1, 1, 2, ?)",
            (FUTURE_TS, OLD_TS),
        )
        conn.execute(
            "INSERT INTO cards (lesson_id, example_id, channel, due_at, interval, ease,"
            " reps, lapses, introduced) VALUES (1, 2, 'listen', ?, 0, 2.5, 0, 0, 0)",
            (FUTURE_TS,),
        )
        for grade in (3, 2, 0, 2, 1, 2):
            conn.execute(
                "INSERT INTO reviews (card_id, lesson_id, channel, grade, seconds, at)"
                " VALUES (1, 1, 'read', ?, 4.2, ?)",
                (grade, OLD_TS),
            )
        conn.commit()
    finally:
        conn.close()
    return isolated_data


def test_the_new_columns_are_added_without_touching_the_old_ones(old_database):
    from app import db

    db.connect()

    lesson_cols = {r["name"] for r in db.query("PRAGMA table_info(lessons)")}
    example_cols = {r["name"] for r in db.query("PRAGMA table_info(examples)")}
    assert "plan_id" in lesson_cols
    assert {"scene_image", "image_query"} <= example_cols

    # 老列一个都不能少 —— 迁移是 ALTER TABLE ADD COLUMN，不是重建。
    assert {"target", "gloss", "status", "created_at"} <= lesson_cols
    assert {"sentence", "zh_variants", "audio_path"} <= example_cols


def test_the_plans_table_shows_up_on_an_old_database(old_database):
    from app import db

    tables = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    assert "plans" in tables


def test_review_progress_survives_the_upgrade(old_database):
    """**这是整条迁移里唯一真正重要的断言。**

    这些数字丢了不会有任何报错：界面照常，只是所有卡从零开始，
    用户练了几个月的东西一夜之间回到第一天 —— 而且他没法知道为什么。
    """
    from app import db

    card = db.one("SELECT * FROM cards WHERE id = 1")
    assert card["interval"] == 17.5
    assert card["ease"] == 2.31
    assert card["reps"] == 6
    assert card["lapses"] == 1
    assert card["introduced"] == 1
    assert card["last_grade"] == 2
    assert card["due_at"] == FUTURE_TS

    assert len(db.query("SELECT * FROM reviews WHERE card_id = 1")) == 6


def test_old_content_is_readable_through_the_new_code(old_database):
    from app import db

    lesson = db.lesson_detail(1)
    assert lesson["target"] == "appreciate"
    assert lesson["gloss"] == "to enjoy"
    assert len(lesson["examples"]) == 2
    assert lesson["examples"][0]["zh_variants"] == ["一个场景。", "（短）场景"]

    payload = db.card_payload(db.one("SELECT id FROM cards WHERE channel = 'read'")["id"])
    assert payload["example"]["sentence"] == "I appreciate it."


def test_the_old_pack_is_reported_as_missing_pictures_not_as_broken(old_database):
    """老包没有场景图 —— 这是**事实**，但要说成「缺图」而不是「用不了」。

    这是 ``ready`` / ``complete`` 那对区分的实际价值：
    老用户升级之后打开自己的训练包，五条通道一条都不少，
    只是详情页多一句「可以补场景图」。
    """
    from app import db

    r = db.readiness(1)
    assert r["ready"] is True
    assert r["complete"] is False
    assert r["missing_image_ids"], "老包必然缺图"
    assert any("场景图" in p for p in r["problems"])
    assert r["audio_ready"] == 0, "老库里没有真的音频文件"


def test_the_review_queue_still_works_after_the_upgrade(old_database):
    """复习队列是用户每天都会碰的东西 —— 迁移之后它必须立刻能用。"""
    from app import db

    queue = db.due_cards(limit=10, new_per_day=10)
    assert queue, "已经引入且到期的卡应当还在队列里"
    assert all(c["channel"] in db.CHANNELS for c in queue)


def test_stats_and_plan_progress_work_on_an_old_database(old_database):
    from app import db

    stats = db.stats()
    assert stats["lessons"] == 1
    assert stats["plans"] == 0, "老库里没有计划，不能报错"


def test_migrating_twice_is_a_no_op(old_database):
    """每次启动都会跑一遍迁移。不幂等的话，第二次启动就崩了 ——
    而这恰好是「第一次用没问题，第二天打开就起不来」的经典形态。
    """
    from app import db

    db.connect()
    db.close()
    db.connect()

    applied = db._migrate(db.connect())
    assert applied == []

    assert len(db.query("SELECT * FROM cards")) == 2
    assert len(db.query("SELECT * FROM reviews")) == 6


def test_a_fresh_database_is_created_with_everything(old_database, tmp_path, monkeypatch):
    """反过来也要成立：完全没有库时，建出来的结构必须和迁移后的结构一致。

    两条路径（新建 / 迁移）产出不同结构的话，一个功能会在「老用户」和
    「新用户」身上表现不一样 —— 而这类差异在开发机上永远只暴露一半。
    """
    from app import db, paths

    tables = ("lessons", "examples", "cards")
    migrated = {
        table: {r["name"] for r in db.query(f"PRAGMA table_info({table})")}
        for table in tables
    }

    fresh = tmp_path / "fresh"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(fresh))
    db.close()
    try:
        created = {
            table: {r["name"] for r in db.query(f"PRAGMA table_info({table})")}
            for table in tables
        }
        assert (fresh / "etude.sqlite3").is_file()
    finally:
        db.close()
        # 让后面的用例（以及 autouse 的隔离夹具）回到原来的目录。
        monkeypatch.undo()

    assert created == migrated


def test_an_old_config_file_gets_the_new_sections(isolated_data):
    """配置也是同一个道理：老 config.json 里没有 ``images`` 段。

    读的时候要能自动补上默认值 —— 否则「升级之后配图功能悄悄是关的」，
    而用户根本不知道有这么个功能。
    """
    from app import config

    isolated_data.mkdir(parents=True, exist_ok=True)
    (isolated_data / "config.json").write_text(
        json.dumps({"llm": {"model": "my-old-model"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = config.load()
    assert loaded["llm"]["model"] == "my-old-model", "老设置不能被默认值盖掉"
    assert loaded["images"]["source"] in {"both", "search", "llm", "off"}
    assert "llm" in loaded["images"]
    assert isinstance(loaded["study"].get("plan_default_count"), int)
