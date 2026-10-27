/* 卡片浏览器：所有卡都在这儿，能筛、能搜、能批量收拾。
 *
 * 这一页是给「已经练了一阵、想调控一下节奏」的人用的：
 * 今天不想看这张、这张太简单了、这个词我想先放一放 —— 都可以一次改一批。
 */

import { get, patch, post } from "../core/api.js";
import { el } from "../core/dom.js";
import { CHANNEL_LABEL, state } from "../core/store.js";
import { chip, errorBlock, field, loading, setPage, toast } from "../core/ui.js";
import { go } from "../core/router.js";

const STATES = [
  { id: "all", label: "全部" },
  { id: "due", label: "今天到期" },
  { id: "new", label: "没学过的" },
  { id: "learning", label: "学过但没到期" },
  { id: "suspended", label: "已暂停" },
];

const CHANNEL_OPTIONS = [{ id: "", label: "全部通道" }].concat(
  Object.entries(CHANNEL_LABEL).map(([id, label]) => ({ id, label })),
);

let query = { lesson_id: 0, channel: "", state: "all", q: "", limit: 200, offset: 0 };
let picked = new Set();
let searchTimer = 0;

export async function render(host) {
  setPage({ title: "卡片", sub: "所有卡都在这里，可以筛选、批量暂停、推后", actions: [] });
  host.replaceChildren();
  host.appendChild(filters(host));
  const table = el("div", { class: "browser", id: "browser-host" }, [loading("读卡片…")]);
  host.appendChild(table);
  await load(host);
}

function filters(host) {
  const box = el("section", { class: "card tight filters" });

  const stateSelect = el(
    "select",
    { class: "input" },
    STATES.map((item) => el("option", { value: item.id, text: item.label, selected: item.id === query.state })),
  );
  stateSelect.addEventListener("change", () => {
    query.state = stateSelect.value;
    query.offset = 0;
    load(host);
  });

  const channelSelect = el(
    "select",
    { class: "input" },
    CHANNEL_OPTIONS.map((item) =>
      el("option", { value: item.id, text: item.label, selected: item.id === query.channel }),
    ),
  );
  channelSelect.addEventListener("change", () => {
    query.channel = channelSelect.value;
    query.offset = 0;
    load(host);
  });

  const search = el("input", {
    class: "input",
    type: "search",
    placeholder: "搜目标词或例句",
    value: query.q,
  });
  search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      query.q = search.value.trim();
      query.offset = 0;
      load(host);
    }, 300);
  });

  box.append(stateSelect, channelSelect, search);
  return box;
}

async function load(host) {
  const target = document.getElementById("browser-host");
  if (!target) return;
  target.replaceChildren(loading("读卡片…"));
  const params = new URLSearchParams();
  if (query.lesson_id) params.set("lesson_id", String(query.lesson_id));
  if (query.channel) params.set("channel", query.channel);
  if (query.q) params.set("q", query.q);
  params.set("state", query.state);
  params.set("limit", String(query.limit));
  params.set("offset", String(query.offset));
  try {
    const data = await get(`/api/cards?${params.toString()}`);
    state.browser = data;
    target.replaceChildren(drawTable(data, host));
  } catch (err) {
    target.replaceChildren(errorBlock(err, () => load(host)));
  }
}

