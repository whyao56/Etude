"""端到端：对着**真的 HTTP 服务**跑一遍。

为什么要单独一组（而不是只 monkeypatch 函数）：
HTTP 请求怎么拼、``response_format`` 不被支持时怎么退让、状态码怎么翻译成
「能修好的方向」—— 这些恰恰是最容易写错的地方，而它们全在 HTTP 那一层。
只调函数的话，这一层永远不会被测到。

这里起的是本地假模型（scripts/openai_stub.py），所以不需要任何真密钥。
"""

from __future__ import annotations

import socket
import threading
import time

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def stub_server():
    """在后台线程里起一个真的 HTTP 服务。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import openai_stub

    import uvicorn

    port = _free_port()
    config = uvicorn.Config(
        openai_stub.app, host="127.0.0.1", port=port, log_level="error", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="stub-model")
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("假模型服务没起来")

    yield f"http://127.0.0.1:{port}/v1"

    server.should_exit = True
    thread.join(timeout=5)


def _point_config_at(base_url: str, model: str = "any-model") -> None:
    from app import config

    config.save({
        "llm": {"base_url": base_url, "model": model, "api_key": "stub-key"},
        # 配图默认的来源是「从开放图库检索」，那是**真的联网**（Openverse）。
        # 这一组测试要的是「真 HTTP 的模型链路」，不该顺手把一个外部图库
        # 也拉进来 —— 那会让一次网络抖动变成一次随机失败。
        # 想验画图那一条的，看下面单独的那个用例。
        "images": {"source": "off"},
    })


def test_chat_against_a_real_endpoint(stub_server):
    from app import config
    from app.providers import llm

    _point_config_at(stub_server)
    cfg = config.load()["llm"]
    text = llm.chat(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        model=cfg["model"],
        messages=[{"role": "user", "content": 'TARGET WORD: "appreciate"\nProvide exactly 3 examples.'}],
    )
    data = llm.extract_json(text)
    assert data["examples"]
    assert "appreciate" in data["examples"][0]["sentence"]


def test_check_reports_round_trip_time(stub_server):
    from app.providers import llm

    result = llm.check(base_url=stub_server, api_key="k", model="m")
    assert result["ok"] is True
    assert result["seconds"] >= 0


def test_response_format_is_dropped_when_the_endpoint_rejects_it(stub_server, monkeypatch):
    """有些兼容端点不认 response_format，会直接 400。

    正确做法是**去掉它重试一次**，而不是猜「哪家支持」——
    厂商的支持情况会变，而 400 的语义不会。
    """
    import httpx

    from app.providers import llm

    seen: list[dict] = []
    real_post = httpx.Client.post

    def spy(self, url, **kwargs):
        if url.endswith("/chat/completions"):
            seen.append(kwargs.get("json") or {})
            if "response_format" in (kwargs.get("json") or {}):
                request = httpx.Request("POST", url)
                return httpx.Response(400, request=request, json={"error": "unknown field"})
        return real_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", spy, raising=True)

    text = llm.chat(
        base_url=stub_server,
        api_key="k",
        model="m",
        messages=[{"role": "user", "content": 'TARGET WORD: "x"\nProvide exactly 3 examples.'}],
    )
    assert llm.extract_json(text)["examples"]
    assert len(seen) >= 2, "应当先带 response_format 试一次、再去掉重试"
    assert "response_format" not in seen[-1]


def test_401_becomes_a_fixable_instruction(stub_server):
    from app.providers.llm import LLMError
    from app.providers import llm

    with pytest.raises(LLMError) as info:
        llm.chat(
            base_url=stub_server,
            api_key="k",
            model="fault-401",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert info.value.kind == "auth"
    assert "API Key" in info.value.hint or "密钥" in info.value.hint


@pytest.mark.parametrize(
    "model,kind",
    [("fault-401", "auth"), ("fault-403", "forbidden"), ("fault-404", "not_found")],
)
def test_each_fault_maps_to_its_own_direction(stub_server, model, kind):
    """每种故障都要落到**它自己的**那一档。

    合成一档是不行的：401 要指向「检查密钥」，403 要指向「开通权限」，
    404 要指向「检查地址和模型名」。给错了方向比不给更糟 ——
    用户会照着一条永远修不好的建议反复折腾。
    """
    from app.providers import llm
    from app.providers.llm import LLMError

    with pytest.raises(LLMError) as info:
        llm.chat(
            base_url=stub_server,
            api_key="k",
            model=model,
            messages=[{"role": "user", "content": "hi"}],
        )
    assert info.value.kind == kind
    assert info.value.hint


def test_the_three_faults_say_different_things(stub_server):
    """三种故障给出的「下一步」必须彼此不同。

    只在代码里分了 kind、却给同一句提示，等于没分。
    """
    from app.providers import llm
    from app.providers.llm import LLMError

    hints = {}
    for model, label in [("fault-401", "auth"), ("fault-403", "forbidden"), ("fault-404", "not_found")]:
        with pytest.raises(LLMError) as info:
            llm.chat(base_url=stub_server, api_key="k", model=model,
                     messages=[{"role": "user", "content": "hi"}])
        hints[label] = info.value.hint
    assert len(set(hints.values())) == 3, f"三种故障给了重复的建议：{hints}"


def test_fenced_and_messy_replies_are_parsed(stub_server):
    """真实厂商的回复经常裹着围栏或前后带解说 —— 三种形态都得吃得下。"""
    from app.providers import llm

    for model in ("fault-fenced", "fault-messy"):
        text = llm.chat(
            base_url=stub_server,
            api_key="k",
            model=model,
            messages=[{"role": "user", "content": 'TARGET WORD: "appreciate"\nProvide exactly 3 examples.'}],
        )
        data = llm.extract_json(text)
        assert data["examples"], f"{model} 的回复没被解析出来"


def test_non_json_reply_gives_json_specific_advice(stub_server):
    from app.providers import llm
    from app.providers.llm import LLMError

    text = llm.chat(
        base_url=stub_server, api_key="k", model="fault-notjson",
        messages=[{"role": "user", "content": "hi"}],
    )
    with pytest.raises(LLMError) as info:
        llm.extract_json(text)
    assert info.value.kind == "bad_json"
    assert info.value.hint


def test_openai_compatible_tts_writes_a_real_file(stub_server):
    """走一次真实的 TTS HTTP 路径（不是桩函数）。"""
    from app.paths import audio_dir
    from app.providers import tts

    name = tts.synthesize(
        "A short line for testing.",
        engine="openai",
        voice="alloy",
        openai_cfg={"base_url": stub_server, "api_key": "k", "model": "tts-1"},
    )
    path = audio_dir() / name
    assert path.is_file()
    assert path.stat().st_size > 0


def test_tts_reports_a_bad_voice_instead_of_silence(stub_server):
    from app.providers import tts

    with pytest.raises(tts.TTSError) as info:
        tts.synthesize(
            "hello",
            engine="openai",
            voice="fault-bad-voice",
            openai_cfg={"base_url": stub_server, "api_key": "k", "model": "tts-1"},
        )
    assert info.value.hint


def test_full_pipeline_against_the_stub(stub_server, isolated_data, monkeypatch):
    """整条流水线走真 HTTP：模型 → 解析 → 规范化 → 落库。

    这一条覆盖的是「上一个项目里一直没验证过的那一项」——
    云模型的真实调用。现在它有了一个不需要账号的版本。
    """
    from app import config, db

    _point_config_at(stub_server)
    config.save({"tts": {"engine": "openai", "openai": {"base_url": stub_server, "api_key": "k", "model": "tts-1", "voice": "alloy"}}})
    # 语音走真 HTTP，模型也走真 HTTP —— 两个都不桩。
    monkeypatch.setattr("app.providers.tts.audio_dir", lambda: __import__("app.paths", fromlist=["audio_dir"]).audio_dir())

    from app import pipeline

    lesson_id = db.create_lesson("appreciate", "word")
    result = pipeline.generate(lesson_id)

    assert result["examples"] >= 3
    assert result["audio"] == result["examples"], "语音应当全部成功"

    lesson = db.get_lesson(lesson_id)
    assert lesson["status"] == "ready"
    assert lesson["gloss"]

    readiness = db.readiness(lesson_id)
    assert readiness["ready"] and readiness["complete"]

    # 每个例句都要有「多种中文说法」—— 理论要求的核心机制。
    for ex in db.example_rows(lesson_id):
        assert len(ex["zh_variants"]) >= 2, f"{ex['sentence']} 只有一种中文说法"
        assert ex["scene_zh"]


def test_full_pipeline_with_generated_pictures(stub_server, isolated_data, stub_tts):
    """整条流水线，**带画图**，同样走真 HTTP。

    比上一条多覆盖三件事，都是只在 HTTP 那一层才会错的地方：

    1. ``/images/generations`` 的请求怎么拼（路径、body、鉴权头）；
    2. 返回的 ``b64_json`` 怎么解成字节（解错一个字，图片就是坏的，
       而界面只会显示一个加载失败的破图框）；
    3. 解出来的字节怎么判格式 —— **靠文件头，不信任何后缀**。

    语音在这里用桩：这一条要验的是图，把语音也压到真网络上只会让它更慢、
    更容易随机红。
    """
    from app import config, db
    from app.paths import images_dir

    _point_config_at(stub_server)
    config.save({
        "images": {
            "source": "llm",
            "llm": {"base_url": stub_server, "api_key": "k", "model": "any-image"},
        },
    })

    from app import pipeline

    lesson_id = db.create_lesson("appreciate", "word")
    result = pipeline.generate(lesson_id)

    assert result["examples"] >= 3
    assert result["images"] >= 1, "配图应当至少成功一张"

    rows = [r for r in db.example_rows(lesson_id) if r["scene_image"]]
    assert rows, "例句上应当挂着图片文件名"
    for row in rows:
        path = images_dir() / row["scene_image"]
        assert path.is_file() and path.stat().st_size > 0
        assert path.read_bytes()[:4] == b"\x89PNG", "存下来的应当是重命名后的真图片"


def test_picture_endpoint_returning_a_url_is_followed(stub_server, isolated_data):
    """有的厂商返回的是**地址**而不是图片数据，得再下一次。

    只认 ``b64_json`` 的话，换个厂商就会报「没返回图片」——
    而图片其实就在那个地址后面。模型名以 ``url-`` 开头就能走到这条分支。
    """
    from app import config
    from app.paths import images_dir
    from app.providers import images

    config.save({"images": {"llm": {"base_url": stub_server, "api_key": "k",
                                     "model": "url-any", "preset": "custom"}}})

    name = images.provide(
        query="ordering coffee",
        prompt="a photo of someone ordering coffee",
        source="llm",
        gen_cfg=config.load()["images"]["llm"],
    )
    path = images_dir() / name
    assert path.is_file()
    assert path.read_bytes()[:4] == b"\x89PNG"


def test_bad_picture_payload_is_refused_not_saved(stub_server, isolated_data):
    """接口返回 200，但里面那坨东西不是图片 —— 必须当场拒绝。

    不拒绝的话，一个 HTML 错误页会被原样存成 ``xxx.jpg``：
    界面上是一个永远加载不出来的破图框，而「配图成功」的计数还是加一的。
    """
    from app import config
    from app.providers import images

    config.save({"images": {"llm": {"base_url": stub_server, "api_key": "k",
                                     "model": "fault-image-badpayload",
                                     "preset": "custom"}}})

    with pytest.raises(images.ImageError) as info:
        images.provide(
            query="q", prompt="p", source="llm",
            gen_cfg=config.load()["images"]["llm"],
        )
    assert info.value.hint, "失败必须带上「下一步怎么办」"
