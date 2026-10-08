"""路径、配置、间隔重复的单元测试。"""

from __future__ import annotations

import json

import pytest


# ------------------------------------------------------------------ 路径


def test_writable_root_follows_the_env_var(tmp_path, monkeypatch):
    """数据目录必须能被环境变量整个挪走。

    端到端检查靠它把数据写到临时目录 —— 那条链路一旦失灵，
    「测试跑到真实数据上」就会悄悄发生。
    """
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("ETUDE_DATA_DIR", str(target))

    from app import paths

    assert paths.writable_root() == target.resolve()
    assert paths.db_path().parent == target.resolve()
    assert paths.audio_dir().parent == target.resolve()


def test_all_writable_paths_live_under_one_root(tmp_path, monkeypatch):
    """可写的东西必须全在一个根下面。

    散成两处的话，用户「删掉数据目录」就会删不干净，
    而残留的那部分会在下次启动时冒出来，看起来像幽灵数据。
    """
    monkeypatch.setenv("ETUDE_DATA_DIR", str(tmp_path / "d"))

    from app import paths

    root = paths.writable_root()
    for path in (paths.db_path(), paths.audio_dir(), paths.log_dir(), paths.config_path()):
        assert root in path.parents, f"{path} 不在 {root} 下面"


def test_frontend_dir_exists_in_source_mode():
    """源码态必须能找到界面文件。

    「找不到界面」在打包后是同一个症状（双击没反应），
    所以这条在源码态就该钉住，别等打包时才发现。
    """
    from app import paths

    assert (paths.frontend_dir() / "index.html").is_file()


def test_path_judgement_happens_in_exactly_one_place():
    """「是不是在 exe 里」只允许在 paths.py 里判断一次。

    散在多处的话，打包时会有一半的模块按源码态算路径 ——
    症状是「有些数据在用户目录、有些在程序目录」，极难查。
    这里用全文搜索把这条规矩钉住。
    """
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path.name == "paths.py":
            continue
        text = path.read_text(encoding="utf-8")
        if 'getattr(sys, "frozen"' in text or "sys._MEIPASS" in text or "hasattr(sys,'frozen')" in text:
            offenders.append(str(path.relative_to(app_dir)))
    assert not offenders, f"这些文件自己判断了冻结态，应当改用 paths 里的函数：{offenders}"


# ------------------------------------------------------------------ 配置


def test_defaults_load_without_a_file(isolated_data):
    from app import config

    cfg = config.load()
    assert cfg["llm"]["preset"]
    assert cfg["tts"]["engine"] == "edge"
    assert cfg["study"]["examples_per_lesson"] >= 3


def test_save_and_load_roundtrip(isolated_data):
    from app import config

    config.save({"llm": {"model": "my-model"}, "study": {"new_per_day": 7}})
    cfg = config.load()
    assert cfg["llm"]["model"] == "my-model"
    assert cfg["study"]["new_per_day"] == 7
    # 没提到的字段必须保持默认，不能被抹掉。
    assert cfg["llm"]["preset"]


