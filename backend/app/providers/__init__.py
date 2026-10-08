"""外部能力接入层：大模型与语音合成。"""

from . import llm, tts  # noqa: F401

__all__ = ["llm", "tts"]
