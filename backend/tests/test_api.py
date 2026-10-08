"""HTTP 接口测试（走真实路由，只是模型和语音换成了桩）。"""

from __future__ import annotations

import pytest


# ------------------------------------------------------------- 自述接口


def test_health_can_identify_which_process_is_answering(client):
    """这个接口存在的唯一理由是：让「端口上有旧进程」一眼可辨。

    上个项目里用户报「检查更新显示 0.3.1，但界面是 0.4.0」——
    真因不是文案，是 0.3.1 时代的旧进程还在端口上答话。
    """
    from app import __version__

    data = client.get("/api/health").json()
    assert data["app"] == "Etude"
    assert data["version"] == __version__
    assert data["mode"] == "source"
    assert isinstance(data["pid"], int) and data["pid"] > 0
    assert data["started_at"]
    assert data["data_dir"]


def test_health_distinguishes_two_processes(monkeypatch):
    """两个不同的实例必须能被区分开 —— 靠 pid 和启动时刻。"""
    from app import api

    a = api.health()
    monkeypatch.setattr(api, "STARTED_AT", api.STARTED_AT - 3600)
    b = api.health()
    assert a["pid"] == b["pid"]
    assert a["started_at"] != b["started_at"], "光看版本号分不出旧进程，启动时刻才能"


def test_index_is_never_cached(client):
    """界面必须每次现取。

    缓存住了的话，用户拿到的还是旧前端 —— 而那正是「版本不一致」红条
    要检测的东西，检测本身就失效了。
    """
    resp = client.get("/")
    assert resp.status_code == 200
    assert "no-store" in resp.headers.get("cache-control", "")
    assert "Etude" in resp.text


def test_catalog_lists_channels_and_presets(client):
    data = client.get("/api/catalog").json()
    assert {"read", "listen", "speak", "type", "build"} == {c["id"] for c in data["channels"]}
    assert "deepseek" in data["llm_presets"]
    assert data["tts_voices"]["edge"]


# --------------------------------------------------------------- 生成


def test_full_round_trip(client):
    """生成 → 队列 → 取卡 → 评分，整条走一遍。"""
    created = client.post("/api/lessons", json={"target": "appreciate"})
    assert created.status_code == 200
    lesson_id = created.json()["lesson_id"]

    lesson = client.get(f"/api/lessons/{lesson_id}").json()
    assert lesson["status"] == "ready"
    assert lesson["readiness"]["ready"] is True
    assert len(lesson["examples"]) == 4
    assert len(lesson["cards"]) == 4 * 5

    queue = client.get("/api/queue?limit=10").json()
    assert len(queue["cards"]) >= 1

    card = client.get(f"/api/cards/{queue['cards'][0]['id']}").json()
    assert card["example"]["sentence"]
    assert set(card["srs_preview"]) == {"0", "1", "2", "3"}

    graded = client.post(f"/api/cards/{card['card']['id']}/grade", json={"grade": 2})
    assert graded.status_code == 200
    assert graded.json()["interval_days"] > 0


def test_creating_the_same_target_twice_reuses_the_running_one(client):
    """同一个目标已经在生成时，不要开第二份。

    开了的话用户会得到两个一模一样的包，卡片也是两份。
    """
    from app import db

    lesson_id = db.create_lesson("duplicated", "word")  # 手工造一个 generating 的
    resp = client.post("/api/lessons", json={"target": "duplicated"})
    assert resp.json()["reused"] is True
    assert resp.json()["lesson_id"] == lesson_id


def test_blank_target_is_rejected(client):
    assert client.post("/api/lessons", json={"target": "   "}).status_code == 400


