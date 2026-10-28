/* 跟读面板：听原音 → 自己念 → 打分 → 看到哪一项最弱。
 *
 * 这块代码的三条原则：
 * 1. **没配转写服务也要能用。** 那时它只测「说完了没、卡不卡、声音够不够」，
 *    界面上明确写「内容对不对：未检查」，而不是给一个 0 分装作测过了。
 * 2. **录音不是必须留下的。** 设置里能关掉保存；关掉之后分数照记。
 * 3. **分数只是参考，一句话建议才是给人看的。**
 */

import { sendWav, qs, get } from "../core/api.js";
import { duration, stop as stopAudio } from "../core/audio.js";
import { el, paintIcons } from "../core/dom.js";
import { Recorder, micSupported } from "../core/recorder.js";
import { state, recordingUrl } from "../core/store.js";
import { errorBlock, toast } from "../core/ui.js";

const recorder = new Recorder();

const MODE_LABEL = {
  acoustic: "只看声音",
  mixed: "词级对比",
};

function statusNote() {
  const info = state.pronounce;
  if (!info) return "正在检查发音设置…";
  if (!info.enabled) return "发音训练在设置里关掉了。";
  return info.note || "";
}

function scoreTone(score) {
  if (score >= 85) return "ok";
  if (score >= 65) return "warn";
  return "bad";
}

/**
 * @param {object} options
 * @param {number} [options.cardId] 挂在某张卡上（分数会写进这张卡的历史）
 * @param {number} [options.exampleId] 挂在某条例句上（训练包详情用）
 * @param {string} options.sentence 目标句（只为显示；后端自己从库里取）
 * @param {string} [options.audioSrc] 模型语音地址，用来量「参考时长」
 * @param {boolean} [options.open] 是不是默认展开
 */
