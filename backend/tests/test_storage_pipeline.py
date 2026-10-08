"""存储层与生成流水线。

这里最要紧的一组用例在文件末尾：**``ready`` 与 ``complete`` 必须是两件事**。
它们的差集正是「看着配好了、用起来没反应」的全部来源。
"""

from __future__ import annotations

import json

import pytest


# ------------------------------------------------------------------ 建包


def test_new_lesson_starts_as_generating(isolated_data):
    from app import db

    lesson_id = db.create_lesson("appreciate", "word")
    lesson = db.get_lesson(lesson_id)
    assert lesson["status"] == "generating"
    assert lesson["error"] == ""


def test_cards_are_spread_over_examples_times_channels(isolated_data):
    """每个例句 × 每个通道 = 一张卡。

    理论要的是「用大量不同例子重塑大脑连接」，所以卡不是按目标词建的，
    是按**例句**铺的。
    """
    from app import db

    lesson_id = db.create_lesson("appreciate", "word")
    payload = {
        "gloss": "to enjoy",
        "examples": [{"sentence": f"S{i}", "zh_variants": ["x"]} for i in range(3)],
        "swaps": [],
    }
    db.finish_lesson(lesson_id, payload, {0: "a.mp3", 1: "b.mp3", 2: "c.mp3"})

    cards = db.query("SELECT * FROM cards WHERE lesson_id = ?", (lesson_id,))
    assert len(cards) == 3 * len(db.CHANNELS)
    assert {c["channel"] for c in cards} == set(db.CHANNELS)
    assert all(c["introduced"] == 0 for c in cards)


def test_regenerating_does_not_duplicate_content(isolated_data):
    """重新生成必须把旧例句、旧替换、旧卡片一起清掉。

    不清的话，同一个包会攒出两套例句和两套卡 ——
    用户会看到重复的句子，而且复习队列里会出现同一个句子的两份。
    """
    from app import db

    lesson_id = db.create_lesson("appreciate", "word")
    first = {"gloss": "g1", "examples": [{"sentence": "A"}], "swaps": []}
    db.finish_lesson(lesson_id, first, {})
    second = {"gloss": "g2", "examples": [{"sentence": "B"}, {"sentence": "C"}], "swaps": []}
    db.finish_lesson(lesson_id, second, {})

    assert len(db.example_rows(lesson_id)) == 2
    assert len(db.query("SELECT * FROM cards WHERE lesson_id = ?", (lesson_id,))) == 2 * len(db.CHANNELS)
    assert db.get_lesson(lesson_id)["gloss"] == "g2"


def test_deleting_a_lesson_removes_its_cards_and_examples(isolated_data):
    from app import db

    lesson_id = db.create_lesson("x", "word")
    db.finish_lesson(lesson_id, {"gloss": "g", "examples": [{"sentence": "S"}], "swaps": []}, {})
    db.delete_lesson(lesson_id)

    assert db.get_lesson(lesson_id) is None
    assert db.query("SELECT * FROM cards WHERE lesson_id = ?", (lesson_id,)) == []
    assert db.query("SELECT * FROM examples WHERE lesson_id = ?", (lesson_id,)) == []


def test_stale_generating_lessons_are_recovered(isolated_data):
    """程序被关掉时正在生成的包，下次启动要收拾掉。

    不收拾的话，那些包会永远停在「生成中」—— 界面一直转圈，
    而实际上**进程都没了，状态还在**。
    """
    from app import db, pipeline

    lesson_id = db.create_lesson("stuck", "word")
    assert db.get_lesson(lesson_id)["status"] == "generating"

    assert pipeline.recover_stale() == 1
    lesson = db.get_lesson(lesson_id)
    assert lesson["status"] == "failed"
    assert "跑完" in lesson["error"]


# ------------------------------------------------------ ready ≠ complete


def _seed(lesson_id, sentences, audio_map):
    from app import db

    db.finish_lesson(
        lesson_id,
        {
            "gloss": "a gloss",
            "examples": [{"sentence": s, "scene_zh": "场景", "zh_variants": ["场景"]}
                         for s in sentences],
            "swaps": [],
        },
        audio_map,
    )


def test_ready_and_complete_are_different_things(isolated_data):
    """这是整个项目最要紧的一条区分。

    ``ready`` = 现在拿起来就能练（有例句）
    ``complete`` = 素材齐全（语音也在）

    合成成一个是错的：语音挂了不代表读写和造句不能用。
    """
    from app import db
    from app.paths import audio_dir

    lesson_id = db.create_lesson("x", "word")
    _seed(lesson_id, ["One.", "Two."], {})

    r = db.readiness(lesson_id)
    assert r["ready"] is True, "有例句就该能练"
    assert r["complete"] is False, "没有语音就不算齐全"
    assert r["missing_audio_ids"], "应当报出哪几条缺语音"
    assert any("语音" in p for p in r["problems"])

    # 补上语音之后才齐全。
    names = []
    for i in range(2):
        name = f"edge-{'%020d' % i}.mp3"
        (audio_dir() / name).write_bytes(b"x")
        names.append(name)
    from app import db as db2

    for row, name in zip(db2.example_rows(lesson_id), names):
        db2.execute("UPDATE examples SET audio_path = ? WHERE id = ?", (name, row["id"]))

    r2 = db.readiness(lesson_id)
    assert r2["ready"] and r2["complete"]
    assert r2["problems"] == []


