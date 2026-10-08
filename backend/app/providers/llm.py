"""大模型接入。

只实现一种协议：**OpenAI 兼容的 /chat/completions**。
DeepSeek、豆包（火山方舟）、通义（百炼）、智谱、Kimi、OpenAI、本地 Ollama
都提供这个端点，所以「预置厂商」本质上只是几个填好的默认值，
真正决定连哪里的永远是 ``base_url`` + ``api_key`` + ``model`` 三个字段。
换厂商不需要改代码。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from typing import Any

import httpx

# 预置厂商：只填 base_url 和默认模型，不含任何密钥。
# 密钥只存在于用户本机的 config.json，见 config.py 的说明。
PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek 深度求索",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "hint": "在 platform.deepseek.com 申请密钥",
    },
    "doubao": {
        "label": "豆包 · 火山方舟",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",
        "hint": "模型名要填你在方舟上创建的「接入点 ID」（形如 ep-xxxx）或模型 ID",
    },
    "qwen": {
        "label": "通义千问 · 阿里百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "hint": "在 bailian.console.aliyun.com 申请密钥",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "hint": "在 bigmodel.cn 申请密钥",
    },
    "moonshot": {
        "label": "Kimi · 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "hint": "在 platform.moonshot.cn 申请密钥",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint": "需要能直连 api.openai.com 的网络环境",
    },
    "ollama": {
        "label": "本地 Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "hint": "密钥随便填一个非空值即可；先用 ollama pull qwen2.5:7b 拉模型",
    },
    "custom": {
        "label": "自定义（任意 OpenAI 兼容端点）",
        "base_url": "",
        "model": "",
        "hint": "填完整的 base_url，通常以 /v1 结尾",
    },
}

# 本机地址不经过系统代理。上一个项目的实测教训：代理会把对本机的请求
# 改写成绝对地址，于是本地服务收到一个 404 而不是「连不上」——
# 症状像功能坏了，其实是代理插了一脚。
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


class LLMError(RuntimeError):
    """带「能修好的方向」的错误。

    ``hint`` 是给用户看的下一步动作，不是技术术语堆砌。
    """

    def __init__(self, message: str, hint: str = "", *, kind: str = "unknown"):
        super().__init__(message)
        self.hint = hint
        self.kind = kind

    def as_dict(self) -> dict:
        return {"error": str(self), "hint": self.hint, "kind": self.kind}


# --------------------------------------------------------------- 错误翻译


def _is_local(base_url: str) -> bool:
    try:
        host = urllib.parse.urlparse(base_url).hostname or ""
    except ValueError:
        return False
    return host.lower() in _LOCAL_HOSTS


def explain_error(exc: Exception, base_url: str = "", model: str = "") -> LLMError:
    """把底层报错翻成人话，并给一个**方向正确**的下一步。

    顺序很讲究：**「缺组件」必须排在所有网络关键词之前**。
    模块名有可能碰巧命中网络关键词（比如某个包叫 ``http_something``），
    排在后面就会被误判成网络故障 —— 而换镜像、重试都补不上一个
    没打进包的模块。这类错误必须被指到「重新安装 / 换完整包」上去。
    """
    text = f"{type(exc).__name__}: {exc}"

    # ① 缺组件 —— 排在网络判断之前，因为「重试 / 换镜像」修不好它。
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return LLMError(
            f"程序缺少一个运行组件：{exc}",
            "这不是网络问题，重试也不会好。请重新安装完整发行包，"
            "或把这份报错原样反馈给作者。",
            kind="missing_dependency",
        )

    # ② 连不上本机服务。方向是「把服务起起来」，不是「检查网络」。
    if _is_local(base_url) and isinstance(
        exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)
    ):
        return LLMError(
            f"连不上本机的大模型服务：{base_url}",
            "base_url 指向的是本机地址。请先确认服务已经启动"
            "（例如在命令行跑 ollama serve），再点一次。",
            kind="local_service_down",
        )

    # ③ 明确的 HTTP 状态码。
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        body = _short_body(exc.response)
        if code == 401:
            return LLMError(
                f"密钥没通过验证（401）。服务端说：{body}",
                "检查 API Key 是否复制完整、有没有多余空格；"
                "确认这个 Key 属于当前 base_url 对应的厂商。",
                kind="auth",
            )
        if code == 403:
            return LLMError(
                f"这个密钥没有调用权限（403）。服务端说：{body}",
                "确认账号已开通对应模型；部分厂商需要先实名或充值。",
                kind="forbidden",
            )
        if code == 404:
            return LLMError(
                f"接口地址或模型名不对（404）。服务端说：{body}",
                f"检查 base_url 是否以 /v1 结尾、模型名「{model}」是否拼写正确。"
                "豆包的用户注意：模型名要填接入点 ID。",
                kind="not_found",
            )
        if code == 429:
            return LLMError(
                f"被限流或余额不足（429）。服务端说：{body}",
                "稍等一会儿再试；如果一直是这个错，去厂商控制台看余额和额度。",
                kind="rate_limit",
            )
        if code >= 500:
            return LLMError(
                f"模型服务端出错（{code}）。服务端说：{body}",
                "这是对方的问题，不是你配错了。等几分钟再试一次。",
                kind="server",
            )
        return LLMError(
            f"请求被拒绝（{code}）。服务端说：{body}",
            "把这段报错连同你填的 base_url、模型名一起反馈给作者。",
            kind="http",
        )

    # ④ 超时 / 网络。
    if isinstance(exc, (httpx.TimeoutException,)):
        return LLMError(
            "等模型返回超时了。",
            "例句较多的训练包本来就要几十秒。可以在设置里把超时调大，"
            "或者换成响应更快的模型。",
            kind="timeout",
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ProxyError, httpx.TransportError)):
        return LLMError(
            f"连不上模型服务：{exc}",
            "检查网络和 base_url 拼写。如果挂了代理，试试关掉代理再点一次。",
            kind="network",
        )

    # ⑤ 兜底。
    return LLMError(
        f"调用模型时出了个没预料到的错：{text}",
        "点一次重试；如果一直失败，把这段报错反馈给作者。",
        kind="unknown",
    )


def _short_body(response: httpx.Response, limit: int = 300) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - 读 body 失败不该盖住真正的错误
        return "<读不到响应内容>"
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or "<空响应>"


# --------------------------------------------------------------- JSON 解析


def extract_json(text: str) -> dict:
    """从模型回复里抠出 JSON 对象。

    三层兜底，因为不同厂商对「请输出 JSON」的执行力度差别很大：
    规规矩矩的、套 ```json 围栏的、前后带解说词的，三种都遇到过。
    """
    if not text or not text.strip():
        raise LLMError("模型返回了空内容。", "点一次重试，或换一个更稳定的模型。")

    raw = text.strip()

    # ① 直接就是 JSON。
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # ② 剥掉代码围栏。
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        try:
            data = json.loads(fence.group(1).strip())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # ③ 从**每一个** { 开始试，配平地扫到对应的 }，取第一个能解析成功的。
    #
    #    为什么不是「从第一个 { 试一次就放弃」：解说词里本来就可能有花括号
    #    （「结果是 {这样的} 一个对象：{...}」）。只试一次的话，
    #    会在那个无关的花括号上失败，然后报「模型没按 JSON 返回」——
    #    而实际上 JSON 就在后面，好好的。
    for start in (i for i, ch in enumerate(raw) if ch == "{"):
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(raw[start : i + 1])
                    except json.JSONDecodeError:
                        break  # 这一段不是合法 JSON，换下一个起点
                    if isinstance(data, dict):
                        return data
                    break

    raise LLMError(
        f"模型没有按要求的 JSON 格式返回。开头是：{raw[:180]}",
        "点一次重试通常就好了。如果反复如此，换一个指令遵循更强的模型"
        "（例如把模型名换成带 pro / max 后缀的版本）。",
        kind="bad_json",
    )


# --------------------------------------------------------------- 调用


def _client(base_url: str, timeout: float) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=15.0),
        # 本机地址关掉代理读取，避免代理把 localhost 请求改写成绝对地址。
        trust_env=not _is_local(base_url),
        headers={"Content-Type": "application/json"},
    )


def chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float = 0.9,
    max_tokens: int = 4096,
    timeout: float = 120.0,
    json_mode: bool = True,
) -> str:
    """调一次 chat/completions，返回正文。

    ``json_mode`` 会先带 ``response_format`` 试；有些兼容端点不认这个字段
    会直接 400，那就**去掉它重试一次**。这比猜「哪家支持」可靠 ——
    厂商的支持情况会变，而 400 的语义不会。
    """
    if not base_url.strip():
        raise LLMError("还没填接口地址。", "去「设置」里选一个厂商，或填自定义 base_url。")
    if not model.strip():
        raise LLMError("还没填模型名。", "去「设置」里填上模型名再试。")

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key.strip() else {}

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    use_json_mode = json_mode
    last_error: Exception | None = None

    for attempt in range(_MAX_ATTEMPTS):
        body = dict(payload)
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            with _client(base_url, timeout) as client:
                resp = client.post(url, json=body, headers=headers)
            if resp.status_code == 400 and use_json_mode:
                # 这个端点不认 response_format。去掉它重来，不计入重试次数。
                use_json_mode = False
                continue
            resp.raise_for_status()
            data = resp.json()
            return _content_of(data)
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code not in _RETRYABLE_STATUS:
                raise explain_error(exc, base_url, model) from exc
        except (
            httpx.TimeoutException,
            httpx.TransportError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
        ) as exc:
            last_error = exc

        if attempt < _MAX_ATTEMPTS - 1:
            # 退避：1s, 2s。限流场景下立刻重试只会再被拒一次。
            time.sleep(1.0 * (2**attempt))

    assert last_error is not None
    raise explain_error(last_error, base_url, model) from last_error


def _content_of(data: dict) -> str:
    """取出正文。同时容忍推理模型把内容放在别的字段里。"""
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            f"返回的格式不像 OpenAI 兼容接口：{json.dumps(data, ensure_ascii=False)[:200]}",
            "确认 base_url 是 OpenAI 兼容端点（通常以 /v1 结尾）。",
            kind="shape",
        ) from exc

    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # 有的厂商返回分块结构。
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not content:
        content = message.get("reasoning_content") or choice.get("text") or ""
    if not str(content).strip():
        raise LLMError("模型返回了空正文。", "点一次重试，或换一个模型。")
    return str(content)


def check(*, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> dict:
    """设置页的「测试连接」。只发一个最小请求，成本几乎为零。"""
    started = time.time()
    text = chat(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=[
            {"role": "user", "content": '只回复一个 JSON：{"ok": true}'},
        ],
        temperature=0.0,
        max_tokens=64,
        timeout=timeout,
    )
    return {"ok": True, "seconds": round(time.time() - started, 2), "echo": text[:120]}


def resolve_preset(name: str) -> dict[str, str]:
    return PRESETS.get(name, PRESETS["custom"])


def apply_preset(config: dict, name: str) -> dict:
    """把预置厂商的默认值写进配置。**不动 api_key** ——
    换厂商时用户可能只是先看看，把他的密钥抹掉会很烦。"""
    preset = resolve_preset(name)
    llm = dict(config.get("llm", {}))
    llm["preset"] = name
    if preset["base_url"]:
        llm["base_url"] = preset["base_url"]
    if preset["model"]:
        llm["model"] = preset["model"]
    config["llm"] = llm
    return config


def environ_key() -> str:
    """允许用环境变量给密钥（CI 和临时试用用）。"""
    return os.environ.get("ETUDE_LLM_API_KEY", "")
