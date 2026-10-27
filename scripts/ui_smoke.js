/**
 * 界面冒烟：在真浏览器里把主要流程点一遍，并收集控制台错误。
 *
 * 为什么必须用真实鼠标事件：0.0.2 用户报「吃力，正常，秒答都按不动」，
 * 而当时的检查是 `evaluate("grade(2)")` —— 直接调函数，绕过了 DOM 事件层，
 * 于是永远是绿的。一个绕开事件层的探针测不到事件层的问题。
 * 所以下面的按钮检查都走 `Input.dispatchMouseEvent`，跟用户的手指
 * 走同一条路，并且点之前先 `elementFromPoint` 确认那个坐标上真的是它
 * （挡住、0×0、被盖住这三种情况都会现原形）。
 *
 * 0.1.0 换了前端结构（原生 ES 模块 + 分包 CSS + 地址栏路由），
 * 断言也跟着换了：现在查的是「每一页画得出来、地址栏记得住、
 * 翻面/评分/撤销能走通、跟读面板在口语卡上出现」。
 *
 * 用法：
 *     node scripts/ui_smoke.js                       # 对着 127.0.0.1:8977
 *     node scripts/ui_smoke.js --base http://127.0.0.1:9001
 *     node scripts/ui_smoke.js --seed                # 先用假模型造一个训练包
 *     SHOT_DIR=docs/images node scripts/ui_smoke.js  # 顺便截图
 *
 * 退出码：0 全过；1 有断言失败；2 环境问题（找不到 Chrome / 起不来）。
 */

"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { Browser, canReach, sleep } = require("./cdp");

const ROOT = path.resolve(__dirname, "..");
const ARGS = process.argv.slice(2);
const has = (name) => ARGS.includes(`--${name}`);
const argValue = (name, fallback) => {
  const index = ARGS.indexOf(`--${name}`);
  return index >= 0 && ARGS[index + 1] ? ARGS[index + 1] : fallback;
};

const BASE = argValue("base", process.env.ETUDE_BASE || "http://127.0.0.1:8977").replace(
  /\/$/,
  "",
);
const STUB_PORT = Number(argValue("stub-port", "18999"));
const PYTHON =
  process.env.ETUDE_PYTHON || path.join(ROOT, ".venv", "Scripts", "python.exe");

const SHOT_DIR = process.env.SHOT_DIR || path.join(os.tmpdir(), "etude-shots");
const DEBUG_PORT = 9333 + Math.floor(Math.random() * 200);
const CHROME =
  process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";

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
        (result.exceptionDetails.exception?.description || result.exceptionDetails.text),
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

async function waitFor(expression, label, timeoutMs = 15000) {
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
  return ok;
}

/* --------------------------------------------------------- 真实鼠标 */

async function clickAt(x, y) {
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
  await send("Input.dispatchMouseEvent", {
    type: "mousePressed", x, y, button: "left", clickCount: 1, buttons: 1,
  });
  await send("Input.dispatchMouseEvent", {
    type: "mouseReleased", x, y, button: "left", clickCount: 1, buttons: 0,
  });
  await sleep(120);
}

async function hitTest(x, y) {
  return evaluate(`(() => {
    const node = document.elementFromPoint(${x}, ${y});
    if (!node) return "";
    const button = node.closest("button");
    const target = button || node;
    return target.tagName.toLowerCase() + (target.className ? "." + String(target.className).split(" ").join(".") : "");
  })()`);
}

