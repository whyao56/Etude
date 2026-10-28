"""0.1.1 新加的四个口子：进度、备份/恢复、打开目录、导入导出。

这些用例的重点不是「接口返回 200」，而是几条不写在代码里就会踩的规则：

- 复习记录按**本机时区**归日（库里是 UTC），夜里练的那张不能算到第二天；
- 恢复备份前必须先把当前的库存一份，恢复完 `-wal` 不能留在原地；
- 「打开目录」只认四个写死的名字，不能变成给网页执行任意程序的口子；
- 导出的文件里不带二进制，导入回来必须落成「缺语音」而不是「坏了」。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.paths import backups_dir, db_path


def _make_lesson(client, target: str = "appreciate") -> int:
    created = client.post("/api/lessons", json={"target": target}).json()
    lesson_id = created.get("lesson_id") or created.get("lesson", {}).get("id")
    assert lesson_id, created
    return lesson_id


# ------------------------------------------------------------------ 进度


def test_progress_explains_the_last_two_weeks(client):
    _make_lesson(client)
    body = client.get("/api/progress?days=14").json()

    assert len(body["series"]) == 14
    # 传了 14 就该正好 14 个桶，不多不少。
    assert {day["date"] for day in body["series"]}.__len__() == 14
    assert body["days"] == 14
    for key in ("reviews", "attempts", "grades"):
        assert key in body["series"][0]
    assert set(body["totals"]) >= {"lessons", "examples", "cards", "reviews", "due_now"}
    assert len(body["channels"]) == 5
    assert body["streak"]["current"] >= 0
    assert isinstance(body["weak"], list)


def test_progress_clamps_the_window(client):
    assert client.get("/api/progress?days=1").status_code == 422
    assert client.get("/api/progress?days=999").status_code == 422


def test_progress_counts_a_review_on_the_local_day(client):
    """夜里十一点练的那张，要算在今天，不能漂到明天。

    库里的 `at` 是 UTC，而「连着练了几天」是按用户所在时区算的 ——
    拿 UTC 日期切桶，中国用户晚上八点之后练的都会掉进后一天。
    """
    lesson_id = _make_lesson(client)
    card = db.due_cards(limit=1, new_per_day=99)[0]
    db.grade_card(card["id"], 2, 12.0)
    assert lesson_id

    body = client.get("/api/progress?days=7").json()
    today = body["today"]
    assert today["reviews"] >= 1
    assert body["streak"]["today_done"] is True
    last = body["series"][-1]
    assert last["date"] == today["date"]
    assert last["reviews"] >= 1


# ------------------------------------------------------------------ 备份


def test_backup_list_starts_empty_and_grows(client):
    assert client.get("/api/backups").json()["backups"] == []

    made = client.post("/api/backup", json={"name": "hand-made"}).json()
    assert made["ok"] is True
    assert made["bytes"] > 0

    listed = client.get("/api/backups").json()
    names = [item["name"] for item in listed["backups"]]
    assert "hand-made.sqlite3" in names
    # 界面要显示「你现在有多少行」，所以 current 必须是真数字。
    assert "cards" in listed["current"]


def test_restore_keeps_a_copy_of_the_current_database(client):
    lesson_id = _make_lesson(client, "restore-me")
    client.post("/api/backup", json={"name": "before-more-data"})
    before = client.get("/api/stats").json()

    # 备份之后再建一个包：恢复之后它应该消失。
    extra = _make_lesson(client, "created-after-backup")
    assert db.get_lesson(extra) is not None

    result = client.post("/api/backups/restore", json={"name": "before-more-data.sqlite3"})
    assert result.status_code == 200
    body = result.json()
    assert body["ok"] is True
    assert body["safety_copy"].startswith("before-restore-")
    # 兜底那份必须真的躺在磁盘上，否则「恢复也能后悔」是句空话。
    assert (backups_dir() / body["safety_copy"]).is_file()

    assert db.get_lesson(extra) is None
    assert db.get_lesson(lesson_id) is not None
    # 恢复之后连得上、数得出来才算真的恢复好了（重建连接时迁移也会跑一遍）。
    assert client.get("/api/stats").json()["lessons"] == before["lessons"]


def test_restore_rejects_a_name_that_is_a_path(client):
    assert client.post("/api/backups/restore", json={"name": "../config.json"}).status_code == 400
    assert client.post("/api/backups/restore", json={"name": "nope.sqlite3"}).status_code == 404


def test_auto_backup_only_fires_once_a_week(tmp_path, monkeypatch):
    from app.paths import writable_root

    monkeypatch.setenv("ETUDE_DATA_DIR", str(tmp_path / "data"))
    db.close()
    assert writable_root() == (tmp_path / "data").resolve()

    first = db.auto_backup()
    assert first.endswith(".sqlite3")
    # 刚备份过就不该再生成一份。
    assert db.auto_backup() == ""
    assert len(list(backups_dir().glob("etude-*.sqlite3"))) == 1


def test_auto_backup_keeps_only_a_handful(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("ETUDE_DATA_DIR", str(tmp_path / "data"))
    db.close()
    folder = backups_dir()
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for index in range(9):
        path = folder / f"etude-2020010{index}-000000.sqlite3"
        path.write_bytes(b"x")
        os.utime(path, (now - 86400 * (30 - index),) * 2)

    made = db.auto_backup(days=7, keep=4)
    assert made
    assert len(list(folder.glob("etude-*.sqlite3"))) <= 4


# ------------------------------------------------------------- 打开目录


def test_open_folder_only_accepts_the_four_known_names(client):
    assert client.post("/api/open-folder", json={"which": "C:/Windows"}).status_code == 400
    assert client.post("/api/open-folder", json={"which": "../../etc"}).status_code == 400


def test_open_folder_calls_the_system_opener(client, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(path), raising=False)

    body = client.post("/api/open-folder", json={"which": "logs"}).json()
    assert body["ok"] is True
    assert body["path"].endswith("logs")
    assert opened and opened[0] == body["path"]


# ------------------------------------------------------------- 导入导出


def test_export_never_carries_binaries(client):
    lesson_id = _make_lesson(client)
    detail = client.get(f"/api/lessons/{lesson_id}").json()
    body = client.get(f"/api/lessons/{lesson_id}/export").json()

    assert body["format"] == "etude-lesson"
    assert body["format_version"] == 1
    assert body["lesson"]["target"] == "appreciate"
    # 导出条目数必须和详情页里画出来的一致，否则「导出」看着成功、内容却少了。
    assert len(body["examples"]) == len(detail["examples"])
    # 语音是文件名、配图是别的目录里的文件，都不进导出文件。
    for example in body["examples"]:
        assert "audio_path" not in example
        assert "scene_image" not in example
    assert "语音不随文件导出" in body["note"]


def test_import_round_trip_lands_as_missing_audio(client):
    lesson_id = _make_lesson(client)
    payload = client.get(f"/api/lessons/{lesson_id}/export").json()
    expected = len(payload["examples"])

    imported = client.post("/api/lessons/import", json=payload)
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["examples"] == expected
    assert body["need_audio"] is True

    detail = client.get(f"/api/lessons/{body['lesson_id']}").json()
    assert detail["readiness"]["ready"] is True
    assert detail["readiness"]["complete"] is False
    assert any("语音" in problem for problem in detail["readiness"]["problems"])


def test_import_rejects_a_foreign_file(client):
    assert client.post("/api/lessons/import", json={"format": "anki", "lesson": {"target": "x"}}).status_code == 400
    assert client.post("/api/lessons/import", json={"lesson": {"target": ""}}).status_code == 400
    assert client.post(
        "/api/lessons/import",
        json={"lesson": {"target": "ok", "language": "ja"}, "examples": []},
    ).status_code == 400


def test_import_without_examples_is_rejected(client):
    res = client.post(
        "/api/lessons/import",
        json={"format": "etude-lesson", "lesson": {"target": "lonely"}, "examples": []},
    )
    assert res.status_code == 400
    assert "一句" in res.json()["detail"] or "例句" in res.json()["detail"]


def test_import_drops_junk_examples_but_keeps_good_ones(client):
    payload = {
        "format": "etude-lesson",
        "lesson": {"target": "tidy", "kind": "word", "language": "en"},
        "examples": [
            {"sentence": "This one is real."},
            {"scene_en": "no sentence here"},
            {"sentence": "This one is real."},  # 重复的应被去掉
        ],
    }
    body = client.post("/api/lessons/import", json=payload).json()
    detail = client.get(f"/api/lessons/{body['lesson_id']}").json()
    sentences = [item["sentence"] for item in detail["examples"]]
    assert sentences == ["This one is real."]
