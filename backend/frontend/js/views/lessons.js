/* 训练包列表 + 批量建包。
 *
 * 「批量」有两条路，都很实际：
 * 1. 你自己有一串词（课本、错题本）→ 贴进来，一行一个。
 * 2. 你只有主题（「面试」「厨房」）→ 让模型先起草一批目标，你看一眼再删改。
 * 第二条故意分两步：起草只花一次模型调用（几秒），
 * 而建 20 个包要十几分钟 —— 先看到清单再开工，比跑完才发现跑题便宜得多。
 */

import { del, get, post } from "../core/api.js";
import { el, humanWhen, paintIcons } from "../core/dom.js";
import { state } from "../core/store.js";
import {
  banner,
  chip,
  emptyBlock,
  errorBlock,
  field,
  hideBanner,
  loading,
  sectionHead,
  setPage,
  toast,
} from "../core/ui.js";
import { go } from "../core/router.js";

const KIND_LABEL = { word: "词", phrase: "固定说法", sentence: "整句" };
const timers = new Map();

export async function render(host) {
  setPage({
    title: "训练包",
    sub: "一个词 / 一句话，配 8 条不同场景的例句",
    actions: [
      el("button", {
        class: "btn",
        type: "button",
        text: "刷新",
        onClick: () => render(host),
      }),
    ],
  });
  host.replaceChildren(loading("读训练包…"));
  try {
    const data = await get("/api/lessons");
    state.lessons = data.lessons || [];
    state.stats = data.stats || state.stats;
  } catch (err) {
    host.replaceChildren(errorBlock(err, () => render(host)));
    return;
  }
  host.replaceChildren();
  host.appendChild(builder(host));
  host.appendChild(listBlock(host));
  paintIcons(host);
}

/* ------------------------------------------------------------------ 新建 */

function builder(host) {
  const box = el("section", { class: "card builder" });

  const input = el("input", {
    class: "input",
    type: "text",
    id: "target-input",
    placeholder: "一个词、一个固定说法，或者一整句英文",
    autocomplete: "off",
  });
  const message = el("div", { class: "hint", id: "create-msg", text: "" });
  const button = el("button", { class: "btn primary", type: "button", text: "建一个" });

  const create = async () => {
    const target = input.value.trim();
    if (!target) {
      input.focus();
      message.textContent = "先写一个词或者一句话。";
      return;
    }
    button.disabled = true;
    message.textContent = "正在建…";
    try {
      const res = await post("/api/lessons", { target, language: "en" });
      input.value = "";
      message.textContent = res.reused ? "这个词已经在生成了，正在等它。" : "已经在生成了，去列表里看进度。";
      watch(res.lesson_id, host);
      await refreshList(host);
    } catch (err) {
      message.textContent = err.message;
      if (err.hint) banner("bad", `${err.message}（${err.hint}）`);
    } finally {
      button.disabled = false;
    }
  };
  button.addEventListener("click", create);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      create();
    }
  });

  box.append(
    el("div", { class: "row" }, [input, button, importButton(host, message)]),
    message,
  );

  const details = el("details", { class: "batch" }, [
    el("summary", { text: "批量：一次贴一串词，或者给个主题让模型起草" }),
    batchPanel(host),
  ]);
  box.appendChild(details);
  return box;
}

/* 导入：接住自己导出的那份 JSON。
 * 藏在一个 file input 后面 —— 按钮长得像按钮，但点下去是系统文件框。 */
function importButton(host, message) {
  const file = el("input", {
    type: "file",
    accept: "application/json,.json",
    style: { display: "none" },
  });
  const button = el("button", {
    class: "btn ghost",
    type: "button",
    text: "导入 JSON",
    title: "选一个之前从这里导出的训练包文件",
  });
  button.addEventListener("click", () => file.click());
  file.addEventListener("change", async () => {
    const picked = file.files && file.files[0];
    if (!picked) return;
    button.disabled = true;
    message.textContent = "正在读文件…";
    try {
      const text = await picked.text();
      const payload = JSON.parse(text);
      const res = await post("/api/lessons/import", payload);
      message.textContent = `导进来了：${res.examples} 条例句。语音要在这儿重新生成。`;
      await refreshList(host);
      go("lessons", res.lesson_id);
    } catch (err) {
      message.textContent = err.message || "这个文件读不了。";
    } finally {
      button.disabled = false;
      file.value = "";
    }
  });
  return el("span", { class: "import-wrap" }, [file, button]);
}

