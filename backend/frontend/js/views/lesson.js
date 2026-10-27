/* 训练包详情：一包里的每条句子都在这里，能听、能看、能改、能单练。
 *
 * 0.0.x 这里进不了地址栏，刷新就掉回列表 —— 0.1.0 起 `#/lessons/<id>` 就是它。
 * 详情页承担的是「素材出了问题就地修」：缺语音补语音、缺图补图、
 * 中文说法不对就改、句子不满意就删掉或重写。
 */

import { del, get, patch, post } from "../core/api.js";
import { play, speak } from "../core/audio.js";
import { el, humanWhen, paintIcons } from "../core/dom.js";
import { CHANNEL_LABEL, audioUrl, imageUrl, state } from "../core/store.js";
import {
  banner,
  chip,
  drawer,
  errorBlock,
  field,
  loading,
  setPage,
  toast,
  zoomImage,
} from "../core/ui.js";
import { go } from "../core/router.js";
import { pronouncePanel } from "./pronounce.js";
import { render as renderStudy } from "./study.js";

const KIND_LABEL = { word: "词", phrase: "固定说法", sentence: "整句" };
const CHANNELS = ["read", "listen", "speak", "type", "build"];

export async function render(host, lessonId) {
  setPage({ title: "训练包", sub: "正在读…", actions: [] });
  host.replaceChildren(loading("读训练包…"));
  let lesson;
  try {
    lesson = await get(`/api/lessons/${lessonId}`);
  } catch (err) {
    setPage({ title: "训练包", sub: "打不开" });
    host.replaceChildren(
      errorBlock(err, () => render(host, lessonId)),
      el("div", { class: "row" }, [
        el("button", {
          class: "btn ghost",
          type: "button",
          text: "回训练包列表",
          onClick: () => go("lessons"),
        }),
      ]),
    );
    return;
  }
  state.lessonDetail = lesson;
  setPage({
    title: lesson.target,
    sub: `${KIND_LABEL[lesson.kind] || lesson.kind} · ${lesson.examples.length} 条例句`,
    actions: [
      el("button", {
        class: "btn ghost",
        type: "button",
        text: "回列表",
        onClick: () => go("lessons"),
      }),
    ],
  });
  host.replaceChildren();
  host.appendChild(summaryCard(host, lesson));
  host.appendChild(readinessCard(host, lesson));
  host.appendChild(examplesCard(host, lesson));
  host.appendChild(addExampleCard(host, lesson));
  host.appendChild(dangerCard(host, lesson));
  paintIcons(host);
  if (lesson.status === "generating") pollJob(host, lessonId);
}

let jobTimer = 0;

function pollJob(host, lessonId) {
  if (jobTimer) window.clearInterval(jobTimer);
  jobTimer = window.setInterval(async () => {
    try {
      const job = await get(`/api/lessons/${lessonId}/job`);
      if (job.status !== "generating") {
        window.clearInterval(jobTimer);
        jobTimer = 0;
        if (job.status === "failed") banner("bad", `生成失败：${job.error || ""}`);
        await render(host, lessonId);
      }
    } catch (err) {
      window.clearInterval(jobTimer);
      jobTimer = 0;
    }
  }, 1500);
}

/* ------------------------------------------------------------------ 概要 */

function summaryCard(host, lesson) {
  const box = el("section", { class: "card" });
  const head = el("div", { class: "row" }, [
    el("span", { class: "target-big", text: lesson.target }),
    chip(KIND_LABEL[lesson.kind] || lesson.kind),
    lesson.status === "generating" ? chip((lesson.job && lesson.job.step) || "正在生成", "accent") : null,
    lesson.status === "failed" ? chip("生成失败", "bad") : null,
    el("span", { class: "grow" }),
    el("span", { class: "hint", text: lesson.created_at ? humanWhen(lesson.created_at) : "" }),
  ]);
  box.appendChild(head);

  if (lesson.ipa) box.appendChild(el("div", { class: "ipa mono", text: `/${lesson.ipa}/` }));
  if (lesson.gloss) {
    box.appendChild(
      el("div", { class: "gloss" }, [
        el("span", { class: "lab", text: "英英释义" }),
        el("span", { text: lesson.gloss }),
      ]),
    );
  }
  if (lesson.note) {
    box.appendChild(
      el("div", { class: "scene-en" }, [el("span", { class: "lab", text: "备注" }), el("span", { text: lesson.note })]),
    );
  }
  if (lesson.status === "failed" && lesson.error) {
    box.appendChild(el("div", { class: "err" }, [el("b", { text: lesson.error })]));
  }
  return box;
}

