"""本地假模型服务。

存在的意义有两个，都很实际：

1. **让整条链路能被测。** 只 monkeypatch ``llm.chat`` 的话，HTTP 请求怎么拼、
   401/404 怎么翻译、``response_format`` 不被支持时怎么退让，全都测不到 ——
   而这些恰恰是最容易写错的地方。
2. **不用真密钥就能验证一个包能不能跑通。** 上一个项目里「云模型真实效果」
   一直是没验证的一项，因为手上没有可用的 Key。有了这个桩，
   流水线的正确性不再依赖外部账号。

用法：
    python scripts/openai_stub.py --port 18999
然后设置里把 base_url 填 http://127.0.0.1:18999/v1 ，密钥随便填，模型随便填。

它还会按 base_url 里的标记模拟几种坏情况（见 FAULTS），
用来验证错误提示是不是真的指向了能修好的方向。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from fastapi import FastAPI, Header, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402

app = FastAPI(title="Etude fake model")

# 假的「mp3」。内容不重要，只要是非空的字节 ——
# 我们要验的是「文件被写出来了、路径能取到、大小不为零」，
# 不是解码。真要验解码得另说，那是另一件事。
FAKE_MP3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 2048

# 每个场景给三种**彼此不同**的中文说法：一种极短、一种正常、一种换个角度。
# 这是理论要求的核心机制（「中文解释每次都要不同」），桩也得照着模拟 ——
# 如果桩只给一种中文，界面上那个「每次随机换一种」的功能就永远是摆设，
# 也就永远测不出来。
SCENES = [
    {
        "en": "At a concert, someone worries the crowd won't get it — you say it appeals to anyone.",
        "zh": ["这不挑人。", "音乐节上有人担心台下听不懂，你说谁都能听得进去。", "台上刚起第一个音，旁边的人还在犹豫，你已经知道这曲子对谁都成立。"],
        "register": "日常口语",
    },
    {
        "en": "A friend is being modest about a dish they cooked; you tell them not to put themselves down.",
        "zh": ["别谦虚了。", "朋友说自己做的菜一般，你让他别客气。", "筷子还没放下，你已经把盘子往他那边推了推。"],
        "register": "日常口语",
    },
    {
        "en": "You are describing a melody that lands with everyone regardless of taste.",
        "zh": ["谁听了都舒服。", "聊到一段不用挑听众的旋律。", "客厅里放着，写代码的和写作业的都没抬头抗议。"],
        "register": "日常口语",
    },
    {
        "en": "Someone asks whether the kids will sit through the show.",
        "zh": ["小孩也坐得住。", "有人问孩子看这场演出会不会闹。", "散场时最小的那个还在跟着哼，你就知道答案了。"],
        "register": "日常口语",
    },
    {
        "en": "A teacher explains why an old piece still holds up today.",
        "zh": ["老东西经得起时间。", "老师说这首老曲子放到今天听依然站得住。", "几十年过去，课堂里第一次听的人还是会安静下来。"],
        "register": "正式",
    },
    {
        "en": "You are describing a painting that does not need any explaining.",
        "zh": ["不用解释也看得懂。", "说到一幅画，站在前面的人自己就明白了。", "标签还没读完，人已经在那儿站了两分钟。"],
        "register": "书面",
    },
    {
        "en": "A colleague doubts that ordinary people will care about it.",
        "zh": ["普通人也会在意。", "同事觉得这事跟一般人没关系，你不这么看。", "把数据摊开之后，会议室里没人再问「这跟我们有什么关系」。"],
        "register": "正式",
    },
    {
        "en": "You are recommending a film to someone with completely different taste.",
        "zh": ["你也会喜欢。", "给口味完全不同的人推荐一部片子，你说这个不用挑人。", "他平时只看纪录片，这次看到一半就把字幕关了。"],
        "register": "日常口语",
    },
]

# 按 kind 生成自然的例句，而不是把目标硬塞进一个固定模板
# （硬塞的话，目标是整句时就会拼出病句，而演示数据里全是病句
# 会让「到底是桩坏了还是程序坏了」变得难判断）。
TEMPLATES = {
    "word": [
        "I really {t} the way this one works.",
        "She didn't {t} what he was trying to do.",
        "Most people {t} a good cup of coffee.",
        "You have to {t} how hard that was.",
        "He learned to {t} the small things.",
        "We {t} it more as we get older.",
        "Anyone can {t} a well-told story.",
        "They never {t} what she did for them.",
    ],
    "phrase": [
        "It'll pay off {t}.",
        "We'll see {t} whether it was worth it.",
        "She's been quiet {t}.",
        "I'll take the slower route {t}.",
        "It works {t}, just not quickly.",
        "Let's revisit this {t}.",
        "He's been right {t}.",
        "You'll thank yourself {t}.",
    ],
    "sentence": [
        "{t}",
        "Honestly, {t_lower}",
        "{t} That's the whole reason we came.",
        "I keep saying it: {t_lower}",
        "{t} Nobody argued with that.",
        "After everything, {t_lower}",
        "{t} And that was that.",
        "She put it plainly — {t_lower}",
    ],
}


def build_sentences(target: str, kind: str, count: int) -> list[str]:
    templates = TEMPLATES.get(kind, TEMPLATES["word"])
    out: list[str] = []
    for i in range(count):
        template = templates[i % len(templates)]
        text = template.format(t=target, t_lower=target[0].lower() + target[1:])
        # 句子目标不重复塞同一句：换人称、换时态，保持句型不变。
        if kind == "sentence" and i > 0 and i % 3 == 0:
            text = _shift(text)
        out.append(text)
    return out


def _shift(sentence: str) -> str:
    """把句子换个人称或时态 —— 保持结构，只动一个变量。

    理论要的是「不同例子的重复」：变形的是情境，不是句型。
    """
    swaps = [("I ", "She "), ("I'd ", "He'd "), ("I would ", "They would "),
             ("my ", "her "), ("we ", "they "), ("We ", "They ")]
    for old, new in swaps:
        if old in sentence:
            return sentence.replace(old, new, 1)
    return sentence + " Still true."


def build_payload(target: str, kind: str, count: int, zh_variants: int) -> dict:
    sentences = build_sentences(target, kind, count)
    examples = []
    for i, sentence in enumerate(sentences):
        scene = SCENES[i % len(SCENES)]
        variants = scene["zh"][: max(1, min(zh_variants, len(scene["zh"])))]
        examples.append(
            {
                "sentence": sentence,
                "scene_en": scene["en"],
                "scene_zh": variants[0] if variants else "",
                "zh_variants": variants,
                "register": scene["register"],
            }
        )

    subject = "I" if kind != "sentence" else "She"
    verb = target if kind == "word" else "really"
    obj = "the way this one works" if kind == "word" else "this"
    return {
        "kind": kind,
        "ipa": "əˈpriːʃieɪt" if kind == "word" else "",
        "gloss": (
            f"to understand how good {target} is, or to enjoy it"
            if kind == "word"
            else f"how people use “{target}” in real conversation"
        ),
        "note": "Common in everyday speech.",
        "examples": examples,
        "swaps": [
            {
                "role": "subject",
                "original": subject,
                "candidates": ["We", "They", "Most people"],
                "samples": [f"We {verb} it.", f"They {verb} it."],
            },
            {
                "role": "object",
                "original": obj,
                "candidates": ["the design", "the ending", "the effort"],
                "samples": [f"{subject} {verb} the design."],
            },
            {
                "role": "verb",
                "original": verb,
                "candidates": ["enjoy", "understand", "value"],
                "samples": [f"{subject} enjoy it.", f"{subject} understand it."],
            },
        ],
    }


def build_syllabus_payload(count: int) -> dict:
    """一份**形状正确**的大纲。

    刻意照着提示词里那条最要紧的约束来造：功能词 / 固定说法 / 句型 / 话题词
    各占一部分，而不是一张词表。桩要是造出一张词表，那么「提示词有没有
    真的把形状要求传下去」这件事就永远测不出来了。
    """
    words = [
        ("actually", "word", "A2", "把「其实」这一层意思说出来，不再只会说 really"),
        ("be about to", "phrase", "B1", "说清楚「正要去做」，比 I will 精确"),
        ("I'd rather ...", "sentence", "B1", "客气又有立场地说出自己更想要哪个"),
        ("get used to", "phrase", "B2", "讲「习惯了」这件事，而不是只会 used to"),
        ("though", "word", "B1", "把补充和转折挂在句尾，像母语者那样说话"),
        ("make sense", "phrase", "B1", "说「这个讲得通 / 我理解了」"),
        ("What do you mean by ...?", "sentence", "B1", "听不懂时把话头接住，而不是沉默"),
        ("waste of time", "phrase", "B1", "表达不值当，语气比 not good 重"),
    ]
    outline = []
    for i in range(count):
        target, kind, band, why = words[i % len(words)]
        outline.append({
            "target": f"{target}" if i < len(words) else f"{target} {i + 1}",
            "kind": kind,
            "domain": "日常口语",
            "band": band,
            "why": why,
        })
    return {
        "note": "先从能立刻开口的功能词和固定说法开始，再往句型走。每天练几个就行，不用一次做完。",
        "outline": outline,
    }


def _png_bytes(width: int = 320, height: int = 200) -> bytes:
    """造一张**尺寸像样**的真 PNG。

    不引第三方库：桩的依赖越少越好，而且这个函数本身也顺手验证了
    「响应体真的是 PNG」—— ``images._sniff`` 就是靠文件头判断格式的。

    为什么不用 1×1：界面截图是要拿去看/放进文档的。1×1 的图在
    ``max-width:100%`` 下渲染出来还是 1×1 像素，于是每张截图里
    场景图的位置看上去都是「图没加载出来」—— 而实际上链路全通。
    那会让人去修一个根本不存在的 bug。
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    horizon = int(height * 0.55)
    for y in range(height):
        raw.append(0)  # 每行的 filter 字节：0 = 不滤波
        for _x in range(width):
            if y < horizon:
                # 上半：天，由上到下渐亮。
                t = y / max(horizon, 1)
                r, g, b = int(118 + 72 * t), int(150 + 58 * t), int(192 + 38 * t)
            else:
                # 下半：地面，越往下越暗 —— 看着像一张户外照片。
                t = (y - horizon) / max(height - horizon, 1)
                r, g, b = int(152 - 46 * t), int(142 - 44 * t), int(118 - 40 * t)
            raw += bytes((r, g, b))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


