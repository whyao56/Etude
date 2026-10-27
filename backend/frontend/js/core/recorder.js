/* 录音：麦克风 → Float32 → 16kHz 单声道 WAV。
 *
 * 三个必须处理好的现实问题：
 * 1. **没有麦克风 / 用户拒绝授权** —— 要一句话说清，不能让按钮点了没反应。
 * 2. **采样率不确定** —— 浏览器给 48k、44.1k 还是 16k 取决于设备，
 *    统一降到 16k 再编码（后端按 16k 分析，也因为体积）。
 * 3. **必须能停下来** —— 松开、点停、切走页面都要把轨道和 AudioContext 关掉，
 *    否则录音灯一直亮着，用户会以为程序在偷偷录。
 */

import { encodeWav, resample, TARGET_RATE } from "./wav.js";

export function micSupported() {
  return Boolean(
    typeof navigator !== "undefined" &&
      navigator.mediaDevices &&
      typeof navigator.mediaDevices.getUserMedia === "function" &&
      typeof window.AudioContext !== "undefined",
  );
}

export class RecorderError extends Error {
  constructor(message, hint = "") {
    super(message);
    this.name = "RecorderError";
    this.hint = hint;
  }
}

export class Recorder {
  constructor() {
    this.stream = null;
    this.context = null;
    this.node = null;
    this.source = null;
    this.gain = null;
    this.chunks = [];
    this.startedAt = 0;
    this.timer = null;
    this.onLevel = null;
    this.onTick = null;
    this.rate = TARGET_RATE;
  }

  get active() {
    return Boolean(this.stream);
  }

  async start({ onLevel = null, onTick = null } = {}) {
    if (this.active) return;
    if (!micSupported()) {
      throw new RecorderError(
        "这个浏览器不支持录音。",
        "用 Edge 或 Chrome 打开 Etude（程序默认就是内嵌 Edge）。",
      );
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (err) {
      const name = err && err.name ? err.name : "";
      if (name === "NotAllowedError" || name === "SecurityError") {
        throw new RecorderError(
          "麦克风权限被拒绝了。",
          "点浏览器地址栏左边的锁/相机图标，把麦克风改成允许，再回来点一次。",
        );
      }
      if (name === "NotFoundError" || name === "DevicesNotFoundError") {
        throw new RecorderError(
          "没找到麦克风。",
          "插上耳机麦克风或者在系统设置里启用一个输入设备。",
        );
      }
      throw new RecorderError(
        `打不开麦克风：${err && err.message ? err.message : err}`,
        "检查系统设置 → 隐私 → 麦克风里有没有允许浏览器使用。",
      );
    }

    this.onLevel = onLevel;
    this.onTick = onTick;
    this.chunks = [];
    this.stream = stream;
    this.rate = TARGET_RATE;
    this.startedAt = Date.now();

    const Context = window.AudioContext || window.webkitAudioContext;
    const context = new Context();
    // ScriptProcessorNode 确实被标了 deprecated，但它在所有内嵌 WebView 里
    // 都稳；AudioWorklet 要多一个文件 + 异步加载，为一个三十行的录音器不值得。
    const source = context.createMediaStreamSource(stream);
    const node = context.createScriptProcessor(4096, 1, 1);
    const gain = context.createGain();
    gain.gain.value = 0; // 不把麦克风声音放出来（否则会啸叫）
    node.onaudioprocess = (event) => {
      const channel = event.inputBuffer.getChannelData(0);
      this.chunks.push(new Float32Array(channel));
      if (this.onLevel) {
        let sum = 0;
        for (let i = 0; i < channel.length; i += 1) sum += channel[i] * channel[i];
        this.onLevel(Math.sqrt(sum / channel.length));
      }
    };
    source.connect(node);
    node.connect(gain);
    gain.connect(context.destination);

    this.context = context;
    this.source = source;
    this.node = node;
    this.gain = gain;
    this.timer = window.setInterval(() => {
      if (this.onTick) this.onTick(this.seconds());
    }, 100);
  }

  seconds() {
    return this.startedAt ? (Date.now() - this.startedAt) / 1000 : 0;
  }

  /** 停并返回 { blob, seconds, rate }。没有录音时返回 null。 */
  async stop() {
    if (!this.active) return null;
    const seconds = this.seconds();
    const chunks = this.chunks;
    const rate = this.context && this.context.sampleRate ? this.context.sampleRate : TARGET_RATE;
    const peak = this._peak();
    this._teardown();

    const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
    const samples = new Float32Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      samples.set(chunk, offset);
      offset += chunk.length;
    }
    const mono = resample(samples, rate, TARGET_RATE);
    const blob = encodeWav(mono, TARGET_RATE);
    return { blob, seconds, rate, peak, samples: mono.length };
  }

  /** 直接丢掉（用户点取消）。 */
  cancel() {
    this._teardown();
  }

  _peak() {
    let peak = 0;
    for (const chunk of this.chunks) {
      for (let i = 0; i < chunk.length; i += 1) {
        const value = Math.abs(chunk[i]);
        if (value > peak) peak = value;
      }
    }
    return peak;
  }

  _teardown() {
    if (this.timer) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
    try {
      if (this.node) {
        this.node.onaudioprocess = null;
        this.node.disconnect();
      }
      if (this.source) this.source.disconnect();
      if (this.gain) this.gain.disconnect();
    } catch (err) {
      /* 已经断了 */
    }
    try {
      if (this.stream) {
        for (const track of this.stream.getTracks()) track.stop();
      }
    } catch (err) {
      /* 忽略 */
    }
    try {
      if (this.context && this.context.state !== "closed") this.context.close();
    } catch (err) {
      /* 忽略 */
    }
    this.stream = null;
    this.context = null;
    this.node = null;
    this.source = null;
    this.gain = null;
    this.chunks = [];
    this.startedAt = 0;
    this.onLevel = null;
    this.onTick = null;
  }
}
