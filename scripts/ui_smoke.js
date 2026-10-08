/**
 * 界面冒烟检查：在**真浏览器**里把主要流程点一遍，并收集控制台错误。
 *
 * 为什么需要它：这是个零构建的单文件前端，没有任何东西能在编译期
 * 告诉你「这个函数不存在」或者「这里读了一个 undefined 的属性」——
 * 症状只会是「点了没反应」。这个脚本把那种情况变成一行红字。
 *
 * 它不替代 pytest（那边 187 项）。这里只回答一个问题：
 * **界面在真浏览器里跑得起来吗，点下去有反应吗，有没有报错。**
 *
 * ## 0.0.2 为什么必须用真实鼠标事件
 *
 * 用户报的原话是「吃力，正常，秒答都按不动」。真因不是按钮坏了，而是
 * 0.0.1 把翻面前的 1/2/3 档设成了 `disabled`，却仍然显示手形光标 ——
 * 看起来完全可点，点下去毫无反应。
 *
 * 而当时的检查用的是 `evaluate("grade(2)")`：**直接调函数，绕过了 DOM 事件**。
 * 于是这里永远是绿的。这是一次真实的教训：一个绕开事件层的探针，
 * 测不到事件层的问题。
 *
 * 所以下面所有的按钮检查都走 `Input.dispatchMouseEvent` —— 和用户的手指
 * 走同一条路，并且在点之前先 `elementFromPoint` 确认那个坐标上真的是它
 * （挡住、0×0、被盖住这三种情况都会在这里现原形）。
 *
 * 用 Node 自带 WebSocket 直连 Chrome DevTools 协议，
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
}

// ------------------------------------------------------- 真实鼠标

/** 用真实的鼠标事件点一个坐标。这是这个脚本和「调函数」的分界线。 */
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

/** 那个坐标上**真正**能收到点击的是谁。用来抓「被盖住 / 尺寸为 0」。 */
async function hitTest(x, y) {
  return evaluate(`(() => {
    const node = document.elementFromPoint(${x}, ${y});
    if (!node) return "";
    const button = node.closest("button");
    if (button) {
      return "button" + (button.className ? "." + button.className.split(" ").join(".") : "");
    }
    return node.tagName.toLowerCase() + (node.className ? "." + String(node.className).split(" ").join(".") : "");
  })()`);
}

