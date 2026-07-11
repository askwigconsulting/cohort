"use strict";
// The per-launch token is injected server-side into a <meta> tag (not into this
// file, which is served verbatim and same-origin). Reading it here keeps the
// script static so `script-src 'self'` — no 'unsafe-inline' — can hold.
const TOKEN = document.querySelector('meta[name="cohort-token"]').getAttribute("content");
const $ = (id) => document.getElementById(id);
let STATE = null, PENDING = null, FOCUS = null;

/* ---------- plumbing ---------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "X-Cohort-Token": TOKEN, "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
  return body;
}
function toast(msg, cls) {
  const t = document.createElement("div");
  t.className = "toast " + (cls || ""); t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), cls === "err" ? 9000 : 5000);
}
function confirmDlg(title, body, yesLabel) {
  // Resolve through the dialog's close event so a native dismissal (Escape)
  // settles the promise too — a pending promise with live listeners could
  // otherwise fire a previously-dismissed action from a later dialog.
  return new Promise((resolve) => {
    $("cf-title").textContent = title; $("cf-body").textContent = body;
    $("cf-yes").textContent = yesLabel || "Confirm";
    const dlg = $("dlg-confirm");
    dlg.returnValue = "";
    $("cf-yes").addEventListener("click", () => dlg.close("yes"), { once: true });
    $("cf-no").addEventListener("click", () => dlg.close("no"), { once: true });
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "yes"), { once: true });
    dlg.showModal();
  });
}
async function runAction(btn, action, args, okMsg) {
  if (PENDING) return null;
  PENDING = action;
  if (btn) { btn.disabled = true; btn.classList.add("busy"); }
  try {
    const a = { ...(args || {}) }; if (FOCUS != null) a.project = FOCUS;
    const report = await api("/api/action", { method: "POST", body: JSON.stringify({ action, args: a }) });
    if (report.warning) toast(report.warning, "warn");
    if (okMsg) toast(okMsg);
    await refresh();
    return report;
  } catch (e) {
    toast(e.message, "err");
    return null;
  } finally {
    PENDING = null;
    if (btn) { btn.disabled = false; btn.classList.remove("busy"); }
  }
}

/* ---------- rendering ---------- */
function setDot(id, cls, label) {
  const d = $(id);
  d.className = "dot " + cls;
  d.lastElementChild.textContent = label;
}
/* Every kind shows on a card, tagged by kind, with metadata, clickable for detail. */
const KIND_META = {
  agent:   { tag: "AGENT",   plural: "Agents" },
  skill:   { tag: "SKILL",   plural: "Skills" },
  command: { tag: "COMMAND", plural: "Commands" },
  hook:    { tag: "HOOK",    plural: "Hooks" },
  memory:  { tag: "MEMORY",  plural: "Memories" },
  context: { tag: "CONTEXT", plural: "Context" },
};
const KIND_ORDER = ["agent", "skill", "command", "hook", "memory", "context"];
function kindTag(kind) {
  const km = KIND_META[kind] || { tag: (kind || "?").toUpperCase() };
  const t = document.createElement("span");
  t.className = "tag k-" + kind; t.textContent = km.tag;
  return t;
}
/* the short metadata chips shown on a card, per kind */
function metaBits(it) {
  const bits = [];
  if (it.kind === "agent") bits.push(
    it.advisory === false ? "⚡ doer" : (it.topology === "generalist" ? "triage" : "advisor"));
  if (it.kind === "hook" && it.event) bits.push(it.event);
  if (it.department) bits.push(it.department);
  for (const t of (it.targets || [])) if (t !== "all") bits.push(t);
  return bits;
}
function thumb(label, title, onClick) {
  const b = document.createElement("button");
  b.className = "thumb"; b.textContent = label; b.title = title;
  b.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
  return b;
}
function artifactCard(it) {
  const el = document.createElement("div");
  el.className = "artifact" + (it.active === false ? " off" : "");
  el.tabIndex = 0; el.setAttribute("role", "button");
  el.title = "View " + it.kind + " · " + it.name;

  const head = document.createElement("div"); head.className = "art-head";
  head.appendChild(kindTag(it.kind));
  const name = document.createElement("div"); name.className = "art-name";
  name.textContent = it.display_name || it.name;
  head.appendChild(name); el.appendChild(head);

  const desc = document.createElement("div"); desc.className = "art-desc";
  desc.textContent = it.description || ""; el.appendChild(desc);

  const foot = document.createElement("div"); foot.className = "art-foot";
  for (const b of metaBits(it)) {
    const chip = document.createElement("span"); chip.className = "meta"; chip.textContent = b;
    foot.appendChild(chip);
  }
  if (it.active === false) {
    const c = document.createElement("span"); c.className = "meta warn"; c.textContent = "not on roster"; foot.appendChild(c);
  }
  if (it.overrides) {
    const c = document.createElement("span"); c.className = "meta"; c.textContent = "override"; foot.appendChild(c);
  }
  el.appendChild(foot);

  // Per-card actions (revealed on hover) — click never bubbles to the detail view.
  const actions = document.createElement("div"); actions.className = "art-actions";
  if (it.kind === "agent") {
    actions.appendChild(thumb("👍", "Rate " + it.name + " up",
      () => runAction(null, "feedback", { rating: "up", agent: it.name }, "Feedback recorded for " + it.name)));
    actions.appendChild(thumb("👎", "Rate " + it.name + " down",
      () => runAction(null, "feedback", { rating: "down", agent: it.name }, "Feedback recorded for " + it.name)));
  }
  if (it.layer === "my") {
    actions.appendChild(thumb("✎", "Edit " + it.name, () => openEdit(it.kind, it.name)));
  }
  if (it.layer === "project" && it.kind === "agent") {
    actions.appendChild(thumb("✕", "Remove " + it.name + " from this project", async () => {
      const ok = await confirmDlg("Remove " + it.name + "?",
        "Removes the specialist's canonical source, compiled output, and placement from this repo — the same as cohort remove-specialist.", "Remove");
      if (ok) runAction(null, "remove-specialist", { name: it.name }, "Removed " + it.name);
    }));
  }
  el.appendChild(actions);

  const open = () => openDetail(it);
  el.addEventListener("click", (e) => { if (!e.target.closest(".art-actions")) open(); });
  el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  return el;
}
function ghostCard(label, onClick) {
  const g = document.createElement("div"); g.className = "artifact ghost"; g.textContent = label;
  g.addEventListener("click", onClick);
  return g;
}
/* One level (company / you / project): all kinds, grouped by kind, each tagged. */
function renderLevel(layer, holderId, opts) {
  opts = opts || {};
  const holder = $(holderId); holder.textContent = "";
  const items = (STATE.inventory || []).filter((it) => it.layer === layer);
  const cnt = opts.countId ? $(opts.countId) : null;
  if (cnt) cnt.textContent = items.length ? items.length + " item" + (items.length === 1 ? "" : "s") : "";

  if (!items.length && !opts.addCard) {
    const e = document.createElement("div"); e.className = "empty"; e.textContent = opts.empty || "Nothing here yet.";
    holder.appendChild(e);
    return;
  }
  for (const kind of KIND_ORDER) {
    const of = items.filter((it) => it.kind === kind);
    if (!of.length) continue;
    const d = document.createElement("div"); d.className = "dept";
    const h = document.createElement("h4"); h.textContent = (KIND_META[kind].plural) + " · " + of.length; d.appendChild(h);
    const grid = document.createElement("div"); grid.className = "grid";
    for (const it of of) grid.appendChild(artifactCard(it));
    d.appendChild(grid); holder.appendChild(d);
  }
  if (opts.addCard) {
    const d = document.createElement("div"); d.className = "dept";
    if (!items.length) {
      const h = document.createElement("h4"); h.textContent = opts.empty || ""; d.appendChild(h);
    }
    const grid = document.createElement("div"); grid.className = "grid";
    grid.appendChild(ghostCard(opts.addCard.label, opts.addCard.onClick));
    d.appendChild(grid); holder.appendChild(d);
  }
}
function renderLevels() {
  renderLevel("office", "level-office", {
    countId: "cnt-office",
    empty: "No company artifacts yet — run Recompile (or cohort setup) to install the office.",
  });
  renderLevel("my", "level-my", {
    countId: "cnt-my",
    empty: "Nothing that's just yours yet — create an agent, skill, command, or hook.",
    addCard: { label: "＋ Create", onClick: () => openCreate("my") },
  });
  renderLevel("project", "level-project", {
    countId: "cnt-project",
    empty: "Nothing here yet — create an agent, skill, command, or hook for this repo.",
    addCard: { label: "＋ Create", onClick: () => openCreate("project") },
  });
}
/* Click a card → full detail (metadata + body), fetched read-only for any layer. */
async function openDetail(it) {
  $("dt-title").textContent = (it.display_name || it.name);
  const meta = $("dt-meta"); meta.textContent = "";
  meta.appendChild(kindTag(it.kind));
  const LAYER_LABEL = { office: "COMPANY", my: "YOU", project: "PROJECT" };
  for (const text of [LAYER_LABEL[it.layer] || it.layer].concat(metaBits(it))) {
    const c = document.createElement("span"); c.className = "meta"; c.textContent = text; meta.appendChild(c);
  }
  if (it.active === false) { const c = document.createElement("span"); c.className = "meta warn"; c.textContent = "not on roster"; meta.appendChild(c); }
  $("dt-desc").textContent = it.description || "";
  $("dt-body").textContent = "Loading…";
  const editBtn = $("dt-edit");
  editBtn.hidden = it.layer !== "my";
  editBtn.onclick = it.layer === "my" ? () => { $("dlg-detail").close(); openEdit(it.kind, it.name); } : null;
  $("dlg-detail").showModal();
  try {
    const art = await api("/api/artifact?" + new URLSearchParams({ layer: it.layer, kind: it.kind, name: it.name }));
    $("dt-body").textContent = art.body || "(no body)";
  } catch (e) { $("dt-body").textContent = "Could not load: " + e.message; }
}
function renderProject(p) {
  $("project-section").hidden = !p; $("no-project").hidden = !!p;
  $("activity-section").hidden = !p;
  $("repo-chip").hidden = !p;
  if (!p) return;
  $("repo-chip").textContent = p.repo.split("/").slice(-2).join("/");
  const st = p.staleness || {};
  setDot("st-stale", st.stale ? "warn" : "", st.stale ? "context stale — snapshot?" : "context fresh");
  // Project specialists render in the "This project" level section (renderLevels);
  // renderProject owns only the staleness dot and the activity feed/stats below.
  const s = p.signals || {};
  const stats = $("stats"); stats.textContent = "";
  const rows = [
    [s.sessions || 0, "sessions captured"], [s.feedback_total || 0, "feedback entries"],
    [(s.low_rated_agents || []).length, "low-rated agents"], [(p.proposals || []).length, "proposals drafted"],
  ];
  for (const row of rows) {
    const el = document.createElement("div"); el.className = "stat";
    const b = document.createElement("b"); b.textContent = row[0];
    const sp = document.createElement("span"); sp.textContent = row[1];
    el.appendChild(b); el.appendChild(sp); stats.appendChild(el);
  }
  const feed = $("feed"); feed.textContent = "";
  const items = [];
  for (const x of (p.sessions || [])) items.push({ t: x.timestamp, ico: "⏱",
    txt: "Session captured" + (x.branch ? " on " + x.branch : "") + (x.author ? " by " + x.author : "") });
  for (const x of (p.feedback || [])) items.push({ t: x.timestamp, ico: x.rating === "up" ? "👍" : "👎",
    txt: (x.agent || x.command || "office") + " rated " + x.rating + (x.note ? " — " + x.note : "") });
  for (const x of (p.proposals || [])) items.push({ t: x.created_at, ico: "✦",
    txt: "Improvement proposal drafted" + (x.upstream_candidate ? " (upstream candidate)" : "") + (x.submitted_at ? " · submitted" : "") });
  items.sort((a, b) => String(b.t || "").localeCompare(String(a.t || "")));
  if (!items.length) {
    const li = document.createElement("li"); li.className = "empty";
    li.textContent = "Quiet so far — snapshots, ratings, and proposals will appear here.";
    feed.appendChild(li);
  }
  for (const it of items.slice(0, 12)) {
    const li = document.createElement("li");
    const ico = document.createElement("div"); ico.className = "ico"; ico.textContent = it.ico;
    const what = document.createElement("div"); what.className = "what"; what.textContent = it.txt;
    const when = document.createElement("div"); when.className = "when";
    when.textContent = (it.t || "").replace("T", " ").slice(0, 16);
    li.appendChild(ico); li.appendChild(what); li.appendChild(when); feed.appendChild(li);
  }
}
function renderProjectSwitcher(s) {
  const sel = $("project-switcher");
  const projects = s.projects || [];
  if (!projects.length) { sel.hidden = true; return; }
  sel.hidden = false;
  const focusedPath = s.focused_project;
  sel.textContent = "";
  for (const pr of projects) {
    const o = document.createElement("option");
    o.value = String(pr.index);
    o.textContent = pr.name + " · " + pr.specialists + " spec" + (pr.specialists === 1 ? "" : "s");
    if (pr.path === focusedPath) o.selected = true;
    sel.appendChild(o);
  }
}
/* The all-projects view: every registered Cohort project as a card; click one to
   manage it (focuses it → the "Managing" section shows its artifacts + actions). */