function readinessCard(host, lesson) {
  const ready = lesson.readiness || {};
  const box = el("section", { class: "card" });
  box.appendChild(
    el("div", { class: "row" }, [
      chip(ready.ready ? "能练了" : "还不能练", ready.ready ? "ok" : "warn"),
      chip(`${ready.example_count || 0} 条例句`),
      chip(ready.audio_ready ? "语音齐了" : `缺 ${(ready.missing_audio_ids || []).length} 条语音`, ready.audio_ready ? "ok" : "warn"),
      ready.images_expected
        ? chip(
            ready.image_ready ? "配图齐了" : `缺 ${(ready.missing_image_ids || []).length} 张图`,
            ready.image_ready ? "ok" : "warn",
          )
        : chip("没开配图"),
      el("span", { class: "grow" }),
    ]),
  );

  const problems = ready.problems || [];
  if (problems.length) {
    box.appendChild(
      el(
        "div",
        { class: "problems" },
        problems.map((text) => el("div", { class: "problem", text })),
      ),
    );
  }

  const actions = el("div", { class: "row" });
  const audioBtn = el("button", { class: "btn", type: "button", text: "补齐缺的语音" });
  audioBtn.addEventListener("click", async () => {
    audioBtn.disabled = true;
    audioBtn.textContent = "正在合成…";
    try {
      const res = await post(`/api/lessons/${lesson.id}/audio`, {});
      toast(res.skipped ? res.skipped : `补了 ${res.ok || 0} 条语音。`, "ok");
      await render(host, lesson.id);
    } catch (err) {
      toast(err.message, "bad");
      audioBtn.disabled = false;
      audioBtn.textContent = "补齐缺的语音";
    }
  });
  actions.appendChild(audioBtn);

  if (ready.images_expected) {
    const imgBtn = el("button", { class: "btn", type: "button", text: "补齐缺的配图" });
    imgBtn.addEventListener("click", async () => {
      imgBtn.disabled = true;
      imgBtn.textContent = "正在配图…";
      try {
        const res = await post(`/api/lessons/${lesson.id}/images`, {});
        toast(res.skipped ? res.skipped : `补了 ${res.ok || 0} 张图。`, "ok");
        await render(host, lesson.id);
      } catch (err) {
        toast(err.message, "bad");
        imgBtn.disabled = false;
        imgBtn.textContent = "补齐缺的配图";
      }
    });
    actions.appendChild(imgBtn);
  }
  if (!lesson.cards || !lesson.cards.length) {
    const cardsBtn = el("button", { class: "btn", type: "button", text: "补卡片" });
    cardsBtn.addEventListener("click", async () => {
      try {
        const res = await post(`/api/lessons/${lesson.id}/rebuild-cards`, {});
        toast(`补了 ${res.created || 0} 张卡。`, "ok");
        await render(host, lesson.id);
      } catch (err) {
        toast(err.message, "bad");
      }
    });
    actions.appendChild(cardsBtn);
  }
  box.appendChild(actions);
  return box;
}

/* ------------------------------------------------------------------ 例句 */

function examplesCard(host, lesson) {
  const box = el("section", { class: "card" });
  box.appendChild(el("h3", { text: `例句 ${lesson.examples.length} 条` }));
  if (!lesson.examples.length) {
    box.appendChild(
      el("div", { class: "hint", text: lesson.status === "generating" ? "还在生成，稍等一下。" : "还没有例句。也可以自己加一条。" }),
    );
    return box;
  }
  for (const example of lesson.examples) {
    box.appendChild(exampleRow(host, lesson, example));
  }
  return box;
}

