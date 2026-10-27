"""跟读录音的接口（0.1.0）。

三条硬约束，都是踩过才知道的：

1. **录音走原始字节，不走 JSON/base64。** 一段 3 秒的 16bit 16kHz 单声道
   是 ~96KB，base64 之后 128KB 还要在 JS 里拼字符串；直接 POST
   ``audio/wav`` 更省事，也少一层编码出错的可能。
2. **文件名一律白名单。** 录音是用户自己录的，但它照样是一个能被
   ``..%2F..%2Fconfig.json`` 打穿的文件名参数。
3. **评分失败不是接口失败。** 「没听见」是这次练习的结论，不是程序出错 ——
   所以照样落库、照样返回 200，界面上才有东西可说。
"""

from __future__ import annotations

import hashlib
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import config, db, pronounce
from ..paths import recordings_dir
from ..providers import asr as asr_mod

router = APIRouter()

# 8 MB ≈ 4 分钟 16kHz 单声道。跟读一句最多十几秒，这个上限只是防手滑。
MAX_WAV_BYTES = 8 * 1024 * 1024
_RECORDING_NAME = re.compile(r"^[0-9a-f]{16}\.wav$")


def _settings() -> dict:
    return config.load().get("pronounce") or {}


def _remember(wav: bytes) -> str:
    """按内容寻址存录音，返回文件名。

    内容寻址的理由和语音、配图一样：同一段录音不会存两份，
    搬数据目录时不会留死链。**但录音是不能重生成的东西**，
    所以它单独放在 ``recordings/``，不和模型语音混在一起。
    """
    digest = hashlib.sha1(wav).hexdigest()[:16]
    name = f"{digest}.wav"
    path = recordings_dir() / name
    if not path.exists():
        path.write_bytes(wav)
    return name


async def _read_wav(request: Request) -> bytes:
    data = await request.body()
    if not data:
        raise HTTPException(400, "录音是空的。")
    if len(data) > MAX_WAV_BYTES:
        raise HTTPException(
            413, "这段录音太长了（超过 8 MB）。跟读一句话就够了。"
        )
    return data


def _finish(
    wav: bytes,
    *,
    sentence: str,
    ref_seconds: float,
    card_id: int = 0,
    example_id: int = 0,
    lesson_id: int = 0,
    channel: str = "speak",
    word_count: int = 0,
) -> dict:
    settings = _settings()
    mode = str(settings.get("mode") or "auto")
    result = pronounce.evaluate(
        wav,
        target=sentence,
        ref_seconds=ref_seconds,
        word_count=word_count,
        cfg={"mode": mode, "asr": dict(settings.get("asr") or {})},
    )

    audio = ""
    if settings.get("save_recordings", True):
        audio = _remember(wav)

    attempt = db.record_attempt(
        card_id=card_id,
        lesson_id=lesson_id,
        example_id=example_id,
        channel=channel,
        mode=result["mode"],
        score=result["score"],
        seconds=float(result["analysis"].get("duration") or 0.0),
        audio=audio,
        detail={
            "items": result["items"],
            "heard": result["heard"],
            "advice": result["advice"],
            "analysis": result["analysis"],
            "asr_error": result["asr_error"],
        },
    )
    result["attempt"] = attempt
    result["audio"] = audio
    result["summary"] = db.attempt_summary(card_id=card_id, example_id=example_id)
    return result


# ------------------------------------------------------------------ 打分


@router.post("/api/cards/{card_id}/attempt")
async def score_card(card_id: int, request: Request, ref_seconds: float = 0.0) -> dict:
    """给一张卡跟读打分。

    句子从数据库里取，不由前端传 —— 前端传的话，改一个参数就能让
    「随便念点什么」拿到高分，那份分数就再也不是「这句话念得怎么样」了。
    """
    payload = db.card_payload(card_id)
    if payload is None:
        raise HTTPException(404, "没有这张卡。")
    example = payload.get("example") or {}
    card = payload.get("card") or {}
    wav = await _read_wav(request)
    return _finish(
        wav,
        sentence=str(example.get("sentence") or ""),
        ref_seconds=ref_seconds,
        card_id=card_id,
        example_id=int(card.get("example_id") or 0),
        lesson_id=int(card.get("lesson_id") or 0),
        channel=str(card.get("channel") or "speak"),
        word_count=len(str(example.get("sentence") or "").split()),
    )


