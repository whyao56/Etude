"""0.0.3 新增的「Anki 式调控」：撤销、暂停、批量推后、手改 SRS、加删例句。

这组功能有一个共同的危险：**它们都能在没有报错的情况下把数据改错**。
撤销差一点会把进度清零，暂停差一点会让卡从队列里静悄悄地消失，
删除例句差一点会留下五张指向不存在例句的卡。

所以下面每一条都盯着「改坏了会怎样」，而不只是「改成功了没有」。
"""

from __future__ import annotations

import pytest


def _seed(lesson_id, sentences, audio_map=None, image_map=None):
    from app import db

    db.finish_lesson(
        lesson_id,
        {
            "gloss": "a gloss",
            "examples": [
                {"sentence": s, "scene_en": "scene", "scene_zh": "场景", "zh_variants": ["场景"]}
                for s in sentences
            ],
            "swaps": [],
        },
        audio_map or {},
        image_map,
    )


def _lesson(n=1, target="appreciate"):
    from app import db

    lesson_id = db.create_lesson(target, "word")
    _seed(lesson_id, [f"Example number {i}." for i in range(n)])
    return lesson_id


def _all_cards(lesson_id):
    """这个包的全部卡片 id，按 id 排好。

    每条例句铺 ``len(CHANNELS)`` 张卡，所以「n 条例句」是 ``n * 5`` 张。
    下面的用例一律从这里取数量，不写死 —— 写死的话，通道数一变
    就会以「像是逻辑坏了」的样子红掉。
    """
    from app import db

    return [r["id"] for r in db.query(
        "SELECT id FROM cards WHERE lesson_id = ? ORDER BY id", (lesson_id,)
    )]


# ---------------------------------------------------------------- 撤销评分


def test_undo_puts_the_srs_state_back_exactly(isolated_data):
    """撤销之后，卡片的 SRS 数字必须和**这一步之前**逐位相同。

    刻意用「评两次、分两次撤」而不是「评一次、撤一次」：
    只撤一次的话，就算实现是「把间隔减半」这种歪办法也可能碰巧对上。
    撤到第二步才是真正检验快照有没有被原样写回。

    也要留意这里的期望值：撤销是「退一步」，不是「回到最初」。
    撤一次应该等于**第一次评分之后**的状态 —— 写成「等于评分前」
    的话，用例会在实现完全正确的时候红掉。
    """
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    origin = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))

    db.grade_card(card["id"], 3)
    after_first = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    db.grade_card(card["id"], 2)
    assert db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))["interval"] != after_first["interval"]

    fields = ("interval", "ease", "reps", "lapses", "due_at", "introduced", "last_grade")

    db.undo_grade(card["id"])
    back = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    for f in fields:
        assert back[f] == after_first[f], f"撤一次之后 {f} 没有回到上一步：{after_first[f]} → {back[f]}"

    db.undo_grade(card["id"])
    home = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    for f in fields:
        assert home[f] == origin[f], f"撤两次之后 {f} 没有回到最初：{origin[f]} → {home[f]}"


def test_undo_deletes_the_review_row(isolated_data):
    """撤销要连复习记录一起删掉。

    留着记录的话 ``due_cards()`` 会照旧认为「今天已经练过一张」，
    用户就会莫名其妙地发现新卡配额少了 —— 进度看起来凭空被吃掉。
    """
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    db.grade_card(card["id"], 2)
    assert db.one("SELECT COUNT(*) AS n FROM reviews") ["n"] == 1

    db.undo_grade(card["id"])
    assert db.one("SELECT COUNT(*) AS n FROM reviews")["n"] == 0


def test_undo_twice_goes_back_two_steps(isolated_data):
    """连撤两次，要退回**第一次评分之前**，而不是原地打转。"""
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    origin = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))

    db.grade_card(card["id"], 3)
    db.grade_card(card["id"], 3)
    db.undo_grade(card["id"])
    db.undo_grade(card["id"])

    now = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    assert now["interval"] == origin["interval"]
    assert now["reps"] == origin["reps"]
    assert db.one("SELECT COUNT(*) AS n FROM reviews")["n"] == 0


