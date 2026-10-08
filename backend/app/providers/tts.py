"""语音合成。

两个引擎：
- **edge**：微软 Edge 的神经网络语音。免费、不需要密钥、音质接近真人，
  英式/美式/澳式都有多种音色。默认走这条。
- **openai**：任意 OpenAI 兼容的 ``/audio/speech`` 端点（OpenAI、豆包、
  硅基流动等）。要额外花钱，但音色可控性更强。

音频按内容寻址存盘（文件名 = 引擎+音色+文本的哈希），
所以同一句话重复生成不会产生第二个文件，换音色也不会互相覆盖。
"""

from __future__ import annotations

import asyncio
import hashlib
import urllib.parse
from pathlib import Path

import httpx

from ..paths import audio_dir
from .llm import LLMError, explain_error  # 同一套「能修好的方向」的错误口径

# 预置音色。都是 Edge TTS 的公开音色名，可以在设置里改。
EDGE_VOICES: list[dict[str, str]] = [
    {"id": "en-US-AriaNeural", "label": "美式女声 · Aria（默认，清晰稳）"},
    {"id": "en-US-GuyNeural", "label": "美式男声 · Guy"},
    {"id": "en-US-JennyNeural", "label": "美式女声 · Jenny（自然口语）"},
    {"id": "en-US-AndrewNeural", "label": "美式男声 · Andrew（沉稳）"},
    {"id": "en-GB-SoniaNeural", "label": "英式女声 · Sonia"},
    {"id": "en-GB-RyanNeural", "label": "英式男声 · Ryan"},
    {"id": "en-AU-NatashaNeural", "label": "澳式女声 · Natasha"},
]

OPENAI_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]

# 供前端做音色试听。
def voice_catalog() -> dict:
    return {"edge": EDGE_VOICES, "openai": OPENAI_VOICES}


class TTSError(RuntimeError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def cache_name(engine: str, voice: str, text: str, rate: str = "+0%") -> str:
    """内容寻址的文件名。同样的输入永远得到同一个名字。"""
    digest = hashlib.sha256(
        "\x1f".join([engine, voice, rate, text]).encode("utf-8")
    ).hexdigest()[:20]
    return f"{engine}-{digest}.mp3"


def resolve_voice(cfg: dict) -> str:
    """从整段 tts 配置里取出**这个引擎真正要用的那个音色**。

    这里踩过一个很贵的坑，写下来免得再犯：

    配置里有两个音色字段 —— ``tts.voice`` 是 Edge 的音色（``en-US-AriaNeural``），
    ``tts.openai.voice`` 是大模型端点的音色（``alloy``）。它们是**两套命名**，
    互不通用。而合成、补语音、试听三条路径原本都只读 ``tts.voice``，
    于是用户一切到「大模型语音」，程序就会把 ``en-US-AriaNeural``
    发给 ``/audio/speech``：

      · OpenAI 官方 → 400 ``Invalid voice``
      · 豆包 / 硅基流动 → 同样报参数错

    每一条语音都失败，而失败只写进日志（界面上是一次静默的「全包没声音」）。
    用户看到的现象就是「例句读不出来」，而且**设置页看着一切正常**——
    因为自检当时只 ``import edge_tts`` 就算过了，试听走的又是同一条错路径。

    所以：音色必须按引擎取，取法只能有这一个地方。
    """
    engine = (cfg.get("engine") or "edge").strip().lower()
    if engine == "openai":
        sub = cfg.get("openai") or {}
        voice = (sub.get("voice") or "").strip()
        return voice or OPENAI_VOICES[0]
    return (cfg.get("voice") or "").strip() or "en-US-AriaNeural"


def needs_key(cfg: dict) -> bool:
    """这个引擎要不要密钥。给设置页和自检共用，避免两边各判一次。"""
    return (cfg.get("engine") or "edge").strip().lower() == "openai"


def synthesize(
    text: str,
    *,
    engine: str = "edge",
    voice: str = "en-US-AriaNeural",
    rate: str = "+0%",
    volume: str = "+0%",
    openai_cfg: dict | None = None,
    timeout: float = 60.0,
    force: bool = False,
) -> str:
    """生成（或复用）一条音频，返回**相对 audio 目录**的路径。

    返回值是相对路径而不是绝对路径：数据库里存绝对路径的话，
    用户换台机器或改了数据目录，所有记录立刻变成死链。
    """
    text = (text or "").strip()
    if not text:
        raise TTSError("没有要合成的文本。")

    name = cache_name(engine, voice, text, rate)
    target = audio_dir() / name
    if target.exists() and target.stat().st_size > 0 and not force:
        return name

    if engine == "openai":
        _synthesize_openai(text, voice, target, openai_cfg or {}, timeout)
    else:
        _synthesize_edge(text, voice, rate, volume, target, timeout)

    if not target.exists() or target.stat().st_size == 0:
        # 有的库会「成功返回但一个字节都没写」。
        # 不检查这个的话，界面会画出一个点了没反应的播放按钮。
        raise TTSError(
            f"语音合成没有产出文件（音色 {voice}）。",
            "换一个音色再试；如果换了还是这样，把这段报错反馈给作者。",
        )
    return name


def _synthesize_edge(
    text: str, voice: str, rate: str, volume: str, target: Path, timeout: float
) -> None:
    try:
        import edge_tts
    except ImportError as exc:
        raise TTSError(
            "缺少语音组件 edge-tts。",
            "这不是网络问题。请重新安装完整发行包"
            "（或执行 pip install edge-tts）。",
        ) from exc

    async def run() -> None:
        communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
        await asyncio.wait_for(communicate.save(str(target)), timeout=timeout)

    try:
        asyncio.run(run())
    except asyncio.TimeoutError as exc:
        raise TTSError(
            "语音合成超时。",
            "Edge 语音需要联网。检查网络，或改用其他音色再试。",
        ) from exc
    except ImportError as exc:
        raise TTSError(
            "缺少语音组件。", "请重新安装完整发行包。"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - edge-tts 会抛各种自己的异常
        message = str(exc)
        if "No audio was received" in message or "Invalid" in message:
            raise TTSError(
                f"这个音色合成不出音频：{voice}",
                "去设置里换一个音色，或把语速改回 +0%。",
            ) from exc
        raise TTSError(
            f"语音合成失败：{message[:200]}",
            "先检查网络；Edge 语音偶尔会短暂不可用，过一会儿再试。",
        ) from exc


def _synthesize_openai(
    text: str, voice: str, target: Path, cfg: dict, timeout: float
) -> None:
    base_url = (cfg.get("base_url") or "").strip()
    api_key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model") or "").strip() or "tts-1"
    if not base_url:
        raise TTSError(
            "选了大模型语音，但没填接口地址。",
            "去设置里填上 base_url（OpenAI 是 https://api.openai.com/v1）。",
        )

    url = base_url.rstrip("/") + "/audio/speech"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=15.0),
            trust_env=not _is_local(base_url),
        ) as client:
            resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        target.write_bytes(resp.content)
    except httpx.HTTPStatusError as exc:
        # 复用大模型那套翻译，保证同样的错在不同功能里说法一致。
        err = explain_error(exc, base_url, model)
        raise TTSError(f"大模型语音合成失败：{err}", err.hint) from exc
    except (httpx.HTTPError, OSError) as exc:
        err = explain_error(exc, base_url, model)
        raise TTSError(f"大模型语音合成失败：{err}", err.hint) from exc


