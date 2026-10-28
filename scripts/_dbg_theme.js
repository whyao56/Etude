"use strict";
const { Browser } = require("./cdp");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const b = new Browser({ width: 1200, height: 800 });
  await b.launch();
  try {
    await b.goto("http://127.0.0.1:8977/#/study", 1500);
    await b.evaluate('localStorage.removeItem("etude-prefs")');
    await b.goto("http://127.0.0.1:8977/#/study", 1500);
    console.log("start:", await b.evaluate('JSON.stringify(window.__etude.state.prefs)'));
    // 点三次：auto -> warm -> light -> dark
    for (let i = 0; i < 3; i += 1) {
      await b.evaluate('document.querySelector("#theme-btn").click()');
      await sleep(300);
      console.log("click", i + 1, await b.evaluate('JSON.stringify(window.__etude.state.prefs)'),
        await b.evaluate('document.documentElement.getAttribute("data-theme")'),
        await b.evaluate('localStorage.getItem("etude-prefs")'));
    }
    await b.goto("http://127.0.0.1:8977/#/study", 1800);
    console.log("after reload:", await b.evaluate('JSON.stringify(window.__etude.state.prefs)'),
      await b.evaluate('document.documentElement.getAttribute("data-theme")'),
      await b.evaluate('localStorage.getItem("etude-prefs")'));
  } finally {
    await b.close();
  }
})();
