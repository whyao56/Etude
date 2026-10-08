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

    目前只有英语是**真的支持**的。别的语言在目录里列出来了（见 LANGUAGES），
    但会明确报「还没开放」而不是默默生成一堆质量没验证过的素材 ——
    「能跑但很差」比「暂时没有」伤害更大，因为用户会把学习效果不好
    归因到方法上。
    """
    profile = language_profile(language)
    if not profile["supported"]:
        raise NotImplementedError(
            f"{profile['label']}（{profile['native']}）还没开放。{profile['note']}"
        )

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


# ------------------------------------------------------------ 语言目录
#
# 用户要求「先提前做好其他主流语言的选项」。这里的做法是：
# **把语言列出来，但只有真的验证过的才标 supported**。
#
# 为什么不做成「填个语言名就生成」：例句生成的质量全靠提示词里那些
# 针对该语言的约束（语域怎么分、有没有 IPA、惯用语怎么用）。
# 拿英语的约束去生成日语，出来的东西能看，但用起来是错的 ——
# 而学习工具里「错得看不出来」是最坏的一种失败。

LANGUAGES: list[dict] = [
    {"id": "en", "label": "英语", "native": "English", "supported": True,
     "note": "已开放。音色、英英释义、IPA、语域都已验证。"},
    {"id": "ja", "label": "日语", "native": "日本語", "supported": False,
     "note": "计划中：需要单独的音色表、假名读音，以及「敬体/简体」这一层语域。"},
    {"id": "ko", "label": "韩语", "native": "한국어", "supported": False,
     "note": "计划中：需要处理「尊敬阶」对句子形态的影响。"},
    {"id": "fr", "label": "法语", "native": "Français", "supported": False,
     "note": "计划中：需要处理联诵和省音，这两个不处理的话听力素材是错的。"},
    {"id": "de", "label": "德语", "native": "Deutsch", "supported": False,
     "note": "计划中：需要处理名词性别和框型结构。"},
    {"id": "es", "label": "西班牙语", "native": "Español", "supported": False,
     "note": "计划中：需要处理 voseo 等地区差异。"},
    {"id": "ru", "label": "俄语", "native": "Русский", "supported": False,
     "note": "计划中：需要处理格变化。"},
    {"id": "it", "label": "意大利语", "native": "Italiano", "supported": False,
     "note": "计划中。"},
    {"id": "pt", "label": "葡萄牙语", "native": "Português", "supported": False,
     "note": "计划中：需要区分巴西和欧洲两种。"},
    {"id": "ar", "label": "阿拉伯语", "native": "العربية", "supported": False,
     "note": "计划中：需要处理从右向左的排版和方言差异。"},
]

_LANG_INDEX = {item["id"]: item for item in LANGUAGES}


def language_profile(code: str) -> dict:
    return _LANG_INDEX.get(code) or {
        "id": code, "label": code, "native": code, "supported": False,
        "note": "不认识的语言代码。",
    }


# ------------------------------------------------------------ 学习目标
#
# 这一节是 0.0.2 的重点，也是用户提得最清楚的一点：
# **目标不是难度标签。** 填「六级」的真正含义是「我要过这场考试」，
# 而「学会一门语言」是另一件事 —— 后者要的是听说读写认五条腿都站起来。
#
# 所以数据结构上这两件事是分开的：
#   goal_kind   —— fluency（真正学会）/ exam（应付考试）/ custom（自己写）
#   goal_detail —— 对 fluency 是「想拿它干什么」，对 exam 才是范围参考
#
# 提示词里会明确告诉模型：**不要把大纲做成一张考纲词表**。

GOAL_KINDS: list[dict] = [
    {
        "id": "fluency",
        "label": "真正学会这门语言",
        "what": "目标是听说读写认五条腿都站起来：能在真实情境里听懂、说出、读懂、写出，"
                "而不是认识一批词。大纲会覆盖高频功能词、固定说法和情境，不只是生词。",
    },
    {
        "id": "exam",
        "label": "应付一场考试",
        "what": "考试是范围参考，不是终点。大纲会照顾该考试的高频考点，"
                "但仍然按「句子」和「情境」组织 —— 只是难度和话题向考纲靠。",
    },
    {
        "id": "custom",
        "label": "我自己描述",
        "what": "直接把你要拿这门语言做的事写下来，越具体越好。",
    },
]

# 考试的参考范围。注意 label 里刻意带上 CEFR ——
# 「六级」对模型来说是个模糊的词，B1–B2 才是它能用的东西。
EXAM_PRESETS: list[dict] = [
    {"id": "zhongkao", "label": "中考", "band": "A1–A2",
     "detail": "中国初中毕业水平；日常话题为主，句短、词高频。"},
    {"id": "gaokao", "label": "高考", "band": "A2–B1",
     "detail": "中国高中毕业水平；话题开始扩到社会、文化、科普。"},
    {"id": "cet4", "label": "大学四级", "band": "B1",
     "detail": "CET-4；校园与一般社会话题，阅读和听力的高频表达。"},
    {"id": "cet6", "label": "大学六级", "band": "B1–B2",
     "detail": "CET-6；话题更抽象，长句和书面语增多。"},
    {"id": "kaoyan", "label": "考研英语", "band": "B2",
     "detail": "考研英语一/二；长难句、学术类与评论类文章。"},
    {"id": "ielts", "label": "雅思", "band": "B2–C1",
     "detail": "IELTS；学术与生活两类场景，口语部分强调真实对话。"},
    {"id": "toefl", "label": "托福", "band": "B2–C1",
     "detail": "TOEFL iBT；北美校园学术场景为主。"},
]

DOMAIN_PRESETS: list[dict] = [
    {"id": "日常", "label": "日常起居"},
    {"id": "社交", "label": "社交与人际"},
    {"id": "职场", "label": "职场与商务"},
    {"id": "学术", "label": "学术与写作"},
    {"id": "旅行", "label": "旅行与出行"},
    {"id": "购物", "label": "购物与金钱"},
    {"id": "健康", "label": "身体与情绪"},
    {"id": "科技", "label": "科技与网络"},
    {"id": "新闻", "label": "新闻与观点"},
    {"id": "文化", "label": "文化与娱乐"},
]

SYLLABUS_SYSTEM = """You design the SYLLABUS for a language-drilling app that \
follows a specific learning theory. You are choosing WHICH THINGS THE LEARNER \
WILL DRILL, not writing the drills themselves.

