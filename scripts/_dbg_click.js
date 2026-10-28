/**
 * 临时：点一下某个选择器，回头看看 #overlay-host 里有没有东西。
 *   node scripts/_dbg_click.js "#about-btn" "#/study"
 * 用完就删。
 */
"use strict";
const { Browser } = require("./cdp");

const sel = process.argv[2] || "#about-btn";
const route = process.argv[3] || "#/study";
const settle = Number(process.argv[4] || 600);

(async () => {
  const browser = new Browser({ width: 1280, height: 900 });
  await browser.launch();
  try {
    await browser.goto("http://127.0.0.1:8977/" + route, 1600);
    const found = await browser.evaluate(`Boolean(document.querySelector(${JSON.stringify(sel)}))`);
    if (!found) {
      console.log("找不到", sel);
      return;
    }
    await browser.evaluate(`document.querySelector(${JSON.stringify(sel)}).click()`);
    await new Promise((r) => setTimeout(r, settle));
    const state = await browser.evaluate(
      `(() => {
        const host = document.querySelector("#overlay-host");
        const drawer = document.querySelector("#overlay-host .drawer");
        return {
          overlayLength: host ? host.innerHTML.length : -1,
          drawer: Boolean(drawer),
          title: drawer ? (drawer.querySelector("h2, h3, .drawer-head") || {}).textContent : "",
          text: drawer ? drawer.innerText.slice(0, 400) : "",
        };
      })()`,
    );
    console.log(JSON.stringify(state, null, 2));
    if (browser.pageErrors.length || browser.consoleErrors.length) {
      console.log("errors", browser.pageErrors, browser.consoleErrors);
    }
  } finally {
    await browser.close();
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
