/**
 * 生成文档截图（0.1.0 新界面）。
 *
 * 和 0.0.x 的版本最大的不同：不再假设「界面上有个叫 setView 的全局函数」。
 * 现在每张图都靠**真的操作**走到的 —— 点导航、按空格翻面、点评分、
 * 点「练」进卡片 —— 所以截图本身也是一次验收：哪一步点不动，这里就出不来图。
 *
 * 用法：
 *   node scripts/screenshots.js                    # 连 127.0.0.1:8977，自己造数据
 *   node scripts/screenshots.js --no-seed           # 用现成的数据
 *   node scripts/screenshots.js --theme dark        # 只换默认配色
 *   node scripts/screenshots.js --only 06,07        # 只出某几张（调试用）
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
  const idx = ARGS.indexOf(`--${name}`);
  return idx >= 0 && ARGS[idx + 1] ? ARGS[idx + 1] : fallback;
};

const EXPLICIT_BASE = argValue("base", process.env.ETUDE_BASE || "");
const SERVE = !has("no-serve") && !EXPLICIT_BASE;
const SERVE_PORT = Number(argValue("port", "8978"));
let BASE = (EXPLICIT_BASE || `http://127.0.0.1:${SERVE_PORT}`).replace(/\/$/, "");
const OUT = path.resolve(ROOT, argValue("out", "docs/images"));
const THEME = argValue("theme", process.env.SHOT_THEME || "warm");
const STUB_PORT = Number(argValue("stub-port", "18999"));
const WIDTH = Number(argValue("width", "1320"));
const HEIGHT = Number(argValue("height", "900"));
const ONLY = argValue("only", "|").split("|").filter(Boolean);
const PYTHON = process.env.ETUDE_PYTHON || path.join(ROOT, ".venv", "Scripts", "python.exe");
const CHROME =
  process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";

const shots = [];
let browser = null;

function want(code) {
  return !ONLY.length || ONLY.some((one) => code.startsWith(one));
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

async function postJson(pathname, payload) {
  return httpCall(pathname, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 造一个「有例句、有语音、没配图」的训练包，够截图用了。 */
async function seed() {
  console.log("\n=== 造数据（假模型，不出网络）===");
  if (!fs.existsSync(PYTHON)) {
    console.error(`找不到 python：${PYTHON}`);
    return null;
  }
  const stub = spawn(PYTHON, [path.join(ROOT, "scripts", "openai_stub.py"), "--port", String(STUB_PORT)], {
    cwd: ROOT,
    stdio: "ignore",
    windowsHide: true,
  });
  let lessonId = 0;
  try {
    let up = false;
    for (let i = 0; i < 80 && !up; i += 1) {
      try {
        const res = await fetch(`http://127.0.0.1:${STUB_PORT}/v1/models`);
        up = res.ok || res.status === 404;
      } catch (err) {
        /* 还没起来 */
      }
      if (!up) await sleep(250);
    }
    if (!up) {
      console.error("假模型没起来。");
      return null;
    }

    await postJson("/api/config", {
      llm: {
        preset: "custom",
        base_url: `http://127.0.0.1:${STUB_PORT}/v1`,
        model: "stub-model",
        api_key: "stub-key",
      },
      images: { source: "off" },
      tts: { engine: "edge" },
      pronounce: { enabled: true, mode: "acoustic", save_recordings: true },
    });

    const target = argValue("target", "appreciate");
    const made = await postJson("/api/lessons", { target });
    lessonId = (made.body && made.body.lesson_id) || 0;
    if (!lessonId) {
      console.error(`没建成训练包：${JSON.stringify(made.body)}`);
      return null;
    }
    let status = "generating";
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      const job = await httpCall(`/api/lessons/${lessonId}/job`);
      status = (job.body && job.body.status) || status;
      if (status === "ready" || status === "failed") break;
      await sleep(400);
    }
    console.log(`训练包 ${lessonId}：${status}`);

    // 目标页要有一份大纲才画得出详情，这里也造一份（失败就只截列表和表单）。
    const plan = await postJson("/api/plans", {
      language: "en",
      goal_kind: "fluency",
      goal_label: "能开口",
      goal_detail: "下个月去冰岛自驾，得能问路和订房",
      domains: ["旅行", "日常"],
      target_count: 6,
    });
    const planId = (plan.body && plan.body.plan && plan.body.plan.id) || 0;
    console.log(`计划：${planId || "（没建成，跳过计划详情）"}`);
    return { lessonId, planId };
  } finally {
    try {
      stub.kill();
    } catch (err) {
      /* 忽略 */
    }
  }
}

