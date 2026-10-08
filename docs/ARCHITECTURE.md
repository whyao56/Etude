# 架构

> 这份文档写给**要改这份代码的人**。重点不是「有哪些文件」，而是
> **哪些地方看着能简化、其实一简化就出错** —— 那些地方都配了测试盯着。

---

## 一、进程模型

一个 exe，一个进程，里面跑四样东西：

```
Etude.exe
  │
  ├─ run_etude.py ────────────── 入口：解析参数 → 自检 → 起服务 → 开窗口
  │
  ├─ uvicorn (后台线程) ──────── 监听 127.0.0.1:8977
  │     └─ api.py  FastAPI 应用
  │           ├─ 静态目录  frontend/index.html（界面）
  │           └─ /api/*    其余全部接口
  │
  ├─ ThreadPoolExecutor(3) ──── 生成流水线。**不能放到请求线程里**：
  │                             一次生成要几十秒（8 次模型调用 + 8 次语音合成），
  │                             请求挂那么久，界面上的进度条就成了摆设。
  │
  └─ 窗口线程 (desktop.py) ───── WebView2 内核，加载上面那个地址
```

**为什么是 127.0.0.1 而不是 0.0.0.0**：这是本机单人用的工具，
没有理由监听所有网卡。监听 `0.0.0.0` 会让同一局域网里的任何人都能读到你的
训练数据、并且能通过 `/api/config` 改你的配置。

**为什么窗口是「可选的一层」**：`desktop.run_window()` 返回 `False` 表示
开不了原生窗口（缺 WebView2、或者 pywebview 没装），此时 `run_etude.py`
用 `webbrowser.open()` 退回浏览器，**功能完全一样**。
这条降级路径是刻意的 —— 它让「窗口」这个最容易在别人机器上出问题的组件，
从「能不能用」降级成「好不好看」。

---

## 二、目录地图

```
etude/
├── backend/
│   ├── run_etude.py            入口。参数 / 自检 / 起服务 / 开窗口 / 退出顺序
│   ├── requirements*.txt
│   ├── app/
│   │   ├── __init__.py         ★ 版本号在这里；BLAS 线程限流也在这里
│   │   ├── paths.py            ★ 全项目唯一判断「在不在 exe 里」的地方
│   │   ├── config.py           设置的读写 + 厂商预置 + 密钥脱敏
│   │   ├── prompts.py          ★ 理论 → 提示词的那一层
│   │   ├── providers/
│   │   │   ├── llm.py          OpenAI 兼容协议 + 错误分诊
│   │   │   └── tts.py          Edge TTS / OpenAI TTS + 内容寻址缓存
│   │   ├── pipeline.py         ★ 生成流水线（作业表 / 逐条失败不拖垮整体）
│   │   ├── db.py               SQLite：四张表 + readiness 计算
│   │   ├── srs.py              间隔重复（纯函数）
│   │   ├── api.py              HTTP 接口
│   │   ├── server.py           端口选择 + 优雅停止
│   │   ├── desktop.py          原生窗口，失败返回 False
│   │   └── selfcheck.py        八项自检
│   ├── frontend/index.html     单文件界面（零构建、零 CDN）
│   └── tests/                  113 项测试
├── scripts/
│   ├── build_exe.py            打包编排
│   ├── make_release.py         发版
│   ├── e2e_check.py            对打包版打真实 HTTP
│   ├── ui_smoke.js             真浏览器点一遍
│   ├── screenshots.js          出文档截图
│   ├── openai_stub.py          本地假模型
│   ├── products.py             ★ 打包产物在哪（三个脚本共用的判断）
│   ├── cdp.js                  自写 CDP 客户端
│   └── check_js.py             前端静态守卫
├── build/
│   ├── etude.spec              打包配置（源文件，要提交）
│   └── size-baseline.json      体积基线
└── docs/
```

带 ★ 的五个文件是「改动风险最高」的。下面逐个说为什么。

---

## 三、数据模型

四张表，全部在 `backend/app/db.py` 的 `SCHEMA` 里。用扁平字典出入，
不引 ORM —— 这些数据最终要原样变成 JSON 给前端，字典省掉一次转换；
而且没有编译器的项目里，ORM 的字段名拼错只会在运行时炸。

```
lessons ──┬── examples ──┬── cards ── reviews
          │              │
          └── swaps ─────┘（swaps 挂在 lesson 上，不挂 example）
```

