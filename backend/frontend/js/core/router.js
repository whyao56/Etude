/* 地址栏路由。
 *
 * 0.0.x 的痛处：训练包详情进不了地址栏 —— 刷新就掉回列表，
 * 想发给别人「你看这个词」也发不了。0.1.0 起路径是有含义的：
 *
 *   #/study              训练
 *   #/lessons            训练包列表
 *   #/lessons/12         第 12 个训练包的详情
 *   #/plans              目标
 *   #/plans/3            第 3 份计划的详情
 *   #/cards              卡片
 *   #/settings           设置
 *
 * 也兼容老的 `#lessons` 写法（旧书签、旧代码里的 location.hash 不会 404）。
 */

export const VIEWS = ["study", "lessons", "plans", "cards", "settings"];

export const VIEW_META = {
  study: { title: "训练", sub: "一次一张卡，翻面之后给自己打分" },
  lessons: { title: "训练包", sub: "一个词 / 一句话，配 8 条不同场景的例句" },
  plans: { title: "目标", sub: "说清要干什么，让模型排出该学什么" },
  cards: { title: "卡片", sub: "所有卡都在这里，可以筛选、批量暂停、推后" },
  settings: { title: "设置", sub: "模型、语音、配图、发音、外观、数据" },
};

export function parseHash(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "").trim();
  if (!raw) return { view: "study", id: 0 };
  const parts = raw.split("/").filter(Boolean);
  const view = VIEWS.includes(parts[0]) ? parts[0] : "study";
  const id = parts[1] ? Number.parseInt(parts[1], 10) : 0;
  return { view, id: Number.isFinite(id) && id > 0 ? id : 0 };
}

export function hashFor(view, id = 0) {
  const safeView = VIEWS.includes(view) ? view : "study";
  return id ? `#/${safeView}/${id}` : `#/${safeView}`;
}

/** 改地址栏（会触发 hashchange，由 router 统一响应）。 */
export function go(view, id = 0) {
  const next = hashFor(view, id);
  if (location.hash === next) return;
  location.hash = next;
}

export function currentRoute() {
  return parseHash(location.hash);
}

/** 起路由：立刻回调一次，之后每次 hashchange 再回调。 */
export function startRouter(handler) {
  window.addEventListener("hashchange", () => handler(parseHash(location.hash)));
  handler(parseHash(location.hash));
}
