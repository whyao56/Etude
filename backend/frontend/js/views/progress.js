/* 进度页。
 *
 * 这一页只回答四个问题，顺序也是按这四个问题排的：
 *   今天练了没有？连着几天了？五个通道哪条落下了？哪几张卡该回头看一眼？
 *
 * 刻意**不做**排行榜、不做「预测你什么时候能学会」——
 * 前者对一个人学语言毫无意义，后者的输入根本不够，说了就是编。
 */

import { get } from "../core/api.js";
import { el, icon, paintIcons } from "../core/dom.js";
import { CHANNEL_LABEL, state } from "../core/store.js";
import { errorBlock, loading, setPage } from "../core/ui.js";
import { go } from "../core/router.js";

const WEEKDAY = ["日", "一", "二", "三", "四", "五", "六"];

function greeting() {
  const hour = new Date().getHours();
  if (hour < 5) return "这个点还在练，明天别硬撑。";
  if (hour < 9) return "早上练记忆最清。";
  if (hour < 12) return "上午好。";
  if (hour < 14) return "中午歇会儿再练。";
  if (hour < 18) return "下午了，来几张？";
  if (hour < 22) return "晚上是练语言的好时候。";
  return "很晚了，练几张就睡。";
}

export async function render(host) {
  setPage({
    title: "进度",
    sub: "练了多少、连着多少天、哪几张最该回头看",
    actions: [
      el("button", {
        class: "btn",
        type: "button",
        text: "刷新",
        onClick: () => render(host),
      }),
    ],
  });
  host.replaceChildren(loading("数一数…"));
  let data;
  try {
    data = await get("/api/progress?days=14");
  } catch (err) {
    host.replaceChildren(errorBlock(err, () => render(host)));
    return;
  }
  state.progress = data;
  host.replaceChildren(
    overviewCard(data),
    chartCard(data),
    channelCard(data),
    weakCard(data),
    pronounceCard(data),
  );
  paintIcons(host);
}

/* ---------------------------------------------------------------- 顶部四块 */

function overviewCard(data) {
  const totals = data.totals || {};
  const streak = data.streak || {};
  const tiles = [
    {
      id: "today",
      label: "今天练了",
      value: data.today.reviews,
      unit: "张",
      note: data.today.attempts ? `另有 ${data.today.attempts} 次跟读` : greeting(),
      icon: "check",
      tone: data.today.reviews ? "ok" : "",
    },
    {
      id: "streak",
      label: "连续",
      value: streak.current || 0,
      unit: "天",
      note: streak.best ? `最长 ${streak.best} 天` : "从今天开始算",
      icon: "flame",
      tone: (streak.current || 0) >= 3 ? "warm" : "",
    },
    {
      id: "reviews",
      label: "累计复习",
      value: totals.reviews || 0,
      unit: "次",
      note: `${totals.cards || 0} 张卡里练过 ${totals.introduced || 0} 张`,
      icon: "refresh",
      tone: "",
    },
    {
      id: "due",
      label: "到期没练",
      value: totals.due_now || 0,
      unit: "张",
      note: totals.due_now ? "现在就能练" : "今天清空了",
      icon: "clock",
      tone: totals.due_now ? "bad" : "ok",
    },
  ];
  const grid = el(
    "div",
    { class: "stat-grid" },
    tiles.map((tile) =>
      el("div", { class: `stat-tile ${tile.tone}`.trim() }, [
        el("div", { class: "stat-label" }, [
          el("span", { class: "ico" }, [icon(tile.icon, 15).firstChild]),
          el("span", { text: tile.label }),
        ]),
        el("div", { class: "stat-value" }, [
          el("b", { text: String(tile.value) }),
          el("span", { class: "stat-unit", text: tile.unit }),
        ]),
        el("div", { class: "stat-note", text: tile.note }),
      ]),
    ),
  );
  const footer = el("div", { class: "row stat-actions" }, [
    (() => {
      const btn = el("button", { class: "btn primary sm", type: "button" }, [
        el("span", { class: "ico", dataset: { icon: "study" } }),
        el("span", { text: totals.due_now ? `练这 ${totals.due_now} 张` : "开始训练" }),
      ]);
      btn.addEventListener("click", () => go("study"));
      return btn;
    })(),
    el("div", { class: "grow" }),
    el("span", {
      class: "hint",
      text: `累计 ${totals.lessons || 0} 个训练包 · ${totals.examples || 0} 条例句 · ${totals.attempts || 0} 次跟读`,
    }),
  ]);
  return el("section", { class: "card" }, [grid, footer]);
}

/* ---------------------------------------------------------------- 柱状图 */

