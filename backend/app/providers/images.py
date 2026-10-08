"""场景配图。

《学习观 07》里「场景」指的是**什么情况下会说这句话**。文字描述场景有一个
真实的弱点：学习者会去「理解这句话」，而不是直接进入情境。一张图能顶掉
这一步 —— 看图是瞬时的，读中文不是。

两条来源，用户自己选：

- **search**：从开放图库检索现成的照片（Openverse 为主，Wikimedia Commons
  兜底）。**不需要密钥、不花钱**，所以是默认值。缺点是只能命中真实存在的
  东西，「收到快递发现是坏的」这种情境搜不到。
- **llm**：让文生图模型画一张。任何 OpenAI 兼容的 ``/images/generations``
  都能用（智谱 CogView-3-Flash 有免费档）。慢一些、可能要花钱，
  但能画出检索不出来的情境。

## 一个必须说清的取舍

理论上「输入（声音/文字）到意思」之间**不该夹图片**。所以图片只出现在
**场景**该出现的地方：

- 阅读 / 听力卡的**背面**（你已经自己想出意思了，图只是核对）；
- 口语 / 打字 / 造句卡的**正面** —— 那里本来就是「场景 → 说出/写出」，
  图不夹在任何语言输入和意思之间，因为它替代的就是「场景」本身。

界面上的设置里可以整个关掉（``source = off``），纯文字路径就完全保留。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from ..paths import images_dir

# 图片生成端点。和语音/大模型一样，只管 base_url + key + model 三个字段。
IMAGE_PRESETS: dict[str, dict[str, str]] = {
    "zhipu": {
        "label": "智谱 CogView-3-Flash（有免费档）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "cogview-3-flash",
        "hint": "和生成例句用的智谱密钥是同一个，填一遍就行",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-image-1",
        "hint": "需要能直连 api.openai.com 的网络环境；这个模型按张计费",
    },
    "doubao": {
        "label": "豆包 · 火山方舟",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",
        "hint": "模型名填方舟上的接入点 ID（形如 ep-xxxx）；以厂商文档为准",
    },
    "custom": {
        "label": "自定义（任意 OpenAI 兼容端点）",
        "base_url": "",
        "model": "",
        "hint": "填完整的 base_url，通常以 /v1 结尾",
    },
}

IMAGE_SOURCES = [
    {"id": "search", "label": "从开放图库检索（免费、无需密钥）",
     "what": "搜真实照片。命中率高、速度快；抽象情境可能搜不到。"},
    {"id": "llm", "label": "用文生图模型画（需要配置）",
     "what": "什么情境都能画；慢一些，可能要花钱。"},
    {"id": "off", "label": "不用图片（只保留文字场景）",
     "what": "理论上最「干净」的形态：输入到意思之间不夹任何图片。"},
]

# 允许的单张图片体积。检索回来的图动辄几 MB，全存下来数据目录会爆。
MAX_BYTES = 4 * 1024 * 1024

_NAME = re.compile(r"^[a-z]+-[0-9a-f]{20}\.(jpg|png|webp|gif)$")

_UA = {"User-Agent": "Etude/0.0.2 (language drilling app; contact via project repo)"}


class ImageError(RuntimeError):
    """带「能修好的方向」的错误 —— 和 llm/tts 用同一套口径。"""

    def __init__(self, message: str, hint: str = "", *, kind: str = "unknown"):
        super().__init__(message)
        self.hint = hint
        self.kind = kind


def valid_name(name: str) -> bool:
    """``/api/images/{name}`` 用的白名单。和音频那条同样的理由：
    这是程序里「用户输入直接变文件路径」的地方，不校验就能读到 config.json。"""
    return bool(_NAME.match(name or ""))


def _is_local(base_url: str) -> bool:
    try:
        host = urllib.parse.urlparse(base_url).hostname or ""
    except ValueError:
        return False
    return host.lower() in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


def _client(timeout: float, base_url: str = "") -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=15.0),
        trust_env=not _is_local(base_url),
        headers=_UA,
        follow_redirects=True,
    )


# --------------------------------------------------------------- 场景 → 图


def build_prompt(scene_en: str, sentence: str = "") -> str:
    """把「场景」变成一句文生图提示词。

    **用场景而不是例句。** 例句是语言，图要画的是情境；拿例句去画会得到
    「一句话的插画」，而不是「说这句话的场合」。

    里面那条 ``no text`` 是必须的：文生图模型往图里写字基本都是乱码，
    而乱码英文出现在一个英语学习工具里格外刺眼。
    """
    scene = (scene_en or "").strip()
    if not scene:
        # 没有英文场景时退回到中文场景，让上游再传一次。
        return ""
    return (
        f"{scene}\n"
        "Photorealistic candid photograph, natural lighting, everyday setting. "
        "Show the situation itself, not an illustration of a sentence. "
        "Do not include any text, words, letters, numbers, signage or logos. "
        "No captions, no watermarks. Landscape orientation."
    )


def build_query(scene_en: str, sentence: str = "") -> str:
    """检索词。检索要的是**几个关键词**，不是一整句描述 ——
    搜索引擎对长句的命中率极差，这是「照着一句话去搜、什么都搜不到」的
    典型原因。"""
    text = (scene_en or "").strip() or (sentence or "").strip()
    if not text:
        return ""
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", text)
    stop = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "to", "of", "in", "on", "at", "for", "with", "and", "or", "but",
        "someone", "somebody", "you", "your", "he", "she", "they", "their",
        "his", "her", "it", "its", "that", "this", "these", "those", "who",
        "when", "while", "as", "has", "have", "had", "do", "does", "did",
        "very", "just", "about", "into", "from", "by", "so", "then",
    }
    picked = [w.lower() for w in words if w.lower() not in stop]
    if not picked:
        picked = [w.lower() for w in words]
    return " ".join(picked[:6])


# --------------------------------------------------------------- 缓存命名


def cache_name(source: str, key: str, ext: str) -> str:
    """内容寻址：同样的输入永远得到同一个名字，同一个情境不会存第二张图。"""
    digest = hashlib.sha256(f"{source}\x1f{key}".encode("utf-8")).hexdigest()[:20]
    ext = ext if ext in (".jpg", ".png", ".webp", ".gif") else ".jpg"
    return f"{source}-{digest}{ext}"


def cached(source: str, key: str) -> str:
    """已经在磁盘上的话直接返回文件名（扩展名可能有好几种，都试一遍）。"""
    for ext in (".jpg", ".png", ".webp", ".gif"):
        name = cache_name(source, key, ext)
        path = images_dir() / name
        if path.exists() and path.stat().st_size > 0:
            return name
    return ""


def _sniff(data: bytes) -> str:
    """从**文件头**判断真实格式。认不出来就返回空串。

    为什么既不看 URL 后缀、也不看 ``Content-Type``：那两个都是对方的**说法**，
    不是事实。真会遇到的是「返回 200、``Content-Type: image/jpeg``、
    内容是一页 HTML 错误页」—— 照着存成 ``xxx.jpg`` 之后，界面上就是一个
    永远加载不出来的破图框，而「配图成功」的计数还照加一。
    这正是本项目最忌讳的一类问题：不报错，但结果不对。

    宁可报错也不要存一张假的。报错会进 failures 列表、详情页写着「缺场景图」，
    用户能换个来源重来；存假图则是安静的错，要等他练到那张卡才发现。

    只认这四种：它们覆盖了开放图库和各家文生图接口实际会吐出来的全部格式。
    认不出来的（SVG、TIFF、BMP、AVIF……）一律拒绝，而不是硬猜一个后缀。
    """
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ""


def _looks_like_html(data: bytes) -> bool:
    """给失败提示用：让「你是不是把错误页存下来了」一眼可辨。"""
    head = data[:256].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html") or head.startswith(b"<?xml")


def _reject_unknown(data: bytes) -> "ImageError":
    """认不出来的内容 —— 造一条说清「下一步怎么办」的错误。"""
    head = data[:80].decode("utf-8", "replace").replace("\n", " ").strip()
    if _looks_like_html(data):
        return ImageError(
            "取回来的不是图片，而是一页网页（多半是错误页或需要登录）。",
            "这个图源可能挡了程序访问。改「从开放图库检索」或换一个画图模型试试。",
            kind="shape",
        )
    return ImageError(
        f"取回来的内容不是常见图片格式（开头是 {head!r}）。",
        "支持 JPEG / PNG / WebP / GIF。换一个图源或画图模型试试。",
        kind="shape",
    )


# ------------------------------------------------------------- 开放图库检索

OPENVERSE = "https://api.openverse.org/v1/images/"
COMMONS = "https://commons.wikimedia.org/w/api.php"


def _search_openverse(query: str, timeout: float) -> list[dict]:
    params = {
        "q": query,
        "page_size": 8,
        "mature": "false",
        "license_type": "all-cc",
    }
    with _client(timeout) as client:
        resp = client.get(OPENVERSE, params=params)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for item in data.get("results") or []:
        url = item.get("url") or ""
        if not url:
            continue
        out.append({
            "url": url,
            "width": int(item.get("width") or 0),
            "height": int(item.get("height") or 0),
            "title": item.get("title") or "",
            "creator": item.get("creator") or "",
            "license": item.get("license") or "",
            "page": item.get("foreign_landing_url") or "",
        })
    return out


def _search_commons(query: str, timeout: float) -> list[dict]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": "6",
        "gsrlimit": "8",
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": "1280",
    }
    with _client(timeout) as client:
        resp = client.get(COMMONS, params=params)
    resp.raise_for_status()
    pages = ((resp.json().get("query") or {}).get("pages") or {})
    out = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url") or ""
        if not url:
            continue
        out.append({
            "url": url,
            "width": int(info.get("thumbwidth") or info.get("width") or 0),
            "height": int(info.get("thumbheight") or info.get("height") or 0),
            "title": page.get("title") or "",
            "creator": "",
            "license": "Commons",
            "page": info.get("descriptionurl") or "",
        })
    return out


def pick_candidate(candidates: list[dict]) -> dict | None:
    """挑一张。

    先按「横图优先」排：竖图放进卡片里要么被裁掉一半，要么把版面撑得很高。
    再按分辨率下限过滤 —— 搜回来的缩略图有时只有 100px 宽，放大后糊成一片，
    还不如不放。
    """
    usable = [c for c in candidates if c.get("width", 0) >= 480]
    if not usable:
        usable = candidates
    if not usable:
        return None
    usable.sort(
        key=lambda c: (
            0 if c.get("width", 0) >= c.get("height", 1) else 1,
            -min(c.get("width", 0), 4000),
        )
    )
    return usable[0]


def search_image(query: str, *, timeout: float = 20.0) -> tuple[bytes, str, dict]:
    """检索并下载一张图。返回 ``(字节, 扩展名, 出处)``。

    两个图库依次试。**一个挂了就换下一个** —— Openverse 偶尔会限流，
    而「限流」不该等于「这个包没有图」。
    """
    if not query.strip():
        raise ImageError("没有可用来检索的场景描述。", "这个例句的 scene_en 是空的。")

    problems: list[str] = []
    for name, fn in (("Openverse", _search_openverse), ("Wikimedia Commons", _search_commons)):
        try:
            candidates = fn(query, timeout)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}：{exc}")
            continue
        best = pick_candidate(candidates)
        if best is None:
            problems.append(f"{name}：没有结果")
            continue
        try:
            raw, ext = _download(best["url"], timeout)
            return raw, ext, best
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name} 下载失败：{exc}")

    raise ImageError(
        "没找到合适的图片。" + ("（" + "；".join(problems[:2]) + "）" if problems else ""),
        "检索用的是例句的场景描述，抽象情境经常搜不到。"
        "可以在设置里把图片来源改成「用文生图模型画」，或者干脆关掉图片。",
        kind="not_found",
    )


def _download(url: str, timeout: float) -> tuple[bytes, str]:
    with _client(timeout) as client:
        resp = client.get(url)
    resp.raise_for_status()
    data = resp.content
    if not data:
        raise ImageError("下载回来是空的。")
    if len(data) > MAX_BYTES:
        raise ImageError(f"图片太大（{len(data) // 1024} KB）。")
    ext = _sniff(data)
    if not ext:
        raise _reject_unknown(data)
    return data, ext


# --------------------------------------------------------- 大模型生成图片


def _generate_llm(prompt: str, cfg: dict, timeout: float) -> tuple[bytes, str]:
    base_url = (cfg.get("base_url") or "").strip()
    api_key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model") or "").strip()
    if not base_url:
        raise ImageError(
            "选了大模型画图，但没填接口地址。",
            "去「设置 → 场景配图」里选一个厂商，或填自定义 base_url。",
            kind="no_config",
        )
    if not model:
        raise ImageError(
            "选了大模型画图，但没填模型名。",
            "去「设置 → 场景配图」里填上模型名（智谱可以用 cogview-3-flash）。",
            kind="no_config",
        )

    url = base_url.rstrip("/") + "/images/generations"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}

    try:
        with _client(timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        body = re.sub(r"\s+", " ", exc.response.text or "")[:220]
        if code == 404:
            raise ImageError(
                f"画图接口地址或模型名不对（404）。服务端说：{body}",
                f"检查 base_url 是否以 /v1 结尾、模型名「{model}」是否正确。",
                kind="not_found",
            ) from exc
        if code in (401, 403):
            raise ImageError(
                f"画图用的密钥没通过验证（{code}）。服务端说：{body}",
                "画图和大模型可能要用同一个密钥，但有些厂商要单独开通画图权限。",
                kind="auth",
            ) from exc
        if code == 429:
            raise ImageError(
                f"画图被限流或余额不足（429）。服务端说：{body}",
                "稍后再试；一直这样就去厂商控制台看余额。",
                kind="rate_limit",
            ) from exc
        raise ImageError(
            f"画图接口返回 {code}：{body}",
            "确认这个端点支持 OpenAI 的 /images/generations 格式；"
            "不支持的话就把图片来源改回「从开放图库检索」。",
            kind="http",
        ) from exc
    except httpx.TimeoutException as exc:
        raise ImageError(
            "等画图返回超时了。",
            "文生图通常要十几秒。可以先把图片来源改成「从开放图库检索」，"
            "或者调小每包生成的图片数量。",
            kind="timeout",
        ) from exc
    except (httpx.HTTPError, OSError) as exc:
        raise ImageError(
            f"连不上画图服务：{exc}",
            "检查网络和 base_url。挂了代理的话试试关掉。",
            kind="network",
        ) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise ImageError(
            f"画图接口返回的不是 JSON：{(resp.text or '')[:160]}",
            "确认 base_url 指向的是 OpenAI 兼容端点。",
            kind="shape",
        ) from exc

    return _extract_image(data, timeout, base_url)


def _extract_image(data: dict, timeout: float, base_url: str) -> tuple[bytes, str]:
    """从返回里取出图片字节。**三种形态都要吃得下**：

    - ``b64_json``（OpenAI 默认、部分国产厂商）
    - ``url``（智谱等，需要再下一次）
    - 直接给 ``data`` 数组里塞 ``image_url`` 之类别的字段名

    只认一种的话，换个厂商就会报「没返回图片」——
    而实际上图片就在响应里。
    """
    items = data.get("data")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        raise ImageError(
            f"画图接口没返回图片：{json.dumps(data, ensure_ascii=False)[:200]}",
            "确认这个端点支持 /images/generations；不支持就改回「从开放图库检索」。",
            kind="shape",
        )

    first = items[0] if isinstance(items[0], dict) else {}
    encoded = first.get("b64_json") or first.get("b64") or first.get("image_base64")
    if encoded:
        try:
            raw = base64.b64decode(encoded)
        except (ValueError, TypeError) as exc:
            raise ImageError("画图返回的图片数据解不开。", "换个模型或重试。") from exc
        ext = _sniff(raw)
        if not ext:
            raise _reject_unknown(raw)
        return raw, ext

    link = (
        first.get("url")
        or first.get("image_url")
        or (first.get("image") if isinstance(first.get("image"), str) else "")
    )
    if link:
        return _download(link, timeout)

    # 少数端点把图片直接放在顶层。
    if isinstance(data.get("url"), str):
        return _download(data["url"], timeout)

    raise ImageError(
        "画图接口的返回里既没有图片数据也没有图片地址。",
        "把这段反馈给作者；同时可以先改回「从开放图库检索」。",
        kind="shape",
    )


def check(*, source: str, query: str = "a person drinking coffee in a kitchen",
          gen_cfg: dict | None = None, timeout: float = 40.0) -> dict:
    """设置页的「试一张」。真的走一遍并落盘，返回文件名和体积。

    只校验配置不试跑的话，用户会以为配好了 —— 而真正的失败
    （模型名不对、端点不兼容、密钥没有画图权限）全在试跑那一步。
    """
    name = provide(
        query=query,
        prompt=build_prompt("A person drinking coffee at a kitchen table in the morning."),
        source=source,
        gen_cfg=gen_cfg or {},
        timeout=timeout,
        force=True,
    )
    path = images_dir() / name
    return {
        "ok": True,
        "source": source,
        "name": name,
        "bytes": path.stat().st_size if path.exists() else 0,
    }


def provide(
    *,
    query: str,
    prompt: str,
    source: str,
    gen_cfg: dict | None = None,
    timeout: float = 40.0,
    force: bool = False,
) -> str:
    """拿一张场景图，返回**相对 images 目录**的文件名；失败抛 ImageError。

    和音频一样存相对路径：存绝对路径的话，用户一改数据目录就全是死链。
    """
    source = (source or "off").strip()
    if source in ("off", "none", ""):
        return ""

    key = f"{source}\x1f{query or prompt}"
    if not force:
        hit = cached(source, key)
        if hit:
            return hit

    if source == "llm":
        raw, ext = _generate_llm(prompt or query, gen_cfg or {}, timeout)
    else:
        raw, ext, _meta = search_image(query or prompt, timeout=timeout)

    if not raw:
        raise ImageError("图片内容是空的。", "换个来源或重试。")
    if len(raw) > MAX_BYTES:
        raise ImageError(f"图片太大（{len(raw) // 1024} KB）。", "换个来源试试。")

    name = cache_name(source, key, ext)
    target = images_dir() / name
    target.write_bytes(raw)
    if target.stat().st_size == 0:
        # 和 TTS 那边同样的理由：有的库/端点会「成功返回但不写字节」。
        raise ImageError("图片没有写进磁盘。", "这多半是磁盘的问题，检查剩余空间。")
    return name


def source_catalog() -> dict:
    return {"sources": IMAGE_SOURCES, "presets": IMAGE_PRESETS}


__all__ = [
    "IMAGE_PRESETS",
    "IMAGE_SOURCES",
    "ImageError",
    "build_prompt",
    "build_query",
    "cache_name",
    "cached",
    "check",
    "provide",
    "search_image",
    "source_catalog",
    "valid_name",
]