def test_undo_refuses_when_there_is_no_snapshot(isolated_data):
    """老记录（0.0.2 之前）没有快照时，**拒绝执行**，而不是把卡清零。

    这是这条功能里最要命的一个岔路口：
    「没有快照」时退化成「清零」会让用户的复习进度凭空消失，
    而且不会报任何错 —— 他只会发现自己的卡突然全变成新卡了。
    """
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    db.grade_card(card["id"], 3)
    # 模拟一条 0.0.2 时代留下的记录：没有 prev_state。
    db.execute("UPDATE reviews SET prev_state = '' WHERE card_id = ?", (card["id"],))
    kept = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))

    with pytest.raises(KeyError):
        db.undo_grade(card["id"])

    after = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    assert after["interval"] == kept["interval"], "拒绝执行时一个字节都不该动"
    assert db.one("SELECT COUNT(*) AS n FROM reviews")["n"] == 1, "记录也不能删"


def test_undo_without_any_review_is_refused(isolated_data):
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    with pytest.raises(KeyError):
        db.undo_grade(card["id"])


def test_card_payload_grows_a_can_undo_flag_after_grading(client):
    """「能不能撤」由后端算，前端不猜。

    前端自己判断的话，判据（有没有 reviews、有没有快照）就成了两份实现，
    迟早会出现「撤销按钮亮着，按下去却报错」—— 正是 0.0.1 / 0.0.2
    反复栽的那个坑（按钮看着能用，实际不能用）。

    这条走真实接口，因为 ``can_undo`` 是在接口层拼上去的。
    """
    lesson_id = _lesson(1)
    card_id = _all_cards(lesson_id)[0]

    assert client.get(f"/api/cards/{card_id}").json()["can_undo"] is False
    client.post(f"/api/cards/{card_id}/grade", json={"grade": 2})
    assert client.get(f"/api/cards/{card_id}").json()["can_undo"] is True


def test_can_undo_is_false_for_a_review_without_a_snapshot(client):
    """老记录没有快照时，界面上的撤销按钮就不该亮。

    亮了但按下去报错，等于把「后端拒绝」这件事丢给用户去发现。
    """
    from app import db

    lesson_id = _lesson(1)
    card_id = _all_cards(lesson_id)[0]
    client.post(f"/api/cards/{card_id}/grade", json={"grade": 2})
    db.execute("UPDATE reviews SET prev_state = '' WHERE card_id = ?", (card_id,))

    assert client.get(f"/api/cards/{card_id}").json()["can_undo"] is False


# ------------------------------------------------------------------ 暂停


def test_suspended_cards_leave_the_queue(isolated_data):
    """暂停的卡不进队列 —— 这就是「今天不想看这一批」的实现方式。

    注意每条例句会铺五个通道的卡，所以「一个例子」是 5 张卡。
    这里一律按 ``CARD_COUNT`` 算，不写死数字 ——
    写死的话，将来通道数一变，这几条会以「看起来是逻辑坏了」的方式红掉。
    """
    from app import db

    lesson_id = _lesson(2)
    cards = _all_cards(lesson_id)
    queue = db.due_cards(limit=200, new_per_day=200)
    assert {c["id"] for c in queue} == set(cards)

    victim = cards[0]
    db.set_card_srs(victim, {"suspended": True})

    after = db.due_cards(limit=200, new_per_day=200)
    assert victim not in {c["id"] for c in after}
    assert {c["id"] for c in after} == set(cards) - {victim}


def test_resuming_brings_the_card_back(isolated_data):
    from app import db

    lesson_id = _lesson(1)
    card = _all_cards(lesson_id)[0]

    db.set_card_srs(card, {"suspended": True})
    assert card not in {c["id"] for c in db.due_cards(limit=200, new_per_day=200)}

    db.set_card_srs(card, {"suspended": False})
    assert card in {c["id"] for c in db.due_cards(limit=200, new_per_day=200)}