function drawTable(data, host) {
  const wrap = el("div", {});
  const cards = data.cards || [];
  picked = new Set();

  wrap.appendChild(
    el("div", { class: "browser-head" }, [
      el("span", { class: "hint grow", text: `一共 ${data.total || 0} 张，这一屏 ${cards.length} 张` }),
      bulkBar(host),
    ]),
  );

  if (!cards.length) {
    wrap.appendChild(el("div", { class: "empty" }, [el("b", { text: "这些条件下没有卡片" })]));
    return wrap;
  }

  const table = el("table", { class: "table" });
  table.appendChild(
    el("thead", {}, [
      el("tr", {}, [
        el("th", { class: "pick" }),
        el("th", { text: "通道" }),
        el("th", { text: "目标 · 例句" }),
        el("th", { text: "到期" }),
        el("th", { text: "间隔" }),
        el("th", { text: "次数" }),
        el("th", { text: "" }),
      ]),
    ]),
  );

  const tbody = el("tbody");
  for (const card of cards) {
    const box = el("input", { type: "checkbox" });
    box.addEventListener("change", () => {
      if (box.checked) picked.add(card.id);
      else picked.delete(card.id);
    });
    const tr = el("tr", { class: card.suspended ? "off" : "", dataset: { id: String(card.id) } }, [
      el("td", { class: "pick" }, [box]),
      el("td", {}, [chip(CHANNEL_LABEL[card.channel] || card.channel)]),
      el("td", { class: "sent" }, [
        el("b", { text: card.target || "" }),
        el("div", { class: "hint", text: card.sentence || "" }),
      ]),
      el("td", { class: "num", text: dueText(card.due_at, card.introduced) }),
      el("td", { class: "num mono", text: fmtInterval(card.interval) }),
      el("td", { class: "num mono", text: String(card.reps || 0) }),
      el(
        "td",
        { class: "rowacts" },
        [
          el("button", {
            class: "btn sm ghost",
            type: "button",
            text: "练",
            onClick: () => {
              state.queue = [{ id: card.id }];
              state.cursor = 0;
              state.current = null;
              state.revealed = false;
              state.session = { done: 0, skipped: 0, history: [], startedAt: Date.now() };
              go("study");
            },
          }),
          el("button", {
            class: "btn sm ghost",
            type: "button",
            text: card.suspended ? "恢复" : "暂停",
            onClick: () => one(card, { suspended: card.suspended ? 0 : 1 }, host),
          }),
        ],
      ),
    ]);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function bulkBar(host) {
  const bar = el("div", { class: "bulkbar" });
  const add = (label, action, cls = "btn sm") => {
    const button = el("button", { class: cls, type: "button", text: label });
    button.addEventListener("click", () => action(button));
    bar.appendChild(button);
  };
  const ids = () => [...picked];
  const needPick = (fn) => async () => {
    const list = ids();
    if (!list.length) {
      toast("先勾几张卡。", "warn");
      return;
    }
    await fn(list);
    await load(host);
  };

  add("暂停", needPick((list) => post("/api/cards/batch", { ids: list, patch: { suspended: 1 } })))
    ;
  add("恢复", needPick((list) => post("/api/cards/batch", { ids: list, patch: { suspended: 0 } })));
  add("推后 1 天", needPick((list) => post("/api/cards/push", { ids: list, days: 1 })));
  add("推后 7 天", needPick((list) => post("/api/cards/push", { ids: list, days: 7 })));
  add("当作没学过", needPick((list) => post("/api/cards/batch", { ids: list, patch: { introduced: 0 } })));
  return bar;
}

async function one(card, patchBody, host) {
  try {
    await patch(`/api/cards/${card.id}`, patchBody);
    await load(host);
  } catch (err) {
    toast(err.message, "bad");
  }
}

function dueText(dueAt, introduced) {
  if (!introduced) return "没学过";
  if (!dueAt) return "—";
  const due = new Date(dueAt);
  const diff = due.getTime() - Date.now();
  if (diff <= 0) return "现在";
  const minutes = Math.round(diff / 60000);
  if (minutes < 60) return `${minutes} 分钟后`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时后`;
  return `${Math.round(hours / 24)} 天后`;
}

function fmtInterval(days) {
  const value = Number(days || 0);
  if (!value) return "—";
  if (value < 1) return `${Math.round(value * 1440)} 分`;
  if (value < 30) return `${value.toFixed(1)} 天`;
  if (value < 365) return `${(value / 30).toFixed(1)} 月`;
  return `${(value / 365).toFixed(1)} 年`;
}