THEORY
- Language is a MOTOR skill built from many DIFFERENT examples, never from rules.
- The unit of learning is a SENTENCE, not a word.
- Learners must be able to hear it, say it, read it, and write it. So the
  syllabus must not be a list of nouns. It must include:
    * high-frequency function words and connectors (the skeleton of every sentence)
    * fixed expressions and chunks real speakers use as one unit
    * whole sentence patterns that recur across situations
    * concrete topic vocabulary
  A list of "important words" is a FAILED syllabus: the learner ends up able to
  label things and unable to say anything.

HARD RULES
1. Output ONE JSON object. No markdown, no commentary, no code fences.
2. Exactly the number of items requested, no more, no fewer.
3. Every item is something that can be drilled as a sentence: a single word,
   a fixed phrase, or a short sentence pattern. Never a bare grammar rule
   (bad: "present perfect tense"; good: "I've just ...").
4. Do NOT repeat the same word twice, and do not list obvious synonyms of each
   other (big / large / huge is ONE entry, not three).
5. Order the items from what a beginner meets first to what comes later.
   Early items must be usable immediately.
6. "why" is ONE short sentence in Chinese explaining what this unlocks for the
   learner. It must be concrete ("点单、问路都能用上"), never a restatement of
   the item itself and never a translation of it.
7. Distribute across the requested domains. If no domains were given, use your
   own judgement and cover everyday life first.
