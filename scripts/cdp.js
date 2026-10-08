/**
 * 一个很小的 Chrome DevTools 协议客户端。
 *
 * 为什么自己写而不是用 playwright / puppeteer：
 * 它们装起来要下几百 MB 的 Chromium，而系统上已经有 Chrome 了。
 * Node 22 自带 WebSocket，所以直连 CDP 只需要这些代码。
 *
 * 用 Node 22+ 运行。不需要 npm install。
 */

"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const DEFAULT_CHROME =
  process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class Browser {
  constructor(opts = {}) {
    this.chromePath = opts.chromePath || DEFAULT_CHROME;
    this.width = opts.width || 1280;
    this.height = opts.height || 940;
    this.consoleErrors = [];
    this.pageErrors = [];
    this._pending = new Map();
    this._nextId = 1;
    this._ws = null;
    this._chrome = null;
  }

  async launch() {
    if (!fs.existsSync(this.chromePath)) {
      throw new Error(
        `找不到 Chrome：${this.chromePath}\n用环境变量 CHROME_PATH 指定路径。`
      );
    }
    const port = 9333 + Math.floor(Math.random() * 400);
    const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "etude-chrome-"));
    this._chrome = spawn(
      this.chromePath,
      [
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        // 关掉代理：本机地址被代理改写成绝对地址的话，
        // 拿到的会是一个 404 而不是「连不上」，判断就全歪了。
        "--no-proxy-server",
        // 强制 1 倍缩放。不设的话，在有显示缩放（125% / 150%）的机器上，
        // 截出来的图会被按缩放比例缩小（实测 1280 宽截成了 1080），
        // 字就糊了 —— 文档截图糊掉比没有还难看。
        "--force-device-scale-factor=1",
        `--remote-debugging-port=${port}`,
        `--user-data-dir=${userDataDir}`,
        `--window-size=${this.width},${this.height}`,
        "about:blank",
      ],
      { stdio: "ignore" }
    );

    const target = await this._waitForTarget(port);
    this._ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      this._ws.addEventListener("open", resolve, { once: true });
      this._ws.addEventListener("error", () => reject(new Error("WebSocket 连不上")), {
        once: true,
      });
    });
    this._ws.addEventListener("message", (event) => this._onMessage(event));

    await this.send("Page.enable");
    await this.send("Runtime.enable");
    await this.send("Log.enable");
    return this;
  }

  _onMessage(event) {
    const msg = JSON.parse(event.data);
    if (msg.id && this._pending.has(msg.id)) {
      const { resolve, reject } = this._pending.get(msg.id);
      this._pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message));
      else resolve(msg.result);
      return;
    }
    if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
      this.consoleErrors.push(
        msg.params.args.map((a) => a.value ?? a.description ?? "").join(" ")
      );
    }
    if (msg.method === "Runtime.exceptionThrown") {
      const d = msg.params.exceptionDetails;
      this.pageErrors.push(d.exception?.description || d.text);
    }
  }

  send(method, params) {
    const id = this._nextId++;
    return new Promise((resolve, reject) => {
      this._pending.set(id, { resolve, reject });
      this._ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => {
        if (this._pending.has(id)) {
          this._pending.delete(id);
          reject(new Error(`${method} 超时`));
        }
      }, 30000);
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (result.exceptionDetails) {
      throw new Error(
        "页面里这段代码抛异常：" +
          (result.exceptionDetails.exception?.description ||
            result.exceptionDetails.text)
      );
    }
    return result.result?.value;
  }

  async goto(url, settleMs = 1600) {
    await this.send("Page.navigate", { url });
    await sleep(settleMs);
  }

  async reload(settleMs = 1600) {
    await this.send("Page.reload", { ignoreCache: true });
    await sleep(settleMs);
  }

  async waitFor(expression, label, timeoutMs = 12000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (await this.evaluate(expression)) return true;
      await sleep(150);
    }
    throw new Error(`等不到：${label || expression}`);
  }

  async screenshot(file) {
    const { data } = await this.send("Page.captureScreenshot", { format: "png" });
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, Buffer.from(data, "base64"));
    return file;
  }

  async setTheme(theme) {
    // 主题存在 localStorage 里，所以要先写进去再刷新。
    await this.evaluate(`localStorage.setItem('etude-theme', ${JSON.stringify(theme)})`);
    await this.reload();
  }

  async setViewport(width, height) {
    this.width = width;
    this.height = height;
    await this.send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: false,
    });
  }

  async close() {
    try { this._ws && this._ws.close(); } catch (e) { /* 忽略 */ }
    try { this._chrome && this._chrome.kill(); } catch (e) { /* 忽略 */ }
  }

  async _waitForTarget(port) {
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      try {
        const resp = await fetch(`http://127.0.0.1:${port}/json/list`);
        const list = await resp.json();
        const page = list.find((t) => t.type === "page");
        if (page && page.webSocketDebuggerUrl) return page;
      } catch (err) {
        /* 还没起来 */
      }
      await sleep(250);
    }
    throw new Error("Chrome 的调试端口一直没就绪");
  }
}

async function canReach(url) {
  try {
    const resp = await fetch(url + "/api/health");
    return resp.ok;
  } catch (err) {
    return false;
  }
}

module.exports = { Browser, canReach, sleep };
