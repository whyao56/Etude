# Etude 交接报告：从本地训练器，到 DeepSeek Harness 插件

> 交接时间：2026-10-28
> 交接版本：**0.1.1**（tag `v0.1.1`，release id `426616149`，`main` = `d0b53eb`）
> 接手项目名：**`dsh-etude`**（新建仓库，不是在这个仓库里继续改）
> 写给：接下来要开「创造模式」把它做成 DSH 插件的那个我 / 那个人
> 一句话结论：**训练器这一半是好的，卡片工厂这一半是坏的 —— 交接的重点是把坏的这半切出去，不要在里面继续修。**

## 0.0 项目名与仓库边界（2026-10-28 补记）

- 新项目叫 **`dsh-etude`**：一个 DSH 插件，卡片制作全走 DSH 侧。
- 本仓库（`whyao56/Etude`，0.1.1）**冻结为参考实现**：里面的训练器、数据模型、SRS、
  五通道界面、测试与守卫都当「可搬运的资产」，不在这里继续加功能。
- 建议落点：代码 `F:\dsharness\dsh-etude`，远端 `github.com/whyao56/dsh-etude`（新仓库）。
- 搬运清单见 §13「不要带过去的七个包袱」与 §3 代码地图（照着挑，不要整体 fork 之后再改到面目全非）。


---

## 目录

