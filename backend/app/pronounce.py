"""跟读评分（0.1.0 的核心新东西）。

这个模块要回答一个问题：**没有音素级对齐的时候，还能诚实地说什么？**

能说的（全部可测，全部给实测值）：
- 你说了多久、原音多长 —— 完整性
- 中间停了几次、最长停了多久 —— 流畅度
- 声音太小还是爆音 —— 电平
- 配了转写服务时：这句话的内容对不对 —— 词级命中

不能说的：
- 「发音准不准」。这需要音素或声学模型。凭波形硬算一个「发音分」是骗人的：
  用户会拿它当进步依据，而它其实主要在测麦克风距离和房间混响。

所以分数由四路实测指标加权得出，每一项都给出实测值 + 一句人话解释。
没配转写服务时，「内容」显示成**未检查**，而不是 0 分 ——
0 分会让人以为「我念错了」，而未检查才是事实。
"""

from __future__ import annotations

import math
import re
import struct

from .providers import asr as asr_mod

# 分帧：25ms 是语音分析里的常用值（10ms 更细但噪声敏感，40ms 以上会糊掉音节边界）。
FRAME_MS = 25
# 多长的静音才算「停顿」。220ms 以下人耳会当成音与音之间的缝隙。
PAUSE_MS = 220
# 一句话的语速下限/上限（词/秒）。超出这个范围基本不是正常朗读。
WPS_MIN = 0.8
WPS_MAX = 4.5


# --------------------------------------------------------------- WAV 解析


def parse_wav(data: bytes) -> tuple[list[float], int]:
    """把 WAV 解成单声道浮点样本。

    只做自己能保证正确的事：认 RIFF/WAVE 头，按 fmt 块里的位深和声道数
    把 data 块解出来，多声道取平均。认不出就抛 ``ValueError`` ——
    宁可让界面说「这段录音格式不认识，重录一遍」，
    也不要拿一段乱码算出个像模像样的分数。
    """
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("这不是一个 WAV 文件。")

    channels = 1
    rate = 16000
    bits = 16
    fmt_tag = 1
    pos = 12
    pcm = b""
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        body = data[pos + 8 : pos + 8 + size]
        if chunk_id == b"fmt " and len(body) >= 16:
            fmt_tag, channels, rate = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
            if fmt_tag == 0xFFFE and len(body) >= 40:  # WAVE_FORMAT_EXTENSIBLE
                fmt_tag = struct.unpack("<H", body[24:26])[0]
        elif chunk_id == b"data":
            pcm = body
        pos += 8 + size + (size & 1)  # 块长度按偶数对齐
        if pcm:
            break

    if not pcm or not channels or not rate:
        raise ValueError("WAV 里没有可用的音频数据。")

    if fmt_tag == 3 or bits == 32 and fmt_tag == 3:  # IEEE float
        count = len(pcm) // 4
        samples = list(struct.unpack("<%df" % count, pcm[: count * 4]))
    elif bits == 16:
        count = len(pcm) // 2
        raw = struct.unpack("<%dh" % count, pcm[: count * 2])
        samples = [v / 32768.0 for v in raw]
    elif bits == 8:
        samples = [(v - 128) / 128.0 for v in pcm]
    elif bits == 24:
        count = len(pcm) // 3
        samples = []
        for i in range(count):
            b0, b1, b2 = pcm[i * 3 : i * 3 + 3]
            value = b0 | (b1 << 8) | (b2 << 16)
            if value & 0x800000:
                value -= 0x1000000
            samples.append(value / 8388608.0)
    elif bits == 32:
        count = len(pcm) // 4
        raw = struct.unpack("<%di" % count, pcm[: count * 4])
        samples = [v / 2147483648.0 for v in raw]
    else:
        raise ValueError(f"不认识的位深：{bits} 位。")

    if channels > 1:
        frames = len(samples) // channels
        mixed = []
        for i in range(frames):
            base = i * channels
            mixed.append(sum(samples[base : base + channels]) / channels)
        samples = mixed

    return samples, int(rate)


# --------------------------------------------------------------- 声学指标


def _frame_rms(samples: list[float], rate: int) -> list[float]:
    size = max(1, int(rate * FRAME_MS / 1000))
    out: list[float] = []
    for start in range(0, len(samples) - size + 1, size):
        window = samples[start : start + size]
        total = sum(v * v for v in window) / size
        out.append(math.sqrt(total))
    return out