def test_generation_failure_is_recorded_with_a_next_step(client, monkeypatch):
    """生成失败必须落库、带方向，而不是留一个转不完的圈。

    这一条对应的是「静默失败」那一类：如果失败只写进日志，
    用户看到的是永远「生成中」的界面。
    """
    from app.providers.llm import LLMError

    def boom(**kwargs):
        raise LLMError("密钥没通过验证（401）。", "检查 API Key 是否复制完整。", kind="auth")

    monkeypatch.setattr("app.pipeline.llm.chat", boom)

    lesson_id = client.post("/api/lessons", json={"target": "will-fail"}).json()["lesson_id"]
    job = client.get(f"/api/lessons/{lesson_id}/job").json()
    assert job["status"] == "failed"
    assert "401" in job["error"]
    assert "API Key" in job["error"], "错误里必须带上「下一步做什么」"


def test_partial_tts_failure_still_produces_a_usable_pack(client, stub_tts_failing):
    """语音全挂，例句照样要有 —— 读写和造句不依赖语音。

    这条拦的是「一条失败就整体回滚」：那样用户会得到「什么都没生成」，
    而他本来可以先练能练的那部分。
    """
    lesson_id = client.post("/api/lessons", json={"target": "no-audio"}).json()["lesson_id"]
    lesson = client.get(f"/api/lessons/{lesson_id}").json()

    assert lesson["status"] == "ready"
    assert len(lesson["examples"]) == 4, "例句不该因为语音失败而丢"
    assert lesson["readiness"]["ready"] is True
    assert lesson["readiness"]["complete"] is False
    assert lesson["readiness"]["problems"]


def test_regenerate_and_delete(client):
    lesson_id = client.post("/api/lessons", json={"target": "regen"}).json()["lesson_id"]
    assert client.post(f"/api/lessons/{lesson_id}/regenerate").status_code == 200
    assert client.get(f"/api/lessons/{lesson_id}").json()["status"] == "ready"
    assert client.delete(f"/api/lessons/{lesson_id}").status_code == 200
    assert client.get(f"/api/lessons/{lesson_id}").status_code == 404


def test_redo_audio_reports_what_it_fixed(client):
    lesson_id = client.post("/api/lessons", json={"target": "audio"}).json()["lesson_id"]
    result = client.post(f"/api/lessons/{lesson_id}/audio").json()
    assert result["ok"] == 4
    assert result["failed"] == []


# --------------------------------------------------------------- 音频


def test_audio_path_traversal_is_rejected(client):
    """这个必须挡住：不做校验的话，一个精心构造的文件名就能读到
    data 目录下的 config.json —— 里面有 API Key。

    这是整个程序里唯一一处「用户输入直接变成文件路径」的地方。
    """
    for name in [
        "..%2F..%2Fconfig.json",
        "....//config.json",
        "config.json",
        "edge-00000000000000000000.mp3%00.txt",
        "%2e%2e%2f%2e%2e%2fconfig.json",
    ]:
        resp = client.get(f"/api/audio/{name}")
        assert resp.status_code in (400, 404), f"{name} 被放行了（{resp.status_code}）"


def test_a_missing_audio_file_says_so(client):
    """缺文件要明确报 404，让界面能标成「这条缺语音」。

    返回 200 + 空内容的话，界面会画出一个点了没反应的播放按钮 ——
    用户只会觉得「播放坏了」。
    """
    resp = client.get("/api/audio/edge-00000000000000000000.mp3")
    assert resp.status_code == 404


def test_existing_audio_is_served(client):
    lesson_id = client.post("/api/lessons", json={"target": "serve"}).json()["lesson_id"]
    lesson = client.get(f"/api/lessons/{lesson_id}").json()
    name = lesson["examples"][0]["audio_path"]
    assert name

    resp = client.get(f"/api/audio/{name}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/")


# --------------------------------------------------------------- 配置


def test_config_save_never_leaks_the_key_back(client):
    resp = client.post("/api/config", json={"llm": {"api_key": "sk-secret-xyz", "model": "m1"}})
    body = resp.text
    assert "sk-secret-xyz" not in body, "密钥不该出现在响应里"
    assert resp.json()["config"]["llm"]["api_key_set"] is True

    assert "sk-secret-xyz" not in client.get("/api/config").text


