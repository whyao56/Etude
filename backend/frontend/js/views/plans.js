/* 目标：说清「我要干什么」，让模型排出该学什么。
 *
 * 为什么要有这一页：给一个词建一个包，是「我知道该学什么」时用的工具；
 * 但更常见的情况是「我知道我要用英语干什么，但不知道从哪开始」。
 * 所以这一页先让模型出一份大纲（几秒、一次调用），你看过、删过，再开工。
 */

import { del, get, post, put } from "../core/api.js";
import { el, humanWhen, paintIcons } from "../core/dom.js";
import { state } from "../core/store.js";
import {
  banner,
  chip,
  emptyBlock,
  errorBlock,
  field,
  loading,
  sectionHead,
  segmented,
  setPage,
  toast,
} from "../core/ui.js";
import { go } from "../core/router.js";

const GOAL_LABEL = { fluency: "能开口", exam: "应付考试", custom: "自己描述" };
const timers = new Map();

export async function render(host) {
  setPage({
    title: "目标",
    sub: "说清要干什么，让模型排出该学什么",
    actions: [],
  });
  host.replaceChildren(loading("读计划…"));
  try {
    await ensureCatalog();
    const data = await get("/api/plans");
    state.plans = data.plans || [];
  } catch (err) {
    host.replaceChildren(errorBlock(err, () => render(host)));
    return;
  }
  host.replaceChildren();
  host.appendChild(formBlock(host));
  host.appendChild(listBlock(host));
  paintIcons(host);
}

async function ensureCatalog() {
  if (state.catalog) return state.catalog;
  state.catalog = await get("/api/catalog");
  return state.catalog;
}

/* ------------------------------------------------------------------ 表单 */

function formBlock(host) {
  const catalog = state.catalog || {};
  const goals = catalog.goal_kinds || [];
  const exams = catalog.exam_presets || [];
  const domains = catalog.domain_presets || [];

  const draft = {
    goalKind: (goals[0] && goals[0].id) || "fluency",
    exam: (exams[0] && exams[0].id) || "",
    detail: "",
    domains: new Set(domains.slice(0, 2).map((item) => item.id)),
    count: 20,
  };

  const box = el("section", { class: "card" });
  box.appendChild(el("h3", { text: "新目标" }));

  const kindHost = el("div", { class: "row" });
  const examHost = el("div", { class: "row" });
  const detail = el("textarea", {
    class: "input",
    rows: "2",
    placeholder: "再具体一点：要能用英语干什么？比如「下个月去冰岛自驾，得能问路和订房」",
  });
  const domainHost = el("div", { class: "chips" });
  const countHost = el("div", { class: "row" });
  const message = el("div", { class: "hint", text: "" });
  const button = el("button", { class: "btn primary", type: "button", text: "生成大纲" });

  function drawKinds() {
    kindHost.replaceChildren(
      segmented(
        goals.map((goal) => ({ id: goal.id, label: goal.label, note: goal.what })),
        draft.goalKind,
        (id) => {
          draft.goalKind = id;
          drawKinds();
          drawExams();
          drawCounts();
        },
      ),
    );
  }

  function drawExams() {
    examHost.replaceChildren();
    if (draft.goalKind !== "exam") return;
    const select = el(
      "select",
      { class: "input" },
      exams.map((exam) =>
        el("option", {
          value: exam.id,
          text: `${exam.label}（${exam.band || ""}）`,
          selected: exam.id === draft.exam,
        }),
      ),
    );
    select.addEventListener("change", () => {
      draft.exam = select.value;
    });
    examHost.appendChild(field("要考的试", select));
  }

  function drawDomains() {
    domainHost.replaceChildren(
      ...domains.map((domain) => {
        const button = el("button", {
          class: "chip pick",
          type: "button",
          text: domain.label,
          title: domain.what || "",
          "aria-pressed": draft.domains.has(domain.id) ? "true" : "false",
        });
        button.addEventListener("click", () => {
          if (draft.domains.has(domain.id)) draft.domains.delete(domain.id);
          else draft.domains.add(domain.id);
          drawDomains();
        });
        return button;
      }),
    );
  }

  function drawCounts() {
    const options = [10, 20, 50];
    countHost.replaceChildren(
      segmented(
        options.map((value) => ({ id: String(value), label: `${value} 个训练包` })),
        String(draft.count),
        (id) => {
          draft.count = Number(id);
          drawCounts();
        },
      ),
      el("span", { class: "hint", text: `大约 ${Math.round(draft.count * 0.9)}–${Math.round(draft.count * 1.6)} 分钟生成` }),
    );
  }

  button.addEventListener("click", async () => {
    button.disabled = true;
    message.textContent = "正在让模型排大纲…（一次调用，几秒）";
    try {
      const payload = {
        language: "en",
        goal_kind: draft.goalKind,
        goal_label: draft.goalKind === "custom" ? detail.value.trim() : GOAL_LABEL[draft.goalKind] || "",
        goal_detail:
          draft.goalKind === "exam"
            ? `${draft.exam} · ${detail.value.trim()}`
            : detail.value.trim(),
        domains: [...draft.domains],
        target_count: draft.count,
      };
      const res = await post("/api/plans", payload);
      message.textContent = res.note || "大纲出来了，去下面看一眼。";
      await render(host);
      if (res.plan && res.plan.id) go("plans", res.plan.id);
    } catch (err) {
      message.textContent = err.message;
    } finally {
      button.disabled = false;
    }
  });

  drawKinds();
  drawExams();
  drawDomains();
  drawCounts();

  box.append(
    field("想干什么", kindHost),
    examHost,
    field("具体一点（可不填）", detail),
    field("侧重哪些场合", domainHost),
    field("要多少", countHost),
    el("div", { class: "row" }, [button, message]),
  );
  return box;
}

