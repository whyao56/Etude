/* 关于 / 快捷键。
 *
 * 为什么这两样要放在界面里，而不是只写在 README：
 * 用户遇到「按钮没反应」的第一反应是找版本号和日志在哪，
 * 而这两样恰好都必须从界面里能点到 —— 让人去翻安装目录，
 * 等于把「不会用」变成「找不到」。
 */

import { get, post } from "../core/api.js";
import { el, kbd } from "../core/dom.js";
import { state } from "../core/store.js";
import { drawer, toast } from "../core/ui.js";
import { APP_VERSION } from "../version.js";

const DASH = "—";

function row(label, value, mono = false) {
  return el("div", { class: "kv-row" }, [
    el("div", { class: "kv-k", text: label }),
    el("div", { class: `kv-v${mono ? " mono" : ""}`, text: value || DASH }),
  ]);
}

function folderRow(label, which) {
  const button = el("button", { class: "btn sm", type: "button" }, [
    el("span", { class: "ico", dataset: { icon: "folder" } }),
    el("span", { text: label }),
  ]);
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await post("/api/open-folder", { which });
      toast(`已经打开了：${result.path}`, "ok");
    } catch (err) {
      toast(err.hint || err.message || "打不开这个文件夹。", "bad");
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

export async function openAbout() {
  const health = state.health || {};
  const dataRow = el("div", { class: "about-folders" });
  const sheet = drawer({
    title: "关于 Etude",
    badge: `v${APP_VERSION}`,
    body: [
      el("div", { class: "about-hero" }, [
        el("div", { class: "about-mark", text: "É" }),
        el("div", {}, [
          el("b", { text: "句子五通道语言训练器" }),
          el("div", {
            class: "hint",
            text: "一个词配 8 条不同场景的例句，阅读、听力、口语、打字、造句分开练，复习间隔由算法排。",
          }),
        ]),
      ]),
      el("div", { class: "kv" }, [
        row("界面版本", APP_VERSION),
        row("后端版本", health.version || DASH),
        row("运行形态", health.mode === "exe" ? "打包版（exe）" : health.mode ? "源码运行" : DASH),
        row("进程号", health.pid ? String(health.pid) : DASH, true),
        row("端口", health.port ? String(health.port) : DASH, true),
        row("数据目录", health.data_dir || DASH, true),
      ]),
      dataRow,
      el("p", {
        class: "card-note",
        text: "所有数据都在你自己的机器上：例句、语音、录音、复习记录，一个字节都不上传。模型和语音是本地程序去调的外网服务，key 也只存在本机。",
      }),
    ],
    footer: [
      (() => {
        const btn = el("button", { class: "btn sm", type: "button" }, [
          el("span", { class: "ico", dataset: { icon: "copy" } }),
          el("span", { text: "复制诊断信息" }),
        ]);
        btn.addEventListener("click", async () => {
          const payload = {
            app: "Etude",
            ui_version: APP_VERSION,
            backend: health,
            data_location: state.dataLocation || null,
            at: new Date().toISOString(),
          };
          try {
            await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
            toast("诊断信息已经复制，粘到哪儿都行。", "ok");
          } catch (err) {
            toast("浏览器不让写剪贴板，手动复制吧。", "warn");
          }
        });
        return btn;
      })(),
      el("div", { class: "grow" }),
      (() => {
        const btn = el("button", { class: "btn sm ghost", type: "button", text: "关闭" });
        // drawer() 返回的是 { close, wrap, onClose } 这个把手，不是一个函数。
        btn.addEventListener("click", () => sheet.close());
        return btn;
      })(),
    ],
  });
  dataRow.append(
    el("div", { class: "row wrap about-buttons" }, [
      folderRow("数据目录", "data"),
      folderRow("日志", "logs"),
      folderRow("录音", "recordings"),
      folderRow("备份", "backups"),
    ]),
  );
  // 有数据目录的详情就顺手带出来，没有也不影响上面的按钮。
  try {
    state.dataLocation = await get("/api/data-location");
    dataRow.prepend(
      el("p", {
        class: "hint",
        text: `占用 ${bytes(state.dataLocation.bytes)} · ${state.dataLocation.files} 个文件 · 来源 ${sourceText(
          state.dataLocation.source,
        )}`,
      }),
    );
  } catch (err) {
    /* 数据目录信息拿不到时，按钮照样能用 */
  }
}

function sourceText(source) {
  if (source === "bootstrap") return "改过位置";
  if (source === "ETUDE_DATA_DIR") return "环境变量";
  return "默认位置";
}

function bytes(value) {
  const size = Number(value) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/* 每一行是 [按键, 干什么]。按键用 | 分隔 —— 以前用空格分隔，
 * 「空格 / Enter」被拆成了三个键帽（空格、斜杠、Enter），看着像要按顺序按。 */
const SHORTCUTS = [
  ["空格|Enter", "翻面看答案"],
  ["1|2|3|4", "四档打分：没想起来 / 吃力 / 正常 / 秒答"],
  ["←|→", "上一张 / 跳过这张"],
  ["Z", "撤销上一次评分"],
  ["E", "编辑这张卡的例句"],
  ["R", "重放这句的语音"],
  ["?", "打开这份快捷键表"],
  ["/", "跳到卡片页并聚焦搜索框"],
  ["Esc", "关掉抽屉 / 收起这一层"],
];

export function openShortcuts() {
  const sheet = drawer({
    title: "快捷键",
    badge: "键盘更快",
    body: [
      el("p", {
        class: "card-note",
        text: "训练时手不用离开键盘。数字键打分之前会先把答案翻开 —— 免得打错。",
      }),
      el(
        "div",
        { class: "shortcut-grid" },
        SHORTCUTS.map(([keys, what]) =>
          el("div", { class: "shortcut-row" }, [
            el("div", { class: "shortcut-keys" }, keys.split("|").map((key) => kbd(key))),
            el("div", { class: "shortcut-what", text: what }),
          ]),
        ),
      ),
    ],
    footer: [
      el("div", { class: "grow" }),
      (() => {
        const btn = el("button", { class: "btn sm ghost", type: "button", text: "知道了" });
        btn.addEventListener("click", () => sheet.close());
        return btn;
      })(),
    ],
  });
}
