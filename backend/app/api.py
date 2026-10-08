"""HTTP 接口。

两条贯穿全文件的原则：

1. **每个响应都能自证身份。** ``/api/health`` 返回版本、启动时刻、运行形态、
   数据目录和进程号。上一个项目里踩过：端口上留着一个旧进程在答话，
   前端每次从磁盘现读（所以是新的），服务却是一直在跑的那个（所以是旧的），
   用户看到「检查更新显示 0.3.1，但界面是 0.4.0」。有了这个接口，
   旧进程一眼可辨。

2. **失败要说清方向。** 所有可能失败的操作都返回 ``hint``，
   而不是把 Python 的异常字符串直接甩给用户。
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__, config, db, pipeline, srs
from .paths import (
    DEFAULT_PORT,
    audio_dir,
    frontend_dir,
    is_frozen,
    writable_root,
)
from .providers import llm, tts

STARTED_AT = time.time()

# 生成是 I/O 等待型（等模型、等语音），线程数不需要大。
# 但也不能只有 1 —— 用户在等第一个包的时候往往想再丢一个进去。
_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="etude-gen")

app = FastAPI(title="Etude", version=__version__, docs_url=None, redoc_url=None)


# ------------------------------------------------------------------ 模型


class LessonIn(BaseModel):
    target: str = Field(min_length=1, max_length=300)
    kind: str | None = None


class GradeIn(BaseModel):
    grade: int = Field(ge=0, le=3)
    seconds: float = Field(default=0.0, ge=0.0, le=3600.0)


class ConfigIn(BaseModel):
    llm: dict | None = None
    tts: dict | None = None
    study: dict | None = None
    channels: dict | None = None
    server: dict | None = None


class PresetIn(BaseModel):
    preset: str


class BackupIn(BaseModel):
    name: str = ""


# ------------------------------------------------------------------ 自述


@app.get("/api/health")
def health() -> dict:
    return {
        "app": "Etude",
        "version": __version__,
        "pid": os.getpid(),
        "started_at": datetime.fromtimestamp(STARTED_AT, timezone.utc).isoformat(
            timespec="seconds"
        ),
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "mode": "exe" if is_frozen() else "source",
        "data_dir": str(writable_root()),
        "port": DEFAULT_PORT,
    }


@app.get("/api/selfcheck")
def selfcheck() -> dict:
    from .selfcheck import run

    return run()


# ------------------------------------------------------------------ 配置


@app.get("/api/config")
def get_config() -> dict:
    return config.public_view()


@app.post("/api/config")
def post_config(payload: ConfigIn) -> dict:
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    merged = config.save(patch)
    return {"ok": True, "config": config.public_view(merged)}


@app.post("/api/config/preset")
def post_preset(payload: PresetIn) -> dict:
    if payload.preset not in llm.PRESETS:
        raise HTTPException(400, f"没有这个厂商：{payload.preset}")
    merged = config.save(llm.apply_preset(config.load(), payload.preset))
    return {"ok": True, "config": config.public_view(merged)}


@app.get("/api/catalog")
def catalog() -> dict:
    return {
        "llm_presets": llm.PRESETS,
        "tts_voices": tts.voice_catalog(),
        "channels": [
            {"id": "read", "label": "阅读", "from": "英文句子", "to": "场景",
             "what": "看英文句子，在脑子里出现场景。不说话、不翻译。"},
            {"id": "listen", "label": "听力", "from": "声音", "to": "场景",
             "what": "只听声音，直接进入场景。中间不经过图片和中文。"},
            {"id": "speak", "label": "口语", "from": "场景", "to": "说出口",
             "what": "看场景，直接把英文说出来，说完再看原文。"},
            {"id": "type", "label": "打字", "from": "场景", "to": "打出来",
             "what": "看场景，把英文打出来，自动判分。"},
            {"id": "build", "label": "造句", "from": "原句", "to": "换词新句",
             "what": "换掉主语/谓语/宾语，造出一个新句子。"},
        ],
    }


@app.post("/api/config/test-llm")
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


@app.post("/api/config/test-tts")
def test_tts() -> dict:
    cfg = config.load()["tts"]
    try:
        return tts.check(
            engine=cfg.get("engine", "edge"),
            voice=cfg.get("voice", "en-US-AriaNeural"),
            openai_cfg=cfg.get("openai", {}),
        )
    except tts.TTSError as exc:
        return JSONResponse(
            status_code=400, content={"error": str(exc), "hint": exc.hint}
        )


# ------------------------------------------------------------------ 训练包


@app.get("/api/lessons")
def list_lessons() -> dict:
    return {"lessons": db.list_lessons(), "stats": db.stats()}


@app.post("/api/lessons")
def create_lesson(payload: LessonIn) -> dict:
    from . import prompts

    target = payload.target.strip()
    if not target:
        raise HTTPException(400, "输入是空的。")

    kind = payload.kind or prompts.classify_target(target)
    if kind not in ("word", "phrase", "sentence"):
        kind = "word"

    existing = db.one(
        "SELECT id, status FROM lessons WHERE lower(target) = lower(?) ORDER BY id DESC",
        (target,),
    )
    if existing and existing["status"] == "generating":
        # 同一个目标正在生成，直接复用，不要开第二份。
        return {"lesson_id": existing["id"], "reused": True}

    lesson_id = db.create_lesson(target, kind)
    _POOL.submit(_run_generation, lesson_id)
    return {"lesson_id": lesson_id, "reused": False}


def _run_generation(lesson_id: int) -> None:
    """跑在线程池里。异常已经在 pipeline 内部落库，这里只防止它冒到线程外。"""
    try:
        pipeline.generate(lesson_id)
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/lessons/{lesson_id}")
def get_lesson(lesson_id: int) -> dict:
    lesson = db.lesson_detail(lesson_id)
    if lesson is None:
        raise HTTPException(404, "没有这个训练包。")
    lesson["job"] = pipeline.job_status(lesson_id)
    lesson["srs_preview"] = srs.preview(0.0, 2.5, 0, 0)
    return lesson


@app.get("/api/lessons/{lesson_id}/job")
def get_job(lesson_id: int) -> dict:
    lesson = db.get_lesson(lesson_id)
    if lesson is None:
        raise HTTPException(404, "没有这个训练包。")
    return {
        "status": lesson["status"],
        "error": lesson["error"],
        "job": pipeline.job_status(lesson_id),
    }


@app.delete("/api/lessons/{lesson_id}")
def delete_lesson(lesson_id: int) -> dict:
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    db.delete_lesson(lesson_id)
    return {"ok": True}


@app.post("/api/lessons/{lesson_id}/regenerate")
def regenerate_lesson(lesson_id: int) -> dict:
    """重新生成整个训练包（例句 + 语音）。

    给「生成失败」和「对素材不满意」两种情况用。会**先清空旧素材**，
    所以复习进度也会一起没掉 —— 这一点在界面上要问清楚，不能默默做。
    """
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    db.execute(
        "UPDATE lessons SET status = 'generating', error = '', updated_at = ? WHERE id = ?",
        (db.now(), lesson_id),
    )
    _POOL.submit(_run_generation, lesson_id)
    return {"ok": True, "lesson_id": lesson_id}


@app.post("/api/lessons/{lesson_id}/audio")
def redo_audio(lesson_id: int) -> dict:
    if db.get_lesson(lesson_id) is None:
        raise HTTPException(404, "没有这个训练包。")
    return _sync_audio(lesson_id)


def _sync_audio(lesson_id: int) -> dict:
    try:
        return pipeline.regenerate_audio(lesson_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"补语音时出错：{exc}") from exc


# ------------------------------------------------------------------ 音频
# 只允许取「内容寻址」形态的文件名（`<engine>-<20位十六进制>.mp3`）。
# 不做这个校验的话，`/api/audio/..%2F..%2Fconfig.json` 就能读到配置文件里的密钥。
_AUDIO_NAME = re.compile(r"^[a-z]+-[0-9a-f]{20}\.mp3$")


@app.get("/api/audio/{name}")
def get_audio(name: str) -> FileResponse:
    if not _AUDIO_NAME.match(name):
        raise HTTPException(400, "文件名不合法。")
    path = audio_dir() / name
    if not path.exists():
        # 界面会画出一个播放按钮却点了没反应 —— 明确报 404，
        # 让前端能把它标成「这条缺语音」而不是静默失败。
        raise HTTPException(404, "这条语音还没生成。")
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "max-age=31536000"})


# ------------------------------------------------------------------ 复习


@app.get("/api/queue")
def queue(limit: int = 40) -> dict:
    cfg = config.load()
    limit = max(1, min(200, limit))
    cards = db.due_cards(limit=limit, new_per_day=int(cfg["study"].get("new_per_day", 20)))
    return {"cards": [_decorate(c) for c in cards], "stats": db.stats()}


def _decorate(card: dict) -> dict:
    """给卡片行补上界面要用的字段（目标词、有没有语音）。"""
    return {
        "id": card["id"],
        "lesson_id": card["lesson_id"],
        "example_id": card["example_id"],
        "channel": card["channel"],
        "target": card.get("target", ""),
        "introduced": card["introduced"],
    }


@app.get("/api/cards/{card_id}")
def get_card(card_id: int) -> dict:
    payload = db.card_payload(card_id)
    if payload is None:
        raise HTTPException(404, "没有这张卡。")
    card = payload["card"]
    payload["srs_preview"] = srs.preview(
        card["interval"], card["ease"], card["reps"], card["lapses"]
    )
    return payload


@app.post("/api/cards/{card_id}/grade")
def grade_card(card_id: int, payload: GradeIn) -> dict:
    try:
        return db.grade_card(card_id, payload.grade, payload.seconds)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/stats")
def get_stats() -> dict:
    return db.stats()


# ------------------------------------------------------------------ 备份


@app.post("/api/backup")
def backup(payload: BackupIn) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = (payload.name or f"etude-backup-{stamp}").strip()
    if not re.match(r"^[\w.\-]+$", name):
        raise HTTPException(400, "备份名只能用字母、数字、点、下划线和横线。")
    if not name.endswith(".sqlite3"):
        name += ".sqlite3"
    dest = Path(writable_root()) / "backups" / name
    try:
        db.backup_to(dest)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"备份失败：{exc}") from exc
    return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size}


# ------------------------------------------------------------------ 界面

# 前端自带一个版本号常量，和 /api/health 比对。不一致时页面顶部弹红条。
# 所以 HTML 绝不能被缓存 —— 否则用户拿到的还是旧前端，检测也就失效了。
_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


@app.get("/")
def index() -> FileResponse:
    path = frontend_dir() / "index.html"
    if not path.exists():
        return JSONResponse(
            status_code=500,
            content={
                "error": "找不到界面文件。",
                "hint": f"期望它在 {path}。这是打包时漏了资源，"
                "重试修不好，请重新安装完整发行包。",
            },
        )
    return FileResponse(path, media_type="text/html; charset=utf-8", headers=_NO_STORE)


@app.exception_handler(500)
async def on_500(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": f"程序内部出错：{exc}",
            "hint": "这多半是程序的问题。把这段连同数据目录里的 logs 一起反馈给作者。",
            "path": str(request.url.path),
        },
    )