FAKE_PNG = _png_bytes()


def payload_from_messages(messages: list[dict]) -> dict:
    system = ""
    user = ""
    for m in messages:
        if m.get("role") == "system" and not system:
            system = m.get("content", "")
    for m in reversed(messages):
        if m.get("role") == "user":
            user = m.get("content", "")
            break

    # 大纲请求走另一套结构。靠系统提示词里的标记分路，
    # 不靠「用户那句话长什么样」—— 后者随时会被改文案。
    if "You design the SYLLABUS" in system:
        import re

        m = re.search(r"Provide exactly (\d+) items", user)
        return build_syllabus_payload(int(m.group(1)) if m else 5)

    target = "appreciate"
    kind = "word"
    for marker, name in (
        ("TARGET WORD: ", "word"),
        ("TARGET PHRASE: ", "phrase"),
        ("TARGET SENTENCE: ", "sentence"),
    ):
        if marker in user:
            start = user.index(marker) + len(marker)
            target = user[start:].split("\n")[0].strip().strip('"') or target
            kind = name
            break
    import re

    m = re.search(r"Provide exactly (\d+) examples", user)
    count = int(m.group(1)) if m else 8
    variants = 3 if "种彼此不同的中文说法" in user else 2
    return build_payload(target, kind, count, variants)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "etude-fake-model"}