/* ------------------------------------------------------------ 浏览器动作 */

async function evalIn(expression) {
  return await browser.evaluate(expression);
}

async function goto(hash, settle = 900) {
  await browser.goto(`${BASE}/${hash}`, settle);
}

async function setPrefs(prefs) {
  const payload = JSON.stringify(prefs);
  await evalIn(`localStorage.setItem("etude-prefs", ${JSON.stringify(payload)}); true`);
  // 写完必须等一下再跳：无头 Chrome 里「写 localStorage → 立刻 navigate」
  // 会把这次写丢掉，结果是两张配色图和一张一模一样（实测踩过）。
  await sleep(260);
}

/** 兜底：跳过去之后如果配色还是不对，就点按钮点到对为止。
 *  判据读 localStorage 里的 prefs —— 那才是 savePrefs 写的真值；
 *  data-theme 在「跟随系统」时是被删掉的，拿它判断会误以为没切换。 */
async function ensureTheme(theme) {
  for (let i = 0; i < 6; i += 1) {
    const now = await evalIn(
      `(() => { try { return JSON.parse(localStorage.getItem("etude-prefs") || "{}").theme || "auto"; } catch (e) { return "auto"; } })()`,
    );
    if (now === theme) return true;
    await evalIn(`document.querySelector("#theme-btn").click(); true`);
    await sleep(220);
  }
  return false;
}

async function resetPrefs() {
  await setPrefs({ theme: THEME, size: "m", side: "open" });
}

async function waitFor(expression, label, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ok = await evalIn(`(() => { try { return Boolean(${expression}); } catch (e) { return false; } })()`);
    if (ok) return true;
    await sleep(120);
  }
  throw new Error(`等不到：${label}`);
}

/** 用真实鼠标事件点一个元素 —— 和 ui_smoke 里那套一样，先命中测试再点。 */
async function click(selector, index = 0) {
  const box = await evalIn(`(() => {
    const nodes = document.querySelectorAll(${JSON.stringify(selector)});
    const node = nodes[${index}];
    if (!node) return null;
    node.scrollIntoView({ block: "center" });
    const r = node.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height };
  })()`);
  if (!box) throw new Error(`找不到 ${selector}[${index}]`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await browser.send("Input.dispatchMouseEvent", {
      type,
      x: Math.round(box.x),
      y: Math.round(box.y),
      button: "left",
      clickCount: 1,
    });
  }
  await sleep(220);
}

async function key(keyName, code, keyCode) {
  await browser.send("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: keyName,
    code,
    windowsVirtualKeyCode: keyCode,
    nativeVirtualKeyCode: keyCode,
  });
  await browser.send("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: keyName,
    code,
    windowsVirtualKeyCode: keyCode,
    nativeVirtualKeyCode: keyCode,
  });
  await sleep(200);
}

async function shot(code, name, scrollTop = 0) {
  if (!want(code)) return;
  if (scrollTop) {
    await evalIn(`(() => { const main = document.querySelector(".content") || window; main.scrollTo ? main.scrollTo(0, ${scrollTop}) : (main.scrollTop = ${scrollTop}); if (!main.scrollTo) window.scrollTo(0, ${scrollTop}); return true; })()`);
    await sleep(260);
  } else {
    // 不显式滚动时也回到顶部：翻面之后页面会自己往下滚，
    // 拍出来的「整页」图就变成半张，看上去像界面裁掉了。
    await evalIn(
      `(() => { const main = document.querySelector(".content"); if (main) main.scrollTop = 0; window.scrollTo(0, 0); return true; })()`,
    );
    await sleep(180);
  }
  const file = path.join(OUT, `${code}-${name}.png`);
  await browser.screenshot(file);
  shots.push({ file, code, name });
  console.log(`  ${path.basename(file)}`);
}

/** 排到未翻面状态：队列里第一张就是 read 卡。 */
async function openStudyClean() {
  await resetPrefs();
  await goto("#/study", 1200);
  await waitFor(`document.querySelector(".stage")`, "训练台画出来");
}