function exampleRow(host, lesson, example) {
  const row = el("div", { class: "example-row" });

  const media = el("div", { class: "example-media" });
  if (example.scene_image) {
    const img = el("img", { src: imageUrl(example.scene_image), alt: "", loading: "lazy" });
    img.addEventListener("click", () => zoomImage(imageUrl(example.scene_image)));
    img.addEventListener("error", () => media.replaceChildren(el("div", { class: "imgfail", text: "图打不开了" })));
    media.appendChild(img);
  } else {
    media.appendChild(el("div", { class: "imgfail", text: "没有图" }));
  }

  const body = el("div", { class: "example-body" });
  body.appendChild(
    el("div", { class: "example-en" }, [
      el("b", { text: example.sentence }),
      example.register ? chip(example.register) : null,
    ]),
  );
  if (example.scene_en) body.appendChild(el("div", { class: "example-scene", text: example.scene_en }));
  if (example.scene_zh) body.appendChild(el("div", { class: "example-zh", text: example.scene_zh }));
  const variants = (example.zh_variants || []).filter(Boolean);
  if (variants.length > 1) {
    body.appendChild(
      el("div", { class: "variants" }, variants.map((text) => el("span", { class: "variant", text }))),
    );
  }

  const actions = el("div", { class: "chanbtns" });
  for (const channel of CHANNELS) {
    const card = (lesson.cards || []).find(
      (item) => item.example_id === example.id && item.channel === channel,
    );
    const button = el("button", {
      class: "chanbtn",
      type: "button",
      text: CHANNEL_LABEL[channel],
      title: card ? "练这条" : "这张卡还没建出来",
      disabled: !card,
    });
    button.addEventListener("click", () => practiceOne(lesson, card, host));
    actions.appendChild(button);
  }

  const tools = el("div", { class: "row small" }, [
    playButton(example),
    el("button", {
      class: "btn sm ghost",
      type: "button",
      text: "跟读",
      onClick: () => openPronounce(example),
    }),
    el("button", {
      class: "btn sm ghost",
      type: "button",
      text: "编辑",
      onClick: () => openEditor(host, lesson, example),
    }),
    el("button", {
      class: "iconbtn",
      type: "button",
      title: "删掉这条例句",
      onClick: async () => {
        if (!window.confirm("删掉这条例句？它上面的复习进度也会一起没。")) return;
        try {
          const res = await del(`/api/examples/${example.id}`);
          toast(`删掉了（连带 ${res.deleted_cards || 0} 张卡）。`, "ok");
          await render(host, lesson.id);
        } catch (err) {
          toast(err.message, "bad");
        }
      },
    }, [el("span", { class: "ico", dataset: { icon: "trash" } })]),
  ]);

  body.appendChild(actions);
  body.appendChild(tools);
  row.append(media, body);
  return row;
}

function playButton(example) {
  const button = el("button", { class: "btn sm", type: "button" }, [
    el("span", { class: "ico", dataset: { icon: "play" } }),
    el("span", { text: "听" }),
  ]);
  button.addEventListener("click", async () => {
    if (example.audio_path) {
      try {
        await play(audioUrl(example.audio_path));
      } catch (err) {
        toast(err.message, "bad");
      }
      return;
    }
    if (speak(example.sentence)) toast("这条还没有语音，用系统语音念的。", "warn");
    else toast("这条还没有语音。点「补齐缺的语音」。", "bad");
  });
  return button;
}

function practiceOne(lesson, card, host) {
  if (!card) return;
  state.queue = [{ id: card.id }];
  state.cursor = 0;
  state.current = null;
  state.revealed = false;
  state.session = { done: 0, skipped: 0, history: [], startedAt: Date.now() };
  go("study");
  const app = document.getElementById("app");
  if (app) renderStudy(app);
}

function openPronounce(example) {
  const panel = pronouncePanel({
    exampleId: example.id,
    audioSrc: example.audio_path ? audioUrl(example.audio_path) : "",
    open: true,
  });
  drawer({
    title: `跟读`,
    badge: chip(example.sentence.length > 28 ? `${example.sentence.slice(0, 28)}…` : example.sentence),
    body: [panel],
  });
}

/* ------------------------------------------------------------------ 编辑 */