function batchPanel(host) {
  const box = el("div", { class: "batch-body" });

  // 一、贴词
  const text = el("textarea", {
    class: "input",
    rows: "4",
    id: "batch-text",
    placeholder: "一行一个，也可以用逗号分隔：\nkitchen\nput the kettle on\nI am running late.",
  });
  const examples = el("select", { class: "input", id: "batch-examples" }, [
    el("option", { value: "0", text: "每包例句数：用设置里的值" }),
    el("option", { value: "6", text: "每包 6 条（快）" }),
    el("option", { value: "8", text: "每包 8 条（默认）" }),
    el("option", { value: "12", text: "每包 12 条（慢）" }),
  ]);
  const pasteButton = el("button", { class: "btn primary", type: "button", text: "按这份清单建包" });
  const pasteMsg = el("div", { class: "hint", text: "" });

  pasteButton.addEventListener("click", async () => {
    const raw = text.value.trim();
    if (!raw) {
      toast("先把词贴进来。", "warn");
      text.focus();
      return;
    }
    pasteButton.disabled = true;
    pasteMsg.textContent = "正在建…";
    try {
      const res = await post("/api/lessons/batch", {
        text: raw,
        language: "en",
        examples: Number(examples.value) || 0,
        skip_existing: true,
      });
      pasteMsg.textContent = describeBatch(res);
      text.value = "";
      for (const lesson of state.lessons || []) {
        if (lesson.status === "generating") watch(lesson.id, host);
      }
      await refreshList(host);
    } catch (err) {
      pasteMsg.textContent = err.message;
    } finally {
      pasteButton.disabled = false;
    }
  });

  // 二、主题 → 目标清单
  const theme = el("input", {
    class: "input",
    type: "text",
    id: "theme-input",
    placeholder: "主题，比如「在咖啡馆点单」「面试开场」",
  });
  const count = el("input", {
    class: "input narrow",
    type: "number",
    min: "1",
    max: "60",
    value: "12",
    id: "theme-count",
  });
  const draftButton = el("button", { class: "btn", type: "button", text: "起草清单" });
  const draftHost = el("div", { class: "draft" });

  draftButton.addEventListener("click", async () => {
    const value = theme.value.trim();
    if (!value) {
      theme.focus();
      return;
    }
    draftButton.disabled = true;
    draftHost.replaceChildren(el("div", { class: "hint", text: "正在起草…（一次模型调用，几秒）" }));
    try {
      const res = await post("/api/lessons/batch/draft", {
        theme: value,
        count: Math.max(1, Math.min(60, Number(count.value) || 12)),
      });
      drawDraft(res, draftHost, host);
    } catch (err) {
      draftHost.replaceChildren(errorBlock(err));
    } finally {
      draftButton.disabled = false;
    }
  });

  box.append(
    field("① 你的清单", text, "一行一个：词、固定说法、整句都行。已经有过的会跳过。"),
    el("div", { class: "row" }, [examples, pasteButton]),
    pasteMsg,
    el("hr", { class: "rule" }),
    field("② 或者只给主题", el("div", { class: "row" }, [theme, count, draftButton])),
    draftHost,
  );
  return box;
}

function describeBatch(res) {
  const bits = [];
  if (res.created) bits.push(`新建 ${res.created} 个`);
  if (res.retried) bits.push(`重试 ${res.retried} 个失败的`);
  if (res.skipped) bits.push(`跳过 ${res.skipped} 个已有/在生成的`);
  if (!bits.length) return "没有建。";
  return `${bits.join("，")}。`;
}

function drawDraft(res, host, pageHost) {
  const targets = res.targets || [];
  if (!targets.length) {
    host.replaceChildren(el("div", { class: "hint", text: "模型没给出清单，换个说法再试。" }));
    return;
  }
  const picked = new Set(targets.map((item) => item.target));
  const rows = targets.map((item) => {
    const box = el("input", { type: "checkbox", checked: true, value: item.target });
    box.addEventListener("change", () => {
      if (box.checked) picked.add(item.target);
      else picked.delete(item.target);
    });
    return el("label", { class: "draft-row" }, [
      box,
      el("span", { class: "draft-target", text: item.target }),
      chip(KIND_LABEL[item.kind] || item.kind || "词"),
      item.why ? el("span", { class: "hint grow", text: item.why }) : el("span", { class: "grow" }),
    ]);
  });

  const start = el("button", {
    class: "btn primary",
    type: "button",
    text: `建这 ${targets.length} 个`,
  });
  start.addEventListener("click", async () => {
    const chosen = [...picked];
    if (!chosen.length) {
      toast("一个都没选。", "warn");
      return;
    }
    start.disabled = true;
    start.textContent = "正在建…";
    try {
      const res2 = await post("/api/lessons/batch", {
        targets: chosen,
        language: "en",
        skip_existing: true,
      });
      toast(describeBatch(res2), "ok");
      host.replaceChildren(el("div", { class: "hint", text: describeBatch(res2) }));
      await refreshList(pageHost);
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      start.disabled = false;
      start.textContent = `建这 ${targets.length} 个`;
    }
  });

  host.replaceChildren(
    el("div", { class: "draft-head" }, [
      el("span", { class: "hint grow", text: res.note || `模型给了 ${targets.length} 条，勾掉不要的。` }),
      start,
    ]),
    el("div", { class: "draft-list" }, rows),
  );
  paintIcons(host);
}

