/*
 * 训练：一次一张卡，五个通道各看各的。
 *
 * 这一页是 Etude 的全部意义所在，所以几条规矩写在这里（改之前先读）：
 *
 * - **评分键永远不 disable。** 没翻面就按「正常」，先翻面再说一声，
 *   而不是让按钮变灰 —— 变灰的按钮在键盘上按下去什么都不会发生，
 *   用户会以为程序卡住了。
 * - **翻面之后才给中文。** 中文说法每次随机一种，同一张卡的两面必须对得上
 *   （所以说法在取卡时就定好，存在 state.variant 里）。
 * - **卡片 id 绝不自己推算。** 从训练包详情点「练这条」时，id 一定来自
 *   后端返回的 cards 列表，不能拿「例句序号 × 5 + 通道序号」猜。
 */

import { get, post } from "../core/api.js";
import { canSpeak, play, speak } from "../core/audio.js";
import { el, paintIcons } from "../core/dom.js";
import {
  CHANNEL_LABEL,
  CHANNEL_TIPS,
  GRADE_HINT,
  GRADE_LABEL,
  audioUrl,
  imageUrl,
  state,
} from "../core/store.js";
import { banner, emptyBlock, hideBanner, setPage, toast } from "../core/ui.js";
import { abortRecording, pronouncePanel } from "./pronounce.js";

const GRADE_NUM = ["1", "2", "3", "4"];

function emptySession() {
  return { done: 0, skipped: 0, history: [], startedAt: Date.now() };
}

export async function loadQueue() {
  state.queue = [];
  state.cursor = 0;
  state.current = null;
  state.revealed = false;
  state.session = emptySession();
  try {
    const data = await get("/api/queue?limit=40");
    state.queue = data.cards || [];
    state.stats = data.stats || state.stats;
  } catch (err) {
    banner("bad", `取训练队列时出错：${err.message}`);
  }
}

/** 训练页入口。 */
export async function render(host) {
  setPage({ title: "训练", sub: "一次一张卡，五个通道各看各的", actions: [] });
  host.replaceChildren();

  if (!state.queue.length && !state.current) {
    // 这一页自己去取队列：从侧栏点进来、刷新页面、地址栏直接打字，
    // 都得有卡可练 —— 不能指望「上一页顺手帮我取过了」。
    host.appendChild(el("div", { class: "hint", text: "取训练队列…" }));
    await loadQueue();
    host.replaceChildren();
    if (!state.queue.length) {
      host.appendChild(sessionDone());
      paintIcons(host);
      return;
    }
  }

  if (!state.current) {
    host.appendChild(el("div", { class: "hint", text: "取卡片…" }));
    const ok = await loadCurrent();
    if (!ok) {
      host.replaceChildren(sessionDone());
      paintIcons(host);
      return;
    }
    host.replaceChildren();
  }

  host.appendChild(stage());
  if (state.revealed) host.appendChild(answerBlock());
  host.appendChild(graderBlock());
  host.appendChild(footerBlock());
  paintIcons(host);
}

/* ------------------------------------------------------------------ 取卡 */

async function loadCurrent() {
  while (state.cursor < state.queue.length) {
    const item = state.queue[state.cursor];
    try {
      const payload = await get(`/api/cards/${item.id}`);
      state.current = payload;
      state.revealed = false;
      state.typed = "";
      state.typedResult = null;
      state.variant = pickVariant(payload.example);
      state.cardSeconds = 0;
      state.cardStartedAt = Date.now();
      return true;
    } catch (err) {
      // 卡片被删了或被并掉了：跳过，不要卡在这一张上。
      state.session.skipped += 1;
      state.cursor += 1;
    }
  }
  return false;
}

function pickVariant(example) {
  if (!example) return "";
  const variants = Array.isArray(example.zh_variants) ? example.zh_variants.filter(Boolean) : [];
  if (!variants.length) return example.scene_zh || "";
  const policy =
    (state.config && state.config.study && state.config.study.zh_variant_policy) || "rotate";
  if (policy === "fixed") return example.scene_zh || variants[0];
  return variants[Math.floor(Math.random() * variants.length)];
}

/* ------------------------------------------------------------------ 舞台 */

