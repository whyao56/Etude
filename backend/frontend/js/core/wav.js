/* 手写 16bit PCM WAV 编码。
 *
 * 为什么不用 MediaRecorder：它给的是 webm/opus，后端要解它就得塞一个
 * ffmpeg 或 libopus 进来（打包体积 + 许可）。而我们只需要「一句话的
 * 波形」—— 16kHz 单声道 16bit 足够，一段 3 秒的录音 ~96KB，
 * 编码器也就三十行。
 */

const TARGET_RATE = 16000;

/** Float32（-1..1）→ 16bit PCM 小端字节。多声道已经在上游降成单声道。 */
function pcm16(samples) {
  const out = new Uint8Array(samples.length * 2);
  const view = new DataView(out.buffer);
  for (let i = 0; i < samples.length; i += 1) {
    let value = samples[i];
    if (value > 1) value = 1;
    else if (value < -1) value = -1;
    // 负半轴乘 0x8000、正半轴乘 0x7fff —— 否则 +1 会溢出成 -32768。
    const int16 = value < 0 ? value * 0x8000 : value * 0x7fff;
    view.setInt16(i * 2, Math.round(int16), true);
  }
  return out;
}

/** 线性插值降采样。语音只有 16kHz 有效带宽，高采样率多出来的只是体积。 */
export function resample(samples, fromRate, toRate = TARGET_RATE) {
  if (!fromRate || fromRate === toRate) return samples;
  const ratio = fromRate / toRate;
  const length = Math.max(1, Math.floor(samples.length / ratio));
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const pos = i * ratio;
    const left = Math.floor(pos);
    const right = Math.min(samples.length - 1, left + 1);
    const frac = pos - left;
    out[i] = samples[left] * (1 - frac) + samples[right] * frac;
  }
  return out;
}

function writeAscii(view, offset, text) {
  for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
}

/** 44 字节头 + PCM。字段顺序就是 RIFF 规范，别改。 */
export function encodeWav(samples, sampleRate = TARGET_RATE) {
  const data = pcm16(samples);
  const buffer = new ArrayBuffer(44 + data.length);
  const view = new DataView(buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + data.length, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits
  writeAscii(view, 36, "data");
  view.setUint32(40, data.length, true);
  new Uint8Array(buffer, 44).set(data);
  return new Blob([buffer], { type: "audio/wav" });
}

export { TARGET_RATE };