function renderProjectsOverview(s) {
  const projects = s.projects || [];
  const holder = $("projects-list"); holder.textContent = "";
  $("cnt-projects").textContent = projects.length ? projects.length + " project" + (projects.length === 1 ? "" : "s") : "";
  $("proj-focus-name").textContent = "";
  if (!projects.length) {
    const e = document.createElement("div"); e.className = "empty";
    e.textContent = "No Cohort projects registered yet — run `cohort init` in a repo to add it here.";
    holder.appendChild(e);
    return;
  }
  const focusedPath = s.focused_project;
  for (const p of projects) {
    const focused = p.path === focusedPath;
    if (focused) $("proj-focus-name").textContent = "· " + p.name;
    const el = document.createElement("div");
    el.className = "proj-card" + (focused ? " focused" : "");
    el.tabIndex = 0; el.setAttribute("role", "button");
    el.title = (focused ? "Managing " : "Manage ") + p.name;

    const head = document.createElement("div"); head.className = "proj-head";
    const name = document.createElement("div"); name.className = "proj-name"; name.textContent = p.name;
    head.appendChild(name);
    if (focused) {
      const b = document.createElement("span"); b.className = "tag proj"; b.textContent = "MANAGING"; head.appendChild(b);
    }
    el.appendChild(head);

    const path = document.createElement("div"); path.className = "proj-path";
    path.textContent = p.path; path.title = p.path; el.appendChild(path);

    const foot = document.createElement("div"); foot.className = "proj-foot";
    const spec = document.createElement("span"); spec.className = "meta";
    spec.textContent = p.specialists + " specialist" + (p.specialists === 1 ? "" : "s"); foot.appendChild(spec);
    const wire = document.createElement("span");
    wire.className = "meta" + (p.wiring === "present" ? "" : " warn");
    wire.textContent = p.wiring === "present" ? "wired" : ("wiring " + p.wiring); foot.appendChild(wire);
    el.appendChild(foot);

    const focus = () => { if (!focused) { FOCUS = String(p.index); refresh(); } };
    el.addEventListener("click", focus);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); focus(); } });
    holder.appendChild(el);
  }
}
/* Office-wide activity: recent session records from every initialized project
   (s.activity — server-merged, newest-first). Distinct from renderProject's feed,
   which is the focused project's own sessions/feedback/proposals. */
