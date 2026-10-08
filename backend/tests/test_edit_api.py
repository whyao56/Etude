"""0.0.3 新增接口的走接口验证。

db 层的用例已经证明了逻辑对不对；这一层盯的是**别的**东西：

- 参数形状。前端传 ``{"suspended": true}``、``{"register": "口语"}``
  这些名字，和后端的字段名必须对得上 —— 对不上时接口会「成功返回 200
  但什么都没改」，这是最难查的一类。
- 状态码。前端要根据 400 / 404 决定说哪句话。
- 出错之后数据没被改坏。
"""

from __future__ import annotations

import pytest


def _lesson_with_examples(client, n=2):
    """建一个真包（走接口，顺带把桩的语音和配图都跑一遍）。"""
    created = client.post("/api/lessons", json={"target": "appreciate", "kind": "word"}).json()
    lesson_id = created["id"]
    # 生成是丢进线程池的；client 用的 sync_pool 会同步跑完。
    return lesson_id


def _seed_directly(n=2) -> int:
    from app import db

    lesson_id = db.create_lesson("appreciate", "word")
    db.finish_lesson(
        lesson_id,
        {
            "gloss": "a gloss",
            "examples": [
                {"sentence": f"Example number {i}.", "scene_en": "scene",
                 "scene_zh": "场景", "zh_variants": ["场景"]}
                for i in range(n)
            ],
            "swaps": [],
        },
        {},
    )
    return lesson_id


def _cards(lesson_id):
    from app import db

    return db.query("SELECT * FROM cards WHERE lesson_id = ? ORDER BY id", (lesson_id,))


# ------------------------------------------------------------------ 撤销


def test_undo_route_returns_the_restored_state(client):
    lesson_id = _seed_directly(1)
    card_id = _cards(lesson_id)[0]["id"]
    client.post(f"/api/cards/{card_id}/grade", json={"grade": 3})

    data = client.post(f"/api/cards/{card_id}/undo").json()
    assert data["card_id"] == card_id
    assert data["restored"]["interval"] == 0.0


def test_undo_on_a_card_with_no_history_is_404(client):
    """没有可撤的东西时给 404，而不是 500 或者「假装成功」。

    「假装成功」最糟：前端会弹一句「已撤销」，而用户看到的卡片毫无变化。
    """
    lesson_id = _seed_directly(1)
    card_id = _cards(lesson_id)[0]["id"]
    assert client.post(f"/api/cards/{card_id}/undo").status_code == 404


def test_undo_does_not_delete_the_earlier_reviews(client):
    """撤一步只能删一条记录 —— 多删的话，用户连撤两次就退过头了。"""
    from app import db

    lesson_id = _seed_directly(1)
    card_id = _cards(lesson_id)[0]["id"]
    client.post(f"/api/cards/{card_id}/grade", json={"grade": 3})
    client.post(f"/api/cards/{card_id}/grade", json={"grade": 2})

    client.post(f"/api/cards/{card_id}/undo")
    assert db.one("SELECT COUNT(*) AS n FROM reviews WHERE card_id = ?", (card_id,))["n"] == 1


# ------------------------------------------------------- 单张改 SRS 参数


def test_patch_card_takes_suspended(client):
    """``suspended`` 这个名字必须和前端发出的完全一致。"""
    lesson_id = _seed_directly(1)
    card_id = _cards(lesson_id)[0]["id"]

    data = client.patch(f"/api/cards/{card_id}", json={"suspended": True}).json()
    assert data["card"]["suspended"] == 1

    data = client.patch(f"/api/cards/{card_id}", json={"suspended": False}).json()
    assert data["card"]["suspended"] == 0


def test_patch_card_takes_interval_and_ease(client):
    lesson_id = _seed_directly(1)
    card_id = _cards(lesson_id)[0]["id"]

    data = client.patch(f"/api/cards/{card_id}", json={"interval": 21, "ease": 2.4}).json()
    assert data["card"]["interval"] == 21.0
    assert abs(data["card"]["ease"] - 2.4) < 0.001


def test_patch_card_with_an_empty_body_is_400(client):
    """空 body 要报错。

    静默返回 200 的话，前端把字段名拼错（``suspend`` 少了 ed）时
    用户会看到「已保存」而什么都没变。
    """
    lesson_id = _seed_directly(1)
    card_id = _cards(lesson_id)[0]["id"]
    assert client.patch(f"/api/cards/{card_id}", json={}).status_code == 400


def test_patch_card_rejects_unknown_fields_instead_of_guessing(client):
    """拼错的字段名要被拒，不能被当成「空 body」以外的什么东西悄悄吞掉。"""
    lesson_id = _seed_directly(1)
    card_id = _cards(lesson_id)[0]["id"]
    resp = client.patch(f"/api/cards/{card_id}", json={"suspend": True})
    assert resp.status_code == 400


