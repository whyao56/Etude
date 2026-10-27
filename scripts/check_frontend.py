"""前端守卫（ES 模块版）。

0.0.x 的前端是一个 3900 行的 index.html，`check_js.py` 从 `<script>` 里抠出
代码做检查。0.1.0 拆成 17 个 ES 模块之后那套做法整份失效了：每个文件的
「外部名字」来自 `import`，不再来自同一段作用域里的声明。

这个脚本查四件事，每一条都对应一类**不报错但界面会坏**的错：

1. **import 的名字在目标模块里真的有 export。**
   改了导出名忘了改导入名，浏览器只在控制台报一句 undefined。
2. **图标名在 dom.js 的 PATHS 里有。**
   拼错不报错，只画一个空方块 —— 或者什么都没有。
3. **JS 里用到的 class 名在 CSS 里真有规则。**
   对不上不报错，页面只是看起来「没做完」，而且能一直这么发出去。
4. **js/version.js 的 APP_VERSION 与 app/__init__.py 的 __version__ 一致。**
   发版时「标签对、内容错」就是这么来的。

做法和 check_js.py 一样：每个探针先用 `_SELF_TEST` 自测一遍再说结论。
探针坏了会伪装成被测对象坏了 —— 这句话是 0.0.2 一次误报换来的。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "backend" / "frontend"
JS_DIR = FRONTEND / "js"
CSS_DIR = FRONTEND / "css"

IMPORT_RE = re.compile(
    r"""^\s*import\s+(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)\s+from\s+["']([^"']+)["']""",
    re.MULTILINE,
)
NAMED_IMPORT_RE = re.compile(r"^\s*import\s+\{([^}]*)\}\s+from\s+[\"']([^\"']+)[\"']", re.MULTILINE)
DEFAULT_IMPORT_RE = re.compile(r"^\s*import\s+(\w+)\s+from\s+[\"']([^\"']+)[\"']", re.MULTILINE)
EXPORT_DECL_RE = re.compile(
    r"^\s*export\s+(?:async\s+)?(?:function|class|const|let|var)\s+([\w$]+)", re.MULTILINE
)
EXPORT_LIST_RE = re.compile(r"^\s*export\s*\{([^}]*)\}", re.MULTILINE)
EXPORT_DEFAULT_RE = re.compile(r"^\s*export\s+default\b", re.MULTILINE)

ICON_CALL_RE = re.compile(r"\bicon(?:Svg)?\(\s*[\"']([a-z0-9-]+)[\"']")
ICON_DATA_RE = re.compile(r"icon:\s*[\"']([a-z0-9-]+)[\"']")
PATHS_BLOCK_RE = re.compile(r"\bconst\s+PATHS\s*=\s*\{(.*?)\n\};", re.DOTALL)
PATH_KEY_RE = re.compile(r"^\s{2}([a-z][a-z0-9-]*):", re.MULTILINE)

CLASS_USE_RE = re.compile(r"\bclass:\s*[\"'`]([^\"'`]+)[\"'`]")
CLASS_LIST_RE = re.compile(r"classList\.(?:add|remove|toggle)\(\s*[\"']([^\"']+)[\"']")
CLASS_NAME_RE = re.compile(r"^[A-Za-z][\w-]*$")
TEMPLATE_EXPR_RE = re.compile(r"[\w-]*\$\{[^}]*\}")
CSS_SELECTOR_RE = re.compile(r"\.([A-Za-z][\w-]*)")


def class_tokens(blob: str) -> list[str]:
    """把 `class: "a b c"` / 模板串里的类名拆出来。

    模板串里可能夹着表达式（`class: `grade g${item.g}`）—— 先把 `${...}`
    整段换成空格，再只留形如标识符的片段。不这么做的话，守卫会把
    `.===`、`.?`、`.state.variant` 这种东西当成缺样式的类名报出来，
    十条里九条是假的，看的人第三次就会忽略它。
    """
    cleaned = TEMPLATE_EXPR_RE.sub(" ", blob)
    tokens: list[str] = []
    for piece in cleaned.split():
        if CLASS_NAME_RE.match(piece):
            tokens.append(piece)
    return tokens

_SELF_TEST_SAMPLES = [
    # (样本, 说明)
    (".a { color: red }", "普通规则"),
    (".item-row .detail { grid-column: 2 / -1 }", "后代选择器"),
    ('[aria-pressed="true"] .chip { }', "属性选择器里的引号"),
    (".x::before { content: '.not-a-class' }", "content 里的类名是假阳性"),
]


def strip_noise(text: str) -> str:
    """把注释、字符串、模板串、正则字面量变成空白，**保持长度和换行**。

    行号必须准：一个报告「views.css:1173」的守卫，如果行号偏了三十行，
    用的人第一件事就是不信它。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
        elif ch in "\"'`":
            quote = ch
            out[i] = " "
            i += 1
            while i < n:
                if text[i] == "\\":
                    out[i] = " "
                    if i + 1 < n and text[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if text[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                if text[i] != "\n":
                    out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def self_test() -> list[str]:
    """先证明探针还能用。返回问题清单（空 = 都好）。"""
    problems: list[str] = []
    for sample, why in _SELF_TEST_SAMPLES:
        stripped = strip_noise(sample)
        if len(stripped) != len(sample):
            problems.append(f"strip_noise 改变了长度（{why}）")
        if stripped.count("\n") != sample.count("\n"):
            problems.append(f"strip_noise 吞掉了换行（{why}）")

    # 字符串里的内容必须被抹掉，但长度不变
    probe = 'const a = ".fake { color: red }";'
    if "color" in strip_noise(probe):
        problems.append("strip_noise 没抹掉字符串内容")

    # 行号：第 3 行的东西必须还在第 3 行
    multi = "a\nb\nc\n"
    if strip_noise(multi).splitlines()[2] != "c":
        problems.append("strip_noise 改变了行号")

    # import / export 探针
    code = 'import { a, b as c } from "../core/api.js";\nexport function a() {}\nexport const b = 1;\n'
    if NAMED_IMPORT_RE.search(code) is None:
        problems.append("NAMED_IMPORT_RE 没认出命名导入")
    if set(EXPORT_DECL_RE.findall(code)) != {"a", "b"}:
        problems.append("EXPORT_DECL_RE 没认出导出")

    # 图标探针
    if ICON_CALL_RE.findall('icon("trash", 16) iconSvg("mic")') != ["trash", "mic"]:
        problems.append("ICON_CALL_RE 没认出图标名")
    return problems


def js_files() -> list[Path]:
    return sorted(JS_DIR.rglob("*.js"))


def exports_of(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    names = set(EXPORT_DECL_RE.findall(text))
    for block in EXPORT_LIST_RE.findall(text):
        for piece in block.split(","):
            piece = piece.strip()
            if not piece:
                continue
            # export { a as b } —— 对外叫 b
            names.add(piece.split(" as ")[-1].strip())
    if EXPORT_DEFAULT_RE.search(text):
        names.add("default")
    return names


def check_imports(files: list[Path]) -> list[str]:
    problems: list[str] = []
    cache: dict[Path, set[str]] = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        for named, target in NAMED_IMPORT_RE.findall(text):
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                problems.append(f"{rel(path)}: 导入的 {target} 不存在")
                continue
            if resolved not in cache:
                cache[resolved] = exports_of(resolved)
            available = cache[resolved]
            for piece in named.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                wanted = piece.split(" as ")[0].strip()
                if wanted and wanted not in available:
                    problems.append(
                        f"{rel(path)}: 从 {target} 导入了 {wanted}，但那边没有导出它"
                    )
        for name, target in DEFAULT_IMPORT_RE.findall(text):
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                continue
            if resolved not in cache:
                cache[resolved] = exports_of(resolved)
            if "default" not in cache[resolved]:
                problems.append(f"{rel(path)}: {target} 没有默认导出")
    return problems


def paths_of_dom() -> set[str]:
    text = (JS_DIR / "core" / "dom.js").read_text(encoding="utf-8")
    block = PATHS_BLOCK_RE.search(text)
    if block is None:
        raise SystemExit("dom.js 里找不到 PATHS 表 —— 探针过期了，先修守卫。")
    return set(PATH_KEY_RE.findall(block.group(1)))


def check_icons(files: list[Path]) -> list[str]:
    known = paths_of_dom()
    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name in ICON_CALL_RE.findall(text) + ICON_DATA_RE.findall(text):
            if name not in known:
                problems.append(f"{rel(path)}: 图标 {name} 不在 PATHS 里")
    return problems


def css_classes() -> dict[str, str]:
    """类名 -> 第一次出现的位置，用来说「在哪个文件里有的」。"""
    found: dict[str, str] = {}
    for path in sorted(CSS_DIR.glob("*.css")):
        clean = strip_noise(path.read_text(encoding="utf-8"))
        for name in CSS_SELECTOR_RE.findall(clean):
            found.setdefault(name, path.name)
    return found


def check_classes(files: list[Path]) -> list[str]:
    known = css_classes()
    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        uses: list[str] = []
        for blob in CLASS_USE_RE.findall(text) + CLASS_LIST_RE.findall(text):
            uses.extend(class_tokens(blob))
        for name in sorted(set(uses)):
            if "${" in name or not name.strip():
                continue
            if name not in known:
                problems.append(f"{rel(path)}: 类名 .{name} 在所有 CSS 里都没有规则")
    return problems


VERSION_JS_RE = re.compile(r"APP_VERSION\s*=\s*[\"']([^\"']+)[\"']")
PY_VERSION_RE = re.compile(r"__version__\s*=\s*[\"']([^\"']+)[\"']")


def check_version() -> list[str]:
    js_text = (JS_DIR / "version.js").read_text(encoding="utf-8")
    py_text = (ROOT / "backend" / "app" / "__init__.py").read_text(encoding="utf-8")
    js_version = VERSION_JS_RE.search(js_text)
    py_version = PY_VERSION_RE.search(py_text)
    if not js_version or not py_version:
        return ["读不出版本号（version.js 或 app/__init__.py 的写法变了）"]
    if js_version.group(1) != py_version.group(1):
        return [
            f"版本不一致：js/version.js 是 {js_version.group(1)}，"
            f"app/__init__.py 是 {py_version.group(1)}"
        ]
    return []


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def run() -> int:
    broken = self_test()
    if broken:
        print("守卫自测没过，先别信它的结论：")
        for line in broken:
            print(f"  - {line}")
        return 2

    files = js_files()
    if not files:
        print("一个前端模块都没找到 —— 路径变了？")
        return 2

    problems = []
    problems += check_imports(files)
    problems += check_icons(files)
    problems += check_classes(files)
    problems += check_version()

    if problems:
        print(f"前端守卫：{len(problems)} 个问题")
        for line in problems:
            print(f"  - {line}")
        return 1

    print(
        f"前端守卫通过：{len(files)} 个模块，"
        f"{len(paths_of_dom())} 个图标，{len(css_classes())} 个类名，版本 "
        f"{VERSION_JS_RE.search((JS_DIR / 'version.js').read_text(encoding='utf-8')).group(1)}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检查前端 ES 模块（导入、图标、类名、版本）")
    parser.add_argument("--quiet", action="store_true", help="只在出错时说话")
    args = parser.parse_args()
    code = run()
    if args.quiet and code == 0:
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main())
