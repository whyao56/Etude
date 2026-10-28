/**
 * 临时调试：打印某个选择器的 outerHTML / 文本 / 计算样式。
 *   node scripts/_dbg_dom.js ".side-foot"            -> outerHTML
 *   node scripts/_dbg_dom.js ".grade" html|text|css  -> 同上，或取计算样式
 * 用完就删。
 */
"use strict";
const { Browser } = require("./cdp");

const selector = process.argv[2] || "body";
const mode = process.argv[3] || "html";
const route = process.argv[4] || "#/study";

(async () => {
  const browser = new Browser({ width: 1280, height: 900 });
  await browser.launch();
  try {
    await browser.goto("http://127.0.0.1:8977/" + route, 1600);
    const expr = `(() => {
      const node = document.querySelector(${JSON.stringify(selector)});
      if (!node) return "NO NODE: " + ${JSON.stringify(selector)};
      if (${JSON.stringify(mode)} === "text") return node.textContent;
      if (${JSON.stringify(mode)} === "css") return JSON.stringify(getComputedStyle(node), null, 1);
      return node.outerHTML;
    })()`;
    console.log(await browser.evaluate(expr));
  } finally {
    await browser.close();
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