def test_patch_card_on_a_missing_card_is_404(client):
    assert client.patch("/api/cards/999999", json={"suspended": True}).status_code == 404


# ------------------------------------------------------------- 批量与推后


def test_batch_route_applies_to_every_id(client):
    lesson_id = _seed_directly(2)
    ids = [c["id"] for c in _cards(lesson_id)][:3]

    data = client.post("/api/cards/batch", json={"ids": ids, "patch": {"suspended": True}}).json()
    assert data["changed"] == 3

    from app import db

    rows = db.query("SELECT suspended FROM cards WHERE id IN (?,?,?)", tuple(ids))
    assert all(r["suspended"] == 1 for r in rows)


def test_batch_route_with_an_empty_patch_is_400(client):
    lesson_id = _seed_directly(1)
    ids = [c["id"] for c in _cards(lesson_id)][:2]
    assert client.post("/api/cards/batch", json={"ids": ids, "patch": {}}).status_code == 400


def test_push_route_takes_days(client):
    """返回的字段名是 ``changed``，前端读的也是 ``changed``。

    这两边对不上时的症状特别隐蔽：接口 200、数据也真的改了，
    但横幅上写「把 undefined 张卡往后推了 7 天」。
    """
    from app import db

    lesson_id = _seed_directly(1)
    ids = [c["id"] for c in _cards(lesson_id)][:2]

    data = client.post("/api/cards/push", json={"ids": ids, "days": 7}).json()
    assert data["changed"] == 2
    assert data["days"] == 7
    for cid in ids:
        assert db.one("SELECT interval FROM cards WHERE id = ?", (cid,))["interval"] == 7.0


# ------------------------------------------------------------- 卡片浏览器


def test_cards_list_route_filters_and_pages(client):
    lesson_id = _seed_directly(3)     # 3 × 5 = 15 张

    page = client.get("/api/cards", params={"limit": 4}).json()
    assert len(page["cards"]) == 4
    assert page["total"] == 15

    by_channel = client.get("/api/cards", params={"channel": "type"}).json()
    assert by_channel["total"] == 3
    assert all(c["channel"] == "type" for c in by_channel["cards"])

    by_lesson = client.get("/api/cards", params={"lesson_id": lesson_id}).json()
    assert by_lesson["total"] == 15

    searched = client.get("/api/cards", params={"q": "Example number 1"}).json()
    assert searched["total"] == 5


def test_cards_list_route_state_filter_uses_the_same_words_as_the_ui(client):
    """``state`` 的取值必须是界面下拉框里那几个。

    界面传 ``suspended`` 而后端只认 ``paused`` 的话，筛选会「什么都不返回」
    而用户以为是「一张都没暂停」。
    """
    lesson_id = _seed_directly(1)
    card_id = _cards(lesson_id)[0]["id"]
    client.patch(f"/api/cards/{card_id}", json={"suspended": True})

    assert client.get("/api/cards", params={"state": "suspended"}).json()["total"] == 1
    assert client.get("/api/cards", params={"state": "new"}).json()["total"] == 4
    assert client.get("/api/cards", params={"state": "all"}).json()["total"] == 5


# --------------------------------------------------------------- 例句编辑


def test_patch_example_takes_the_register_alias(client):
    """``register`` 这个名字在这个项目里是个坑。

    pydantic 的 ``BaseModel`` 上已经有一个 ``register`` 方法，
    直接叫 ``register`` 会盖掉它（pydantic 会警告）。所以模型里的字段
    叫 ``register_text``，对外的别名才是 ``register``。

    这条用例守的就是「别名有没有真的接上」—— 别名没接上的话，
    界面改了「语域」保存成功，读回来却是空的。
    """
    from app import db

    lesson_id = _seed_directly(1)
    ex_id = db.example_rows(lesson_id)[0]["id"]

    data = client.patch(f"/api/examples/{ex_id}", json={"register": "书面语"}).json()
    assert data["example"]["register"] == "书面语"


def test_patch_example_takes_the_three_text_fields(client):
    from app import db

    lesson_id = _seed_directly(1)
    ex_id = db.example_rows(lesson_id)[0]["id"]

    data = client.patch(
        f"/api/examples/{ex_id}",
        json={"sentence": "Rewritten.", "scene_en": "A new scene.", "scene_zh": "新场景。"},
    ).json()
    assert data["example"]["sentence"] == "Rewritten."
    assert data["example"]["scene_en"] == "A new scene."
    assert data["example"]["scene_zh"] == "新场景。"