export function pronouncePanel({ cardId = 0, exampleId = 0, audioSrc = "", open = false }) {
  const box = el("section", { class: `pronounce${open ? " open" : ""}` });

  const micButton = el(
    "button",
    { class: "btn mic", type: "button" },
    [el("span", { class: "ico", dataset: { icon: "mic" } }), el("span", { text: "跟读一句" })],
  );
  const meter = el("div", { class: "meter", "aria-hidden": "true" }, [el("i")]);
  const timer = el("span", { class: "hint mono", text: "" });
  const note = el("span", { class: "hint grow", text: statusNote() });

  const resultHost = el("div", { class: "result" });
  const head = el("div", { class: "pronounce-head" }, [micButton, meter, timer, note]);

  box.append(head, resultHost);

  // 面板自己保证「那行说明」有内容：state.pronounce 是启动时取的，
  // 从别的页面直接刷新进训练页时它可能是空的 —— 那时这里就永远停在
  // 「正在检查发音设置…」上，看起来像卡住了。
  if (!state.pronounce) {
    get("/api/pronounce")
      .then((info) => {
        state.pronounce = info;
        note.textContent = statusNote();
      })
      .catch(() => {
        note.textContent = "读不到发音设置（后端没回应）。";
      });
  }

  let busy = false;

  function setMic(mode, text) {
    micButton.className = `btn mic ${mode}`.trim();
    micButton.replaceChildren(
      el("span", { class: "ico", dataset: { icon: mode === "recording" ? "stop" : "mic" } }),
      el("span", { text }),
    );
    paintIcons(micButton);
  }

  function drawResult(payload) {
    if (!payload) return;
    resultHost.replaceChildren();
    const items = payload.items || [];
    const score = Number(payload.score || 0);
    const good = items.filter((item) => item.score !== null);

    const verdict =
      score >= 85 ? "很稳" : score >= 70 ? "能用" : score >= 50 ? "再念一遍" : "再慢一点";

    const scoreNode = el("div", { class: `score ${scoreTone(score)}` }, [
      el("b", { text: String(Math.round(score)) }),
      el("span", { class: "unit", text: "分" }),
    ]);

    const rows = items.map((item) => {
      const value = item.score === null ? "—" : `${Math.round(Number(item.score))}`;
      return el("div", { class: `item-row ${item.level || ""}` }, [
        el("span", { class: "name", text: item.label || item.id }),
        el("span", { class: "value mono", text: item.value || value }),
        el("div", { class: "item-bar" }, [
          el("i", {
            style: { width: `${item.score === null ? 0 : Math.max(3, Number(item.score))}%` },
          }),
        ]),
        // detail 原样用后端给的话：「没配转写服务，这项不判（时长、停顿、音量照常测）」
        // 这种句子才是真正要说给用户听的，别用「未检查」把它顶掉。
        el("span", { class: "detail", text: item.detail || "" }),
      ]);
    });

    const heard = payload.heard
      ? el("div", { class: "heard" }, [
          el("span", { class: "lab", text: "听到的是" }),
          el("span", { text: payload.heard }),
        ])
      : null;

    const own = payload.audio
      ? el("audio", {
          controls: true,
          src: recordingUrl(payload.audio),
          // metadata 不是 none：none 时进度条上永远写 0:00 / 0:00，
          // 用户分不清「这段没录上」还是「还没加载」。
          preload: "metadata",
        })
      : null;

    const summary = payload.summary && payload.summary.count
      ? el("div", { class: "hint", text: summaryText(payload.summary) })
      : null;

    const advice = el("div", { class: "advice" }, [
      el("span", { class: "ico", dataset: { icon: "bulb" } }),
      el("span", { text: payload.advice || "" }),
    ]);

    // 逐词对照只在真做了转写时才有（后端只在那时给 words）——
    // 声学模式编一份「全命中」是假信息，看着还像是听懂了。
    const words =
      Array.isArray(payload.words) && payload.words.length
        ? el("div", { class: "words" }, [
            el("span", { class: "lab", text: "逐词对照" }),
            el(
              "span",
              { class: "word-row" },
              payload.words.map((word) =>
                el("span", { class: `w ${word.hit ? "hit" : "miss"}`, text: word.word }),
              ),
            ),
          ])
        : null;

    resultHost.append(
      el("div", { class: "score-line" }, [
        scoreNode,
        el("span", { class: `chip verdict ${scoreTone(score)}`, text: verdict }),
        el("span", { class: "chip mode", text: MODE_LABEL[payload.mode] || payload.mode || "" }),
        el("span", { class: "grow" }),
      ]),
      advice,
      el("div", { class: "items" }, rows),
      words,
      heard,
      own ? el("div", { class: "own" }, [el("span", { class: "lab", text: "你自己念的" }), own]) : null,
      summary,
      good.length ? null : el("div", { class: "hint", text: "这一遍没量到有效数据。" }),
    );
    // append(null) 会把字面量 "null" 画到页面上（截图里那行孤零零的 null 就是这么来的）。
    // 这里统一清一遍空槽：既能丢掉 null，也能丢掉条件表达式漏出来的 undefined。
    for (const child of [...resultHost.childNodes]) {
      if (child.nodeType === Node.TEXT_NODE) child.remove();
    }
    paintIcons(resultHost);
  }

  function summaryText(summary) {
    const bits = [`已经跟读 ${summary.count} 次`];
    if (summary.best) bits.push(`最高 ${Math.round(summary.best)} 分`);
    if (summary.delta) {
      const sign = summary.delta > 0 ? "+" : "";
      bits.push(`比第一次 ${sign}${Math.round(summary.delta)}`);
    }
    return bits.join(" · ");
  }

  async function upload(blob, seconds) {
    const params = qs({ ref_seconds: seconds ? seconds.toFixed(2) : "" });
    const path = cardId
      ? `/api/cards/${cardId}/attempt${params}`
      : `/api/examples/${exampleId}/attempt${params}`;
    resultHost.replaceChildren(el("div", { class: "hint", text: "正在评分…" }));
    const payload = await sendWav(path, blob);
    drawResult(payload);
    if (payload.score >= 85) toast("这一遍很稳。", "ok");
  }

  async function toggleRecord() {
    if (busy) return;
    stopAudio();
    if (!recorder.active) {
      if (!micSupported()) {
        resultHost.replaceChildren(
          errorBlock({
            message: "这个浏览器不支持录音。",
            hint: "用程序自带窗口（内嵌 Edge）或者 Chrome 打开。",
          }),
        );
        return;
      }
      try {
        await recorder.start({
          onLevel: (level) => {
            const percent = Math.min(100, Math.round(level * 320));
            meter.firstChild.style.width = `${percent}%`;
          },
          onTick: (seconds) => {
            timer.textContent = `${seconds.toFixed(1)} 秒`;
            if (seconds > 20) toggleRecord(); // 20 秒还没停，帮它停
          },
        });
      } catch (err) {
        setMic("idle", "跟读一句");
        resultHost.replaceChildren(errorBlock(err));
        return;
      }
      setMic("recording", "说完了，点这里");
      note.textContent = "把整句念完，再点一次停下。";
      return;
    }

    busy = true;
    setMic("idle", "评分中…");
    meter.firstChild.style.width = "0%";
    timer.textContent = "";
    let taken = null;
    try {
      taken = await recorder.stop();
    } finally {
      busy = false;
    }
    note.textContent = statusNote();
    if (!taken) {
      setMic("idle", "跟读一句");
      return;
    }
    if (!taken.samples) {
      setMic("idle", "跟读一句");
      resultHost.replaceChildren(
        errorBlock({ message: "这段录音是空的。", hint: "说话前先确认麦克风没静音。" }),
      );
      return;
    }
    let refSeconds = 0;
    if (audioSrc) refSeconds = await duration(audioSrc);
    try {
      await upload(taken.blob, refSeconds);
    } catch (err) {
      resultHost.replaceChildren(errorBlock(err));
    } finally {
      setMic("idle", "再念一遍");
    }
  }

  micButton.addEventListener("click", toggleRecord);

  // 展开状态记住了，下次进来不用再点一遍
  if (open) {
    const cached = state.pronounce;
    if (cached && !cached.enabled) {
      resultHost.replaceChildren(
        el("div", { class: "hint", text: "发音训练在设置里关掉了。想用的话去设置 → 发音。" }),
      );
    }
  }
  return box;
}

/** 训练页要复用同一个录音器：切卡时把正在录的停掉。 */
export function abortRecording() {
  if (recorder.active) recorder.cancel();
}
