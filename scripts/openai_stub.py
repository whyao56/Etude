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


def payload_from_messages(messages: list[dict]) -> dict:
    user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user = m.get("content", "")
            break
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18999)
    args = parser.parse_args()

    import uvicorn

    print(f"假模型服务已启动 → http://127.0.0.1:{args.port}/v1")
    print("设置里 base_url 填它，API Key 和模型名随便填。")
    print("模型名用 fault-401 / fault-404 / fault-500 / fault-notjson / "
          "fault-fenced / fault-messy / fault-slow 可以模拟各种坏情况。")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