/** 用卡片浏览器进某个通道的第一张卡（比在训练页里连点五次稳）。 */
async function practiceChannel(channelLabel) {
  await goto("#/cards", 1100);
  await waitFor(`document.querySelector(".browser table tbody tr")`, "卡片表格");
  await evalIn(`(() => {
    const select = document.querySelectorAll(".filters select")[1];
    const option = [...select.options].find((one) => one.textContent === ${JSON.stringify(channelLabel)});
    select.value = option.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  })()`);
  await sleep(900);
  await waitFor(`document.querySelector(".browser table tbody tr .rowacts .btn")`, "筛选后的第一行");
  await click(".browser table tbody tr .rowacts .btn", 0);
  await waitFor(`document.querySelector(".stage")`, "进了训练台");
  await sleep(500);
}

/* ------------------------------------------------------------ 一组图 */

async function gallery(data) {
  const { lessonId = 0, planId = 0 } = data || {};

  // 01 训练页：阅读通道，未翻面
  if (want("01")) {
    await openStudyClean();
    await shot("01", "study-read");
  }

  // 02 翻面：答案 + 评分条
  if (want("02")) {
    await openStudyClean();
    await key(" ", "Space", 32);
    await waitFor(`document.querySelector(".answer")`, "翻面");
    await sleep(400);
    await shot("02", "study-answer");
  }

  // 03 口语卡：跟读面板
  if (want("03")) {
    await practiceChannel("口语");
    await waitFor(`document.querySelector(".pronounce")`, "跟读面板");
    await shot("03", "study-speak");
  }

  // 04 跟读结果：假麦克风真录一段，走完整条打分链路
  if (want("04")) {
    try {
      await practiceChannel("口语");
      await waitFor(`document.querySelector(".btn.mic")`, "录音键");
      await click(".btn.mic", 0);
      await sleep(2100);
      await click(".btn.mic", 0);
      await waitFor(`document.querySelector(".result .score")`, "评分结果", 20000);
      await sleep(500);
      await shot("04", "study-pronounce");
    } catch (err) {
      console.log(`  （跟读那张没出：${err.message}）`);
    }
  }

  // 05 打字通道：打完之后的逐词对比
  if (want("05")) {
    try {
      await practiceChannel("打字");
      await waitFor(`document.querySelector(".typerow input")`, "打字输入框");
      const sentence = await evalIn(
        `(window.__etude.state.current && window.__etude.state.current.example && window.__etude.state.current.example.sentence) || ""`,
      );
      if (sentence) {
        const node = await evalIn(`(() => {
          const input = document.querySelector(".typerow input");
          return input ? true : false;
        })()`);
        if (node) {
          await click(".typerow input", 0);
          // 故意少打一个词，对比才有内容可看。
          const typed = sentence.split(" ").filter((word, i) => i !== 2).join(" ");
          for (const type of ["insertText"]) {
            await browser.send("Input." + type, { text: typed });
          }
          await sleep(200);
          // 用「比对」按钮而不是回车：真实鼠标点得到才是真能点。
          await click(".typerow .btn", 0);
          await waitFor(`document.querySelector(".diff-host .w")`, "逐词对比", 8000);
          await sleep(400);
          await shot("05", "study-type");
        }
      }
    } catch (err) {
      console.log(`  （打字那张没出：${err.message}）`);
    }
  }

  // 06 训练包列表
  if (want("06")) {
    await goto("#/lessons", 1200);
    await waitFor(`document.querySelector(".lesson-row")`, "训练包列表");
    await shot("06", "lessons");
  }

  // 07 批量建包面板展开
  if (want("07")) {
    await goto("#/lessons", 1200);
    await waitFor(`document.querySelector("details.batch summary")`, "批量面板");
    await evalIn(`(() => { const d = document.querySelector("details.batch"); d.open = true; return true; })()`);
    await sleep(400);
    await shot("07", "lessons-batch");
  }

  // 08 训练包详情
  if (want("08") && lessonId) {
    await goto(`#/lessons/${lessonId}`, 1400);
    await waitFor(`document.querySelector(".example-row")`, "例句列表");
    await shot("08", "lesson-detail");
  }

  // 09 目标页
  if (want("09")) {
    await goto("#/plans", 1200);
    await waitFor(`document.querySelector(".card .chips")`, "目标表单");
    await shot("09", "plans");
  }

  // 10 计划详情：大纲
  if (want("10") && planId) {
    await goto(`#/plans/${planId}`, 1400);
    await waitFor(`document.querySelector(".outline-row")`, "大纲");
    await shot("10", "plan-detail");
  }

  // 11 卡片浏览器
  if (want("11")) {
    await goto("#/cards", 1400);
    await waitFor(`document.querySelector(".browser table tbody tr")`, "卡片表");
    await shot("11", "cards");
  }

  // 12 设置
  if (want("12")) {
    await goto("#/settings", 1400);
    await waitFor(`document.querySelectorAll(".card").length >= 8`, "八张设置卡");
    await shot("12", "settings");
  }

  // 13 设置里的发音训练：转写服务那一段展开
  if (want("13")) {
    await goto("#/settings", 1400);
    await waitFor(`document.querySelectorAll(".card").length >= 8`, "八张设置卡");
    const opened = await evalIn(`(() => {
      const heads = [...document.querySelectorAll(".card-head h3")];
      const target = heads.find((h) => h.textContent.includes("发音")) || null;
      if (!target) return false;
      const card = target.closest(".card");
      const details = card.querySelector("details");
      if (details) details.open = true;
      card.scrollIntoView({ block: "start" });
      return true;
    })()`);
    if (opened) {
      await sleep(400);
      await shot("13", "settings-pronounce");
    }
  }

  // 14/15 深色与冷白 —— 拍进度页：一屏里有数字、图、条，配色差得最清楚，
  // 而且它不依赖训练队列走到第几张卡（拍训练台的话每次内容都不一样）。
  for (const [code, theme, name] of [
    ["14", "dark", "theme-dark"],
    ["15", "light", "theme-light"],
  ]) {
    if (!want(code)) continue;
    await setPrefs({ theme, size: "m", side: "open" });
    // 必须整页 reload：改 hash 只是换视图，不会重跑 boot()->applyLook()，
    // 结果就是 prefs 里写着 dark、屏幕上还是上一个配色（实测踩过两次）。
    await browser.reload(1400);
    await goto("#/progress", 1600);
    await waitFor(`document.querySelector(".stat-grid")`, "进度页");
    await ensureTheme(theme);
    // 换配色会弹一条 toast，等它自己消失再拍 —— 图里挂着提示很显眼。
    await sleep(3200);
    await shot(code, name);
  }

  // 16 自检结果
  if (want("16")) {
    await goto("#/settings", 1300);
    try {
      await waitFor(`document.querySelectorAll(".card").length >= 8`, "八张设置卡");
      const ok = await evalIn(`(() => {
        const heads = [...document.querySelectorAll(".card-head h3")];
        const target = heads.find((h) => h.textContent.includes("自检")) || null;
        if (!target) return false;
        const card = target.closest(".card");
        const button = card.querySelector(".btn");
        if (!button) return false;
        card.scrollIntoView({ block: "start" });
        button.click();
        return true;
      })()`);
      if (ok) {
        await sleep(2200);
        await shot("16", "selfcheck");
      }
    } catch (err) {
      console.log(`  （自检那张没出：${err.message}）`);
    }
  }

  // 17 进度页
  if (want("17")) {
    await goto("#/progress", 1600);
    try {
      await waitFor(`document.querySelector(".stat-grid")`, "进度页四块指标");
      await shot("17", "progress");
    } catch (err) {
      console.log(`  （进度页那张没出：${err.message}）`);
    }
  }

  // 18 关于抽屉（诊断信息 + 打开目录）
  if (want("18")) {
    await goto("#/study", 1300);
    const opened = await evalIn(`(() => {
      const button = document.querySelector("#about-btn");
      if (!button) return false;
      button.click();
      return true;
    })()`);
    if (opened) {
      await sleep(700);
      await shot("18", "about");
      await evalIn(`(() => { const close = document.querySelector(".drawer .drawer-foot .btn");
        if (close) close.click(); return true; })()`);
      await sleep(300);
    }
  }

  // 19 快捷键抽屉
  if (want("19")) {
    await goto("#/study", 1200);
    const opened = await evalIn(`(() => {
      const button = document.querySelector("#help-btn");
      if (!button) return false;
      button.click();
      return true;
    })()`);
    if (opened) {
      await sleep(600);
      await shot("19", "shortcuts");
      await evalIn(`(() => { const close = document.querySelector(".drawer .drawer-foot .btn");
        if (close) close.click(); return true; })()`);
      await sleep(300);
    }
  }

  await resetPrefs();
}

