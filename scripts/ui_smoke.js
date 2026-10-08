/**
 * 界面冒烟检查：在**真浏览器**里把主要流程点一遍，并收集控制台错误。
 *
 * 为什么需要它：这是个零构建的单文件前端，没有任何东西能在编译期
 * 告诉你「这个函数不存在」或者「这里读了一个 undefined 的属性」——
 * 症状只会是「点了没反应」。这个脚本把那种情况变成一行红字。
 *
 * 它不替代 pytest：断言都在 Python 那边（109 项）。这里只回答一个问题：
 * **界面在真浏览器里跑得起来吗，有没有报错。**
 *
 * 用 Node 22 自带的 WebSocket 直连 Chrome DevTools 协议，
 * 不装 playwright / puppeteer（这台机器上装它们要下几百 MB）。
 *
 * 用法：
 *     node scripts/ui_smoke.js [baseUrl]
 *     CHROME_PATH=/path/to/chrome node scripts/ui_smoke.js
 *
 * 退出码：0 全过；1 有断言失败；2 环境问题（找不到 Chrome / 起不来）。
 */

"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const BASE = (process.argv[2] || "http://127.0.0.1:8977").replace(/\/$/, "");
const CHROME =
  process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";

const SHOT_DIR = process.env.SHOT_DIR || path.join(os.tmpdir(), "etude-shots");
const DEBUG_PORT = 9333 + Math.floor(Math.random() * 200);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let nextId = 1;
let ws = null;
const pending = new Map();
const consoleErrors = [];
const pageErrors = [];

function send(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(`${method} 超时`));
      }
    }, 30000);
  });
}

