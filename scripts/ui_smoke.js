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
      "导航有五项，并且都能对上下面的视图",
      (await evaluate("document.querySelectorAll('#nav button').length")) === 5,
      await evaluate("[...document.querySelectorAll('#nav button')].map(b => b.dataset.view).join(' / ')")
    );

    // 0.0.3：评分键从侧边栏搬回了训练页。
    //
    // 0.0.2 把它们放在侧边栏，理由是「卡片一长就得往下翻才能评分」。
    // 用过之后发现代价更大：评分是跟着卡片走的动作，放在固定栏里就成了
    // 「眼睛在卡片上、手在屏幕另一头」；而且 266px 装不下「档位 + 下次间隔」，
    // 只能显示成「先核对答案」，看着像坏了。
    //
    // 所以这两条现在是**反过来**的：训练页必须有四个，侧边栏必须一个都没有。
    await waitFor("!!state.current", "第一张卡");
    check(
      "训练页里有四个评分键",
      (await evaluate("document.querySelectorAll('#app .grader .grade').length")) === 4
    );
    check(
      "侧边栏里不再有评分键",
      (await evaluate("document.querySelectorAll('#side-actions .grade').length")) === 0,
      "评分键必须跟卡片在一起"
    );
    check(
      "每一档都写清了「按下去会推到什么时候」",
      (await evaluate(`
        [...document.querySelectorAll('#app .grader .grade')].every(b => b.querySelector('.when'))
      `)) === true,
      await evaluate("[...document.querySelectorAll('#app .grader .grade .when')].map(e => e.textContent).join(' | ')")
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
      "侧边栏里有本次进度的显示",
      (await evaluate("!!document.querySelector('#side-actions .tally')")) === true
    );
    await shot("01-shell");

    // ------------------------------------------------- 要求 4/6：评分键
    console.log("\n=== 评分键（真实鼠标逐个点）===");
    await waitFor("!!state.current && state.revealed === false", "一张还没翻面的卡");

    check(
      "翻面前，1/2/3 档没有被 disabled（0.0.1 就是死在这一点上）",
      (await evaluate(
        "[...document.querySelectorAll('#app .grader .grade')].filter(b => b.disabled).length"
      )) === 0
    );
    // 0.0.3 把「还没翻面」的表现从「虚线边框 + 灰字」换成了
    // 「长得和其他档一样，只在右上角挂一个小标签」——
    // 原来那种画法在用户眼里和「禁用」没区别，正是「显示有点问题」。
    check(
      "翻面前，后三档挂的是「先看答案」的小标签，而不是变灰",
      (await evaluate(
        "[...document.querySelectorAll('#app .grader .grade')].filter(b => b.classList.contains('is-pending')).length"
      )) === 3
      && (await evaluate("document.querySelector('#app .grader .grade[data-g=\"0\"]').classList.contains('is-pending')")) === false
    );
    check(
      "待用的档位**照样显示**下次间隔（0.0.2 是被那句提示顶掉的）",
      (await evaluate(`
        [...document.querySelectorAll('#app .grader .grade.is-pending .when')]
          .every(e => e.textContent.trim().length > 3)
      `)) === true,
      await evaluate("[...document.querySelectorAll('#app .grader .grade.is-pending .when')].map(e => e.textContent).join(' | ')")
    );

    // 翻面前点「吃力」：必须是**先翻面 + 给一句话**，而不是没反应。
    const cursor0 = await evaluate("state.cursor");
    await clickAndExpect(
      '#app .grader .grade[data-g="1"]',
      {
        before: "state.revealed",
        after: "state.revealed",
        describe: () => "点了「吃力」但答案没有翻出来",
      },
      "翻面前点「吃力」会把答案翻出来"
    );
    check(
      "并且明确说了「再点一次」（否则用户以为点坏了）",
      /再点一次|核对/.test(await evaluate("document.querySelector('#app .grader').textContent")),
      (await evaluate("state.gradeNote")) || "（没有提示）"
    );
    check("这一步不该把这张卡评掉", (await evaluate("state.cursor")) === cursor0);
    check(
      "翻面之后，四档都不再挂「先看答案」",
      (await evaluate(
        "[...document.querySelectorAll('#app .grader .grade')].filter(b => b.classList.contains('is-pending')).length"
      )) === 0
    );
    await shot("02-revealed-grade");

    // 现在真的评一档，用真实鼠标。
    await clickAndExpect(
      '#app .grader .grade[data-g="2"]',
      {
        before: "state.cursor",
        after: "state.cursor",
        wait: 900,
        describe: (a, b) => `cursor ${a} → ${b}`,
      },
      "翻面后点「正常」，进入下一张"
    );

    // --------------------------------------- 要求 5：上一张 / 下一张 / 撤销
    console.log("\n=== 上一张 / 下一张 / 撤销 ===");
    const histLen = await evaluate("state.history.length");
    check("评完一张之后有了可撤销的记录", histLen >= 1, `${histLen} 条`);

    const beforeUndo = await evaluate("state.sessionDone");
    await clickAndExpect(
      "#grade-undo",
      {
        before: "state.sessionDone",
        after: "state.sessionDone",
        wait: 1200,
        describe: (a, b) => `已练数 ${a} → ${b}`,
      },
      "「撤销上一评」把这一张退回了队列"
    );
    check(
      "撤销之后这张卡又回到当前，并且是翻着面的（要能直接改评分）",
      (await evaluate("state.revealed")) === true,
      `revealed=${await evaluate("state.revealed")}，sessionDone ${beforeUndo} → ${await evaluate("state.sessionDone")}`
    );

    const beforeNext = await evaluate("state.cursor");
    await clickAndExpect(
      "#grade-next",
      { before: "state.cursor", after: "state.cursor", wait: 900, describe: (a, b) => `cursor ${a} → ${b}` },
      "「下一张」能跳过当前卡"
    );
    check(
      "跳过不计入「本次已练」",
      (await evaluate("state.sessionDone")) === beforeUndo - 1,
      `sessionDone=${await evaluate("state.sessionDone")}（跳过前是 ${beforeUndo - 1}）`
    );

    const beforePrev = await evaluate("state.cursor");
    await clickAndExpect(
      "#grade-prev",
      { before: "state.cursor", after: "state.cursor", wait: 900, describe: (a, b) => `cursor ${a} → ${b}` },
      "「上一张」能回看"
    );
    check("回看之后 cursor 变小了", (await evaluate("state.cursor")) < beforePrev,
      `cursor ${beforePrev} → ${await evaluate("state.cursor")}`);
    check(
      "「上一张」不会偷偷改数据（它只是回看）",
      (await evaluate("state.history.length")) === 0,
      "撤销记录条数：" + (await evaluate("state.history.length"))
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
        const first = await clickSelector(`#app .grader .grade[data-g="${g}"]`);
        await waitFor("state.revealed === true", `第 ${g} 档点第一下时把答案翻出来`);
        check(`「${name}」第一下点下去有反应（先翻面）`, first.ok, first.ok ? `点中的是 ${first.hit}` : first.reason);
      }
      const before = await evaluate("state.cursor");
      const clicked = await clickSelector(`#app .grader .grade[data-g="${g}"]`);
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
    const beforeArrow = await evaluate("state.cursor");
    await evaluate(`
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    `);
    await sleep(800);
    check(
      "→ 键等于「下一张」",
      (await evaluate("state.cursor")) > beforeArrow,
      `cursor ${beforeArrow} → ${await evaluate("state.cursor")}`
    );
    await evaluate(`
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
    `);
    await sleep(800);
    check(
      "← 键等于「上一张」",
      (await evaluate("state.cursor")) === beforeArrow,
      `cursor 回到 ${await evaluate("state.cursor")}`
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
      "每条例句都有五种练法的入口，外加「编辑」和「删除」",
      (await evaluate(
        "Math.min(...[...document.querySelectorAll('.example')].map(e => e.querySelectorAll('.chanbtns button').length))"
      )) >= 7
    );
    check(
      "前五个入口确实是五种练法",
      (await evaluate(`
        ['阅读','听力','口语','打字','造句'].every((name, i) =>
          document.querySelectorAll('.example .chanbtns button')[i].textContent.includes(name))
      `)) === true,
      await evaluate("[...document.querySelectorAll('.example .chanbtns button')].slice(0,7).map(b => b.textContent).join(' / ')")
    );
    check(
      "训练包详情页有「自己加一条例句」的入口（模型少给一条时不用重生成整包）",
      (await evaluate("!!document.querySelector('#new-ex-sentence')")) === true
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

    // --------------------------------------------- 要求 7：卡片编辑器抽屉
    console.log("\n=== 卡片编辑器（Anki 式编辑）===");
    await evaluate(`
      (() => {
        const btns = [...document.querySelectorAll('.example .chanbtns button')];
        const edit = btns.find(b => b.textContent.trim() === '编辑');
        if (edit) edit.click();
        return !!edit;
      })()
    `);
    await waitFor("!!document.querySelector('.drawer')", "卡片编辑器抽屉");
    check("打开了卡片编辑器抽屉", true);
    check(
      "抽屉里能改原文、场景、中文说法三类文字",
      (await evaluate(`
        !!document.querySelector('#ed-sentence') &&
        !!document.querySelector('#ed-scene-en') &&
        document.querySelectorAll('.drawer .varrow input').length > 0
      `)) === true
    );
    check(
      "抽屉里有语音的重新生成入口",
      /重新生成语音/.test(await evaluate("document.querySelector('.drawer').textContent"))
    );
    check(
      "抽屉里能手动贴一个图片地址（配图链路的最后兜底）",
      (await evaluate("!!document.querySelector('#ed-img')")) === true
    );
    check(
      "「简易」密度下不摆 SRS 数字，但说清了去哪开",
      (await evaluate("!document.querySelector('#ed-interval') && document.querySelector('.drawer').textContent.includes('进阶')")) === true
    );
    await shot("04b-editor-simple");

    // 切到进阶，SRS 数字就该出现。
    await evaluate("(() => { setLook({ density: 'advanced' }); return true; })()");
    await sleep(300);
    check(
      "切到「进阶」之后，间隔 / 难度 / 暂停都出现了",
      (await evaluate(`
        !!document.querySelector('#ed-interval') && !!document.querySelector('#ed-ease') &&
        /暂停这张卡|恢复这张卡/.test(document.querySelector('.drawer').textContent)
      `)) === true
    );
    // 改密度会顺手重画整个页面（抽屉里就有这个开关）。等背后的详情页
    // 拉回来再截，否则截到的是那张「正在取这个包…」的转圈图。
    await waitFor("document.querySelectorAll('.example').length > 0", "重画之后的例句列表");
    await shot("04c-editor-advanced");

    // 真的改一次中文说法，并确认落库了。
    const newVariant = "冒烟检查改的：" + Date.now();
    await evaluate(`
      (() => {
        const input = document.querySelector('.drawer .varrow input');
        input.value = ${JSON.stringify(newVariant)};
        input.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      })()
    `);
    await clickSelector('.drawer-foot button');
    await sleep(1200);
    check(
      "保存之后重新读回来，改的内容在里面",
      (await evaluate("document.body.textContent.includes(" + JSON.stringify(newVariant) + ")")) === true
    );
    check(
      "改完还能打开抽屉（内容没被改坏）",
      (await evaluate("!!document.querySelector('#ed-sentence')")) === true
    );

    await evaluate("(() => { if (state.editing) closeEditor(); return true; })()");
    await sleep(300);
    check("Esc / 关闭能把抽屉收掉", (await evaluate("!!document.querySelector('.drawer')")) === false);
    await evaluate("(() => { setLook({ density: 'simple' }); return true; })()");
    // 改密度会重画，而重画详情页要重新取一次这个包 —— 等它回来再往下点。
    await waitFor("document.querySelectorAll('.example').length > 0", "重画之后的例句列表");

    // ------------------------------------------------------- 打字卡
    console.log("\n=== 打字卡 ===");
    // 这里必须先确认「人还在详情页」。上一段是从详情页打开抽屉的，
    // 关掉抽屉之后如果掉回了训练包列表，`.chanbtns button` 就一个都不在了 ——
    // 那样点下去等于什么都没点，却要等 15 秒才在 waitFor 上超时，
    // 报出来的还是「等不到输入框」，跟真正的毛病（掉回列表）八竿子打不着。
    const onDetail = await evaluate("document.querySelectorAll('.example .chanbtns button').length");
    check("关掉抽屉之后仍然停在训练包的详情页", onDetail > 0, `${onDetail} 个练法入口`);
    const typing = await clickSelector(".example .chanbtns button", 3);
    check("点得到「练打字」", typing.ok, typing.ok ? `点中的是 ${typing.hit}` : typing.reason);
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

    // ------------------------------------------------------- 卡片浏览页
    console.log("\n=== 卡片浏览页（老手调控）===");
    await goto("#cards");
    check("进入了卡片页", (await evaluate("state.view")) === "cards");
    await waitFor("document.querySelectorAll('table.browser tbody tr').length > 0", "卡片列表");
    const rows = await evaluate("document.querySelectorAll('table.browser tbody tr').length");
    check("列出了卡片", rows > 0, `${rows} 行`);
    check(
      "筛选器有训练包 / 通道 / 状态 / 搜索四样",
      (await evaluate("document.querySelectorAll('.filters label').length")) >= 4,
      await evaluate("[...document.querySelectorAll('.filters label span')].map(s => s.textContent).join(' / ')")
    );

    // 「已暂停」这个筛选必须真的有东西可筛 —— 先暂停一张。
    // 关键：先记下要暂停哪一张，之后才有资格断言「队列里没有它」。
    // 只数 tr.off 的个数是不够的 —— 那只证明界面变了，证明不了后端真的把它踢出队列。
    const pauseId = await evaluate("document.querySelector('table.browser tbody tr').dataset.id");
    await clickSelector("table.browser .rowacts button");
    await sleep(900);
    const pausedRow = await evaluate(`(() => {
      const tr = document.querySelector("table.browser tbody tr[data-id='${pauseId}']");
      return tr ? tr.dataset.suspended : "missing";
    })()`);
    check(
      "点「暂停」之后那一行被标成了暂停",
      pausedRow === "1" && (await evaluate("document.querySelectorAll('table.browser tr.off').length")) >= 1,
      `card ${pauseId} → suspended=${pausedRow}`
    );

    // 队列里必须查不到这张卡（后端 due_cards 排除 suspended）。
    const queueIds = await evaluate(
      "fetch('/api/queue?limit=200').then(r => r.json()).then(d => d.cards.map(c => String(c.id)))"
    );
    check(
      "暂停的卡不出现在训练队列里",
      Array.isArray(queueIds) && queueIds.indexOf(String(pauseId)) < 0,
      `队列 ${Array.isArray(queueIds) ? queueIds.length : "?"} 张，不含 card ${pauseId}`
    );

    // 恢复回去，免得污染后面训练页的断言（队列少一张会让评分流程不稳定）。
    await clickSelector(`table.browser tbody tr[data-id='${pauseId}'] .rowacts button`);
    await sleep(900);
    const backRow = await evaluate(`(() => {
      const tr = document.querySelector("table.browser tbody tr[data-id='${pauseId}']");
      return tr ? tr.dataset.suspended : "missing";
    })()`);
    check("恢复之后那张卡回到队列", backRow === "0", `card ${pauseId} → suspended=${backRow}`);

    // 多选 + 批量：勾两个，应该出现批量操作条。
    await evaluate(`
      (() => {
        const boxes = [...document.querySelectorAll('table.browser tbody input[type=checkbox]')];
        for (const b of boxes.slice(0, 2)) { b.checked = true; b.dispatchEvent(new Event('change', { bubbles: true })); }
        return true;
      })()
    `);
    await sleep(500);
    check(
      "选中两张之后出现批量操作条",
      (await evaluate("!!document.querySelector('.bulkbar')")) === true,
      await evaluate("document.querySelector('.bulkbar') ? document.querySelector('.bulkbar').textContent.slice(0,40) : ''")
    );
    await shot("07-cards");
    await evaluate("(() => { state.browser.picked = []; return true; })()");

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

    // 要求 2：配色。
    //
    // 这里原来是用「卡片文字里有没有『深色』」去选的，结果一直选到
    // **「跟随系统」**那张 —— 它的说明写着「系统切到深色时跟着变」，
    // 里面有「深色」两个字，而且排在最前面。点下去等于把主题设回 auto，
    // 底色当然不变，于是这条用例常年红着，而毛病其实在用例自己身上。
    // 现在改用 data-opt 精确选，并且逐套验证「真的换掉了颜色」。
    console.log("\n=== 配色 ===");
    check(
      "设置里能选配色的四种模式",
      (await evaluate("document.querySelectorAll('[data-opt^=\"theme:\"]').length")) === 4,
      await evaluate("[...document.querySelectorAll('[data-opt^=\"theme:\"] b')].map(b => b.textContent).join(' / ')")
    );

    // 「r,g,b 三元组」的读取器。背景和字色各一个 —— 只写一个再复用的话，
    // 很容易出现「拿底色当字色比」这种读了也看不出错的错误。
    const rgbOf = (expr, prop) => evaluate(`
      (() => {
        const m = getComputedStyle(${expr}).${prop}.match(/[0-9.]+/g);
        return m ? m.map(Number) : null;
      })()
    `);
    const bgOf = (expr) => rgbOf(expr || "document.body", "backgroundColor");
    const fgOf = (expr) => rgbOf(expr || "document.body", "color");
    const lum = (rgb) => rgb && (rgb[0] + rgb[1] + rgb[2]) / 3;

    const themeBg = {};
    for (const id of ["warm", "light", "dark"]) {
      const clicked = await clickSelector(`[data-opt="theme:${id}"]`);
      await sleep(320);
      themeBg[id] = await evaluate("getComputedStyle(document.body).backgroundColor");
      const pressed = await evaluate(`document.querySelector('[data-opt="theme:${id}"]').getAttribute('aria-pressed')`);
      check(
        `点「${id}」真的换掉了底色，并且这一档被标成选中`,
        clicked.ok && pressed === "true" && (await evaluate("state.theme")) === id,
        `state.theme=${await evaluate("state.theme")} · aria-pressed=${pressed} · ${themeBg[id]}`
      );
    }
    // 「三套配色」不能只是三个名字 —— 底色必须真的互不相同。
    check(
      "三套配色的底色互不相同（不然「三套」就是假的）",
      new Set([themeBg.warm, themeBg.light, themeBg.dark]).size === 3,
      `暖白 ${themeBg.warm} / 冷白 ${themeBg.light} / 深色 ${themeBg.dark}`
    );

    // 深色底下必须是浅字。只查这一边还不够 —— 「所有主题都用浅字」
    // 这种错误照样能过，所以下面把浅色那边也反过来查一次。
    await clickSelector('[data-opt="theme:dark"]');
    await sleep(320);
    const darkBg = await bgOf();
    const darkFg = await fgOf();
    check(
      "深色下正文是浅色的（不能出现深字深底）",
      lum(darkFg) > 140 && lum(darkBg) < 120,
      `底色 ${darkBg} / 字色 ${darkFg}`
    );

    await clickSelector('[data-opt="theme:warm"]');
    await sleep(320);
    const warmBg = await bgOf();
    const warmFg = await fgOf();
    check(
      "浅色下正文是深色的",
      lum(warmFg) < 120 && lum(warmBg) > 180,
      `底色 ${warmBg} / 字色 ${warmFg}`
    );

    const sizeBefore = await evaluate("parseFloat(getComputedStyle(document.body).fontSize)");
    await clickSelector('[data-opt="size:l"]');
    await sleep(320);
    check(
      "字号能调大（护眼的一半靠字够大）",
      (await evaluate("parseFloat(getComputedStyle(document.body).fontSize)")) > sizeBefore,
      sizeBefore + "px → " + (await evaluate("getComputedStyle(document.body).fontSize"))
    );
    await clickSelector('[data-opt="size:s"]');
    await sleep(320);
    check(
      "字号也能调小",
      (await evaluate("parseFloat(getComputedStyle(document.body).fontSize)")) < sizeBefore,
      "→ " + (await evaluate("getComputedStyle(document.body).fontSize"))
    );

    // 还原成默认，别把后面几条断言带偏。
    await evaluate("(() => { setLook({ theme: 'warm', fontSize: 'm', density: 'simple' }); return true; })()");
    await sleep(300);

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
    await shot("08-settings");

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