function openEditor(host, lesson, example) {
  {
    const sentence = el("input", { class: "input", type: "text", value: example.sentence || "" });
    const sceneEn = el("textarea", { class: "input", rows: "2", value: example.scene_en || "" });
    const sceneZh = el("textarea", { class: "input", rows: "2", value: example.scene_zh || "" });
    const variants = el("textarea", {
      class: "input",
      rows: "3",
      value: (example.zh_variants || []).join("\n"),
      placeholder: "一行一种说法",
    });
    const register = el("input", { class: "input", type: "text", value: example.register || "" });
    const message = el("div", { class: "hint", text: "" });

    const save = el("button", { class: "btn primary", type: "button", text: "保存" });
    const close = () => drawerRef.close();
    save.addEventListener("click", async () => {
      save.disabled = true;
      message.textContent = "正在保存…";
      try {
        await patch(`/api/examples/${example.id}`, {
          sentence: sentence.value.trim(),
          scene_en: sceneEn.value.trim(),
          scene_zh: sceneZh.value.trim(),
          zh_variants: variants.value
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean),
          register: register.value.trim(),
        });
        close();
        toast("改好了。", "ok");
        await render(host, lesson.id);
      } catch (err) {
        message.textContent = err.message;
        save.disabled = false;
      }
    });

    const redAudio = el("button", { class: "btn sm", type: "button", text: "重新生成语音" });
    redAudio.addEventListener("click", async () => {
      redAudio.disabled = true;
      redAudio.textContent = "合成中…";
      try {
        await post(`/api/examples/${example.id}/audio`, {});
        toast("语音好了。", "ok");
        close();
        await render(host, lesson.id);
      } catch (err) {
        message.textContent = err.message;
        redAudio.disabled = false;
        redAudio.textContent = "重新生成语音";
      }
    });

    const redImage = el("button", { class: "btn sm", type: "button", text: "重新配图" });
    redImage.addEventListener("click", async () => {
      redImage.disabled = true;
      redImage.textContent = "找图中…";
      try {
        await post(`/api/examples/${example.id}/image`, {});
        toast("图好了。", "ok");
        close();
        await render(host, lesson.id);
      } catch (err) {
        message.textContent = err.message;
        redImage.disabled = false;
        redImage.textContent = "重新配图";
      }
    });

    const drawerRef = drawer({
      title: "改这条例句",
      badge: chip(lesson.target),
      body: [
        field("英文句子", sentence),
        field("场景（英文）", sceneEn, "写「什么情况下会说这句话」，不要写这句话的翻译。"),
        field("中文场景", sceneZh, "口语一点：什么场合、谁对谁说。"),
        field("中文说法（多行）", variants, "同一句可以有两种说法，翻面时随机取一种。"),
        field("语域", register, "日常口语 / 正式 / 书面 / 俚语"),
        message,
        el("div", { class: "row" }, [redAudio, redImage]),
      ],
      footer: [
        el("button", { class: "btn ghost", type: "button", text: "取消", onClick: () => drawerRef.close() }),
        save,
      ],
    });
  }
}

/* ------------------------------------------------------------------ 追加 */

function addExampleCard(host, lesson) {
  const box = el("section", { class: "card" });
  box.appendChild(el("h3", { text: "自己加一条例句" }));
  box.appendChild(
    el("div", { class: "hint", text: "加完会顺手合成语音、找一张配图 —— 听力卡上不会出现「永远没声音」的句子。" }),
  );
  const sentence = el("input", { class: "input", type: "text", placeholder: "英文句子" });
  const sceneEn = el("input", { class: "input", type: "text", placeholder: "场景（英文，可以不填）" });
  const message = el("div", { class: "hint", text: "" });
  const button = el("button", { class: "btn primary", type: "button", text: "加进去" });
  button.addEventListener("click", async () => {
    const text = sentence.value.trim();
    if (!text) {
      sentence.focus();
      return;
    }
    button.disabled = true;
    message.textContent = "正在加（会顺带合成语音和配图）…";
    try {
      const res = await post(`/api/lessons/${lesson.id}/examples`, {
        sentence: text,
        scene_en: sceneEn.value.trim(),
        make_media: true,
      });
      const failures = res.failures || [];
      if (failures.length) message.textContent = `加好了，但：${failures.join("；")}`;
      else message.textContent = "加好了。";
      sentence.value = "";
      sceneEn.value = "";
      await render(host, lesson.id);
    } catch (err) {
      message.textContent = err.message;
    } finally {
      button.disabled = false;
    }
  });
  box.append(
    field("英文句子", sentence),
    field("场景（英文）", sceneEn),
    el("div", { class: "row" }, [button, message]),
  );
  return box;
}

function dangerCard(host, lesson) {
  const box = el("section", { class: "card tight" });
  const regen = el("button", { class: "btn", type: "button", text: "整个重生成" });
  regen.addEventListener("click", async () => {
    if (
      !window.confirm(
        "重新生成会先清空现在这些例句和语音，复习进度也会一起没。确定要重来？",
      )
    ) {
      return;
    }
    try {
      await post(`/api/lessons/${lesson.id}/regenerate`, {});
      toast("重新开始生成了。", "ok");
      await render(host, lesson.id);
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  const remove = el("button", { class: "btn danger", type: "button", text: "删掉这个训练包" });
  remove.addEventListener("click", async () => {
    if (!window.confirm(`删掉「${lesson.target}」？不可恢复。`)) return;
    try {
      await del(`/api/lessons/${lesson.id}`);
      toast("删掉了。", "ok");
      go("lessons");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  box.append(el("div", { class: "row" }, [regen, el("span", { class: "grow" }), remove]));
  return box;
}
