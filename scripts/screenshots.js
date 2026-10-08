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
 * 需要先有一个跑起来的服务，并且里面已经有内容（用假模型生成几个训练包即可）。
 */

"use strict";

const path = require("path");
const { Browser, canReach, sleep } = require("./cdp");

const BASE = (process.argv[2] || "http://127.0.0.1:8977").replace(/\/$/, "");
const OUT = path.resolve(process.argv[3] || path.join(__dirname, "..", "docs", "images"));

const THEME = process.env.SHOT_THEME || "dark";
const WIDTH = Number(process.env.SHOT_WIDTH || 1320);
const HEIGHT = Number(process.env.SHOT_HEIGHT || 900);

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

    // ---- 阅读卡（正面）----
    await browser.goto(`${BASE}/#study`);
    if (await browser.evaluate("!!document.querySelector('.stage .chan')")) {
      made.push(await browser.screenshot(path.join(OUT, "01-study-read.png")));
    }

    // ---- 训练包列表 ----
    await browser.goto(`${BASE}/#lessons`);
    await browser.waitFor("document.querySelectorAll('.lesson').length > 0", "训练包列表");
    made.push(await browser.screenshot(path.join(OUT, "02-lessons.png")));

    // ---- 训练包详情 ----
    await browser.evaluate(`
      [...document.querySelectorAll('.lesson button')]
        .find(b => b.textContent.trim() === '打开').click()
    `);
    await browser.waitFor("document.querySelectorAll('.example').length > 0", "例句列表");
    made.push(await browser.screenshot(path.join(OUT, "03-lesson-detail.png")));

    // ---- 听力卡（翻面前 / 后）----
    await openChannel(browser, "听力");
    await browser.waitFor("!!document.querySelector('.playbtn')", "听力卡的播放按钮");
    made.push(await browser.screenshot(path.join(OUT, "04-listen-front.png")));
    await browser.evaluate("reveal()");
    await sleep(400);
    made.push(await browser.screenshot(path.join(OUT, "05-listen-revealed.png")));

    // ---- 打字卡（提交后，带差异比对）----
    await browser.goto(`${BASE}/#lessons`);
    await browser.waitFor("document.querySelectorAll('.lesson').length > 0", "训练包列表");
    await browser.evaluate(`
      [...document.querySelectorAll('.lesson button')]
        .find(b => b.textContent.trim() === '打开').click()
    `);
    await browser.waitFor("document.querySelectorAll('.example').length > 0", "例句列表");
    await openChannel(browser, "打字");
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

    // ---- 口语卡（翻面后）----
    await browser.goto(`${BASE}/#lessons`);
    await browser.waitFor("document.querySelectorAll('.lesson').length > 0", "训练包列表");
    await browser.evaluate(`
      [...document.querySelectorAll('.lesson button')]
        .find(b => b.textContent.trim() === '打开').click()
    `);
    await browser.waitFor("document.querySelectorAll('.example').length > 0", "例句列表");
    await openChannel(browser, "口语");
    await sleep(300);
    await browser.evaluate("reveal()");
    await sleep(300);
    made.push(await browser.screenshot(path.join(OUT, "07-speak-revealed.png")));

    // ---- 造句卡（翻面后）----
    await browser.goto(`${BASE}/#lessons`);
    await browser.waitFor("document.querySelectorAll('.lesson').length > 0", "训练包列表");
    await browser.evaluate(`
      [...document.querySelectorAll('.lesson button')]
        .find(b => b.textContent.trim() === '打开').click()
    `);
    await browser.waitFor("document.querySelectorAll('.example').length > 0", "例句列表");
    await openChannel(browser, "造句");
    await sleep(300);
    await browser.evaluate("reveal()");
    await sleep(300);
    made.push(await browser.screenshot(path.join(OUT, "08-build-revealed.png")));

    // ---- 设置页 ----
    await browser.goto(`${BASE}/#settings`);
    await browser.waitFor("document.querySelectorAll('table.self tr').length > 0", "自检表");
    made.push(await browser.screenshot(path.join(OUT, "09-settings.png")));

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

async function openChannel(browser, label) {
  // 从训练包详情里的「练XX」按钮进 —— 比在队列里翻 40 张卡快得多，
  // 而且顺带验证了「自由练」这条路是通的。
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

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error("生成截图时出错：" + err.message);
    process.exit(1);
  });
