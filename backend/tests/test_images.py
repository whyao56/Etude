"""场景配图。

「场景」是这套方法里最容易被做坏的一环。做坏有两种方式：

1. **场景退化成翻译。** 卡片正面写着「咖啡店点单」然后要你说英文，
   练的就是中译英。所以配图要画的是**情境**，不是句子。
2. **悄悄存下一张假图。** 接口返回 200、内容却是一页 HTML 错误页 ——
   照后缀存成 ``xxx.jpg`` 之后，界面上是个永远加载不出来的破图框，
   而「配图成功」的计数还照加。

第 2 条是真踩到的，所以下面有好几条专门盯着它。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

# 一张真的 1×1 PNG。不引第三方库现造 ——
# 「响应体真的是图片」这件事本来就要靠文件头判断，样本得是真的。
PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\xd9\x0e"
    b"\x1b\x0e"
    b"\x00\x00\x00\x0cIDATx\x9cc\xfc\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
HTML = b"<!DOCTYPE html><html><body>403 Forbidden</body></html>"


@pytest.fixture
def fake_net(monkeypatch):
    """把 HTTP 传输层换成假的。

    **刻意不动 ``_download`` / ``_search_openverse`` 的代码。**
    直接 monkeypatch ``_download`` 会让「下载回来的到底是不是图片」这一段
    被整个绕过 —— 而那正是最容易出错的地方。只换传输层，判断逻辑照跑。
    """
    import httpx

    import app.providers.images as images_mod

    routes: dict[str, object] = {}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        for prefix, responder in routes.items():
            if url.startswith(prefix):
                return responder(url) if callable(responder) else responder
        return httpx.Response(404, json={"error": "no route for " + url})

    def fake_client(timeout, base_url=""):
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(timeout),
        )

    monkeypatch.setattr(images_mod, "_client", fake_client)
    return SimpleNamespace(routes=routes, calls=calls, mod=images_mod)


def _openverse(payload: dict) -> object:
    return lambda url: __import__("httpx").Response(200, json=payload)


def _candidate(url: str, width: int, height: int) -> dict:
    return {"url": url, "width": width, "height": height, "title": "", "creator": "",
            "license": "", "page": ""}


# --------------------------------------------------------------- 场景 → 图


def test_query_is_a_few_keywords_not_a_sentence():
    """检索词必须是**几个关键词**。

    照着一整句话去搜，命中率极差 —— 这是「按场景配图」看起来永远配不上
    的第一大原因。
    """
    from app.providers.images import build_query

    query = build_query("A barista hands you a cup across the counter at a busy cafe.")
    words = query.split()
    assert len(words) <= 6
    assert "the" not in words and "a" not in words and "at" not in words
    assert "barista" in words and "counter" in words


def test_query_falls_back_to_the_sentence_when_scene_is_missing():
    from app.providers.images import build_query

    assert build_query("", "I would rather stay home tonight.") != ""
    assert build_query("", "") == ""


def test_prompt_forbids_letters_in_the_picture():
    """图里不能出现文字。

    文生图模型往图里写字基本都是乱码，而乱码英文出现在一个**英语学习工具**
    里格外刺眼 —— 用户会以为自己看到的是待学内容。
    """
    from app.providers.images import build_prompt

    prompt = build_prompt("A barista hands you a cup.")
    assert "A barista hands you a cup." in prompt
    assert "no text" in prompt.lower() or "Do not include any text" in prompt


def test_prompt_is_empty_without_a_scene():
    """没有场景就不画 —— 拿例句去画会得到「一句话的插画」，不是场合。"""
    from app.providers.images import build_prompt

    assert build_prompt("") == ""
    assert build_prompt("   ") == ""


# ------------------------------------------------------------ 命名与安全


def test_names_are_content_addressed(isolated_data):
    from app.providers.images import cache_name, valid_name

    a = cache_name("search", "coffee shop", ".jpg")
    b = cache_name("search", "coffee shop", ".jpg")
    c = cache_name("search", "coffee shop", ".png")
    d = cache_name("search", "coffee shop!", ".jpg")
    assert a == b, "同样的输入必须得到同一个名字 —— 否则同一张图会存很多份"
    assert a != c and a != d
    assert valid_name(a)
    assert not valid_name("随便什么.jpg")


@pytest.mark.parametrize(
    "name",
    [
        "../config.json",
        "..%2Fconfig.json",
        "edge-00000000000000000000.mp3",
        "search-00000000000000000000.exe",
        "search-0000000000000000000.jpg",     # 少一位
        "search-000000000000000000000.jpg",   # 多一位
        "",
    ],
)
def test_image_names_that_are_not_content_addressed_are_refused(name):
    """文件名也是用户输入变路径 —— 和音频那条一样，必须挡。"""
    from app.providers.images import valid_name

    assert not valid_name(name)


# ------------------------------------------------------------ 挑哪一张


def test_landscape_beats_a_taller_picture():
    """竖图放进卡片里要么被裁掉一半，要么把版面撑得很高。"""
    from app.providers.images import pick_candidate

    picked = pick_candidate([
        _candidate("portrait", 900, 1600),
        _candidate("landscape", 1200, 800),
    ])
    assert picked["url"] == "landscape"


def test_tiny_pictures_are_skipped_when_something_bigger_exists():
    """搜回来的缩略图有时只有 100px 宽，放大后糊成一片，还不如不放。"""
    from app.providers.images import pick_candidate

    picked = pick_candidate([
        _candidate("tiny", 100, 80),
        _candidate("ok", 800, 600),
    ])
    assert picked["url"] == "ok"


def test_a_tiny_picture_is_still_better_than_nothing():
    from app.providers.images import pick_candidate

    picked = pick_candidate([_candidate("tiny", 100, 80)])
    assert picked["url"] == "tiny"


def test_no_candidates_picks_nothing():
    from app.providers.images import pick_candidate

    assert pick_candidate([]) is None


# ------------------------------------------------- 下载回来的到底是不是图


def test_a_picture_is_downloaded_and_named_by_its_real_format(fake_net, isolated_data):
    """Content-Type 说是 JPEG、内容其实是 PNG —— 存的名字必须按**内容**来。

    按响应头存的话，内容寻址的命名就不可靠了：同一个 key 换个后缀会存两份。
    """
    import httpx

    images_mod = fake_net.mod
    fake_net.routes["https://api.openverse.org/"] = lambda url: httpx.Response(
        200, json={"results": [{"url": "https://cdn.example/x", "width": 1200,
                                "height": 800}]},
    )
    fake_net.routes["https://cdn.example/"] = lambda url: httpx.Response(
        200, content=PNG, headers={"content-type": "image/jpeg"},
    )

    raw, ext = images_mod._download("https://cdn.example/x", 10.0)
    assert raw == PNG
    assert ext == ".png", "应当按文件头判断，而不是照抄响应头"


def test_an_html_error_page_is_refused_not_saved(fake_net, isolated_data):
    """**这是真踩到的那条。**

    图源挡了程序访问，返回 200 + 一页 HTML。照着后缀存成 ``xxx.jpg`` 之后，
    界面上是个永远加载不出来的破图框，而「配图成功」的计数还照加一。
    宁可报错：报错会进 failures 列表，详情页写着「缺场景图」，用户能换来源重来。
    """
    import httpx

    images_mod = fake_net.mod
    fake_net.routes["https://api.openverse.org/"] = lambda url: httpx.Response(
        200, json={"results": [{"url": "https://cdn.example/blocked", "width": 1200,
                                "height": 800}]},
    )
    fake_net.routes["https://cdn.example/"] = lambda url: httpx.Response(
        200, content=HTML, headers={"content-type": "image/jpeg"},
    )

    with pytest.raises(images_mod.ImageError) as info:
        images_mod.search_image("coffee shop", timeout=5.0)

    assert info.value.hint, "失败必须带上「下一步怎么办」"
    assert "网页" in str(info.value) or "格式" in str(info.value)

    from app.paths import images_dir

    assert list(images_dir().glob("*")) == [], "不能把错误页留在磁盘上"


def test_the_second_library_is_tried_when_the_first_fails(fake_net, isolated_data):
    """Openverse 偶尔会限流，而「限流」不该等于「这个包没有图」。"""
    import httpx

    images_mod = fake_net.mod
    fake_net.routes["https://api.openverse.org/"] = lambda url: httpx.Response(
        429, json={"error": "rate limited"}
    )
    fake_net.routes["https://commons.wikimedia.org/"] = lambda url: httpx.Response(
        200,
        json={"query": {"pages": {"1": {
            "title": "File:coffee.jpg",
            "imageinfo": [{"thumburl": "https://upload.example/coffee.jpg",
                           "thumbwidth": 1280, "thumbheight": 853}],
        }}}},
    )
    fake_net.routes["https://upload.example/"] = lambda url: httpx.Response(
        200, content=PNG
    )

    raw, ext, source = images_mod.search_image("coffee shop", timeout=5.0)
    assert raw[:4] == b"\x89PNG"
    assert source["license"] == "Commons"
    assert any("openverse" in c for c in fake_net.calls)


def test_both_libraries_failing_says_what_to_do_instead(fake_net, isolated_data):
    import httpx

    images_mod = fake_net.mod
    fake_net.routes["https://api.openverse.org/"] = lambda url: httpx.Response(
        500, text="boom"
    )
    fake_net.routes["https://commons.wikimedia.org/"] = lambda url: httpx.Response(
        500, text="boom"
    )

    with pytest.raises(images_mod.ImageError) as info:
        images_mod.search_image("a very abstract feeling", timeout=5.0)
    assert info.value.kind == "not_found"
    assert "文生图" in info.value.hint or "关掉图片" in info.value.hint


# ------------------------------------------------------------------ 落盘


def test_provide_writes_the_file_and_reuses_it(fake_net, isolated_data):
    """第二次同样的场景不能再下一次 —— 内容寻址就是为了这个。"""
    import httpx

    from app.paths import images_dir

    images_mod = fake_net.mod
    fake_net.routes["https://api.openverse.org/"] = lambda url: httpx.Response(
        200, json={"results": [{"url": "https://cdn.example/x", "width": 1200,
                                "height": 800}]},
    )
    fake_net.routes["https://cdn.example/"] = lambda url: httpx.Response(200, content=PNG)

    name = images_mod.provide(query="coffee shop", prompt="", source="search", timeout=5.0)
    assert (images_dir() / name).read_bytes() == PNG

    before = len(fake_net.calls)
    again = images_mod.provide(query="coffee shop", prompt="", source="search", timeout=5.0)
    assert again == name
    assert len(fake_net.calls) == before, "第二次不该再发请求"


def test_off_source_returns_empty_and_never_touches_the_network(fake_net, isolated_data):
    images_mod = fake_net.mod
    assert images_mod.provide(query="q", prompt="p", source="off") == ""
    assert fake_net.calls == []


def test_an_oversized_picture_is_refused(fake_net, isolated_data):
    """检索回来的图动辄几 MB，全存下来数据目录会爆。"""
    import httpx

    images_mod = fake_net.mod
    fake_net.routes["https://cdn.example/"] = lambda url: httpx.Response(
        200, content=b"\xff\xd8\xff" + b"\x00" * (5 * 1024 * 1024)
    )
    with pytest.raises(images_mod.ImageError) as info:
        images_mod._download("https://cdn.example/huge", timeout=5.0)
    assert "太大" in str(info.value)


def test_catalog_lists_every_source(isolated_data):
    """界面的下拉框直接读这里 —— 少一个，用户就少一条出路。

    （这个用例原来叫 ``..._the_three_sources``。0.0.3 加了 ``both``
    之后名字就不对了 —— 名字和断言对不上比没名字更糟，
    下次读到它的人会以为这里只该有三个。）
    """
    from app.providers.images import IMAGE_SOURCES, source_catalog

    ids = {s["id"] for s in IMAGE_SOURCES}
    # both 是 0.0.3 加的：检索和画图并行，互为兜底。它必须排在第一个 ——
    # 界面上「第一个就是默认值」，而它确实该是默认值。
    assert ids == {"both", "search", "llm", "off"}
    assert IMAGE_SOURCES[0]["id"] == "both"
    assert set(source_catalog()) == {"sources", "presets"}