def analyze(samples: list[float], rate: int) -> dict:
    """从波形里量出四件事：多长、有声多久、停了几次、声音多大。"""
    duration = len(samples) / float(rate or 1)
    frames = _frame_rms(samples, rate)
    if not frames:
        return {
            "duration": round(duration, 2),
            "speech_seconds": 0.0,
            "silence_ratio": 1.0,
            "pauses": [],
            "longest_pause": 0.0,
            "peak": 0.0,
            "clipping": 0.0,
            "frames": 0,
        }

    peak = max((abs(v) for v in samples), default=0.0)
    ordered = sorted(frames)
    # 噪声底：取最安静那 10% 的分位数。用 0 当阈值会把远处的空调声也算成说话。
    noise = ordered[max(0, min(len(ordered) - 1, int(len(ordered) * 0.1)))]
    threshold = max(peak * 0.06, 0.004)
    # 只有噪声底**明显**低于峰值时才采信它。全局静音或整段都是一样响的时候
    # （麦克风很近、没有背景声），10% 分位数本身就等于峰值，
    # 无条件乘 2.5 会把整段话判成静音 —— 这个 bug 让 1.2 秒的纯音
    # 测出「语音时长 0.0 秒」，界面上的结论是「没听到说话声」。
    if noise * 2.5 < peak * 0.35:
        threshold = max(threshold, noise * 2.5)
    voiced = [value >= threshold for value in frames]

    speech_frames = sum(1 for flag in voiced if flag)
    speech_seconds = speech_frames * FRAME_MS / 1000.0

    pauses: list[dict] = []
    run = 0
    for index, flag in enumerate(voiced):
        if flag:
            if run:
                length = run * FRAME_MS / 1000.0
                if length * 1000 >= PAUSE_MS:
                    pauses.append(
                        {
                            "start": round((index - run) * FRAME_MS / 1000.0, 2),
                            "seconds": round(length, 2),
                        }
                    )
                run = 0
            continue
        run += 1
    # 结尾的静音不算停顿（人说完会松一口气，那不是卡壳）。
    longest = max((p["seconds"] for p in pauses), default=0.0)
    clipping = (
        sum(1 for v in samples if abs(v) >= 0.995) / float(len(samples)) if samples else 0.0
    )

    return {
        "duration": round(duration, 2),
        "speech_seconds": round(speech_seconds, 2),
        "silence_ratio": round(1.0 - speech_frames / float(len(frames)), 3),
        "pauses": pauses[:12],
        "longest_pause": round(longest, 2),
        "peak": round(peak, 3),
        "clipping": round(clipping, 4),
        "frames": len(frames),
    }


# --------------------------------------------------------------- 词级对比


def _words(text: str) -> list[str]:
    """只留字母和内部的撇号。``don't`` 是一个词，``don’t``（花引号）也是。"""
    cleaned = (text or "").lower().replace("’", "'")
    return re.findall(r"[a-z]+(?:'[a-z]+)?", cleaned)


def align_words(target: str, heard: str) -> dict:
    """把「该说的」和「听到的」按最长公共子序列对齐。

    用 LCS 而不是逐位比较：漏掉一个词之后，逐位比较会把后面全都判错
    （「他漏了一个 the，于是整句红成一片」），而 LCS 只标出真正少的那一个。
    """
    want = _words(target)
    got = _words(heard)
    if not want:
        return {"items": [], "ratio": 0.0, "extra": got, "target_words": [], "heard_words": got}

    rows, cols = len(want) + 1, len(got) + 1
    table = [[0] * cols for _ in range(rows)]
    for i in range(len(want) - 1, -1, -1):
        for j in range(len(got) - 1, -1, -1):
            if want[i] == got[j]:
                table[i][j] = table[i + 1][j + 1] + 1
            else:
                table[i][j] = max(table[i + 1][j], table[i][j + 1])

    matched: set[int] = set()
    used: set[int] = set()
    i = j = 0
    while i < len(want) and j < len(got):
        if want[i] == got[j]:
            matched.add(i)
            used.add(j)
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1

    items = [
        {"word": word, "hit": index in matched} for index, word in enumerate(want)
    ]
    extra = [word for index, word in enumerate(got) if index not in used]
    return {
        "items": items,
        "ratio": round(len(matched) / float(len(want)), 3),
        "extra": extra[:20],
        "target_words": want,
        "heard_words": got,
    }


# --------------------------------------------------------------- 打分