function renderCrossProjectActivity(s) {
  const feed = $("xproj-feed"); feed.textContent = "";
  const items = s.activity || [];
  if (!items.length) {
    const li = document.createElement("li"); li.className = "empty";
    li.textContent = "No sessions captured yet — run `cohort snapshot` in a Cohort project to start the feed.";
    feed.appendChild(li);
    return;
  }
  for (const it of items) {
    const li = document.createElement("li");
    const ico = document.createElement("div"); ico.className = "ico"; ico.textContent = "⏱";
    const what = document.createElement("div"); what.className = "what";
    what.textContent = (it.project || "?") + " — session captured" +
      (it.branch ? " on " + it.branch : "") + (it.author ? " by " + it.author : "");
    const when = document.createElement("div"); when.className = "when";
    when.textContent = (it.timestamp || "").replace("T", " ").slice(0, 16);
    li.appendChild(ico); li.appendChild(what); li.appendChild(when); feed.appendChild(li);
  }
}
/* Per-agent scorecards: binary up/down feedback aggregated across every
   initialized project (s.scorecards), with a day-by-day sparkline over the last
   30 days. Bar heights/colors are computed from numeric counts only — no
   disk-derived string ever reaches innerHTML. */
function renderScorecards(s) {
  const holder = $("scorecards"); holder.textContent = "";
  const cards = s.scorecards || [];
  if (!cards.length) {
    const e = document.createElement("div"); e.className = "empty";
    e.textContent = "No feedback recorded yet — rate an agent (👍/👎) to start building scorecards.";
    holder.appendChild(e);
    return;
  }
  for (const c of cards) {
    const el = document.createElement("div"); el.className = "score-card";

    const head = document.createElement("div"); head.className = "score-head";
    const name = document.createElement("div"); name.className = "score-name"; name.textContent = c.agent;
    head.appendChild(name);
    const net = document.createElement("span");
    net.className = "meta" + (c.net < 0 ? " bad" : "");
    net.textContent = (c.net > 0 ? "+" : "") + c.net + " net";
    head.appendChild(net);
    el.appendChild(head);

    const counts = document.createElement("div"); counts.className = "score-counts";
    const up = document.createElement("span"); up.className = "meta"; up.textContent = "👍 " + c.up;
    const down = document.createElement("span"); down.className = "meta"; down.textContent = "👎 " + c.down;
    counts.appendChild(up); counts.appendChild(down);
    if (c.up_ratio != null) {
      const ratio = document.createElement("span"); ratio.className = "meta";
      ratio.textContent = Math.round(c.up_ratio * 100) + "% up";
      counts.appendChild(ratio);
    }
    el.appendChild(counts);

    const spark = document.createElement("div"); spark.className = "sparkline";
    spark.title = "last 30 days";
    const trend = c.trend || [];
    if (!trend.length) {
      const t = document.createElement("span"); t.className = "meta"; t.textContent = "quiet last 30 days";
      spark.appendChild(t);
    } else {
      for (const day of trend) {
        const bar = document.createElement("span"); bar.className = "spark-bar";
        const dayNet = day.up - day.down;
        bar.style.height = Math.min(24, 4 + Math.abs(dayNet) * 4) + "px";
        bar.style.background = dayNet >= 0 ? "var(--ok)" : "var(--bad)";
        spark.appendChild(bar);
      }
    }
    el.appendChild(spark);
    holder.appendChild(el);
  }
}
/* ---------- mission control (life template, RFC 0003 §4) ----------
   Every node here is built with textContent / createElement — no disk- or
   connector-derived string ever reaches innerHTML. Edit controls dispatch a
   `cohort life` verb via runAction (the action bridge); the page performs no
   write itself. */