8. If the learner's goal is passing an exam, still follow all the rules above —
   let the RANGE lean toward the exam, but keep the shape (functions, chunks,
   patterns, then vocabulary). Never produce an exam word list.
"""


def _syllabus_schema(count: int) -> str:
    return f"""
Return exactly this JSON shape:

{{
  "note": "One short sentence in Chinese telling the learner what this plan is \
shaped like and how to use it. Never promise a result.",
  "outline": [
    {{
      "target": "the word, fixed phrase, or short pattern to drill",
      "kind": "word | phrase | sentence",
      "domain": "which requested domain this belongs to",
      "band": "CEFR level: A1 | A2 | B1 | B2 | C1",
      "why": "一句中文：学会它之后，具体能多做一件什么事"
    }}
  ]
}}

Provide exactly {count} items in "outline".
Mix kinds deliberately: roughly half "word", a quarter "phrase", a quarter
"sentence". A syllabus that is 95% words is a failed syllabus."""


def build_syllabus_messages(
    *,
    language: str = "en",
    goal_kind: str = "fluency",
    goal_label: str = "",
    goal_detail: str = "",
    domains: list[str] | None = None,
    count: int = 20,
) -> list[dict]:
    """拼出「生成学习大纲」的提示词。

    这里最重要的一句话是：**目标不是难度标签**。用户填「六级」的意思
    是「我要过这场考试」，而不是「请给我一张六级词表」。所以提示词里
    既要把范围收窄到该考试的区间，又要把形状保持成
    「功能词 → 固定说法 → 句型 → 话题词」，并且明确禁止输出词表。
    """
    profile = language_profile(language)
    if not profile["supported"]:
        raise NotImplementedError(
            f"{profile['label']}（{profile['native']}）还没开放。{profile['note']}"
        )

    domains = [d for d in (domains or []) if d]
    if goal_kind == "exam":
        goal = (
            f"The learner is preparing for {goal_label or 'an exam'} "
            f"(roughly CEFR {goal_detail or 'B1'}). "
            "Lean the range toward that exam's topics and difficulty, "
            "but the SHAPE of the syllabus must stay the same."
        )
    elif goal_kind == "custom":
        goal = (
            "The learner described their own goal in Chinese:\n"
            f"  {goal_detail or goal_label}\n"
            "Design for what they actually said they want to do with the language. "
            "Do not silently turn this into a generic word list."
        )
    else:
        goal = (
            "The learner wants to genuinely learn this language — to hear it, "
            "speak it, read it and write it — not to pass a test. "
            "Start from the highest-frequency core and build outward."
        )

    if goal_detail and goal_kind == "exam":
        goal += f"\nContext from the app: {goal_detail}"

    user = (
        f"TARGET LANGUAGE: {profile['native']} ({profile['label']})\n\n"
        f"LEARNER'S GOAL\n{goal}\n\n"
        f"DOMAINS TO COVER\n{', '.join(domains) if domains else '(none specified — choose sensibly, everyday life first)'}\n\n"
        f"{_syllabus_schema(count)}\n\n"
        "Strict JSON only. No code fences."
    )
    return [
        {"role": "system", "content": SYLLABUS_SYSTEM},
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
    # 缩写先还原再比对。``I'd rather stay home`` 的第一个词是 ``i'd``，
    # 直接查表会漏掉，于是这句话被判成短语 —— 提示词接着会让模型
    # 去「保持短语的形态」，方向从一开始就偏了。
    for suffix in ("'ve", "'ll", "'re", "'d", "'m", "'s", "’ve", "’ll", "’re", "’d", "’m", "’s"):
        if first.endswith(suffix):
            first = first[: -len(suffix)]
            break
    if first in _SENTENCE_STARTERS:
        return "sentence"

    # 6 个词以上、又不像固定搭配的，基本就是句子了。
    if len(words) >= 6:
        return "sentence"

    return "phrase"
