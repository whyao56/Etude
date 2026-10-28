/* 设置：把「这台机器上该怎么跑」一次说清。
 *
 * 八张卡，顺序按「你多久才会改一次」排：
 * 外观 → 大模型 → 语音 → 配图 → 学习量 → 发音 → 自检 → 数据。
 * 每个能改的地方都配一个「试一下」：不会失败的检查不是检查，
 * 而配置项最典型的问题就是「看着填对了」。
 */

import { get, post } from "../core/api.js";
import { el, humanSize } from "../core/dom.js";
import {
  CHANNEL_LABEL,
  SIZES,
  THEMES,
  applyLook,
  savePrefs,
  state,
} from "../core/store.js";
import { chip, errorBlock, field, loading, sectionHead, setPage, toast } from "../core/ui.js";

const SECTION_IDS = ["look", "llm", "tts", "images", "study", "pronounce", "selfcheck", "data"];
const SECTION_LABEL = {
  look: "外观",
  llm: "大模型",
  tts: "语音",
  images: "配图",
  study: "学习量",
  pronounce: "发音",
  selfcheck: "自检",
  data: "数据",
};

export async function render(host) {
  setPage({ title: "设置", sub: "外部服务、学习量、数据位置", actions: [] });
  host.replaceChildren(loading("读配置…"));
  try {
    await ensure();
  } catch (err) {
    host.replaceChildren(errorBlock(err, () => render(host)));
    return;
  }
  host.replaceChildren();
  // 八张卡一路到底太长，顶上给一排能跳的锚点。
  // 这几张卡的顺序是按「多久才会改一次」排的，不是按重要性 —— 所以需要能跳。
  host.appendChild(sectionNav());
  const cards = [
    lookCard(),
    llmCard(),
    ttsCard(),
    imagesCard(),
    studyCard(),
    pronounceCard(),
    selfcheckCard(),
    dataCard(),
  ];
  cards.forEach((node, index) => {
    node.id = `set-${SECTION_IDS[index]}`;
  });
  host.append(...cards);
}

function sectionNav() {
  const bar = el("div", { class: "settings-nav" });
  const buttons = new Map();
  for (const id of SECTION_IDS) {
    const button = el("button", { class: "chip pick", type: "button", text: SECTION_LABEL[id] });
    button.addEventListener("click", () => {
      const target = document.getElementById(`set-${id}`);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    buttons.set(id, button);
    bar.appendChild(button);
  }

  // 滚到哪一张就把哪一颗点亮 —— 光有跳转不够，回来时不知道自己在第几节。
  if (typeof IntersectionObserver === "function") {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const id = entry.target.id.replace(/^set-/, "");
          for (const [key, button] of buttons) {
            button.classList.toggle("is-here", key === id);
          }
        }
      },
      { rootMargin: "-15% 0px -70% 0px", threshold: 0 },
    );
    // 卡片是同步插进 DOM 的，等一帧再观察，否则拿到的是还没挂上去的节点。
    requestAnimationFrame(() => {
      for (const id of SECTION_IDS) {
        const node = document.getElementById(`set-${id}`);
        if (node) observer.observe(node);
      }
    });
  }
  return bar;
}

async function ensure(force = false) {
  if (state.catalog && state.config && !force) return;
  const [config, catalog] = await Promise.all([get("/api/config"), get("/api/catalog")]);
  state.config = config;
  state.catalog = catalog;
}

function card(title, words) {
  const box = el("section", { class: "card" });
  box.appendChild(el("div", { class: "card-head" }, [el("h3", { text: title })]));
  if (words) box.appendChild(el("p", { class: "card-note", text: words }));
  return box;
}

function input(value, { type = "text", placeholder = "", rows = 0, mono = false } = {}) {
  if (rows) {
    return el("textarea", { class: "input", rows: String(rows), placeholder, text: value || "" });
  }
  return el("input", {
    class: "input" + (mono ? " mono" : ""),
    type,
    value: value == null ? "" : String(value),
    placeholder,
  });
}

/**
 * 后端的清单有的直接是数组，有的按 id 做成对象（`{deepseek: {...}}`）。
 * 界面这边统一成 `[{id, label, ...}]` —— 不然就会像 0.1.0 第一次冒烟那样，
 * 设置页直接抛 `options.map is not a function`。
 */