def test_suspended_cards_are_not_counted_as_due_now(isolated_data):
    """「今天到期」这个数字不能把暂停的算进去。

    算进去的话，用户暂停了几张卡，侧边栏还一直显示「待复习 N」——
    他会以为暂停没生效，然后再点几次。
    """
    from app import db

    lesson_id = _lesson(2)               # 2 条例句 × 5 个通道 = 10 张卡
    total = db.one("SELECT COUNT(*) AS n FROM cards WHERE lesson_id = ?", (lesson_id,))["n"]

    for card in db.due_cards(limit=200, new_per_day=200):
        db.grade_card(card["id"], 0)     # 判「没想起来」，10 分钟后到期
    db.execute("UPDATE cards SET due_at = ?", (db.now(),))

    assert db.stats()["due_now"] == total
    assert db.stats()["suspended"] == 0

    victim = db.query("SELECT id FROM cards ORDER BY id")[0]["id"]
    db.set_card_srs(victim, {"suspended": True})

    assert db.stats()["due_now"] == total - 1, "暂停的那张还被算在「今天到期」里"
    assert db.stats()["suspended"] == 1


def test_suspending_does_not_touch_the_interval(isolated_data):
    """只勾「暂停」不该顺手把间隔重置掉。

    实现上这很容易写错：把 patch 里的字段一股脑拼进 UPDATE，
    漏一个 ``if`` 就会把没传的字段写成 NULL 或 0。
    """
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    db.grade_card(card["id"], 3)
    before = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))

    db.set_card_srs(card["id"], {"suspended": True})
    after = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    assert after["interval"] == before["interval"]
    assert after["ease"] == before["ease"]
    assert after["reps"] == before["reps"]
    assert after["due_at"] == before["due_at"]


# ------------------------------------------------------- 手改 SRS 数字


def test_setting_the_interval_also_moves_the_due_date(isolated_data):
    """改间隔必须把到期时间一起算出来。

    只改 interval 不改 due_at 的话，界面上会出现
    「间隔 30 天，但明天到期」这种自相矛盾的卡 —— 用户没法判断该信哪个。
    """
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    db.set_card_srs(card["id"], {"interval": 30})

    row = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    assert abs(row["interval"] - 30) < 0.01
    days = (db.parse_ts(row["due_at"]) - db.parse_ts(db.now())).total_seconds() / 86400
    assert 29.5 < days < 30.5, f"到期时间没跟着走：{days} 天"


def test_ease_is_clamped(isolated_data):
    """难度夹在 1.3~3.5。超出去会让间隔指数爆炸或永远推不动。"""
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]

    db.set_card_srs(card["id"], {"ease": 99})
    assert db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))["ease"] == 3.5

    db.set_card_srs(card["id"], {"ease": 0.1})
    assert db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))["ease"] == 1.3


def test_negative_interval_does_not_go_into_the_past(isolated_data):
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    db.set_card_srs(card["id"], {"interval": -5})
    assert db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))["interval"] == 0.0


def test_resetting_to_new_card_clears_the_stats(isolated_data):
    """打回新卡时，间隔 / 次数 / 遗忘次数 / 上次评分要一起归零。

    只翻 introduced 的话，这张卡排到队列里时是「新卡」，
    却带着 20 天的间隔和 2.8 的难度 —— 第一眼就是自相矛盾的。

    注意「没想起来」会把 interval 设成 0（重来间隔是**分钟**级的，
    由 delay_minutes 承载），所以这里不能拿 interval 当「学过了」的证据，
    要看 reps 和 last_grade。
    """
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]

    for _ in range(3):
        db.grade_card(card["id"], 3)
    grown = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    assert grown["interval"] > 0 and grown["reps"] > 0, "连点三次「秒答」之后这张卡应该已经长起来了"

    db.grade_card(card["id"], 0)   # 判「没想起来」，lapses +1，进重来队列
    lapsed = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    assert lapsed["lapses"] > 0
    assert lapsed["introduced"] == 1

    db.set_card_srs(card["id"], {"introduced": False})
    back = db.one("SELECT * FROM cards WHERE id = ?", (card["id"],))
    assert back["introduced"] == 0
    assert back["interval"] == 0.0
    assert back["reps"] == 0
    assert back["lapses"] == 0
    assert back["last_grade"] is None


def test_srs_patch_with_nothing_in_it_is_refused(isolated_data):
    """空 patch 要报错，不能静默成功。

    静默成功的话，前端拼错字段名（比如 ``suspend`` 少了 ed）时
    用户会看到「保存成功」而什么都没变 —— 最难查的一类 bug。
    """
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    with pytest.raises(ValueError):
        db.set_card_srs(card["id"], {})
    with pytest.raises(ValueError):
        db.set_card_srs(card["id"], {"suspend": True})