def test_a_card_whose_audio_file_vanished_is_reported(isolated_data):
    """数据库里有音频路径、磁盘上文件没了 —— 最阴的一种。

    界面会照常画出一个播放按钮，点了没反应。所以完整度必须**去磁盘核实**，
    不能只看数据库里那个字符串非空。
    """
    from app import db
    from app.paths import audio_dir

    lesson_id = db.create_lesson("x", "word")
    _seed(lesson_id, ["Only one."], {0: "edge-00000000000000000000.mp3"})
    path = audio_dir() / "edge-00000000000000000000.mp3"
    path.write_bytes(b"x")
    assert db.readiness(lesson_id)["complete"] is True

    path.unlink()  # 模拟被清理软件删掉 / 换了数据目录
    r = db.readiness(lesson_id)
    assert r["ready"] is True
    assert r["complete"] is False
    assert r["audio_ready"] == 0


def test_empty_lesson_is_not_ready(isolated_data):
    """没有例句就不能练 —— 这条允许拦人。"""
    from app import db

    lesson_id = db.create_lesson("nothing", "word")
    r = db.readiness(lesson_id)
    assert r["ready"] is False
    assert "没有例句" in r["problems"]


# ------------------------------------------------------------ 复习队列


def test_queue_introduces_new_cards_then_reviews_them(isolated_data):
    from app import db

    lesson_id = db.create_lesson("x", "word")
    _seed(lesson_id, ["One.", "Two."], {})

    queue = db.due_cards(limit=10, new_per_day=3)
    assert len(queue) == 3, "新卡要受每日配额限制"
    assert all(c["introduced"] == 0 for c in queue)

    db.grade_card(queue[0]["id"], 2)
    after = db.due_cards(limit=10, new_per_day=3)
    introduced = [c for c in after if c["introduced"] == 1]
    # 刚评「正常」的那张被推到明天，所以此刻到期的应当为空。
    assert introduced == []
    assert len(after) == 2, "配额里已经用掉一张"


def test_grading_writes_a_review_row(isolated_data):
    from app import db

    lesson_id = db.create_lesson("x", "word")
    _seed(lesson_id, ["One."], {})
    card = db.due_cards(limit=5, new_per_day=5)[0]

    db.grade_card(card["id"], 3, seconds=4.5)
    rows = db.query("SELECT * FROM reviews WHERE card_id = ?", (card["id"],))
    assert len(rows) == 1
    assert rows[0]["grade"] == 3
    assert rows[0]["seconds"] == 4.5
    assert rows[0]["channel"] == card["channel"]


def test_relearn_card_comes_back_in_the_same_session(isolated_data):
    """评「没想起来」之后，这张卡必须立刻还在队列里。"""
    from app import db

    lesson_id = db.create_lesson("x", "word")
    _seed(lesson_id, ["One."], {})
    card = db.due_cards(limit=5, new_per_day=5)[0]

    result = db.grade_card(card["id"], 0)
    assert result["next_in_minutes"] < 60

    # due_at 是 10 分钟后，此刻严格来说还没到期；但卡片状态必须是「已引入」。
    row = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    assert row["introduced"] == 1
    assert row["lapses"] == 1


def test_card_payload_carries_everything_the_ui_needs(isolated_data):
    """界面不该为了画一张卡再发五个请求 —— 少一次往返就少一处不一致。"""
    from app import db

    lesson_id = db.create_lesson("appreciate", "word")
    _seed(lesson_id, ["Hello there."], {})
    card = db.due_cards(limit=5, new_per_day=5)[0]

    payload = db.card_payload(card["id"])
    assert payload["card"]["id"] == card["id"]
    assert payload["lesson"]["target"] == "appreciate"
    assert payload["example"]["sentence"] == "Hello there."
    assert isinstance(payload["example"]["zh_variants"], list)
    assert isinstance(payload["swaps"], list)


def test_card_payload_of_a_missing_card_is_none(isolated_data):
    from app import db

    assert db.card_payload(999999) is None


def test_grading_a_missing_card_raises_keyerror(isolated_data):
    from app import db

    with pytest.raises(KeyError):
        db.grade_card(999999, 2)


