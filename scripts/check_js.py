"""前端调用点静态守卫。

为什么需要它：单文件 HTML + 零构建 = **没有编译器帮你**。
上个项目里，一次重构漏改了 5 个调用点，一路走到用户面前才炸 ——
而且全量测试竟然是绿的（因为那条路径没被测到）。

这个脚本做一件很朴素的事：把脚本里所有 ``名字(`` 抠出来，
减去「声明过的」「语言关键字的」「运行环境自带的」「点号后面的」，
剩下的必须为空。剩下的每一个都可能是「引用了不存在的函数」。

两个刻意的处理：

1. **先把字符串和注释清掉再扫。** 不清的话，``el("i",{style:"width:min(...)"})``
   会带出一个 ``min(``，而它是个 CSS 函数 —— 报出一堆假警报的守卫，
   用户很快就会学会无视它，那还不如没有。
2. **点号后面的名字一律不算。** ``obj.method()`` 里的 method 不归本文件管。

用法：
    python scripts/check_js.py            # 检查并打印结论
    python scripts/check_js.py -v         # 连「减掉了什么」一起打印
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "backend" / "frontend" / "index.html"

# 语言关键字 + 运行环境自带的全局函数。
# 只列真的会用到的，不追求完整 —— 漏了的会报成「可疑」，
# 看一眼就知道是自己的问题还是白名单要补。
KEYWORDS = {
    "if", "for", "while", "switch", "catch", "function", "return", "typeof",
    "instanceof", "in", "of", "new", "delete", "void", "do", "else", "try",
    "throw", "await", "async", "yield", "case", "with", "super",
}

BUILTINS = {
    # 语言内建
    "Array", "Object", "String", "Number", "Boolean", "Symbol", "BigInt",
    "Map", "Set", "WeakMap", "WeakSet", "Promise", "Date", "RegExp", "Error",
    "TypeError", "RangeError", "JSON", "Math", "Proxy", "Reflect",
    # 浏览器 / 宿主
    "alert", "confirm", "prompt", "fetch", "setTimeout", "clearTimeout",
    "setInterval", "clearInterval", "requestAnimationFrame", "queueMicrotask",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    "parseInt", "parseFloat", "isNaN", "isFinite", "structuredClone",
    "matchMedia", "getComputedStyle", "Audio", "Image", "FileReader",
    "Blob", "URL", "URLSearchParams", "AbortController", "TextEncoder",
    "TextDecoder", "localStorage", "sessionStorage", "Intl", "PerformanceObserver",
}


def _regex_allowed_before(text: str, index: int) -> bool:
    """判断这个 ``/`` 是正则字面量的开头，还是除法运算符。

    判据是它前面那个**有效字符**。表达式位置上（``(``、``=``、``,``、
    ``return`` 之后……）出现 ``/`` 是正则；紧跟在标识符、数字、`)`、`]` 之后的
    是除法。

    这个判断必须做对。不做的话，形如 ``/[^a-z0-9' ]/g`` 的正则里那个单引号
    会被当成字符串开头，然后**把中间一大段代码整个吃掉** ——
    于是守卫会报出一堆「未定义」的假警报，而真正的问题被淹没。
    这个坑我第一版就踩了，所以下面还配了一个自检（见 self_test）。
    """
    i = index - 1
    while i >= 0 and text[i] in " \t\r\n":
        i -= 1
    if i < 0:
        return True
    prev = text[i]
    if prev in "(,=:[!&|?{};+-*%~^<>":
        return True
    # `return /re/` 这类关键字后面也是表达式位置。
    head = text[: i + 1]
    word = re.search(r"([A-Za-z_$][\w$]*)$", head)
    if word and word.group(1) in {
        "return", "typeof", "instanceof", "in", "of", "new", "delete",
        "void", "case", "do", "else", "yield", "await",
    }:
        return True
    return False


def strip_noise(source: str) -> str:
    """把注释、字符串、模板串、正则字面量里的内容清空，但**保持长度和换行**。

    保持长度是为了让报出来的行号还是准的 —— 一个指错行的守卫，
    排查成本比它省下来的还高。
    """
    out = list(source)
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch == "/" and nxt == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                if source[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = out[i + 1] = " "
            i += 2
            continue
        if ch == "/" and _regex_allowed_before(source, i):
            # 正则字面量：扫到结束的那个 /（方括号里的 / 不算，字符类里可以出现 /）。
            out[i] = " "
            i += 1
            in_class = False
            while i < n:
                cur = source[i]
                if cur == "\\":
                    out[i] = " "
                    if i + 1 < n and source[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if cur == "[":
                    in_class = True
                elif cur == "]":
                    in_class = False
                elif cur == "/" and not in_class:
                    out[i] = " "
                    i += 1
                    break
                elif cur == "\n":
                    # 正则不能跨行，说明这不是正则而是除法 —— 回退。
                    break
                if cur != "\n":
                    out[i] = " "
                i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out[i] = " "
            i += 1
            while i < n:
                if source[i] == "\\":
                    out[i] = " "
                    if i + 1 < n and source[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if source[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                if source[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


# 探针自检。**先拿已知为正的样本校验探针，再去相信它的结论** ——
# 上个项目里「探测脚本自己写错了」骗过一次人（三个修复全报 False，
# 看起来像重建没生效，其实是探针用错了成员判断）。
# 这里的样本专门覆盖几个会骗过朴素实现的东西。
_SELF_TEST_SAMPLES = [
    # （样本，必须仍然可见的片段；必须已经被清掉的片段）
    (
        """function keepMe() { return /[^a-z0-9' ]/g.test("x"); }
           function alsoKeep() { }""",
        ["function keepMe", "function alsoKeep", "return"],
        ["a-z0-9"],
    ),
    (
        """var a = b / c; function afterDivision() {}""",
        ["function afterDivision", "var a"],
        [],
    ),
    (
        """// 注释里有 ' 和 " 和 `
           function afterComment() {}""",
        ["function afterComment"],
        ["注释里有"],
    ),
    (
        """function afterString() { var s = "he said \\" , and ' too"; }""",
        ["function afterString"],
        ["he said"],
    ),
    (
        """var t = `模板 ' 与 " 与 ${x}`; function afterTemplate() {}""",
        ["function afterTemplate", "var t"],
        ["模板"],
    ),
]


def self_test() -> list[str]:
    """跑一遍探针自检，返回失败项（空列表表示探针可信）。"""
    failures: list[str] = []
    for idx, (sample, must_keep, must_drop) in enumerate(_SELF_TEST_SAMPLES):
        cleaned = strip_noise(sample)
        if len(cleaned) != len(sample):
            failures.append(f"样本 {idx}：清洗改变了长度（{len(sample)} → {len(cleaned)}）")
        for frag in must_keep:
            if frag not in cleaned:
                failures.append(f"样本 {idx}：应当保留的 {frag!r} 被吃掉了")
        for frag in must_drop:
            if frag in cleaned:
                failures.append(f"样本 {idx}：应当清掉的 {frag!r} 还在")
    return failures


def extract_scripts(html: str) -> list[tuple[int, str]]:
    """抠出所有 <script> 块，连同它在原文件里的起始行号。"""
    blocks = []
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        line = html[: match.start(1)].count("\n") + 1
        blocks.append((line, match.group(1)))
    return blocks


def declared_names(source: str) -> set[str]:
    """本文件里声明过的函数名和变量名。"""
    names: set[str] = set()
    # function foo(  /  async function foo(
    names |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", source))
    # const foo = ... / let foo = ... / var foo = ...
    names |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", source))
    # const { a, b } = ...  解构
    for group in re.findall(r"\b(?:const|let|var)\s*\{([^}]*)\}", source):
        names |= set(re.findall(r"[A-Za-z_$][\w$]*", group))
    # 对象字面量里的方法简写：  { foo(a) { ... } }   以及  { foo: function(a){} }
    names |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*:\s*(?:async\s+)?function\b", source))
    names |= set(
        re.findall(r"(?:^|[,{]\s*)([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", source, re.M)
    )
    # for (const x of y)  /  catch (e)
    names |= set(re.findall(r"\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)", source))
    names |= set(re.findall(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)", source))
    return names


def called_names(source: str) -> dict[str, int]:
    """所有 ``名字(`` 的出现位置（行号），排除点号后面的。"""
    found: dict[str, int] = {}
    for match in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", source):
        name = match.group(1)
        found.setdefault(name, source[: match.start()].count("\n") + 1)
    return found


def bad_event_type_literals(source: str) -> tuple[list[tuple[str, int]], bool]:
    """找出 ``addEventListener("Click", ...)`` 这种写法。

    ``addEventListener`` 的事件类型串是**大小写敏感**的：
    ``"Click"`` / ``"Input"`` 既不会报错、也不会触发。
    症状是「界面照常画出来，但点什么都没反应」——
    而截图看不出来、单元测试也测不到（这是第一版真实踩到的 bug，
    靠真浏览器里的点击检查才发现）。

    返回 ``(有问题的位置列表, 是否存在「把事件名转小写」这一步)``。
    第二个值用来确认修好了 —— 光把大写改成小写还不够，
    得保证 ``el()`` 里那一步真的做了转换。
    """
    offenders: list[tuple[str, int]] = []
    pattern = re.compile(r"""addEventListener\s*\(\s*["'`]([^"'`]+)["'`]""")
    for match in pattern.finditer(source):
        name = match.group(1)
        if re.search(r"[A-Z]", name):
            offenders.append((name, source[: match.start()].count("\n") + 1))
    lowered = re.search(
        r"""addEventListener\s*\(\s*[^,]+?\.toLowerCase\s*\(\s*\)""", source
    )
    return offenders, lowered is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true", help="打印减掉的项")
    parser.add_argument("--html", default=str(HTML), help="要检查的 HTML 文件")
    args = parser.parse_args()

    path = Path(args.html)
    if not path.exists():
        print(f"找不到 {path}", file=sys.stderr)
        return 2

    # 先校验探针本身。探针坏了会伪装成被测对象坏了 —— 那比没有守卫更糟，
    # 因为你会照着一堆假线索去改代码。
    probe_failures = self_test()
    if probe_failures:
        print("探针自检没通过，先修守卫再谈结论：")
        for line in probe_failures:
            print("  · " + line)
        return 2

    html = path.read_text(encoding="utf-8")
    blocks = extract_scripts(html)
    if not blocks:
        print("文件里没有 <script> 块。")
        return 1

    all_declared: set[str] = set()
    suspicious: list[tuple[str, int]] = []
    bad_events: list[tuple[str, int]] = []
    lowercases_events = False

    for offset, code in blocks:
        clean = strip_noise(code)
        declared = declared_names(clean)
        all_declared |= declared
        for name, rel_line in called_names(clean).items():
            if name in declared or name in KEYWORDS or name in BUILTINS:
                continue
            suspicious.append((name, offset + rel_line - 1))

        # 事件名检查要扫**原文**（事件名本身是个字符串，已被清洗掉）。
        found, lowers = bad_event_type_literals(code)
        bad_events += [(n, offset + l - 1) for n, l in found]
        lowercases_events = lowercases_events or lowers

    if args.verbose:
        print(f"声明过的名字（{len(all_declared)}）：{sorted(all_declared)}")
        print(f"白名单关键字：{len(KEYWORDS)} 个；内建：{len(BUILTINS)} 个")

    failed = False

    if bad_events:
        failed = True
        print(f"发现 {len(bad_events)} 处大小写错误的事件名（这种写法不会触发）：")
        for name, line in bad_events:
            print(f"  {path.name}:{line}  addEventListener({name!r})")
        print("  事件类型串是大小写敏感的，要用全小写。")

    # 这条盯的是「修好了没有」：el() 里必须有一处把 onXxx 转成小写。
    if not bad_events and not lowercases_events:
        failed = True
        print("找不到把事件名转小写的那一步。")
        print("  如果 el() 改成别的实现了，请确认它仍然对事件名做了 toLowerCase()。")

    if suspicious:
        failed = True
        print(f"发现 {len(suspicious)} 个「调用了但本文件没定义」的名字：")
        for name, line in suspicious:
            print(f"  {path.name}:{line}  {name}()")
        print("要么是漏改了调用点（把它删掉或改名了），要么是白名单要补一个内建。")

    if failed:
        return 1

    total = sum(len(called_names(strip_noise(c))) for _o, c in blocks)
    print(f"前端调用点检查通过：{total} 处调用，全部有定义；事件名大小写正确。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