function lifeGroup(title, extra) {
  const g = document.createElement("div"); g.className = "grp";
  const h = document.createElement("div"); h.className = "grp-h";
  const label = document.createElement("span"); label.textContent = title; h.appendChild(label);
  if (extra) h.appendChild(extra);
  g.appendChild(h);
  return g;
}
function diagChips(host, diagnostics) {
  for (const d of (diagnostics || [])) {
    const s = document.createElement("span"); s.className = "diag"; s.textContent = "⚠ " + d;
    host.appendChild(s);
  }
}
/* One checklist item. Interactive scopes ("day-top3", "week-plan") dispatch a
   toggle by ENUMERATED index; a null scope renders read-only (e.g. goals). */
function checkItem(item, index, scope) {
  const li = document.createElement("li"); li.className = "check" + (item.done ? " done" : "");
  const box = document.createElement("input"); box.type = "checkbox"; box.checked = !!item.done;
  const label = document.createElement("label"); label.textContent = item.text || "(empty)";
  if (scope) {
    box.title = "Toggle via cohort life toggle-task";
    box.addEventListener("change", () => {
      // The server re-resolves the target by enumeration and rewrites the file;
      // refresh() then re-renders from disk truth, so no optimistic state is kept.
      runAction(null, "life-toggle-task", { scope, index },
        (item.done ? "Reopened: " : "Completed: ") + (item.text || ""));
    });
  } else {
    box.disabled = true;
  }
  li.appendChild(box); li.appendChild(label);
  return li;
}
function checkList(items, scope) {
  const ul = document.createElement("ul"); ul.className = "checks";
  items.forEach((it, i) => ul.appendChild(checkItem(it, i, scope)));
  if (!items.length) {
    const e = document.createElement("li"); e.className = "empty"; e.textContent = "Nothing here yet.";
    ul.appendChild(e);
  }
  return ul;
}
function renderToday(today) {
  const host = $("life-today"); host.textContent = "";
  const h = document.createElement("h3");
  h.appendChild(document.createTextNode("Today "));
  const small = document.createElement("small"); small.textContent = today.date || ""; h.appendChild(small);
  host.appendChild(h);
  diagChips(host, today.diagnostics);

  const ag = lifeGroup("Agenda");
  const agUl = document.createElement("ul"); agUl.className = "agenda";
  for (const ev of (today.agenda || [])) {
    const li = document.createElement("li");
    const t = document.createElement("span"); t.className = "t"; t.textContent = ev.time || "—";
    const ttl = document.createElement("span"); ttl.textContent = ev.title || "";
    li.appendChild(t); li.appendChild(ttl); agUl.appendChild(li);
  }
  if (!(today.agenda || []).length) {
    const e = document.createElement("li"); e.className = "empty"; e.textContent = "No events."; agUl.appendChild(e);
  }
  ag.appendChild(agUl); host.appendChild(ag);

  const editTop3 = document.createElement("button");
  editTop3.className = "thumb"; editTop3.textContent = "✎ edit"; editTop3.title = "Set today's top 3";
  editTop3.addEventListener("click", () => openTop3(today.top3 || []));
  const top = lifeGroup("Top 3", editTop3);
  top.appendChild(checkList(today.top3 || [], "day-top3"));
  host.appendChild(top);

  if (today.log) {
    const lg = lifeGroup("Log");
    const p = document.createElement("div"); p.className = "life-log"; p.textContent = today.log;
    lg.appendChild(p); host.appendChild(lg);
  }
}
function renderWeek(week) {
  const host = $("life-week"); host.textContent = "";
  const h = document.createElement("h3");
  h.appendChild(document.createTextNode("This week "));
  const small = document.createElement("small"); small.textContent = week.week || ""; h.appendChild(small);
  host.appendChild(h);
  diagChips(host, week.diagnostics);

  const plan = lifeGroup("Plan");
  plan.appendChild(checkList(week.plan || [], "week-plan"));
  const row = document.createElement("div"); row.className = "addrow";
  const input = document.createElement("input");
  input.placeholder = "Add a task to this week's plan…"; input.maxLength = 200;
  const add = document.createElement("button"); add.className = "btn"; add.textContent = "＋ Add";
  const submit = () => {
    const text = input.value.trim();
    if (!text) return;
    runAction(add, "life-add-task", { scope: "week-plan", text }, "Task added").then((r) => { if (r) input.value = ""; });
  };
  add.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
  row.appendChild(input); row.appendChild(add);
  plan.appendChild(row);
  host.appendChild(plan);

  if (week.review) {
    const rv = lifeGroup("Review");
    const p = document.createElement("div"); p.className = "life-log"; p.textContent = week.review;
    rv.appendChild(p); host.appendChild(rv);
  }
}
function renderGoals(goalFiles) {
  const host = $("life-goals"); host.textContent = "";
  const h = document.createElement("h3"); h.textContent = "Goals"; host.appendChild(h);
  let any = false;
  for (const file of (goalFiles || [])) {
    diagChips(host, file.diagnostics);
    for (const goal of (file.goals || [])) {
      any = true;
      const g = lifeGroup(goal.goal || "(goal)");
      const items = goal.items || [];
      const done = items.filter((x) => x.done).length;
      if (items.length) {
        const bar = document.createElement("div"); bar.className = "goalbar";
        const fill = document.createElement("i"); fill.style.width = Math.round((done / items.length) * 100) + "%";
        bar.appendChild(fill); g.appendChild(bar);
      }
      g.appendChild(checkList(items, null));  // goals are read-only in v1
      host.appendChild(g);
    }
  }
  if (!any) {
    const e = document.createElement("div"); e.className = "empty"; e.textContent = "No goals yet — add them in goals/."; host.appendChild(e);
  }
}
function renderRunbar(commands) {
  const bar = $("life-runbar"); bar.textContent = "";
  for (const cmd of (commands || [])) {
    const b = document.createElement("button"); b.className = "btn";
    b.appendChild(document.createTextNode("▶ /" + cmd));
    b.title = "Enqueue /" + cmd + " (a human-started `cohort run` executes it)";
    b.addEventListener("click", () => runAction(b, "life-enqueue", { command: cmd },
      "Enqueued /" + cmd + " — run it with `cohort run`"));
    bar.appendChild(b);
  }
}
function renderJobsAndBriefing(life) {
  const jobs = $("life-jobs"); jobs.textContent = "";
  const list = life.jobs || [];
  if (list.length) {
    for (const j of list) {
      const row = document.createElement("div"); row.className = "job-row";
      const cmd = document.createElement("span"); cmd.className = "meta"; cmd.textContent = "/" + (j.command || "?");
      const st = document.createElement("span"); st.className = "meta"; st.textContent = j.status || "queued";
      const when = document.createElement("span"); when.className = "when"; when.textContent = (j.requested_at || "").replace("T", " ").slice(0, 16);
      row.appendChild(cmd); row.appendChild(st); row.appendChild(when); jobs.appendChild(row);
    }
  } else {
    const e = document.createElement("div"); e.className = "empty";
    e.textContent = "No jobs requested — click a ▶ button to enqueue one.";
    jobs.appendChild(e);
  }

  const q = $("life-quarantine"); q.textContent = "";
  const briefing = life.briefing;
  if (briefing) {
    const banner = document.createElement("div"); banner.className = "banner";
    banner.textContent = "⚠ Untrusted, connector-derived — " + (briefing.name || "briefing") + " · shown as plain text";
    q.appendChild(banner);
    const pre = document.createElement("div"); pre.className = "quarantine";
    pre.textContent = briefing.text || "";  // textContent only — never innerHTML
    q.appendChild(pre);
    const more = (life.quarantine || []).length - 1;
    if (more > 0) {
      const n = document.createElement("div"); n.className = "empty";
      n.textContent = more + " older briefing" + (more === 1 ? "" : "s") + " in the quarantine.";
      q.appendChild(n);
    }
  }
}
function renderLife(life) {
  const sec = $("life-section");
  sec.hidden = !life;
  if (!life) return;
  $("life-name").textContent = life.date ? "· " + life.date : "";
  renderToday(life.today || {});
  renderWeek(life.week || {});
  renderGoals(life.goals || []);
  renderRunbar(life.commands || []);
  renderJobsAndBriefing(life);
}
function render(s) {
  STATE = s;
  $("version").textContent = "v" + (s.version || "?");
  const g = s.global || {};
  const src = g.source || {};
  setDot("st-source", src.ok ? "" : "bad", src.ok ? (src.linked ? "source linked" : "source ok") : "source broken — relink");
  const parity = Object.values(g.parity || {});
  const parityOk = parity.every((p) => p.ok !== false);
  setDot("st-parity", parity.length === 0 ? "warn" : parityOk ? "" : "warn",
    parity.length === 0 ? "not compiled" : parityOk ? "renderers in parity" : "parity gap");
  setDot("st-placed", g.roster.count ? "" : "warn", g.roster.count + " agents placed");
  const wiring = (s.project && s.project.wiring) || null;
  setDot("st-wiring", !wiring ? "warn" : wiring.state === "present" ? "" : "warn",
    !wiring ? "no project here" : wiring.state === "present" ? "project wired" : "wiring " + wiring.state + " — re-init");
  const up = g.update || {};
  const behind = up.behind || 0;
  $("behind").hidden = behind <= 0;
  if (behind > 0) $("behind").textContent = behind;
  $("btn-update").title = behind > 0
    ? behind + " commit" + (behind === 1 ? "" : "s") + " behind upstream"
    : "Office is current with upstream";
  const unmanaged = (g.unmanaged || []).length;
  $("pipe-sub").textContent = unmanaged
    ? unmanaged + " unmanaged file" + (unmanaged === 1 ? "" : "s") + " in ~/.claude — run cohort status for adopt hints"
    : "how your office actually works";
  renderProjectSwitcher(s);
  renderProjectsOverview(s);
  renderCrossProjectActivity(s);
  renderScorecards(s);
  renderLevels();
  renderProject(s.project || null);
  renderLife(s.life || null);
}
async function refresh() {
  try { render(await api("/api/state" + (FOCUS != null ? "?project=" + FOCUS : ""))); }
  catch (e) { console.error("refresh failed:", e); } // transient poll failure or render bug
}