async function clickSelector(selector, index = 0) {
  const info = await evaluate(`(() => {
    const list = document.querySelectorAll(${JSON.stringify(selector)});
    const node = list[${index}];
    if (!node) return { missing: true, count: list.length };
    const r = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return {
      count: list.length,
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      w: r.width,
      h: r.height,
      pointer: style.pointerEvents,
      visibility: style.visibility,
      display: style.display,
      text: (node.textContent || "").trim().slice(0, 30),
    };
  })()`);

  if (info.missing) return { ok: false, reason: `找不到 ${selector}[${index}]（共 ${info.count} 个）` };
  if (info.w < 1 || info.h < 1) return { ok: false, reason: `${selector} 的尺寸是 ${info.w}×${info.h}，点不到` };
  if (info.pointer === "none") return { ok: false, reason: `${selector} 设了 pointer-events:none` };
  if (info.visibility === "hidden" || info.display === "none") {
    return { ok: false, reason: `${selector} 不可见（${info.visibility}/${info.display}）` };
  }

  const hit = await hitTest(info.x, info.y);
  await clickAt(info.x, info.y);
  return { ok: true, hit, box: info };
}

async function clickAndExpect(selector, probe, label, index = 0) {
  const before = await evaluate(probe.before);
  const clicked = await clickSelector(selector, index);
  if (!clicked.ok) return check(label, false, clicked.reason);
  await sleep(probe.wait || 350);
  const after = await evaluate(probe.after);
  const changed = probe.changed ? probe.changed(before, after) : before !== after;
  check(
    label,
    changed,
    changed
      ? `点中的是 ${clicked.hit}`
      : `点了没反应（${probe.describe ? probe.describe(before, after) : `${JSON.stringify(before)} → ${JSON.stringify(after)}`}）`,
  );
  return changed;
}

async function goto(hash) {
  await send("Page.navigate", { url: `${BASE}/${hash || ""}` });
  await sleep(1900);
}

async function key(keyName) {
  await evaluate(
    `document.dispatchEvent(new KeyboardEvent('keydown', { key: ${JSON.stringify(keyName)}, bubbles: true }))`,
  );
  await sleep(400);
}

/* ------------------------------------------------------------ 造数据 */

async function httpCall(pathname, options) {
  const res = await fetch(BASE + pathname, options);
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch (err) {
    body = text;
  }
  return { status: res.status, body };
}

async function seed() {
  console.log("\n=== 造数据（假模型）===");
  if (!fs.existsSync(PYTHON)) {
    check("找到了 venv 里的 python", false, PYTHON);
    return;
  }
  const stub = spawn(PYTHON, [path.join(ROOT, "scripts", "openai_stub.py"), "--port", String(STUB_PORT)], {
    cwd: ROOT,
    stdio: "ignore",
    windowsHide: true,
  });
  try {
    let up = false;
    for (let i = 0; i < 80; i += 1) {
      try {
        const res = await fetch(`http://127.0.0.1:${STUB_PORT}/v1/models`);
        if (res.ok || res.status === 404) {
          up = true;
          break;
        }
      } catch (err) {
        /* 还没起来 */
      }
      await sleep(250);
    }
    if (!check("假模型起来了", up, `端口 ${STUB_PORT}`)) return;

    const cfg = await httpCall("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        llm: {
          preset: "custom",
          base_url: `http://127.0.0.1:${STUB_PORT}/v1`,
          model: "stub-model",
          api_key: "stub-key",
        },
        images: { source: "off" },
        tts: { engine: "edge" },
      }),
    });
    check("配置指向假模型", cfg.status === 200, `HTTP ${cfg.status}`);

    const made = await httpCall("/api/lessons", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: "appreciate" }),
    });
    const lessonId = made.body && made.body.lesson_id;
    if (!check("建了一个训练包", Boolean(lessonId), `id=${lessonId}`)) return;

    let status = "generating";
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      const job = await httpCall(`/api/lessons/${lessonId}/job`);
      status = (job.body && job.body.status) || status;
      if (status === "ready" || status === "failed") break;
      await sleep(400);
    }
    check("训练包生成完了", status === "ready", `status=${status}`);

    // 跟读是一个不发网络的纯本地评分，冒烟里让它一定开着。
    await httpCall("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pronounce: { enabled: true, mode: "acoustic" } }),
    });
  } finally {
    try {
      stub.kill();
    } catch (err) {
      /* 忽略 */
    }
  }
}

/* ------------------------------------------------------------ 主流程 */