@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str | None = Header(default=None)) -> Response:
    body = await request.json()

    # 按模型名模拟故障，用来验证错误提示指向的方向对不对。
    model = str(body.get("model", ""))
    if model.startswith("fault-401"):
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
    if model.startswith("fault-403"):
        return JSONResponse({"error": {"message": "no permission for this model"}}, status_code=403)
    if model.startswith("fault-404"):
        return JSONResponse({"error": {"message": "model not found"}}, status_code=404)
    if model.startswith("fault-500"):
        return JSONResponse({"error": {"message": "upstream boom"}}, status_code=500)
    if model.startswith("fault-429"):
        return JSONResponse({"error": {"message": "rate limited"}}, status_code=429)
    if model.startswith("fault-notjson"):
        return {"choices": [{"message": {"content": "抱歉，我不太明白你的要求。"}}]}
    if model.startswith("fault-fenced"):
        payload = payload_from_messages(body.get("messages", []))
        text = "好的，这是结果：\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n希望有帮助！"
        return {"choices": [{"message": {"content": text}}]}
    if model.startswith("fault-messy"):
        # 前后带解说、末尾还有花括号干扰 —— 用来测 extract_json 的配平扫描。
        payload = payload_from_messages(body.get("messages", []))
        text = "Here you go {not json}: " + json.dumps(payload, ensure_ascii=False) + " done }"
        return {"choices": [{"message": {"content": text}}]}
    if model.startswith("fault-slow"):
        time.sleep(3.0)
    if model.startswith("fault-nokey") and not (authorization or "").strip():
        return JSONResponse({"error": {"message": "missing key"}}, status_code=401)

    payload = payload_from_messages(body.get("messages", []))
    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}