/* ------------------------------------------------------------ 假麦克风 */

/** 造一段「像说话」的音频给 --use-file-for-fake-audio-capture 用。
 *  直接拿 tests/wavfmt 里的函数，省得在这里再写一遍 WAV 头。 */
function fakeMicFile() {
  const out = path.join(os.tmpdir(), "etude-fake-mic.wav");
  const script = [
    "import sys",
    "sys.path.insert(0, r'" + path.join(ROOT, "backend") + "')",
    "from tests.wavfmt import speech_then_pause",
    "data = speech_then_pause(first=1.0, pause=0.22, second=1.1)",
    "open(r'" + out + "', 'wb').write(data)",
  ].join("\n");
  try {
    const res = spawn(PYTHON, ["-c", script], { stdio: "inherit", windowsHide: true });
    return new Promise((resolve) => {
      res.on("exit", (code) => resolve(code === 0 ? out : ""));
    });
  } catch (err) {
    return Promise.resolve("");
  }
}

/* ------------------------------------------------------------ 主流程 */

/**
 * 自己起一个服务，用**全新的**数据目录。
 *
 * 为什么非要新的：截图是给文档用的，里面出现五个一模一样的「appreciate」
 * 训练包（那是前几次冒烟留下的）就变成了「这个产品有 bug 吧」。
 * 一次性数据目录让每一批截图都从同一个干净状态开始 —— 也因此可复现。
 */
