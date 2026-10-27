/* 启动、路由、全局键盘。
 *
 * 这个文件只做四件事：
 * 1. 把存下来的外观偏好贴到 <html> 上（先贴再画，避免闪一下白底）。
 * 2. 问一次后端的健康状况 —— 版本不一致是最容易被误当成「按钮坏了」的情况。
 * 3. 把地址栏的变化翻译成一次重画。
 * 4. 把没接住的异常显示到屏幕上，而不是只留在控制台里。
 */

import { get } from "./core/api.js";
import { $, el, paintIcons } from "./core/dom.js";
import { VIEWS, VIEW_META, currentRoute, go, startRouter } from "./core/router.js";
import { applyLook, loadPrefs, state } from "./core/store.js";
import { banner, clearTransientBanner, closeDrawer, drawerOpen, hideBanner, setPage, toast } from "./core/ui.js";
import { APP_VERSION } from "./version.js";

import * as cards from "./views/cards.js";
import * as lesson from "./views/lesson.js";
import * as lessons from "./views/lessons.js";
import * as plans from "./views/plans.js";
import * as settings from "./views/settings.js";
import * as study from "./views/study.js";

const app = $("#app");
const sideBlock = $("#side-block");

/* ------------------------------------------------------------------ 导航 */

function buildNav() {
  const nav = $("#nav");
  nav.replaceChildren(
    ...VIEWS.map((view) => {
      // 取 title 不取 label：VIEW_META 里只有 title/sub，
      // 之前取 meta.label 拿到 undefined，五个导航项的 label 全是空 span ——
      // 界面上只剩图标和角标，看起来像「导航坏了」。
      const meta = VIEW_META[view] || { title: view };
      const button = el("button", { class: "nav-item", type: "button", dataset: { view } }, [
        el("span", { class: "ico", dataset: { icon: view } }),
        el("span", { class: "label", text: meta.title }),
        el("span", { class: "count", id: `count-${view}` }),
      ]);
      button.title = meta.sub || meta.title;
      button.addEventListener("click", () => go(view));
      return button;
    }),
  );
  paintIcons(nav);
}

function markNav(view) {
  for (const button of document.querySelectorAll("#nav .nav-item")) {
    button.setAttribute("aria-current", button.dataset.view === view ? "page" : "false");
  }
}

function setCount(view, value) {
  const node = document.getElementById(`count-${view}`);
  if (!node) return;
  const text = value ? String(value) : "";
  node.textContent = text;
  node.style.display = text ? "" : "none";
}

async function refreshCounts() {
  try {
    const stats = await get("/api/stats");
    state.stats = stats;
    setCount("study", stats.due_now || 0);
    setCount("cards", stats.cards || 0);
    setCount("lessons", stats.lessons || 0);
    setCount("plans", stats.plans || 0);
  } catch (err) {
    /* 角标画不出来不影响用什么 */
  }
}

/* ------------------------------------------------------------------ 渲染 */

const VIEWS_IMPL = {
  study,
  lessons: {
    render: (host, id) => (id ? lesson.render(host, id) : lessons.render(host)),
  },
  // 计划也分列表和详情：#/plans/<id> 是「这份计划里有什么」，
  // 之前这里漏了 id 分支，地址栏进去了、页面还停在列表上。
  plans: {
    render: (host, id) => (id ? plans.renderDetail(host, id) : plans.render(host)),
  },
  cards,
  settings,
};

let rendering = 0;

async function paint() {
  const route = currentRoute();
  state.route = route;
  const token = ++rendering;
  closeDrawer();
  // 上一页的报错不该跟着走到这一页来（会让人以为这一页也坏了）。
  clearTransientBanner();
  markNav(route.view);
  app.replaceChildren();
  if (sideBlock) sideBlock.replaceChildren();

  const impl = VIEWS_IMPL[route.view] || VIEWS_IMPL.study;
  try {
    await impl.render(app, route.id);
  } catch (err) {
    if (token !== rendering) return;
    app.replaceChildren(
      el("div", { class: "empty" }, [
        el("b", { text: "这一页没画出来。" }),
        el("div", { class: "hint", text: err.message || String(err) }),
      ]),
    );
    banner("bad", err.message || "页面渲染出错了。");
    return;
  }
  if (token !== rendering) return;

  if (route.view === "study") {
    refreshSide();
  } else if (sideBlock) {
    const start = el("button", { class: "btn primary block", type: "button", text: "开始训练" });
    start.addEventListener("click", () => go("study"));
    sideBlock.replaceChildren(
      el("div", { class: "side-card" }, [
        el("div", { class: "hint", text: state.stats ? `${state.stats.due_now || 0} 张到期` : "看看今天有多少" }),
        start,
      ]),
    );
  }
  paintIcons(document.body);
  refreshCounts();
}