# ------------------------------------------------------------ 批量往后推


def test_bulk_push_moves_each_card_from_its_own_due_date(isolated_data):
    """批量推后要从**各自当前**的到期时间往后推。

    统一设成 ``now + days`` 的话，一张压了三周的卡和一张今天到期的卡
    会被抹平成同一天 —— 用户攒了两周的间隔差就这么没了。
    """
    from app import db

    lesson_id = _lesson(2)
    cards = db.query("SELECT id FROM cards WHERE lesson_id = ? ORDER BY id", (lesson_id,))
    # 一张现在到期，一张推到两周后。
    db.set_card_srs(cards[0]["id"], {"interval": 0})
    db.set_card_srs(cards[1]["id"], {"interval": 14})

    db.push_cards([cards[0]["id"], cards[1]["id"]], 7)

    a = db.parse_ts(db.one("SELECT due_at FROM cards WHERE id = ?", (cards[0]["id"],))["due_at"])
    b = db.parse_ts(db.one("SELECT due_at FROM cards WHERE id = ?", (cards[1]["id"],))["due_at"])
    gap = (b - a).total_seconds() / 86400
    assert 13.5 < gap < 14.5, f"两张卡的间隔被抹平了，只剩 {gap} 天"


def test_bulk_push_skips_ids_that_do_not_exist(isolated_data):
    """批次里混进一个不存在的 id 不该让整批失败。"""
    from app import db

    _lesson(1)
    card = db.due_cards(limit=5, new_per_day=5)[0]
    db.push_cards([card["id"], 999999], 3)
    assert db.one("SELECT interval FROM cards WHERE id = ?", (card["id"],))["interval"] == 3.0


# ------------------------------------------------------- 卡片浏览器列表


def test_list_cards_reports_the_total_before_paging(isolated_data):
    """分页要能说出「共多少张」，不然界面上没法显示「显示 20 / 共 120 张」。"""
    from app import db

    _lesson(3)
    page = db.list_cards(limit=4, offset=0)
    assert len(page["cards"]) == 4
    assert page["total"] == 3 * len(db.CHANNELS)


def test_list_cards_pages_without_overlap(isolated_data):
    from app import db

    _lesson(3)
    first = db.list_cards(limit=5, offset=0)["cards"]
    second = db.list_cards(limit=5, offset=5)["cards"]
    assert {c["id"] for c in first}.isdisjoint({c["id"] for c in second})


def test_list_cards_filters_by_channel_and_state(isolated_data):
    from app import db

    lesson_id = _lesson(2)
    db.grade_card(db.list_cards(lesson_id=lesson_id, limit=1)["cards"][0]["id"], 2)
    db.set_card_srs(db.list_cards(lesson_id=lesson_id, limit=1)["cards"][0]["id"], {"suspended": True})

    only_type = db.list_cards(channel="type", limit=50)
    assert only_type["cards"] and all(c["channel"] == "type" for c in only_type["cards"])

    paused = db.list_cards(state="suspended", limit=50)
    assert paused["total"] == 1

    fresh = db.list_cards(state="new", limit=50)
    assert all(c["introduced"] == 0 for c in fresh["cards"])


def test_list_cards_search_matches_sentence_and_target(isolated_data):
    from app import db

    _lesson(2, target="peculiar")

    by_target = db.list_cards(search="peculiar", limit=50)
    assert by_target["total"] == 2 * len(db.CHANNELS)

    by_sentence = db.list_cards(search="Example number 1", limit=50)
    assert by_sentence["total"] == len(db.CHANNELS)


# ---------------------------------------------------------- 例句的增删改


def test_adding_an_example_also_lays_down_every_card(isolated_data):
    """手加一条例句，必须**同时**把五个通道的卡铺好。

    只插例句不插卡的话，界面上会多出一条「看得见但练不到」的例句：
    它出现在详情页里，点「练阅读」却什么都不发生。
    """
    from app import db

    lesson_id = _lesson(1)
    before = db.one("SELECT COUNT(*) AS n FROM cards WHERE lesson_id = ?", (lesson_id,))["n"]

    ex_id = db.add_example(lesson_id, "A brand new sentence.")
    cards = db.query("SELECT * FROM cards WHERE example_id = ?", (ex_id,))
    assert {c["channel"] for c in cards} == set(db.CHANNELS)
    assert db.one("SELECT COUNT(*) AS n FROM cards WHERE lesson_id = ?", (lesson_id,))["n"] == before + len(db.CHANNELS)