/* ------------------------------------------------------------------ 列表 */

function listBlock(host) {
  const box = el("div", { class: "list-block" });
  box.appendChild(sectionHead("已有的训练包", `${(state.lessons || []).length} 个`));
  box.appendChild(listRows(host));
  return box;
}

function listRows(host) {
  const wrap = el("div", { class: "lesson-list", id: "lesson-list" });
  const lessons = state.lessons || [];
  if (!lessons.length) {
    wrap.appendChild(
      emptyBlock({
        art: "spark",
        title: "还没有训练包",
        text: "上面输入一个词，几秒到一分钟就有一个装满例句和语音的训练包。",
      }),
    );
    return wrap;
  }
  for (const lesson of lessons) wrap.appendChild(lessonRow(lesson, host));
  return wrap;
}

async function refreshList(host) {
  try {
    const data = await get("/api/lessons");
    state.lessons = data.lessons || [];
    state.stats = data.stats || state.stats;
  } catch (err) {
    return;
  }
  const node = document.getElementById("lesson-list");
  if (node && node.parentNode) {
    node.replaceWith(listRows(host));
    paintIcons(document.getElementById("lesson-list"));
  }
  if (typeof window.__etudeRefreshSide === "function") window.__etudeRefreshSide();
}

function lessonRow(lesson, host) {
  const row = el("div", { class: `lesson-row ${lesson.status}`, dataset: { id: String(lesson.id) } });

  const open = el("button", { class: "lesson-open", type: "button" }, [
    el("b", { class: "t", text: lesson.target }),
    el("span", { class: "meta", text: metaText(lesson) }),
  ]);
  open.addEventListener("click", () => go("lessons", lesson.id));

  const chips = el("div", { class: "chips" }, [statusChip(lesson)]);

  const acts = el("div", { class: "rowacts" });
  if (lesson.status === "ready") {
    acts.appendChild(
      el("button", {
        class: "btn sm",
        type: "button",
        text: "打开",
        onClick: () => go("lessons", lesson.id),
      }),
    );
  }
  if (lesson.status === "failed") {
    acts.appendChild(
      el("button", {
        class: "btn sm",
        type: "button",
        text: "重试",
        onClick: async () => {
          try {
            await post(`/api/lessons/${lesson.id}/regenerate`, {});
            watch(lesson.id, host);
            await refreshList(host);
          } catch (err) {
            toast(err.message, "bad");
          }
        },
      }),
    );
  }
  acts.appendChild(
    el("button", {
      class: "iconbtn",
      type: "button",
      title: "删除这个训练包",
      onClick: async () => {
        if (!window.confirm(`删掉「${lesson.target}」？例句、语音和复习进度会一起没。`)) return;
        try {
          await del(`/api/lessons/${lesson.id}`);
          await refreshList(host);
          toast("删掉了。", "ok");
        } catch (err) {
          toast(err.message, "bad");
        }
      },
    }, [el("span", { class: "ico", dataset: { icon: "trash" } })]),
  );

  if (lesson.status === "generating") watch(lesson.id, host);
  row.append(open, chips, acts);
  return row;
}

function metaText(lesson) {
  const bits = [];
  if (lesson.created_at) bits.push(humanWhen(lesson.created_at));
  if (lesson.example_count) bits.push(`${lesson.example_count} 条例句`);
  if (lesson.introduced_count) bits.push(`练过 ${lesson.introduced_count} 张`);
  else if (lesson.card_count) bits.push(`${lesson.card_count} 张卡`);
  return bits.join(" · ");
}

function statusChip(lesson) {
  if (lesson.status === "generating") {
    const step = (lesson.job && lesson.job.step) || "正在生成";
    return chip(step, "accent");
  }
  if (lesson.status === "failed") return chip(lesson.error || "生成失败", "bad");
  const missing = lesson.readiness;
  if (missing && !missing.complete) {
    if (!missing.audio_ready) return chip("缺语音", "warn");
    if (missing.images_expected && !missing.image_ready) return chip("缺配图", "warn");
  }
  return chip("能练了", "ok");
}

/* ------------------------------------------------------------------ 轮询 */

function watch(lessonId, host) {
  if (timers.has(lessonId)) return;
  const timer = window.setInterval(async () => {
    try {
      const job = await get(`/api/lessons/${lessonId}/job`);
      if (job.status !== "generating") {
        window.clearInterval(timer);
        timers.delete(lessonId);
        hideBanner();
        await refreshList(host);
        if (job.status === "failed") {
          banner("bad", `「生成失败」${job.error || ""}`);
        }
        return;
      }
      const node = document.querySelector(`.lesson-row[data-id="${lessonId}"] .chips`);
      if (node && job.job && job.job.step) {
        node.replaceChildren(chip(job.job.step, "accent"));
      }
    } catch (err) {
      window.clearInterval(timer);
      timers.delete(lessonId);
    }
  }, 1500);
  timers.set(lessonId, timer);
}