@router.post("/api/examples/{example_id}/attempt")
async def score_example(
    example_id: int, request: Request, ref_seconds: float = 0.0
) -> dict:
    """训练包详情页里的「试一试」—— 不挂在某张卡上。"""
    example = db.example_by_id(example_id)
    if example is None:
        raise HTTPException(404, "没有这条例句。")
    wav = await _read_wav(request)
    sentence = str(example.get("sentence") or "")
    return _finish(
        wav,
        sentence=sentence,
        ref_seconds=ref_seconds,
        example_id=example_id,
        lesson_id=int(example.get("lesson_id") or 0),
        channel="speak",
        word_count=len(sentence.split()),
    )


@router.get("/api/attempts")
def list_attempts(card_id: int = 0, example_id: int = 0, limit: int = 20) -> dict:
    return {
        "attempts": db.list_attempts(card_id=card_id, example_id=example_id, limit=limit),
        "summary": db.attempt_summary(card_id=card_id, example_id=example_id),
    }


@router.get("/api/recordings/{name}")
def get_recording(name: str) -> FileResponse:
    if not _RECORDING_NAME.match(name):
        raise HTTPException(400, "文件名不合法。")
    path = recordings_dir() / name
    if not path.exists():
        raise HTTPException(404, "这段录音不在了。")
    return FileResponse(
        path,
        media_type="audio/wav",
        # 内容寻址的名字不会变内容，可以长缓存 —— 和语音、配图同一个口径。
        headers={"Cache-Control": "max-age=31536000"},
    )


# ------------------------------------------------------------------ 状态


@router.get("/api/pronounce")
def pronounce_status() -> dict:
    """发音训练在当前配置下**能不能用、走到哪一档**。

    界面拿它决定：显示不显示录音按钮、要不要提示去配转写服务、
    以及「内容对不对」这一项是「未检查」还是真的会判。
    """
    settings = _settings()
    asr_cfg = dict(settings.get("asr") or {})
    mode = str(settings.get("mode") or "auto")
    ready = asr_mod.configured({"asr": asr_cfg})
    return {
        "enabled": bool(settings.get("enabled", True)),
        "mode": mode,
        "asr_ready": ready,
        "asr_model": str(asr_cfg.get("model") or ""),
        "save_recordings": bool(settings.get("save_recordings", True)),
        "count": db.attempts_in_repository(),
        # 一句话说清现在处在哪一档，界面直接显示 —— 比让用户自己推要好。
        "note": (
            "会做词级对比（录音会转成文字）。"
            if ready
            else "只测时长、停顿和音量；配上转写服务才能判内容对不对。"
        ),
    }


@router.post("/api/pronounce/check")
def check_asr() -> dict:
    """真跑一遍转写：合成一句 → 转写它 → 比对文字。

    返回 200 表示整条链路通了（TTS → 服务 → 文本）。失败时给
    一句「接下来该干什么」，而不是把 httpx 的异常字符串甩出去。
    """
    settings = _settings()
    cfg = {"mode": settings.get("mode") or "auto", "asr": dict(settings.get("asr") or {})}
    if not asr_mod.configured(cfg):
        return JSONResponse(
            status_code=400,
            content={
                "error": "还没配转写服务。",
                "hint": "设置 → 发音 → 转写服务，填一个 OpenAI 兼容的地址和 key。",
                "kind": "not_configured",
            },
        )
    try:
        return asr_mod.check(cfg)
    except asr_mod.ASRError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": str(exc), "hint": exc.hint, "kind": exc.kind},
        )