async function evaluate(expression) {
  const result = await send("Runtime.evaluate", {
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

async function shot(name) {
  try {
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    fs.mkdirSync(SHOT_DIR, { recursive: true });
    const file = path.join(SHOT_DIR, `${name}.png`);
    fs.writeFileSync(file, Buffer.from(data, "base64"));
    return file;
  } catch (err) {
    return null;
  }
}

async function waitFor(expression, label, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await sleep(150);
  }
  throw new Error(`等不到：${label}（条件 ${expression}）`);
}

const checks = [];
function check(label, ok, detail) {
  checks.push({ label, ok, detail });
  console.log(`${ok ? "  OK  " : " FAIL "} ${label}${detail ? " — " + detail : ""}`);
}

async function goto(hash) {
  await send("Page.navigate", { url: `${BASE}/${hash || ""}` });
  await sleep(1600);
}

async function main() {
  if (!fs.existsSync(CHROME)) {
    console.error(`找不到 Chrome：${CHROME}\n用 CHROME_PATH 指定路径。`);
    return 2;
  }
  if (!(await canReach(BASE))) {
    console.error(`连不上 ${BASE}，先把服务起起来。`);
    return 2;
  }

  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "etude-chrome-"));
  const chrome = spawn(
    CHROME,
    [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      "--no-first-run",
      "--no-default-browser-check",
      "--no-proxy-server",
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${userDataDir}`,
      "--window-size=1280,940",
      "about:blank",
    ],
    { stdio: "ignore" }
  );

  try {
    const target = await waitForTarget();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", () => reject(new Error("WebSocket 连不上")), {
        once: true,
      });
    });

    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && pending.has(msg.id)) {
        const { resolve, reject } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message));
        else resolve(msg.result);
        return;
      }
      if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
        consoleErrors.push(
          msg.params.args.map((a) => a.value ?? a.description ?? "").join(" ")
        );
      }
      if (msg.method === "Runtime.exceptionThrown") {
        const d = msg.params.exceptionDetails;
        pageErrors.push(d.exception?.description || d.text);
      }
    });

    await send("Page.enable");
    await send("Runtime.enable");
    await send("Log.enable");

    console.log("\n=== 训练页 ===");
    await goto("#study");
    check("训练页渲染出顶部导航", (await evaluate("!!document.querySelector('#nav')")) === true);
    check(
      "版本徽章显示了版本号",
      /v\d+\.\d+\.\d+/.test(await evaluate("document.querySelector('#health').textContent")),
      await evaluate("document.querySelector('#health').textContent")
    );

    const hasCard = await evaluate("!!document.querySelector('.stage .chan')");
    if (hasCard) {
      check("取到并渲染了一张卡", true);
      const before = await evaluate("document.querySelector('.stage').textContent");
      await evaluate("reveal()");
      await sleep(400);
      const after = await evaluate("document.querySelector('.stage').textContent");
      check("翻面后出现了背面内容", after !== before && after.length > before.length);
      check(
        "翻面后评分按钮解除了禁用",
        (await evaluate(
          "[...document.querySelectorAll('.grade')].filter(b=>b.hasAttribute('disabled')).length"
        )) === 0
      );
      await shot("study-revealed");

      const cursorBefore = await evaluate("state.cursor");
      await evaluate("grade(2)");
      await sleep(900);
      check(
        "评分之后进入了下一张",
        (await evaluate("state.cursor")) > cursorBefore,
        `cursor ${cursorBefore} → ${await evaluate("state.cursor")}`
      );
    } else {
      check("队列里有待练的卡", false, "队列是空的，先造点数据");
    }

    console.log("\n=== 训练包页 ===");
    await goto("#lessons");
    await waitFor("document.querySelectorAll('.lesson').length > 0", "训练包列表");
    const lessonCount = await evaluate("document.querySelectorAll('.lesson').length");
    check("列出了训练包", lessonCount > 0, `${lessonCount} 个`);
    await shot("lessons");

    // 打开第一个包
    await evaluate(`
      [...document.querySelectorAll('.lesson button')]
        .find(b => b.textContent.trim() === '打开').click()
    `);
    await waitFor("document.querySelectorAll('.example').length > 0", "例句列表");
    const exCount = await evaluate("document.querySelectorAll('.example').length");
    check("打开了训练包并列出例句", exCount > 0, `${exCount} 条`);
    check(
      "每条例句都有五种练法的入口",
      (await evaluate(
        "Math.min(...[...document.querySelectorAll('.example')].map(e => e.querySelectorAll('.chanbtns button').length))"
      )) === 5
    );
    await shot("lesson-detail");

    console.log("\n=== 打字卡 ===");
    // 第一条例句的「练打字」
    await evaluate(`
      [...document.querySelectorAll('.example')[0].querySelectorAll('.chanbtns button')]
        .find(b => b.textContent.includes('打字')).click()
    `);
    await waitFor("!!document.querySelector('.typerow input')", "打字输入框");
    check("进入了打字训练", true);
    check(
      "打字卡正面不显示英文原文（否则就是抄写而不是回忆）",
      (await evaluate(
        "!document.querySelector('.stage').textContent.includes('I really')"
      )) === true
    );

    // 故意打错几个词，看看差异比对是不是真的在工作
    await evaluate(`
      (() => {
        const input = document.querySelector('.typerow input');
        input.value = 'I really appreciate the way this one work part 2';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        return true;
      })()
    `);
    await sleep(500);
    check("提交后出现了逐词差异比对", (await evaluate("!!document.querySelector('.diff')")) === true);
    check(
      "差异比对里既有命中的词也有没对上的词",
      (await evaluate("!!document.querySelector('.diff .hit') && !!document.querySelector('.diff .miss')")) === true
    );
    await shot("type-card");

    console.log("\n=== 设置页 ===");
    await goto("#settings");
    await waitFor("document.querySelectorAll('table.self tr').length > 0", "自检表");
    const selfRows = await evaluate("document.querySelectorAll('table.self tr').length");
    check("自检面板列出了检查项", selfRows >= 5, `${selfRows} 项`);
    check(
      "大模型那一项显示了配置状态",
      (await evaluate(
        "[...document.querySelectorAll('table.self tr')].some(r => r.textContent.includes('大模型'))"
      )) === true
    );
    check(
      "密钥不会回填到输入框里",
      (await evaluate("document.querySelector('#llm-key').value")) === ""
    );
    await shot("settings");

    console.log("\n=== 深链接 ===");
    await goto("#settings");
    check("刷新后仍停在设置页", (await evaluate("state.view")) === "settings");
    await goto("#lessons");
    check("直接打开 #lessons 会进训练包页", (await evaluate("state.view")) === "lessons");

    console.log("\n=== 控制台 ===");
    check("没有未捕获的页面异常", pageErrors.length === 0, pageErrors.join(" | "));
    check("没有 console.error", consoleErrors.length === 0, consoleErrors.join(" | "));
    check(
      "错误横幅没有被触发",
      (await evaluate("document.querySelector('#banner').className")) !== "bad"
    );

    const failed = checks.filter((c) => !c.ok);
    console.log(
      `\n${checks.length - failed.length}/${checks.length} 项通过` +
        (failed.length ? `，${failed.length} 项失败` : "")
    );
    console.log(`截图目录：${SHOT_DIR}`);
    return failed.length ? 1 : 0;
  } finally {
    try { ws && ws.close(); } catch (e) { /* 忽略 */ }
    try { chrome.kill(); } catch (e) { /* 忽略 */ }
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

async function waitForTarget() {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
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

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error("界面检查出错：" + err.message);
    process.exit(1);
  });