def test_zh_variants_survive_a_roundtrip(isolated_data):
    """多种中文说法是理论要求的核心机制，不能在路上丢了。"""
    from app import db

    lesson_id = db.create_lesson("x", "word")
    db.finish_lesson(
        lesson_id,
        {
            "gloss": "g",
            "examples": [
                {"sentence": "S", "scene_zh": "基本说法", "zh_variants": ["短", "基本说法", "长一些的说法"]}
            ],
            "swaps": [],
        },
        {},
    )
    ex = db.example_rows(lesson_id)[0]
    assert ex["zh_variants"] == ["短", "基本说法", "长一些的说法"]


# ---------------------------------------------------------- 备份（§WAL）


def test_backup_produces_a_readable_single_file(isolated_data, tmp_path):
    """备份必须是**一个自洽的文件**。

    不用 shutil.copy：库开着 WAL，主文件里可能缺着还没 checkpoint 的写。
    复制出来那个文件打开是好的、内容却是旧的 —— 正是「不报错但结果不对」。
    """
    import sqlite3

    from app import db

    lesson_id = db.create_lesson("to-backup", "word")
    _seed(lesson_id, ["One."], {})

    dest = db.backup_to(tmp_path / "backup.sqlite3")
    assert dest.is_file()

    conn = sqlite3.connect(str(dest))
    try:
        rows = conn.execute("SELECT target FROM lessons").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["to-backup"]


# ------------------------------------------------------------ 规范化


def test_normalize_drops_duplicates_and_blank_sentences():
    """「不同例子的重复」是理论的核心；重复的例句几乎没用，应当丢掉。"""
    from app.pipeline import normalize

    payload = {
        "examples": [
            {"sentence": "Same.", "scene_zh": "a", "zh_variants": ["a"]},
            {"sentence": "same.", "scene_zh": "b", "zh_variants": ["b"]},
            {"sentence": "   ", "scene_zh": "c"},
            {"sentence": "Different.", "scene_zh": "d", "zh_variants": []},
        ]
    }
    out = normalize(payload, "x", 8)
    assert [e["sentence"] for e in out["examples"]] == ["Same.", "Different."]


def test_normalize_falls_back_when_variants_are_missing():
    """模型忘了给多种说法时，至少要有一种 —— 否则口语卡正面会是空的。

    这是典型的「不报错但结果不对」：界面画得出来，只是卡片正面一片空白。
    """
    from app.pipeline import normalize

    out = normalize(
        {"examples": [{"sentence": "Hi.", "scene_zh": "打招呼"}]}, "x", 8
    )
    assert out["examples"][0]["zh_variants"] == ["打招呼"]


def test_normalize_refuses_an_empty_result():
    from app.providers.llm import LLMError
    from app.pipeline import normalize

    with pytest.raises(LLMError):
        normalize({"examples": []}, "x", 8)

    with pytest.raises(LLMError):
        normalize({}, "x", 8)

    with pytest.raises(LLMError):
        normalize({"examples": "不是列表"}, "x", 8)


def test_normalize_keeps_swaps_readable():
    from app.pipeline import normalize

    out = normalize(
        {
            "examples": [{"sentence": "S", "zh_variants": ["x"]}],
            "swaps": [
                {"role": "subject", "original": "I", "candidates": ["We", "  ", "They"], "samples": ["We go."]},
                {"role": "", "original": "junk"},
                "不是字典",
            ],
        },
        "x",
        8,
    )
    assert len(out["swaps"]) == 1
    assert out["swaps"][0]["candidates"] == ["We", "They"]


@pytest.mark.parametrize(
    "target,expect",
    [
        ("appreciate", "word"),
        ("in the long run", "phrase"),
        ("get along with someone", "phrase"),
        ("I would rather stay home tonight.", "sentence"),
        ("She has been working here for years", "sentence"),
    ],
)
def test_target_classification(target, expect):
    from app.prompts import classify_target

    assert classify_target(target) == expect


def test_prompts_forbid_translation_shaped_scenes():
    """提示词里必须明确写出「场景不是翻译」。

    少了这句话，口语卡会退化成中译英 —— 训练者做的是翻译，
    而不是从场景直接到表达，这整套方法就废了。
    """
    from app.prompts import SYSTEM, build_messages

    assert "SITUATION" in SYSTEM
    assert "not a translation" in SYSTEM or "NOT a translation" in SYSTEM

    messages = build_messages("appreciate", kind="word", examples=6, zh_variants=2)
    user = messages[-1]["content"]
    assert "appreciate" in user
    assert "exactly 6 examples" in user


def test_prompts_reject_unsupported_languages_clearly():
    """加语言是个明确的功能边界，不能静默降级成英语。"""
    from app.prompts import build_messages

    with pytest.raises(NotImplementedError):
        build_messages("hola", language="es")