def test_corrupt_config_does_not_prevent_startup(isolated_data):
    """配置坏掉不该让程序起不来。

    用户手改 JSON 改坏了是很常见的事。这里要求：读得到就返回默认值，
    而且下一次保存会把它重写成合法的。
    """
    from app import config

    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{这不是合法 JSON", encoding="utf-8")

    cfg = config.load()
    assert cfg["tts"]["engine"] == "edge"

    config.save({"llm": {"model": "recovered"}})
    assert json.loads(path.read_text(encoding="utf-8"))["llm"]["model"] == "recovered"


def test_env_var_overrides_config_file(isolated_data, monkeypatch):
    """环境变量优先于配置文件 —— CI 和临时试用靠它注入。"""
    from app import config

    config.save({"llm": {"model": "from-file"}})
    monkeypatch.setenv("ETUDE_LLM_MODEL", "from-env")
    assert config.load()["llm"]["model"] == "from-env"


def test_switching_preset_keeps_the_key(isolated_data):
    """换厂商时不要动用户的密钥。

    用户常常只是先点开看看别的厂商有哪些模型 —— 把密钥抹掉会很烦人。
    """
    from app import config
    from app.providers import llm

    config.save({"llm": {"api_key": "keep-me", "preset": "deepseek"}})
    merged = llm.apply_preset(config.load(), "qwen")
    config.save(merged)

    cfg = config.load()
    assert cfg["llm"]["api_key"] == "keep-me"
    assert cfg["llm"]["preset"] == "qwen"
    assert "dashscope" in cfg["llm"]["base_url"]


def test_preset_does_not_overwrite_with_empty_values(isolated_data):
    """预置项里空着的字段不该把已有值擦掉（豆包的模型名就是空的）。"""
    from app import config
    from app.providers import llm

    config.save({"llm": {"model": "my-careful-choice"}})
    merged = llm.apply_preset(config.load(), "doubao")
    assert merged["llm"]["model"] == "my-careful-choice"
    assert "volces" in merged["llm"]["base_url"]


# ------------------------------------------------------------------ 间隔重复


def test_lapse_reschedules_within_the_session():
    """「没想起来」必须是分钟级的重来，不能推到明天。

    推到明天的话，这张卡今天等于没练 —— 而当次会话里再遇到一次，
    才是真正在做「短期巩固」。
    """
    from app import srs

    interval, ease, reps, lapses, minutes = srs.schedule(10.0, 2.5, 5, 0, srs.AGAIN)
    assert interval == 0.0
    assert reps == 0
    assert lapses == 1
    assert 0 < minutes < srs.MINUTES_PER_DAY
    assert ease < 2.5


def test_ease_has_floors_and_ceils():
    from app import srs

    ease = 2.5
    for _ in range(30):
        _i, ease, _r, _l, _m = srs.schedule(1.0, ease, 1, 0, srs.AGAIN)
    assert ease == srs.EASE_MIN

    ease = 2.5
    for _ in range(30):
        _i, ease, _r, _l, _m = srs.schedule(1.0, ease, 1, 0, srs.EASY)
    assert ease == srs.EASE_MAX


def test_hard_never_pays_better_than_good():
    """「吃力」的间隔必须小于「正常」。

    不小于的话，「吃力」就变成一种比「正常」更划算的选择 ——
    涨得一样多还少花力气，用户会学会一直点它，调度就废了。
    """
    from app import srs

    _i, _e, _r, _l, hard_min = srs.schedule(10.0, 2.5, 3, 0, srs.HARD)
    _i, _e, _r, _l, good_min = srs.schedule(10.0, 2.5, 3, 0, srs.GOOD)
    _i, _e, _r, _l, easy_min = srs.schedule(10.0, 2.5, 3, 0, srs.EASY)
    assert hard_min < good_min < easy_min


def test_first_exposure_gives_a_sane_first_step():
    from app import srs

    _i, _e, reps, _l, minutes = srs.schedule(0.0, 2.5, 0, 0, srs.GOOD)
    assert reps == 1
    assert minutes == srs.MINUTES_PER_DAY


def test_interval_is_capped():
    """间隔要有上限。

    没有上限的话，几轮「秒答」之后这张卡会被推到几十年后 ——
    用户再也不会遇到它，而界面还会显示「下次复习：2037 年」。
    """
    from app import srs

    interval, _e, _r, _l, _m = srs.schedule(300.0, 3.0, 20, 0, srs.EASY)
    assert interval <= srs.MAX_INTERVAL_DAYS


def test_unknown_grade_is_rejected():
    from app import srs

    with pytest.raises(ValueError):
        srs.schedule(1.0, 2.5, 1, 0, 99)


def test_preview_covers_all_four_buttons():
    from app import srs

    preview = srs.preview(5.0, 2.5, 2, 0)
    assert set(preview) == {0, 1, 2, 3}
    for info in preview.values():
        assert info["label"] and info["human"]


@pytest.mark.parametrize(
    "minutes,expect",
    [(0.5, "马上"), (10, "分钟"), (120, "小时"), (2000, "天"), (60 * 24 * 60, "个月"), (60 * 24 * 400, "年")],
)
def test_human_delay_never_prints_raw_days(minutes, expect):
    """界面上不能出现「0.007 天」这种 —— 人话才有人看。"""
    from app import srs

    assert expect in srs.human_delay(minutes)