function chartCard(data) {
  const series = data.series || [];
  const peak = Math.max(1, ...series.map((day) => Math.max(day.reviews, day.attempts)));
  const totalReviews = series.reduce((sum, day) => sum + day.reviews, 0);
  const totalAttempts = series.reduce((sum, day) => sum + day.attempts, 0);

  const cols = series.map((day) => {
    const date = new Date(day.date + "T00:00:00");
    const isToday = day.date === data.to;
    const reviewHeight = Math.round((day.reviews / peak) * 100);
    const attemptHeight = Math.round((day.attempts / peak) * 100);
    const column = el(
      "div",
      {
        class: `chart-col${isToday ? " is-today" : ""}${day.reviews || day.attempts ? "" : " is-empty"}`,
        title: `${day.date.slice(5)} · 复习 ${day.reviews} 张 · 跟读 ${day.attempts} 次`,
      },
      [
        el("div", { class: "chart-bars" }, [
          el("i", {
            class: "bar reviews",
            style: { height: `${reviewHeight}%` },
            "aria-hidden": "true",
          }),
          el("i", {
            class: "bar attempts",
            style: { height: `${attemptHeight}%` },
            "aria-hidden": "true",
          }),
        ]),
        el("div", { class: "chart-tick", text: day.date === data.to ? "今天" : String(date.getDate()) }),
        el("div", { class: "chart-week", text: WEEKDAY[date.getDay()] }),
      ],
    );
    return column;
  });

  const legend = el("div", { class: "chart-legend" }, [
    el("span", { class: "legend-item" }, [el("i", { class: "swatch reviews" }), el("span", { text: `复习 ${totalReviews} 张` })]),
    el("span", { class: "legend-item" }, [el("i", { class: "swatch attempts" }), el("span", { text: `跟读 ${totalAttempts} 次` })]),
    el("span", { class: "grow" }),
    el("span", { class: "hint", text: `最忙的一天 ${peak} 张 · 最近 14 天` }),
  ]);

  return el("section", { class: "card" }, [
    el("h3", { text: "最近两周" }),
    el("p", { class: "card-note", text: "柱子高的是「复习过的卡」，细条是「跟读次数」。空着的那天不用补，接着练就是了。" }),
    el("div", { class: "chart" }, cols),
    legend,
  ]);
}

/* ---------------------------------------------------------------- 五通道 */

function channelCard(data) {
  const channels = data.channels || [];
  const maxCards = Math.max(1, ...channels.map((row) => row.cards));
  const rows = channels.map((row) => {
    const width = Math.round((row.cards / maxCards) * 100);
    const done = row.cards ? Math.round((row.introduced / row.cards) * 100) : 0;
    const gradeText =
      row.avg_grade === null || row.avg_grade === undefined
        ? "还没打过卡"
        : `平均 ${["没想起来", "吃力", "正常", "秒答"][Math.round(row.avg_grade)] || "—"}`;
    return el("div", { class: "channel-row" }, [
      el("div", { class: "channel-name", text: CHANNEL_LABEL[row.channel] || row.channel }),
      el("div", { class: "channel-bar" }, [
        el("i", { class: "fill", style: { width: `${width}%` } }),
        el("i", { class: "done", style: { width: `${(width * done) / 100}%` } }),
      ]),
      el("div", { class: "channel-num mono", text: `${row.introduced}/${row.cards}` }),
      el("div", { class: "channel-note", text: gradeText }),
    ]);
  });
  return el("section", { class: "card" }, [
    el("h3", { text: "五个通道" }),
    el("p", {
      class: "card-note",
      text: "深色是「练过的」，浅色是「还没开始的」。五个通道各练一张才算把一条例句吃透。",
    }),
    el("div", { class: "channel-list" }, rows),
  ]);
}

/* ---------------------------------------------------------------- 该回头 */

function weakCard(data) {
  const weak = data.weak || [];
  if (!weak.length) {
    return el("section", { class: "card" }, [
      el("h3", { text: "该回头看的" }),
      el("p", { class: "card-note", text: "还没有「忘过」的卡。等你练过一阵，这里会冒出几张老忘的。" }),
    ]);
  }
  const rows = weak.map((item) =>
    el("div", { class: "weak-row" }, [
      el("div", { class: "weak-body" }, [
        el("div", { class: "weak-sentence", text: item.sentence || item.target || "（例句已删）" }),
        el("div", {
          class: "weak-meta",
          text: `${item.target || ""} · ${CHANNEL_LABEL[item.channel] || item.channel} · 忘过 ${item.lapses} 次`,
        }),
      ]),
      (() => {
        const btn = el("button", { class: "btn sm", type: "button", text: "练这张" });
        btn.addEventListener("click", () => {
          // 直接练这张卡：和卡片页的「练」是同一个入口。
          state.queue = [{ id: item.card_id }];
          state.cursor = 0;
          go("study");
        });
        return btn;
      })(),
    ]),
  );
  return el("section", { class: "card" }, [
    el("h3", { text: "该回头看的" }),
    el("p", { class: "card-note", text: "按「忘过几次」排的。这几张不是笨，是还没到该记住的时候。" }),
    el("div", { class: "weak-list" }, rows),
  ]);
}

/* ---------------------------------------------------------------- 跟读 */

function pronounceCard(data) {
  const info = data.pronounce || {};
  const recent = info.recent || [];
  if (!info.count) {
    return el("section", { class: "card" }, [
      el("h3", { text: "跟读" }),
      el("p", {
        class: "card-note",
        text: "还没跟读过。练到「口语」卡时会有一个录音键，念完能看到自己的分。",
      }),
    ]);
  }
  const bars = recent.map((score, index) =>
    el("i", {
      class: `spark-bar${score >= 85 ? " is-good" : score >= 70 ? "" : " is-meh"}`,
      style: { height: `${Math.max(6, Math.round(score))}%` },
      title: `第 ${index + 1} 次：${score} 分`,
    }),
  );
  const latest = recent[recent.length - 1];
  const first = recent[0];
  const trend = latest - first;
  return el("section", { class: "card" }, [
    el("h3", { text: "跟读" }),
    el("div", { class: "pronounce-summary" }, [
      el("div", { class: "big-score" }, [
        el("b", { text: String(info.average ?? "—") }),
        el("span", { class: "unit", text: "均分" }),
      ]),
      el("div", { class: "pronounce-facts" }, [
        el("div", { text: `一共 ${info.count} 次 · 最高 ${info.best ?? "—"}` }),
        el("div", {
          class: "hint",
          text:
            trend > 0
              ? `最近比最早高 ${trend.toFixed(1)} 分`
              : trend < 0
                ? `最近比最早低 ${Math.abs(trend).toFixed(1)} 分`
                : "分数和第一次差不多",
        }),
      ]),
      el("div", { class: "spark" }, bars),
    ]),
  ]);
}
