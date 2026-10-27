# Etude

**句子五通道语言训练器。** 给一个词、一句固定说法或一整句话，它生成 8 条不同场景的例句，
每条例句铺成 5 张卡 —— 阅读、听力、口语、打字、造句 —— 然后按间隔重复排给你练。

0.1.0 起还练**发音**：对着麦克风念一遍，当场给出分数与四项指标（内容、时长、停顿、音量）。

界面是一个本机跑的小程序：双击 `Etude.exe`，浏览器窗口里就是它。
模型、语音、配图都走你自己的 API，数据全部留在本机。

---

## 三十秒版本

1. 下载 `Etude-0.1.0-win64.zip`，解压到一个**不含中文的路径**（例如 `D:\Etude`）。
2. 双击 `Etude.exe`。第一次会要你填大模型（DeepSeek、豆包、通义、智谱、Kimi、OpenAI、
   Ollama 里挑一个，或者填任意 OpenAI 兼容地址）。
3. 「训练包」页输入一个词 → 等 30 秒左右 → 「训练」页开始练。
4. 不认识的东西先跑 `Etude.exe --check`（在那台机器上双击没反应时，这是唯一的出口）。

---

## 它和「摘卡器」不一样

市面上多数工具是「把 Anki 变成自动化」：给词、出卡、塞进 Anki。
Etude 赌的是另一件事 —— **语言不是知识，是运动**：

| | 摘卡器 | Etude |
|---|---|---|
| 单位 | 词 + 释义 | **句子**（4–12 个词，真人会说的那种） |
| 一个词给几条例句 | 1 条 | **8 条，场景各不相同** |
| 卡面 | 正面给中文释义 | **正面给场景，不给中文** |
| 中文 | 一句固定翻译 | 每次随机从 2~3 种说法里取一种 |
| 通道 | 一条（看正面想背面） | **五条，各练各的** |
| 依赖 | Anki + AnkiConnect | 无（自己带训练器） |

五条通道的意义在于「输入」和「输出」是两件事：

| 通道 | 看到什么 | 要求做什么 |
|---|---|---|
| 阅读 | 英文句子 | 让场景自己浮出来，不出声、不想中文 |
| 听力 | 只有声音 | 直接进入场景，不经过图片或中文 |
| 口语 | 场景（中文描述 + 配图） | 真的出声说出来，说完再看原文 |
| 打字 | 场景 | 把英文打出来，打完逐词比对 |
| 造句 | 句子 + 可替换成分 | 换掉一个成分造新句，念得出来才算过 |

0.1.0 的「口语」通道多了跟读评分：念完立刻给分，能看出是「漏了词」
还是「念得太短」还是「声音太小」。

---

## 0.1.0 有什么

**新的**

- **发音训练（跟读）。** 小声念完就能看到分数 + 四项指标。不配转写服务也能用：
  它至少测得出「说完了没有」「有没有停顿」「音量够不够」。配了 OpenAI 兼容的
  `/audio/transcriptions`（whisper-1 等）之后，还会逐词告诉你漏了哪个词。
- **批量建包。** 训练包页里一次贴一整串词（一行一个、逗号分隔都行），
  或者给一个主题让模型起草清单（「厨房里会用到的话」→ 12 条目标）再勾选。
- **地址栏跟得上内容。** `#/lessons/12`、`#/plans/3` 刷新还在原页、能后退。
- **界面整个重做。** 原生 ES 模块（零构建、零依赖），四套配色（跟随系统 / 暖纸 /
  冷白 / 深色），三档字号，评分条粘在视口底部（翻面后不会把评分键挤出屏幕）。

**修掉的**

- 0.0.3 里「大模型语音」下语速/音量静默失效 —— 现在音色只有一处来源。
- 训练包详情页刷新回列表 —— 现在有真路由。
- 卡片浏览器里「点『正常』没反应」那一类：提示条不再盖住按钮。

**没变的**

- 数据还是全在本机 `%LOCALAPPDATA%\Etude`，换目录、备份、`VACUUM INTO` 都还在。
- SRS 还是那套 SM-2 变体（见 `app/srs.py`，纯函数，没有隐藏状态）。

---

## 五个页

| 页 | 干什么 |
|---|---|
| 训练 | 一次一张卡。空格翻面、1–4 打分、← → 换卡、Z 撤销上一次评分 |
| 训练包 | 建包（单个 / 批量 / 主题起草）、看完整度、逐条例句编辑、补语音补配图 |
| 目标 | 说清「要干什么」，模型排出该学什么（大纲可勾选后再生成） |
| 卡片 | 所有卡。按状态/通道/关键词筛，批量暂停、推后、当作没学过 |
| 设置 | 模型、语音、配图、发音、学习量、自检、数据位置 |

侧栏左下角永远是 `v版本 · 形态`，顶部横幅只在真的出事时出现。

---

## 配置

设置页里点点就行，也可以直接改 `%LOCALAPPDATA%\Etude\config.json`。
密钥只存在本机，接口永远只回 `api_key_set: true` —— 不把密钥回传给界面。

| 段 | 关键项 |
|---|---|
| `llm` | `base_url`（要以 `/v1` 结尾）、`model`、`api_key`、`temperature`、`timeout` |
| `tts` | `engine`（`edge` / `openai`）、`voice`、`rate`、`volume`、`openai.*` |
| `images` | `source`（`both` / `search` / `llm` / `off`）、`max_per_lesson`、`llm.*` |
| `pronounce` | `enabled`、`mode`（`auto` / `acoustic` / `mixed`）、`save_recordings`、`asr.*` |
| `study` | `examples_per_lesson`、`new_per_day`、`zh_variant_policy` |
| `channels` | 五个通道各开不开 |