function stage() {
  const { card, lesson, example } = state.current;
  const channel = card.channel;
  const box = el("section", { class: "stage" });

  box.appendChild(
    el("div", { class: "stage-top" }, [
      el("span", { class: `chan ${channel}`, text: CHANNEL_LABEL[channel] || channel }),
      el("span", { class: "target", text: lesson.target || "" }),
      el("span", { class: "grow" }),
      el("span", { class: "counter mono", text: progressText() }),
    ]),
  );

  const body = el("div", { class: "prompt" });
  if (channel === "read") {
    body.appendChild(el("div", { class: "prompt-en", text: example ? example.sentence : "" }));
    body.appendChild(el("div", { class: "task", text: CHANNEL_TIPS.read }));
  } else if (channel === "listen") {
    body.appendChild(
      el("div", { class: "listen-row" }, [
        playButton(example, { big: true }),
        el("div", { class: "col" }, [
          el("div", { class: "task", text: "只听声音。不要看字，也不要先想中文。" }),
          el("div", { class: "hint", text: CHANNEL_TIPS.listen }),
        ]),
      ]),
    );
  } else if (channel === "speak") {
    body.appendChild(sceneBlock(example));
    body.appendChild(
      el("div", { class: "task", text: "看场景，用英文说出来（真的出声）。说完再翻面。" }),
    );
  } else if (channel === "type") {
    body.appendChild(sceneBlock(example));
    body.appendChild(el("div", { class: "task", text: "看场景，把英文打出来。" }));
    body.appendChild(typingBlock());
  } else {
    body.appendChild(el("div", { class: "prompt-en", text: example ? example.sentence : "" }));
    if (state.current.swaps && state.current.swaps.length) {
      body.appendChild(swapsBlock(state.current.swaps));
    }
    body.appendChild(el("div", { class: "task", text: CHANNEL_TIPS.build }));
    body.appendChild(
      el("textarea", {
        class: "input build-input",
        rows: "2",
        id: "build-input",
        placeholder: "写一句新的话（换掉一个成分，意思还得成立）",
      }),
    );
  }
  box.appendChild(body);

  if (channel === "speak") {
    box.appendChild(
      pronouncePanel({
        cardId: state.current.card.id,
        audioSrc: example && example.audio_path ? audioUrl(example.audio_path) : "",
        open: true,
      }),
    );
  }
  return box;
}

function progressText() {
  const total = state.session.done + Math.max(0, state.queue.length - state.cursor);
  return `${state.session.done} / ${Math.max(total, 1)}`;
}

function sceneBlock(example) {
  const box = el("div", { class: "scene" });
  box.appendChild(
    el("div", {
      class: "prompt-scene",
      text: state.variant || (example && example.scene_zh) || "",
    }),
  );
  if (example && example.scene_image) {
    const src = imageUrl(example.scene_image);
    const img = el("img", { src, alt: "", class: "scene-img", loading: "lazy" });
    img.addEventListener("error", () =>
      img.replaceWith(el("div", { class: "imgfail", text: "这张图打不开了。" })),
    );
    box.appendChild(img);
  }
  return box;
}

function swapsBlock(swaps) {
  return el(
    "div",
    { class: "swaps" },
    swaps.slice(0, 2).map((swap) =>
      el("div", { class: "swap" }, [
        el("span", { class: "role", text: swap.role || "" }),
        el("span", { class: "from", text: swap.original || "" }),
        el("span", { class: "arrow", text: "→" }),
        el("span", { class: "to", text: (swap.candidates || []).slice(0, 3).join(" / ") }),
      ]),
    ),
  );
}

function playButton(example, { big = false, label = "" } = {}) {
  const button = el(
    "button",
    { class: `btn play${big ? " big" : ""}`, type: "button", title: "播放这句的语音" },
    [
      el("span", { class: "ico", dataset: { icon: "play" } }),
      el("span", { text: label || (big ? "播放" : "听") }),
    ],
  );
  button.addEventListener("click", async () => {
    const name = example && example.audio_path;
    if (name) {
      try {
        button.classList.add("is-playing");
        await play(audioUrl(name), `card:${example.id}`);
        button.classList.remove("is-playing");
      } catch (err) {
        button.classList.remove("is-playing");
        if (canSpeak() && example && example.sentence) {
          speak(example.sentence);
          toast("这条语音文件放不出来，先用系统语音念一遍。", "warn");
        } else {
          toast(err.message, "bad");
        }
      }
      return;
    }
    if (canSpeak() && example && example.sentence) {
      speak(example.sentence);
      toast("这条还没有模型语音，先用系统语音念一遍。", "warn");
      return;
    }
    toast("这条没有语音，也念不了。去训练包里补一下语音。", "bad");
  });
  return button;
}