def _is_local(base_url: str) -> bool:
    try:
        host = urllib.parse.urlparse(base_url).hostname or ""
    except ValueError:
        return False
    return host.lower() in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def synthesize_cfg(text: str, cfg: dict, *, timeout: float = 60.0, force: bool = False) -> str:
    """按整段 tts 配置合成一条语音。**训练包生成走这条。**

    存在意义和 :func:`check` 一样：调用方只交出配置，不自己挑音色。
    三处调用点（生成训练包、补语音、试听）以前各自写一遍取音色的逻辑，
    于是三处一起错。现在只剩一个地方会错。
    """
    return synthesize(
        text,
        engine=(cfg.get("engine") or "edge").strip().lower(),
        voice=resolve_voice(cfg),
        rate=cfg.get("rate", "+0%"),
        volume=cfg.get("volume", "+0%"),
        openai_cfg=cfg.get("openai", {}),
        timeout=timeout,
        force=force,
    )


def check(cfg: dict, *, timeout: float = 30.0) -> dict:
    """设置页的「试听一句」和自检共用。

    传的是**整段 tts 配置**而不是零散的 engine / voice —— 理由见
    :func:`resolve_voice`：只要还允许调用方自己挑音色，就一定会有人挑错。
    音色的决定权在配置里，不在这里。
    """
    probe = "This is a short test."
    engine = (cfg.get("engine") or "edge").strip().lower()
    voice = resolve_voice(cfg)
    name = synthesize_cfg(probe, cfg, timeout=timeout, force=True)
    path = audio_dir() / name
    size = path.stat().st_size if path.exists() else 0
    return {"ok": True, "engine": engine, "voice": voice, "bytes": size}


__all__ = [
    "EDGE_VOICES",
    "OPENAI_VOICES",
    "TTSError",
    "cache_name",
    "check",
    "needs_key",
    "resolve_voice",
    "synthesize",
    "synthesize_cfg",
    "voice_catalog",
]