/* ------------------------------------------------------------------ 列表 */

function listBlock(host) {
  const box = el("div", { class: "list-block" });
  box.appendChild(sectionHead("已有计划", `${(state.plans || []).length} 份`));
  const plans = state.plans || [];
  if (!plans.length) {
    box.appendChild(
      emptyBlock({
        art: "wand",
        title: "还没有计划",
        text: "上面选一下「想干什么」，模型会先给一份该学什么的清单。",
      }),
    );
    return box;
  }
  for (const plan of plans) box.appendChild(planRow(plan, host));
  return box;
}

function planRow(plan, host) {
  const progress = plan.progress || {};
  const total = Number(progress.total || 0);
  const ready = Number(progress.ready || 0);
  const row = el("div", { class: "plan-row" });

  const open = el("button", { class: "plan-open", type: "button" }, [
    el("b", { text: goalText(plan) }),
    el("span", { class: "meta", text: `${plan.target_count} 个目标 · ${humanWhen(plan.created_at)}` }),
  ]);
  open.addEventListener("click", () => go("plans", plan.id));

  const bar = el("div", { class: "bar" }, [
    el("i", { style: { width: `${total ? Math.round((ready / total) * 100) : 0}%` } }),
  ]);

  const chips = el("div", { class: "chips" }, [
    chip(statusLabel(plan.status), plan.status === "failed" ? "bad" : plan.status === "generating" ? "accent" : "ok"),
    total ? chip(`${ready}/${total} 个可用`) : chip("还没开工"),
  ]);

  row.append(open, chips, bar);
  if (plan.status === "generating") watch(plan.id, host);
  return row;
}

function goalText(plan) {
  if (plan.goal_label) return plan.goal_label;
  return GOAL_LABEL[plan.goal_kind] || plan.goal_kind;
}

function statusLabel(status) {
  return { draft: "只有大纲", generating: "正在生成", ready: "生成完了", failed: "生成失败" }[status] || status;
}

/* ------------------------------------------------------------------ 详情 */

export async function renderDetail(host, planId) {
  setPage({ title: "计划", sub: "正在读…", actions: [] });
  host.replaceChildren(loading("读计划…"));
  let plan;
  try {
    plan = await get(`/api/plans/${planId}`);
  } catch (err) {
    setPage({ title: "计划", sub: "打不开" });
    host.replaceChildren(errorBlock(err, () => renderDetail(host, planId)));
    return;
  }
  state.openPlan = plan;
  setPage({
    title: goalText(plan),
    sub: `${(plan.outline || []).length} 条大纲 · ${plan.target_count} 个目标`,
    actions: [
      el("button", { class: "btn ghost", type: "button", text: "回目标", onClick: () => go("plans") }),
    ],
  });
  host.replaceChildren();
  host.appendChild(outlineCard(host, plan));
  host.appendChild(lessonsCard(host, plan));
  paintIcons(host);
  if (plan.status === "generating") pollPlan(host, planId);
}