def test_patch_example_takes_zh_variants_as_a_list(client):
    from app import db

    lesson_id = _seed_directly(1)
    ex_id = db.example_rows(lesson_id)[0]["id"]

    data = client.patch(
        f"/api/examples/{ex_id}", json={"zh_variants": ["甲", "乙"]}
    ).json()
    assert data["example"]["zh_variants"] == ["甲", "乙"]


def test_patch_missing_example_is_404(client):
    assert client.patch("/api/examples/999999", json={"sentence": "x"}).status_code == 404


def test_delete_example_route_cascades(client):
    from app import db

    lesson_id = _seed_directly(2)
    ex_id = db.example_rows(lesson_id)[0]["id"]

    data = client.delete(f"/api/examples/{ex_id}").json()
    assert data["deleted_cards"] == 5
    assert db.example_by_id(ex_id) is None


def test_add_example_route_lays_down_cards(client):
    lesson_id = _seed_directly(1)
    before = client.get("/api/cards", params={"lesson_id": lesson_id}).json()["total"]

    data = client.post(
        f"/api/lessons/{lesson_id}/examples",
        json={"sentence": "A brand new sentence.", "scene_en": "new scene"},
    ).json()
    assert data["example"]["sentence"] == "A brand new sentence."

    after = client.get("/api/cards", params={"lesson_id": lesson_id}).json()["total"]
    assert after == before + 5, "加了例句却没铺卡，这条例句会「看得见但练不到」"


def test_add_example_route_can_skip_media(client):
    """手工加例句可以只要文字，不等语音和配图。

    不给这个开关的话，加一条例句要等十几秒（画图慢），
    而用户可能只是想先把句子记下来。
    """
    lesson_id = _seed_directly(1)
    data = client.post(
        f"/api/lessons/{lesson_id}/examples",
        json={"sentence": "Text only.", "make_media": False},
    ).json()
    assert data["example"]["sentence"] == "Text only."
    assert data["failures"] == []


def test_add_example_route_rejects_a_blank_sentence(client):
    lesson_id = _seed_directly(1)
    assert client.post(
        f"/api/lessons/{lesson_id}/examples", json={"sentence": "   "}
    ).status_code == 400


def test_add_example_route_on_a_missing_lesson_is_404(client):
    assert client.post(
        "/api/lessons/999999/examples", json={"sentence": "x"}
    ).status_code == 404


def test_redo_audio_route_writes_a_real_file(client):
    """「重新生成语音」要真的落一个文件，不能只改数据库。"""
    from app import db
    from app.paths import audio_dir

    lesson_id = _seed_directly(1)
    ex_id = db.example_rows(lesson_id)[0]["id"]

    data = client.post(f"/api/examples/{ex_id}/audio").json()
    name = data["audio_path"]
    assert name
    assert (audio_dir() / name).exists() and (audio_dir() / name).stat().st_size > 0
    assert db.example_by_id(ex_id)["audio_path"] == name


def test_redo_image_route_writes_a_real_file(client):
    from app import db
    from app.paths import images_dir

    lesson_id = _seed_directly(1)
    ex_id = db.example_rows(lesson_id)[0]["id"]

    name = client.post(f"/api/examples/{ex_id}/image").json()["scene_image"]
    assert name
    assert (images_dir() / name).exists() and (images_dir() / name).stat().st_size > 0
    assert db.example_by_id(ex_id)["scene_image"] == name


def test_redo_audio_route_reports_failure_as_400(client, monkeypatch, isolated_data):
    """合成失败要给 400 + 一句人话，不是 500 堆栈。

    前端拿 400 才会把 ``detail`` 显示成提示；拿 500 只能弹「服务器错误」。
    """
    from app import db

    lesson_id = _seed_directly(1)
    ex_id = db.example_rows(lesson_id)[0]["id"]

    import app.providers.tts as tts_mod

    def boom(text, **kwargs):
        raise tts_mod.TTSError("桩：合成失败", "桩：换个音色。")

    monkeypatch.setattr("app.pipeline.tts.synthesize", boom)
    resp = client.post(f"/api/examples/{ex_id}/audio")
    assert resp.status_code == 400
    assert "桩：合成失败" in resp.json()["detail"]


def test_rebuild_cards_route_fills_gaps(client):
    from app import db

    lesson_id = _seed_directly(2)
    doomed = db.query("SELECT id FROM cards WHERE lesson_id = ? ORDER BY id LIMIT 2", (lesson_id,))
    for row in doomed:
        db.execute("DELETE FROM cards WHERE id = ?", (row["id"],))

    data = client.post(f"/api/lessons/{lesson_id}/rebuild-cards").json()
    assert data["created"] == 2
    assert client.get("/api/cards", params={"lesson_id": lesson_id}).json()["total"] == 10


def test_rebuild_cards_route_on_a_missing_lesson_is_404(client):
    assert client.post("/api/lessons/999999/rebuild-cards").status_code == 404
