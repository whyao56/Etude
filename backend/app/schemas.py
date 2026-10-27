"""请求体模型。

全部集中在一处，理由很实际：这些类就是**接口契约**，散在九个路由文件里
以后没人能一眼看出「前端到底能传什么」。字段注释里记着踩过的坑
（``register``/``copy`` 会盖住 pydantic 自带方法之类），
所以注释跟着字段走，不另开文档。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LessonIn(BaseModel):
    target: str = Field(min_length=1, max_length=300)
    kind: str | None = None
    language: str = "en"
    plan_id: int = 0


class GradeIn(BaseModel):
    grade: int = Field(ge=0, le=3)
    seconds: float = Field(default=0.0, ge=0.0, le=3600.0)


class CardSrsIn(BaseModel):
    """老手直接改一张卡的复习参数。只改传进来的字段。"""

    interval: float | None = Field(default=None, ge=0.0, le=3650.0)
    ease: float | None = Field(default=None, ge=1.3, le=3.5)
    due_at: str | None = None
    suspended: bool | None = None
    # 打回「新卡」：Anki 的 Forgot / Reschedule 里那个「当作没学过」。
    # 单独一个开关而不是 interval=0 —— 后者只改到期时间，
    # 卡片仍然算「已引入」，会占着每天的复习配额。
    introduced: bool | None = None


class CardBatchIn(BaseModel):
    """批量改卡。一次请求，不是 N 次 —— 200 张卡逐张发请求会卡住界面。"""

    ids: list[int] = Field(min_length=1, max_length=500)
    patch: CardSrsIn


class CardPushIn(BaseModel):
    """把一批卡一起往后推 N 天。"""

    ids: list[int] = Field(min_length=1, max_length=500)
    days: float = Field(ge=0.0, le=3650.0)


class ExampleIn(BaseModel):
    """改一条例句。字段全部可选 —— 只传改了的那几个。

    ``register`` 对外照旧叫 ``register``，Python 属性名换成 ``register_text``：
    叫 ``register`` 会盖住 ``BaseModel`` 上的同名方法，pydantic 每次校验
    都会为此发一行警告。0.0.2 的 ``DataLocationIn.copy`` 是同一个坑，
    这里用同一种办法处理 —— 接口形状不变，只换内部名字。
    取数据时用 ``by_alias=True`` 换回 ``register``，正好对上数据库字段名。
    """

    model_config = ConfigDict(populate_by_name=True)

    sentence: str | None = Field(default=None, max_length=600)
    scene_en: str | None = Field(default=None, max_length=2000)
    scene_zh: str | None = Field(default=None, max_length=2000)
    register_text: str | None = Field(default=None, alias="register", max_length=120)
    zh_variants: list[str] | None = None
    scene_image: str | None = Field(default=None, max_length=120)


class NewExampleIn(BaseModel):
    """手工加一条例句。"""

    model_config = ConfigDict(populate_by_name=True)

    sentence: str = Field(min_length=1, max_length=600)
    scene_en: str = Field(default="", max_length=2000)
    scene_zh: str = Field(default="", max_length=2000)
    register_text: str = Field(default="", alias="register", max_length=120)
    zh_variants: list[str] = Field(default_factory=list)
    # 加完顺手把语音（和配图）也生成了。默认开 —— 手工加的句子同样要能听。
    make_media: bool = True


class ConfigIn(BaseModel):
    llm: dict | None = None
    tts: dict | None = None
    images: dict | None = None
    study: dict | None = None
    channels: dict | None = None
    server: dict | None = None
    # 0.1.0：发音训练（开关、模式、转写服务）。深合并由 config.save 处理，
    # 所以只传 {"pronounce": {"asr": {"api_key": "..."}}} 不会把别的字段冲掉。
    pronounce: dict | None = None


class PresetIn(BaseModel):
    preset: str


class BackupIn(BaseModel):
    name: str = ""


class PlanIn(BaseModel):
    language: str = "en"
    goal_kind: str = "fluency"
    goal_label: str = ""
    goal_detail: str = Field(default="", max_length=2000)
    domains: list[str] = Field(default_factory=list)
    target_count: int = Field(default=20, ge=1, le=200)


class OutlineIn(BaseModel):
    outline: list[dict] = Field(default_factory=list)


class DataLocationIn(BaseModel):
    """改数据目录的请求体。

    字段对外叫 ``copy``，但 Python 属性名必须叫别的 —— ``copy`` 会盖住
    ``BaseModel.copy``，pydantic 每次校验都会为此发一条警告。
    接口形状没变，只是内部属性换了个名字。
    """

    model_config = ConfigDict(populate_by_name=True)

    path: str = ""
    copy_files: bool = Field(default=True, alias="copy")


class BatchIn(BaseModel):
    """批量建包：一次丢一堆目标进来。

    两种输入方式都收：``text``（粘贴一整列，一行一个，或者用逗号隔开）和
    ``targets``（界面上逐个输入框拼出来的数组）。粘贴是主用法 ——
    用户手里往往就是一张从别处抄来的单子。
    """

    text: str = Field(default="", max_length=8000)
    targets: list[str] = Field(default_factory=list, max_length=80)
    language: str = "en"
    # 每个目标生成几条例句。0 = 用设置里的默认值。
    examples: int = Field(default=0, ge=0, le=24)
    # 已经开始生成的目标要不要跳过（默认跳过，不然重复点击会开一堆一样的包）。
    skip_existing: bool = True


class ThemeIn(BaseModel):
    """按主题先出一批目标（只出清单，不建包 —— 让人先看再动手）。"""

    theme: str = Field(min_length=1, max_length=200)
    count: int = Field(default=12, ge=1, le=60)
    language: str = "en"
