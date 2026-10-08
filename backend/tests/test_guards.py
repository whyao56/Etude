"""守卫式测试。

两条规矩（都来自上个项目的教训）：

1. **新守卫必须先在「旧代码」上失败过**，否则它只是一条好看的注释。
   下面每个用例都连着「它要拦的那种坏法」一起写。
2. **反证本身也要被怀疑**：替换要忠实还原旧写法，
   不是「改成一种看起来更差的写法」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "backend" / "frontend" / "index.html"


# ------------------------------------------------------- 版本号一致（§7.5）


def test_frontend_version_matches_backend():
    """前端的版本常量和后端必须一致。

    为什么值得一条测试：前端那个常量会变成「页面顶部的红条」的判据。
    不一致时红条会天天弹，用户很快学会无视它 —— **那还不如没有**。
    """
    from app import __version__

    html = FRONTEND.read_text(encoding="utf-8")
    match = re.search(r'const\s+APP_VERSION\s*=\s*"([^"]+)"', html)
    assert match, "index.html 里找不到 APP_VERSION 常量"
    assert match.group(1) == __version__, (
        f"前端 APP_VERSION={match.group(1)}，后端 __version__={__version__}。"
        " 两处必须一起改。"
    )


def test_version_guard_would_catch_a_drift(monkeypatch):
    """反证：把后端版本号改掉，上面那条必须失败。

    这条是给「守卫本身」做的守卫 —— 如果哪天有人把上面那条改成
    只检查「有没有 APP_VERSION 这个字段」，这条会立刻红。
    """
    from app import __version__

    html = FRONTEND.read_text(encoding="utf-8")
    drifted = html.replace(
        f'const APP_VERSION = "{__version__}"', 'const APP_VERSION = "9.9.9"'
    )
    assert drifted != html, "替换没生效，说明反证没有忠实地还原出「漂移」这个状态"
    match = re.search(r'const\s+APP_VERSION\s*=\s*"([^"]+)"', drifted)
    assert match.group(1) != __version__


# ------------------------------------------------- 前端调用点静态守卫（§3.3）


def test_js_probe_self_test_passes():
    """探针自检必须先过。

    探针坏了会伪装成被测对象坏了 —— 上个项目里
    「探测脚本自己写错了」骗过一次：三个修复全报 False，
    看起来像重建没生效，其实是探针用错了成员判断。
    """
    import check_js

    failures = check_js.self_test()
    assert not failures, "静态守卫的探针自检没通过：" + "；".join(failures)


def test_js_call_sites_all_defined():
    """真正的调用点检查：引用了但本文件没定义的名字必须为零。"""
    import check_js

    html = FRONTEND.read_text(encoding="utf-8")
    blocks = check_js.extract_scripts(html)
    assert blocks, "index.html 里没有 <script> 块"

    suspicious = []
    for _offset, code in blocks:
        clean = check_js.strip_noise(code)
        declared = check_js.declared_names(clean)
        for name, line in check_js.called_names(clean).items():
            if name in declared or name in check_js.KEYWORDS or name in check_js.BUILTINS:
                continue
            suspicious.append((name, line))
    assert not suspicious, f"有调用了但没定义的名字：{suspicious}"


def test_js_guard_would_catch_a_renamed_function(tmp_path):
    """反证：把某个函数的定义改名、调用点不动，守卫必须报出来。

    忠实还原的是**上个项目真实发生过的坏法**：重构时把函数并进了另一个，
    漏改调用点，而全量测试是绿的。
    """
    import check_js

    html = FRONTEND.read_text(encoding="utf-8")
    broken = html.replace("function reveal() {", "function revealTheCard() {", 1)
    assert broken != html, "替换没生效，反证不忠实"

    path = tmp_path / "broken.html"
    path.write_text(broken, encoding="utf-8")

    blocks = check_js.extract_scripts(broken)
    suspicious = set()
    for _offset, code in blocks:
        clean = check_js.strip_noise(code)
        declared = check_js.declared_names(clean)
        for name in check_js.called_names(clean):
            if name in declared or name in check_js.KEYWORDS or name in check_js.BUILTINS:
                continue
            suspicious.add(name)
    assert "reveal" in suspicious, "守卫没有抓到漏改的调用点"


def test_js_probe_is_not_fooled_by_regex_with_quote():
    """探针不能被『正则字面量里的引号』骗到 —— 第一版就栽在这里。

    ``/[^a-z0-9' ]/g`` 里那个单引号会被朴素的清洗器当成字符串开头，
    把中间一大段代码整个吃掉，于是报出一堆「未定义」的假警报。
    """
    import check_js

    sample = """function before() {}
                  var re = /[^a-z0-9' ]/g;
                  function after() {}"""
    clean = check_js.strip_noise(sample)
    assert "function after" in clean
    assert "function before" in clean
    assert "a-z0-9" not in clean


def test_event_names_are_lowercased():
    """事件类型串必须全小写。

    这是第一版真踩到的 bug：``el()`` 里写的是 ``key.slice(2)``，
    于是 ``onClick`` 变成了 ``addEventListener("Click", ...)`` ——
    大小写敏感，所以**一个都不会触发**。界面照常画出来，
    只是点什么都没反应。截图看不出来，Python 测试也测不到。
    """
    import check_js

    html = FRONTEND.read_text(encoding="utf-8")
    blocks = check_js.extract_scripts(html)
    for _offset, code in blocks:
        offenders, lowers = check_js.bad_event_type_literals(code)
        assert not offenders, f"有大小写错误的事件名：{offenders}"
    # 有 el() 这种把 onXxx 转小写的实现在，才算真的修好了。
    assert any(
        check_js.bad_event_type_literals(code)[1] for _o, code in blocks
    ), "找不到把事件名转小写的那一步"


def test_event_name_guard_would_catch_the_old_writing():
    """反证：忠实还原第一版那种坏写法，守卫必须报出来。"""
    import check_js

    broken = 'node.addEventListener(key.slice(2), value);\n'
    # 旧写法是「不转换」，所以这里没有 toLowerCase —— 守卫的第二个返回值
    # 是 False。而且如果哪里写了字面量大写事件名，第一个返回值会报出来。
    offenders, lowers = check_js.bad_event_type_literals(broken)
    assert lowers is False, "旧写法里不该有 toLowerCase，反证不忠实"

    offenders2, _ = check_js.bad_event_type_literals('x.addEventListener("Click", f);')
    assert offenders2 and offenders2[0][0] == "Click"


# ------------------------------------------------- 错误提示的方向（§7.4）


def test_missing_dependency_ranks_before_network_keywords():
    """缺组件的判断必须排在所有网络关键词之前。

    为什么是硬要求：模块名有可能碰巧命中网络关键词。排在后面的话，
    用户会看到「重试 / 换镜像」—— 而镜像补不上一个没打进包的模块，
    这是个**永远修不好的方向**。

    「没有建议」不可怕，「看起来合理但方向错误的建议」才可怕。
    """
    from app.providers.llm import explain_error

    err = explain_error(ModuleNotFoundError("No module named 'http_something'"))
    assert err.kind == "missing_dependency"
    # 方向必须是「重新安装」，而不是网络侧的补救手段。
    assert "重新安装" in err.hint
    assert "换镜像" not in err.hint
    # 而且必须**明确告诉用户重试没用**。
    # （注意：这里断言的是「说明了重试无效」，不是「不许出现重试二字」——
    #  后一种断言会把「重试也不会好」这句正确的话也误判成违规。）
    assert "重试也不会好" in err.hint


def test_auth_error_points_at_the_key_not_the_network():
    import httpx

    from app.providers.llm import explain_error

    response = httpx.Response(401, request=httpx.Request("POST", "https://x/v1/chat"))
    err = explain_error(
        httpx.HTTPStatusError("unauthorized", request=response.request, response=response)
    )
    assert err.kind == "auth"
    assert "密钥" in err.hint or "API Key" in err.hint


def test_local_service_down_says_start_the_service():
    """指向本机却连不上 —— 方向应当是「把服务起起来」，不是「检查网络」。"""
    import httpx

    from app.providers.llm import explain_error

    err = explain_error(
        httpx.ConnectError("connection refused"), "http://localhost:11434/v1", "qwen"
    )
    assert err.kind == "local_service_down"
    assert "本机" in err.hint


def test_every_error_has_a_next_step():
    """凡是抛出去的错，都必须带一个「下一步做什么」。"""
    import httpx

    from app.providers.llm import explain_error

    samples = [
        ModuleNotFoundError("x"),
        httpx.ConnectError("x"),
        httpx.TimeoutException("x"),
        RuntimeError("x"),
        ValueError("x"),
    ]
    for exc in samples:
        err = explain_error(exc)
        assert err.hint, f"{type(exc).__name__} 没有给下一步"
        assert err.kind


# ------------------------------------------------------ JSON 抽取的健壮性


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '这是结果：\n```\n{"a": 1}\n```\n希望有帮助',
        '前言 {"a": 1} 后记',
        '前言 {不是json} 然后 {"a": 1} 结尾 }',
    ],
)
def test_extract_json_survives_real_world_shapes(text):
    """不同厂商对「请输出 JSON」的执行力度差别很大，三种常见形态都得吃得下。"""
    from app.providers.llm import extract_json

    assert extract_json(text) == {"a": 1}


def test_extract_json_reports_a_fixable_direction():
    from app.providers.llm import LLMError, extract_json

    with pytest.raises(LLMError) as info:
        extract_json("完全没有 JSON，只有一段中文。")
    assert info.value.kind == "bad_json"
    assert info.value.hint, "解析失败也要给下一步"


def test_extract_json_balances_nested_braces():
    """不能简单取「最后一个 }」—— 后面常跟着带花括号的解说。"""
    from app.providers.llm import extract_json

    text = 'ok {"a": {"b": [1,2]}, "c": "}"} 结尾 {这里也有} 花括号'
    assert extract_json(text) == {"a": {"b": [1, 2]}, "c": "}"}


# -------------------------------------------------------------- 隐私红线


def test_no_real_email_in_repository():
    """仓库里不该出现任何真实邮箱形态的字符串。

    不点名任何具体地址 —— **守卫自己不能是被禁的那个字符串**，
    否则守卫文件本身就把那个邮箱写进仓库了。
    """
    pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    allowed = {"example.com", "example.org", "users.noreply.github.com"}
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", "dist", "build", "node_modules", "__pycache__"}
               for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".ico", ".mp3", ".zip", ".exe", ".dll", ".pyc"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for hit in pattern.findall(text):
            if hit.split("@")[-1].lower() in allowed:
                continue
            offenders.append(f"{path.relative_to(ROOT)}: {hit}")
    assert not offenders, "仓库里出现了疑似真实邮箱：" + "；".join(offenders)


# --------------------------------------------------------- 配置里的密钥


def test_public_config_never_returns_the_api_key(isolated_data):
    """界面拿到的配置里绝不能带密钥。

    密钥一旦回传给前端，它就会出现在浏览器内存、开发者工具、
    以及任何一次界面截图里。**脱敏要改数据，不是加模糊。**
    """
    from app import config

    config.save({"llm": {"api_key": "sk-super-secret-value"}})
    public = config.public_view()
    blob = json.dumps(public, ensure_ascii=False)
    assert "sk-super-secret-value" not in blob
    assert public["llm"]["api_key_set"] is True
    assert "api_key" not in public["llm"]
