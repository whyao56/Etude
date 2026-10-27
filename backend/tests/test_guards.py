"""守卫式测试。

两条规矩（都来自上个项目的教训）：

1. **新守卫必须先在「旧代码」上失败过**，否则它只是一条好看的注释。
   下面每个用例都连着「它要拦的那种坏法」一起写。
2. **反证本身也要被怀疑**：替换要忠实还原旧写法，
   不是「改成一种看起来更差的写法」。

0.1.0 的改动：前端从「一个 index.html」变成 17 个 ES 模块，
原来的 `check_js.py`（从 `<script>` 里抠代码）整份换成了
`check_frontend.py`（查导入、图标、类名、版本）。这里跟着换，
但「先让守卫在坏的输入上失败」这条纪律不变。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSION_JS = ROOT / "backend" / "frontend" / "js" / "version.js"


# ------------------------------------------------------- 版本号一致（§7.5）


def test_frontend_version_matches_backend():
    """前端的版本常量和后端必须一致。

    为什么值得一条测试：前端那个常量会变成「页面顶部的红条」的判据。
    不一致时红条会天天弹，用户很快学会无视它 —— **那还不如没有**。
    """
    from app import __version__

    text = VERSION_JS.read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    assert match, "js/version.js 里找不到 APP_VERSION 常量"
    assert match.group(1) == __version__, (
        f"前端 APP_VERSION={match.group(1)}，后端 __version__={__version__}。"
        " 两处必须一起改。"
    )


def test_version_guard_would_catch_a_drift():
    """反证：把版本号改成别的值，上面那条的判据必须失败。"""
    from app import __version__

    text = VERSION_JS.read_text(encoding="utf-8")
    drifted = re.sub(r'APP_VERSION\s*=\s*"[^"]+"', 'APP_VERSION = "9.9.9"', text)
    assert drifted != text, "替换没生效，说明反证没有忠实地还原出「漂移」这个状态"
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', drifted)
    assert match.group(1) != __version__


# ------------------------------------------------- 前端静态守卫（模块版）


def _guard():
    import sys

    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import check_frontend

    return check_frontend


def test_frontend_probe_self_test_passes():
    """探针先自证：探针坏了会伪装成「被测对象坏了」。"""
    guard = _guard()
    assert guard.self_test() == []


def test_frontend_guard_passes_on_the_real_tree():
    guard = _guard()
    problems = (
        guard.check_imports(guard.js_files())
        + guard.check_icons(guard.js_files())
        + guard.check_classes(guard.js_files())
        + guard.check_version()
    )
    assert problems == [], "前端守卫有问题：" + "；".join(problems)


def test_import_guard_catches_a_renamed_export(tmp_path):
    """把导出的名字改掉、导入处没跟着改 —— 浏览器只会报一句 undefined。"""
    guard = _guard()
    (tmp_path / "a.js").write_text("export function foo() {}\n", encoding="utf-8")
    (tmp_path / "b.js").write_text(
        'import { bar } from "./a.js";\nbar();\n', encoding="utf-8"
    )
    problems = guard.check_imports([tmp_path / "b.js"])
    assert any("bar" in line for line in problems), problems


def test_icon_guard_catches_a_typo(tmp_path):
    """图标名拼错不会报错，只画一个空方块。"""
    guard = _guard()
    (tmp_path / "x.js").write_text('icon("tarsh");\n', encoding="utf-8")
    problems = guard.check_icons([tmp_path / "x.js"])
    assert any("tarsh" in line for line in problems), problems


def test_icon_guard_accepts_a_real_icon(tmp_path):
    guard = _guard()
    (tmp_path / "x.js").write_text('icon("trash");\n', encoding="utf-8")
    assert guard.check_icons([tmp_path / "x.js"]) == []


def test_class_guard_catches_a_style_that_does_not_exist(tmp_path):
    """JS 里写了类名、CSS 里没有规则 —— 不报错，页面只是「看起来没做完」。"""
    guard = _guard()
    (tmp_path / "y.js").write_text('el("div", { class: "no-such-style" });\n', encoding="utf-8")
    problems = guard.check_classes([tmp_path / "y.js"])
    assert any("no-such-style" in line for line in problems), problems


def test_class_guard_accepts_a_class_that_exists(tmp_path):
    guard = _guard()
    (tmp_path / "y.js").write_text('el("div", { class: "card" });\n', encoding="utf-8")
    assert guard.check_classes([tmp_path / "y.js"]) == []


def test_class_guard_ignores_template_expressions(tmp_path):
    """拼出来的类名（模板串）没法静态判断，不该误报。"""
    guard = _guard()
    (tmp_path / "y.js").write_text(
        'el("button", { class: `grade g${level}` });\n', encoding="utf-8"
    )
    assert guard.check_classes([tmp_path / "y.js"]) == []


def test_strip_noise_keeps_line_numbers():
    """行号必须准：一个报告「views.css:1173」的守卫，行号偏三十行就没人信它。"""
    guard = _guard()
    sample = "/* 注释\n还是注释 */\n.a { color: red }\n"
    cleaned = guard.strip_noise(sample)
    assert len(cleaned) == len(sample)
    assert cleaned.count("\n") == sample.count("\n")
    assert "color" in cleaned


def test_strip_noise_erases_strings():
    guard = _guard()
    cleaned = guard.strip_noise('const a = ".not-a-class { }";')
    assert ".not-a-class" not in cleaned


# ------------------------------------------------- 错误提示的方向（§6.3）


def test_missing_dependency_is_reported_before_network_trouble():
    """模块没打进包，提示必须是「重新安装」，不能是「换个网络」。

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
    # 跳过集合里的每一个都是「不是我们的代码」：
    #   .venv / venv        —— 装进来的第三方包，邮箱是包作者的（0.1.0 加：本机建了 venv）
    #   .git / dist / build —— 版本库与产物
    #   node_modules        —— 前端依赖
    skip = {".git", "dist", "build", "node_modules", "__pycache__",
            ".venv", "venv", ".mypy_cache", ".pytest_cache"}
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip for part in path.parts):
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