环境变量覆盖（优先级高于配置文件）：`ETUDE_DATA_DIR`、`ETUDE_LLM_BASE_URL`、
`ETUDE_LLM_API_KEY`、`ETUDE_LLM_MODEL`、`ETUDE_TTS_ENGINE`、`ETUDE_TTS_VOICE`、
`ETUDE_IMAGE_SOURCE`、`ETUDE_ASR_BASE_URL`、`ETUDE_ASR_API_KEY`、`ETUDE_ASR_MODEL`。

---

## 数据在哪

```
%LOCALAPPDATA%\Etude\
  etude.sqlite3      卡片、例句、复习记录、跟读尝试（一个 SQLite 文件）
  config.json        设置（含密钥，只在本机）
  audio\             模型念的 mp3（内容寻址，可随时重新生成）
  recordings\        你自己念的 wav（只有跟读才会写）
  images\            场景图（内容寻址）
  logs\              生成 / 自检日志
  backups\           手动备份
```

- **换机器**：设置页「数据位置」里填新目录，选「复制文件」，重启即可。
  也可以直接设 `ETUDE_DATA_DIR`（此时界面里的按钮会变成只读说明）。
- **备份**：设置页一键备份，走的是 `VACUUM INTO`，不是 cp —— 复制正在写的
  SQLite 文件会得到一个看起来正常、其实缺最后一页的库。
- **录音**：默认保存（在设置里能关）。它是唯一「模型生成不出来」的东西，
  所以和 `audio\` 分开放，清理语音缓存时不会把它一起删掉。

---

## 命令行

```
Etude.exe                # 正常启动：起服务 + 开窗口（起不来就退回浏览器）
Etude.exe --check        # 自检，结果打印出来并写 logs\selfcheck.txt
Etude.exe --check-deep   # 自检 + 真的调一次模型 / 合成一句语音 / 转写一次
Etude.exe --port 8978    # 换端口（默认 8977；被占会自动往上找）
Etude.exe --no-window    # 只起服务，不开窗口
Etude.exe --data-dir D:\EtudeData
Etude.exe --version
```

`--check` 在双击没反应时最有用：它是唯一不依赖窗口的出口。
窗口起不来（没有 WebView2）时，程序会自动退回系统浏览器，功能一样。

---

## 开发

```
Etude/
  backend/
    app/
      api.py          FastAPI 装配（含线程池，最多 3 个并发生成）
      routes/         路由：meta config lessons batch plans cards pronounce data media
      store/          存储：schema lessons cards plans readiness attempts（schema 里管连接与迁移）
      providers/      llm.py（8 家预置 + JSON 抽取）tts.py（Edge / OpenAI）images.py asr.py
      pipeline.py     生成作业：例句 → 语音 → 配图 → 完工；还有大纲
      pronounce.py    跟读评分引擎（WAV 解析、停顿检测、LCS 词对齐）
      prompts.py      三条提示词：例句、大纲、主题起草
      srs.py          SM-2 变体（纯函数）
      selfcheck.py    十一项自检
      paths.py        数据目录 / 资源目录（全项目唯一判断 frozen 的地方）
    frontend/         原生 ES 模块 + 分包 CSS，零构建
      js/core/        dom api store router audio wav recorder ui
      js/views/       study lessons lesson plans cards settings pronounce
      css/            tokens base layout components views
    tests/            316 个用例
  scripts/            打包、发版、e2e、截图、冒烟、假模型、守卫
  build/etude.spec    PyInstaller（onedir）
  docs/               ARCHITECTURE / THEORY / DEVLOG / STATUS / QUICKSTART / releases
```

常用命令（在仓库根目录）：

```bash
# 测试（316 个）
cd backend && python -m pytest tests/ -q

# 前端守卫：导入名、图标名、类名、版本号四项（0.1.0 新增）
python scripts/check_frontend.py

# 真浏览器冒烟：真鼠标点、真键盘按，41 项
node scripts/ui_smoke.js --base http://127.0.0.1:8977

# 出文档截图（自己起服务 + 全新临时数据目录，16 张）
node scripts/screenshots.js

# 打包（含前置检查、归档核对、冻结态自检、体积对比）
python scripts/build_exe.py

# 对打包产物做黑盒验收：起进程、查自述、查界面、查越权文件名
python scripts/e2e_check.py

# 发版（打 tag、建 release、传 zip，--check 只打包不发布）
python scripts/make_release.py --check
```

假模型（e2e / 冒烟 / 截图都用它，不出网）：

```bash
python scripts/openai_stub.py --port 18999
# 模型名里带 fault-401 / fault-404 / fault-500 / fault-notjson /
# fault-fenced / fault-messy / fault-slow 可以现场造故障
```

---

## 已知限制

- 只有英语。第二门语言在 `prompts.LANGUAGES` 里是「计划中」，选了会明确报错。
- 只有 Windows 打包。源码在任何有 Python 3.11+ 的机器上都能跑。
- 跟读评分的「内容」项需要你自己配一个 OpenAI 兼容的转写服务；
  不配就只能测时长、停顿、音量（界面会说明这一点，不会假装算过分）。
- 没有同步、没有多人、没有云。
- 界面里没有真人长期使用数据 —— 排期参数是照 SM-2 调的，不是照一万个用户调的。

---

## 路线图

- **0.2.x** 导出 Anki `.apkg`（想让 Anki 用户也能用上这八条例句）
- **0.3.x** 第二门目标语言（提示词与评分都要重新调）
- 也许：听力通道的听写模式、连续跟读对练、按主题自动补全一个包的缺口

---

## 许可

MIT。例句、语音、配图都是你用自己的额度生成的，版权随你。
