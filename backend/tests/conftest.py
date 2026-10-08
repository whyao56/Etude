"""测试夹具。

第一条就很重要：**每个用例一个全新的数据目录**。
上一个项目里踩过「测试跑到真实数据」和「端口上留着旧进程」两件事，
所以这里把数据目录整个指到临时目录，并且每个用例之间把数据库连接丢掉
（``db`` 里的连接是模块级的，不清就会连到上一个用例的库上，
表现为**用例互相污染，单跑绿、一起跑红**）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

ROOT = BACKEND.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("ETUDE_DATA_DIR", str(tmp_path / "data"))

    from app import db

    db.close()
    yield tmp_path / "data"
    db.close()


@pytest.fixture
def sync_pool(monkeypatch):
    """把线程池换成同步执行。

    不这么做的话，「生成」会在线程里跑，测试就得靠 sleep 去等 ——
    那是「用时间换确定性」，在慢机器上会随机红。
    """

    class SyncPool:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

            class _Done:
                def result(self):
                    return None

            return _Done()

    import app.api as api

    pool = SyncPool()
    monkeypatch.setattr(api, "_POOL", pool)
    return pool


@pytest.fixture
def stub_llm(monkeypatch):
    """把大模型调用换成桩，返回符合约定的 JSON。

    桩返回的是**完整**结构。要暴露「字段缺失时程序怎么办」，
    请用 tests/test_pipeline.py 里那些专门构造残缺结构的用例 ——
    「什么都能做」的桩会把问题盖住。
    """
    import json

    import app.providers.llm as llm_mod

    def fake_chat(**kwargs):
        payload = {
            "kind": "word",
            "ipa": "əˈpriːʃieɪt",
            "gloss": "to understand how good something is, or to enjoy it",
            "note": "Everyday.",
            "examples": [
                {
                    "sentence": f"Sentence number {i} about it.",
                    "scene_en": f"Scene {i}.",
                    "scene_zh": f"场景 {i}。",
                    "zh_variants": [f"场景 {i}。", f"（短）{i}", f"（长）这是第 {i} 个场景。"],
                    "register": "日常口语",
                }
                for i in range(4)
            ],
            "swaps": [
                {
                    "role": "subject",
                    "original": "I",
                    "candidates": ["We", "They", "You"],
                    "samples": ["We do.", "They do."],
                }
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    return fake_chat


@pytest.fixture
def stub_tts(monkeypatch):
    """把语音合成换成桩，真的往 audio 目录写一个文件。

    为什么要真写文件：``readiness`` 会去磁盘上核对音频到底在不在。
    只改数据库不落盘的桩，会让「音频其实没生成」这类问题测不出来。
    """
    import app.providers.tts as tts_mod
    from app.paths import audio_dir

    def fake_synthesize(text, **kwargs):
        name = tts_mod.cache_name("edge", kwargs.get("voice", "v"), text)
        (audio_dir() / name).write_bytes(b"ID3" + b"\x00" * 64)
        return name

    monkeypatch.setattr(tts_mod, "synthesize", fake_synthesize)
    monkeypatch.setattr("app.pipeline.tts.synthesize", fake_synthesize)
    return fake_synthesize


@pytest.fixture
def stub_tts_failing(monkeypatch):
    """语音合成**全部失败**的桩。

    用来验「逐条失败不拖垮整体」：例句该有还是有，只是标着缺语音。
    """
    import app.providers.tts as tts_mod

    def boom(text, **kwargs):
        raise tts_mod.TTSError("桩：语音合成失败", "桩：换个音色试试。")

    monkeypatch.setattr("app.pipeline.tts.synthesize", boom)
    return boom


@pytest.fixture
def client(isolated_data, sync_pool, stub_llm, stub_tts):
    """跑得通全流程的测试客户端。"""
    from fastapi.testclient import TestClient

    import app.api as api

    with TestClient(api.app) as c:
        yield c
