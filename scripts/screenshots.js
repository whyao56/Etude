/**
 * 生成文档用的界面截图。
 *
 * 为什么把它做成脚本而不是「手动截一次」：
 * README 里的截图一旦和当前版本对不上，就会开始误导人 ——
 * 上个项目的 README 上还挂着早已撤下的语音面板。**旧图比没有图更糟**，
 * 因为它看起来是可信的。
 *
 * 所以每次发版前跑一遍这个脚本，图就跟代码同步了。
 *
 * 用法：
 *     node scripts/screenshots.js http://127.0.0.1:8977 [输出目录]
 *
 * 需要先有一个跑起来的服务，并且里面已经有内容：
 * 几个生成完的训练包（带场景配图）、一个**大纲待确认**的学习计划。
 * 内容不够时脚本会直接报「等不到 xxx」而不是截一张空壳回去 ——
 * 空壳图同样是「看起来可信的错图」。
 */

"use strict";

const path = require("path");
const { Browser, canReach, sleep } = require("./cdp");

const BASE = (process.argv[2] || "http://127.0.0.1:8977").replace(/\/$/, "");
const OUT = path.resolve(process.argv[3] || path.join(__dirname, "..", "docs", "images"));

const THEME = process.env.SHOT_THEME || "dark";
const WIDTH = Number(process.env.SHOT_WIDTH || 1320);
const HEIGHT = Number(process.env.SHOT_HEIGHT || 900);

const FIRST_LESSON = `
  [...document.querySelectorAll('.lesson button')]
    .find(b => b.textContent.trim() === '打开').click()
`;

const FIRST_PLAN = `
  [...document.querySelectorAll('.plan button')]
    .find(b => b.textContent.trim() === '打开').click()
`;

async function main() {
  if (!(await canReach(BASE))) {
    console.error(`连不上 ${BASE}，先把服务起起来。`);
    return 2;
  }

  const browser = await new Browser({ width: WIDTH, height: HEIGHT }).launch();
  const made = [];

  try {
    await browser.setViewport(WIDTH, HEIGHT);
    await browser.goto(`${BASE}/#study`);
    await browser.setTheme(THEME);

    // ---- 01 训练页（队列里当前这张卡）----
    // 队列第一张卡是哪个通道不由脚本决定，所以这里就叫「训练页」，
    // 不假装它是阅读卡 —— 文件名说谎比没有文件名更糟。
    await browser.goto(`${BASE}/#study`);
    if (await browser.evaluate("!!document.querySelector('.stage .chan')")) {
      made.push(await browser.screenshot(path.join(OUT, "01-study.png")));
    }

    // ---- 02 训练包列表 ----
    await openLessonList(browser);
    made.push(await browser.screenshot(path.join(OUT, "02-lessons.png")));

    // ---- 03 训练包详情 ----
    await openFirstLesson(browser);
    made.push(await browser.screenshot(path.join(OUT, "03-lesson-detail.png")));

    // ---- 04 / 05 听力卡（正面只有声音，翻面才给原文）----
    await practice(browser, "听力");
    await browser.waitFor("!!document.querySelector('.playbtn')", "听力卡的播放按钮");
    made.push(await browser.screenshot(path.join(OUT, "04-listen-front.png")));
    await revealAndSettle(browser);
    made.push(await browser.screenshot(path.join(OUT, "05-listen-revealed.png")));

    // ---- 06 打字卡（提交后，带逐词差异）----
    await practice(browser, "打字");
    await browser.waitFor("!!document.querySelector('.typerow input')", "打字输入框");
    await browser.evaluate(`
      (() => {
        const input = document.querySelector('.typerow input');
        input.value = 'i really appreciate the way this one work';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      })()
    `);
    await sleep(500);
    made.push(await browser.screenshot(path.join(OUT, "06-type-card.png")));

    // ---- 07 口语卡（翻面后）----
    await practice(browser, "口语");
    await revealAndSettle(browser);
    made.push(await browser.screenshot(path.join(OUT, "07-speak-revealed.png")));

    // ---- 08 造句卡（翻面后）----
    await practice(browser, "造句");
    await revealAndSettle(browser);
    made.push(await browser.screenshot(path.join(OUT, "08-build-revealed.png")));

    // ---- 09 口语卡正面：场景 = 一张图 ----
    // 场景配图**只出现在「场景」这一侧**，所以口语卡正面是唯一能
    // 一眼说明「场景拿到了图、而语言输入那一侧干净」的位置。
    await practice(browser, "口语");
    await browser.waitFor(
      "!!document.querySelector('.imgwrap img.sceneimg')", "场景配图");
    made.push(await browser.screenshot(path.join(OUT, "09-speak-scene.png")));

    // ---- 10 目标列表 ----
    // 先清 openPlan 再切：setView 只在「离开 plans 视图」时才清它，
    // 而这里是从设置页过来的，不显式清就可能直接落到某个计划的详情上。
    await browser.goto(`${BASE}/#plans`);
    await browser.evaluate("state.openPlan = null; setView('plans')");
    await browser.waitFor("document.querySelectorAll('.plan').length > 0", "目标列表");
    made.push(await browser.screenshot(path.join(OUT, "10-plans.png")));

    // ---- 11 目标大纲（待确认，还没开始生成）----
    await browser.evaluate(FIRST_PLAN);
    await browser.waitFor("document.querySelectorAll('.outline-item').length > 0", "大纲条目");
    await scrollTo(browser, ".outline-item");
    made.push(await browser.screenshot(path.join(OUT, "11-plan-outline.png")));

    // ---- 12 / 13 / 14 设置页的三张卡 ----
    await browser.goto(`${BASE}/#settings`);
    await browser.evaluate("setView('settings')");
    await browser.waitFor("document.querySelectorAll('table.self tr').length > 0", "自检表");
    made.push(await browser.screenshot(path.join(OUT, "12-settings-llm.png")));

    await scrollToCard(browser, "场景配图");
    made.push(await browser.screenshot(path.join(OUT, "13-settings-images.png")));

    await scrollToCard(browser, "数据");
    made.push(await browser.screenshot(path.join(OUT, "14-settings-data.png")));

    console.log(`生成了 ${made.length} 张截图（主题：${THEME}，${WIDTH}×${HEIGHT}）：`);
    for (const file of made) console.log("  " + path.relative(process.cwd(), file));

    if (browser.pageErrors.length || browser.consoleErrors.length) {
      console.error("\n页面里有报错，截图未必可信：");
      for (const e of browser.pageErrors) console.error("  异常: " + e);
      for (const e of browser.consoleErrors) console.error("  console.error: " + e);
      return 1;
    }
    return 0;
  } finally {
    await browser.close();
  }
}

