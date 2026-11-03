"use strict";
/* Home, the other side of the top bar's Chat | Home: the lab at a glance, labMaster's Home in Kelvin's colours.
   One line of hello (a greeting, what goes on now, the lab's name); three tiles on one row (the sequence, the runs, and
   the lab with a line of its first watched reading over the last ten minutes), each the link to its pane; under them,
   in the rest of the height, the blocks Kelvin keeps for this person (homeboard.py: a result, a next step, a reading to
   watch, a note, a checklist), which a person pins, ticks or takes off. Home fits the window by construction (home.css).
   It draws from the page's state poll (kelvin:state) and asks the server only when something moved: the blocks when the
   state's home stamp changes, the runs when a run starts or ends, and both when Home is shown. */
(() => {
  const SVGNS = "http://www.w3.org/2000/svg";
  // a block's kind: the edge of its card (the kit's kind colours), its small icon, and the icon's name
  const KINDS = {
    result: { cls: "k-exp", name: "Result", icon: '<path d="M4 19V5M4 19h16"/><path d="M7 15l4-5 3 3 5-7"/>' },
    next: { cls: "k-scan", name: "Next step", icon: '<path d="M5 12h13M13 6l6 6-6 6"/>' },
    watch: { cls: "k-step", name: "Watch",
             icon: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>' },
    note: { cls: "k-wait", name: "Note", icon: '<path d="M5 4h14v10.5L13.5 20H5z"/><path d="M13.5 20v-5.5H19"/>' },
    check: { cls: "k-script", name: "Checklist", icon: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 12.5l2.8 2.8L16 9.8"/>' },
  };
  const PIN = '<path d="M9.5 3.5h5l-.9 6.2 3.4 3.3v1.5H7v-1.5l3.4-3.3z"/><path d="M12 14.5V21"/>';
  const ENDED = new Set(["done", "failed", "stopped", "skipped"]);
  const RUNS_ASKED = 50;   // as many as the Runs pane asks for: enough to count today's
  // the Lab tile's line: the first watched reading over the last ten minutes, as this page's state polls saw it, drawn in
  // a box of SW x SH stretched to the tile's width (its stroke keeps its own), from three values on
  const SPAN_S = 600, SW = 300, SH = 48;
  // a reading's unit with its SI prefix (cards.si: "100 ms", "4.12 µs"): 999 mK and 1.001 K are on one line
  const SI_UNITS = new Set(["V", "A", "Hz", "W", "s", "Ω", "T", "K", "F", "H", "S", "m"]);
  const SI_PREFIX = { G: 1e9, M: 1e6, k: 1e3, m: 1e-3, "µ": 1e-6, n: 1e-9, p: 1e-12, f: 1e-15 };
  // what Home holds between state ticks: its parts, the blocks shown (by id), what was last asked of the server, and the
  // line's points ([time, value in the unprefixed unit], oldest first)
  const H = { parts: null, stamp: undefined, known: null, nodes: new Map(), blocksAsked: 0,
              runs: null, runsKey: undefined, runsAsked: 0, runsSig: null, watchRows: new Map(), watchSig: null,
              spark: { key: null, unit: null, pts: [] } };
  let K = null;   // window.Kelvin, once the page is ready

  const $ = (id) => document.getElementById(id);
  const shown = () => { const v = $("homeview"); return !!v && !v.hidden; };
  function guard(fn) {   // Home never throws into the page: a part that fails is skipped, the rest is drawn
    try { return fn(); } catch (e) { if (window.console) console.warn("Home:", e); return undefined; }
  }
  const safe = (fn) => function () { const args = arguments; return guard(() => fn.apply(this, args)); };
  function svg(paths) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    s.innerHTML = paths;   // constant markup only
    return s;
  }
  function put(el, words) {   // a text changed in place, only when it differs (no flicker, no needless redraw)
    const t = words == null ? "" : String(words);
    if (el.textContent !== t) el.textContent = t;
  }
  function cls(el, name) { if (el.className !== name) el.className = name; }
  const clamp = (x) => (x > 0 ? (x < 1 ? x : 1) : 0);
  const hhmm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const planOf = (runId) => String(runId || "").split("-").slice(2).join("-") || String(runId || "");

  function greeting(d) {   // labMaster's rule, on this browser's clock
    const hr = d.getHours();
    return hr >= 5 && hr < 12 ? "Good morning" : hr >= 12 && hr < 18 ? "Good afternoon"
      : hr >= 18 && hr < 23 ? "Good evening" : "Working late?";
  }
  function ago(t) {
    if (!t) return "";
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 50) return "just now";
    if (s < 90) return "1 min ago";
    if (s < 3300) return Math.round(s / 60) + " min ago";
    if (s < 5400) return "1 h ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    const d = Math.round(s / 86400);
    return d === 1 ? "yesterday" : d + " days ago";
  }
  function left(sec) {   // a step's time to go: 40 s, 12 min, 1.5 h
    if (!(sec > 0)) return "";
    return sec < 90 ? Math.ceil(sec) + " s left" : sec < 5400 ? Math.round(sec / 60) + " min left" : (sec / 3600).toFixed(1) + " h left";
  }
  function num(v) {   // a value as Kelvin wrote it; a float's binary tail trimmed
    if (typeof v !== "number" || !isFinite(v)) return String(v);
    return String(Number(v.toPrecision(6)));
  }
  function reading(s, key) {   // "tc.T_sample" → its value in the state, already with its unit
    const k = String(key || ""), dot = k.indexOf(".");
    if (dot < 1 || !s || !s.status) return null;
    const rows = s.status[k.slice(0, dot)];
    const row = Array.isArray(rows) ? rows.find((r) => r && r.name === k.slice(dot + 1)) : null;
    return row ? String(row.value) : null;
  }
  function measure(text) {   // "1.598 K" → {x: 1.598, unit: "K"}, "100 ms" → {x: 0.1, unit: "s"}; a word ("off", "ERROR") → null
    const m = /^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(\S*)\s*$/.exec(text == null ? "" : String(text));
    if (!m) return null;
    let x = Number(m[1]), unit = m[2];
    if (!isFinite(x)) return null;
    if (!SI_UNITS.has(unit) && unit.length > 1 && SI_PREFIX[unit[0]] && SI_UNITS.has(unit.slice(1))) {
      x *= SI_PREFIX[unit[0]];
      unit = unit.slice(1);
    }
    return { x, unit };
  }
  // the alarms on now (a Got it does not end one: its reading is still out of range until it clears)
  const alarmsOn = (s) => ((s && s.alarms) || []).filter((a) => a && !a.cleared);

  // ------------------------------------------------------------------ the page: the hello, the tiles, the blocks
  // a tile's place: its pane, the view of it the tile shows (Runs: the Sequence, or History's list of all runs), and the
  // page's own toggle for it, for a page without the panes' click model
  const PLACES = {
    seq: ["runpane", "schedule", () => K.toggleSeq(true)],
    runs: ["runpane", "history", () => K.openRunList()],
    lab: ["labpanel", null, () => K.toggleLab(true)],
    analysis: ["anpane", null, () => K.toggleAnalysis(true)],
  };
  // a tile is a link to its place (brief 1.3): Kelvin.paneShow shows the pane when it is not on the screen (in full
  // screen too), and when it is, leaves it there and rings its head; a tile never hides it
  function goTo(name) {
    const [pane, view, open] = PLACES[name];
    if (K.paneShow) K.paneShow(pane, view); else open();
  }
  function tile(name, title) {   // a card that is its pane's link: its title, its number, what it holds
    const h = K.h;
    const t = { name };
    t.go = h("button", { class: "home-tile-go", type: "button" }, title);
    t.go.addEventListener("click", safe(() => goTo(name)));
    t.chip = h("span", { class: "chip", hidden: true });
    t.kpi = h("span", { class: "home-kpi-n" }, "–");
    t.unit = h("span", { class: "home-kpi-unit" });
    t.body = h("div", { class: "home-tile-body" });
    t.el = h("div", { class: "home-tile home-tile-" + name },
      h("div", { class: "home-tile-head" }, t.go, t.chip), h("div", { class: "home-kpi" }, t.kpi, t.unit), t.body);
    return t;
  }
  function sparkline() {   // the Lab tile's line (drawn in drawSpark): an area, and the line over it
    const s = document.createElementNS(SVGNS, "svg"), fill = document.createElementNS(SVGNS, "path");
    const line = document.createElementNS(SVGNS, "polyline");
    s.setAttribute("class", "home-spark");
    s.setAttribute("viewBox", "0 0 " + SW + " " + SH);
    s.setAttribute("preserveAspectRatio", "none");
    s.setAttribute("aria-hidden", "true");
    fill.setAttribute("class", "home-spark-fill");
    line.setAttribute("class", "home-spark-line");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    s.append(fill, line);
    return { svg: s, fill, line };
  }
  function build() {
    const h = K.h;
    const p = H.parts = {};
    p.greet = h("h1", { class: "home-greet display" }, greeting(new Date()));
    p.dot = h("span", { class: "home-dot", "aria-hidden": "true" });
    p.say = h("span", { class: "home-say" });
    p.open = h("button", { class: "btn small home-open", type: "button", hidden: true }, "Open");
    p.open.addEventListener("click", safe(openApproval));
    p.lab = h("span", { class: "home-labname" });
    p.state = h("div", { class: "home-state", role: "status" }, p.dot, p.say, p.open);
    p.seq = tile("seq", "Sequence");
    p.seqFill = h("span", { class: "home-bar-fill" });
    p.seqName = h("span", { class: "home-line-name" });
    p.seqNum = h("span", { class: "home-line-num" });
    p.seq.body.append(h("div", { class: "home-bar", "aria-hidden": "true" }, p.seqFill),
      h("div", { class: "home-line" }, p.seqName, p.seqNum));
    p.runs = tile("runs", "Runs");
    p.runList = h("ul", { class: "home-mini home-runs" });
    p.runs.body.append(p.runList);
    p.labt = tile("lab", "Lab");
    p.watchList = h("ul", { class: "home-mini home-watches" });
    p.spark = sparkline();
    p.sparkRow = h("li", { class: "home-spark-row", hidden: true }, p.spark.svg);   // under the first watched reading
    p.labt.body.append(p.watchList);
    // the blocks: the rest of the height, the one part of Home that scrolls (brief 1.7: a region)
    p.blocks = h("div", { class: "home-blocks", "data-region": "home-blocks", hidden: true });
    $("homeview").replaceChildren(h("div", { class: "home-col" },
      h("header", { class: "home-hello" }, p.greet, p.state, p.lab),
      h("div", { class: "home-tiles" }, p.seq.el, p.runs.el, p.labt.el),
      p.blocks));
  }
  function chip(t, kind, words) {
    t.chip.hidden = !words;
    if (!words) return;
    cls(t.chip, "chip" + (kind ? " " + kind : ""));
    put(t.chip, words);
  }

  // ------------------------------------------------------------------ the state line: what goes on, in a few words
  function sequence(s) {   // the queue as the tiles and the line read it
    const q = (s && s.queue) || {};
    const items = Array.isArray(q.items) ? q.items : [];
    let run = items.findIndex((i) => i.status === "running");
    if (run < 0 && q.running) run = items.findIndex((i) => i.id === q.running);
    const next = items.findIndex((i) => i.status === "waiting");
    const item = run >= 0 ? items[run] : null;
    return { q, items, run, next, item, total: items.length, prog: item ? progress(item, s) : null,
             ended: items.filter((i) => ENDED.has(i.status)).length,
             at: run >= 0 ? run + 1 : next >= 0 ? next + 1 : items.length };
  }
  function progress(item, s) {   // the running item's: a scan's points, a step's wait; {frac, words} or null
    const p = item.progress || {};
    const now = Date.now() / 1000;
    if (p.total > 0) return { frac: clamp((p.done || 0) / p.total), words: (p.done || 0) + "/" + p.total };
    if (p.until) {
      if (p.phase === "stabilize") return { frac: 0, words: "settling" };
      const from = item.started || now;
      return { frac: p.until > from ? clamp((now - from) / (p.until - from)) : 0, words: left(p.until - now) };
    }
    const live = s && s.live;   // before the queue says it: the run going on now is this scan's (one thing at a time)
    if (item.kind === "plan" && live && live.status === "running" && live.total > 0) {
      return { frac: clamp((live.done || 0) / live.total), words: (live.done || 0) + "/" + live.total };
    }
    return null;
  }
  function stateLine(s) {
    const on = alarmsOn(s);
    if (on.length) return { tone: "bad", words: on.length + (on.length === 1 ? " alarm: " : " alarms: ") + on[0].text };
    const pending = (s.pending || []).length;
    if (pending) return { tone: "warn", words: pending === 1 ? "Approval waiting" : pending + " approvals waiting", open: true };
    const n = sequence(s);
    if (n.q.state === "running" || n.q.state === "pausing") {
      const it = n.item || (n.next >= 0 ? n.items[n.next] : null);
      return { tone: n.q.state === "running" ? "ok" : "warn",
               words: it ? it.name + " · " + n.at + " of " + n.total : n.q.state === "running" ? "Running" : "Pausing" };
    }
    if (n.q.state === "paused") return { tone: "warn", words: "Paused" };
    const live = s.live;
    if (live && live.status === "running") {
      return { tone: "ok", words: (live.plan || "Run") + (live.total ? " · " + (live.done || 0) + "/" + live.total : "") };
    }
    return { tone: "", words: "Ready" };
  }
  function renderState(s) {
    const p = H.parts, line = stateLine(s);
    cls(p.state, "home-state" + (line.tone ? " " + line.tone : ""));
    put(p.say, line.words);
    p.open.hidden = !line.open;
    put(p.lab, s.lab || "");
    p.lab.hidden = !s.lab;
  }
  function openApproval() {   // shown in a conversation: back to it; otherwise the Lab panel asks it
    const s = K.S.last || {}, cards = K.S.approvals;
    const here = (s.pending || []).some((r) => {
      const c = cards && typeof cards.get === "function" ? cards.get(r.id) : null;
      return !!(c && c.el && c.el.isConnected);
    });
    if (here) K.setMode("chat"); else goTo("lab");
  }

  // ------------------------------------------------------------------ the tiles
  function renderSeq(s) {
    const n = sequence(s), t = H.parts.seq, st = n.q.state;
    // states are tinted pills; the running one the accent's soft fill (brief 1.6.3)
    const [kind, words] = st === "running" ? ["blue", "Running"] : st === "pausing" ? ["warn", "Pausing"]
      : st === "paused" ? ["warn", "Paused"] : !n.total || n.next >= 0 ? ["line", "Idle"]
      : n.items.some((i) => i.status === "failed") ? ["bad", "Failed"] : ["ok", "Done"];
    chip(t, kind, words);
    put(t.kpi, n.at + " / " + n.total);
    const frac = n.total ? clamp((n.ended + (n.prog ? n.prog.frac : 0)) / n.total) : 0;
    const width = (frac * 100).toFixed(1) + "%";
    if (H.parts.seqFill.style.width !== width) H.parts.seqFill.style.width = width;
    let name, more = "";
    if (n.item) { name = "→ " + n.item.name; more = n.prog ? n.prog.words : ""; }
    else if (n.next >= 0) name = "Next: " + n.items[n.next].name;
    else if (n.total) name = n.q.last && n.q.last.ended ? "Ended " + hhmm(n.q.last.ended) : "Nothing waiting";
    else name = "Empty";
    put(H.parts.seqName, name);
    put(H.parts.seqNum, more);
    H.parts.seqNum.hidden = !more;
    t.el.classList.toggle("live", st === "running" || st === "pausing");
    const title = n.q.name ? String(n.q.name) : "";
    if (t.go.title !== title) t.go.title = title;
  }
  function renderRuns(s) {
    const t = H.parts.runs, runs = H.runs;
    const live = s && s.live && s.live.status === "running" ? s.live : null;
    chip(t, "blue", live ? "Live" : "");
    if (!runs) return;
    const today = new Date().toDateString();
    let n = 0;
    for (const r of runs) if (r.started_at && new Date(r.started_at * 1000).toDateString() === today) n++;
    put(t.kpi, n >= RUNS_ASKED ? RUNS_ASKED + "+" : String(n));
    put(t.unit, "today");
    const latest = runs.slice(0, 3);
    const sig = JSON.stringify(latest.map((r) => [r.run_id, r.status, r.plan]));
    if (sig === H.runsSig) return;
    H.runsSig = sig;
    const h = K.h;
    H.parts.runList.replaceChildren(...(latest.length ? latest.map((r, i) => {
      const st = r.status === "done" ? "ok" : r.status === "running" ? "run" : /fail|error/.test(r.status || "") ? "bad" : "warn";
      const b = h("button", { class: "home-run", type: "button", title: r.run_id + (r.status ? " · " + r.status : "") },
        h("span", { class: "home-st " + st, "aria-hidden": "true" }), h("span", { class: "home-run-name" }, r.plan || r.run_id),
        h("span", { class: "home-run-time" }, runTime(r)));
      b.addEventListener("click", safe((e) => { e.stopPropagation(); K.openRun(r.run_id, r.plan); }));
      return h("li", { class: i === 2 ? "k-minor" : null }, b);   // the third one: a minor line (the tight tier leaves it out)
    }) : [h("li", { class: "home-none" }, "No runs")]));
  }
  function runTime(r) {   // 14:22 today, else 30 Oct
    if (!r.started_at) return "";
    const t = new Date(r.started_at * 1000);
    return t.toDateString() === new Date().toDateString() ? t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : t.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }
  function renderLab(s) {
    const t = H.parts.labt, inst = Array.isArray(s.instruments) ? s.instruments : [];
    put(t.kpi, inst.filter((i) => i && i.enabled).length + " / " + inst.length);
    put(t.unit, "instruments");
    const on = alarmsOn(s);
    chip(t, on.length ? "bad" : "ok", on.length ? on.length + (on.length === 1 ? " alarm" : " alarms") : "No alarms");
    const watched = watchedReadings(s).slice(0, 3);
    const sig = JSON.stringify(watched.map((w) => [w.key, w.rules]));
    if (sig !== H.watchSig) {   // the rows again only when the watched readings change; their values in place
      H.watchSig = sig;
      H.watchRows = new Map();
      const rows = watched.map((w, i) => {   // the second and third: minor lines (the tight tier keeps the first and its line)
        const value = K.h("span", { class: "home-watch-val" });
        const row = K.h("li", { class: "home-watch" + (i ? " k-minor" : ""), title: w.rules.join("; ") || w.key },
          K.h("span", { class: "home-watch-key" }, w.key), value);
        H.watchRows.set(w.key, { row, value });
        return row;
      });
      if (rows.length) rows.splice(1, 0, H.parts.sparkRow);   // the line under the first one
      H.parts.watchList.replaceChildren(...rows);
    }
    for (const w of watched) {
      const r = H.watchRows.get(w.key);
      if (!r) continue;
      put(r.value, reading(s, w.key) || "—");
      r.row.classList.toggle("alarm", w.alarm);
    }
    drawSpark(watched[0]);
  }
  function watchedReadings(s) {   // each watched reading once (a reading may have several watches): in alarm if any is
    const seen = new Map();
    for (const w of Array.isArray(s && s.watches) ? s.watches : []) {
      if (!w || !w.key) continue;
      const r = seen.get(w.key) || { key: w.key, alarm: false, rules: [] };
      r.alarm = r.alarm || !!w.alarm;
      if (w.rule) r.rules.push(w.rule);
      seen.set(w.key, r);
    }
    return [...seen.values()];
  }

  // ------------------------------------------------------------------ the line of the first watched reading
  function sample(s) {   // every state, Home shown or not: its value now, kept for ten minutes
    const sp = H.spark, first = watchedReadings(s)[0];
    if (!first) { sp.key = null; sp.pts = []; return; }
    if (first.key !== sp.key) { sp.key = first.key; sp.unit = null; sp.pts = []; }
    const v = measure(reading(s, first.key));
    if (!v) return;   // switched off, or not a number now: the line waits
    if (v.unit !== sp.unit) { sp.unit = v.unit; sp.pts = []; }   // another unit: another line
    const now = Date.now() / 1000;
    sp.pts.push([now, v.x]);
    while (sp.pts.length && (sp.pts[0][0] < now - SPAN_S || sp.pts.length > 1000)) sp.pts.shift();
  }
  function drawSpark(first) {
    const row = H.parts.sparkRow, sp = H.spark, pts = sp.pts;
    const on = !!first && first.key === sp.key && pts.length > 2;
    row.hidden = !on;
    if (!on) return;
    row.classList.toggle("alarm", !!first.alarm);
    const t1 = pts[pts.length - 1][0], span = Math.max(t1 - pts[0][0], 1e-3);
    let lo = Infinity, hi = -Infinity;
    for (const p of pts) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
    const pad = 4, xy = pts.map(([t, x]) => [(SW - (t1 - t) / span * SW).toFixed(1),
      (hi > lo ? pad + (hi - x) / (hi - lo) * (SH - 2 * pad) : SH / 2).toFixed(1)]);   // a steady reading: a level line
    const line = xy.map((p) => p[0] + "," + p[1]).join(" ");
    const area = "M" + xy[0][0] + "," + SH + " L" + line.replace(/ /g, " L") + " L" + xy[xy.length - 1][0] + "," + SH + " Z";
    if (H.parts.spark.line.getAttribute("points") !== line) {
      H.parts.spark.line.setAttribute("points", line);
      H.parts.spark.fill.setAttribute("d", area);
    }
  }

  // ------------------------------------------------------------------ Kelvin's blocks
  function blockNode(b) {
    const h = K.h, kind = KINDS[b.kind] || KINDS.note;
    const pin = h("button", { class: "hk-btn hk-pin", type: "button", "data-f": "pin", title: b.pinned ? "Unpin" : "Pin",
                              "aria-label": b.pinned ? "Unpin" : "Pin", "aria-pressed": b.pinned ? "true" : "false" }, svg(PIN));
    pin.addEventListener("click", safe(() => pinBlock(b, pin)));
    const del = h("button", { class: "hk-btn hk-del", type: "button", "data-f": "del", title: "Remove", "aria-label": "Remove" },
      K.icon("x"));
    del.addEventListener("click", safe(() => removeBlock(b, del)));
    const t = b.updated || b.created || 0;   // how long ago: after the title, a minor line (the tight tier leaves it out)
    const when = h("time", { class: "hk-ago k-minor", "data-t": String(t), datetime: t ? new Date(t * 1000).toISOString() : null,
      title: t ? new Date(t * 1000).toLocaleString() : null }, ago(t));
    const el = h("article", { class: "kcard home-block " + kind.cls + (b.pinned ? " pinned" : ""), "data-id": b.id },
      h("div", { class: "hk-head" }, h("span", { class: "hk-kind", title: kind.name }, svg(kind.icon)),
        h("div", { class: "hk-name" }, h("h2", { class: "hk-title" }, b.title || ""), " ", when),
        h("span", { class: "hk-tools" }, pin, del)));
    const tone = ["ok", "warn", "bad"].includes(b.tone) ? " " + b.tone : "";
    if (b.kind === "result" && b.value != null && b.value !== "") {
      el.append(h("div", { class: "hk-value" + tone }, num(b.value)
        + (b.error != null && b.error !== "" ? " ± " + num(b.error) : "") + (b.unit ? " " + b.unit : "")));
    }
    if (b.kind === "watch") el.append(h("div", { class: "hk-value hk-live" + tone, "data-watch": b.watch || "", title: b.watch || null }, "—"));
    if (b.text) {   // md escapes; links only http(s). Cut to its lines (home.css): the whole is its tip then
      const text = h("div", { class: "hk-text", html: K.md(b.text) });
      text.addEventListener("mouseenter", () => {
        const cut = text.scrollHeight > text.clientHeight + 1;
        if (cut ? text.title !== text.textContent : text.title) text.title = cut ? text.textContent : "";
      });
      el.append(text);
    }
    if (b.kind === "check" && Array.isArray(b.items) && b.items.length) el.append(checklist(b));
    const acts = actions(b);
    if (acts) el.append(acts);
    return el;
  }
  function checklist(b) {
    const h = K.h;
    return h("ul", { class: "hk-check" }, ...b.items.map((it, i) => {
      const box = h("input", { type: "checkbox", "data-f": "tick-" + i });
      box.checked = !!it.done;
      box.addEventListener("change", safe(() => tick(b, i, box)));
      return h("li", { class: it.done ? "done" : null }, h("label", {}, box, h("span", {}, it.text)));
    }));
  }
  function actions(b) {   // its buttons, the first one primary; a result's run after them (a button too: brief 1.6.2)
    const list = (Array.isArray(b.actions) ? b.actions : []).slice();
    const runOwn = b.run_id && !list.some((a) => a && a.do === "run" && a.run_id === b.run_id);
    if (!list.length && !runOwn) return null;
    const h = K.h;
    const buttons = list.map((a, i) => {
      const btn = h("button", { class: "btn small" + (i === 0 ? " primary" : ""), type: "button", "data-f": "act-" + i }, a.label || "Open");
      btn.addEventListener("click", safe(() => act(a, btn, b)));
      return btn;
    });
    if (runOwn) {
      const btn = h("button", { class: "btn small", type: "button", "data-f": "run" }, "Open run");
      btn.addEventListener("click", safe(() => K.openRun(b.run_id, planOf(b.run_id))));
      buttons.push(btn);
    }
    return h("div", { class: "hk-acts" }, ...buttons);
  }
  async function act(a, btn, b) {   // a click is the person's: nothing a block offers runs by itself
    try {
      if (a.do === "queue") {
        if (btn.dataset.busy) return;   // one click, one item in the sequence
        btn.dataset.busy = "1";
        try {
          // from_block: the chat that put the block here hears its suggestion was taken (notices.py)
          await K.api("/api/queue", { body: { op: "add", text: a.text, from_block: b && b.id } });
          K.toast("Added");
          goTo("seq");
        } finally { delete btn.dataset.busy; }
      } else if (a.do === "ask") K.ask(a.text || "");
      else if (a.do === "run") K.openRun(a.run_id, planOf(a.run_id));
      else if (a.do === "pane") {   // a link to its place, as a tile is
        const name = { sequence: "seq", runs: "runs", lab: "lab", analysis: "analysis" }[a.pane];
        if (name) goTo(name);
      }
    } catch (e) { K.toast(e.message); }
  }
  async function change(id, opts) {   // a person's change to a block; Home as it is then, drawn at once
    const asked = ++H.blocksAsked;   // an older load still on its way is not drawn over it
    const v = await K.api("/api/home/blocks/" + encodeURIComponent(id), opts);
    if (asked === H.blocksAsked) guard(() => renderBlocks(v.blocks || [], false));
  }
  // a button at work is marked busy, not disabled: a disabled button drops the focus the keyboard had on it
  async function pinBlock(b, btn) {
    if (btn.dataset.busy) return;
    btn.dataset.busy = "1";
    try { await change(b.id, { body: { pin: !b.pinned } }); } catch (e) { K.toast(e.message); }
    delete btn.dataset.busy;
  }
  async function removeBlock(b, btn) {   // asked inline: the × becomes "Remove?" for 3 s
    if (btn.dataset.busy) return;
    const back = () => {
      btn.classList.remove("confirm");
      btn.replaceChildren(K.icon("x"));
      btn.setAttribute("aria-label", "Remove");
    };
    if (!btn.classList.contains("confirm")) {
      btn.classList.add("confirm");
      btn.replaceChildren("Remove?");
      btn.setAttribute("aria-label", "Remove?");
      btn.timer = setTimeout(back, 3000);
      return;
    }
    clearTimeout(btn.timer);
    btn.dataset.busy = "1";
    const box = H.parts.blocks, at = [...box.children].findIndex((c) => c.dataset.id === b.id);
    const keyed = document.activeElement === btn;   // the keyboard was on it: it goes on to the block now in its place
    try { await change(b.id, { method: "DELETE" }); } catch (e) { back(); K.toast(e.message); }
    delete btn.dataset.busy;
    const a = document.activeElement;
    if (keyed && at >= 0 && (!a || a === document.body)) {
      const next = box.children[Math.min(at, box.children.length - 1)];
      const pin = next && next.querySelector('[data-f="pin"]');
      if (pin) pin.focus({ preventScroll: true });
    }
  }
  async function tick(b, i, box) {
    const done = box.checked;
    try { await change(b.id, { body: { tick: i, done } }); } catch (e) { box.checked = !done; K.toast(e.message); }
  }
  function focusKey() {   // what in the blocks has the focus, to find it again after they are drawn
    const a = document.activeElement, box = H.parts.blocks;
    if (!a || !box.contains(a) || !a.dataset || !a.dataset.f) return null;
    const card = a.closest("[data-id]");
    return card ? [card.dataset.id, a.dataset.f] : null;
  }
  function refocus(key) {
    if (!key) return;
    const a = document.activeElement;
    if (a && a !== document.body && a.isConnected) return;   // the person is elsewhere now, or it is still there
    const card = [...H.parts.blocks.children].find((c) => c.dataset.id === key[0]);
    const el = card && [...card.querySelectorAll("[data-f]")].find((x) => x.dataset.f === key[1]);
    if (el) el.focus({ preventScroll: true });
  }
  function renderBlocks(blocks, ring) {   // keyed by id: a block is drawn again only when it changed
    const box = H.parts.blocks, keep = focusKey(), before = H.known, known = new Map(), want = [];
    for (const b of blocks) {
      if (!b || typeof b.id !== "string") continue;
      const sig = JSON.stringify(b);
      let n = H.nodes.get(b.id);
      if (!n || n.sig !== sig) {
        const el = guard(() => blockNode(b));
        if (!el) continue;
        if (n && n.el.parentNode === box) box.replaceChild(el, n.el);
        n = { el, sig };
        H.nodes.set(b.id, n);
      }
      const stamp = b.updated || b.created || 0;
      known.set(b.id, stamp);
      want.push(n.el);
      if (ring && before && before.get(b.id) !== stamp) flash(n.el);   // it arrived (or Kelvin changed it) just now
    }
    for (const [id, n] of H.nodes) if (!known.has(id)) { n.el.remove(); H.nodes.delete(id); }
    want.forEach((el, i) => { if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null); });
    box.hidden = !want.length;
    H.known = known;
    guard(() => liveValues(K.S.last));
    refocus(keep);
  }
  function flash(el) {
    el.classList.add("fresh");
    setTimeout(() => el.classList.remove("fresh"), 2000);
  }
  function liveValues(s) {   // every tick: a watch block's reading, and how long ago each block came
    const box = H.parts.blocks;
    if (box.hidden) return;
    if (s) {
      const alarmed = new Set(watchedReadings(s).filter((w) => w.alarm).map((w) => w.key));
      for (const el of box.querySelectorAll(".hk-live")) {
        const key = el.dataset.watch;
        put(el, reading(s, key) || "—");
        el.classList.toggle("alarm", alarmed.has(key));
      }
    }
    for (const el of box.querySelectorAll(".hk-ago")) put(el, ago(Number(el.dataset.t)));
  }

  // ------------------------------------------------------------------ asking the server, when something moved
  async function loadBlocks(ring) {
    const asked = ++H.blocksAsked;
    let v;
    try { v = await K.api("/api/home"); } catch (e) { return; }   // the next stamp, or showing Home, asks again
    if (asked !== H.blocksAsked) return;
    guard(() => renderBlocks(Array.isArray(v.blocks) ? v.blocks : [], ring));
  }
  async function loadRuns() {
    const asked = ++H.runsAsked;
    let v;
    try { v = await K.api("/api/runs?limit=" + RUNS_ASKED); } catch (e) { return; }
    if (asked !== H.runsAsked) return;
    H.runs = Array.isArray(v.runs) ? v.runs : [];
    guard(() => renderRuns(K.S.last || {}));
  }

  function paint(s) {   // everything on Home from one state: cheap, and only what changed is touched
    guard(() => put(H.parts.greet, greeting(new Date())));
    if (!s) return;
    guard(() => renderState(s));
    guard(() => renderSeq(s));
    guard(() => renderRuns(s));
    guard(() => renderLab(s));
    guard(() => liveValues(s));
  }
  function onShow() {
    paint(K.S.last);
    loadBlocks(false);
    loadRuns();
  }
  function onState(s) {
    if (!s || typeof s !== "object") return;
    guard(() => sample(s));   // the Lab tile's line keeps its last ten minutes while Home is out of sight
    const visible = shown();
    // out of sight, nothing is asked: showing Home loads both
    if (s.home !== H.stamp) {   // Kelvin (or a person, elsewhere) changed the blocks
      const first = H.stamp === undefined;   // the first stamp: a change just before it may have missed the first load
      H.stamp = s.home;
      if (visible) loadBlocks(!first);
    }
    const run = s.live && s.live.status === "running" ? String(s.live.run_id || "") : "";
    if (run !== H.runsKey) {   // a run started or ended: the runs again
      const first = H.runsKey === undefined;
      H.runsKey = run;
      if (visible && (!first || run)) loadRuns();
    }
    if (visible) paint(s);
  }
  function onMode(mode) {
    if (mode !== "home") return;
    if (K.$("app").classList.contains("full")) K.exitFull();   // a panel in full screen: Home asked for, Home shown
    onShow();
  }

  function start() {
    if (H.parts || !window.Kelvin) return;
    K = window.Kelvin;
    if (!guard(() => { build(); return true; })) return;
    document.addEventListener("kelvin:state", (e) => guard(() => onState(e.detail)));
    document.addEventListener("kelvin:mode", (e) => guard(() => onMode(e.detail && e.detail.mode)));
    if (shown()) onShow();
  }
  // the page lends its parts once it is ready; this file may also arrive after that (the event is then gone)
  if (window.Kelvin && window.Kelvin.S && window.Kelvin.S.settings) start();
  else document.addEventListener("kelvin:ready", () => guard(start), { once: true });
})();
