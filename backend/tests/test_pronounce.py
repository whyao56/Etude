"""跟读评分（``app.pronounce``）的用例。

这些用例的重点不是「分数等于多少」，而是三件事：
1. 量出来的数**真的来自波形**（长度、停顿、音量）；
2. 没有转写服务时，「内容」是**未检查**而不是 0 分；
3. 录不上的时候给的是一句人话，不是异常。
"""

from __future__ import annotations

import pytest

from app import pronounce
from wavfmt import loud_wav, quiet_wav, silence_wav, speech_then_pause, tone_wav


def test_parse_wav_reads_length_and_rate():
    samples, rate = pronounce.parse_wav(tone_wav(seconds=1.5, rate=8000))
    assert rate == 8000
    assert len(samples) == pytest.approx(12000, abs=2)
    assert max(abs(v) for v in samples) == pytest.approx(0.3, abs=0.02)


def test_parse_wav_mixes_stereo_down_to_mono():
    samples, rate = pronounce.parse_wav(tone_wav(seconds=0.5, channels=2))
    assert rate == 16000
    assert len(samples) == pytest.approx(8000, abs=2)


def test_parse_wav_rejects_rubbish():
    with pytest.raises(ValueError):
        pronounce.parse_wav(b"this is not a wav file at all, not even close")


def test_analyze_finds_the_pause_in_the_middle():
    samples, rate = pronounce.parse_wav(speech_then_pause(first=1.0, pause=0.4, second=0.8))
    result = pronounce.analyze(samples, rate)
    assert result["duration"] == pytest.approx(2.2, abs=0.05)
    assert len(result["pauses"]) == 1
    assert result["longest_pause"] == pytest.approx(0.4, abs=0.06)


def test_analyze_reports_quiet_recording_as_low_peak():
    samples, rate = pronounce.parse_wav(quiet_wav())
    assert pronounce.analyze(samples, rate)["peak"] < 0.03


def test_level_item_rejects_a_recording_nobody_can_hear():
    item = pronounce._level({"peak": 0.001, "clipping": 0.0})
    assert item["level"] == "bad"
    assert item["score"] is not None and item["score"] < 40
    assert "听不见" in item["detail"]


def test_level_item_flags_clipping():
    item = pronounce._level({"peak": 0.99, "clipping": 0.15})
    assert item["level"] == "warn"
    assert "爆音" in item["detail"]


def test_align_words_only_blames_the_word_that_is_missing():
    # 漏掉 rather 之后，逐位比较会把后面全判错；LCS 只该标出这一个。
    result = pronounce.align_words(
        "I would rather stay home tonight", "i would stay home tonight"
    )
    missed = [item["word"] for item in result["items"] if not item["hit"]]
    assert missed == ["rather"]
    assert result["ratio"] == pytest.approx(5 / 6, abs=0.01)


def test_align_words_tolerates_curly_apostrophes():
    result = pronounce.align_words("I don't know", "I don’t know")
    assert result["ratio"] == 1.0


def test_content_is_skipped_when_no_transcription_is_configured():
    items = pronounce.evaluate(
        tone_wav(seconds=1.2), target="I would rather stay home", ref_seconds=1.2
    )["items"]
    content = next(item for item in items if item["id"] == "content")
    assert content["level"] == "skip"
    assert content["score"] is None
    assert content["value"] == "未检查"


def test_acoustic_score_is_not_dragged_down_by_the_unjudged_item():
    """没判「内容」时，总分不该被它拖低 —— 0 分会被读成「我念错了」。"""
    result = pronounce.evaluate(
        tone_wav(seconds=1.2), target="a b c", ref_seconds=1.2
    )
    assert result["mode"] == "acoustic"
    assert result["score"] > 70


def test_completeness_marks_guessed_reference_duration():
    item = pronounce._completeness(1.0, 0.0, word_count=13)
    assert "估" in item["detail"]


def test_too_short_recording_is_a_conclusion_not_an_exception():
    result = pronounce.evaluate(tone_wav(seconds=0.1), target="hi")
    assert result["score"] == 0.0
    assert "太短" in result["advice"]


def test_silence_only_recording_says_it_heard_nothing():
    result = pronounce.evaluate(silence_wav(1.0), target="hello")
    assert result["score"] == 0.0
    assert "说话声" in result["advice"]


def test_asr_path_judges_the_words(monkeypatch):
    import app.providers.asr as asr_mod

    monkeypatch.setattr(
        asr_mod, "configured", lambda cfg: True
    )
    monkeypatch.setattr(
        asr_mod,
        "transcribe",
        lambda wav, cfg, **kw: {"text": "i would rather stay home tonight", "model": "m", "seconds": 0.1},
    )

    result = pronounce.evaluate(
        tone_wav(seconds=1.4),
        target="I would rather stay home tonight",
        ref_seconds=1.4,
        cfg={"mode": "auto", "asr": {"api_key": "x"}},
    )
    content = next(item for item in result["items"] if item["id"] == "content")
    assert result["mode"] == "mixed"
    assert content["level"] == "ok"
    assert content["score"] == 100.0


def test_asr_failure_keeps_the_acoustic_score(monkeypatch):
    import app.providers.asr as asr_mod

    def boom(wav, cfg, **kw):
        raise asr_mod.ASRError("转写服务返回 401", "检查 key。")

    monkeypatch.setattr(asr_mod, "configured", lambda cfg: True)
    monkeypatch.setattr(asr_mod, "transcribe", boom)

    result = pronounce.evaluate(
        tone_wav(seconds=1.2),
        target="I would rather stay home",
        ref_seconds=1.2,
        cfg={"mode": "auto", "asr": {"api_key": "x"}},
    )
    content = next(item for item in result["items"] if item["id"] == "content")
    assert content["level"] == "skip"
    assert "401" in content["detail"]
    assert result["score"] > 60


def test_loud_recording_is_measured_as_clipping():
    samples, rate = pronounce.parse_wav(loud_wav(0.5))
    analysis = pronounce.analyze(samples, rate)
    assert analysis["clipping"] > 0.02
