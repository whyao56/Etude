"""启动前自检。

打包成 exe 之后，「报错」这件事会变样：源码态是控制台一片红字加 traceback，
冻结态是**双击没反应**。所以每条可能的失败都必须有一个出口 —— 这个自检
就是那个出口，结果同时写进数据目录的 ``logs/selfcheck.txt``。

三档，和界面上的颜色一一对应：
    ok   —— 好的
    warn —— 能用，但有件事你该知道（比如还没填密钥）
    fail —— 现在用不了，必须处理
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, config, db
from .paths import (
    audio_dir,
    frontend_dir,
    images_dir,
    is_frozen,
    log_dir,
    writable_root,
    writable_root_source,
)


def _item(name: str, level: str, detail: str, hint: str = "") -> dict:
    return {"name": name, "level": level, "detail": detail, "hint": hint}


def run(*, deep: bool = False) -> dict:
    """跑一遍自检。``deep=True`` 会真的发一次网络请求（慢，但更准）。"""
    items: list[dict] = []

    # ① 数据目录可写。这是打包后第一个会出问题的地方 ——
    #    程序目录可能只读，而数据写在哪必须在动手前就定好。
    try:
        root = writable_root()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        items.append(_item("数据目录", "ok", str(root)))
    except OSError as exc:
        items.append(
            _item(
                "数据目录",
                "fail",
                f"写不进去：{exc}",
                "检查这个目录的权限，或者用环境变量 ETUDE_DATA_DIR "
                "把数据指到一个你有写权限的位置。",
            )
        )

    # ② 界面文件在不在。
    index = frontend_dir() / "index.html"
    if index.exists():
        size = index.stat().st_size
        items.append(_item("界面文件", "ok", f"{index}（{size / 1024:.0f} KB）"))
    else:
        items.append(
            _item(
                "界面文件",
                "fail",
                f"找不到 {index}",
                "这是打包时漏了资源，重试修不好。请重新安装完整发行包。",
            )
        )

    # ③ 数据库能不能真的干活。
    #    注意判的是「能不能查到东西」，不是「连接对象造得出来」——
    #    这两者的差集正是「看着配好了、用起来没反应」。
    try:
        stats = db.stats()
        items.append(
            _item(
                "数据库",
                "ok",
                f"{stats['lessons']} 个训练包 / {stats['cards']} 张卡",
            )
        )
    except Exception as exc:  # noqa: BLE001
        items.append(
            _item(
                "数据库",
                "fail",
                f"打不开或查不了：{exc}",
                "如果这是升级后第一次启动，试着把数据目录里的 "
                "etude.sqlite3 改名备份，让程序重建一个新库。",
            )
        )

    # ④ 模型配置。
    cfg = config.load()
    llm_cfg = cfg["llm"]
    if not (llm_cfg.get("base_url") or "").strip():
        items.append(
            _item("大模型", "warn", "还没填接口地址", "去「设置」里选一个厂商。")
        )
    elif not (llm_cfg.get("api_key") or "").strip():
        items.append(
            _item(
                "大模型",
                "warn",
                f"{llm_cfg['base_url']} · {llm_cfg.get('model') or '没填模型'} · 还没填密钥",
                "本地 Ollama 之外的厂商都需要密钥。去「设置」里填。",
            )
        )
    elif not (llm_cfg.get("model") or "").strip():
        items.append(
            _item("大模型", "warn", "还没填模型名", "去「设置」里填上模型名。")
        )
    else:
        detail = f"{llm_cfg['base_url']} · {llm_cfg['model']}"
        if deep:
            from .providers import llm

            try:
                result = llm.check(
                    base_url=llm_cfg["base_url"],
                    api_key=llm_cfg["api_key"],
                    model=llm_cfg["model"],
                    timeout=20.0,
                )
                detail += f" · 实测连通（{result['seconds']} 秒）"
                items.append(_item("大模型", "ok", detail))
            except Exception as exc:  # noqa: BLE001
                hint = getattr(exc, "hint", "")
                items.append(
                    _item("大模型", "fail", f"{detail} · 连不上：{exc}", hint)
                )
        else:
            items.append(_item("大模型", "ok", detail))

    # ⑤ 语音引擎。
    try:
        import edge_tts  # noqa: F401

        items.append(
            _item("语音引擎", "ok", f"edge-tts · 默认音色 {cfg['tts'].get('voice')}")
        )
    except ImportError as exc:
        items.append(
            _item(
                "语音引擎",
                "fail",
                f"缺少 edge-tts：{exc}",
                "这不是网络问题，重试修不好。请重新安装完整发行包，"
                "或者把语音引擎切到「大模型语音」。",
            )
        )

    # ⑥ 原生窗口。这一项存在的理由很具体：用户报「双击没反应」时，
    #    最常见的原因就是这台机器缺 WebView2 运行时。
    #    让他跑一次 --check 就能知道，而不用去猜。
    level, detail, hint = _window_status()
    items.append(_item("原生窗口", level, detail, hint))

    # ⑦ 音频目录。
    try:
        count = len(list(audio_dir().glob("*.mp3")))
        items.append(_item("音频缓存", "ok", f"{count} 个文件"))
    except OSError as exc:
        items.append(_item("音频缓存", "fail", f"读不了：{exc}"))

    # ⑧ 场景配图。**这一项最高只报 warn** —— 图片不是训练的必需品，
    #    五条通道少了图照样能练。把它报成 fail 会让用户以为程序坏了。
    items.append(_image_item(cfg))

    # ⑨ 运行形态。这一条让「版本号对不上」在报告里一眼可辨。
    #    顺带报数据目录的来源：用户改过存放位置之后，
    #    「我的数据到底在哪、是谁定的」是最常被问的一件事。
    items.append(
        _item(
            "运行形态",
            "ok",
            f"{'打包版 exe' if is_frozen() else '源码运行'} · "
            f"Python {sys.version.split()[0]} · Etude {__version__} · "
            f"数据目录来源：{_source_label(writable_root_source())}",
        )
    )

    levels = {i["level"] for i in items}
    overall = "fail" if "fail" in levels else ("warn" if "warn" in levels else "ok")
    report = {
        "overall": overall,
        "version": __version__,
        "pid": os.getpid(),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": items,
    }
    _dump(report)
    return report


# WebView2 运行时的常见安装位置。它是 Windows 自带组件（Win11 默认有），
# 但精简版系统、部分 LTSC 和老版本 Win10 上可能缺。
_WEBVIEW2_DIRS = (
    r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
    r"C:\Program Files\Microsoft\EdgeWebView\Application",
)


def _source_label(source: str) -> str:
    return {
        "bootstrap": "设置里指定过（bootstrap.json）",
        "ETUDE_DATA_DIR": "环境变量 ETUDE_DATA_DIR",
        "default": "平台默认位置",
    }.get(source, source)


def _image_item(cfg: dict) -> dict:
    """场景配图的配置和缓存。

    **最高只报 warn。** 图片不是训练的必需品：五条通道少了图照样能练。
    把它报成 fail 会把一个「能用但少了点东西」的状态说成「不能用」。
    """
    images_cfg = cfg.get("images", {})
    source = (images_cfg.get("source") or "off").strip()

    try:
        count = len(list(images_dir().glob("*")))
    except OSError as exc:
        return _item("场景配图", "warn", f"读不了图片缓存：{exc}",
                     "图片不是必需项，训练不受影响。")

    if source == "off":
        return _item("场景配图", "ok", f"已关闭 · 缓存里还有 {count} 张")

    if source == "llm":
        gen = images_cfg.get("llm") or {}
        if not (gen.get("base_url") or "").strip() or not (gen.get("model") or "").strip():
            return _item(
                "场景配图",
                "warn",
                f"选了「用模型画图」但还没配好 · 缓存里 {count} 张",
                "去「设置 → 场景配图」填 base_url 和模型名，"
                "或者把来源改回「从开放图库检索」（免费、不需要配置）。",
            )
        if not (gen.get("api_key") or "").strip():
            return _item(
                "场景配图",
                "warn",
                f"{gen.get('base_url')} · {gen.get('model')} · 还没填密钥",
                "画图要密钥。填上，或者改回「从开放图库检索」。",
            )
        return _item("场景配图", "ok", f"用模型画图 · {gen.get('model')} · 缓存 {count} 张")

    return _item(
        "场景配图",
        "ok",
        f"开放图库检索（无需密钥，需要联网）· 缓存 {count} 张",
        "" if count else "还没有图，生成新包时会去检索。",
    )


def _window_status() -> tuple[str, str, str]:
    """原生窗口能不能用。返回 ``(level, detail, hint)``。

    **窗口不是必需品。** 缺了它程序会退回浏览器打开界面，功能一模一样，
    所以这里最高只报 warn —— 报 fail 会把一个「能用但没那么好看」的状态
    说成「不能用」，那是误导。
    """
    try:
        import webview  # noqa: F401
    except ImportError as exc:
        return (
            "warn",
            f"没有装原生窗口组件（{exc}）",
            "双击后会改用浏览器打开界面，功能不受影响。",
        )

    if not is_frozen():
        return ("ok", "源码运行，窗口组件可用", "")

    found = [d for d in _WEBVIEW2_DIRS if Path(d).exists()]
    if found:
        return ("ok", f"pywebview + 系统 WebView2（{found[0]}）", "")
    return (
        "warn",
        "有窗口组件，但没找到系统的 WebView2 运行时",
        "双击后会改用浏览器打开界面，功能不受影响。"
        "想要原生窗口的话，去微软官网装一下免费的 WebView2 Runtime。",
    )


def _dump(report: dict) -> None:
    """把报告落到磁盘。写不进去也就算了 —— 自检本身不该因此失败。"""
    try:
        lines = [
            f"Etude 自检 · {report['checked_at']} · 版本 {report['version']} · "
            f"pid {report['pid']}",
            f"总体：{report['overall']}",
            "",
        ]
        for item in report["items"]:
            lines.append(f"[{item['level'].upper():4}] {item['name']}：{item['detail']}")
            if item["hint"]:
                lines.append(f"        → {item['hint']}")
        (log_dir() / "selfcheck.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def format_text(report: dict) -> str:
    """给 ``--check`` 用的纯文本输出。"""
    icon = {"ok": "  OK  ", "warn": " WARN ", "fail": " FAIL "}
    out = [
        f"Etude {report['version']} 自检（{report['checked_at']}）",
        f"总体：{report['overall'].upper()}",
        "-" * 60,
    ]
    for item in report["items"]:
        out.append(f"[{icon.get(item['level'], '  ??  ')}] {item['name']}：{item['detail']}")
        if item["hint"]:
            out.append(f"           → {item['hint']}")
    return "\n".join(out)
