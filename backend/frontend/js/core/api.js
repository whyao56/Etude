/* 跟后端说话的唯一出口。
 *
 * 两件事必须由这里统一做掉：
 * 1. **把 hint 带到前端。** 后端每个失败响应都带一句「接下来该干什么」，
 *    丢掉它就等于把「检查 API key」变成「HTTP 401」。
 * 2. **把异常变成 Error，并把 hint 挂在 err.hint 上。** 视图层只负责显示，
 *    不负责猜这个错从哪来。
 */

const JSON_HEADERS = { "Content-Type": "application/json" };

function errorText(payload, status) {
  if (payload && typeof payload === "object") {
    if (typeof payload.error === "string" && payload.error) return payload.error;
    if (typeof payload.detail === "string" && payload.detail) return payload.detail;
    if (Array.isArray(payload.detail) && payload.detail.length) {
      // FastAPI 的校验错误：把「哪个字段、为什么」拼成一句人话。
      const parts = payload.detail.map((item) => {
        const where = Array.isArray(item.loc) ? item.loc.slice(1).join(".") : "";
        return where ? `${where}: ${item.msg}` : item.msg;
      });
      return parts.join("；");
    }
  }
  return `请求失败（HTTP ${status}）`;
}

export async function api(path, options = {}) {
  const init = { method: "GET", ...options };
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    const wrapped = new Error("连不上本机的程序。它可能已经退出了。");
    wrapped.hint = "重开 Etude.exe。如果反复出现，把数据目录里的 logs 发给作者。";
    wrapped.cause = err;
    throw wrapped;
  }
  const text = await res.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      payload = { error: text.slice(0, 300) };
    }
  }
  if (!res.ok) {
    const error = new Error(errorText(payload, res.status));
    error.status = res.status;
    error.hint = (payload && payload.hint) || "";
    error.kind = (payload && payload.kind) || "";
    error.payload = payload;
    throw error;
  }
  return payload;
}

export function get(path) {
  return api(path);
}

export function post(path, body) {
  return api(path, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body ?? {}) });
}

export function patch(path, body) {
  return api(path, { method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify(body ?? {}) });
}

export function put(path, body) {
  return api(path, { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(body ?? {}) });
}

export function del(path) {
  return api(path, { method: "DELETE" });
}

/** 录音上传：身体就是 WAV 字节，不套 JSON。 */
export function sendWav(path, blob) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "audio/wav" },
    body: blob,
  });
}

/** 把对象拼成 query string，空值一律省掉 —— 后端靠默认值兜。 */
export function qs(params) {
  const parts = [];
  for (const key of Object.keys(params || {})) {
    const value = params[key];
    if (value === null || value === undefined || value === "" || value === 0) continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(value)}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

/** 视图层用这个把错误画出来，不用知道后端长什么样。 */
export function errorMessage(err) {
  return {
    text: err && err.message ? err.message : String(err),
    hint: (err && err.hint) || "",
  };
}