def test_key_survives_a_save_that_omits_it(client):
    """界面留空表示「不改动」，不能变成「清空」。"""
    client.post("/api/config", json={"llm": {"api_key": "keep-me"}})
    client.post("/api/config", json={"llm": {"model": "changed"}})

    from app import config

    assert config.load()["llm"]["api_key"] == "keep-me"


def test_unknown_preset_is_rejected(client):
    assert client.post("/api/config/preset", json={"preset": "不存在的厂商"}).status_code == 400


def test_selfcheck_reports_levels(client):
    report = client.get("/api/selfcheck").json()
    assert report["overall"] in {"ok", "warn", "fail"}
    names = {i["name"] for i in report["items"]}
    assert "界面文件" in names
    assert "数据库" in names
    assert "语音引擎" in names
    for item in report["items"]:
        assert item["level"] in {"ok", "warn", "fail"}


def test_selfcheck_flags_a_missing_key_as_a_warning_not_a_failure(client):
    """没填密钥是「注意」，不是「有问题」—— 用户本来就该在界面里看到它，
    而不是被一个红叉拦住去路。"""
    report = client.get("/api/selfcheck").json()
    llm_item = next(i for i in report["items"] if i["name"] == "大模型")
    assert llm_item["level"] == "warn"
    assert report["overall"] != "fail"


def test_selfcheck_never_fails_on_the_window_component(client):
    """缺原生窗口**不能**报 fail。

    窗口不是必需品 —— 缺了会退回浏览器，功能一模一样。
    把「能用但没那么好看」说成「不能用」是误导，
    而误导会让用户去折腾一个根本不需要解决的问题。
    """
    report = client.get("/api/selfcheck").json()
    window = next((i for i in report["items"] if i["name"] == "原生窗口"), None)
    assert window is not None, "自检里应当有一项报告窗口可用性"
    assert window["level"] in {"ok", "warn"}, f"窗口那项报了 {window['level']}"
    if window["level"] == "warn":
        assert window["hint"], "warn 必须告诉用户「这影响什么、要不要处理」"


def test_selfcheck_reports_the_data_dir_it_actually_uses(client, isolated_data):
    """自检要能回答「我的数据到底在哪」—— 打包后这是最常被问的一件事。"""
    report = client.get("/api/selfcheck").json()
    data_item = next(i for i in report["items"] if i["name"] == "数据目录")
    assert str(isolated_data.resolve()) in data_item["detail"]


# --------------------------------------------------------------- 备份


def test_backup_endpoint(client):
    client.post("/api/lessons", json={"target": "backup-me"})
    resp = client.post("/api/backup", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["bytes"] > 0

    from pathlib import Path

    assert Path(data["path"]).is_file()


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", ""])
def test_backup_name_is_sanitised(client, name):
    """备份文件名也是用户输入变路径 —— 同样要挡。"""
    resp = client.post("/api/backup", json={"name": name})
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        from pathlib import Path

        assert Path(resp.json()["path"]).parent.name == "backups"


# --------------------------------------------------------------- 评分


def test_grade_rejects_out_of_range_values(client):
    lesson_id = client.post("/api/lessons", json={"target": "grades"}).json()["lesson_id"]
    card_id = client.get(f"/api/lessons/{lesson_id}").json()["cards"][0]["id"]

    assert client.post(f"/api/cards/{card_id}/grade", json={"grade": 9}).status_code == 422
    assert client.post(f"/api/cards/{card_id}/grade", json={"grade": -1}).status_code == 422


def test_grading_a_missing_card_is_404(client):
    assert client.post("/api/cards/999999/grade", json={"grade": 2}).status_code == 404


def test_card_of_a_missing_lesson_is_404(client):
    assert client.get("/api/cards/999999").status_code == 404
