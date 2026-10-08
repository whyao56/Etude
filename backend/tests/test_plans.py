"""以目标为导向的学习计划（大纲 → 批量储备）。

这一组测试盯着的是一句产品判断：**目标不是难度标签。**

用户填「六级」的意思通常是「我要过这场考试」，而不是「请给我一张六级词表」。
把目标实现成一个难度下拉框，会得到一份「重要单词表」——
学习者最后能指着东西叫出名字，却说不出一句话，这整套方法就废了。

所以这里既测数据结构（``goal_kind`` 三值），也测提示词里那几条硬要求。
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------- 语言目录


def test_only_english_is_claimed_as_supported():
    """列出来 ≠ 支持。

    「还没开放」是个诚实的答复；「默默用英语的规则去生成法语素材」
    会产出一堆看起来像那么回事、实际上错的东西 —— 而用户没有能力分辨。
    """
    from app.prompts import LANGUAGES, build_messages, build_syllabus_messages, language_profile

    supported = [l for l in LANGUAGES if l["supported"]]
    assert [l["id"] for l in supported] == ["en"]
    assert len(LANGUAGES) >= 8, "主流语言要列出来，用户才知道以后会有"

    profile = language_profile("ja")
    assert profile["supported"] is False
    assert profile["note"], "没开放的语言要说明「为什么还没开放」"

    # 真正拦住的是提示词构建这一步 —— 这里才是会拿语言规则去生成素材的地方。
    with pytest.raises(NotImplementedError) as info:
        build_messages("こんにちは", kind="word", language="ja")
    assert "还没开放" in str(info.value)

    with pytest.raises(NotImplementedError):
        build_syllabus_messages(language="fr", count=5)


def test_an_unknown_language_code_does_not_silently_become_english():
    """代码写错时（``lang="eng"``）不能悄悄按英语办。"""
    from app.prompts import language_profile

    assert language_profile("eng")["supported"] is False


def test_an_unsupported_language_says_so_instead_of_generating(client):
    """接口层同样要拦住，而不是「英文兜底」。

    兜底比报错更糟：用户会得到一堆英语素材，以为这就是「日语功能」。
    """
    resp = client.post("/api/lessons", json={"target": "こんにちは", "language": "ja"})
    assert resp.status_code == 400
    assert "还没开放" in resp.json()["detail"]

    resp = client.post("/api/plans", json={"language": "fr", "goal_kind": "fluency"})
    assert resp.status_code == 400


# --------------------------------------------------------------- 目标类型


def test_goal_kinds_separate_what_you_want_from_how_hard_it_is():
    from app.prompts import GOAL_KINDS

    ids = [g["id"] for g in GOAL_KINDS]
    assert ids == ["fluency", "exam", "custom"]
    for goal in GOAL_KINDS:
        assert goal["label"] and goal["what"], "每一项都要在界面上说清自己是干什么的"


def test_exam_presets_carry_a_range_not_a_word_list():
    """考试预设只提供「范围参考」（CEFR 区间），不提供词表。

    一旦预设里带词表，提示词就有了一个「照着填」的捷径，
    最后生成的必然是词表 —— 而不再是「功能词 → 固定说法 → 句型 → 话题词」。
    """
    from app.prompts import EXAM_PRESETS

    labels = {p["label"] for p in EXAM_PRESETS}
    assert {"中考", "高考", "雅思"} <= labels
    assert any("四级" in l for l in labels) and any("六级" in l for l in labels)

    for preset in EXAM_PRESETS:
        assert preset["band"], f"{preset['label']} 缺 CEFR 区间"
        assert preset["detail"], f"{preset['label']} 缺一句说明"
        assert "words" not in preset and "vocabulary" not in preset


def test_the_syllabus_prompt_forbids_a_word_list():
    """这条是整个功能的核心约束，必须落在提示词里，而不是只写在注释里。"""
    from app.prompts import SYLLABUS_SYSTEM, build_syllabus_messages

    assert "FAILED syllabus" in SYLLABUS_SYSTEM
    assert "Never produce an exam word list" in SYLLABUS_SYSTEM

    messages = build_syllabus_messages(goal_kind="exam", goal_label="六级",
                                      goal_detail="B1", count=12)
    blob = "\n".join(m["content"] for m in messages)
    assert "六级" in blob
    assert "B1" in blob
    assert "Provide exactly 12 items" in blob


def test_a_fluency_goal_is_not_described_as_an_exam():
    """「真正学会一门语言」和「过一场考试」在提示词里必须是两条不同的说法。

    分成「我想用它干什么」而不是「我要考到几分」，
    是因为后者会让模型去收窄范围，前者会去铺开能力。
    """
    from app.prompts import build_syllabus_messages

    fluency = "\n".join(
        m["content"] for m in build_syllabus_messages(goal_kind="fluency", count=5)
    )
    assert "genuinely learn" in fluency
    assert "not to pass a test" in fluency

    exam = "\n".join(
        m["content"] for m in build_syllabus_messages(
            goal_kind="exam", goal_label="雅思", goal_detail="B2", count=5
        )
    )
    assert "preparing for" in exam
    assert "SHAPE of the syllabus must stay the same" in exam


def test_a_custom_goal_is_passed_through_verbatim():
    """用户自己写的目标要原样带进去 —— 转述一遍就会丢掉他要的那个侧面。"""
    from app.prompts import build_syllabus_messages

    detail = "我要能看懂英文的技术文档，还要能在代码评审里跟人argue"
    blob = "\n".join(
        m["content"] for m in build_syllabus_messages(
            goal_kind="custom", goal_detail=detail, count=5
        )
    )
    assert detail in blob
    assert "Do not silently turn this into a generic word list" in blob


# ------------------------------------------------------------ 大纲规范化


def _entry(target, kind="word", **rest):
    base = {"target": target, "kind": kind, "domain": "日常", "band": "A2",
            "why": "能多干一件事"}
    base.update(rest)
    return base


def test_outline_drops_duplicates_and_fixes_the_kind():
    """同一个词列两遍 = 后面生成两个一模一样的训练包。"""
    from app.pipeline import normalize_outline

    out = normalize_outline(
        {"note": "n", "outline": [
            _entry("appreciate"),
            _entry("Appreciate"),           # 大小写不同，仍然算重复
            _entry("I'd rather stay home", kind="瞎写的"),
            _entry(""),                     # 空的
            "不是字典",
        ]},
        10,
    )
    targets = [i["target"] for i in out["outline"]]
    assert targets == ["appreciate", "I'd rather stay home"]
    assert out["outline"][1]["kind"] == "sentence", "kind 认不出来时要自己判一次"


def test_outline_is_trimmed_to_the_requested_count():
    from app.pipeline import normalize_outline

    out = normalize_outline({"outline": [_entry(f"w{i}") for i in range(30)]}, 8)
    assert len(out["outline"]) == 8


def test_an_empty_outline_is_an_error_not_an_empty_plan():
    """返回空大纲的话，用户会得到一个「点了开始但什么都没发生」的计划。

    那种计划比报错更糟：界面上看着是成功的，只是永远没有内容。
    """
    from app.providers.llm import LLMError
    from app.pipeline import normalize_outline

    with pytest.raises(LLMError):
        normalize_outline({"outline": []}, 5)
    with pytest.raises(LLMError):
        normalize_outline({}, 5)
    with pytest.raises(LLMError):
        normalize_outline({"outline": "不是列表"}, 5)


# ----------------------------------------------------- 作业表不会被互相抢占


def test_lesson_and_plan_jobs_do_not_share_a_key():
    """训练包 3 和计划 3 是两件不同的活，不能互相覆盖进度。

    0.0.1 用的是裸整数 key，加上计划之后 3 和 3 会撞在一起 ——
    症状是「计划在生成，训练包却显示已失败」，而且只在特定编号下出现。
    """
    from app.pipeline import job_key_lesson, job_key_plan

    assert job_key_lesson(3) != job_key_plan(3)
    assert job_key_lesson(3) == "lesson:3"
    assert job_key_plan(3) == "plan:3"


# ---------------------------------------------------------------- 接口层


def test_plan_round_trip(client):
    """建计划 → 出大纲 → 改大纲 → 开工 → 每个条目变成一个训练包。"""
    created = client.post("/api/plans", json={
        "language": "en", "goal_kind": "fluency",
        "goal_label": "", "goal_detail": "",
        "domains": ["日常口语"], "target_count": 4,
    })
    assert created.status_code == 200, created.text
    body = created.json()
    plan_id = body["plan"]["id"]
    assert body["note"], "大纲要带一句「这份计划长什么样」"
    assert len(body["plan"]["outline"]) == 4
    assert body["plan"]["status"] == "draft"

    # 用户勾掉两条不要的 —— 这正是「先出大纲」的意义所在。
    kept = [body["plan"]["outline"][0], body["plan"]["outline"][2]]
    edited = client.put(f"/api/plans/{plan_id}/outline", json={"outline": kept})
    assert edited.status_code == 200
    assert len(edited.json()["outline"]) == 2

    started = client.post(f"/api/plans/{plan_id}/start")
    assert started.status_code == 200

    detail = client.get(f"/api/plans/{plan_id}").json()
    assert detail["status"] == "ready"
    assert detail["progress"]["total"] == 2
    assert detail["progress"]["ready"] == 2

    targets = [l["target"] for l in detail["lessons"]]
    assert targets == [i["target"] for i in edited.json()["outline"]]


def test_starting_a_plan_without_an_outline_says_what_to_do_first(client):
    from app import db

    plan_id = db.create_plan(language="en", goal_kind="fluency", goal_label="",
                             goal_detail="", domains=[], target_count=3)
    resp = client.post(f"/api/plans/{plan_id}/start")
    assert resp.status_code == 400
    assert "大纲" in resp.json()["detail"]


def test_a_custom_goal_needs_at_least_one_sentence(client):
    resp = client.post("/api/plans", json={
        "language": "en", "goal_kind": "custom", "goal_label": "", "goal_detail": "",
    })
    assert resp.status_code == 400
    assert "写一句话" in resp.json()["detail"]


def test_an_unknown_goal_kind_is_rejected(client):
    resp = client.post("/api/plans", json={"language": "en", "goal_kind": "随便"})
    assert resp.status_code == 400


def test_deleting_a_plan_takes_its_lessons_unless_asked_otherwise(client):
    """默认连训练包一起删。

    只删计划的话，那些训练包会以「不属于任何计划」的样子留在列表里 ——
    用户会以为删除没生效，然后再删一遍。
    """
    plan_id = client.post("/api/plans", json={
        "language": "en", "goal_kind": "fluency", "target_count": 3,
    }).json()["plan"]["id"]
    client.post(f"/api/plans/{plan_id}/start")
    assert client.get(f"/api/plans/{plan_id}").json()["progress"]["total"] == 3

    removed = client.delete(f"/api/plans/{plan_id}").json()
    assert removed["lessons_removed"] == 3
    assert client.get("/api/lessons").json()["lessons"] == []
    assert client.get(f"/api/plans/{plan_id}").status_code == 404


def test_keeping_the_lessons_is_possible_on_purpose(client):
    """「素材已经生成好了，只是不想要这份计划了」也得有出路。"""
    plan_id = client.post("/api/plans", json={
        "language": "en", "goal_kind": "fluency", "target_count": 2,
    }).json()["plan"]["id"]
    client.post(f"/api/plans/{plan_id}/start")

    removed = client.delete(f"/api/plans/{plan_id}?keep_lessons=true").json()
    assert removed["lessons_removed"] == 0
    lessons = client.get("/api/lessons").json()["lessons"]
    assert len(lessons) == 2
    assert all(l["plan_id"] == 0 for l in lessons), "要脱钩，不能留一个指向已删计划的 id"


def test_stale_generating_plans_are_recovered(isolated_data):
    """和训练包同理：程序被关掉时正在生成的计划，下次启动要收拾掉。"""
    from app import db, pipeline

    plan_id = db.create_plan(language="en", goal_kind="fluency", goal_label="",
                             goal_detail="", domains=[], target_count=3)
    db.set_plan_status(plan_id, "generating")
    assert pipeline.recover_stale() == 1

    plan = db.get_plan(plan_id)
    assert plan["status"] == "failed"
    assert "跑完" in plan["error"]


def test_generating_a_plan_reports_which_item_it_is_on(client):
    """20 个包要十几分钟。不说「正在做第几个」的话，用户无法分辨
    「在干活」和「卡住了」。"""
    from app import pipeline

    plan_id = client.post("/api/plans", json={
        "language": "en", "goal_kind": "fluency", "target_count": 3,
    }).json()["plan"]["id"]
    client.post(f"/api/plans/{plan_id}/start")

    job = client.get(f"/api/plans/{plan_id}/job").json()
    assert job["status"] in {"draft", "ready", "generating"}
    assert job["progress"]["total"] == 3
    assert pipeline.job_key_plan(plan_id) != pipeline.job_key_lesson(plan_id)


def test_catalog_exposes_everything_the_plan_form_needs(client):
    """界面上的下拉框全靠这一处 —— 少一项，那一项就永远只有默认值。"""
    data = client.get("/api/catalog").json()
    for key in ("languages", "goal_kinds", "exam_presets", "domain_presets",
                "image_sources", "image_presets"):
        assert data.get(key), f"/api/catalog 里缺 {key}"
    assert len(data["domain_presets"]) >= 5
