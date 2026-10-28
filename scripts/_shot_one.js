/**
 * 临时：给某个路由拍一张图。
 *   node scripts/_shot_one.js "#/progress" "%TEMP%\polish\progress.png" [theme]
 * 用完就删。
 */
"use strict";
const path = require("path");
const { Browser } = require("./cdp");

const route = process.argv[2] || "#/study";
const out = process.argv[3] || path.join(process.env.TEMP, "polish", "shot.png");
const theme = process.argv[4] || "";
const script = process.argv[5] || "";

(async () => {
  const browser = new Browser({ width: 1280, height: 900 });
  await browser.launch();
  try {
    await browser.goto("http://127.0.0.1:8977/" + route, 1700);
    if (theme) {
      // 主题走真实的按钮（点 #theme-btn 循环切换），不要直接写 localStorage ——
      // 写完立刻 navigate 在无头 Chrome 里可能丢这次写，而且真实路径才验得到持久化。
      for (let i = 0; i < 5; i += 1) {
        const now = await browser.evaluate("document.documentElement.dataset.theme || 'auto'");
        const effective =
          now === "auto"
            ? await browser.evaluate(
                "window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'",
              )
            : now;
        if (now === theme || effective === theme) break;
        await browser.evaluate("document.querySelector('#theme-btn').click()");
        await new Promise((r) => setTimeout(r, 220));
      }
      await browser.reload(1200);
    }
    if (script) {
      await browser.evaluate(script);
      await new Promise((r) => setTimeout(r, 500));
    }
    await browser.screenshot(out);
    console.log("saved", out);
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
