"""跟读接口的用例（``app.routes.pronounce``）。

覆盖四件在真实使用里会出事的东西：
1. 录音字节进来 → 评分 → 落库 → 能取回录音；
2. 同一张卡第二次跟读时，「比第一次进步多少」是真的算出来的；
3. 文件名白名单（录音是用户数据，但它同样是一个文件系统参数）；
4. 没配转写服务时状态接口**说清楚现在处在哪一档**。
"""

from __future__ import annotations

from wavfmt import speech_then_pause, tone_wav


def _make_lesson(client) -> dict:
    created = client.post("/api/lessons", json={"target": "appreciate"}).json()
    lesson_id = created["lesson_id"]
    lesson = client.get(f"/api/lessons/{lesson_id}").json()
    return lesson


def test_example_attempt_scores_and_is_stored(client):
    lesson = _make_lesson(client)
    example_id = lesson["examples"][0]["id"]

    res = client.post(
        f"/api/examples/{example_id}/attempt?ref_seconds=1.5",
        content=tone_wav(seconds=1.4),
        headers={"Content-Type": "audio/wav"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["score"] > 0
    assert body["attempt"]["id"] > 0
    assert body["audio"].endswith(".wav")
    assert body["summary"]["count"] == 1

    # 录音真的落盘了，而且能按名字取回来。
    saved = client.get(f"/api/recordings/{body['audio']}")
    assert saved.status_code == 200
    assert saved.headers["content-type"].startswith("audio/wav")
    assert len(saved.content) == len(tone_wav(seconds=1.4))


def test_second_attempt_reports_the_delta(client):
    lesson = _make_lesson(client)
    example_id = lesson["examples"][0]["id"]

    first = client.post(
        f"/api/examples/{example_id}/attempt?ref_seconds=1.2",
        content=tone_wav(seconds=0.5),  # 只说了半句
        headers={"Content-Type": "audio/wav"},
    ).json()
    second = client.post(
        f"/api/examples/{example_id}/attempt?ref_seconds=1.2",
        content=tone_wav(seconds=1.2),  # 说完整了
        headers={"Content-Type": "audio/wav"},
    ).json()

    assert second["score"] > first["score"]
    assert second["summary"]["count"] == 2
    assert second["summary"]["delta"] == round(second["summary"]["latest"] - second["summary"]["first"], 1)


def test_pauses_are_measured_through_the_api(client):
    lesson = _make_lesson(client)
    example_id = lesson["examples"][0]["id"]
    body = client.post(
        f"/api/examples/{example_id}/attempt?ref_seconds=2.2",
        content=speech_then_pause(first=1.0, pause=0.4, second=0.8),
        headers={"Content-Type": "audio/wav"},
    ).json()
    fluency = next(item for item in body["items"] if item["id"] == "fluency")
    assert "1 次停顿" in fluency["value"]


def test_card_attempt_uses_the_sentence_from_the_database(client):
    lesson = _make_lesson(client)
    card_id = next(card["id"] for card in lesson["cards"] if card["channel"] == "speak")
    body = client.post(
        f"/api/cards/{card_id}/attempt?ref_seconds=1.5",
        content=tone_wav(seconds=1.4),
        headers={"Content-Type": "audio/wav"},
    ).json()
    assert body["mode"] == "acoustic"
    # 内容项是「未检查」，因为测试环境没配转写服务。
    content = next(item for item in body["items"] if item["id"] == "content")
    assert content["value"] == "未检查"


def test_empty_body_is_a_400_not_a_500(client):
    lesson = _make_lesson(client)
    example_id = lesson["examples"][0]["id"]
    res = client.post(
        f"/api/examples/{example_id}/attempt",
        content=b"",
        headers={"Content-Type": "audio/wav"},
    )
    assert res.status_code == 400
    assert "空" in res.json()["detail"]


def test_unknown_card_is_a_404(client):
    res = client.post(
        "/api/cards/9999/attempt", content=tone_wav(seconds=1.0)
    )
    assert res.status_code == 404


def test_recording_names_are_whitelisted(client):
    # 路径穿越会被 ASGI 层先规范化成 /config.json（于是 404）——
    # 这里要保证的是「带点带斜杠的名字绝不会落到 FileResponse 上」。
    res = client.get("/api/recordings/..%2F..%2Fconfig.json")
    assert res.status_code in (400, 404)
    res = client.get("/api/recordings/bad-name.wav")
    assert res.status_code == 400
    res = client.get("/api/recordings/0123456789abcdef.wav")
    assert res.status_code == 404


def test_pronounce_status_says_which_tier_we_are_in(client):
    body = client.get("/api/pronounce").json()
    assert body["enabled"] is True
    assert body["asr_ready"] is False
    assert "转写" in body["note"]


def test_asr_check_without_a_key_explains_what_to_do(client):
    res = client.post("/api/pronounce/check")
    assert res.status_code == 400
    payload = res.json()
    assert payload["kind"] == "not_configured"
    assert "设置" in payload["hint"]


def test_attempts_endpoint_lists_what_was_recorded(client):
    lesson = _make_lesson(client)
    example_id = lesson["examples"][0]["id"]
    client.post(
        f"/api/examples/{example_id}/attempt?ref_seconds=1.0",
        content=tone_wav(seconds=1.0),
        headers={"Content-Type": "audio/wav"},
    )
    body = client.get(f"/api/attempts?example_id={example_id}").json()
    assert body["summary"]["count"] == 1
    assert body["attempts"][0]["mode"] == "acoustic"