def test_a_manually_added_example_goes_last(isolated_data):
    """手工加的内容要排在最后，不能插到已有进度前面去。"""
    from app import db

    lesson_id = _lesson(2)
    ex_id = db.add_example(lesson_id, "Added later.")
    assert db.example_by_id(ex_id)["ord"] == 2


def test_blank_sentence_is_refused(isolated_data):
    from app import db

    lesson_id = _lesson(1)
    with pytest.raises(ValueError):
        db.add_example(lesson_id, "   ")


def test_deleting_an_example_cascades_to_its_cards(isolated_data):
    """删例句要连带删掉它的卡。

    ``cards.example_id`` 允许是 0（整包级的卡），所以没法用外键约束，
    必须手写级联。漏掉的话会留下五张指向不存在例句的卡 ——
    练习时点进去直接空屏。
    """
    from app import db

    lesson_id = _lesson(2)
    victim = db.example_rows(lesson_id)[0]

    result = db.delete_example(victim["id"])
    assert result["deleted_cards"] == len(db.CHANNELS)
    assert db.query("SELECT * FROM cards WHERE example_id = ?", (victim["id"],)) == []
    assert db.example_by_id(victim["id"]) is None
    assert db.one("SELECT COUNT(*) AS n FROM examples WHERE lesson_id = ?", (lesson_id,))["n"] == 1


def test_deleting_a_missing_example_raises(isolated_data):
    from app import db

    with pytest.raises(KeyError):
        db.delete_example(999999)


def test_updating_an_example_only_touches_the_whitelist(isolated_data):
    """改例句只认白名单字段。

    不是白名单的话，前端传个 ``{"id": 5}`` 就能把这条例句的 id 改掉。
    """
    from app import db

    lesson_id = _lesson(1)
    ex = db.example_rows(lesson_id)[0]
    db.update_example(ex["id"], {"sentence": "Rewritten.", "id": 999, "lesson_id": 999})

    after = db.example_by_id(ex["id"])
    assert after["sentence"] == "Rewritten."
    assert after["id"] == ex["id"]
    assert after["lesson_id"] == lesson_id


def test_zh_variants_are_stored_as_a_list_not_a_string(isolated_data):
    """中文说法进库是一段 JSON。

    直接当字符串写会把整个数组变成一个元素，
    界面上就只剩一行「["a","b"]」。
    """
    from app import db

    lesson_id = _lesson(1)
    ex = db.example_rows(lesson_id)[0]
    db.update_example(ex["id"], {"zh_variants": ["第一", "第二", "  ", "第三"]})

    got = db.example_by_id(ex["id"])["zh_variants"]
    assert got == ["第一", "第二", "第三"], "空白项要被丢掉"


def test_rebuild_fills_gaps_without_touching_existing_cards(isolated_data):
    """重建的语义是**补缺**，不是重置进度。"""
    from app import db

    lesson_id = _lesson(2)
    # 制造一个半成品：删掉两张卡。
    doomed = db.query("SELECT id FROM cards WHERE lesson_id = ? ORDER BY id LIMIT 2", (lesson_id,))
    for row in doomed:
        db.execute("DELETE FROM cards WHERE id = ?", (row["id"],))
    survivor_before = db.one(
        "SELECT * FROM cards WHERE lesson_id = ? ORDER BY id DESC LIMIT 1", (lesson_id,)
    )

    result = db.rebuild_cards(lesson_id)
    assert result["created"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM cards WHERE lesson_id = ?", (lesson_id,))["n"] == 2 * len(db.CHANNELS)

    survivor_after = db.one("SELECT * FROM cards WHERE id = ?", (survivor_before["id"],))
    assert survivor_after == survivor_before, "已存在的卡不该被动过"


def test_rebuild_is_a_no_op_when_nothing_is_missing(isolated_data):
    from app import db

    lesson_id = _lesson(2)
    assert db.rebuild_cards(lesson_id)["created"] == 0
