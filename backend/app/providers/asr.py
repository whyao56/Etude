"""语音转写（可选能力）。

**为什么是可选的**：跟读评分里唯一「能说出内容对不对」的办法是把录音变成文字。
这件事必须联网、必须有一个转写服务；而 Etude 的承诺是装上就能用，
不能因为「你没配转写服务」就让发音训练整块不可用。所以：

- 没配 → 只用声学指标打分（时长、停顿、电平），界面上把「内容」标成未检查；
- 配了 → 加一路词级对比，分数按权重合并。

接口形状只认 OpenAI 的 ``POST {base_url}/audio/transcriptions``（multipart，
字段 ``file`` / ``model`` / ``language``）。这是事实标准，本机 ollama、各家
兼容服务、官方 API 都是这个形状。
"""

from __future__ import annotations

import time

from . import llm


class ASRError(RuntimeError):
    """转写失败。带一句「接下来该干什么」，界面直接显示这句。"""

    def __init__(self, message: str, hint: str = "", kind: str = "unknown") -> None:
        super().__init__(message)
        self.hint = hint
        self.kind = kind

    def as_dict(self) -> dict:
        return {"error": str(self), "hint": self.hint, "kind": self.kind}


def configured(cfg: dict) -> bool:
    """能不能用。判据只有一条：填了 key。

    不检查 base_url 是否可达 —— 那是 ``check()`` 的事。启动时不做网络请求，
    否则没网的用户会看到一条启动就红的横幅，而实际上他只是没配转写服务。
    """
    asr = (cfg or {}).get("asr") or {}
    return bool(str(asr.get("api_key") or "").strip())


