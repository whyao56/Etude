/* 共享状态、外观偏好、通道词汇表。
 *
 * 原则：**能算出来的不存**。这里只放「跨视图必须一致」的东西
 * （当前配置、当前卡、会话进度、外观偏好），其余都是各视图自己的局部变量。
 */

export const CHANNELS = ["read", "listen", "speak", "type", "build"];

export const CHANNEL_LABEL = {
  read: "阅读",
  listen: "听力",
  speak: "口语",
  type: "打字",
  build: "造句",
};

export const CHANNEL_TIPS = {
  read: "看英文，让场景自己浮出来。不要读出声，不要想中文。",
  listen: "只听声音，直接进入场景。中间不要经过图片或中文。",
  speak: "看场景，用英文说出来（真的出声）。说完再看原文。",
  type: "看场景，把英文打出来。打完自动比对。",
  build: "换掉一个成分，造出一个新句子。造的句子要能出声念出来。",
};

export const GRADE_LABEL = ["没想起来", "吃力", "正常", "秒答"];
export const GRADE_HINT = [
  "完全不记得，明天再说",
  "想了一会儿，或者念得磕巴",
  "正常想起来了",
  "不假思索，脱口而出",
];

export const THEMES = [
  { id: "auto", label: "跟随系统", note: "系统深色就深色" },
  { id: "warm", label: "暖纸", note: "米黄底，久看不累" },
  { id: "light", label: "冷白", note: "白天看清爽" },
  { id: "dark", label: "深色", note: "夜里不刺眼" },
];

export const SIZES = [
  { id: "s", label: "小", note: "一屏放得下更多" },
  { id: "m", label: "标准", note: "默认" },
  { id: "l", label: "大", note: "离屏幕远一点" },
];

export const state = {
  health: null,
  config: null,
  catalog: null,
  stats: null,
  lessons: [],
  plans: [],
  queue: [],
  cursor: 0,
  current: null,
  revealed: false,
  session: { done: 0, skipped: 0, history: [] },
  pronounce: null,
  prefs: { theme: "auto", size: "m", side: "open" },
  route: { view: "study", id: 0 },
};

const PREF_KEY = "etude-prefs";

export function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREF_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved && typeof saved === "object") {
      state.prefs = { ...state.prefs, ...saved };
    }
  } catch (err) {
    // localStorage 坏掉不该让界面起不来。
    state.prefs = { theme: "auto", size: "m", side: "open" };
  }
}

export function savePrefs(patch) {
  state.prefs = { ...state.prefs, ...patch };
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify(state.prefs));
  } catch (err) {
    /* 隐私模式下写不进去，忽略 */
  }
  applyLook();
}

/** 把偏好写到 <html> 上。所有颜色都靠这两个属性切换。 */
export function applyLook() {
  const root = document.documentElement;
  if (state.prefs.theme && state.prefs.theme !== "auto") {
    root.setAttribute("data-theme", state.prefs.theme);
  } else {
    root.removeAttribute("data-theme");
  }
  if (state.prefs.size && state.prefs.size !== "m") {
    root.setAttribute("data-size", state.prefs.size);
  } else {
    root.removeAttribute("data-size");
  }
  root.setAttribute("data-side", state.prefs.side === "collapsed" ? "collapsed" : "open");
}

export function audioUrl(name) {
  if (!name) return "";
  if (/^https?:\/\//i.test(name)) return name;
  return `/api/audio/${encodeURIComponent(name)}`;
}

export function imageUrl(name) {
  if (!name) return "";
  if (/^https?:\/\//i.test(name)) return name;
  return `/api/images/${encodeURIComponent(name)}`;
}

export function recordingUrl(name) {
  if (!name) return "";
  if (/^https?:\/\//i.test(name)) return name;
  return `/api/recordings/${encodeURIComponent(name)}`;
}

export function channelLabel(channel) {
  return CHANNEL_LABEL[channel] || channel || "—";
}