def _level_of(score: float) -> str:
    if score >= 80:
        return "ok"
    if score >= 55:
        return "warn"
    return "bad"


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _completeness(speech_seconds: float, ref_seconds: float, word_count: int) -> dict:
    """完整性：你说的时长和原音比。

    参考时长拿不到（老数据没记音频长度）时退化成一个语速估计：
    英语朗读大约每秒 2.6 个词。**这个估计是写死在提示里的假设**，
    所以界面上会把「参考时长是估的」标出来 —— 不标的话分数看起来像实测。
    """
    estimated = False
    ref = float(ref_seconds or 0.0)
    if ref <= 0.1 and word_count:
        ref = word_count / 2.6
        estimated = True
    if ref <= 0.1:
        return {
            "id": "duration",
            "label": "说完整了",
            "level": "skip",
            "value": "—",
            "detail": "没有参考时长，这项不判。",
            "hint": "",
            "score": None,
        }

    ratio = speech_seconds / ref
    if 0.75 <= ratio <= 1.65:
        score = 100.0
    elif ratio < 0.75:
        # 短于 75% 通常意味着漏了词或者念得太快；线性压到 0（比例 0.3 以下算 0）。
        score = _clamp((ratio - 0.3) / (0.75 - 0.3) * 100.0)
    else:
        # 长于 165% 是拖沓，也可能是中间犹豫；到 3 倍算 0。
        score = _clamp((3.0 - ratio) / (3.0 - 1.65) * 100.0)

    if score >= 80:
        detail = f"说了 {speech_seconds:.1f} 秒，原音约 {ref:.1f} 秒 —— 长度很接近。"
    elif ratio < 0.75:
        detail = f"只说了 {speech_seconds:.1f} 秒，原音约 {ref:.1f} 秒 —— 少了 {ref - speech_seconds:.1f} 秒。"
    else:
        detail = f"说了 {speech_seconds:.1f} 秒，原音约 {ref:.1f} 秒 —— 拖得有点长。"
    if estimated:
        detail += "（参考时长是按词数估的）"
    return {
        "id": "duration",
        "label": "说完整了",
        "level": _level_of(score),
        "value": f"{ratio * 100:.0f}%",
        "detail": detail,
        "hint": "" if score >= 80 else "漏词就重念一遍整句，别只念一半。",
        "score": round(score, 1),
    }


def _fluency(analysis: dict) -> dict:
    pauses = analysis.get("pauses") or []
    longest = float(analysis.get("longest_pause") or 0.0)
    score = 100.0
    # 前两次停顿是正常的换气，不扣分。之后每多一次扣 18。
    score -= max(0, len(pauses) - 1) * 18.0
    score -= max(0.0, longest - 0.6) * 120.0
    score = _clamp(score)
    if pauses:
        detail = f"停顿 {len(pauses)} 次，最长 {longest:.2f} 秒。"
    else:
        detail = "一口气说完，中间没有明显停顿。"
    return {
        "id": "fluency",
        "label": "顺不顺",
        "level": _level_of(score),
        "value": f"{len(pauses)} 次停顿",
        "detail": detail,
        "hint": "" if score >= 80 else "卡住的那一处，单独把那半句念五遍。",
        "score": round(score, 1),
    }


def _level(analysis: dict) -> dict:
    peak = float(analysis.get("peak") or 0.0)
    clipping = float(analysis.get("clipping") or 0.0)
    if peak < 0.03:
        score, level = 20.0, "bad"
        detail = f"最大音量只有 {peak:.2f}，几乎听不见。"
        hint = "凑近一点、把麦克风音量调大；或者检查系统是不是选错了输入设备。"
    elif clipping > 0.02:
        score, level = 55.0, "warn"
        detail = f"{clipping * 100:.1f}% 的样本削顶（爆音）。"
        hint = "离麦克风远一点，或者把输入音量降下来 —— 爆音会把细节全削平。"
    elif peak < 0.12:
        score, level = 70.0, "warn"
        detail = f"最大音量 {peak:.2f}，偏小。"
        hint = "再大声一点：这句是要「说出口」的，不是默念。"
    else:
        score, level = 100.0, "ok"
        detail = f"最大音量 {peak:.2f}，没有爆音。"
        hint = ""
    return {
        "id": "level",
        "label": "音量",
        "level": level,
        "value": f"{peak:.2f}",
        "detail": detail,
        "hint": hint,
        "score": round(score, 1),
    }


