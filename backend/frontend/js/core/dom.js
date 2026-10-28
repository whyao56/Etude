/* DOM 小工具 + 图标。
 *
 * 这套东西存在的唯一理由是：**别让每个视图都手写 createElement**。
 * 0.0.x 最大的一个 bug 就藏在这里 —— `el()` 用 `key.slice(2)` 生成事件名，
 * 于是 `onClick` 变成了 `addEventListener("Click")`，大小写敏感，
 * 浏览器不报错、截图看不出来、所有按钮全死。这里保留 `.toLowerCase()`，
 * 并且 scripts/check_frontend.py 有专门一条守卫盯着它。
 */

export const $ = (selector, root = document) => root.querySelector(selector);

export const $$ = (selector, root = document) =>
  Array.from(root.querySelectorAll(selector));

/**
 * 建一个元素。
 * attrs 支持：class / text / html / style(对象) / dataset(对象) /
 *             onXxx(函数，自动 toLowerCase) / 布尔属性 / 其它走 setAttribute。
 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  const spec = attrs || {};
  for (const key of Object.keys(spec)) {
    const value = spec[key];
    if (value === null || value === undefined || value === false) continue;
    if (key === "class" || key === "className") {
      node.className = value;
      continue;
    }
    if (key === "text") {
      node.textContent = String(value);
      continue;
    }
    if (key === "html") {
      node.innerHTML = String(value);
      continue;
    }
    if (key === "style" && typeof value === "object") {
      Object.assign(node.style, value);
      continue;
    }
    if (key === "dataset" && typeof value === "object") {
      for (const name of Object.keys(value)) node.dataset[name] = value[name];
      continue;
    }
    if (key.startsWith("on") && typeof value === "function") {
      // 事件类型必须小写。这一行是整个界面「点得动」的原因。
      node.addEventListener(key.slice(2).toLowerCase(), value);
      continue;
    }
    if (typeof value === "boolean" && key in node) {
      node[key] = value;
      continue;
    }
    node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(node, child);
    else if (child instanceof Node) node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  if (!node) return node;
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function fill(node, children) {
  clear(node);
  append(node, children);
  paintIcons(node);
  return node;
}

/* ------------------------------------------------------------------ 图标
 * 24×24 stroke 图标，`currentColor` 上色，所以跟着文字颜色走。
 */