| 表 | 一行是什么 | 关键字段 |
|---|---|---|
| `lessons` | 一个训练包 | `target`（用户输入的那个词/句）、`kind`、`status`、`error` |
| `examples` | 一条例句 | `sentence`、`scene_en`、`scene_zh`、`zh_variants`(JSON)、`audio_path`、`audio_voice` |
| `swaps` | 一组换词候选 | `role`(主/谓/宾)、`original`、`candidates`(JSON)、`samples`(JSON) |
| `cards` | 一张卡 = (例句, 通道) | `due_at`、`interval`、`ease`、`reps`、`lapses`、`introduced` |
| `reviews` | 一次评分 | `card_id`、`grade`、`seconds`、`at` |

### 主键是 `(例句, 通道)`，不是 `(单词)`

```sql
UNIQUE (lesson_id, example_id, channel)
```

这是整个数据模型里唯一一处「看着可以合并、其实不能」的设计。
理由见 [THEORY.md 主张 1](THEORY.md#主张-1--五种通道各建一张卡而不是一张卡多用途)：
看懂 ≠ 听得出 ≠ 说得出。8 条例句 × 5 个通道 = 40 张卡，
每张独立调度、独立评分、独立遗忘曲线。

### `example_id = 0` 是合法值

`cards.example_id` 允许为 0，表示「这张卡作用于整个训练包，不属于任何一条例句」。
0.0.1 里没有产生这种卡的代码路径，但字段和注释保留着 ——
将来的「整包综合复习」会用到，而且加一个值比改主键便宜。

### 时间戳一律 UTC ISO 字符串

`db.now()` 的返回值和 `db.parse_ts()` 的入参是同一个口径。
刻意不用 `sqlite3` 的 `DATETIME` 类型：它没有时区概念，
而「今晚 8 点该复习了」这件事需要跨时区正确。

---

## 四、必须守住的不变量

以下每一条都对应真实踩过的坑，并且都有测试盯着。

### 4.1 「在 exe 里吗」只在 `paths.py` 里判断

```python
# tests/test_core.py
def test_frozen_check_happens_in_exactly_one_place():
    """其它模块一律调 audio_dir() / db_path()，不许自己看 sys.frozen。"""
```

**为什么**：一旦有两处判断，早晚会有一处漏掉，表现是
「源码态好好的，打包后设置存不下去」—— 而这类 bug 打包前测不出来。

### 4.2 `ready` ≠ `complete`

```python
readiness(lesson_id) -> {
    "ready": bool,      # 现在能不能练（有例句就行）
    "complete": bool,   # 素材齐不齐（音频也在）
    "problems": [...],  # 具体缺什么，直接给用户看
    ...
}
```

**为什么不能合成一个布尔值**：

- 合成一个 → 语音挂了，整个包判定为「不可练」→ 读写造句四个通道明明是好的，
  却跟着一起被封掉；
- 分开 → 语音挂了只影响听力卡，其余照练，界面上显示一条提示条
  「有例句但没有声音，点这里补语音」。
- 而且 `readiness` **不看 `status` 字段**，一律从实际内容算。
  `status='ready'` 只表示「生成任务跑完了」，不表示「能用」。
  这两者的差集，就是「看着配好了、用起来没反应」的全部来源。

有**三组**测试专门打这个区分（`tests/test_storage_pipeline.py`）。

### 4.3 音频文件名是内容寻址的

```
<engine>-<sha256(engine|voice|text|rate)[:20]>.mp3
例：edge-3f8a1c9d2b4e5f6a7b8c.mp3
```

生成的相对路径存进数据库。两个后果都是想要的：

1. 同一句话换音色会生成新文件，换回来又命中旧文件 —— **不重复存**；
2. `/api/audio/{name}` 用正则 `^[a-z]+-[0-9a-f]{20}\.mp3$` 校验，
   路径穿越（`..%2F..%2Fconfig.json`）在**进入文件系统之前**就被挡掉。

**存相对路径而不是绝对路径**：绝对路径换个数据目录就是死链，
而且备份还原到另一台机器也全废。

### 4.4 一条失败不能拖垮整体

`pipeline.generate()` 里，每条例句的语音合成都被单独包住：

```python
try:
    rel = tts.synthesize(...)
except TTSError as exc:
    problems.append(...)   # 记录
    continue               # 不中断
```

失败详情写 `logs/lesson-N.log`，同时进 `readiness.problems`。
**音频失败降级为「记录」，不降级为「失败」** —— 8 条句子里有 2 条没合成出来，
用户拿到的是「6 条能听、2 条要补」，而不是「生成失败，请重试」。

### 4.5 前端事件名必须小写

```javascript
// backend/frontend/index.html   el()
else if (key.slice(0, 2) === "on") {
  node.addEventListener(key.slice(2).toLowerCase(), value);
}
```

**这是踩过的最大一个坑**：`addEventListener("Click", ...)` 既不报错也不触发。
界面照常画出来、截图看不出来、Python 测试也测不到 ——
所有按钮都是死的。`scripts/check_js.py` 里有两条守卫盯着它
（一条查字面量、一条查探针本身会不会漏报）。

### 4.6 端口被占用时先问「对面是不是自己」

`server.choose_port()` 的分类处理：

| 对面是谁 | 做法 |
|---|---|
| 空闲 | 直接用 |
| 是 Etude（`/api/health` 应答） | **复用**，不抢端口。前端拿到 `/api/health` 的版本号和 pid，不一致就弹红条 |
| 是别的程序 | 换下一个端口，最多试 20 个 |

**为什么不能无脑换端口**：用户双击两次 exe 是最常见的情况，
无脑换端口会让第二个实例悄悄起在一个随机端口上、开一个新窗口，
数据看着「丢了」（其实是在同一个 sqlite 上，只是两个窗口在互相盖）。

### 4.7 退出顺序

`ask_stop()` 只置停止标志，**不 join**。

**为什么**：从窗口关闭回调里调 `server.join()` 会和 uvicorn 的线程互相等，
直接死锁 —— 表现是「关了窗口，进程还在任务管理器里」。
现在的顺序是：窗口关闭 → 置标志 → 主线程 join(超时 5s)。

---

## 五、HTTP 接口

全部在 `backend/app/api.py`。划分是「页面对应一组」，不是 REST 教科书式分法。

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/health` | **自述**：版本、pid、运行形态、数据目录。旧进程一眼可辨 |
| GET | `/api/selfcheck` | 八项自检结果（界面「设置」页那个按钮） |
| GET | `/api/config` | 读设置。**api_key 永远不在这里面** |
| POST | `/api/config` | 改设置（局部更新） |
| POST | `/api/config/preset` | 切厂商：把 base_url / 模型名一整套换掉 |
| GET | `/api/catalog` | 预置厂商表 + TTS 音色表 + 五通道说明（给界面填选项用） |
| POST | `/api/config/test-llm` | 真连一次模型 |
| POST | `/api/config/test-tts` | 真合成一小段 |
| GET | `/api/lessons` | 训练包列表（带 readiness 摘要） |
| POST | `/api/lessons` | 建包，**立刻返回**，生成在后台 |
| GET | `/api/lessons/{id}` | 详情：例句 + swaps + 卡片 + readiness |
| GET | `/api/lessons/{id}/job` | **轮询用**的进度 |
| POST | `/api/lessons/{id}/regenerate` | 重新生成例句 |
| POST | `/api/lessons/{id}/audio` | 只补音频（不动例句） |
| DELETE | `/api/lessons/{id}` | 删包（级联删例句/卡/记录） |
| GET | `/api/audio/{name}` | 音频。正则校验，带缓存头 |
| GET | `/api/queue` | 队列：到期卡 + 新卡配额 |
| GET | `/api/cards/{id}` | 单张卡（含该通道要显示什么） |
| POST | `/api/cards/{id}/grade` | 评分 → 重排期 |
| GET | `/api/stats` | 统计 |
| POST | `/api/backup` | 备份（`VACUUM INTO`） |
| GET | `/` | 界面 HTML，`no-store` |

### 几个刻意的选择

**`/api/lessons` 立刻返回，生成走后台。** 8 条例句要好几十秒。
同步返回的话，用户点完「生成」看到的是一段没有反馈的空白，然后超时。

**作业表在内存里**（`pipeline._JOBS`），不落库。它只活一次会话；
重启后 `recover_stale()` 会把上次崩在 `generating` 的行标成 `failed`，
让用户可以点重生成，而不是永远卡着。

**界面 HTML 带 `no-store`。** 这是本项目最反直觉的一条缓存策略：
打包后界面文件是从 `_internal/` 读的，改了代码重新打包，
如果浏览器还拿着旧 HTML 就会「代码改了但没生效」。
宁可每次都重读磁盘（几百 KB，无所谓）。

**`/api/config` 不回传密钥。** `config.public_view()` 里明确剔除。
界面上那个输入框永远显示占位符「已保存，留空表示不改动」。
有测试盯着这条（`test_api.py` 里搜 `api_key` 是否出现在响应里）。

---

## 六、间隔重复

`backend/app/srs.py` 是**纯函数**，不碰数据库、不碰时间（`now` 由调用方传）。

```python
schedule(interval, ease, reps, lapses, grade) -> (interval, ease, reps, lapses, delay_minutes)
```

和标准 SM-2 的两处偏离，都是有意的：

**偏离一：「没想起来」用分钟级重来，而不是推到明天。**

```python
RELEARN_MINUTES = 10.0
```

原文没提间隔重复，这是我的判断：一张卡在当次会话里就再也见不到，
和「练三五次形成本能」是矛盾的。10 分钟足够让短时记忆退掉一点，
又不至于当场想起答案。

**偏离二：`delay_minutes` 和 `interval_days` 分为两个返回值。**

「10 分钟后」和「0.007 天后」是同一个时刻，但只有一个能在界面上显示。
分成两个字段，`human_delay()` 才能给出「10 分钟后 / 1.0 天后 / 1.2 个月后」
这种读得懂的话。

**为什么做成纯函数**：这一层是最容易出「方向反了」的地方
（比如「吃力」的间隔比「正常」还长），而它又完全没有副作用，
做成纯函数就能用几百个用例把它钉死，不用起服务、不用建数据库。

---

## 七、前端

`backend/frontend/index.html`，约 1480 行，**单文件、零构建、零 CDN**。

| 决定 | 为什么 |
|---|---|
| 不用任何 CDN | **断网也要能练**。这是本地工具，例句和语音都在本机，没道理打开界面还要等一个 jsdelivr 请求 |
| 不用框架 | 没有编译步骤 = 双击 `index.html` 就能调界面。代价是失去编译期检查，用 `check_js.py` 补 |
| 不用 `innerHTML` 拼字符串 | 例句里有用户输入的目标词，拼字符串就是 XSS 入口。全部走 `el(tag, attrs, children)` 建 DOM |
| hash 路由 | `#/study` `#/lessons` `#/settings`。刷新不会 404，不需要服务端配合 |
| 每包独立轮询 | `pollTimers = new Map()`，键是 lesson_id。共享一个定时器的话，同时生成多个包只有最后一个会被轮询 |

**`el()` 的 attrs 分三类**：`on*` 走 `addEventListener`（**转小写**）、
`className`/`textContent` 直接赋值、其余走 `setAttribute`。

**全局错误兜底**：`window.addEventListener("error"/"unhandledrejection")`
把异常摆到页面顶部的 banner 上。没有这个的话，JS 出错只会让界面
「安静地不响应」，用户完全不知道该反馈什么。

---

## 八、打包

`build/etude.spec` + `scripts/build_exe.py`。

### onedir，不是 onefile

| | onedir | onefile |
|---|---|---|
| 启动 | 直接起 | 每次解压到临时目录，几百 MB 好几十秒 |
| 退出 | 正常 | **退出即删临时目录** —— 用户会以为数据丢了 |
| 杀毒软件 | 少误报 | 自解压行为经常被拦 |

### `console=True`

`--check` 是「双击没反应」的**唯一出口**。关掉控制台，
用户遇到问题就只剩「它不动」这一条信息。

### 显式 `excludes`

```python
excludes = ["numpy", "scipy", "pandas", "matplotlib", "onnxruntime",
            "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
            "av", "soundcard", "PIL", "pymupdf", "tkinter", "PyQt5", ...]
```

**这一段不是可选的。** 开发用的 venv 里装着上个项目的 faster-whisper、
torch 相关的东西，PyInstaller 会顺着 import 图把它们全拉进来 ——
包会从 49 MB 涨到好几百 MB，而且启动变慢。显式切断是唯一可靠的做法。

代价是：如果哪天代码真的 import 了 `PIL`，包会打出来但运行时报
`ModuleNotFoundError`。所以 `build_exe.py` 里有一道**归档核对**：

```python
required = ["fastapi", "starlette", "uvicorn.protocols.http.h11_impl",
            "edge_tts", "httpx", "app.api", "app.pipeline",
            "app.providers.llm", "app.providers.tts", "app.selfcheck"]
```

用 PyInstaller 自己的归档读取器查（不用 `grep` 二进制 —— 那会有一堆假阳性），
少一个就**不产出 dist**。这道检查是唯一能防住「`excludes` 和代码打架」的东西。

### 体积报警

`build/size-baseline.json` 存着上次的体积。超过 260 MB 报警（不失败）。
**只报警不失败**的理由：体积增长有时候是合理的（加了功能），
有时候是失误（漏了 excludes）。报警让人去看一眼，失败则会把合理的增长也拦下。

### 输出目录：为什么不覆盖上一个

PyInstaller 的 COLLECT 阶段会先把已存在的输出目录**整个删掉**再重建
（`PyInstaller/building/api.py`：`_rmtree(self.name)`）。

本机有一层批量删除保护：一次删除超过 50 个文件的目录需要人工确认，
而这个确认传不进脚本里。后果是**第二次打包必定失败**，
且报出来的只是一句很含糊的「PyInstaller 失败了」—— 排查成本远高于修它。

所以 `scripts/products.py` 的做法是：**换一个空位写新产物，一个文件都不删。**

```
dist/Etude              第一次
dist/build2/Etude       第二次
dist/build3/Etude       第三次
```

`build_exe.py` / `make_release.py` / `e2e_check.py` 都通过 `products.newest_output()`
去找**最近一次**的产物 —— 按 `Etude.exe` 的修改时间排序，不按目录名
（`build2` / `build10` 按字符串比大小会得到错的顺序）。

要清理就自己删 `dist/` 下的旧目录。**脚本永远不会替你删。**

附带的好处：上一份产物还在，体积对比、行为对比、回滚都不用重新打包。

---

## 九、测试与守卫的分工

| 手段 | 覆盖什么 | 为什么不能用别的手段 |
|---|---|---|
| `pytest`（113 项） | 逻辑、边界、错误方向 | —— |
| `check_js.py` | 前端调用点、事件名大小写 | 零构建项目没有编译器，这是补偿 |
| `ui_smoke.js` | 真浏览器里点一遍 + 收集控制台错误 | Python 测试测不到「点了没反应」 |
| `e2e_check.py` | 对**打包版**打真实 HTTP | 源码态跑通 ≠ 打包后跑通 |
| `build_exe.py` 归档核对 | 打包后模块到底在不在 | 只看 exit code 会漏掉「打出来了但是空的」 |
| 冻结态 `--check` | 打包后自检 | 同上 |

**守卫式的测试要能证明自己会失败。** `check_js.py` 里有一组
`_SELF_TEST_SAMPLES`，每次运行前先验探针本身；`test_guards.py` 里也有
反证用例（比如 `test_event_name_guard_would_catch_the_old_writing` 故意喂
一个 `onClick` 进去，断言守卫会报）。

**为什么**：探针坏了会伪装成被测对象坏了。这个项目上真发生过一次 ——
`check_js.py` 的清洗器被一个正则字面量里的单引号骗了，
把一大段代码当成字符串吃掉，然后报了 8 个「你的函数未定义」的假警报。
没有探针自检的话，那 8 个警报会把人引到完全错误的方向。

---

## 十、如果要改……

| 想做的事 | 动哪里 | 别忘 |
|---|---|---|
| 加一门目标语言 | `prompts.py` 加分支（`if language != "en"`）、`tts.py` 的音色表 | 数据模型不用动，见 [THEORY.md 第四节](THEORY.md#四加一门语言的成本) |
| 加一个通道 | `db.CHANNELS`、`frontend` 的 `VIEWS`/渲染分支、`config` 的默认值 | 老数据的 `cards` 表不会有新通道的行，得有个补卡迁移 |
| 加一个模型厂商 | `providers/llm.py` 的 `PRESETS`（一处） | 如果是非 OpenAI 兼容协议，那是另一件事 |
| 改提示词 | `prompts.py` 的 `SYSTEM` | 跑一遍 `test_prompts_forbid_translation_shaped_scenes` |
| 改前端 | `frontend/index.html` | 跑 `check_js.py` + `ui_smoke.js` |
| 改打包 | `build/etude.spec` | 重新跑 `build_exe.py`（归档核对会告诉你有没有切坏） |
