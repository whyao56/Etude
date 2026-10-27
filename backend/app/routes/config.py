from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from .. import config
from ..providers import images as images_mod
from ..providers import llm, tts
from ..schemas import ConfigIn, PresetIn

router = APIRouter()


@router.get("/api/config")
def get_config() -> dict:
    return config.public_view()


@router.post("/api/config")
def post_config(payload: ConfigIn) -> dict:
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    merged = config.save(patch)
    return {"ok": True, "config": config.public_view(merged)}


@router.post("/api/config/preset")
def post_preset(payload: PresetIn) -> dict:
    if payload.preset not in llm.PRESETS:
        raise HTTPException(400, f"没有这个厂商：{payload.preset}")
    merged = config.save(llm.apply_preset(config.load(), payload.preset))
    return {"ok": True, "config": config.public_view(merged)}


@router.post("/api/config/test-image")
def test_image() -> dict:
    cfg = config.load().get("images", {})
    try:
        return images_mod.check(
            source=cfg.get("source", "search"),
            gen_cfg=dict(cfg.get("llm") or {}),
        )
    except images_mod.ImageError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": str(exc), "hint": exc.hint, "kind": exc.kind},
        )


@router.post("/api/config/test-llm")
def test_llm() -> dict:
    cfg = config.load()["llm"]
    try:
        return llm.check(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            timeout=min(float(cfg.get("timeout", 120.0)), 30.0),
        )
    except llm.LLMError as exc:
        return JSONResponse(status_code=400, content=exc.as_dict())


@router.post("/api/config/test-tts")
def test_tts() -> dict:
    cfg = config.load()["tts"]
    try:
        # 整段配置交进去，音色由 tts.resolve_voice 按引擎决定。
        # 0.0.2 之前这里传的是 cfg["voice"]（Edge 音色名），
        # 切到「大模型语音」后试听和生成会一起失败 —— 见 resolve_voice 的注释。
        return tts.check(cfg)
    except tts.TTSError as exc:
        return JSONResponse(
            status_code=400, content={"error": str(exc), "hint": exc.hint}
        )
