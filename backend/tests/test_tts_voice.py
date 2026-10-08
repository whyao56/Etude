"""语音音色的解析。

这组用例针对的是一个**静默失败**：配置里有两个互不通用的音色字段 ——
``tts.voice`` 是 Edge 的音色名（``en-US-AriaNeural``），
``tts.openai.voice`` 是大模型端点的音色名（``alloy``）。

0.0.2 的合成、补语音、试听三条路径都只读 ``tts.voice``。用户一切到
「大模型语音」，程序就把 ``en-US-AriaNeural`` 发给 ``/audio/speech``，
服务端回 400 ``Invalid voice``，**每一条语音都失败**，
而失败只写进日志 —— 界面上就是一次静默的「整包没声音」，
设置页还看着一切正常。

所以这里盯的不是「函数返回值对不对」，而是「切引擎之后发出去的音色
有没有跟着换」。
"""

from __future__ import annotations

import pytest


def test_edge_engine_uses_the_edge_voice():
    from app.providers import tts

    assert tts.resolve_voice({"engine": "edge", "voice": "en-US-GuyNeural"}) == "en-US-GuyNeural"


def test_openai_engine_uses_the_openai_voice():
    """这条是整组里最要紧的一条 —— 它守着的就是上面那个坑。"""
    from app.providers import tts

    cfg = {
        "engine": "openai",
        "voice": "en-US-AriaNeural",          # Edge 的音色名，大模型端点不认
        "openai": {"voice": "alloy"},
    }
    assert tts.resolve_voice(cfg) == "alloy", "切到大模型语音后，音色必须换成大模型那一套"


def test_openai_engine_never_falls_back_to_the_edge_voice():
    """大模型语音没配音色时，退回**大模型自己的**默认值，而不是 Edge 的。

    退回 Edge 的名字就等于把上面那个坑重新挖一遍：一样是 400，
    一样是整包静音，而且更难查 —— 因为「配置里确实填了音色」。
    """
    from app.providers import tts

    got = tts.resolve_voice({"engine": "openai", "voice": "en-US-AriaNeural", "openai": {}})
    assert got in tts.OPENAI_VOICES
    assert got not in {v["id"] for v in tts.EDGE_VOICES}


def test_engine_name_is_trimmed_and_case_insensitive():
    from app.providers import tts

    assert tts.resolve_voice({"engine": " OpenAI ", "openai": {"voice": "nova"}}) == "nova"


def test_missing_engine_means_edge():
    """老配置里没有 engine 这个键。缺省必须是 Edge —— 那是它的历史行为。"""
    from app.providers import tts

    assert tts.resolve_voice({}) == "en-US-AriaNeural"
    assert tts.resolve_voice({"voice": ""}) == "en-US-AriaNeural"


def test_only_the_openai_engine_needs_a_key():
    from app.providers import tts

    assert tts.needs_key({"engine": "edge"}) is False
    assert tts.needs_key({}) is False
    assert tts.needs_key({"engine": "openai"}) is True


def test_the_voice_catalog_marks_which_engine_each_voice_belongs_to():
    """两套音色必须在目录里分得开。

    混在一起的话，设置页的下拉框会把 ``alloy`` 和 ``en-US-AriaNeural``
    并排列出来，用户选了不匹配的那个，就又是一次静默静音。
    """
    from app.providers import tts

    cat = tts.voice_catalog()
    edge_ids = {v["id"] for v in cat["edge"]}
    assert "en-US-AriaNeural" in edge_ids
    assert set(cat["openai"]) == set(tts.OPENAI_VOICES)
    assert edge_ids.isdisjoint(set(cat["openai"]))


def test_synthesize_cfg_passes_the_resolved_voice(monkeypatch, isolated_data):
    """``synthesize_cfg`` 必须把**解析后**的音色交下去。

    这条用的是「把合成函数换成记录器」的办法：真正要断言的不是
    「合成有没有成功」，而是「发出去的音色是哪一个」。
    合成成功与否要靠网络，那是另一件事。
    """
    from app.providers import tts

    seen: dict = {}

    def fake_synthesize(text, **kwargs):
        seen.update(kwargs)
        seen["text"] = text
        return "fake.mp3"

    monkeypatch.setattr(tts, "synthesize", fake_synthesize)

    cfg = {"engine": "openai", "voice": "en-US-AriaNeural", "openai": {"voice": "shimmer"}}
    assert tts.synthesize_cfg("Hello there.", cfg) == "fake.mp3"
    assert seen["engine"] == "openai"
    assert seen["voice"] == "shimmer"
    assert seen["text"] == "Hello there."


def test_synthesize_cfg_forwards_force(monkeypatch, isolated_data):
    """「重新生成语音」要能强制绕过缓存。

    不透传 force 的话，用户点「重新生成」会拿回上一次缓存的坏音频，
    然后以为是按钮没生效。
    """
    from app.providers import tts

    seen: dict = {}
    monkeypatch.setattr(
        tts, "synthesize",
        lambda text, **kw: (seen.update(kw), "x.mp3")[1],
    )
    tts.synthesize_cfg("x", {"engine": "edge", "voice": "en-US-AriaNeural"}, force=True)
    assert seen["force"] is True


def test_every_language_has_a_speech_tag_for_the_browser_fallback():
    """每种语言都要带一个 BCP-47 的 ``speech`` 标记。

    前端在 Edge / 大模型都拿不到音频时会退到浏览器的 ``speechSynthesis``，
    而它需要一个 ``lang``（``en-US`` / ``ja-JP``）。缺了这个字段，
    兜底那条路会把语音按当前系统语言读出来 —— 练英语却听见中文腔。
    """
    from app.prompts import LANGUAGES

    for lang in LANGUAGES:
        assert lang.get("speech"), f"{lang.get('id')} 没有 speech 标记"
        assert "-" in lang["speech"], f"{lang['speech']} 看起来不是一个地区化标记"