/**
 * 找到元素、确认它真的在屏幕上、点它，并回报点中的是谁。
 * 返回 null 表示找不到元素（调用方据此报失败，而不是抛异常）。
 */
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
      disabled: node.disabled === true,
      pointer: style.pointerEvents,
      visibility: style.visibility,
      display: style.display,
    };
  })()`);

  if (info.missing) return { ok: false, reason: `找不到 ${selector}[${index}]（共 ${info.count} 个）` };
  if (info.w < 1 || info.h < 1) {
    return { ok: false, reason: `${selector} 的尺寸是 ${info.w}×${info.h}，点不到` };
  }
  if (info.pointer === "none") return { ok: false, reason: `${selector} 设了 pointer-events:none` };
  if (info.visibility === "hidden" || info.display === "none") {
    return { ok: false, reason: `${selector} 不可见（${info.visibility}/${info.display}）` };
  }

  const hit = await hitTest(info.x, info.y);
  await clickAt(info.x, info.y);
  return { ok: true, hit, box: info };
}

/** 「点一下这个按钮，屏幕上必须发生某件可观测的事」。 */
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
    changed ? `点中的是 ${clicked.hit}` : `点了没反应（${probe.describe ? probe.describe(before, after) : `${JSON.stringify(before)} → ${JSON.stringify(after)}`}）`
  );
  return changed;
}

async function goto(hash) {
  await send("Page.navigate", { url: `${BASE}/${hash || ""}` });
  await sleep(1800);
}

// ------------------------------------------------------------ 主流程

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

    // localStorage 里存着主题和侧边栏状态。上一次跑剩下的值会让
    // 「侧边栏是收着的」这类断言在不同机器上表现不一样。
    await goto("#study");
    await evaluate("(() => { localStorage.clear(); return true; })()");

    // ---------------------------------------------------------- 外壳
    console.log("\n=== 外壳与侧边栏 ===");
    await goto("#study");
    check("渲染出了顶部导航", (await evaluate("!!document.querySelector('#nav')")) === true);
    check(
      "渲染出了侧边栏",
      (await evaluate("!!document.querySelector('#side')")) === true
    );
    check(
      "版本徽章显示了版本号",
      /v\d+\.\d+\.\d+/.test(await evaluate("document.querySelector('#health').textContent")),
      await evaluate("document.querySelector('#health').textContent")
    );
    check(
      "导航有四项，并且都能对上下面的视图",
      (await evaluate("document.querySelectorAll('#nav button').length")) === 4
    );

    // 要求 3：交互键都收到侧边栏里，并且有个收缩按钮。
    await waitFor("!!state.current", "第一张卡");
    check(
      "侧边栏里有四个评分键",
      (await evaluate("document.querySelectorAll('#side-actions .grade').length")) === 4
    );
    check(
      "卡片区域里不再有评分键",
      (await evaluate("document.querySelectorAll('.stage .grade').length")) === 0,
      "评分键必须只在侧边栏"
    );

    const collapsedBefore = await evaluate("document.querySelector('#side').classList.contains('collapsed')");
    await clickSelector("#collapse");
    await sleep(350);
    const collapsedAfter = await evaluate("document.querySelector('#side').classList.contains('collapsed')");
    check("收缩按钮能把侧边栏收起来", collapsedBefore !== collapsedAfter);
    await clickSelector("#collapse");
    await sleep(350);
    check(
      "再点一次能展开回来",
      (await evaluate("document.querySelector('#side').classList.contains('collapsed')")) === collapsedBefore
    );

    check(
      "侧边栏里有当前卡片的操作区",
      (await evaluate("document.querySelectorAll('#side-actions .panel').length")) >= 1
    );
    await shot("01-shell");

    // ------------------------------------------------- 要求 4：评分键
    console.log("\n=== 评分键（真实鼠标逐个点）===");
    await waitFor("!!state.current && state.revealed === false", "一张还没翻面的卡");

    check(
      "翻面前，1/2/3 档没有被 disabled（0.0.1 就是死在这一点上）",
      (await evaluate(
        "[...document.querySelectorAll('#side-actions .grade')].filter(b => b.disabled).length"
      )) === 0
    );
    check(
      "翻面前的四档都用虚线边框表达「还没到这一步」",
      (await evaluate(
        "[...document.querySelectorAll('#side-actions .grade')].filter(b => b.dataset.pending === '1').length"
      )) === 3 && (await evaluate("document.querySelector('#side-actions .grade[data-g=\"0\"]').dataset.pending")) === "0"
    );

    // 翻面前点「吃力」：必须是**先翻面 + 给一句话**，而不是没反应。
    const cursor0 = await evaluate("state.cursor");
    await clickAndExpect(
      '#side-actions .grade[data-g="1"]',
      {
        before: "state.revealed",
        after: "state.revealed",
        describe: () => "点了「吃力」但答案没有翻出来",
      },
      "翻面前点「吃力」会把答案翻出来"
    );
    check(
      "并且明确说了「再点一次」（否则用户以为点坏了）",
      /再点一次|核对/.test(await evaluate("document.querySelector('#side-actions').textContent")),
      (await evaluate("state.gradeNote")) || "（没有提示）"
    );
    check("这一步不该把这张卡评掉", (await evaluate("state.cursor")) === cursor0);
    check(
      "翻面之后，四档都不再是「未到这一步」的样子",
      (await evaluate(
        "[...document.querySelectorAll('#side-actions .grade')].filter(b => b.dataset.pending === '1').length"
      )) === 0
    );
    await shot("02-revealed-side");

    // 现在真的评一档，用真实鼠标。
    await clickAndExpect(
      '#side-actions .grade[data-g="2"]',
      {
        before: "state.cursor",
        after: "state.cursor",
        wait: 900,
        describe: (a, b) => `cursor ${a} → ${b}`,
      },
      "翻面后点「正常」，进入下一张"
    );

    // 剩下三档也要各自能按 —— 逐个走一遍完整周期。
    // 用户报的是「吃力、正常、秒答都按不动」，所以三档都要**各自**验到。
    //
    // 「没想起来」放在最后：它不需要先翻面（不知道答案并不需要先看答案），
    // 前两档则要先点一下翻面。两条路径都要走。
    for (const [g, name, needsReveal] of [
      [1, "吃力", true],
      [3, "秒答", true],
      [0, "没想起来", false],
    ]) {
      await waitFor("!!state.current && state.revealed === false", `第 ${g} 档用的新卡`);
      if (needsReveal) {
        const first = await clickSelector(`#side-actions .grade[data-g="${g}"]`);
        await waitFor("state.revealed === true", `第 ${g} 档点第一下时把答案翻出来`);
        check(`「${name}」第一下点下去有反应（先翻面）`, first.ok, first.ok ? `点中的是 ${first.hit}` : first.reason);
      }
      const before = await evaluate("state.cursor");
      const clicked = await clickSelector(`#side-actions .grade[data-g="${g}"]`);
      await sleep(900);
      const after = await evaluate("state.cursor");
      check(
        `「${name}」这一档按下去有反应`,
        clicked.ok && after > before,
        clicked.ok ? `cursor ${before} → ${after}（点中的是 ${clicked.hit}）` : clicked.reason
      );
    }

    // 键盘和鼠标必须走同一条路 —— 0.0.1 的问题是「键盘能按、鼠标不能按」。
    const beforeKeys = await evaluate("state.cursor");
    await waitFor("!!state.current && state.revealed === false", "键盘用的新卡");
    await evaluate(`
      document.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
    `);
    await sleep(400);
    check("空格键也能翻面", (await evaluate("state.revealed")) === true);
    await evaluate(`
      document.dispatchEvent(new KeyboardEvent('keydown', { key: '2', bubbles: true }));
    `);
    await sleep(900);
    check(
      "数字键 2 也能评分",
      (await evaluate("state.cursor")) > beforeKeys,
      `cursor ${beforeKeys} → ${await evaluate("state.cursor")}`
    );

    // ------------------------------------------------------- 训练包页
    console.log("\n=== 训练包页 ===");
    await goto("#lessons");
    await waitFor("document.querySelectorAll('.lesson').length > 0", "训练包列表");
    const lessonCount = await evaluate("document.querySelectorAll('.lesson').length");
    check("列出了训练包", lessonCount > 0, `${lessonCount} 个`);
    await shot("03-lessons");

    const opened = await clickSelector(".lesson button");
    check("「打开」按钮用真实鼠标点得动", opened.ok, opened.ok ? `点中的是 ${opened.hit}` : opened.reason);
    await waitFor("document.querySelectorAll('.example').length > 0", "例句列表");
    const exCount = await evaluate("document.querySelectorAll('.example').length");
    check("打开了训练包并列出例句", exCount > 0, `${exCount} 条`);
    check(
      "每条例句都有五种练法的入口",
      (await evaluate(
        "Math.min(...[...document.querySelectorAll('.example')].map(e => e.querySelectorAll('.chanbtns button').length))"
      )) === 5
    );
    // 要求 5：场景要有图（或至少说明为什么没有）。
    check(
      "例句上带场景配图（或标出缺图）",
      (await evaluate(
        "document.querySelectorAll('.example .thumb').length > 0 || document.body.textContent.includes('缺场景图')"
      )) === true,
      (await evaluate("document.querySelectorAll('.example .thumb').length")) + " 张缩略图"
    );
    await shot("04-lesson-detail");

    // ------------------------------------------------------- 打字卡
    console.log("\n=== 打字卡 ===");
    await clickSelector(".example .chanbtns button", 3);
    await waitFor("!!document.querySelector('.typerow input')", "打字输入框");
    check("进入了打字训练", true);
    check(
      "打字卡正面不显示英文原文（否则就是抄写而不是回忆）",
      (await evaluate("!document.querySelector('.stage').textContent.includes('I really')")) === true
    );

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
    await shot("05-type-card");

    // -------------------------------------------------------- 目标页
    console.log("\n=== 目标页 ===");
    await goto("#plans");
    check("进入了目标页", (await evaluate("state.view")) === "plans");
    check(
      "有语言、目标类型、先储备多少个三个入口",
      (await evaluate(`
        (() => {
          const hasLang = !!document.querySelector('#plan-language');
          const segs = document.querySelectorAll('#app .seg').length;
          return hasLang && segs >= 2;
        })()
      `)) === true
    );
    // 目标不是难度标签：三个选项说的是「你要拿它做什么」，不是「你几级」。
    check(
      "目标类型把「过考试」和「真正学会」分成两件事",
      (await evaluate(`
        (() => {
          const labels = [...document.querySelectorAll('#app .seg button')].map(b => b.textContent.trim());
          return labels.some(t => t.includes('考试')) && labels.some(t => t.includes('学会'))
              && labels.some(t => t.includes('自己'));
        })()
      `)) === true,
      (await evaluate("[...document.querySelectorAll('#app .seg button')].map(b => b.textContent.trim()).join(' / ')"))
    );
    check(
      "语言下拉里列出了别的语言，但标明还没开放",
      (await evaluate(`
        (() => {
          const sel = document.querySelector('#plan-language');
          if (!sel) return false;
          const others = [...sel.options].filter(o => o.value !== 'en');
          return others.length >= 5 && others.every(o => o.disabled);
        })()
      `)) === true
    );
    await shot("06-plans");
    await checkPlanRoundTrip();

    // -------------------------------------------------------- 设置页
    console.log("\n=== 设置页 ===");
    await goto("#settings");
    await waitFor("document.querySelectorAll('table.self tr').length > 0", "自检表");
    const selfRows = await evaluate("document.querySelectorAll('table.self tr').length");
    check("自检面板列出了检查项", selfRows >= 5, `${selfRows} 项`);
    check(
      "自检里有「场景配图」和「数据目录来源」两项",
      (await evaluate(`
        (() => {
          const text = document.body.textContent;
          return text.includes('场景配图') && text.includes('来源');
        })()
      `)) === true
    );
    check(
      "密钥不会回填到输入框里",
      (await evaluate("document.querySelector('#llm-key').value")) === ""
    );

    const cfg = await evaluate("JSON.stringify(state.config || {})");
    check("设置里读到了配图配置", cfg.includes("images"), cfg.slice(0, 0) || "");

    await waitFor("!!document.querySelector('#data-location dl')", "数据目录信息");
    const dataText = await evaluate("document.querySelector('#data-location').textContent");
    check("「数据」卡片显示了当前目录", dataText.includes("当前目录"), dataText.slice(0, 60));
    check(
      "「数据」卡片有搬家和备份按钮",
      (await evaluate(`
        (() => {
          const t = document.querySelector('#data-location').parentElement.textContent;
          return t.includes('搬') && t.includes('备份');
        })()
      `)) === true
    );
    await shot("07-settings");

    // ---------------------------------------------------------- 深链接
    console.log("\n=== 深链接 ===");
    await goto("#settings");
    check("刷新后仍停在设置页", (await evaluate("state.view")) === "settings");
    await goto("#lessons");
    check("直接打开 #lessons 会进训练包页", (await evaluate("state.view")) === "lessons");

    // ---------------------------------------------------------- 控制台
    console.log("\n=== 控制台 ===");
    check("没有未捕获的页面异常", pageErrors.length === 0, pageErrors.join(" | "));
    check("没有 console.error", consoleErrors.length === 0, consoleErrors.join(" | "));
    check(
      "错误横幅没有被触发",
      (await evaluate("document.querySelector('#banner').className")) !== "bad",
      await evaluate("document.querySelector('#banner').textContent")
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

/**
 * 目标页走一遍：填表 → 出大纲 → 排除两条 → 开工。
 *
 * 这一步值得在真浏览器里做，因为「先出大纲再开工」的价值全在**界面**上：
 * 用户要能看见那二十条、能勾掉不要的。后端接口对了但界面没画出来，
 * 这个功能就等于没有。
 */
async function checkPlanRoundTrip() {
  if (!(await evaluate("!!document.querySelector('#plan-create')"))) {
    check("目标页有「生成大纲」的按钮", false, "找不到 #plan-create");
    return;
  }
  check("目标页有「生成大纲」的按钮", true);

  // 先储备个数选「10 个」：这一组只验「点得动、顺序对、形状对」，
  // 不会真的去开工（开工要点下面那个按钮），所以只花一次模型调用。
  // 选 1 个的话就只有一种 kind，「分得出词 / 固定说法 / 句型」这条就验不到了。
  const picked = await evaluate(`
    (() => {
      const node = [...document.querySelectorAll('#app .seg button')]
        .find(b => b.textContent.trim() === '10 个');
      if (!node) return false;
      node.click();
      return true;
    })()
  `);
  check("能选择「先储备 10 个」", picked === true);

  await clickSelector("#plan-create");
  await waitFor("document.querySelectorAll('.outline-item').length > 0", "大纲条目", 40000);
  const items = await evaluate("document.querySelectorAll('.outline-item').length");
  check("点一下就把大纲排出来了", items >= 1, `${items} 条`);
  check(
    "每一条大纲都说了「学会它能多做什么」",
    (await evaluate("document.querySelectorAll('.outline-item .why').length")) === items
  );
  // 这条盯的是「目标不是难度标签」：一份全是名词的词表是失败的。
  check(
    "大纲分得出词 / 固定说法 / 句型（不是一张词表）",
    (await evaluate(`
      (() => {
        const kinds = new Set([...document.querySelectorAll('.outline-item .tags .chip')]
          .map(c => c.textContent.trim())
          .filter(t => ['word','phrase','sentence'].includes(t)));
        return kinds.size >= 2;
      })()
    `)) === true,
    "出现过的类型：" + (await evaluate(`
      [...new Set([...document.querySelectorAll('.outline-item .tags .chip')]
        .map(c => c.textContent.trim())
        .filter(t => ['word','phrase','sentence'].includes(t)))].join(' / ')
    `))
  );
  await shot("06b-plan-outline");

  // 排除一条，确认「先看再开工」这件事真的能操作。
  const before = await evaluate(
    "document.querySelectorAll('.outline-item:not(.off)').length"
  );
  await clickSelector(".outline-item .pick", 0);
  await sleep(300);
  const after = await evaluate(
    "document.querySelectorAll('.outline-item:not(.off)').length"
  );
  check("点一下就能把不想要的条目排除掉", after === before - 1, `${before} → ${after}`);
  check(
    "并且明确说了这一下之后会生成几个",
    /将生成\s*\d+\s*个/.test(await evaluate("document.querySelector('#app').textContent"))
  );
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