/**
 * 只重画侧栏那半块。
 *
 * 训练页评完一次分、跳过一张，主区域会自己重画；如果连侧栏的
 * 「本次已练 N 张」都跟着整页重跑一遍，正在录音的麦克风会被一起掐掉。
 */
function refreshSide() {
  if (!sideBlock) return;
  if (currentRoute().view !== "study") return;
  sideBlock.replaceChildren(study.sideBlock());
  paintIcons(sideBlock);
}

/* ------------------------------------------------------------------ 全局键 */

function bindKeys() {
  document.addEventListener("keydown", async (event) => {
    if (event.key === "Escape" && drawerOpen()) {
      closeDrawer();
      event.preventDefault();
      return;
    }
    const target = event.target;
    if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (drawerOpen()) return;
    if (currentRoute().view !== "study") return;
    const handled = await study.handleKey(event);
    if (handled) event.preventDefault();
  });
}

/* ------------------------------------------------------------------ 外观 */

function bindChrome() {
  const theme = $("#theme-btn");
  if (theme) {
    theme.addEventListener("click", () => {
      const order = ["auto", "warm", "light", "dark"];
      const now = state.prefs.theme || "auto";
      const next = order[(order.indexOf(now) + 1) % order.length];
      state.prefs.theme = next;
      import("./core/store.js").then((mod) => {
        mod.savePrefs();
        applyLook();
        const label = (mod.THEMES.find((one) => one.id === next) || {}).label || next;
        toast(`配色：${label}`);
      });
    });
  }
  const side = $("#side-btn");
  if (side) {
    side.addEventListener("click", () => {
      state.prefs.side = state.prefs.side === "collapsed" ? "open" : "collapsed";
      import("./core/store.js").then((mod) => {
        mod.savePrefs();
        applyLook();
      });
    });
  }
}

/* ------------------------------------------------------------------ 健康 */

async function checkHealth() {
  const dot = $("#health-dot");
  try {
    const health = await get("/api/health");
    state.health = health;
    if (dot) {
      dot.className = "dot ok";
      dot.title = `v${health.version} · pid ${health.pid}`;
    }
    const text = $("#health-text");
    if (text) text.textContent = `v${health.version} · ${health.mode === "exe" ? "打包版" : "源码"}`;
    if (health.version !== APP_VERSION) {
      // 粘性：这个问题跨页面都成立，翻页不该把它抹掉。
      banner(
        "bad",
        `版本不一致：界面是 ${APP_VERSION}，后端是 ${health.version}（pid ${health.pid}，数据目录 ${health.data_dir}）。多半是端口上还留着一个旧进程，关掉它再重开程序。`,
        true,
      );
    }
  } catch (err) {
    if (dot) dot.className = "dot bad";
    const text = $("#health-text");
    if (text) text.textContent = "连不上后端";
    banner("bad", err.message || "连不上本机的程序。", true);
  }
}

/* ------------------------------------------------------------------ 主流程 */

async function boot() {
  loadPrefs();
  applyLook();
  buildNav();
  bindChrome();
  bindKeys();

  window.addEventListener("error", (event) => {
    const text = event.message || "未知错误";
    // 资源加载失败（比如缓存里的旧模块）也走这里 —— 这一条最有用的信息是文件名。
    banner("bad", `界面出错了：${text}`);
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    const text = reason && reason.message ? reason.message : String(reason);
    if (text && text.includes("Failed to fetch")) return;
    banner("bad", `有个操作没做完：${text}`);
  });

  await checkHealth();

  // 这两个失败不致命：各页面自己会再报一次。
  try {
    state.config = await get("/api/config");
  } catch (err) {
    /* 设置页会再报 */
  }
  try {
    state.catalog = await get("/api/catalog");
  } catch (err) {
    /* 同上 */
  }

  startRouter(() => {
    paint();
  });
  await paint();

  // 给 scripts/ui_smoke.js 留一个只读手柄。
  //
  // 为什么要在生产代码里放这个：界面拆成模块之后，脚本里就没有一个
  // 叫 `state` 的全局变量可读了，而冒烟检查要断言的恰恰是
  // 「翻面了没有」「游标动了没有」这类**内部状态**。只看 DOM 的话，
  // 「点了按钮但什么都没变」和「点了按钮只是样式没刷新」分不开。
  //
  // 它只暴露对象本身，不暴露任何动作，也没有 getter/setter 语义，
  // 不会改变程序的行为。
  window.__etude = { state, go, version: APP_VERSION, refreshSide };
}

boot();
