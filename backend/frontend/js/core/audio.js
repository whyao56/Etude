/* 播放：模型语音、用户自己的录音、以及系统语音兜底。
 *
 * 只有一处「当前在放什么」的状态 —— 否则会出现两段声音叠在一起，
 * 而用户根本不知道该点哪个键停下来。
 */

let current = null;
let currentTag = "";

export function stop() {
  if (!current) return;
  try {
    current.pause();
    current.currentTime = 0;
  } catch (err) {
    /* 已经结束了，忽略 */
  }
  current = null;
  currentTag = "";
}

export function playingTag() {
  return current ? currentTag : "";
}

/**
 * 放一段音频。返回 Promise：放完 resolve(true)，放不出来 reject。
 *
 * 用 <audio> 而不是 WebAudio 解码，理由很实在：model 生成的 mp3
 * 交给浏览器自己解最稳，我们不需要样本数据（只有录音才需要）。
 */
export function play(src, tag = "") {
  if (!src) {
    return Promise.reject(new Error("这条没有音频文件。"));
  }
  stop();
  const audio = new Audio(src);
  audio.preload = "auto";
  current = audio;
  currentTag = tag;
  return new Promise((resolve, reject) => {
    audio.addEventListener("ended", () => {
      if (current === audio) stop();
      resolve(true);
    });
    audio.addEventListener("error", () => {
      if (current === audio) stop();
      reject(new Error("这条音频放不出来（文件可能不在了）。"));
    });
    const started = audio.play();
    if (started && typeof started.catch === "function") {
      started.catch((err) => {
        if (current === audio) stop();
        reject(new Error(`浏览器不让放：${err && err.message ? err.message : err}`));
      });
    }
  });
}

/** 量一段音频有多长（秒）。拿不到返回 0 —— 评分那边会按词数估。 */
export function duration(src) {
  if (!src) return Promise.resolve(0);
  return new Promise((resolve) => {
    const audio = new Audio();
    const done = (value) => resolve(Number.isFinite(value) && value > 0 ? value : 0);
    audio.preload = "metadata";
    audio.addEventListener("loadedmetadata", () => done(audio.duration));
    audio.addEventListener("error", () => done(0));
    window.setTimeout(() => done(audio.duration), 4000);
    audio.src = src;
  });
}

export function canSpeak() {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

/**
 * 系统语音兜底：模型语音没生成出来时，至少还能听见这句怎么念。
 *
 * 它不是训练语音：音色取决于系统装了什么，语速也没法按设置调。
 * 界面上必须说清楚是「系统语音」，否则用户会以为模型念错了。
 */
export function speak(text, lang = "en-US") {
  if (!canSpeak() || !text) return false;
  try {
    window.speechSynthesis.cancel();
    const utter = new SpeechSynthesisUtterance(text);
    utter.lang = lang;
    utter.rate = 0.95;
    window.speechSynthesis.speak(utter);
    return true;
  } catch (err) {
    return false;
  }
}

export function stopSpeak() {
  if (!canSpeak()) return;
  try {
    window.speechSynthesis.cancel();
  } catch (err) {
    /* 忽略 */
  }
}
