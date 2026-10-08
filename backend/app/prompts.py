"""提示词 —— 《学习观 07》落到产品上的那一层。

理论原文的要点，逐条对应到提示词的约束：

| 理论原文 | 提示词里的约束 |
|---|---|
| 语言是运动类知识，靠例子反复训练大脑自动找规律 | 要多给**不同上下文**的例句，而不是同一句重复 |
| 以句子为单位，不是单词 | 输出的是完整例句；单词只作为「必须在例句中出现」的约束 |
| 输入是「声音/文字」直接关联「意思」，中间不能夹图片或中文 | `scene` 描述的是**什么情况下会说这句话**，不是这句话的字面意思 |
| 不要思考、不思考中文 | 例句要短到能一口气处理完；不给语法讲解 |
| 输出始终一致（教宠物狗） | 同一句话每次对应同一个意思，不做「多种解释」的歧义句 |
| 中文解释每次都要不同（逆向应用） | 每条例句给 2~3 种**彼此不同**的中文说法，界面每次随机取一种 |
| 造句：抓住主谓宾 / 主系表两个基本句型 | `swaps` 只做主语、谓语、宾语三个位置的替换 |

最关键的一条是 `scene` 的定义。如果场景描述写成「任何人都能欣赏我们的音乐」，
那么口语卡就退化成中译英 —— 学习者在做翻译，而不是在从场景直接到表达。
所以提示词里明确要求：**场景是「什么情况下会说这句话」，不是「这句话是什么意思」**。
"""

from __future__ import annotations

SYSTEM = """You are the material generator inside a language-drilling app that \
follows a specific learning theory. Read the rules carefully; they are unusual \
and you must follow them exactly.

THEORY (why these rules exist)
- Language is a MOTOR skill, not a thinking skill. The learner must build \
automatic input→output links by meeting many DIFFERENT examples, never by \
analysing grammar. So: never explain grammar, never give rules.
- The unit of learning is a SENTENCE, not a word.
- "Scene" means THE SITUATION IN WHICH SOMEONE WOULD SAY THIS SENTENCE — \
not a translation of the sentence. This is critical: the app shows the scene \
as a prompt and asks the learner to produce the English. If your scene is just \
a literal translation, the exercise degrades into translation practice and the \
whole method fails.

HARD RULES
1. Output ONE JSON object. No markdown, no commentary, no code fences.
2. The "situation" fields (scene_en, scene_zh) must NOT contain the target \
sentence or a word-for-word translation of it.
3. scene_zh must be natural spoken Chinese describing the situation. It may \
hint at the meaning but must never be a literal mirror of the English.
4. zh_variants: 2-3 DIFFERENT Chinese renderings of the sentence's meaning, \
deliberately NOT consistent with each other — one very short and colloquial, \
one fuller, one from a different angle. The app shows a different one each \
review so the learner cannot lock onto a single Chinese equivalent.
5. Sentences must be SHORT (mostly 4-12 words) — the learner has to process \
them without stopping to think.
6. Every sentence must be something a real person would actually say. \
No textbook filler, no tongue twisters.
7. Do not repeat the same sentence pattern with only the noun swapped; \
vary the situation, the register, and the sentence structure.
8. All English must be correct and idiomatic. British or American spelling, \
but be consistent."""


