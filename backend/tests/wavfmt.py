"""造 WAV 的小工具，给跟读相关用例共用。

为什么要真造波形而不是塞一段随便的字节：评分代码里最要命的一类错误是
「**看着有数、其实全错**」—— 比如把静音算成说话、把爆音算成正常。
只有喂进已知形状的波形（多长、多大、中间有没有静音）才能验证它量得对不对。
"""

from __future__ import annotations

import math
import struct


def tone_wav(
    *,
    seconds: float = 1.0,
    rate: int = 16000,
    freq: float = 180.0,
    peak: float = 0.3,
    channels: int = 1,
) -> bytes:
    """一段纯音。``peak`` 是 0~1 的振幅。"""
    frames = bytearray()
    count = int(rate * seconds)
    for i in range(count):
        value = int(32767 * peak * math.sin(2 * math.pi * freq * i / rate))
        for _ in range(channels):
            frames += struct.pack("<h", value)
    return _wrap(bytes(frames), rate=rate, channels=channels, bits=16)


def silence(seconds: float, *, rate: int = 16000) -> bytes:
    """裸 PCM 的静音。**没有 RIFF 头** —— 它是给拼接用的中间件。"""
    return b"\x00\x00" * int(rate * seconds)


def silence_wav(seconds: float = 1.0, *, rate: int = 16000) -> bytes:
    """一整段静音录音（有 RIFF 头），用来验「什么都没录上」。"""
    return _wrap(silence(seconds, rate=rate), rate=rate)


def speech_then_pause(
    *, first: float = 1.0, pause: float = 0.4, second: float = 0.8, rate: int = 16000
) -> bytes:
    """说一句、停一下、再说一句。用来验停顿检测。"""
    body = (
        tone_wav(seconds=first, rate=rate)[44:]
        + silence(pause, rate=rate)
        + tone_wav(seconds=second, rate=rate)[44:]
    )
    return _wrap(body, rate=rate)


def loud_wav(seconds: float = 1.0, *, peak: float = 0.999, rate: int = 16000) -> bytes:
    """几乎削顶的录音，用来验爆音判定。"""
    frames = bytearray()
    count = int(rate * seconds)
    for i in range(count):
        value = 32767 if i % 4 < 2 else -32768
        frames += struct.pack("<h", max(-32768, min(32767, int(value * peak))))
    return _wrap(bytes(frames), rate=rate)


def quiet_wav(seconds: float = 1.0, *, rate: int = 16000) -> bytes:
    frames = bytearray()
    for i in range(int(rate * seconds)):
        frames += struct.pack("<h", int(200 * math.sin(2 * math.pi * 180 * i / rate)))
    return _wrap(bytes(frames), rate=rate)


def _wrap(pcm: bytes, *, rate: int, channels: int = 1, bits: int = 16) -> bytes:
    byte_rate = rate * channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, channels, rate, byte_rate, channels * bits // 8, bits
    )
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm
