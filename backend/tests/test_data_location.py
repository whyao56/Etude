"""数据目录可以搬走。

数据会长大：音频每条约几十 KB，场景图每张几十到几百 KB，一个训练包
40 张卡就是几 MB。攒几百个包，C 盘会告急。

这个功能只有一条不可违反的约束，也是这一组测试里最要紧的一条：

    **指针（bootstrap.json）永远留在平台默认位置，不跟着数据搬走。**

它跟着搬走的话，程序下次启动就再也找不到数据了 —— 而用户看到的是
「我的学习记录没了」。这类错误在开发机上永远复现不出来，
因为开发机上的默认位置一直存在。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def relocatable(tmp_path, monkeypatch):
    """一个「可以搬走」的环境。

    两件事都必须做：

    · **解开 ``ETUDE_DATA_DIR``。** ``isolated_data`` 靠它把数据指到临时目录，
      而它同时也把「改数据目录」这个功能锁住了（这是对的行为 ——
      环境变量优先级最高）。要测搬迁本身就得先把锁解开。
    · **把平台默认位置也指到临时目录。** 否则搬迁测试会去动用户真实的
      ``%LOCALAPPDATA%\\Etude`` —— 那是不可接受的事故。
    """
    import app.paths as paths
    from app import db

    default = tmp_path / "platform-default"
    default.mkdir(parents=True, exist_ok=True)

    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "default_root", lambda: default)

    db.close()
    yield default
    db.close()


# ------------------------------------------------------------ 位置是怎么算出来的


def test_describe_says_where_the_data_is_and_how_it_got_there(relocatable):
    from app import storage

    info = storage.describe()
    assert info["source"] == "default"
    assert info["moved"] is False
    assert info["current"] == str(relocatable)
    assert info["bootstrap"] == str(relocatable / "bootstrap.json")
    assert info["bytes"] >= 0


def test_the_env_var_outranks_the_pointer_and_locks_the_setting(relocatable, monkeypatch):
    """环境变量 > 指针 > 默认。这是「端到端检查把数据写到临时目录」
    这条能力的基础 —— 它不能被界面上的设置盖掉。"""
    import app.paths as paths

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(relocatable / "forced"))
    assert paths.writable_root_source() == paths.DATA_DIR_ENV
    assert storage_locked()


def storage_locked() -> bool:
    from app import storage

    return storage.env_locked()


# ------------------------------------------------------------------ 搬


def _make_some_data(root):
    """造一点「看起来像数据」的东西。"""
    from app import db

    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text('{"llm": {"api_key": "k"}}', encoding="utf-8")
    (root / "audio").mkdir(exist_ok=True)
    (root / "audio" / "edge-00000000000000000000.mp3").write_bytes(b"ID3")
    (root / "images").mkdir(exist_ok=True)
    (root / "images" / "search-00000000000000000000.jpg").write_bytes(b"\xff\xd8\xff")
    db.create_lesson("搬走之前建的", "word")


def test_relocating_copies_the_data_and_leaves_the_original_alone(relocatable, tmp_path):
    from app import storage

    _make_some_data(relocatable)
    target = tmp_path / "D-drive" / "Etude"

    result = storage.relocate(str(target))
    assert result["ok"] is True, result
    assert result["copied"] >= 3, "数据库、config.json、audio、images 都该跟上"
    assert result["left_behind"] == str(relocatable), "要把旧位置告诉用户，让他自己删"
    assert result["restart_required"] is True

    assert (target / "config.json").is_file()
    assert (target / "audio" / "edge-00000000000000000000.mp3").is_file()
    assert (target / "images" / "search-00000000000000000000.jpg").is_file()
    assert (target / "etude.sqlite3").is_file()

    # 旧数据一个都没少 —— 自动删旧数据是这类操作里唯一不可逆的一步，
    # 而这一步没有任何好处。
    assert (relocatable / "config.json").is_file()
    assert (relocatable / "etude.sqlite3").is_file()


def test_the_pointer_stays_behind_where_it_was(relocatable, tmp_path):
    """**这一条是整套设计的核心。**

    bootstrap.json 跟着数据一起搬走的话，程序下次启动时在默认位置
    找不到指针，就会静默退回默认目录 —— 用户看到的界面是空的，
    而他的数据其实还在新位置上。这个 bug 在开发机上永远不出现
    （默认位置一直存在、里面一直有指针），只会在用户机器上出现一次、
    且看起来像「数据丢了」。
    """
    import app.paths as paths
    from app import storage

    _make_some_data(relocatable)
    target = tmp_path / "new-home"
    assert storage.relocate(str(target))["ok"] is True

    # 指针还在老地方，而且指向新家。
    # 解析一遍再比，不要拿字符串去比 —— 指针是 JSON，Windows 路径里的
    # 反斜杠在 JSON 里是转义过的，直接子串匹配永远不成立。
    import json
    from pathlib import Path

    pointer = relocatable / "bootstrap.json"
    assert pointer.is_file(), "指针不能跟着搬走"
    pointed = json.loads(pointer.read_text(encoding="utf-8"))["data_dir"]
    assert Path(pointed).resolve() == target.resolve()

    # 指针**不能**在新家出现 —— 那说明有人在用 copytree 整目录递归。
    assert not (target / "bootstrap.json").exists()

    # 解析顺序跟着走：现在数据目录就是新家。
    assert paths.writable_root() == target.resolve()
    assert paths.writable_root_source() == "bootstrap"
    assert storage.describe()["moved"] is True


def test_relocating_can_also_just_point_without_copying(relocatable, tmp_path):
    """「我数据已经手动拷过去了，只让它认那一边」也得有出路。"""
    from app import storage

    _make_some_data(relocatable)
    target = tmp_path / "already-there"
    target.mkdir()

    result = storage.relocate(str(target), copy=False)
    assert result["ok"] is True
    assert result["copied"] == 0
    assert result["left_behind"] == "", "什么都没复制，就没有「旧的那份」"
    assert not (target / "config.json").exists()
    # 老数据仍在原地 —— 随时可以改回来。
    assert (relocatable / "config.json").is_file()


# --------------------------------------------------------- 不能搬的情况


def test_the_same_directory_is_refused(relocatable):
    from app import storage

    result = storage.relocate(str(relocatable))
    assert result["ok"] is False
    assert "当前的数据目录" in result["error"]


def test_a_target_inside_the_current_directory_is_refused(relocatable):
    """搬到自己里面 = 复制自己 = 无限递归，中途磁盘写满。

    这个必须在**复制开始之前**就拦下来：复制到一半才发现，
    用户面对的是两个都不完整的数据目录。
    """
    from app import storage

    _make_some_data(relocatable)
    result = storage.relocate(str(relocatable / "sub"))
    assert result["ok"] is False
    assert "里面" in result["error"]

    # 关键：什么都没发生。
    assert not (relocatable / "sub").exists()
    assert not (relocatable / "bootstrap.json").exists(), "失败时不能留下指针"


def test_a_target_that_is_a_file_is_refused(relocatable, tmp_path):
    from app import storage

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    result = storage.relocate(str(blocker))
    assert result["ok"] is False
    assert "不是一个目录" in result["error"]


def test_an_empty_target_is_refused(relocatable):
    from app import storage

    result = storage.relocate("   ")
    assert result["ok"] is False


def test_a_failed_copy_never_writes_the_pointer(relocatable, tmp_path, monkeypatch):
    """复制到一半失败 —— **绝不能在此时写指针**。

    顺序反过来的话，会留下一个「指向空目录」的指针。用户重启之后
    看到的是一个全新的空程序，而他的数据其实还好好地在原地。
    这是典型的「不报错但结果不对」，而且看起来像数据丢了。
    """
    import shutil

    from app import storage

    _make_some_data(relocatable)
    target = tmp_path / "half-way"

    def boom(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(shutil, "copytree", boom)

    result = storage.relocate(str(target))
    assert result["ok"] is False
    assert "复制数据时出错" in result["error"]
    assert result["hint"], "要告诉用户「新位置可能是个半成品」"
    assert not (relocatable / "bootstrap.json").exists(), "失败时绝不能写指针"
    assert paths_source() == "default", "数据目录还得是原来那个"


def paths_source() -> str:
    from app import paths

    return paths.writable_root_source()


# ------------------------------------------------------------------ 改回来


def test_reset_to_default_moves_the_pointer_back(relocatable, tmp_path):
    """搬回来**不复制**任何东西 —— 只是把指针删掉。

    真正会把数据搬回来的是用户自己的文件管理器；程序不该猜他想留哪一份。
    """
    import app.paths as paths
    from app import storage

    _make_some_data(relocatable)
    target = tmp_path / "elsewhere"
    storage.relocate(str(target))
    assert paths.writable_root() == target.resolve()

    result = storage.reset_to_default()
    assert result["ok"] is True
    assert result["moved_to"] == str(relocatable)
    assert result["restart_required"] is True
    assert paths.writable_root() == relocatable
    assert paths.writable_root_source() == "default"

    # 两边的东西都还在。
    assert (target / "config.json").is_file()
    assert (relocatable / "config.json").is_file()


def test_a_broken_pointer_file_falls_back_to_the_default(relocatable):
    """指针文件坏掉不能把程序卡死 —— 退回默认位置，比起不来好得多。"""
    import app.paths as paths

    (relocatable / "bootstrap.json").write_text("{这不是 json", encoding="utf-8")
    assert paths.writable_root_source() == "default"
    assert paths.writable_root() == relocatable


# ------------------------------------------------------------------ 接口层


def test_the_endpoint_refuses_while_the_env_var_is_pinning_it(client):
    """界面上的设置按了不生效时，必须**说清为什么**。

    ``isolated_data`` 正是用环境变量做的隔离，所以这条路径在测试里
    天然可测 —— 而在真实机器上，它对应的是「用户设了 ETUDE_DATA_DIR
    却还在界面上点来点去」。
    """
    resp = client.post("/api/data-location", json={"path": "D:/Somewhere"})
    assert resp.status_code == 400
    assert "ETUDE_DATA_DIR" in resp.json()["detail"]


def test_the_endpoint_reports_the_current_location(client):
    data = client.get("/api/data-location").json()
    assert data["current"]
    assert data["source"] == "ETUDE_DATA_DIR"
    assert data["locked_by_env"] is True
    assert data["default"] and data["bootstrap"]


def test_the_endpoint_relocates_when_it_is_free_to_do_so(tmp_path, monkeypatch):
    """走真路由的那一遍：不带环境变量、默认位置在临时目录。"""
    from fastapi.testclient import TestClient

    import app.paths as paths
    from app import config, db, storage

    default = tmp_path / "platform-default"
    default.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "default_root", lambda: default)

    db.close()
    try:
        (default / "config.json").write_text("{}", encoding="utf-8")
        config.save({"llm": {"model": "before-the-move"}})

        import app.api as api

        with TestClient(api.app) as c:
            target = tmp_path / "new-place"
            resp = c.post("/api/data-location",
                          json={"path": str(target), "copy": True})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["restart_required"] is True
            assert body["moved_to"] == str(target.resolve())

            # 重启之前，接口仍然读着老的库 —— 这正是「必须重启」的原因，
            # 也是为什么返回值里要带 restart_required 而不是假装已经生效。
            assert c.get("/api/data-location").json()["moved"] is True
            assert storage.describe()["source"] == "bootstrap"
    finally:
        db.close()
