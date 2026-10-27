/* 界面零件：提示条、抽屉、放大图、空状态、错误块、页头。
 *
 * 这些东西每个视图都要用，所以集中在一处；它们只管画，不知道任何业务。
 */

import { $, $$, append, clear, el, fill, icon } from "./dom.js";

/* ------------------------------------------------------------------ 页头 */

export function setPage({ title, sub, actions }) {
  const titleNode = $("#page-title");
  const subNode = $("#page-sub");
  if (titleNode && title !== undefined) titleNode.textContent = title;
  if (subNode && sub !== undefined) subNode.textContent = sub || "";
  const host = $("#page-actions");
  if (host) fill(host, actions || []);
}

/* ------------------------------------------------------------------ 横幅 */

let bannerSticky = false;

/**
 * 顶部横幅。
 *
 * `sticky` 是给「版本不一致」这类**跨页面都成立**的问题用的：
 * 普通报错属于某一页，翻页就该消失（切到别的页面还挂着上一页的错误，
 * 会让人以为这一页也坏了）。
 */
export function banner(level, text, sticky = false) {
  const node = $("#banner");
  if (!node) return;
  bannerSticky = Boolean(sticky);
  clear(node);
  append(node, text);
  node.className = `banner show ${level || ""}`.trim();
}

export function hideBanner() {
  bannerSticky = false;
  const node = $("#banner");
  if (!node) return;
  node.className = "banner";
  clear(node);
}

/** 切页时调用：粘性的横幅留着，其它的清掉。 */
export function clearTransientBanner() {
  if (!bannerSticky) hideBanner();
}

/* ------------------------------------------------------------------ 轻提示 */

export function toast(text, level = "ok", ms = 3200) {
  const host = $("#toasts");
  if (!host) return;
  const node = el("div", { class: `toast ${level}`, text });
  host.appendChild(node);
  window.setTimeout(() => {
    node.style.opacity = "0";
    window.setTimeout(() => node.remove(), 220);
  }, ms);
}

/* ------------------------------------------------------------------ 抽屉 */

let activeDrawer = null;

/**
 * 右侧抽屉。返回 { close } —— 调用方在保存成功后自己关，
 * 这样「保存失败」时抽屉还开着，用户填的东西不会没。
 */
export function drawer({ title, badge, body, footer, onClose }) {
  closeDrawer();
  const host = $("#overlay-host");
  const wrap = el("div", { class: "drawer-wrap" });
  const panel = el("aside", { class: "drawer", role: "dialog", "aria-modal": "true" });

  const head = el("div", { class: "drawer-head" }, [
    el("h2", { text: title || "" }),
    badge || null,
    el(
      "button",
      {
        class: "iconbtn",
        type: "button",
        title: "关闭",
        "aria-label": "关闭",
        onClick: () => closeDrawer(),
      },
      [icon("close", 16)],
    ),
  ]);

  const bodyNode = el("div", { class: "drawer-body" });
  append(bodyNode, body || []);
  panel.appendChild(head);
  panel.appendChild(bodyNode);
  if (footer) panel.appendChild(el("div", { class: "drawer-foot" }, footer));

  wrap.appendChild(panel);
  wrap.addEventListener("mousedown", (event) => {
    if (event.target === wrap) closeDrawer();
  });
  host.appendChild(wrap);
  activeDrawer = { close: () => closeDrawer(), wrap, onClose };
  // 抽屉一开就把焦点送进去，键盘用户不用先 Tab 半天。
  const focusable = panel.querySelector("input, textarea, select, button");
  if (focusable) window.setTimeout(() => focusable.focus(), 30);
  return activeDrawer;
}

export function closeDrawer(onClose) {
  const host = $("#overlay-host");
  if (host) clear(host);
  const previous = activeDrawer;
  activeDrawer = null;
  if (previous && typeof previous.onClose === "function") previous.onClose();
  if (typeof onClose === "function") onClose();
}

export function drawerOpen() {
  return Boolean(activeDrawer);
}

/* ------------------------------------------------------------------ 放大图 */

export function zoomImage(src) {
  if (!src) return;
  const host = $("#overlay-host");
  const wrap = el("div", { class: "zoom", title: "点任意处关闭" }, [
    el("img", { src, alt: "" }),
  ]);
  const close = () => wrap.remove();
  wrap.addEventListener("click", close);
  host.appendChild(wrap);
}

/* ------------------------------------------------------------------ 小块 */

export function chip(text, level = "") {
  return el("span", { class: `chip ${level}`.trim(), text });
}

export function errorBlock(err, onRetry) {
  const text = (err && err.message) || String(err);
  const hint = (err && err.hint) || "";
  return el("div", { class: "err" }, [
    el("b", { text }),
    hint ? el("div", { class: "next", text: `→ ${hint}` }) : null,
    onRetry
      ? el("div", { class: "row", style: { marginTop: "8px" } }, [
          el("button", { class: "btn sm", type: "button", text: "重试", onClick: onRetry }),
        ])
      : null,
  ]);
}

export function emptyBlock({ art = "bulb", title, text, actions }) {
  return el("div", { class: "empty" }, [
    el("div", { class: "art" }, [icon(art, 40)]),
    el("b", { text: title || "" }),
    text ? el("div", { class: "faint", text }) : null,
    actions ? el("div", { class: "row", style: { marginTop: "6px" } }, actions) : null,
  ]);
}

export function field(label, control, tip) {
  return el("label", { class: "field" }, [
    el("span", { class: "lab", text: label }),
    control,
    tip ? el("span", { class: "tip", text: tip }) : null,
  ]);
}

export function sectionHead(title, hint, actions) {
  return el("div", { class: "section-head" }, [
    el("div", { class: "grow" }, [
      el("h2", { text: title }),
      hint ? el("div", { class: "hint", text: hint }) : null,
    ]),
    actions || null,
  ]);
}

export function loading(label = "加载中…") {
  return el("div", { class: "row", style: { padding: "24px 4px" } }, [
    el("span", { class: "spin" }),
    el("span", { class: "hint", text: label }),
  ]);
}

export function confirmAsk(text) {
  return window.confirm(text);
}

/** 分段控件：一组按钮，只有一个 aria-pressed=true。 */
export function segmented(options, current, onPick, { block = false } = {}) {
  return el(
    "div",
    { class: `seg${block ? " seg-block" : ""}`, role: "group" },
    options.map((option) =>
      el("button", {
        type: "button",
        text: option.label,
        title: option.note || "",
        "aria-pressed": option.id === current ? "true" : "false",
        onClick: () => onPick(option.id),
      }),
    ),
  );
}

/** 给动态插入的片段补图标。 */
export function repaint(node) {
  for (const host of $$("[data-icon]", node || document)) {
    if (host.dataset.iconDone === "1") continue;
    host.appendChild(icon(host.dataset.icon, host.classList.contains("ico-lg") ? 20 : 16));
    host.dataset.iconDone = "1";
  }
}