function asList(value) {
  if (Array.isArray(value)) {
    return value.map((item) => (typeof item === "string" ? { id: item, label: item } : item));
  }
  if (value && typeof value === "object") {
    return Object.entries(value).map(([id, item]) =>
      typeof item === "string" ? { id, label: item } : { id, label: id, ...(item || {}) },
    );
  }
  return [];
}

function selectOf(options, value) {
  return el(
    "select",
    { class: "input" },
    asList(options).map((item) => {
      const id = typeof item === "string" ? item : item.id;
      const label = typeof item === "string" ? item : item.label || item.id;
      return el("option", { value: id, text: label, selected: String(id) === String(value) });
    }),
  );
}

function saveButton(onClick, label = "保存") {
  const button = el("button", { class: "btn primary", type: "button", text: label });
  button.addEventListener("click", () => onClick(button));
  return button;
}

async function savePatch(patch, button, message) {
  if (button) button.disabled = true;
  const label = button ? button.textContent : "";
  try {
    const res = await post("/api/config", patch);
    state.config = res.config || state.config;
    if (message) {
      message.textContent = "存好了。";
      message.className = "hint ok";
    }
    // 按钮自己也要说话：点了之后顶部没变化时，人只会以为没点上。
    if (button) {
      button.textContent = "已保存";
      button.classList.add("is-saved");
      setTimeout(() => {
        button.textContent = label;
        button.classList.remove("is-saved");
      }, 1400);
    }
    toast("存好了。", "ok");
    if (patch.channels || patch.study) state.stats = null;
    return res;
  } catch (err) {
    if (message) {
      message.textContent = err.message;
      message.className = "hint bad";
    } else {
      toast(err.message, "bad");
    }
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

/* ------------------------------------------------------------------ 外观 */

function lookCard() {
  const box = card("外观", "只影响这台机器，不进训练数据。");
  const prefs = state.prefs;

  const themeRow = el("div", { class: "chips" });
  for (const theme of THEMES) {
    const button = el("button", {
      class: "chip pick",
      type: "button",
      text: theme.label,
      "aria-pressed": prefs.theme === theme.id ? "true" : "false",
    });
    button.addEventListener("click", () => {
      prefs.theme = theme.id;
      savePrefs();
      applyLook();
      for (const other of themeRow.children) other.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-pressed", "true");
    });
    themeRow.appendChild(button);
  }

  const sizeRow = el("div", { class: "chips" });
  for (const size of SIZES) {
    const button = el("button", {
      class: "chip pick",
      type: "button",
      text: size.label,
      "aria-pressed": prefs.size === size.id ? "true" : "false",
    });
    button.addEventListener("click", () => {
      prefs.size = size.id;
      savePrefs();
      applyLook();
      for (const other of sizeRow.children) other.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-pressed", "true");
    });
    sizeRow.appendChild(button);
  }

  box.append(field("配色", themeRow), field("字号", sizeRow));
  return box;
}

/* ------------------------------------------------------------------ 大模型 */

function llmCard() {
  const cfg = (state.config && state.config.llm) || {};
  const presets = asList(state.catalog && state.catalog.llm_presets);
  const box = card("大模型", "例句、场景、中文说法、大纲都是它生成的。填一个 OpenAI 兼容的地址就行。");
  const message = el("div", { class: "hint", text: "" });

  const preset = selectOf(presets, cfg.preset || "");
  const base = input(cfg.base_url, { placeholder: "https://api.deepseek.com/v1", mono: true });
  const model = input(cfg.model, { placeholder: "deepseek-chat", mono: true });
  const key = input("", {
    type: "password",
    placeholder: cfg.api_key_set ? "已经存了，留空不改" : "粘贴你的 key",
  });
  const temperature = input(cfg.temperature ?? 0.9, { type: "number" });
  const timeout = input(cfg.timeout ?? 120, { type: "number" });

  preset.addEventListener("change", () => {
    const item = presets.find((one) => one.id === preset.value);
    if (!item) return;
    base.value = item.base_url || "";
    if (item.model) model.value = item.model;
    if (item.hint) message.textContent = item.hint;
  });

  const save = saveButton(async () => {
    const patch = {
      preset: preset.value,
      base_url: base.value.trim(),
      model: model.value.trim(),
      temperature: Number(temperature.value || 0.9),
      timeout: Number(timeout.value || 120),
    };
    if (key.value.trim()) patch.api_key = key.value.trim();
    await savePatch({ llm: patch }, save, message);
  });

  const test = el("button", { class: "btn", type: "button", text: "试一下" });
  test.addEventListener("click", async () => {
    test.disabled = true;
    message.className = "hint";
    message.textContent = "正在问一句话…";
    try {
      const res = await post("/api/config/test-llm", {});
      message.className = "hint ok";
      message.textContent = `通了，用了 ${res.seconds} 秒。`;
    } catch (err) {
      message.className = "hint bad";
      message.textContent = err.hint ? `${err.message} —— ${err.hint}` : err.message;
    } finally {
      test.disabled = false;
    }
  });

  box.append(
    field("厂商", preset),
    field("接口地址", base, "要以 /v1 结尾。Ollama 之类本机服务也填在这里。"),
    field("模型名", model),
    field("key", key, "只存在本机，接口永远不会把它原样返回。"),
    // 温度和超时是「填错了也能用」的那类，收起来不占视线。
    el("details", { class: "adv" }, [
      el("summary", { text: "高级：温度、超时" }),
      el("div", { class: "grid2" }, [
        field("温度", temperature, "越高越会换着说法写例句。"),
        field("超时（秒）", timeout, "网络慢就往大调，默认 120。"),
      ]),
    ]),
    el("div", { class: "row" }, [save, test, message]),
  );
  return box;
}

/* ------------------------------------------------------------------ 语音 */

function ttsCard() {
  const cfg = (state.config && state.config.tts) || {};
  const openai = cfg.openai || {};
  const voices = (state.catalog && state.catalog.tts_voices) || { edge: [], openai: [] };
  const box = card("语音", "每条例句都有一条真人念的音频。Edge 免费、不用 key。");
  const message = el("div", { class: "hint", text: "" });

  const engine = selectOf(
    [
      { id: "edge", label: "Edge 语音（免费）" },
      { id: "openai", label: "大模型语音（OpenAI 兼容）" },
    ],
    cfg.engine || "edge",
  );
  const voice = selectOf(asList(voices.edge), cfg.voice);
  const rate = input(cfg.rate || "+0%", { placeholder: "+0%" });
  const volume = input(cfg.volume || "+0%", { placeholder: "+0%" });
  const oBase = input(openai.base_url, { placeholder: "https://api.openai.com/v1", mono: true });
  const oModel = input(openai.model, { placeholder: "tts-1", mono: true });
  const oVoice = selectOf(asList(voices.openai), openai.voice);
  const oKey = input("", {
    type: "password",
    placeholder: openai.api_key_set ? "已经存了，留空不改" : "大模型语音需要 key",
  });

  const openaiFields = el("div", {}, [
    field("大模型语音 · 地址", oBase),
    field("大模型语音 · 模型", oModel),
    field("大模型语音 · 音色", oVoice),
    field("大模型语音 · key", oKey),
  ]);

  const sync = () => {
    openaiFields.style.display = engine.value === "openai" ? "" : "none";
  };
  engine.addEventListener("change", sync);
  sync();

  const save = saveButton(async () => {
    const patch = {
      engine: engine.value,
      voice: voice.value,
      rate: rate.value.trim() || "+0%",
      volume: volume.value.trim() || "+0%",
      openai: {
        base_url: oBase.value.trim(),
        model: oModel.value.trim(),
        voice: oVoice.value,
      },
    };
    if (oKey.value.trim()) patch.openai.api_key = oKey.value.trim();
    await savePatch({ tts: patch }, save, message);
  });

  const test = el("button", { class: "btn", type: "button", text: "试一下" });
  test.addEventListener("click", async () => {
    test.disabled = true;
    message.className = "hint";
    message.textContent = "正在真合成一句…";
    try {
      const res = await post("/api/config/test-tts", {});
      message.className = "hint ok";
      message.textContent = `合成成功：${res.voice} · ${res.bytes} 字节。先保存，再回来试听。`;
    } catch (err) {
      message.className = "hint bad";
      message.textContent = err.hint ? `${err.message} —— ${err.hint}` : err.message;
    } finally {
      test.disabled = false;
    }
  });

  box.append(
    field("引擎", engine),
    field("Edge 音色", voice),
    el("div", { class: "grid2" }, [
      field("语速", rate, "比如 +10% 就是快一点。"),
      field("音量", volume, "Edge 引擎有效；大模型语音按服务商能力走。"),
    ]),
    openaiFields,
    el("div", { class: "row" }, [save, test, message]),
  );
  return box;
}

/* ------------------------------------------------------------------ 配图 */

function imagesCard() {
  const cfg = (state.config && state.config.images) || {};
  const llm = cfg.llm || {};
  const sources = asList(state.catalog && state.catalog.image_sources);
  const presets = asList(state.catalog && state.catalog.image_presets);
  const box = card("场景配图", "给例句配一张实拍感的图，翻面时看一眼就抓住场景。");
  const message = el("div", { class: "hint", text: "" });

  const source = selectOf(sources.map((s) => ({ id: s.id, label: s.label })), cfg.source || "both");
  const cap = input(cfg.max_per_lesson ?? 8, { type: "number" });
  const preset = selectOf(presets, llm.preset || "");
  const base = input(llm.base_url, { placeholder: "https://open.bigmodel.cn/api/paas/v4", mono: true });
  const model = input(llm.model, { placeholder: "cogview-3-flash", mono: true });
  const key = input("", {
    type: "password",
    placeholder: llm.api_key_set ? "已经存了，留空不改" : "画图服务需要 key",
  });

  preset.addEventListener("change", () => {
    const item = presets.find((one) => one.id === preset.value);
    if (!item) return;
    base.value = item.base_url || "";
    if (item.model) model.value = item.model;
  });

  const save = saveButton(async () => {
    const patch = {
      source: source.value,
      max_per_lesson: Number(cap.value || 0),
      llm: {
        preset: preset.value,
        base_url: base.value.trim(),
        model: model.value.trim(),
      },
    };
    if (key.value.trim()) patch.llm.api_key = key.value.trim();
    await savePatch({ images: patch }, save, message);
  });

  const test = el("button", { class: "btn", type: "button", text: "试一下" });
  test.addEventListener("click", async () => {
    test.disabled = true;
    message.className = "hint";
    message.textContent = "正在真找一张图…（检索几秒，画图十几秒）";
    try {
      const res = await post("/api/config/test-image", {});
      message.className = "hint ok";
      message.textContent = `拿到了：${res.name} · ${res.bytes} 字节。`;
    } catch (err) {
      message.className = "hint bad";
      message.textContent = err.hint ? `${err.message} —— ${err.hint}` : err.message;
    } finally {
      test.disabled = false;
    }
  });

  box.append(
    field("图从哪来", source, "检索 + 画图并行：谁先回来用谁，所以两个都最好填好。"),
    field("每个包配几张", cap, "0 = 每条例句都配。一包配一张也能起到定位作用。"),
    field("画图厂商", preset),
    field("画图 · 地址", base),
    field("画图 · 模型", model),
    field("画图 · key", key),
    el("div", { class: "row" }, [save, test, message]),
  );
  return box;
}

/* ------------------------------------------------------------------ 学习量 */

function studyCard() {
  const cfg = (state.config && state.config.study) || {};
  const channels = (state.config && state.config.channels) || {};
  const box = card("学习量", "这些数字决定「今天有多少东西可练」。");
  const message = el("div", { class: "hint", text: "" });

  const examples = input(cfg.examples_per_lesson ?? 8, { type: "number" });
  const newPerDay = input(cfg.new_per_day ?? 5, { type: "number" });
  const variants = selectOf(
    [
      { id: "rotate", label: "每次换一种说法" },
      { id: "fixed", label: "固定用第一种" },
    ],
    cfg.zh_variant_policy || "rotate",
  );

  const channelRow = el("div", { class: "chips" });
  const channelIds = Object.keys(CHANNEL_LABEL);
  for (const id of channelIds) {
    const button = el("button", {
      class: "chip pick",
      type: "button",
      text: CHANNEL_LABEL[id],
      "aria-pressed": channels[id] ? "true" : "false",
    });
    button.addEventListener("click", () => {
      const next = button.getAttribute("aria-pressed") !== "true";
      button.setAttribute("aria-pressed", next ? "true" : "false");
    });
    channelRow.appendChild(button);
  }

  const save = saveButton(async () => {
    const patch = {
      study: {
        examples_per_lesson: Number(examples.value || 8),
        new_per_day: Number(newPerDay.value || 5),
        zh_variant_policy: variants.value,
      },
      channels: Object.fromEntries(
        [...channelRow.children].map((button, index) => [
          channelIds[index],
          button.getAttribute("aria-pressed") === "true",
        ]),
      ),
    };
    await savePatch(patch, save, message);
  });

  box.append(
    el("div", { class: "grid2" }, [
      field("每个包几条例句", examples, "8 条是默认值：够多样，又不会贵。"),
      field("每天几张新卡", newPerDay, "新手 5 张/天，一周后按手感调。"),
    ]),
    field("中文说法", variants),
    field("练哪些通道", channelRow),
    el("div", { class: "row" }, [save, message]),
  );
  return box;
}

/* ------------------------------------------------------------------ 发音 */

function pronounceCard() {
  const cfg = (state.config && state.config.pronounce) || {};
  const asr = cfg.asr || {};
  const box = card(
    "发音训练",
    "跟读录音当场评分：时长、停顿、音量一定测；配了转写服务才会判「词对不对」。",
  );
  const message = el("div", { class: "hint", text: "" });

  const enabled = el("input", { type: "checkbox", checked: cfg.enabled !== false });
  const mode = selectOf(
    [
      { id: "auto", label: "有转写就用，没有就只测声学" },
      { id: "asr", label: "只用转写（没配就报错）" },
      { id: "acoustic", label: "只测声学（不发录音到任何服务）" },
    ],
    cfg.mode || "auto",
  );
  const keep = el("input", { type: "checkbox", checked: cfg.save_recordings !== false });
  const base = input(asr.base_url, { placeholder: "https://api.openai.com/v1", mono: true });
  const model = input(asr.model, { placeholder: "whisper-1", mono: true });
  const key = input("", {
    type: "password",
    placeholder: asr.api_key_set ? "已经存了，留空不改" : "转写服务需要 key",
  });

  const note = el("div", { class: "hint", text: "" });
  get("/api/pronounce")
    .then((info) => {
      state.pronounce = info;
      note.textContent = info.asr_ready
        ? `现在：词级对比（${info.asr_model}），一共存了 ${info.count} 段录音。`
        : `现在：只测时长、停顿和音量，一共存了 ${info.count} 段录音。`;
    })
    .catch(() => {
      note.textContent = "";
    });

  const save = saveButton(async () => {
    const patch = {
      enabled: enabled.checked,
      mode: mode.value,
      save_recordings: keep.checked,
      asr: {
        base_url: base.value.trim(),
        model: model.value.trim(),
        language: "en",
      },
    };
    if (key.value.trim()) patch.asr.api_key = key.value.trim();
    const res = await savePatch({ pronounce: patch }, save, message);
    if (res) state.pronounce = null;
  });

  const test = el("button", { class: "btn", type: "button", text: "试一下转写" });
  test.addEventListener("click", async () => {
    test.disabled = true;
    message.className = "hint";
    message.textContent = "合成一句 → 发给转写服务 → 比对文字…";
    try {
      const res = await post("/api/pronounce/check", {});
      message.className = "hint ok";
      message.textContent = res.ok
        ? `通了：认出来是「${res.text || "（空）"}」，用了 ${res.seconds} 秒。`
        : "服务有回应，但文字对不上。换个模型试试。";
    } catch (err) {
      message.className = "hint bad";
      message.textContent = err.hint ? `${err.message} —— ${err.hint}` : err.message;
    } finally {
      test.disabled = false;
    }
  });

  box.append(
    note,
    field(
      "打开跟读",
      el("label", { class: "switch" }, [enabled, el("span", { text: "口语卡上出现录音按钮" })]),
    ),
    field("怎么判", mode),
    field(
      "留录音",
      el("label", { class: "switch" }, [
        keep,
        el("span", { text: "存下来，以后能听自己两周前的声音" }),
      ]),
    ),
    el("details", { class: "sub" }, [
      el("summary", { text: "转写服务（可选）" }),
      field("地址", base, "OpenAI 兼容的 /v1 地址。"),
      field("模型", model, "whisper-1，或服务商的语音识别模型。"),
      field("key", key),
      el("p", {
        class: "hint",
        text: "只说声学也能练：它测「说完整了没有、哪里卡住、音量够不够」。判不了词，但不会给你假的满分。",
      }),
    ]),
    el("div", { class: "row" }, [save, test, message]),
  );
  return box;
}

/* ------------------------------------------------------------------ 自检 */

function selfcheckCard() {
  const box = card("自检", "真的跑一遍：能不能写数据、模型通不通、语音合成得出来吗。");
  const host = el("div", { class: "selfcheck" }, [el("div", { class: "hint", text: "还没跑。" })]);
  const run = el("button", { class: "btn", type: "button", text: "跑一遍自检" });
  run.addEventListener("click", async () => {
    run.disabled = true;
    host.replaceChildren(loading("正在检查…（语音合成那一步要几秒）"));
    try {
      const report = await get("/api/selfcheck");
      host.replaceChildren(drawSelfCheck(report));
    } catch (err) {
      host.replaceChildren(errorBlock(err, () => run.click()));
    } finally {
      run.disabled = false;
    }
  });
  box.append(run, host);
  return box;
}

function drawSelfCheck(report) {
  const wrap = el("div", {});
  const overall = { ok: "都正常", warn: "能用，但有几处要注意", fail: "有问题，先修这些" };
  wrap.appendChild(
    el("div", { class: "row" }, [
      chip(
        overall[report.overall] || report.overall,
        report.overall === "fail" ? "bad" : report.overall === "warn" ? "warn" : "ok",
      ),
      el("span", { class: "hint", text: `v${report.version} · pid ${report.pid}` }),
    ]),
  );
  const table = el("table", { class: "table" });
  table.appendChild(
    el("thead", {}, [
      el("tr", {}, [
        el("th", { text: "检查项" }),
        el("th", { text: "结果" }),
        el("th", { text: "详情" }),
      ]),
    ]),
  );
  const body = el("tbody");
  for (const item of report.items || []) {
    body.appendChild(
      el("tr", {}, [
        el("td", { text: item.name }),
        el("td", {}, [
          chip(
            item.level === "ok" ? "好" : item.level === "warn" ? "注意" : "有问题",
            item.level === "ok" ? "ok" : item.level === "warn" ? "warn" : "bad",
          ),
        ]),
        el("td", {}, [
          el("div", { text: item.detail || "" }),
          item.hint ? el("div", { class: "hint", text: item.hint }) : null,
        ]),
      ]),
    );
  }
  table.appendChild(body);
  wrap.appendChild(table);
  return wrap;
}

/* ------------------------------------------------------------------ 数据 */

function dataCard() {
  const box = card("数据", "整个程序的数据（数据库、音频、配图、录音）都在一个目录里。");
  const host = el("div", { class: "kv" }, [loading("读目录…")]);
  const message = el("div", { class: "hint", text: "" });

  async function refresh() {
    try {
      const info = await get("/api/data-location");
      host.replaceChildren();
      const sourceLabel =
        {
          ETUDE_DATA_DIR: "环境变量（这里改动不会生效）",
          bootstrap: "之前搬过一次",
          default: "默认位置",
        }[info.source] || info.source;
      const rows = [
        ["现在用哪个目录", info.current],
        ["怎么定的", sourceLabel],
        ["占了多大", `${humanSize(info.bytes)} · ${info.files} 个文件`],
        ["这个盘还剩", info.free_bytes >= 0 ? humanSize(info.free_bytes) : "读不到"],
        ["默认位置", info.default],
      ];
      for (const [key, value] of rows) {
        host.append(el("dt", { text: key }), el("dd", { class: "mono", text: String(value) }));
      }
      if (info.locked_by_env) {
        message.className = "hint bad";
        message.textContent = "环境变量 ETUDE_DATA_DIR 锁住了数据目录，这一页的搬动不会生效。";
      }
    } catch (err) {
      host.replaceChildren(errorBlock(err, refresh));
    }
  }
  refresh();

  const path = input("", { placeholder: "搬到哪里（留空 = 搬回默认位置）", mono: true });
  const move = el("button", { class: "btn", type: "button", text: "搬过去" });
  move.addEventListener("click", async () => {
    const copy = window.confirm(
      "要一起把现有数据复制过去吗？\n\n确定 = 复制（原目录留着）\n取消 = 只换指针，数据自己搬",
    );
    move.disabled = true;
    try {
      const res = await post("/api/data-location", { path: path.value.trim(), copy });
      if (res.ok) {
        message.className = "hint ok";
        message.textContent = res.restart_required
          ? "记下了。关掉程序再打开，之后就用新目录了。"
          : "记下了。";
        await refresh();
      } else {
        message.className = "hint bad";
        message.textContent = [res.error, res.hint].filter(Boolean).join(" —— ");
      }
    } catch (err) {
      message.className = "hint bad";
      message.textContent = err.hint ? `${err.message} —— ${err.hint}` : err.message;
    } finally {
      move.disabled = false;
    }
  });

  const backup = el("button", { class: "btn", type: "button", text: "备份数据库" });
  backup.addEventListener("click", async () => {
    backup.disabled = true;
    try {
      const res = await post("/api/backup", {});
      message.className = "hint ok";
      message.textContent = `备份好了：${res.path}（${humanSize(res.bytes)}）`;
      await loadBackups();
    } catch (err) {
      message.className = "hint bad";
      message.textContent = err.message;
    } finally {
      backup.disabled = false;
    }
  });

  // 备份列表 + 恢复。恢复是会覆盖当前库的操作，所以按钮是 ghost，确认框里写清后果。
  const backupsHost = el("div", { class: "backups" });

  async function loadBackups() {
    try {
      const data = await get("/api/backups");
      const list = data.backups || [];
      backupsHost.replaceChildren();
      if (!list.length) {
        backupsHost.append(el("div", { class: "hint", text: "还没有备份。备份是 sqlite 的一整份快照，几 MB。" }));
        return;
      }
      backupsHost.append(sectionHead(`已有的备份（${list.length} 个）`));
      for (const item of list) {
        const restore = el("button", { class: "btn ghost sm", type: "button", text: "恢复这一份" });
        restore.addEventListener("click", async () => {
          if (
            !window.confirm(
              `用「${item.name}」覆盖现在的数据？\n\n现在的库会先自动备份一份（before-restore-…），但训练进度会回到这份备份的时间点。`,
            )
          ) {
            return;
          }
          restore.disabled = true;
          try {
            await post("/api/backups/restore", { name: item.name });
            // 恢复之后**整页重载**：后端已经换了库，界面里那一堆缓存（统计、
            // 训练队列、进度）全是旧数据。让用户自己去刷新一次，多半就是
            // 「看着恢复了、点进去还是旧的」。
            toast("恢复好了，正在重新载入…", "ok");
            window.setTimeout(() => window.location.reload(), 700);
          } catch (err) {
            toast(err.message || "恢复失败。", "bad");
          } finally {
            restore.disabled = false;
          }
        });
        backupsHost.append(
          el("div", { class: "backup-row" }, [
            el("span", { class: "mono backup-name", text: item.name }),
            el("span", { class: "hint", text: `${humanSize(item.bytes)} · ${humanWhen(item.at)}` }),
            el("span", { class: "grow" }),
            restore,
          ]),
        );
      }
    } catch (err) {
      backupsHost.replaceChildren(el("div", { class: "hint bad", text: err.message }));
    }
  }

  const openers = el("div", { class: "row wrap" });
  for (const [which, label] of [
    ["data", "打开数据目录"],
    ["logs", "打开日志"],
    ["recordings", "打开录音"],
    ["backups", "打开备份"],
  ]) {
    const button = el("button", { class: "btn ghost sm", type: "button", text: label });
    button.addEventListener("click", async () => {
      try {
        const res = await post("/api/open-folder", { which });
        toast(`打开了：${res.path}`, "ok");
      } catch (err) {
        toast(err.message || "打不开。", "bad");
      }
    });
    openers.append(button);
  }

  loadBackups();

  box.append(
    host,
    openers,
    field("搬到别处", path),
    el("div", { class: "row" }, [move, backup, message]),
    backupsHost,
  );
  return box;
}