def transcribe(wav: bytes, cfg: dict, *, timeout: float = 60.0) -> dict:
    """把一段 WAV 转成文字。返回 ``{"text", "model", "seconds"}``。"""
    asr = (cfg or {}).get("asr") or {}
    if not wav:
        raise ASRError("录音是空的。", "重录一遍。", kind="empty")
    if not configured(cfg):
        raise ASRError(
            "还没配转写服务。",
            "设置 → 发音 → 转写服务，填一个 OpenAI 兼容的地址和 key。",
            kind="not_configured",
        )

    base_url = str(asr.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise ASRError("转写服务没填地址。", "设置 → 发音 → 转写服务。", kind="config")
    url = f"{base_url}/audio/transcriptions"
    model = str(asr.get("model") or "whisper-1").strip()
    language = str(asr.get("language") or "").strip()

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - 依赖已经装了，纯防御
        raise ASRError(
            "缺少 httpx，没法调用转写服务。", "重新安装完整发行包。", kind="import"
        ) from exc

    form = {"model": model}
    if language:
        form["language"] = language

    started = time.time()
    try:
        with httpx.Client(
            timeout=timeout, trust_env=not llm._is_local(base_url)
        ) as client:
            resp = client.post(
                url,
                data=form,
                files={"file": ("attempt.wav", wav, "audio/wav")},
                headers={"Authorization": f"Bearer {asr.get('api_key')}"},
            )
    except Exception as exc:  # noqa: BLE001
        raise ASRError(
            f"连不上转写服务：{exc}",
            "检查地址、网络，以及这个地址是不是要开代理。",
            kind="network",
        ) from exc

    if resp.status_code != 200:
        raise _http_error(resp, base_url)

    try:
        data = resp.json()
    except ValueError as exc:
        raise ASRError(
            "转写服务返回的不是 JSON。",
            "这个地址可能不是 OpenAI 兼容的 /audio/transcriptions。",
            kind="bad_json",
        ) from exc

    text = str(data.get("text") or "").strip()
    if not text:
        raise ASRError(
            "转写结果是空的。",
            "确认录音里真的有人说话、音量不太小。",
            kind="empty_text",
        )
    return {
        "text": text,
        "model": str(data.get("model") or model),
        "seconds": round(time.time() - started, 2),
    }


def _http_error(resp, base_url: str) -> ASRError:
    code = resp.status_code
    body = llm._short_body(resp.text)
    if code in (401, 403):
        return ASRError(
            f"转写服务拒绝了 key（HTTP {code}）。" + (f"服务返回：{body}" if body else ""),
            "检查转写服务的 key 是否有效、是否有这个模型的权限。",
            kind="auth",
        )
    if code == 404:
        return ASRError(
            f"转写接口不存在（HTTP 404）：{base_url}/audio/transcriptions",
            "填的地址应该是 base_url（通常以 /v1 结尾），不是完整接口地址。",
            kind="not_found",
        )
    if code == 413:
        return ASRError(
            "录音太大，服务拒绝了（HTTP 413）。",
            "别录太长 —— 跟读一句就够了。",
            kind="too_large",
        )
    if code == 429:
        return ASRError(
            "转写服务限流了（HTTP 429）。",
            "等一会儿再试，或者换个服务。",
            kind="rate_limit",
        )
    if code >= 500:
        return ASRError(
            f"转写服务内部出错（HTTP {code}）。" + (f"服务返回：{body}" if body else ""),
            "这不是你的配置问题，过一会儿再试。",
            kind="server",
        )
    return ASRError(
        f"转写失败（HTTP {code}）。" + (f"服务返回：{body}" if body else ""),
        "看一眼服务端的错误信息。",
        kind="http",
    )


def check(cfg: dict, *, timeout: float = 45.0) -> dict:
    """真跑一遍：合成一句 → 转写它 → 比对文字。

    **不会失败的检查不是检查。** 只测「连得上」的话，地址填错到某个
    返回 200 的首页也能过 —— 那种检查我已经吃过一次亏（见 DEVLOG 0.0.3）。
    """
    from .providers import tts as tts_mod

    if not configured(cfg):
        raise ASRError(
            "还没配转写服务。",
            "设置 → 发音 → 转写服务，填一个 OpenAI 兼容的地址和 key。",
            kind="not_configured",
        )

    probe = "This is a short test."
    tts_cfg = {
        "engine": "edge",
        "voice": "en-US-AriaNeural",
        "rate": "+0%",
        "volume": "+0%",
        "openai": {},
    }
    try:
        name = tts_mod.synthesize(probe, **{
            "engine": tts_cfg["engine"],
            "voice": tts_cfg["voice"],
            "rate": tts_cfg["rate"],
            "volume": tts_cfg["volume"],
            "openai_cfg": tts_cfg["openai"],
            "timeout": timeout,
        })
    except Exception as exc:  # noqa: BLE001
        raise ASRError(
            f"没法做这项检查：合成测试语音时失败（{exc}）。",
            "先解决语音引擎的问题，再来测转写。",
            kind="tts",
        ) from exc

    from ..paths import audio_dir

    wav = audio_dir() / name
    data = wav.read_bytes() if wav.exists() else b""
    if not data:
        raise ASRError(
            "测试语音没写出来，没法转写。", "先解决语音引擎的问题。", kind="tts"
        )

    # Edge TTS 吐的是 MP3。转写服务自己会解码，所以直接把 mp3 原样发过去 ——
    # 我们不在本地做转码（那要带一个解码器进来，为了一个探针不值得）。
    asr_cfg = {"asr": {**((cfg or {}).get("asr") or {}), "language": "en"}}
    result = _transcribe_any(data, asr_cfg, timeout=timeout, filename="probe.mp3",
                             mime="audio/mpeg")
    heard = result["text"].lower()
    hits = [w for w in ("test", "short", "this") if w in heard]
    return {
        "ok": True,
        "seconds": result["seconds"],
        "model": result["model"],
        "heard": result["text"],
        "matched": len(hits),
    }


def _transcribe_any(
    data: bytes, cfg: dict, *, timeout: float, filename: str, mime: str
) -> dict:
    """和 ``transcribe`` 同一条路，但允许指定文件名/类型（探针发的是 mp3）。"""
    asr = (cfg or {}).get("asr") or {}
    base_url = str(asr.get("base_url") or "").strip().rstrip("/")
    url = f"{base_url}/audio/transcriptions"
    form = {"model": str(asr.get("model") or "whisper-1")}
    if asr.get("language"):
        form["language"] = str(asr["language"])

    import httpx

    started = time.time()
    try:
        with httpx.Client(timeout=timeout, trust_env=not llm._is_local(base_url)) as client:
            resp = client.post(
                url,
                data=form,
                files={"file": (filename, data, mime)},
                headers={"Authorization": f"Bearer {asr.get('api_key')}"},
            )
    except Exception as exc:  # noqa: BLE001
        raise ASRError(
            f"连不上转写服务：{exc}", "检查地址与网络。", kind="network"
        ) from exc
    if resp.status_code != 200:
        raise _http_error(resp, base_url)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ASRError(
            "转写服务返回的不是 JSON。", "这个地址可能不是 OpenAI 兼容接口。",
            kind="bad_json",
        ) from exc
    return {
        "text": str(payload.get("text") or "").strip(),
        "model": str(payload.get("model") or form["model"]),
        "seconds": round(time.time() - started, 2),
    }
