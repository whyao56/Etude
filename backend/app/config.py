"""配置读写。

配置存在 ``%LOCALAPPDATA%\\Etude\\config.json`` —— **不在仓库里、不在程序目录下**。
API Key 只落在这个文件里，永远不会进 git（它在仓库外面，从根上杜绝了误提交）。

读取顺序（后者覆盖前者）：
    内置默认值 → config.json → 环境变量

环境变量这一层不是摆设：测试和 CI 靠它注入桩服务的地址，
不需要在磁盘上写任何凭据。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any

from .paths import config_path

_LOCK = threading.RLock()

DEFAULTS: dict[str, Any] = {
    "llm": {
        # preset 只决定界面上的「快捷选择」，真正生效的永远是下面这几个字段。
        # 这样换厂商不需要改代码，只要改配置。
        "preset": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.9,
        "max_tokens": 4096,
        "timeout": 120.0,
    },
    "tts": {
        # edge = 微软神经网络语音，免费、不需要 key；openai = 任意 OpenAI 兼容 TTS。
        "engine": "edge",
        "voice": "en-US-AriaNeural",
        "rate": "+0%",
        "volume": "+0%",
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "tts-1",
            "voice": "alloy",
        },
    },
    "images": {
        # 场景配图。off = 不用图片，search = 开放图库检索（免费、无需密钥），
        # llm = 文生图模型画（要配置），both = 两条路并行（推荐）。
        #
        # 默认给 both 而不是 search：单靠检索，抽象情境搜不到、图库限流就整个包没图；
        # 单靠画图，慢而且质量不稳。并行之后互为兜底，延迟还是两条里快的那条。
        "source": "both",
        # 一个训练包最多配几张图。0 = 每条例句都配。
        "max_per_lesson": 8,
        "llm": {
            "preset": "zhipu",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "",
            "model": "cogview-3-flash",
        },
    },
    "study": {
        # 一次生成几个例句。理论要求「多例子重复」——不同例子的重复，
        # 而不是同一句重复，所以这个数不宜太小。
        "examples_per_lesson": 8,
        # 每条例句存几种不同的中文说法。界面每次复习随机取一种，
        # 让大脑无法形成「英文↔某个固定中文」的偷懒关联。
        "zh_variants_per_example": 3,
        "zh_variant_policy": "rotate",  # rotate | fixed
        "new_per_day": 5,
        "scale": 100,  # 0-100，由它推出各通道的权重
        # 新建学习计划时默认储备多少个（界面上还能改）。
        "plan_default_count": 20,
    },
    "channels": {
        "read": True,
        "listen": True,
        "speak": True,
        "type": True,
        "build": True,
    },
    "pronounce": {
        # 跟读录音（0.1.0）。enabled=False 时界面上不出现录音入口 ——
        # 远程桌面、虚拟机、没有麦克风的台式机上，点了必然失败；
        # 与其让人对着一个永远报错的按钮，不如一开始就不给这个入口。
        "enabled": True,
        # auto = 配了转写服务就用词级对比，没配就只用声学指标（时长/停顿/音量）；
        # acoustic = 永远只用声学指标；asr = 强制走转写（没配就在界面上说清楚）。
        #
        # 为什么要有 acoustic 这一档：转写服务是**可选的**，
        # 而这个功能的承诺是「装上就能练」。
        "mode": "auto",
        # 录音留不留。默认留着 —— 「上礼拜和今天的我」才是跟读真正想给的东西，
        # 但要在设置里能关（有人不想让机器里堆自己的声音）。
        "save_recordings": True,
        "asr": {
            # 任意 OpenAI 兼容的 /audio/transcriptions（whisper-1 这类）。
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "whisper-1",
            "language": "en",
        },
    },
    "server": {
        "port": 8977,
        "open_window": True,
    },
}

# 环境变量覆盖表： 环境变量名 -> (顶层键, 二级键)
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "ETUDE_LLM_BASE_URL": ("llm", "base_url"),
    "ETUDE_LLM_API_KEY": ("llm", "api_key"),
    "ETUDE_LLM_MODEL": ("llm", "model"),
    "ETUDE_TTS_ENGINE": ("tts", "engine"),
    "ETUDE_TTS_VOICE": ("tts", "voice"),
    "ETUDE_IMAGE_SOURCE": ("images", "source"),
    "ETUDE_IMAGE_BASE_URL": ("images", "llm", "base_url"),
    "ETUDE_IMAGE_API_KEY": ("images", "llm", "api_key"),
    "ETUDE_IMAGE_MODEL": ("images", "llm", "model"),
    "ETUDE_ASR_BASE_URL": ("pronounce", "asr", "base_url"),
    "ETUDE_ASR_API_KEY": ("pronounce", "asr", "api_key"),
    "ETUDE_ASR_MODEL": ("pronounce", "asr", "model"),
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    """读配置。文件不存在或坏掉都返回默认值，不抛异常。"""
    data: dict = {}
    path = config_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, json.JSONDecodeError):
            # 配置坏掉不该让程序起不来 —— 用默认值继续，界面上会提示。
            # 静默吞掉才是错的：save() 时会把它重写成合法 JSON。
            data = {}

    merged = _deep_merge(DEFAULTS, data)

    for env_name, path in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if not value:
            continue
        node = merged
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value

    return merged


def save(patch: dict) -> dict:
    """把 patch 合并进现有配置并落盘，返回合并后的完整配置。"""
    with _LOCK:
        current = load()
        merged = _deep_merge(current, patch)
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换：中途崩溃不会留下半个 JSON，
        # 那会让下一次启动读到一个坏配置。
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
        return merged


def public_view(config: dict | None = None) -> dict:
    """给界面看的配置：把 key 换成「有没有填」的布尔值。

    绝不把 key 回传给前端 —— 那样它会出现在浏览器内存、开发者工具和任何一次
    界面截图里（上一个项目踩过：脱敏要改数据，不是加模糊）。
    """
    cfg = copy.deepcopy(config if config is not None else load())
    llm_key = cfg.get("llm", {}).pop("api_key", "") or ""
    tts_key = cfg.get("tts", {}).get("openai", {}).pop("api_key", "") or ""
    img_key = cfg.get("images", {}).get("llm", {}).pop("api_key", "") or ""
    cfg["llm"]["api_key_set"] = bool(llm_key.strip())
    cfg["tts"]["openai"]["api_key_set"] = bool(tts_key.strip())
    cfg["images"]["llm"]["api_key_set"] = bool(img_key.strip())
    asr_key = cfg.get("pronounce", {}).get("asr", {}).pop("api_key", "") or ""
    cfg["pronounce"]["asr"]["api_key_set"] = bool(asr_key.strip())
    return cfg