def _content(text: str, target: str, skip_reason: str = "") -> dict:
    if skip_reason:
        return {
            "id": "content",
            "label": "内容对不对",
            "level": "skip",
            "value": "未检查",
            "detail": skip_reason,
            "hint": "想看词级对比的话，去设置里配一个转写服务。",
            "score": None,
        }
    aligned = align_words(target, text)
    ratio = float(aligned["ratio"])
    score = ratio * 100.0
    missed = [item["word"] for item in aligned["items"] if not item["hit"]]
    extra = aligned["extra"]
    detail = f"听成「{text}」。" if text else "转写结果为空。"
    if missed:
        detail += "没听到：" + "、".join(missed[:6]) + "。"
    if extra:
        detail += "多出来的：" + "、".join(extra[:6]) + "。"
    return {
        "id": "content",
        "label": "内容对不对",
        "level": _level_of(score),
        "value": f"{ratio * 100:.0f}%",
        "detail": detail,
        "hint": "" if score >= 80 else "先照着原文念准每个词，再追求一口气说完。",
        "score": round(score, 1),
        "words": aligned["items"],
        "heard": text,
        "extra": extra,
    }


WEIGHTS_WITH_ASR = {"content": 0.55, "duration": 0.15, "fluency": 0.20, "level": 0.10}
WEIGHTS_ACOUSTIC = {"content": 0.0, "duration": 0.40, "fluency": 0.40, "level": 0.20}


def evaluate(
    wav: bytes,
    *,
    target: str = "",
    ref_seconds: float = 0.0,
    word_count: int = 0,
    cfg: dict | None = None,
    allow_asr: bool = True,
) -> dict:
    """跑完一次评分。返回的东西直接给界面画。

    失败也返回结果（``score=0`` + 一句说明），不走异常 ——
    「录音没听见」不是程序出错，是这次练习的结论。
    """
    cfg = cfg or {}
    try:
        samples, rate = parse_wav(wav)
    except ValueError as exc:
        return _blank(str(exc))

    analysis = analyze(samples, rate)
    if analysis["duration"] < 0.3:
        return _blank("录音太短了（不到 0.3 秒），是不是没按住就松开了？", analysis)
    if analysis["speech_seconds"] < 0.2:
        return _blank("录音里没有听到说话声。", analysis)

    heard = ""
    asr_error = ""
    asr_used = False
    if allow_asr and (cfg.get("mode") or "auto") != "acoustic" and target:
        try:
            if asr_mod.configured(cfg):
                heard = asr_mod.transcribe(wav, cfg)["text"]
                asr_used = True
            elif (cfg.get("mode") or "auto") == "asr":
                asr_error = "设置里选了「只用转写」，但没有配转写服务。"
        except asr_mod.ASRError as exc:
            asr_error = str(exc)

    items = [
        _content(
            heard,
            target,
            "" if asr_used else (
                f"转写没跑成：{asr_error}" if asr_error
                else "没配转写服务，这项不判（时长、停顿、音量照常测）。"
            ),
        ),
        _completeness(analysis["speech_seconds"], ref_seconds, word_count),
        _fluency(analysis),
        _level(analysis),
    ]

    weights = WEIGHTS_WITH_ASR if asr_used else WEIGHTS_ACOUSTIC
    total = 0.0
    weight_sum = 0.0
    for item in items:
        weight = weights.get(item["id"], 0.0)
        if item["score"] is None or weight <= 0:
            continue
        total += item["score"] * weight
        weight_sum += weight
    # 权重归一化：某项没判（比如没有参考时长）时，不能用 0 分身拉低总分 ——
    # 那会把「没有数据」说成「做得不好」。
    score = round(total / weight_sum, 1) if weight_sum else 0.0

    return {
        "score": score,
        "mode": "mixed" if asr_used else "acoustic",
        "items": items,
        "analysis": analysis,
        "heard": heard,
        "asr_error": asr_error,
        "advice": _advice(items),
        # 逐词命中。只有真的做了转写才有 —— 声学模式下不知道你念了哪些词，
        # 编一份「全命中」出来会让人以为发音没问题，那是假信息。
        "words": align_words(target, heard)["items"] if (asr_used and target) else [],
    }


def _blank(reason: str, analysis: dict | None = None) -> dict:
    return {
        "score": 0.0,
        "mode": "acoustic",
        "items": [
            {
                "id": "level",
                "label": "这次没录上",
                "level": "bad",
                "value": "—",
                "detail": reason,
                "hint": "按住录音键，把整句说完再松手。",
                "score": 0.0,
            }
        ],
        "analysis": analysis or {},
        "heard": "",
        "asr_error": "",
        "advice": reason,
    }


def _advice(items: list[dict]) -> str:
    """挑最弱的那一项说一句话。分数会被人忘掉，一句话不会。"""
    graded = [item for item in items if item.get("score") is not None]
    if not graded:
        return "这次没量出有效数据。"
    worst = min(graded, key=lambda item: float(item.get("score") or 0.0))
    if float(worst.get("score") or 0.0) >= 85:
        return "这一遍很稳。换一句，或者直接去评分。"
    return str(worst.get("hint") or worst.get("detail") or "")