const PATHS = {
  study: "M8 5.5v13l11-6.5z",
  cards:
    "M4 8.5 12 4l8 4.5-8 4.5zM4 12.5 12 17l8-4.5M4 16.5 12 21l8-4.5",
  lessons: "M5 5.5h9.5a3 3 0 0 1 3 3V19H8a3 3 0 0 1-3-3zM17.5 8.5H7",
  plans: "M4 19V5m0 12 5-5 4 3 7-8",
  settings:
    "M4 7h9M17 7h3M4 17h3M11 17h9M15 4.6v4.8M8 14.6v4.8",
  play: "M7 5.5v13l11-6.5z",
  pause: "M8 5.5v13M16 5.5v13",
  mic: "M12 4a2.5 2.5 0 0 1 2.5 2.5v5a2.5 2.5 0 0 1-5 0v-5A2.5 2.5 0 0 1 12 4zM6.5 11.5a5.5 5.5 0 0 0 11 0M12 17v3",
  stop: "M7 7h10v10H7z",
  check: "M5 12.5 10 17.5 19 6.5",
  close: "M6 6l12 12M18 6 6 18",
  trash: "M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12M10 11v5M14 11v5",
  plus: "M12 5v14M5 12h14",
  refresh: "M19 12a7 7 0 1 1-2.2-5.1M19 4v5h-5",
  image: "M4 6h16v12H4zM4 15l4.5-4.5L13 15l2.5-2.5L20 17M15 9.2h.01",
  left: "M14 6l-6 6 6 6",
  right: "M10 6l6 6-6 6",
  sun: "M12 4.5v2M12 17.5v2M4.5 12h2M17.5 12h2M6.4 6.4l1.4 1.4M16.2 16.2l1.4 1.4M17.6 6.4l-1.4 1.4M7.8 16.2l-1.4 1.4M12 8.6a3.4 3.4 0 1 0 0 6.8 3.4 3.4 0 0 0 0-6.8z",
  moon: "M19 14.2A7.4 7.4 0 0 1 9.8 5a7.5 7.5 0 1 0 9.2 9.2z",
  panel: "M4 5h16v14H4zM9.5 5v14",
  drive: "M4 13h16v5H4zM6.5 13 9 7h6l2.5 6M7.5 15.5h.01M11 15.5h.01",
  spark: "M12 4.5 13.7 9 18 10.5 13.7 12 12 16.5 10.3 12 6 10.5 10.3 9z",
  wand: "M5 19 15 9M14 4.5l.8 1.7M19.5 10l-1.7.8M17.5 5.5l-1.2 1.2",
  clock: "M12 5a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM12 8.5V12l2.5 1.6",
  tag: "M4 4h7l9 9-7 7-9-9zM8 8h.01",
  edit: "M6 18h3l8.5-8.5-3-3L6 15zM14 5.5l1.5-1.5 3 3L17 8.5",
  undo: "M9 7 5 11l4 4M5 11h8a5 5 0 0 1 0 10h-1",
  bulb: "M9 17h6M10 20h4M12 3.5a6 6 0 0 0-3.5 10.8V17h7v-2.7A6 6 0 0 0 12 3.5z",
  zoom: "M11 5a6 6 0 1 0 0 12 6 6 0 0 0 0-12zM15.5 15.5 20 20M11 8.5v5M8.5 11h5",
  down: "M6 9l6 6 6-6",
  layers: "M12 4 4 8l8 4 8-4zM4 12l8 4 8-4M4 16l8 4 8-4",
  progress: "M4 19h16M7.5 19v-5.5M12 19V7.5M16.5 19v-9",
  flame: "M12 3.6c2.4 2.9 4.3 5 4.3 7.7a4.3 4.3 0 0 1-8.6 0c0-1.5.7-2.8 1.7-4 .2 1.2.9 2 2 2.2-.5-2 0-4 .6-5.9z",
  folder: "M4 6.5h5.2l1.8 2H20V18H4z",
  help: "M12 17.2h.01M9.5 9.3a2.6 2.6 0 1 1 3.5 2.4c-.7.3-1 .9-1 1.7v.3",
  download: "M12 4v10.5m0 0 3.8-3.8M12 14.5l-3.8-3.8M5 19.5h14",
  copy: "M9 9h10v11H9zM6 15H4.5V4H15v1.5",
  external: "M14 5h5v5M19 5l-7.5 7.5M9.5 5H5v14h14v-4.5",
  target: "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM12 8.2a3.8 3.8 0 1 0 0 7.6 3.8 3.8 0 0 0 0-7.6zM12 11.4a.6.6 0 1 0 0 1.2.6.6 0 0 0 0-1.2z",
};

const SVG_NS = "http://www.w3.org/2000/svg";

/** 返回一个 <span class="ico"> 包着的 svg，颜色跟着 currentColor。 */
export function icon(name, size = 16) {
  const svg = iconSvg(name, size);
  return svg ? el("span", { class: "ico" }, [svg]) : el("span", { class: "ico" });
}

/** 只要那颗 svg。给已经自带 `.ico` 外框的地方用 —— 再包一层会变成 .ico > .ico。 */
export function iconSvg(name, size = 16) {
  const path = PATHS[name];
  if (!path) return null;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const node = document.createElementNS(SVG_NS, "path");
  node.setAttribute("d", path);
  svg.appendChild(node);
  return svg;
}

/** 把带 data-icon 的空 span 填上图标。动态插入的片段要手动调一次。 */
export function paintIcons(root = document) {
  for (const host of $$("[data-icon]", root)) {
    if (host.dataset.iconDone === "1") continue;
    const size = host.classList.contains("ico-lg") ? 20 : 16;
    // 把 svg 直接塞进带 data-icon 的那个 span：那个 span 自己就是 `.ico`。
    // 早先这里调的是 icon()，于是生成 .ico > .ico > svg，外层被 CSS 撑成大方块，
    // 侧栏左上角那个空白色块就是这么来的。
    const svg = iconSvg(host.dataset.icon, size);
    if (svg) host.appendChild(svg);
    host.dataset.iconDone = "1";
  }
}

export function kbd(text) {
  return el("span", { class: "kbd", text });
}

/** 时间：今天/昨天/N 天前。列表里用，精确值放 title。 */
export function humanWhen(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "";
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  if (days < 30) return `${days} 天前`;
  return then.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

/** 字节数。设置页显示数据占用。 */
export function humanSize(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