- [0. 交接边界（先看这条）](#0-交接边界先看这条)- [1. 用户判词与我的认领](#1-用户判词与我的认领)
- [2. 现状：0.1.1 到底有什么](#2-现状011-到底有什么)
- [3. 代码地图](#3-代码地图)
- [4. 数据模型](#4-数据模型)
- [5. HTTP 接口契约](#5-http-接口契约)
- [6. 卡片包格式](#6-卡片包格式)
- [7. 资源管线（配图 / 语音）](#7-资源管线配图--语音)
- [8. 前端架构与视觉规范](#8-前端架构与视觉规范)
- [9. 测试与守卫](#9-测试与守卫)
- [10. 打包与发版](#10-打包与发版)
- [11. 新方向：插件化设计](#11-新方向插件化设计)
- [12. 迁移路线](#12-迁移路线)
- [13. 不要带过去的七个包袱](#13-不要带过去的七个包袱)
- [14. 环境与运维备忘](#14-环境与运维备忘)
- [15. 验收清单](#15-验收清单)

---

## 0. 交接边界（先看这条）

Etude 现在有两个半边，成熟度差得很远：

| 半边 | 内容 | 状态 |
|---|---|---|
| **训练器** | 五通道卡片、SM-2 变体调度、卡片浏览器、进度统计、跟读评分、备份恢复、界面与四套配色 | 好。331 个测试、41 项冒烟、22 项 e2e 都过，界面是可用可看的 |
| **卡片工厂** | 拿一个目标词 → 生成 8 条例句 + 中文说法 + 场景图 + 语音 + 换词 | **坏**。文案能出，但（a）配图不可预览不可挑，（b）语音不可试听，（c）设置里一堆项设置了看不出效果 |

用户的原话是「输出的图片完全就不是人能看的，设置很多东西完全就是无法设置，无法预览」。
这句话是对的，而且**指的就是卡片工厂这一半**。

新方向（也是这份交接要走的方向）：

1. **卡片制作搬进 DeepSeek Harness 插件**，由 DSH 那边的大模型 / 工具链去做，Etude 只当训练器。
2. 配图：**优先从网上找符合情境的实拍图**，可选自己部署开源生图（明确「一般很难做好」，所以默认不开）。
3. 语音：**以 DSH 侧的 TTS 为主，Edge 保底**；以后可以上可训练的语音模型，用不同音色和语气。
4. 卡片 UI 要**特别好看**（这条要有硬标准，见 §11.5）。
5. 卡片与训练记忆**长期存本地**，减少重复生成的费用。
6. 「分享专区」**先不做** —— 等框架达标之后再考虑。

所以这份报告的作用是：**把训练器这一半的契约写清楚，把工厂这一半的毛病写成需求，让下一个我从零开始造工厂，而不是接手一个半成品继续修。**

---

## 1. 用户判词与我的认领

### 1.1 「图片完全不是人能看的」

诚实地说：用户看到的那些图，**是我用假模型生成的渐变块**。

演示数据（`scripts/seed_demo.py` + `_demo_extras.py`）把 `images.source` 指到 `scripts/openai_stub.py`，
假模型返回的是一张 320×200 的程序化渐变 PNG（`backend/tests/wavfmt.py` 同款的「真彩但没内容」图片）。
我为了让 e2e 能跑通「配图那条断言」（`头是 b'\x89PNG'` + 字节数 > 0），把这种图当作「配图成功」的证据。

于是截图、发行说明、报告里的卡片，配图全是色块。用户看到的就是这个。

**但这不是全部原因**，真实链路也有三个硬伤：

| 硬伤 | 位置 | 后果 |
|---|---|---|
| 取图只有一次机会，取完直接写库 | `backend/app/pipeline.py:277` `_make_images()`、`:568` `make_example_image()` | 用户没看过这张图，它就已经是这张卡的一部分了 |
| 取回来的是**文件名**，不是可看的画面 | `backend/app/providers/images.py:663` `_store()` 返回 `f"{source}-{digest}.{ext}"`；界面只显示 `name · bytes`（`backend/frontend/js/views/settings.js:442-457`） | 测试按钮点完只看到一串哈希，等于没测 |
| 检索质量只靠关键词，没人复核 | `backend/app/providers/images.py:207` `build_query()`（砍破折号、滤 stop 词、按词长取前 5） | Commons/Openverse 返回的常常是「差不多但不是这个场景」的图 |

第三条是产品级问题：**情境图这件事，模型一次生成的成功率天然不高，必须有人（或代理）看一眼、换一张、再定。**
现在的实现里根本没有「看一眼」这一步。

### 1.2 「设置很多东西完全就是无法设置」

设置页有八张卡（外观 / 大模型 / 语音 / 配图 / 学习量 / 发音 / 自检 / 数据），
字段能改也能存（`POST /api/config`，深合并，`backend/app/config.py` 的 `save()`）。
「无法设置」的真实体感来自三件事：

1. **改了看不出效果**：语速 `rate` 和音量 `volume` 只在 Edge 引擎下生效
   （`backend/app/providers/tts.py:57` `resolve_voice()` 之后的 `_synthesize_openai()` 根本不传这两个参数），
   切到「大模型语音」后改了等于没改，而且**没有任何提示**。
2. **没有可设置的面**：`config.DEFAULTS` 里有 `llm.max_tokens`、`study.scale`、
   `study.plan_default_count`、`server.port`、`server.open_window` —— 这些界面上一个都没有。
   `tts.voice` 是 7 个预置下拉（`backend/app/providers/tts.py:22` `EDGE_VOICES`），
   想填 Edge 的其它音色名没有入口。
3. **试听按钮不试听**：`POST /api/config/test-tts` 返回 `{ok, engine, voice, bytes}`
   （`backend/app/routes/config.py:63-74`），**没有可播放的 URL**。
   于是设置页只能写「合成成功：xxx 字节。先保存，再回来试听」（`settings.js:377-380`）——
   这句话本身就是「无法预览」的自白。

### 1.3 「无法预览」

明确列一下现在**没有**的预览能力：

- 配图没有预览、没有「换一张」、没有「锁定这张」（只能整个包 `重新生成配图`）。
- 语音没有试听（生成包之后才能在卡片里听）。
- 改字号/配色/密度时，**没有实时小样**（要退出设置页才能看到）。
- 例句生成前没有「先出 3 条我挑一条」的环节，只有「生成完 8 条，不满意整包重来」。
- 没有「这张卡在手机上/窄屏上长什么样」的预览。

### 1.4 我做错的三件事（写给下一个我）

1. **把「测试通过」当成了「产品做好」。** e2e 断言 `\x89PNG` 就算配图过 ——
   这条断言本身就允许「一张纯色块」通过。**守卫要断在「人看了会满意」的近似上，不是字节头。**
2. **用假数据做验收，然后拿假数据的截图当成果。** 演示数据里没有一张真图，
   我却拿这套数据出了 19 张截图、发了版。
   下一个我做演示数据时，**至少要有 3 张真图（真检索回来的）**，否则不准出图。
3. **在工厂没做对之前就往上堆功能。** 0.1.1 加了进度页、导入导出、备份恢复 ——
   都做了，但用户真正关心的「卡片好不好看」没动。
   **判断优先级要看用户会用哪一半，不是哪一半写着顺手。**

---

## 2. 现状：0.1.1 到底有什么

### 2.1 能力清单

**训练侧（保留，直接复用）**

- 五通道：阅读（看英文浮现场景）、听力（只听声音）、口语（看场景说出来 + 跟读评分）、
  打字（看场景打英文 + 逐词对比）、造句（换一个成分造新句）。
- SRS：SM-2 变体（`backend/app/srs.py`），四档评分（没想起来 / 吃力 / 正常 / 秒答），
  撤销一步、暂停、推后、当作没学过、批量操作。
- 卡片浏览器：按状态/通道/关键词筛，全选、批量暂停/推后/重置。
- 进度页：连续天数、两周双柱、五通道进度、最该回看的卡、跟读走势。
- 跟读评分：WAV 上传 → 时长/停顿/音量三档 + 可选 ASR 逐词对照（启发式，不是音素级）。
- 备份与恢复：启动时自动备份（7 天一次，留 6 份），手动恢复（恢复前先备份当前库）。
- 关于抽屉：版本、形态、pid、端口、数据目录、打开目录、复制诊断。

**卡片侧（要搬出去，或重做）**

- 一个词 → 8 条例句 + 英英释义 + IPA + 3 种中文说法 + 场景中英文 + 换词样本。
- 配图：检索（Openverse / Wikimedia Commons）+ 画图（智谱 / OpenAI / 豆包 / 自定义）并行，先到先得。
- 语音：Edge TTS（免费、无 key）或 OpenAI 兼容 `/audio/speech`，按文本内容寻址缓存。

### 2.2 数字（0.1.1 实测）

| 项 | 数字 |
|---|---|
| 测试 | 331 passed / 19 个测试文件 |
| 前端守卫 | 19 个模块 · 39 个图标 · 296 个类名 · 版本一致 |
| 冒烟 | 41/41（真实鼠标 + 真实键盘） |
| 截图 | 19 张（1320×900） |
| e2e | 22/22（对打包产物打真 HTTP） |
| exe | 40.1 MB / 170 文件（onedir） |
| zip | 23.2 MB / `Etude-0.1.1-win64.zip` / sha256 `984f1ce1…` |

---

## 3. 代码地图

```
backend/
  run_etude.py              入口（--check / --no-window / --port / --data-dir）
  app/
    __init__.py             版本号 + BLAS 线程限流（必须最先执行）
    api.py                  装配层：_POOL(3 线程) + include_router
    runtime.py              STARTED_AT（进程启动时刻）
    schemas.py              pydantic 模型（LessonIn / PlanIn / BatchIn / ThemeIn …）
    paths.py                唯一的 frozen 判定、数据目录解析、各子目录
    config.py               DEFAULTS / 深合并保存 / public_view（永不回传 key）
    srs.py                  SM-2 变体，纯函数
    server.py               uvicorn 包装 + 端口探测（自己就是自己时复用）
    desktop.py              pywebview 窗口（关窗只 ask_stop，不 join）
    pipeline.py             生成作业：normalize / generate / _make_audio / _make_images
    prompts.py              例句提示词、大纲提示词、主题清单提示词、classify_target
    pronounce.py            WAV 解析 + 停顿/音量/时长/逐词对齐（LCS）
    selfcheck.py            十一项自检
    storage.py              数据目录搬迁
    providers/
      llm.py                OpenAI 兼容 chat + extract_json + explain_error
      tts.py                Edge / OpenAI 语音 + 内容寻址缓存 + resolve_voice
      images.py             检索（Openverse/Commons）+ 画图（四家）+ both 并行
      asr.py                转写（whisper 兼容），用于逐词对照
    routes/
      meta.py               /api/health /api/selfcheck /static/{path}（手写静态路由）
      config.py             /api/config /test-llm /test-tts /test-image /preset /catalog
      lessons.py            训练包 CRUD + 生成 + export/import + 补语音补图
      batch.py              /api/lessons/batch（贴清单）+ /batch/draft（给主题起草）
      plans.py              目标大纲 + 逐包生成
      cards.py              队列 / 取卡 / 评分 / 撤销 / 批量 / 列表
      pronounce.py          跟读上传与评分、录音白名单、/api/pronounce
      progress.py           /api/progress（14 天桶 + 连续 + 五通道 + 弱项）
      data.py               /api/data-location /api/backups /restore /open-folder /api/backup
      media.py              /api/audio/{name} /api/images/{name}
    store/
      schema.py             SCHEMA / _MIGRATIONS / connect / query / auto_backup
      lessons.py            训练包与例句读写（含完整度 readiness）
      readiness.py          images_expected / readiness / stats
      plans.py              计划读写
      cards.py              复习调度、评分、撤销、推后、浏览器查询
      attempts.py           跟读次数（best_score「只给人看不参与调度」）
      progress.py           进度统计（按本机时区切桶）
  frontend/
    index.html              壳（只挂 5 个 CSS + 一个 module）
    css/                    tokens / base / layout / components / views（共 3,535 行）
    js/
      version.js            APP_VERSION（与后端 __version__ 必须一致）
      main.js               路由装配 + 侧栏 + 快捷键 + 引导
      core/dom.js           el() / icon() / paintIcons（事件名 toLowerCase）
      core/api.js           fetch 包装（错误带 hint/kind/status）
      core/store.js         state / prefs（localStorage `etude-prefs`）/ CHANNELS
      core/router.js        六个视图 + hash 路由（#/lessons/<id>）
      core/ui.js            page / banner / toast / drawer / empty / confirm
      core/audio.js         播放、时长、系统语音兜底
      core/recorder.js      录音（ScriptProcessor，16k 单声道）
      core/wav.js           手写 16bit WAV 编码
      views/                study / lessons / lesson / plans / cards / settings /
                            progress / about / pronounce（9 个）
scripts/
  build_exe.py              前置检查 → PyInstaller → 归档核对 → 体积基线 → 冻结态冒烟
  make_release.py           zip（可复现）→ tag → release → 上传 → HEAD 校验
  e2e_check.py              对打包产物打真 HTTP（22 项）
  ui_smoke.js               真鼠标真键盘的 41 项冒烟
  screenshots.js            19 张文档截图（自己起服务 + 造数据）
  check_frontend.py         导入/图标/类名/版本四项守卫
  cdp.js                    手写 CDP 客户端（无 playwright）
  openai_stub.py            假模型（含 fault-* 故障注入）
  seed_demo.py              造「练了三周」的演示数据
  products.py / e2e 辅助     产物目录发现
build/
  etude.spec                onedir 打包描述（datas 整目录收 frontend 与 docs/images）
  size-baseline.json        体积基线
docs/
  ARCHITECTURE.md           架构与不变式（4.x 全是踩出来的规则）
  STATUS.md                 当前状态、已知问题、没做的
  DEVLOG.md                 14 轮开发日志（每轮的坑）
  QUICKSTART.md / THEORY.md 上手 / 理论
  releases/v0.1.0.md v0.1.1.md
```

---

## 4. 数据模型

SQLite，单文件 `%LOCALAPPDATA%\Etude\etude.sqlite3`（WAL）。**时间戳一律 UTC ISO 字符串**。

| 表 | 关键列 | 说明 |
|---|---|---|
| `plans` | `id, language, goal_kind, goal_label, goal_detail, domains(JSON), target_count, status(draft/generating/ready/failed), outline(JSON)` | 「想干什么」→ 模型排一份大纲 |
| `lessons` | `id, target, kind(word/phrase/sentence), language, gloss, ipa, note, status(generating/ready/failed), error, plan_id` | 训练包＝一个目标 |
| `examples` | `id, lesson_id→lessons, ord, sentence, scene_en, scene_zh, zh_variants(JSON), register, audio_path, audio_voice, scene_image, image_query` | 一条例句＝五张卡的源 |
| `swaps` | `id, lesson_id, role, original, candidates(JSON), samples(JSON)` | 造句通道的换词素材 |
| `cards` | `id, lesson_id, example_id, channel, due_at, interval(REAL), ease(REAL 2.5), reps, lapses, introduced, suspended, last_grade, last_at, best_score` | 主键 `UNIQUE(lesson_id, example_id, channel)`；`best_score` 只给人看，不参与调度 |
| `reviews` | `id, card_id, lesson_id, channel, grade, seconds, prev_state(JSON), at` | 每次评分一条；`prev_state` 支撑撤销 |
| `attempts` | `id, card_id, lesson_id, example_id, channel, mode(acoustic/mixed), score, seconds, audio, detail(JSON), at` | 跟读独立一条线，不参与 SRS |

规则（写在 `backend/app/store/schema.py` 与 `docs/ARCHITECTURE.md` §4）：

- `connect()` 必须**先 `_migrate` 补列、再 `executescript(SCHEMA)`**。反序会让老库启动即崩。
- 迁移只加列、不删列；`_MIGRATIONS` 是 `(表, 列, 声明)` 三元组清单。
- 媒体（音频/图）**存相对文件名**，绝对路径换数据目录即死链。
- 音频/图都是**内容寻址**：`sha256(...)[:20]`，文件名即缓存键。
- 备份走 `VACUUM INTO`（不能 `cp`，WAL 会有不一致的中间态）。
- 恢复必须先 `db.close()` → 删 `-wal`/`-shm` → `copyfile`，否则旧 WAL 会重放出「一半新一半旧」。

**迁移到插件时代时，这套表可以整张保留**，只需要给 `examples` 加两列（例如
`image_meta(JSON)` 存候选与来源、`media_state` 存「已确认/待复核」），以及为新格式加一个 `format` 字段。

---

## 5. HTTP 接口契约

全部 `127.0.0.1`，无鉴权（本机单用户）。错误统一 `{error, hint, kind?}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{app, version, pid, started_at, uptime_seconds, mode, data_dir, port}` |
| GET | `/api/selfcheck` | 十一项自检结果 |
| GET/POST | `/api/config` | `public_view()` 永不回传 key；POST 深合并 |
| POST | `/api/config/test-llm` \| `test-tts` \| `test-image` | **试听/试看这三条就是交接里要改的重点**（见 §11.3/§11.4） |
| GET | `/api/catalog` | 厂商预设、音色、图源、语言、目标类型、领域、通道 |
| GET/POST | `/api/lessons` | 列表（带计数）/新建（返回 `{lesson_id, reused}`，**不是** `{lesson:{}}`） |
| GET | `/api/lessons/{id}` | 详情：examples + swaps + cards + readiness + job + srs_preview |
| GET | `/api/lessons/{id}/job` | `{status, error, job}`（轮询用，1500 ms） |
| POST | `/api/lessons/{id}/regenerate` \| `/audio` \| `/images` \| `/rebuild-cards` | 重建素材/卡 |
| GET | `/api/lessons/{id}/export` | 见 §6 |
| POST | `/api/lessons/import` | 接受 §6 的 JSON，同句去重，落地为「缺语音」 |
| POST | `/api/lessons/batch` | 贴清单批量建包（`{created, retried, skipped, total, queued}`） |
| POST | `/api/lessons/batch/draft` | 给主题起草目标清单（不动库） |
| GET/POST | `/api/plans` | 计划；GET 带 `progress{total, ready, failed, running}` |
| PUT | `/api/plans/{id}/outline` \| POST `/start` | 存大纲 / 开始逐包生成 |
| GET | `/api/queue?limit=` | 到期队列 + `stats` |
| GET | `/api/cards/{id}` | `{card, lesson, example, swaps, srs_preview, can_undo}` |
| POST | `/api/cards/{id}/grade` | `{grade:0..3, seconds}` → `{interval_days, due_at, next_in_minutes}` |
| POST | `/api/cards/{id}/undo` | 撤销上一步评分（还原快照 + 删 review 行） |
| PATCH | `/api/cards/{id}` | 手改 SRS（interval/ease/suspended/introduced） |
| GET | `/api/cards?…` | 浏览器分页查询 |
| POST | `/api/cards/batch` \| `/push` | 批量暂停/重置 / 推后 N 天 |
| POST | `/api/cards/{id}/attempt?ref_seconds=` | **body 是 WAV 原始字节**，返回评分 + 逐词 + 历史摘要 |
| GET | `/api/attempts` \| `/api/recordings/{name}` | 跟读历史 / 录音回放 |
| GET | `/api/progress?days=14` | 进度统计（本机时区切桶） |
| GET/POST | `/api/data-location` | 搬家（`copy` 可选） |
| GET | `/api/backups` \| POST `/api/backups/restore` | 备份列表 / 恢复 |
| POST | `/api/backup` \| `/api/open-folder` | 立即备份 / 打开四个目录之一 |
| GET | `/api/audio/{name}` \| `/api/images/{name}` | 白名单 + 长缓存 |
| GET | `/static/{path}` | 前端模块与 CSS（统一 `no-store`） |

**给插件的建议**：不要直接写 SQLite。用 `POST /api/lessons/import`（或新增 `/api/lessons/upsert`）落地，
把「写库」这件事留在 Etude 里。理由：迁移、索引、卡片铺开（每条例句 5 张卡）都在那边做完了，
插件重复实现一遍必然不同步。

---

## 6. 卡片包格式

`GET /api/lessons/{id}/export` 的真实形状（0.1.1 稳定契约）：

```json
{
  "format": "etude-lesson",
  "format_version": 1,
  "exported_at": "2026-10-28T12:04:11+00:00",
  "lesson": {
    "target": "appreciate",
    "kind": "word",
    "language": "en",
    "gloss": "to understand how good appreciate is, or to enjoy it",
    "ipa": "/əˈpriːʃieɪt/",
    "note": "Common in everyday speech.",
    "created_at": "2026-10-28T09:12:03+00:00"
  },
  "examples": [
    {
      "ord": 1,
      "sentence": "I really appreciate the way this one works.",
      "scene_en": "At a concert, someone worries the crowd won't get it — you say it appeals to anyone.",
      "scene_zh": "音乐节上有人担心台下听不懂，你说谁都能听得进去。",
      "zh_variants": ["这不挑人。", "音乐节上有人担心台下听不懂，你说谁都能听得进去。"],
      "register": "日常口语"
    }
  ],
  "swaps": [
    {"role": "object", "original": "the way this one works",
     "candidates": ["the design", "the ending", "the effort"],
     "samples": ["I really appreciate the design."]}
  ],
  "note": "共 8 条例句，其中 8 条带语音（语音不随文件导出）"
}
```

**不含语音与图**（二进制）。导入会校验：`format` 必须是 `etude-lesson`、语言受支持、
`sentence` 必填且 ≤40 条、`zh_variants` ≤4 个、`scene_*` ≤600 字、同句按 `casefold()` 去重。

**建议的 v2（插件对接用）**：

```json
{
  "format": "etude-lesson",
  "format_version": 2,
  "generator": {"kind": "dsh-plugin", "model": "deepseek-v4", "run_id": "…"},
  "lesson": { "...": "同 v1" },
  "examples": [
    { "...": "同 v1",
      "media": {
        "image": {"file": "img/0001.webp", "source": "search:commons", "query": "concert crowd",
                  "license": "CC BY-SA 4.0", "author": "…", "credit_url": "…", "sha256": "…"},
        "audio": {"file": "audio/0001.mp3", "engine": "dsh-tts", "voice": "en-female-warm",
                  "rate": "-8%", "seconds": 2.6, "sha256": "…"}
      },
      "review": {"image_accepted": true, "audio_accepted": true, "by": "agent", "at": "…"}
    }
  ]
}
```

v2 的三条硬要求：**媒体带来源与许可**（检索来的图必须能追溯）、**带 review 记录**（谁点的头）、
**带 sha256**（同一张图在两个包里不重复生成）。这两样东西现在都没有，是「费用」与「合规」两个问题的根。

---

## 7. 资源管线（配图 / 语音）

### 7.1 配图（现状）

```
scene_en ──build_query()──> 关键词 ──> Openverse ─┐
         └─build_prompt()──> 提示词 ──> 画图 API ─┴─ 先到先得 ──> _store() ──> examples.scene_image
```

- `source` 四档：`both`（默认，并行先到）/ `search` / `llm` / `off`。
- 检索：Openverse（`api.openverse.org`，CC 授权）+ Wikimedia Commons（`gsrnamespace=6`），
  排序 = 标题命中数 → 横图优先 → 宽度。
- 画图：智谱 `cogview-3-flash`（免费档）/ OpenAI `gpt-image-1` / 豆包 / 自定义 OpenAI 兼容。
- 缓存：`sha256(f"{source}\x1f{key}")[:20]`，四种扩展名都探。
- 失败：`_sniff()` 只认文件头（jpg/png/webp/gif），认不出直接抛错（有的服务 200 + HTML）。
- 上限：`MAX_BYTES = 4 MB`；`max_per_lesson` 控制每包配几张（0 = 全配）。

**问题**：没有候选、没有预览、没有挑图、没有「这张不行，换一张」；一次成型。
**新方向**：见 §11.3。

### 7.2 语音（现状）

- 引擎二选一：`edge`（默认，免费无 key，7 个预置音色）或 `openai`（`/audio/speech`）。
- 缓存键：`sha256("\x1f".join([engine, voice, rate, text]))[:20]` → `{engine}-{digest}.mp3`。
- 合成后**必须校验文件存在且非空**，否则抛 `TTSError`（有的库「成功返回」但没写字）。
- `resolve_voice()` 是唯一取音色的地方（两个字段 `tts.voice` = Edge 名、`tts.openai.voice` = alloy，
  散在三处就会把 Edge 音色发给 `/audio/speech` → 400 → 整包静音）。
- **缺口**：`rate`/`volume` 在 openai 引擎下被忽略；试听不返回音频；不能填自定义 Edge 音色。

**新方向**：TTS 主链在 DSH 侧（可换任何 OpenAI 兼容 `/audio/speech`，或本地部署 CosyVoice / GPT-SoVITS），
Edge 只作离线兜底。音色/语气作为「包级参数」写进 lesson，而不是全局设置 —— 见 §11.4。

---

## 8. 前端架构与视觉规范

- **零构建**：原生 ES 模块 + 手写 CSS。没有 npm、没有 bundler、没有 CDN。
  好处：`git clone` 就能跑、PyInstaller 直接拷目录；坏处：没有类型、没有 tree-shaking（也没必要）。
- **五个 CSS 分层**：`tokens`（颜色/字号/圆角/阴影变量）→ `base`（重置与排版）→
  `layout`（壳：侧栏/顶栏/内容区）→ `components`（按钮/卡片/表格/抽屉/toast）→
  `views`（各页专属，按页面分段注释）。
- **硬规则**：组件不许写死颜色，一律用 `--` 变量；`html[data-theme]` 四档、`html[data-size]` 三档。
- **事件绑定**：`el("button", { onClick })`，内部 `key.slice(2).toLowerCase()`。
  事件名大小写错误浏览器不报错、截图看不出 —— 所以 `check_frontend.py` 会扫。
- **图标**：`PATHS` 里 39 个 inline SVG，`stroke: currentColor`，不引图标库。
- **状态**：`state` 一个对象，`render(host)` 按路由重画笔；prefs 存 `etude-prefs`。

「好看」现在是什么水平：见 `docs/images/01…19.png`（19 张）。
我的判断：**信息层级和留白是够的，但「卡片本身」不够好看** —— 因为卡片的主体（图）是假的（§1.1），
而真正的卡片视觉（照片 + 句子排版 + 场景说明）从来没有被真图驱动过。

---

## 9. 测试与守卫

| 层 | 命令 | 抓什么 |
|---|---|---|
| 单元/接口 | `cd backend && python -m pytest tests/ -q`（331） | 数据层语义、SRS、导入导出、进度桶、备份恢复、故障注入 |
| 前端静态 | `python scripts/check_frontend.py` | ESM 导入名是否存在、图标名是否存在、类名是否有 CSS 规则、版本一致 |
| 冒烟 | `node scripts/ui_smoke.js --base http://127.0.0.1:8977`（41） | **真鼠标 + 真键盘**、控制台零 error、页面零未捕获异常 |
| 截图 | `node scripts/screenshots.js`（19） | 自己起服务 + 造数据，出图给人和给我看 |
| 端到端 | `python scripts/e2e_check.py --llm-base …`（22） | 对**打包产物**打真 HTTP：健康、静态、自检、越权、生成、队列、评分 |

三个「守卫本身也要被验」的用例：改名一个导出函数、写错一个图标名、删掉一条 CSS 规则 ——
`tests/test_guards.py` 里有反证用例，保证守卫不是永远绿的摆设。

**交接时要加的三条守卫**（现在没有）：

1. 配图必须**人能看**：`test_image_is_not_a_flat_gradient`（方差/熵门限），把假图的通过路径堵死。
2. 试听必须**真的能播**：`/api/config/test-tts` 必须返回可下载 URL 且 `>= 8000` 字节、`mp3` 头正确。
3. 卡片页**首屏必须有图**（当 `source != off`）：`screenshots.js` 出图后检查页面里 `img` 的 `naturalWidth >= 480`。

---

## 10. 打包与发版

- **onedir**（不是 onefile）：onefile 退出即删临时目录，用户以为数据丢了。
- `build/etude.spec`：`datas` 整目录收 `backend/frontend` + `docs/*.md` + `docs/images`；
  `excludes` 显式切断 numpy/scipy/pandas/onnxruntime/faster_whisper/tokenizers/av/…；
  `console=True`（`Etude.exe --check` 是双击没反应的唯一出口）。
- `scripts/build_exe.py`：preflight（前端守卫 + 版本一致）→ PyInstaller → 归档核对 18 个模块 →
  体积基线 → 冻结态 `--check` 冒烟。**体积只报警不失败**。
- `scripts/make_release.py`：zip（**可复现**：条目固定时间戳 1980-01-01、deflate 9、`external_attr` 固定）→
  取凭据（`git credential fill`，不落盘）→ 打 annotated tag + push → 建/复用 release →
  上传附件 → `HEAD` 校验下载页。
- 发版前必须核对**产物里的 `frontend/js/version.js`**（防「标签对、内容错」）。
- 已发布：`v0.1.0`（release 425663278）、`v0.1.1`（release 426616149）。
- 体积基线：0.0.3 51.5 MB → 0.1.0 39.7 MB → 0.1.1 40.1 MB（`build/size-baseline.json` 会被脚本更新）。

---

## 11. 新方向：插件化设计

### 11.1 边界：谁做什么

```
┌──────────────────────── DeepSeek Harness 侧（插件） ────────────────────────┐
│ 卡片工厂 card-factory                                                       │
│  1. 选目标（词/短语/整句）→ 提示词 → 8 条例句（不同场景、不同说法）        │
│  2. 场景 → 图：检索候选 N 张 → 预检（可看/可用/无文字）→ 挑 1 张           │
│  3. 句子 → 语音：TTS（可换音色/语速）→ 试听 → 定稿                        │
│  4. 出「卡片包」：manifest.json + media/（图、音频）+ 可选：质量报告        │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │  POST /api/lessons/import（format_version 2）
┌────────────────────────────────────▼────────────────────────────────────────┐
│ Etude（训练器）                                                             │
│  · 落库、铺 5 张卡/例句、SRS 调度                                           │
│  · 展示与训练：五通道 + 跟读评分 + 进度                                     │
│  · 长期记忆：SQLite（唯一真相）+ 内容寻址媒体 + 导出归档                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

**为什么这么切**：

- 生成是「一次性、贵、需要人看」；训练是「长期、便宜、天天跑」。两者节奏完全不同，混在一个进程里
  就会像现在这样：工厂的毛病拖着训练器一起被骂。
- 生成需要换模型、换图源、换音色、加人工复核；这些在 DSH 插件里是「加一个工具」，在 Etude 里是「改半个后端」。
- 训练器要能在**完全离线**下跑：没有 key、没有网，照样能练已有的卡。现在的 Etude 已经满足这点，
  别把它打破。

### 11.2 卡片工厂的接口（建议）

一次性出包（job 语义，幂等）：

```
POST /api/card-factory/jobs            → {job_id}
GET  /api/card-factory/jobs/{id}       → {status, step, progress, error, result}
POST /api/card-factory/jobs/{id}/cancel
```

产物落地只有一条路：**写 `manifest.json` + `media/` 到 `%LOCALAPPDATA%\Etude\staging\<job_id>\`，
再调 `POST /api/lessons/import`（v2）**。Etude 负责：校验 → 落库 → 铺卡 → 记 media 来源与 sha256。

三条不变式：

1. **同一个 target + 同一套参数，重复 job 不重复收费**：用 `(target, examples, model, prompt_version)`
   做幂等键，命中 staging 里的旧产物就直接复用。
2. **媒体内容寻址**：文件名 = `sha256(bytes)[:20]`。两张卡用同一张图，磁盘上只有一份。
3. **失败要能重来**：单条例句的图失败，不影响其它 7 条；重跑只补失败的那几条（`need_audio` / `need_image` 列表）。

### 11.3 配图子系统（这是用户最不满的一环）

**目标**：卡片上的图必须「一眼就是这个场景」，且**用户能看见、能换、能锁定**。

流程（三段，缺一不可）：

```
场景文本
  ├─(A) 检索：Openverse / Commons / 可选 Unsplash&Pexels（需 key）→ 每个 query 取 6~8 张
  ├─(B) 画图：自部署 SDXL/Flux（可选，默认关）→ 每 prompt 2~4 张
  └─(C) 候选池 → 预检 → 排序 → 展示
```

**预检**（自动，省人力）：

- 尺寸：短边 ≥ 640；宽高比 1.2~2.0 优先；
- 内容：`_sniff` 文件头 + 主色方差/熵（挡掉纯色块与渐变）+ OCR 简查（画面里有大段文字就降权，
  卡片上不该出现英文单词，那会直接剧透）；
- 相关性：标题/标签/描述与场景关键词的命中数（现有 `pick_candidate` 已有雏形）；
- 合规：许可（CC / 公有领域 / 已购）、作者与来源链接**必须一起存**。

**人（或代理）做的一步**：候选池 → 9 宫格预览（每张带来源与许可）→ 点选/换一批/上传自己找的 → 锁定。
锁定后写 `media.image = {file, source, query, license, author, credit_url, sha256}` 与 `review`。

**本地生图**：接口留着，默认关。原因写清楚：自部署 SDXL/Flux 要 6~12 GB 显存、
冷启动 20~60 秒、出一张 3~15 秒，而且「像照片」这件事上仍不如检索。
它的正确用途是**补检索找不到的场景**（抽象动作、特定文化场景），不是主力。

### 11.4 语音子系统

**主链**：DSH 侧 TTS（OpenAI 兼容 `/audio/speech` 或本地 CosyVoice / GPT-SoVITS）。
**兜底**：Edge TTS（免费、离线可用、无 key）。**切换语义**：不是「设置里选一个」，
而是「按可用性自动降级，并在卡片上标注用的是哪个」（用户要能听见差别）。

参数要**真正生效**（现在 `rate`/`volume` 在 openai 引擎下静默失效，这是硬伤）：

| 参数 | Edge | OpenAI 兼容 | 本地 CosyVoice/GPT-SoVITS |
|---|---|---|---|
| 音色 | 音色名 | `voice` | 参考音频（few-shot） |
| 语速 | `rate=+10%` | `speed=1.1` | `speed` |
| 音量 | `volume=+10%` | 不支持 → 本地增益或说明书里写明 | 同左 |
| 语气 | 不支持 | `instructions`（部分兼容层支持） | 参考音频的情感 |

**试听**（现在完全没有）：`POST /api/config/test-tts` 必须返回 `{url, bytes, seconds, engine, voice}`，
界面里直接 `<audio controls>` 播放，并给「三句不同长度」的试听样本（短句 4 词 / 中句 10 词 / 长句 18 词），
因为**音色在长句上才听得出来**。

**音色训练**：先不做。理由：训练一个能用的中文音色要 10~60 分钟样本清洗 + 几十分钟训练，
而它的收益（「听起来更像某个人」）对背句子的帮助很小。等 §11.6 的费用账算清、框架稳定后再开。

### 11.5 卡片 UI 的「好看」标准（可验收）

不定「好看」，就永远做不好。给六条能检查的：

1. **图是主体**：卡片上图文比 ≥ 0.55:1；图必须真照片（非渐变/非插画），
   失败时**不占位**（现在的「没有图」虚线框要删掉，这正是 0.1.1 改过一半的地方）。
2. **文字只有三层**：句子（衬线，1.5~1.9rem）/ 场景说明（1 行，次级色）/ 元信息（IPA、语域，最小）。
   不许出现第四种字号来「强调」。
3. **一屏一卡**：翻面不跳版（翻面前后卡片高度差 < 6%），评分区粘底但不遮内容。
4. **动效 ≤ 180 ms**，只用于翻面与揭示；`prefers-reduced-motion` 时全部关掉。
5. **窄屏（360 px）不塌**：图高 240 px、字号同档、按钮不换行。
6. **暗色是设计过的暗色**：不是把白底反相（`--surface` 与 `--ink` 的对比度 ≥ 7:1，
   图不加滤镜、不压暗超过 6%）。

### 11.6 长期记忆与费用

**本地就是唯一真相**：`%LOCALAPPDATA%\Etude\` 下 `etude.sqlite3` + `audio/` + `images/`。

省钱的四层：

1. **内容寻址缓存**：同一句话 → 同一个 mp3；同一张图 → 同一份文件（现在已有，保持住）。
2. **幂等生成**：`(target, 参数, prompt_version)` 命中就复用，不再花钱（现在没有，要加）。
3. **增量补齐**：只补 `missing_*`，不整包重生成（`readiness()` 已经列出 missing 列表，直接用它）。
4. **失败不复用、成功才入库**：坏图坏音频不许进 staging 的「已生成」集合，
   否则用户会一直看到坏的那张。（现在 `_make_images` 失败只记日志，这点是对的，保持。）

**导出归档**：包级导出（v2 带媒体）→ 一个 zip / 一个目录，可备份、可迁移、以后可上传到分享专区。

### 11.7 分享专区（先不做，留接口）

等三件事齐了再说：包格式稳定（v2 定稿）、内容有许可信息、本地有「我做的包」索引。
届时只需要：`POST /api/packs/publish`（上传 zip + 元信息）与 `GET /api/packs/search`。
**不要现在就做** —— 0.1.1 的教训就是「工厂没做对就先堆功能」。

---

## 12. 迁移路线

### 阶段 0：把「坏的」钉住（半天）

- 加 §9 的三条守卫（配图非纯色、试听可播、首屏有真图）。
- 删掉训练页的「没有图」占位与演示数据里的假图（`seed_demo` 只在没有真图源时不配图）。
- 验收：`pytest` 里出现「纯色块配图会失败」的反证用例。

### 阶段 1：工厂协议（1~2 天）

- 定 v2 manifest（§6），Etude 侧加 `POST /api/lessons/import` 的 v2 分支：
  收媒体文件、校验 sha256、写 `media` 与 `review`、铺卡。
- Etude 侧加 `staging/` 目录约定与 `need_audio/need_image` 的增量补链接口。
- 验收：手工放一个 v2 包进 `staging/` → 导入 → 卡片页显示真图真音，且来源/许可可见。

### 阶段 2：DSH 插件（卡片工厂，3~5 天）

- 目标 → 例句（复用现有 `prompts.py` 的口径，但由 DSH 侧调模型）。
- 图：检索候选 6~8 张 → 预检 → 九宫格挑图（人/代理）→ 定稿。
- 音：TTS 试听三句 → 定稿（音色/语速写进包）。
- 出 v2 包 → 调 import。
- 验收：一条命令给 8 个目标，出的包里**每张图都有人点过头**，每条例句都能播。

### 阶段 3：训练器收口（2~3 天）

- 卡片视觉按 §11.5 六条重做（图为主体、三层文字、一屏一卡、窄屏不塌、暗色对比度）。
- 训练页与详情页删掉所有「假占位」。
- 验收：360 / 960 / 1320 三个宽度各出一张图，六条标准逐条对照。

### 阶段 4：发布与分享（等前三个阶段稳定）

- 发 0.2.0（不兼容 0.1.x 的包格式，但要能导入 v1）。
- 再考虑分享专区。

---

## 13. 不要带过去的七个包袱

1. **「设置里选引擎」** → 改成可用性自动降级 + 卡片上标注实际引擎。
2. **一次成型、无候选的配图** → 改成候选池 + 挑图 + 锁定。
3. **`test-*` 只回字节数** → 必须回可播放/可显示的 URL。
4. **假模型产生的渐变图参与验收** → 演示数据里必须有真图，否则不准出图。
5. **「没有图」虚线占位框** → 没图就不占位（0.1.1 已改一半，插件时代要彻底删掉）。
6. **把 key 塞进全局配置** → 插件时代把凭据放 DSH 侧，Etude 只留「已配置/未配置」布尔。
7. **在工厂没做好之前堆训练器功能** → 优先级按「用户会用哪一半」排，不按「哪一半顺手」。

---

## 14. 环境与运维备忘

| 项 | 事实 |
|---|---|
| 仓库 | `F:\dsharness\Etude-work\Etude`（clone 自 https://github.com/whyao56/Etude） |
| 解释器 | `.venv`（Python 3.11.9；fastapi 0.162 / uvicorn 0.54 / pydantic 2.14 / edge-tts 7.3.1 / pyinstaller 6.22.3 / pywebview / pytest 9.1.1） |
| 服务 | `backend/run_etude.py --no-window --port 8977`；端口被自己占用时**复用**（`probe_etude`），不会开第二个实例 |
| 数据目录 | `%LOCALAPPDATA%\Etude`（可用 `ETUDE_DATA_DIR` 覆盖；演示数据在 `F:\dsharness\Etude-work\demo-data`） |
| 假模型 | `scripts/openai_stub.py --port 18999`（模型名 `fault-401/404/500/notjson/fenced/messy/slow` 注入故障） |
| 凭据 | `git credential fill`（Windows 凭据管理器，helper=manager）。**没有 gh CLI** |
| 禁止 | 不要探测用户 home 下的凭据文件（CC Safety Net 会拦，且不该碰） |
| 已知坑 | ① 改前端文件**不许**用 PowerShell 管道（`Get-Content` 按 GBK 解 UTF-8，会把中文洗成乱码且不可逆）；② `write` 工具没有 append，改长文档用 `edit` 以尾部为锚；③ 换文件后要重启服务（否则旧进程还在服务旧代码）；④ pwsh 管道的退出码不可信，看脚本自己打印的结论 |

---

## 15. 验收清单

交给下一个我，逐条勾：

- [ ] 读完 §1，认同「工厂是坏的、训练器是好的」这个切分
- [ ] 能跑起 0.1.1（`pytest` 331 / 冒烟 41 / e2e 22）
- [ ] 仓库里加好 §9 的三条新守卫，且它们是**能失败**的（有反证用例）
- [ ] v2 manifest 定稿，`import` 能吃下并显示真图真音
- [ ] 一条命令出 8 个包，每个包的每张图都经过「候选 → 挑图 → 锁定」
- [ ] 试听按钮真的能播（三句不同长度）
- [ ] 卡片视觉按 §11.5 六条过一遍（360 / 960 / 1320 三张图对照）
- [ ] 演示数据里有真图，截图里不再出现色块
- [ ] 幂等与增量补齐：重复跑同一个目标不重复收费
- [ ] 发 0.2.0，发行说明里写清 v1 包还能导