/* ---------- wiring ---------- */
$("btn-update").addEventListener("click", async () => {
  const behind = (STATE && STATE.global.update && STATE.global.update.behind) || 0;
  const msg = behind > 0
    ? "Fast-forward the office " + behind + " commit" + (behind === 1 ? "" : "s") +
      " to upstream, reinstall if dependencies changed, and recompile. Only a clean fast-forward is ever applied."
    : "The office looks current — update will verify against upstream and recompile if anything changed.";
  if (await confirmDlg("Update the office?", msg, "Update"))
    runAction($("btn-update"), "update", {}, "Office updated");
});
$("btn-recompile").addEventListener("click", () =>
  runAction($("btn-recompile"), "recompile", {}, "Recompiled and placed"));
$("btn-snapshot").addEventListener("click", () =>
  runAction($("btn-snapshot"), "snapshot", {}, "Session snapshot captured"));
$("btn-propose").addEventListener("click", () =>
  runAction($("btn-propose"), "propose-improvement", {}, "Improvement proposal drafted — review it in .cohort/proposals/"));
$("btn-reinit").addEventListener("click", async () => {
  if (await confirmDlg("Re-init this project?",
    "Re-runs cohort init --force: re-scaffolds anything missing and re-asserts the managed wiring block in this repo's CLAUDE.md. Your project context content is preserved.", "Re-init"))
    runAction($("btn-reinit"), "init", { force: true }, "Project re-initialized");
});
/* ---------- create (my office OR the focused project) ---------- */
let CREATE_LEVEL = "my";
function syncCreateKind() {
  const kind = $("cr-kind").value;
  for (const g of document.querySelectorAll("#create-form [data-kind]"))
    g.hidden = g.getAttribute("data-kind") !== kind;
}
function openCreate(level) {
  CREATE_LEVEL = level === "project" ? "project" : "my";
  if (CREATE_LEVEL === "project") {
    const p = (STATE && STATE.project && STATE.project.repo) ? STATE.project.repo.split("/").slice(-1)[0] : "this project";
    $("cr-title").textContent = "Create in " + p;
    $("cr-hint").textContent = "Authors it in this project (.cohort/canonical/) so it travels with the repo, and recompiles. Advisory read-only where it applies.";
  } else {
    $("cr-title").textContent = "Create an artifact";
    $("cr-hint").textContent = "Authors it in my office (~/.cohort/my) and recompiles — updates and proposals never touch it. Advisory read-only where it applies.";
  }
  syncCreateKind();
  $("dlg-create").showModal();
}
$("cr-kind").addEventListener("change", syncCreateKind);
$("btn-create").addEventListener("click", () => openCreate("my"));
$("cr-cancel").addEventListener("click", () => $("dlg-create").close());
$("create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  const kind = $("cr-kind").value;
  const name = $("cr-name").value.trim();
  const args = { name, description: $("cr-desc").value.trim(), body: $("cr-body").value };
  if (kind === "agent") { args.display_name = $("cr-display").value.trim(); args.department = $("cr-dept").value.trim(); }
  if (kind === "skill") args.triggers = $("cr-triggers").value;
  if (kind === "command") args.invocation = $("cr-invocation").value.trim();
  if (kind === "hook") { args.event = $("cr-event").value; args.action_cmd = $("cr-action").value.trim(); args.matcher = $("cr-matcher").value.trim(); }
  if (kind === "memory") args.priority = $("cr-priority").value;
  let r;
  if (CREATE_LEVEL === "project") {
    args.kind = kind;
    r = await runAction(btn, "create-project", args, kind + " " + name + " created in this project");
  } else {
    r = await runAction(btn, "add-" + kind, args, kind + " " + name + " created in my office");
  }
  if (r) { $("dlg-create").close(); e.target.reset(); }
});