def _schema(examples: int, zh_variants: int) -> str:
    return f"""
Return exactly this JSON shape:

{{
  "kind": "word | phrase | sentence",
  "ipa": "IPA for the target if it is a single word, else empty string",
  "gloss": "A short definition written in SIMPLE English (CEFR A2-B1 vocabulary). \
Explain the meaning the way a dictionary for learners would. Never use Chinese here, \
and avoid using the target word inside its own definition.",
  "note": "One short English sentence about when this is used, or empty string.",
  "examples": [
    {{
      "sentence": "the English sentence",
      "scene_en": "English description of the SITUATION in which this sentence is said (1 short sentence)",
      "scene_zh": "中文描述「什么情况下会说这句话」，口语化，1 句，不要逐字对译",
      "zh_variants": ["{zh_variants} 种彼此不同的中文说法，短的极短，长的换个角度"],
      "register": "one of: 日常口语 / 正式 / 书面 / 俚语"
    }}
  ],
  "swaps": [
    {{
      "role": "subject | verb | object",
      "original": "the part of the sentence being replaced",
      "candidates": ["3 replacement words or phrases that fit grammatically"],
      "samples": ["2 full sentences built by making that replacement"]
    }}
  ]
}}

Provide exactly {examples} examples. Provide one swaps entry for each role that \
actually exists in the examples (subject, then verb, then object). \
If the target is a full sentence rather than a word or phrase, still provide \
{examples} examples — vary them by shifting the situation, the person, or the tense \
while keeping the target structure recognisable."""


def _target_instruction(target: str, kind: str) -> str:
    if kind == "word":
        return (
            f'TARGET WORD: "{target}"\n'
            "Every example sentence must contain this exact word. "
            "Cover several DIFFERENT senses or uses of it if it has more than one — "
            "a word does not have one meaning, and the learner needs the range."
        )
    if kind == "phrase":
        return (
            f'TARGET PHRASE: "{target}"\n'
            "Every example sentence must contain this exact phrase, unchanged. "
            "Vary the surrounding situation, not the phrase itself."
        )
    return (
        f'TARGET SENTENCE: "{target}"\n'
        "Keep this sentence as the reference form and build examples that are close "
        "variations of it: change the situation, the subject, or the tense, but keep "
        "the same structural pattern so the learner absorbs that pattern. "
        "Also include the original sentence itself as one of the examples."
    )


def build_messages(
    target: str,
    *,
    kind: str = "word",
    language: str = "en",
    examples: int = 8,
    zh_variants: int = 3,
) -> list[dict]:
    """拼出给模型的 messages。

    目标语言目前只支持英语（第一个版本的范围），但 ``language`` 参数
    已经预留：加一种语言只需要在这里加一个分支和一条指令，
    不需要动流水线、数据库或界面。
    """
    if language != "en":
        raise NotImplementedError(f"暂时只支持英语，收到 {language!r}")

    user = (
        f"{_target_instruction(target, kind)}\n\n"
        f"{_schema(examples, zh_variants)}\n\n"
        "Remember: scene, not translation. Short sentences. Real speech. "
        "Strict JSON only."
    )
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ]


# 以这些词开头的，基本可以断定是句子而不是短语 —— 它们是主语位置的代词或虚位主语。
_SENTENCE_STARTERS = {
    "i", "you", "he", "she", "it", "we", "they",
    "this", "that", "these", "those", "there", "who", "what",
    "someone", "somebody", "anyone", "anybody", "everyone", "nobody",
    "let's", "dont", "don't",
}


def classify_target(target: str) -> str:
    """猜一下用户输入的是词、短语还是句子。

    **猜错不致命** —— 模型返回时会给出它自己的 ``kind``，程序以那个为准
    （见 pipeline.generate）。这里只是让提示词一开始别偏太远。

    为什么不用「词数 >= 4 就算句子」：``in the long run`` 和
    ``get along with someone`` 都是 4 个词的**短语**，
    一刀切会把它们当成句子，于是提示词会让模型去「保持句型结构」——
    方向一开始就错了。
    """
    text = target.strip()
    if not text:
        return "word"

    if text[-1:] in ".!?":
        return "sentence"

    words = text.split()
    if len(words) == 1:
        return "word"

    first = words[0].strip("'\"").lower()
    if first in _SENTENCE_STARTERS:
        return "sentence"

    # 6 个词以上、又不像固定搭配的，基本就是句子了。
    if len(words) >= 6:
        return "sentence"

    return "phrase"