// 回到训练包列表。
//
// 这里必须**显式重画**，不能只靠改 hash。原因：
// openLesson()（点「打开」进详情）会把 location.hash 也设成 #lessons。
// 于是「再 goto 一次 #lessons」变成了同 hash 导航 ——
// hashchange 不触发，页面上那个 if (next !== state.view) 的比对也不成立，
// 结果是**页面留在训练包详情页**，而后面所有「从列表进」的步骤
// 全都找不到 .lesson，报一句含糊的「等不到训练包列表」。
// setView 不管 hash 变没变都会 render()，所以它才是这里该用的东西。
async function openLessonList(browser) {
  await browser.goto(`${BASE}/#lessons`);
  await browser.evaluate("setView('lessons')");
  await browser.waitFor("document.querySelectorAll('.lesson').length > 0", "训练包列表");
}

async function openFirstLesson(browser) {
  await browser.evaluate(FIRST_LESSON);
  await browser.waitFor("document.querySelectorAll('.example').length > 0", "例句列表");
}

// 从训练包详情里的「练XX」按钮进 —— 比在队列里翻几十张卡快得多，
// 而且顺带验证了「自由练」这条路是通的。
async function practice(browser, label) {
  await openLessonList(browser);
  await openFirstLesson(browser);
  await browser.evaluate(`
    (() => {
      const first = document.querySelectorAll('.example')[0];
      const btn = [...first.querySelectorAll('.chanbtns button')]
        .find(b => b.textContent.includes(${JSON.stringify(label)}));
      if (!btn) throw new Error('找不到「练' + ${JSON.stringify(label)} + '」按钮');
      btn.click();
      return true;
    })()
  `);
  await sleep(900);
}

async function revealAndSettle(browser) {
  await browser.evaluate("reveal()");
  await sleep(400);
}

// 把某个元素滚到视口顶端再截 —— 默认截图只拍视口，
// 页面下半部分的内容不滚过去就永远拍不到。
async function scrollTo(browser, selector) {
  const ok = await browser.evaluate(`
    (() => {
      const node = document.querySelector(${JSON.stringify(selector)});
      if (!node) return false;
      node.scrollIntoView({ block: 'start' });
      return true;
    })()
  `);
  if (!ok) throw new Error(`页面上找不到 ${selector}，滚不过去`);
  await sleep(400);
}

async function scrollToCard(browser, title) {
  const ok = await browser.evaluate(`
    (() => {
      const card = [...document.querySelectorAll('.card')].find(
        (c) => ((c.querySelector('h2') || {}).textContent || '').trim() === ${JSON.stringify(title)}
      );
      if (!card) return false;
      card.scrollIntoView({ block: 'start' });
      return true;
    })()
  `);
  if (!ok) throw new Error(`设置页里找不到标题为「${title}」的卡片`);
  await sleep(400);
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error("生成截图时出错：" + err.message);
    process.exit(1);
  });