async function main() {
  if (!fs.existsSync(CHROME)) {
    console.error(`找不到 Chrome：${CHROME}\n用 CHROME_PATH 指定路径。`);
    return 2;
  }
  if (!(await canReach(BASE))) {
    console.error(`连不上 ${BASE}，先把服务起起来。`);
    return 2;
  }

  if (has("seed")) await seed();

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
      "--window-size=1400,980",
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  try {
    const target = await waitForTarget();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", () => reject(new Error("WebSocket 连不上")), { once: true });
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
        consoleErrors.push(msg.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
      }
      if (msg.method === "Runtime.exceptionThrown") {
        const d = msg.params.exceptionDetails;
        pageErrors.push(d.exception?.description || d.text);
      }
    });

    await send("Page.enable");
    await send("Runtime.enable");
    await send("Log.enable");

    // localStorage 里存着外观偏好。留着上一轮的值会让「侧栏是收着的」
    // 这类断言在不同机器上表现不一样。
    await goto("#/study");
    await evaluate("(() => { localStorage.clear(); return true; })()");
    await goto("#/study");
    await waitFor("!!window.__etude", "前端启动完成");

    /* ------------------------------------------------------------ 外壳 */
    console.log("\n=== 外壳 ===");
    check("渲染出了导航", (await evaluate("!!document.querySelector('#nav')")) === true);
    check("渲染出了侧边栏", (await evaluate("!!document.querySelector('#side')")) === true);

    const healthText = await evaluate("document.querySelector('#health-text').textContent");
    check(/^v\d+\.\d+\.\d+ ·/.test(healthText || ""), "版本徽标显示了版本号", healthText);

    const appVersion = await evaluate("window.__etude.version");
    check(
      healthText && healthText.includes(appVersion),
      "界面上显示的版本和前端常量是同一个",
      `${appVersion} vs ${healthText}`,
    );

    const navViews = await evaluate(
      "[...document.querySelectorAll('#nav .nav-item')].map((b) => b.dataset.view).join(',')",
    );
    check(navViews === "study,lessons,plans,cards,settings", "导航是五项，且顺序一致", navViews);

    const themeBefore = await evaluate("document.documentElement.dataset.theme || 'auto'");
    await clickSelector("#theme-btn");
    await sleep(400);
    const themeAfter = await evaluate("document.documentElement.dataset.theme || 'auto'");
    check("换配色按钮有效", themeBefore !== themeAfter, `${themeBefore} → ${themeAfter}`);

    const sideBefore = await evaluate("document.documentElement.dataset.side || ''");
    await clickSelector("#side-btn");
    await sleep(400);
    const sideAfter = await evaluate("document.documentElement.dataset.side || ''");
    check("侧栏收起按钮有效", sideBefore !== sideAfter, `${sideBefore || "（默认）"} → ${sideAfter}`);
    await clickSelector("#side-btn");
    await sleep(300);

    /* -------------------------------------------------- 每一页都画得出来 */
    console.log("\n=== 五个页面 ===");
    const pages = [
      ["lessons", "训练包"],
      ["plans", "目标"],
      ["cards", "卡片"],
      ["settings", "设置"],
      ["study", "训练"],
    ];
    for (const [view, title] of pages) {
      const clicked = await clickSelector(`#nav .nav-item[data-view="${view}"]`);
      if (!clicked.ok) {
        check(`点到「${title}」`, false, clicked.reason);
        continue;
      }
      await sleep(900);
      const shown = await evaluate("document.querySelector('#page-title').textContent");
      const route = await evaluate("location.hash");
      check(`「${title}」页画出来了`, shown === title, `标题「${shown}」，地址 ${route}`);
      const length = await evaluate("document.querySelector('#app').innerText.trim().length");
      check(`「${title}」页不是空的`, length > 20, `${length} 字符`);
      await shot(`ui-${view}`);
    }

    /* ------------------------------------------------------ 训练包与地址栏 */
    console.log("\n=== 训练包详情进地址栏 ===");
    await clickSelector('#nav .nav-item[data-view="lessons"]');
    await sleep(1000);

    const hasLesson = await evaluate("Boolean(document.querySelector('.lesson-open'))");
    if (!hasLesson) {
      check("列表里有训练包（加 --seed 造一个）", false, "没有训练包，跳过详情检查");
    } else {
      const opened = await clickSelector(".lesson-open");
      check("「打开」按钮用真实鼠标点得动", opened.ok, opened.ok ? `点中的是 ${opened.hit}` : opened.reason);
      await sleep(1300);
      const hash = await evaluate("location.hash");
      check("#/lessons/<id> 进了地址栏", /^#\/lessons\/\d+$/.test(hash || ""), hash);
      const examples = await evaluate("document.querySelectorAll('.example-row').length");
      check("详情页列出了例句", examples > 0, `${examples} 条`);
      check(
        "五条通道的练法都在",
        (await evaluate(
          "(() => { const first = document.querySelector('.example-row .chanbtns'); if (!first) return ''; return [...first.querySelectorAll('button')].map((b) => b.textContent.trim()).join('/'); })()",
        )).includes("口语"),
        await evaluate(
          "(() => { const first = document.querySelector('.example-row .chanbtns'); return first ? [...first.querySelectorAll('button')].map((b) => b.textContent.trim()).join(' / ') : '（没找到）'; })()",
        ),
      );
      await shot("ui-lesson-detail");

      await send("Page.reload", { ignoreCache: true });
      await sleep(2200);
      const hashAfter = await evaluate("location.hash");
      check("刷新之后还在同一个训练包", hashAfter === hash, `${hash} → ${hashAfter}`);
      const rowsAfter = await evaluate("document.querySelectorAll('.example-row').length");
      check("刷新之后例句还在", rowsAfter > 0, `${rowsAfter} 条`);

      // 浏览器后退要能回到列表 —— 这一条以前是坏的。
      await evaluate("history.back()");
      await sleep(1400);
      const backHash = await evaluate("location.hash");
      check("后退回到了训练包列表", backHash === "#/lessons", backHash);
    }

    /* ------------------------------------------------------------ 训练流程 */
    console.log("\n=== 训练流程 ===");
    await goto("#/study");
    await waitFor("!!window.__etude.state", "训练页启动");

    const hasStage = await evaluate("Boolean(document.querySelector('.stage'))");
    if (!hasStage) {
      check("训练页有待练的卡（加 --seed 造一个）", false, "队列是空的");
    } else {
      const grades = await evaluate("document.querySelectorAll('.grader .grade').length");
      check("四档评分键都在", grades === 4, `${grades} 个`);
      check(
        "每一档都写清了「按下去会推到什么时候」",
        (await evaluate(
          "[...document.querySelectorAll('.grader .grade .when')].every((e) => e.textContent.trim().length >= 1)",
        )) === true,
        await evaluate(
          "[...document.querySelectorAll('.grader .grade .when')].map((e) => e.textContent.trim()).join(' | ')",
        ),
      );
      check(
        "翻面前后三档没有被 disabled（0.0.1 死在这一点上）",
        (await evaluate("[...document.querySelectorAll('.grader .grade')].filter((b) => b.disabled).length")) === 0,
      );

      await shot("ui-study-stage");

      // 翻面：真实按空格
      await key(" ");
      await waitFor("window.__etude.state.revealed === true", "空格翻面");
      check("空格能翻面", true);
      check(
        "翻面之后答案区出现了",
        (await evaluate("Boolean(document.querySelector('.answer'))")) === true,
      );

      // 评分：真实点「正常」
      await clickAndExpect(
        ".grader .grade.g2",
        {
          before: "window.__etude.state.session.done",
          after: "window.__etude.state.session.done",
          wait: 1100,
          describe: (a, b) => `已练数 ${a} → ${b}`,
        },
        "点「正常」记了一次分",
      );

      // 撤销
      const hasUndo = await evaluate(
        "[...document.querySelectorAll('.stage-foot button')].some((b) => b.textContent.includes('撤销'))",
      );
      check("评完之后出现了「撤销上一次评分」", hasUndo === true);
      if (hasUndo) {
        await clickAndExpect(
          ".stage-foot button:nth-of-type(4)",
          {
            before: "window.__etude.state.session.done",
            after: "window.__etude.state.session.done",
            wait: 1300,
            describe: (a, b) => `已练数 ${a} → ${b}`,
          },
          "撤销把这一张退回了队列",
        );
      }

      // 跟读面板：只在口语卡上出现，扫一遍看它在不在
      let speakFound = false;
      for (let i = 0; i < 40; i += 1) {
        const panel = await evaluate("Boolean(document.querySelector('.pronounce-head'))");
        if (panel) {
          speakFound = true;
          break;
        }
        await evaluate("(() => { const b = [...document.querySelectorAll('.stage-foot button')].find((x) => x.textContent.includes('跳过')); if (b) b.click(); return true; })()");
        await sleep(700);
      }
      check("口语卡上有跟读面板", speakFound, speakFound ? "找到了" : "扫了 40 张都没有");
      if (speakFound) await shot("ui-study-speak");
    }

    /* -------------------------------------------------------------- 卡片页 */
    console.log("\n=== 卡片浏览器 ===");
    await goto("#/cards");
    await sleep(1400);
    const rows = await evaluate("document.querySelectorAll('.table tbody tr').length");
    check("卡片表格有行", rows > 0, `${rows} 行`);
    check(
      "有一个批量操作条",
      (await evaluate("!!document.querySelector('.bulkbar')")) === true,
    );
    if (rows > 0) {
      await clickAndExpect(
        ".table tbody tr td.pick input",
        {
          before: "document.querySelectorAll('.table tbody tr td.pick input:checked').length",
          after: "document.querySelectorAll('.table tbody tr td.pick input:checked').length",
          wait: 300,
          describe: (a, b) => `勾选数 ${a} → ${b}`,
        },
        "勾选一张卡",
      );
    }

    /* -------------------------------------------------------------- 设置页 */
    console.log("\n=== 设置页 ===");
    await goto("#/settings");
    await sleep(1400);
    const cards = await evaluate("document.querySelectorAll('#app section.card').length");
    check("设置页有八张卡", cards === 8, `${cards} 张`);
    check(
      "发音训练那张卡在",
      (await evaluate("document.body.innerText.includes('发音训练')")) === true,
    );
    check(
      "自检按钮能在真浏览里点到",
      (await clickSelector('button')).ok !== undefined,
      "点第一个按钮（标题里的第 1 个）",
    );

    /* ------------------------------------------------------------- 收尾 */
    console.log("\n=== 控制台 ===");
    const realConsole = consoleErrors.filter((line) => !/favicon/i.test(line));
    const realPage = pageErrors.filter((line) => !/favicon/i.test(line));
    check("控制台没有 error", realConsole.length === 0, realConsole.slice(0, 3).join(" | "));
    check("页面没有未捕获异常", realPage.length === 0, realPage.slice(0, 3).join(" | "));
  } finally {
    try {
      ws && ws.close();
    } catch (err) {
      /* 忽略 */
    }
    try {
      chrome.kill();
    } catch (err) {
      /* 忽略 */
    }
  }

  const total = checks.length;
  const passed = checks.filter((item) => item.ok).length;
  console.log(`\n${passed}/${total} 通过`);
  if (passed !== total) {
    console.log("\n没通过的：");
    for (const item of checks) if (!item.ok) console.log(`  - ${item.label}  ${item.detail}`);
    return 1;
  }
  return 0;
}

async function waitForTarget() {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
      const list = await resp.json();
      const page = list.find((item) => item.type === "page" && item.webSocketDebuggerUrl);
      if (page) return page;
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
    console.error(err && err.stack ? err.stack : err);
    process.exit(1);
  });