function outlineCard(host, plan) {
  const box = el("section", { class: "card" });
  const outline = plan.outline || [];
  box.appendChild(
    el("div", { class: "row" }, [
      el("h3", { class: "grow", text: "大纲" }),
      chip(statusLabel(plan.status), plan.status === "failed" ? "bad" : plan.status === "generating" ? "accent" : ""),
    ]),
  );
  box.appendChild(
    el("div", { class: "hint", text: "钩掉不想现在就学的，剩下的会按顺序一个一个生成。生成要十几分钟，可以先去别处。" }),
  );

  const chosen = new Set(outline.map((item) => item.target));
  const rows = outline.map((item) => {
    const checkbox = el("input", { type: "checkbox", checked: true });
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) chosen.add(item.target);
      else chosen.delete(item.target);
    });
    return el("label", { class: "outline-row" }, [
      checkbox,
      el("span", { class: "outline-target", text: item.target }),
      item.domain ? chip(item.domain) : null,
      item.band ? chip(item.band) : null,
      el("span", { class: "outline-why grow", text: item.why || "" }),
    ]);
  });
  box.appendChild(el("div", { class: "outline-list" }, rows));

  if (plan.status === "generating") {
    const progress = plan.progress || {};
    box.appendChild(
      el("div", { class: "row" }, [
        el("span", { class: "spin" }),
        el("span", {
          class: "hint",
          text: `正在生成：${progress.ready || 0}/${progress.total || 0} 好了，${progress.running || 0} 个在跑。`,
        }),
      ]),
    );
    return box;
  }

  const start = el("button", {
    class: "btn primary",
    type: "button",
    text: `开始生成（${chosen.size} 个）`,
  });
  start.addEventListener("click", async () => {
    const keep = outline.filter((item) => chosen.has(item.target));
    if (!keep.length) {
      toast("一个都没选。", "warn");
      return;
    }
    start.disabled = true;
    start.textContent = "已经排队…";
    try {
      await put(`/api/plans/${plan.id}/outline`, { outline: keep });
      await post(`/api/plans/${plan.id}/start`, {});
      toast("开始生成了。可以先去练别的，这里会自己更新。", "ok");
      await renderDetail(host, plan.id);
    } catch (err) {
      toast(err.message, "bad");
      start.disabled = false;
      start.textContent = `开始生成（${chosen.size} 个）`;
    }
  });

  const remove = el("button", { class: "btn danger", type: "button", text: "删掉这份计划" });
  remove.addEventListener("click", async () => {
    const keepLessons = window.confirm(
      "删掉计划。已经生成的训练包要一起删吗？\n\n确定 = 连训练包一起删\n取消 = 只删计划，训练包留着",
    );
    try {
      await del(`/api/plans/${plan.id}?keep_lessons=${keepLessons ? "false" : "true"}`);
      toast("删掉了。", "ok");
      go("plans");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  box.append(
    el("div", { class: "row" }, [
      start,
      el("span", { class: "grow" }),
      remove,
    ]),
  );
  if (plan.error) box.appendChild(el("div", { class: "err" }, [el("b", { text: plan.error })]));
  return box;
}

function lessonsCard(host, plan) {
  const box = el("section", { class: "card" });
  const lessons = plan.lessons || [];
  box.appendChild(el("h3", { text: `已生成的训练包 ${lessons.length} 个` }));
  if (!lessons.length) {
    box.appendChild(el("div", { class: "hint", text: "还没有。点上面的「开始生成」。" }));
    return box;
  }
  for (const lesson of lessons) {
    const row = el("div", { class: "lesson-row small" }, [
      el("button", {
        class: "lesson-open",
        type: "button",
        onClick: () => go("lessons", lesson.id),
      }, [
        el("b", { class: "t", text: lesson.target }),
        el("span", { class: "meta", text: `${lesson.example_count || 0} 条例句` }),
      ]),
      el("div", { class: "chips" }, [
        lesson.status === "ready"
          ? chip("能练了", "ok")
          : lesson.status === "failed"
            ? chip("失败", "bad")
            : chip("生成中", "accent"),
      ]),
    ]);
    box.appendChild(row);
  }
  return box;
}

/* ------------------------------------------------------------------ 轮询 */

function watch(planId, host) {
  if (timers.has(planId)) return;
  const timer = window.setInterval(async () => {
    try {
      const job = await get(`/api/plans/${planId}/job`);
      if (job.status !== "generating") {
        window.clearInterval(timer);
        timers.delete(planId);
        if (state.plans && state.plans.some((plan) => plan.id === planId)) {
          await render(host);
        }
      }
    } catch (err) {
      window.clearInterval(timer);
      timers.delete(planId);
    }
  }, 2500);
  timers.set(planId, timer);
}

function pollPlan(host, planId) {
  if (timers.has(`detail:${planId}`)) return;
  const timer = window.setInterval(async () => {
    try {
      const job = await get(`/api/plans/${planId}/job`);
      if (job.status !== "generating") {
        window.clearInterval(timer);
        timers.delete(`detail:${planId}`);
        if (job.status === "failed") banner("bad", `生成失败：${job.error || ""}`);
        await renderDetail(host, planId);
      } else {
        const node = document.querySelector(".outline-list");
        if (node && job.progress) {
          const hint = node.parentElement.querySelector(".hint:last-of-type");
          if (hint) {
            hint.textContent = `正在生成：${job.progress.ready || 0}/${job.progress.total || 0} 好了，${job.progress.running || 0} 个在跑。`;
          }
        }
      }
    } catch (err) {
      window.clearInterval(timer);
      timers.delete(`detail:${planId}`);
    }
  }, 2500);
  timers.set(`detail:${planId}`, timer);
}