async function startOwnServer() {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "etude-shots-data-"));
  const proc = spawn(
    PYTHON,
    [
      path.join(ROOT, "backend", "run_etude.py"),
      "--no-window",
      "--port",
      String(SERVE_PORT),
    ],
    {
      cwd: path.join(ROOT, "backend"),
      env: { ...process.env, ETUDE_DATA_DIR: dataDir },
      stdio: "ignore",
      windowsHide: true,
    },
  );
  for (let i = 0; i < 120; i += 1) {
    if (await canReach(BASE)) {
      console.log(`自己起了服务：${BASE}（数据目录 ${dataDir}）`);
      return { proc, dataDir };
    }
    await sleep(250);
  }
  proc.kill();
  console.error(`自己起的服务没起来：${BASE}`);
  return null;
}

async function main() {
  if (!fs.existsSync(CHROME)) {
    console.error(`找不到 Chrome：${CHROME}\n用 CHROME_PATH 指定路径。`);
    return 2;
  }
  const own = SERVE ? await startOwnServer() : null;
  if (!own && SERVE) return 2;
  if (!(await canReach(BASE))) {
    console.error(`连不上 ${BASE}。先起服务：\n  cd backend\n  ..\\.venv\\Scripts\\python.exe run_etude.py --no-window --port 8977`);
    return 2;
  }
  fs.mkdirSync(OUT, { recursive: true });

  const data = has("no-seed") ? { lessonId: Number(argValue("lesson", "0")), planId: Number(argValue("plan", "0")) } : await seed();

  const mic = await fakeMicFile();
  if (!mic) console.log("  （没有假麦克风，跟读那张会跳过）");

  browser = new Browser({
    width: WIDTH,
    height: HEIGHT,
    extraArgs: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      ...(mic ? [`--use-file-for-fake-audio-capture=${mic}`] : []),
    ],
  });
  await browser.launch();
  await browser.send("Emulation.setDeviceMetricsOverride", {
    width: WIDTH,
    height: HEIGHT,
    deviceScaleFactor: 1,
    mobile: false,
  });

  // 先把外观偏好写进去，第一张图就用对的配色。
  await browser.goto(`${BASE}/#/study`, 1200);
  await resetPrefs();
  await browser.reload(1200);

  console.log("\n=== 出图 ===");
  try {
    await gallery(data);
  } finally {
    await browser.close();
    if (own && own.proc) {
      // 关掉自己起的服务：不留后台进程，也不删数据目录
      // （出错时那里面是唯一能看出发生了什么的东西）。
      own.proc.kill();
    }
  }

  console.log(`\n共 ${shots.length} 张，输出到 ${OUT}`);
  if (browser.pageErrors.length || browser.consoleErrors.length) {
    console.error("页面有报错：");
    for (const line of [...browser.pageErrors, ...browser.consoleErrors]) console.error(`  ${line}`);
    return 1;
  }
  return 0;
}

main()
  .then((code) => process.exit(code || 0))
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
