/**
 * 临时脚本：把每个页面按几个视口截图，供改造前盘点问题用。
 * 用完就删（正式出图是 scripts/screenshots.js）。
 *
 *   node scripts/_polish_shot.js [--base http://127.0.0.1:8977] [--out %TEMP%\polish]
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { Browser } = require("./cdp");

function arg(name, fallback) {
  const i = process.argv.indexOf("--" + name);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const BASE = arg("base", "http://127.0.0.1:8977");
const OUT = arg("out", path.join(os.tmpdir(), "polish"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shot(browser, name) {
  const file = path.join(OUT, name + ".png");
  await browser.screenshot(file);
  console.log("  " + name);
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = new Browser({ width: 1280, height: 900 });
  await browser.launch();
  try {
    await browser.goto(BASE + "/#/study", 1500);

    const pages = [
      ["study", "#/study"],
      ["lessons", "#/lessons"],
      ["lesson", "#/lessons/2"],
      ["plans", "#/plans"],
      ["cards", "#/cards"],
      ["settings", "#/settings"],
    ];

    for (const [name, hash] of pages) {
      await browser.goto(BASE + "/" + hash, 1400);
      await shot(browser, "1280-" + name);
    }

    // 训练页的四种通道各来一张（听力/打字/造句与阅读的版式不一样）
    for (const channel of ["听 力", "打字", "造句"]) {
      await browser.goto(BASE + "/#/cards", 1200);
      await browser.evaluate(`(async () => {
        const selects = document.querySelectorAll("#app select");
        const sel = selects[1];
        const opt = [...sel.options].find((o) => o.textContent.includes("${channel.replace(" ", "")}"));
        if (!opt) return "no-option";
        sel.value = opt.value;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        return opt.textContent;
      })()`);
      await sleep(900);
      await browser.evaluate(`(async () => {
        const btn = document.querySelector("#browser-host table tbody tr .rowacts .btn");
        if (!btn) return "no-row";
        btn.click();
        return "clicked";
      })()`);
      await sleep(1200);
      await shot(browser, "1280-study-" + channel.replace(" ", ""));
    }

    // 窄屏：笔记本 1366 的一半、以及窗口最小宽 960
    await browser.setViewport(960, 780);
    for (const [name, hash] of [["study", "#/study"], ["cards", "#/cards"], ["settings", "#/settings"]]) {
      await browser.goto(BASE + "/" + hash, 1200);
      await shot(browser, "960-" + name);
    }

    // 深色主题
    await browser.setViewport(1280, 900);
    for (const theme of ["dark", "light"]) {
      await browser.evaluate(`localStorage.setItem("etude-prefs", JSON.stringify({ theme: "${theme}" }))`);
      await browser.goto(BASE + "/#/study", 1400);
      await shot(browser, "theme-" + theme + "-study");
      await browser.goto(BASE + "/#/settings", 1200);
      await shot(browser, "theme-" + theme + "-settings");
    }

    if (browser.pageErrors.length || browser.consoleErrors.length) {
      console.log("页面报错：", browser.pageErrors, browser.consoleErrors);
    }
  } finally {
    await browser.close();
  }
  console.log("输出到", OUT);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