function typingBlock() {
  const input = el("input", {
    class: "input",
    type: "text",
    id: "type-input",
    placeholder: "打英文，不用标点",
    autocomplete: "off",
    autocapitalize: "off",
    spellcheck: "false",
  });
  const button = el("button", { class: "btn primary", type: "button", text: "对比" });
  const host = el("div", { class: "diff-host" });
  const run = () => {
    const typed = input.value.trim();
    if (!typed) {
      input.focus();
      return;
    }
    state.typed = typed;
    state.typedResult = diffWords(state.current.example.sentence, typed);
    host.replaceChildren(diffBlock(state.typedResult));
    reveal();
  };
  button.addEventListener("click", run);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      run();
    }
  });
  // 切卡后焦点直接落在输入框：打字通道不该还要先点一下。
  window.setTimeout(() => {
    const node = document.getElementById("type-input");
    if (node) node.focus();
  }, 30);
  return el("div", { class: "typerow" }, [input, button, host]);
}

function diffWords(target, typed) {
  const normalize = (text) =>
    String(text || "")
      .toLowerCase()
      .replace(/[‘’]/g, "'")
      .replace(/[^a-z0-9'\s]/g, " ")
      .split(/\s+/)
      .filter(Boolean);
  const want = normalize(target);
  const got = normalize(typed);
  const rows = want.map((word, index) => ({
    word,
    hit: got[index] === word,
    typed: got[index] || "",
  }));
  const extra = got.slice(want.length);
  return { rows, extra, hits: rows.filter((row) => row.hit).length, total: want.length };
}

function diffBlock(result) {
  // 两层：外层 .diff 管「这一整块 + 一句结论」，内层 .diff-host 是能换行的词流。
  // 之前把词直接塞进 .diff 里，而 CSS 里 .diff 是竖排的，于是一行一个词 ——
  // 打一句 9 个词的话，对比区被撑成 9 行，翻面后整页都在跳。
  const box = el("div", { class: "diff" });
  const words = el("div", { class: "diff-host" });
  for (const row of result.rows) {
    words.appendChild(
      el("span", {
        class: `w ${row.hit ? "hit" : "miss"}`,
        text: row.word,
        title: row.typed ? `你打的：${row.typed}` : "没打",
      }),
    );
  }
  for (const word of result.extra) {
    words.appendChild(el("span", { class: "w extra", text: word }));
  }
  box.appendChild(words);
  box.appendChild(
    el("div", {
      class: "hint",
      text: `对上 ${result.hits} / ${result.total} 个词。差的那几个，是这次要记的。`,
    }),
  );
  return box;
}

/* ------------------------------------------------------------------ 答案 */

function answerBlock() {
  const { lesson, example, card } = state.current;
  const box = el("section", { class: "answer" });
  const sentence = example ? example.sentence : "";
  box.appendChild(
    el("div", { class: "answer-en" }, [
      el("span", { class: "text", text: sentence }),
      el("span", { class: "grow" }),
      playButton(example, { label: "听" }),
    ]),
  );
  if (state.typedResult) {
    box.appendChild(diffBlock(state.typedResult));
  }
  if (lesson.gloss) {
    box.appendChild(
      el("div", { class: "gloss" }, [
        el("span", { class: "lab", text: "英英释义" }),
        el("span", { text: lesson.gloss }),
      ]),
    );
  }
  if (lesson.ipa) {
    box.appendChild(el("div", { class: "ipa mono", text: `/${lesson.ipa}/` }));
  }
  if (example && example.scene_en) {
    box.appendChild(
      el("div", { class: "scene-en" }, [
        el("span", { class: "lab", text: "场景" }),
        el("span", { text: example.scene_en }),
      ]),
    );
  }
  box.appendChild(
    el("div", { class: "scene-zh" }, [
      el("span", { class: "lab", text: "这样说的时候" }),
      el("span", { text: state.variant || (example && example.scene_zh) || "" }),
    ]),
  );
  if (example && Array.isArray(example.zh_variants) && example.zh_variants.length > 1) {
    box.appendChild(
      el(
        "div",
        { class: "variants" },
        example.zh_variants.map((text) =>
          el("span", { class: `variant${text === state.variant ? " current" : ""}`, text }),
        ),
      ),
    );
  }
  if (card.channel === "speak" || card.channel === "listen") {
    // 听力卡翻面之后，把声音再放一遍是自然的下一步。
    window.setTimeout(() => {
      if (example && example.audio_path) play(audioUrl(example.audio_path)).catch(() => {});
    }, 120);
  }
  return box;
}

/* ------------------------------------------------------------------ 评分 */

function graderBlock() {
  const preview = state.current.srs_preview || {};
  const grid = el("div", { class: "grader" });
  for (let g = 0; g < 4; g += 1) {
    const info = preview[g] || {};
    const pending = !state.revealed && g !== 0;
    const button = el(
      "button",
      {
        class: `grade g${g}${pending ? " is-pending" : ""}`,
        type: "button",
        title: pending ? "先看一眼答案（按空格），再给自己打分" : "",
      },
      [
        el("span", { class: "num", text: GRADE_NUM[g] }),
        el("b", { class: "label", text: GRADE_LABEL[g] }),
        el("span", { class: "when", text: info.human || "" }),
        el("span", { class: "need", text: "先看答案" }),
      ],
    );
    button.addEventListener("click", () => grade(g));
    grid.appendChild(button);
  }
  return el("section", { class: "grader-wrap" }, [
    grid,
    el("div", { class: "hint", text: GRADE_HINT }),
  ]);
}

function footerBlock() {
  const row = el("section", { class: "stage-foot" });

  const prev = el("button", { class: "btn ghost", type: "button" }, [
    el("span", { class: "ico", dataset: { icon: "left" } }),
    el("span", { text: "上一张" }),
  ]);
  prev.addEventListener("click", () => previous());

  const revealBtn = el("button", {
    class: "btn",
    type: "button",
    text: state.revealed ? "收起答案" : "看答案（空格）",
  });
  revealBtn.addEventListener("click", () => (state.revealed ? unreveal() : reveal()));

  const skip = el("button", { class: "btn ghost", type: "button" }, [
    el("span", { class: "ico", dataset: { icon: "right" } }),
    el("span", { text: "这张先跳过" }),
  ]);
  skip.addEventListener("click", () => next());

  row.append(prev, revealBtn, skip);

  if (state.session.history.length) {
    const undo = el("button", { class: "btn ghost", type: "button" }, [
      el("span", { class: "ico", dataset: { icon: "undo" } }),
      el("span", { text: "撤销上一次评分" }),
    ]);
    undo.addEventListener("click", () => undoLast());
    row.appendChild(undo);
  }
  return row;
}

function currentCardId() {
  return state.current && state.current.card ? state.current.card.id : 0;
}

export async function grade(level) {
  const cardId = currentCardId();
  if (!cardId) return;
  if (!state.revealed) {
    // 不挡住评分：先给看答案，再说一句「已经记成了正常」。
    reveal();
    if (level !== 0) {
      toast("已经记下了。下次先看答案再打分更准。", "warn");
    }
  }
  const seconds = state.cardStartedAt ? (Date.now() - state.cardStartedAt) / 1000 : 0;
  try {
    // 档位就是后端的 0..3：没想起来 / 吃力 / 正常 / 秒答
    await post(`/api/cards/${cardId}/grade`, { grade: level, seconds });
  } catch (err) {
    toast(`记分失败：${err.message}`, "bad");
    return;
  }
  state.session.done += 1;
  state.session.history.push(cardId);
  hideBanner();
  await advance();
}

async function advance() {
  state.cursor += 1;
  state.current = null;
  state.revealed = false;
  state.typedResult = null;
  abortRecording();
  if (state.cursor >= state.queue.length) {
    // 一批练完了：问问后端还有没有。
    try {
      const data = await get("/api/queue?limit=40");
      state.queue = data.cards || [];
      state.cursor = 0;
      state.stats = data.stats || state.stats;
    } catch (err) {
      state.queue = [];
    }
  }
  const app = document.getElementById("app");
  if (app) await render(app);
  if (typeof window.__etudeRefreshSide === "function") window.__etudeRefreshSide();
}

export function next() {
  if (state.queue.length) state.session.skipped += 1;
  advance();
}

export function previous() {
  if (!state.session.history.length) {
    toast("这是今天第一张。", "warn");
    return;
  }
  state.session.history.pop();
  state.cursor = Math.max(0, state.cursor - 1);
  state.current = null;
  state.revealed = false;
  const app = document.getElementById("app");
  if (app) render(app);
}

export function reveal() {
  if (state.revealed) return;
  state.revealed = true;
  const app = document.getElementById("app");
  if (app) render(app);
}

export function unreveal() {
  state.revealed = false;
  const app = document.getElementById("app");
  if (app) render(app);
}

async function undoLast() {
  const cardId = state.session.history[state.session.history.length - 1];
  if (!cardId) return;
  try {
    await post(`/api/cards/${cardId}/undo`, {});
  } catch (err) {
    toast(`撤销失败：${err.message}`, "bad");
    return;
  }
  state.session.history.pop();
  state.session.done = Math.max(0, state.session.done - 1);
  state.cursor = Math.max(0, state.cursor - 1);
  state.current = null;
  state.revealed = false;
  const app = document.getElementById("app");
  if (app) await render(app);
  if (typeof window.__etudeRefreshSide === "function") window.__etudeRefreshSide();
}

/* ------------------------------------------------------------------ 空 / 完成 */

function sessionDone() {
  const stats = state.stats || {};
  const box = el("section", { class: "done-page" });
  const due = Number(stats.due_now || 0);
  const fresh = Number(stats.introduced || 0);
  const total = Number(stats.cards || 0);
  const title = total === 0 ? "还没有卡片" : "今天这一批练完了";
  const text =
    total === 0
      ? "先去「训练包」建一个词，生成之后卡片会自己出现在这里。"
      : due > 0
        ? `还有 ${due} 张今天到期。再开一批继续。`
        : `到期都清了。现在一共 ${total} 张卡，学过 ${fresh} 张。`;
  box.appendChild(
    emptyBlock({
      art: total === 0 ? "spark" : "check",
      title,
      text,
      actions: [
        el("button", {
          class: "btn primary",
          type: "button",
          text: total === 0 ? "去建训练包" : "再来一批",
          onClick: async () => {
            await loadQueue();
            const app = document.getElementById("app");
            if (app) render(app);
          },
        }),
        el("button", {
          class: "btn ghost",
          type: "button",
          text: "看看卡片",
          onClick: () => {
            location.hash = "#/cards";
          },
        }),
      ],
    }),
  );
  const numbers = el("div", { class: "stat-row" });
  for (const [label, value] of [
    ["今天练过", state.session.done],
    ["跳过", state.session.skipped],
    ["今天到期", due],
    ["卡片总数", total],
  ]) {
    numbers.appendChild(
      el("div", { class: "stat" }, [
        el("b", { text: String(value) }),
        el("span", { text: label }),
      ]),
    );
  }
  box.appendChild(numbers);
  return box;
}

/* ------------------------------------------------------------------ 键盘 */

export function handleKey(event) {
  const tag = (event.target && event.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return false;
  if (!state.current) return false;
  if (event.key === " ") {
    event.preventDefault();
    state.revealed ? unreveal() : reveal();
    return true;
  }
  if (event.key >= "1" && event.key <= "4") {
    event.preventDefault();
    grade(Number(event.key) - 1);
    return true;
  }
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    previous();
    return true;
  }
  if (event.key === "ArrowRight") {
    event.preventDefault();
    next();
    return true;
  }
  if (event.key === "z" || event.key === "Z") {
    event.preventDefault();
    undoLast();
    return true;
  }
  return false;
}

/** 训练页的侧栏块：本次进度 + 快捷键。 */
export function sideBlock() {
  const box = el("div", { class: "side-session" });
  const done = state.session.done || 0;
  const left = Math.max(0, state.queue.length - state.cursor);
  box.appendChild(
    el("div", { class: "tally" }, [
      el("b", { text: String(done) }),
      el("span", { text: `本次已练 · 还剩 ${left}` }),
    ]),
  );
  box.appendChild(
    el("div", { class: "hotkeys" }, [
      el("div", {}, [kbd("空格"), el("span", { text: "看答案 / 收起" })]),
      el("div", {}, [kbd("1–4"), el("span", { text: GRADE_LABEL.join(" / ") })]),
      el("div", {}, [kbd("← →"), el("span", { text: "上一张 / 跳过" })]),
      el("div", {}, [kbd("Z"), el("span", { text: "撤销上一次评分" })]),
    ]),
  );
  return box;
}

function kbd(text) {
  return el("kbd", { class: "kbd", text });
}
