"""批量建包（``app.routes.batch``）的用例。

这里最值钱的两条：
- 同一份单子点两次，不该变成两套一模一样的包；
- 上一次失败的包应该**原地重试**，而不是在旁边再堆一个「失败」。
"""

from __future__ import annotations

import pytest

from app.routes.batch import MAX_BATCH, parse_targets


def test_parse_targets_handles_pasted_lists():
    text = """- kitchen
2. sink
· kettle

, the tap,
"""
    assert parse_targets(text, []) == ["kitchen", "sink", "kettle", "the tap"]


def test_parse_targets_dedupes_case_insensitively():
    assert parse_targets("Kitchen\nkitchen\nKITCHEN", []) == ["Kitchen"]


def test_parse_targets_keeps_order_and_drops_empties():
    assert parse_targets("a;b;;c;  ", ["d"]) == ["a", "b", "c", "d"]


def test_parse_targets_caps_the_batch():
    text = "\n".join(f"word{i}" for i in range(MAX_BATCH + 20))
    assert len(parse_targets(text, [])) == MAX_BATCH


def test_batch_creates_lessons(client):
    body = client.post(
        "/api/lessons/batch", json={"text": "kettle\nsink\nput the kettle on"}
    ).json()
    assert body["total"] == 3
    assert len(body["created"]) == 3
    assert body["skipped"] == []

    lessons = client.get("/api/lessons").json()["lessons"]
    targets = sorted(lesson["target"] for lesson in lessons)
    assert targets == ["kettle", "put the kettle on", "sink"]


def test_batch_skips_what_is_already_there(client):
    client.post("/api/lessons/batch", json={"text": "kettle"})
    again = client.post("/api/lessons/batch", json={"text": "kettle\nsink"}).json()
    assert [item["target"] for item in again["skipped"]] == ["kettle"]
    assert [item["target"] for item in again["created"]] == ["sink"]


def test_batch_retries_a_failed_lesson_in_place(client, monkeypatch):
    body = client.post("/api/lessons/batch", json={"text": "kettle"}).json()
    lesson_id = body["created"][0]["lesson_id"]

    from app import db

    db.fail_lesson(lesson_id, "桩：上一次失败了")
    again = client.post("/api/lessons/batch", json={"text": "kettle"}).json()
    assert [item["lesson_id"] for item in again["retried"]] == [lesson_id]
    # 没有多出一个新包。
    assert len(client.get("/api/lessons").json()["lessons"]) == 1


def test_batch_with_nothing_parseable_is_a_400(client):
    res = client.post("/api/lessons/batch", json={"text": "   \n , ； "})
    assert res.status_code == 400
    assert "没解析出" in res.json()["detail"]


def test_batch_rejects_unsupported_language(client):
    res = client.post("/api/lessons/batch", json={"text": "kettle", "language": "ja"})
    assert res.status_code == 400
    assert "还没开放" in res.json()["detail"]


def test_batch_draft_returns_targets_without_creating_lessons(client):
    body = client.post(
        "/api/lessons/batch/draft", json={"theme": "厨房", "count": 6}
    ).json()
    assert body["targets"]
    assert all(item["target"] for item in body["targets"])
    # 只出清单，不动数据库 —— 这是「先看再动手」的全部意义。
    assert client.get("/api/lessons").json()["lessons"] == []


@pytest.mark.parametrize("target", ["kettle", "put the kettle on"])
def test_batch_guesses_the_kind(client, target):
    body = client.post("/api/lessons/batch", json={"text": target}).json()
    lesson_id = body["created"][0]["lesson_id"]
    detail = client.get(f"/api/lessons/{lesson_id}").json()
    assert detail["kind"] in {"word", "phrase", "sentence"}