/* ---------- edit ---------- */
let EDIT_TARGET = null;
async function openEdit(kind, name) {
  try {
    const art = await api("/api/artifact?" + new URLSearchParams({ layer: "my", kind, name }));
    EDIT_TARGET = { kind, name };
    $("ed-title").textContent = "Edit " + kind + " " + name;
    $("ed-desc").value = art.description || "";
    $("ed-body").value = art.body || "";
    $("dlg-edit").showModal();
  } catch (e) { toast(e.message, "err"); }
}
$("ed-cancel").addEventListener("click", () => $("dlg-edit").close());
$("dt-close").addEventListener("click", () => $("dlg-detail").close());
$("edit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!EDIT_TARGET) return;
  const btn = e.target.querySelector("button[type=submit]");
  const r = await runAction(btn, "edit", {
    kind: EDIT_TARGET.kind, name: EDIT_TARGET.name, layer: "my",
    body: $("ed-body").value, description: $("ed-desc").value.trim(),
  }, EDIT_TARGET.kind + " " + EDIT_TARGET.name + " updated");
  if (r) { $("dlg-edit").close(); EDIT_TARGET = null; }
});

/* ---------- set-top-3 (life) ---------- */
function openTop3(items) {
  $("t3-1").value = (items[0] && items[0].text) || "";
  $("t3-2").value = (items[1] && items[1].text) || "";
  $("t3-3").value = (items[2] && items[2].text) || "";
  $("dlg-top3").showModal();
}
$("t3-cancel").addEventListener("click", () => $("dlg-top3").close());
$("top3-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  const items = [$("t3-1").value, $("t3-2").value, $("t3-3").value].map((v) => v.trim()).filter(Boolean);
  const r = await runAction(btn, "life-set-top3", { items }, "Top 3 updated");
  if (r) $("dlg-top3").close();
});

$("project-switcher").addEventListener("change", (e) => { FOCUS = e.target.value; refresh(); });
refresh();
setInterval(() => { if (!PENDING) refresh(); }, 6000);