@app.post("/v1/audio/speech")
async def speech(request: Request) -> Response:
    body = await request.json()
    if not body.get("input"):
        return JSONResponse({"error": {"message": "empty input"}}, status_code=400)
    if str(body.get("voice", "")).startswith("fault"):
        return JSONResponse({"error": {"message": "bad voice"}}, status_code=400)
    return Response(content=FAKE_MP3, media_type="audio/mpeg")


@app.get("/fake-image.png")
def fake_image() -> Response:
    """给「接口返回地址而不是数据」那条分支当下载源。"""
    return Response(content=FAKE_PNG, media_type="image/png")


@app.post("/v1/images/generations")
async def images(request: Request) -> Response:
    """画图。

    三种返回形态都要能被解析，所以这里用模型名控制走哪一条：
      · 默认            → ``b64_json``（OpenAI 默认形态）
      · ``url-...``     → ``url``（智谱等，需要再下一次）
      · ``fault-image-*`` → 各自的坏情况
    只测一种形态的话，换个厂商就会报「没返回图片」，而图片其实就在响应里。
    """
    import base64

    body = await request.json()
    if not body.get("prompt"):
        return JSONResponse({"error": {"message": "empty prompt"}}, status_code=400)

    model = str(body.get("model", ""))
    if model.startswith("fault-image-auth"):
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
    if model.startswith("fault-image-notfound"):
        return JSONResponse({"error": {"message": "model not found"}}, status_code=404)
    if model.startswith("fault-image-empty"):
        return {"data": []}
    if model.startswith("fault-image-badpayload"):
        # 200，但里面有东西不是图片 —— 用来验「不信 URL 后缀、靠文件头嗅探」。
        return {"data": [{"b64_json": base64.b64encode(b"<html>nope</html>").decode()}]}
    if model.startswith("url-"):
        return {"data": [{"url": str(request.base_url) + "fake-image.png"}]}
    return {"data": [{"b64_json": base64.b64encode(FAKE_PNG).decode()}]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18999)
    args = parser.parse_args()

    import uvicorn

    print(f"假模型服务已启动 → http://127.0.0.1:{args.port}/v1")
    print("设置里 base_url 填它，API Key 和模型名随便填。")
    print("模型名用 fault-401 / fault-404 / fault-500 / fault-notjson / "
          "fault-fenced / fault-messy / fault-slow 可以模拟各种坏情况。")
    print("画图同样在这个服务上：/v1/images/generations。"
          "模型名以 url- 开头会返回地址而不是图片数据，"
          "fault-image-auth / -notfound / -empty / -badpayload 是各自的坏情况。")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
