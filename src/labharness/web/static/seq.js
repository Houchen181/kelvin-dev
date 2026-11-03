"use strict";
// The Sequence: Kelvin's queue drawn as blocks (seq.css), the Runs pane's first view (#seq-view). It hears the page
// through kelvin:* events (ready, state, seq, queue) and draws in #seq-body; every change goes through POST /api/queue.
// Its functions are the person's library and the lab's experiments (/api/library, /api/experiments): a click appends
// one, a drag puts it where it is dropped; and a step typed in the step row, or any item pasted as written (app.js
// keeps both: index.html's #seq-step, put here with the functions). The page lends what it uses on window.Kelvin.
(() => {
  let K = null;   // window.Kelvin, once the page is ready
  const SEQ = {
    q: null,                  // the queue as the server last said (GET /api/queue, state.queue, an op's answer)
    live: null,               // state.live: the run in progress
    sig: "",                  // changed|state|reason|name of the queue last drawn
    open: false, wide: false, adding: false,
    lib: { scripts: [], scans: [] }, exps: [], libAt: 0,
    filter: "",
    edit: new Set(),          // the blocks whose editor is open
    confirm: new Map(),       // block id: its timer, while × asks "Remove?"
    fnConfirm: null,          // a function's key while its ⋯ asks "Remove?"
    renaming: null,           // a function's key while its name is a field
    fresh: null,              // a function just added: ringed for 2 s
    cards: new Map(),         // block id: {el, sig}
    seen: new Map(),          // block id: {eta, at}: a scan's time left, counted down between ticks
    drag: null, dragged: false,
    pending: false, fnsPending: false,
    starting: false, msgTimer: 0, finished: "", el: {},
  };
  const FINISHED = new Set(["done", "failed", "stopped", "skipped"]);
  const CHIP = { running: ["Running", "run"], done: ["Done", "ok"], failed: ["Failed", "bad"], stopped: ["Stopped", "warn"],
                 skipped: ["Skipped", "line"] };
  const SWATCHES = [["Blue", "#2a78d6"], ["Green", "#13996b"], ["Violet", "#7155d8"], ["Amber", "#c98500"],
                    ["Rose", "#d6457a"], ["Grey", "#7d8a99"]];
  const MAX_COPIES = 400;
  const SVGNS = "http://www.w3.org/2000/svg";
  const ICO = {
    grip: '<circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/>' +
          '<circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/>',
    play: '<path d="M7.5 5.3v13.4a.7.7 0 0 0 1.05.6l10.7-6.7a.7.7 0 0 0 0-1.2L8.55 4.7a.7.7 0 0 0-1.05.6z"/>',
    pause: '<rect x="6.5" y="5" width="4" height="14" rx="1.2"/><rect x="13.5" y="5" width="4" height="14" rx="1.2"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    again: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3L4.5 9"/><path d="M4.5 4.5V9H9"/>',
    skip: '<path d="M6 6.5l8.5 5.5L6 17.5z"/><path d="M18 6v12"/>',
    hold: '<path d="M9.5 7v10M14.5 7v10"/>',
    run: '<path d="M4 19V5M4 19h16"/><path d="M7 15l4-5 3 3 5-7"/>',
  };

  // ------------------------------------------------------------------ small helpers
  function report(e) { try { console.error("seq.js:", e); } catch (x) { /* nothing to say it with */ } }
  function guard(fn, ...args) { try { return fn(...args); } catch (e) { report(e); return undefined; } }
  function ico(name, solid) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    if (solid) s.setAttribute("class", "seq-solid");
    s.innerHTML = ICO[name] || "";
    return s;
  }
  const pad = (n) => (n < 10 ? "0" : "") + n;
  function clock(s) {   // 75 → 1:15, 3725 → 1:02:05
    s = Math.max(0, Math.round(Number(s) || 0));
    const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
    return hh ? hh + ":" + pad(mm) + ":" + pad(ss) : mm + ":" + pad(ss);
  }
  function span(s) {   // a duration in words: 45 s, 10 min, 2 h
    s = Number(s) || 0;
    if (s < 90) return num(s) + " s";
    if (s < 5400) return num(s / 60) + " min";
    return num(s / 3600) + " h";
  }
  const num = (v) => (typeof v === "number" && isFinite(v) ? String(Number(v.toPrecision(6))) : String(v));
  const firstLine = (s) => String(s || "").split("\n")[0];
  const fieldIn = (root) => {
    const a = document.activeElement;
    return !!(a && root && root.contains(a) && (a.matches("input, select, textarea") || a.isContentEditable));
  };
  // the page's size (a CSS zoom on the page): a length from the pointer or a rect, written into the page, is divided by
  // it (rects and the pointer are the window's pixels, the page's lengths its own)
  const zoom = () => (K && typeof K.zoom === "function" && Number(K.zoom())) || 1;
  const items = () => (SEQ.q && SEQ.q.items) || [];
  const running = () => !!SEQ.q && (SEQ.q.state === "running" || SEQ.q.state === "pausing");
  const base = (p) => String(p || "").split(/[\\/]/).pop();
  const normPath = (p) => String(p || "").trim().replace(/\\/g, "/").toLowerCase();
  const isExpName = (s) => /^[a-z0-9][a-z0-9_-]{0,63}$/.test(String(s || ""));

  // ------------------------------------------------------------------ reading an item's YAML (what it shows)
  // A queue item's text is YAML: top-level keys, values in flow form ({a: 1}, [1, 2]) or one level of block mapping.
  // This reads that much (never the whole of YAML); a text it can't read gives {}.
  function scalar(raw, quoted) {
    if (quoted) return raw;
    const t = raw.trim();
    if (/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(t)) return Number(t);
    if (/^(true|false)$/i.test(t)) return /^true$/i.test(t);
    if (/^(null|~)$/i.test(t) || t === "") return null;
    return t;
  }
  function flow(s) {
    let i = 0;
    const ws = () => { while (i < s.length && /\s/.test(s[i])) i++; };
    function token(stops) {
      ws();
      const q = s[i];
      if (q === '"' || q === "'") {
        let out = "";
        i++;
        while (i < s.length) {
          if (s[i] === q) {
            if (q === "'" && s[i + 1] === "'") { out += "'"; i += 2; continue; }
            i++;
            break;
          }
          if (q === '"' && s[i] === "\\" && i + 1 < s.length) { out += s[i + 1]; i += 2; continue; }
          out += s[i++];
        }
        return { v: out, quoted: true };
      }
      let j = i;
      while (j < s.length && !stops.includes(s[j])) j++;
      const v = s.slice(i, j);
      i = j;
      return { v, quoted: false };
    }
    function value(stops) {
      ws();
      if (s[i] === "{") {
        i++;
        const o = {};
        for (;;) {
          ws();
          if (s[i] === "}") { i++; return o; }
          const k = token(":,}");
          if (s[i] !== ":") throw new Error("flow mapping");
          i++;
          o[k.v.trim()] = value(",}");
          ws();
          if (s[i] === ",") { i++; continue; }
          if (s[i] === "}") { i++; return o; }
          throw new Error("flow mapping");
        }
      }
      if (s[i] === "[") {
        i++;
        const a = [];
        for (;;) {
          ws();
          if (s[i] === "]") { i++; return a; }
          a.push(value(",]"));
          ws();
          if (s[i] === ",") { i++; continue; }
          if (s[i] === "]") { i++; return a; }
          throw new Error("flow list");
        }
      }
      const t = token(stops);
      return scalar(t.v, t.quoted);
    }
    const out = value("");
    ws();
    return out;
  }
  function stripComment(line) {
    let q = null;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (q) { if (c === q) q = null; continue; }
      if (c === '"' || c === "'") { if (i === 0 || /[\s:{[,]/.test(line[i - 1])) q = c; continue; }
      if (c === "#" && (i === 0 || /\s/.test(line[i - 1]))) return line.slice(0, i);
    }
    return line;
  }
  function valueOf(rest) {
    const t = rest.trim();
    if (!t) return null;
    if (t[0] === "{" || t[0] === "[" || t[0] === '"' || t[0] === "'") { try { return flow(t); } catch (e) { return t; } }
    return scalar(t);
  }
  function readYaml(text) {
    const src = String(text || "").trim();
    if (src[0] === "{") {
      try { return JSON.parse(src); } catch (e) { /* YAML's flow form */ }
      try { const v = flow(src); return v && typeof v === "object" ? v : {}; } catch (e) { return {}; }
    }
    const out = {};
    let block = null;
    for (const raw of src.split(/\r?\n/)) {
      const line = stripComment(raw);
      if (!line.trim()) continue;
      const indent = line.length - line.trimStart().length;
      const t = line.trim();
      if (indent > 0 && block) {
        if (t.startsWith("- ")) {
          if (!Array.isArray(out[block])) out[block] = [];
          out[block].push(valueOf(t.slice(2)));
          continue;
        }
        const m = t.match(/^("[^"]*"|'[^']*'|[^:]+?):(?:\s+(.*))?$/);
        if (m) {
          if (!out[block] || typeof out[block] !== "object" || Array.isArray(out[block])) out[block] = {};
          out[block][m[1].replace(/^["']|["']$/g, "")] = valueOf(m[2] || "");
        }
        continue;
      }
      const m = t.match(/^([^\s:][^:]*?):(?:\s+(.*))?$/);
      if (!m) { block = null; continue; }
      const rest = (m[2] || "").trim();
      out[m[1]] = rest ? valueOf(rest) : null;
      block = rest ? null : m[1];
    }
    return out;
  }
  const PARSED = new Map();   // text: what it says, read once
  function parsed(it) {
    const text = String(it.text || "");
    let d = PARSED.get(text);
    if (!d) {
      d = guard(readYaml, text) || {};
      if (PARSED.size > 300) PARSED.clear();
      PARSED.set(text, d);
    }
    return d;
  }
  const waitOnly = (d) => !!Number(d.wait) && !d.set && !d.stabilize;

  // ------------------------------------------------------------------ what a block is: its look and its line
  function libById(id) {
    if (!id) return null;
    const lib = SEQ.lib || {};
    return (lib.scripts || []).find((e) => e.id === id) || (lib.scans || []).find((e) => e.id === id) || null;
  }
  function scriptEntry(path) {
    const scripts = (SEQ.lib && SEQ.lib.scripts) || [];
    if (!path) return null;
    const p = normPath(path);
    const same = scripts.find((e) => normPath(e.path) === p);
    if (same) return same;
    if (/[\\/]/.test(path)) return null;
    const named = scripts.filter((e) => base(e.path).toLowerCase() === p);
    return named.length === 1 ? named[0] : null;
  }
  function lookOf(it) {
    const d = parsed(it);
    let entry = null, kind = "k-scan";
    if (it.kind === "plan") {   // its library entry; else the scan of that name used last (the library keeps a scan
      // as Kelvin writes it out, so a plan typed in another form is found by its name)
      const t = String(it.text || "").trim(), scans = (SEQ.lib && SEQ.lib.scans) || [];
      entry = libById(it.library) || scans.find((e) => String(e.text || "").trim() === t) ||
        scans.find((e) => e.name === it.name) || null;
    } else if (it.kind === "step") {
      kind = waitOnly(d) ? "k-wait" : "k-step";
    } else {
      const path = typeof d.script === "string" ? d.script : "";
      kind = isExpName(path) ? "k-exp" : "k-script";
      entry = kind === "k-script" ? libById(it.library) || scriptEntry(path) : null;
    }
    // the server names and colours a block after its library entry (queue_view); the library read here is the
    // fallback, for an older Kelvin
    const name = it.name || (entry && entry.name && !d.version ? entry.name : "");
    const color = /^#[0-9a-f]{6}$/i.test(String(it.color || "")) ? it.color : entry && entry.color ? entry.color : "";
    return { name, kind, color, entry, d };
  }
  const summaryOf = (it, look) => (typeof it.summary === "string" ? it.summary : guessSummary(it, look));
  function guessSummary(it, look) {   // until the server says it (item.summary): a few words from the text
    const d = look.d;
    if (it.kind === "plan") {
      const parts = [];
      let pts = 1;
      for (const [k, v] of Object.entries(d.sweep && typeof d.sweep === "object" ? d.sweep : {})) {
        if (v && typeof v === "object" && "from" in v && "to" in v) {
          parts.push(k + " " + num(v.from) + " → " + num(v.to));
          const n = v.points ? Number(v.points) : v.step ? Math.floor(Math.abs((v.to - v.from) / v.step) + 1e-9) + 1 : 0;
          pts = n ? pts * n : 0;
        } else if (v && Array.isArray(v.values)) {
          parts.push(k + " ×" + v.values.length);
          pts *= v.values.length;
        }
      }
      return parts.join(" · ") + (parts.length && pts > 1 ? " · " + pts + " points" : "");
    }
    if (it.kind === "step") {
      const parts = [];
      for (const [k, v] of Object.entries(d.set && typeof d.set === "object" ? d.set : {})) parts.push(k + " → " + num(v));
      if (d.stabilize && typeof d.stabilize === "object" && d.stabilize.read) parts.push("settle " + d.stabilize.read);
      if (Number(d.wait)) parts.push((parts.length ? "wait " : "") + span(Number(d.wait)));   // as it is now, whatever its name says
      return parts.join(" · ");
    }
    const params = d.params && typeof d.params === "object" ? d.params : {};
    const parts = Object.entries(params).map(([k, v]) => k + " = " + (typeof v === "object" ? JSON.stringify(v) : num(v)));
    if (look.name !== base(d.script)) parts.unshift(base(d.script));
    if (d.action) parts.unshift(String(d.action));
    return parts.join(" · ");
  }

  // ------------------------------------------------------------------ the server
  function accept(q) {   // a fresh view of the queue (an op's answer): drawn now
    if (!q || !Array.isArray(q.items)) return;
    SEQ.q = q;
    badge(q);
    if (SEQ.open) render();
  }
  async function op(body) {
    try {
      const r = await K.api("/api/queue", { body });
      accept(r);
      return r;
    } catch (e) {
      K.toast(e.message);
      if (SEQ.open) render();   // what the person changed in place goes back to what is so
      return null;
    }
  }
  async function viaQueue(call) {   // a library or experiment run that appends: {queue, message}
    try {
      const r = await call();
      if (r && r.queue) accept(r.queue);
      else K.pollState();
      return r;
    } catch (e) {
      K.toast(e.message);
      return null;
    }
  }
  async function loadQueue() {
    try { accept(await K.api("/api/queue")); } catch (e) { /* the next state tick brings it */ }
  }
  async function loadLib() {
    SEQ.libAt = Date.now();
    const [lib, exps] = await Promise.all([
      K.api("/api/library").catch(() => null),
      K.api("/api/experiments").catch(() => null),
    ]);
    if (lib && Array.isArray(lib.scripts)) SEQ.lib = lib;
    SEQ.exps = exps && Array.isArray(exps.experiments) ? exps.experiments : [];
    renderFns();
    if (SEQ.open) redraw();
  }
  async function libOp(body) {
    try {
      const r = await K.api("/api/library", { body });
      if (r && r.library && Array.isArray(r.library.scripts)) SEQ.lib = r.library;
      renderFns();
      if (SEQ.open) { redraw(); loadQueue(); }   // the blocks are named and coloured after their entries
      return r;
    } catch (e) {
      K.toast(e.message);
      return null;
    }
  }

  // ------------------------------------------------------------------ the pill's badge, and the state's ticks
  function badge(q) {
    const b = K.$("seq-badge"), pill = K.$("toggle-runs");
    if (!b || !pill || !q) return;
    const n = (q.waiting || 0) + (q.running ? 1 : 0);
    if (b.textContent !== String(n)) b.textContent = String(n);
    b.hidden = !n;
    pill.classList.toggle("live", q.state === "running" || q.state === "pausing");
  }
  const sigOf = (q) => [q.changed, q.state, q.reason, q.name].join("|");
  function onState(s) {
    const q = s && s.queue;
    if (!q || !Array.isArray(q.items)) return;
    SEQ.live = s.live || null;
    if (SEQ.q && typeof q.changed === "number" && typeof SEQ.q.changed === "number" && q.changed < SEQ.q.changed) {
      badge(SEQ.q);   // older than an op's answer already drawn
      return;
    }
    SEQ.q = q;
    badge(q);
    if (!SEQ.open && inSight()) { onSeq({ open: true }); return; }   // its opening was missed
    if (!SEQ.open) return;
    if (!SEQ.el.list || !SEQ.el.list.isConnected) { render(); return; }   // a first drawing that failed: again
    if (sigOf(q) !== SEQ.sig || SEQ.pending) render();
    else tick();
    // a block that ended may have left a scan in the library (each scan run is kept there)
    const ended = items().filter((i) => FINISHED.has(i.status)).map((i) => i.id).join(",");
    if (ended !== SEQ.finished) {
      const more = ended.length > SEQ.finished.length;
      SEQ.finished = ended;
      if (more && Date.now() - SEQ.libAt > 4000) loadLib();
    }
  }
  function onSeq(d) {
    const was = SEQ.open;
    SEQ.open = !!d.open;
    if (SEQ.open && !was) {
      SEQ.drawn = false;   // drawn as it is, without motion
      render();
      loadQueue();
      loadLib();
    }
    if (!SEQ.open) closePop();
    if (d.message) say(d.message);
  }
  // in sight: the Runs pane open on the Sequence
  const inSight = () => !K.$("runpane").hidden && !K.$("seq-view").hidden;
  function say(text) {   // in the state's place in the bar, for a few seconds
    const m = SEQ.el.msg;
    if (!m) return;
    const line = firstLine(text);
    m.textContent = line;
    m.title = String(text || "");
    SEQ.el.head.classList.toggle("saying", !!line);
    clearTimeout(SEQ.msgTimer);
    SEQ.msgTimer = setTimeout(() => { m.textContent = ""; m.title = ""; SEQ.el.head.classList.remove("saying"); }, 6000);
  }

  // ------------------------------------------------------------------ building the pane once
  function init() {
    K = window.Kelvin;
    if (!K || !K.$("seq-body") || !K.$("seq-view") || !K.$("runpane")) return;
    build();
    document.addEventListener("kelvin:state", (e) => guard(onState, e.detail));
    document.addEventListener("kelvin:seq", (e) => guard(onSeq, e.detail || {}));
    document.addEventListener("kelvin:queue", (e) => guard(() => {   // the step row's or a pasted item's answer
      const d = e.detail || {};
      if (d.queue) accept(d.queue);
      if (d.message) say(d.message);
    }));
    // opened before this file started (a click while the page was still loading): drawn now
    if (inSight()) guard(onSeq, { open: true });
    if (window.ResizeObserver) new ResizeObserver(() => guard(fit)).observe(SEQ.el.pane);
    SEQ.el.pane.addEventListener("focusout", () => setTimeout(() => guard(() => {
      if (SEQ.pending && !fieldIn(SEQ.el.list) && !(SEQ.drag && SEQ.drag.active)) render();
      if (SEQ.fnsPending && !fieldIn(SEQ.el.groups)) renderFns();
    }), 0));
    document.addEventListener("click", (e) => {   // the click that ends a drag is not a click
      if (SEQ.dragged) { e.stopPropagation(); e.preventDefault(); }
    }, true);
    document.addEventListener("pointerdown", (e) => { if (SEQ.pop && !SEQ.pop.contains(e.target)) closePop(); }, true);
    setInterval(() => guard(() => { if (SEQ.open && running()) tick(); }), 1000);
    setInterval(() => {   // while it runs and is in sight, the state a little more often (the app polls every 4 s,
      // and every 1.5 s itself while the Lab panel is open or an approval waits)
      const fast = K.S.labOpen || !(K.$("lab-badge") || {}).hidden;
      if (SEQ.open && running() && !fast && document.visibilityState === "visible") K.pollState();
    }, 1500);
    SEQ.open = inSight();
    fit();
    if (K.S && K.S.last) onState(K.S.last);
    if (SEQ.open) { render(); loadQueue(); loadLib(); }
  }
  function build() {
    const h = K.h;
    const name = h("input", { class: "seq-name", type: "text", spellcheck: "false", autocomplete: "off", maxlength: "80",
                              "aria-label": "Sequence name", placeholder: "Sequence" });
    const chip = h("span", { class: "chip line seq-state" }, "Idle");
    const msg = h("span", { class: "seq-msg", role: "status" });
    const head = h("div", { class: "seq-head-main" }, name, chip, msg);
    name.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); guard(renameSeq); name.blur(); }
      else if (e.key === "Escape") { e.preventDefault(); name.value = (SEQ.q && SEQ.q.name) || ""; sizeName(); name.blur(); }
    });
    name.addEventListener("input", sizeName);
    name.addEventListener("blur", () => guard(renameSeq));

    // each button keeps its name as its label and tip: in a narrow pane Pause and Stop show only their icons
    const start = h("button", { class: "btn primary seq-go", type: "button", "aria-label": "Start" }, ico("play", true),
      h("span", {}, "Start"));
    const pause = h("button", { class: "btn seq-hold", type: "button", hidden: true, title: "Pause", "aria-label": "Pause" },
      ico("pause", true), h("span", {}, "Pause"));
    const stop = h("button", { class: "btn seq-stop", type: "button", hidden: true, title: "Stop", "aria-label": "Stop" },
      ico("stop", true), h("span", {}, "Stop"));
    const more = h("button", { class: "icon-btn seq-more", type: "button", title: "More", "aria-label": "More",
                               "aria-haspopup": "menu" }, K.icon("dots"));
    const bar = h("div", { class: "seq-bar" }, head, start, pause, stop, more);
    start.addEventListener("click", () => guard(startSeq));
    pause.addEventListener("click", () => guard(op, { op: "pause" }));
    stop.addEventListener("click", () => guard(stopSeq));
    more.addEventListener("click", (e) => { e.stopPropagation(); guard(moreMenu, more); });

    const search = h("input", { class: "kfield seq-search", type: "search", placeholder: "Search", spellcheck: "false",
                                autocomplete: "off", "aria-label": "Search functions" });
    const groups = h("div", { class: "seq-groups" });
    const newFn = h("button", { class: "btn small primary", type: "button" }, "＋ New function");   // as the portal's map names it
    const newScan = h("button", { class: "btn small", type: "button" }, "New scan");
    // a step typed (Set, Wait, Wait until settled) and any item as written: app.js's step row and paste form. The step
    // is one line (Step, Set | Wait | Settle) until it is used
    const stepRow = K.$("step-row"), paste = K.$("queue-paste"), pasteOpen = K.$("queue-paste-open");
    const stepCard = stepRow ? h("div", { class: "kcard k-step seq-stepcard" },
      h("div", { class: "seq-step-head" }, h("span", { class: "seq-step-name" }, "Step"), stepKinds(),
        stepRow.querySelector(".step-go")), stepRow, K.$("step-msg")) : null;
    const step = stepCard ? h("div", { class: "seq-step-part" }, stepCard) : null;
    const fns = h("section", { id: "seq-fns", class: "seq-fns", "aria-label": "Functions", "data-region": "seq-fns", hidden: true },
      h("div", { class: "seq-find" }, search), groups, step, h("div", { class: "seq-fns-foot" }, newFn, newScan, pasteOpen), paste);
    const holder = K.$("seq-step");
    if (holder && stepRow) holder.remove();   // its parts are the functions' now
    if (stepCard) {
      stepCard.addEventListener("focusin", () => guard(syncStep));
      stepCard.addEventListener("focusout", () => setTimeout(() => guard(syncStep), 0));
      stepCard.addEventListener("input", () => guard(syncStep));
      stepCard.querySelector(".seq-step-head").addEventListener("click", (e) => guard(() => {
        if (stepCard.classList.contains("used") || e.target.closest("button, input, select")) return;
        stepCard.classList.add("used");   // a click on its line opens it, at its first field
        const first = stepRowField();
        if (first) first.focus();
      }));
      const save = K.$("step-save");   // a queued step opened here to edit (Save to #n): its fields show
      if (save && window.MutationObserver) new MutationObserver(() => guard(syncStep)).observe(save, { attributes: true, attributeFilter: ["hidden"] });
    }
    search.addEventListener("input", () => { SEQ.filter = search.value; guard(renderFns); });
    search.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || !search.value) return;
      e.preventDefault();
      search.value = SEQ.filter = "";
      guard(renderFns);
    });
    newFn.addEventListener("click", () => guard(newFunction));
    newScan.addEventListener("click", () => guard(() => K.openEditor()));

    const list = h("ol", { class: "seq-list", "aria-label": "Sequence", "data-region": "seq-blocks" });
    // ＋ Add: in a narrow pane the functions open under the blocks (both in sight, each scrolling on its own), and it
    // closes them again; beside the blocks (wide) they are always there, and it goes to their search
    const add = h("button", { class: "seq-add", type: "button", "aria-expanded": "false", "aria-controls": "seq-fns" }, "＋ Add");
    add.addEventListener("click", () => guard(() => {
      if (SEQ.wide) { search.focus(); return; }
      SEQ.adding = !SEQ.adding;
      showFns();
      if (SEQ.adding) loadLib();
    }));
    const main = h("div", { class: "seq-main" }, list, add);
    const cols = h("div", { class: "seq-cols" }, fns, main);
    const body = K.$("seq-body");
    body.replaceChildren(bar, cols);
    Object.assign(SEQ.el, { pane: K.$("seq-view"), body, name, chip, msg, head, start, pause, stop, more, bar, fns, search, groups,
                            newFn, list, add, main, cols, stepCard });
  }
  // the step's card: open while it has the keyboard, a value typed, or a queued step in it to edit; else one line
  function syncStep() {
    const card = SEQ.el.stepCard, row = K.$("step-row"), save = K.$("step-save");
    if (!card || !row) return;
    const typed = [...row.querySelectorAll("input")].some((x) => x.value.trim());
    const used = card.contains(document.activeElement) || typed || !!(save && !save.hidden);
    card.classList.toggle("used", used);
  }
  // the step row's kind (its own list, kept for app.js) as a switch: Set | Wait | Settle
  function stepKinds() {
    const h = K.h, sel = K.$("step-kind");
    if (!sel) return null;
    const box = h("div", { class: "seg seq-kinds", role: "radiogroup", "aria-label": "Step" });
    const sync = () => {
      for (const b of box.querySelectorAll("button")) {
        const on = b.dataset.v === sel.value;
        b.classList.toggle("on", on);
        b.setAttribute("aria-checked", String(on));
      }
    };
    for (const [v, label, title] of [["set", "Set", "Set a value"], ["wait", "Wait", "Wait a time"],
                                     ["settle", "Settle", "Wait until a reading settles"]]) {
      const b = h("button", { type: "button", role: "radio", title }, label);
      b.dataset.v = v;
      b.addEventListener("click", () => guard(() => {
        const card = box.closest(".seq-stepcard");
        if (card) card.classList.add("used");   // its fields show, to be typed in
        if (sel.value !== v) {
          sel.value = v;
          sel.dispatchEvent(new Event("change"));   // app.js shows that kind's fields
        }
        const first = stepRowField();
        if (first) first.focus();
      }));
      box.append(b);
    }
    sel.addEventListener("change", sync);
    sel.hidden = true;
    sync();
    if (K.thumb) K.thumb(box);
    return box;
  }
  const stepRowField = () => K.$("step-row").querySelector(".step-f:not([hidden]) input");
  function sizeName() {   // a title as wide as its words (field-sizing where the browser has it)
    const n = SEQ.el.name;
    if (window.CSS && CSS.supports && CSS.supports("field-sizing", "content")) return;
    n.size = Math.max(6, Math.min(48, (n.value || n.placeholder).length + 1));
  }
  // from 600 px of pane (in the page's own pixels) the functions are a column of their own beside the blocks; from
  // 1100 (full screen) a wider one, their rows in two columns
  function fit() {
    const w = SEQ.el.pane.getBoundingClientRect().width / zoom();
    if (!w) return;   // out of sight: as it was
    const wide = w >= 600, roomy = w >= 1100;
    if (wide === SEQ.wide && roomy === SEQ.roomy && SEQ.fitted) return;
    SEQ.fitted = true;
    SEQ.wide = wide;
    SEQ.roomy = roomy;
    SEQ.el.pane.classList.toggle("seq-wide", wide);
    SEQ.el.pane.classList.toggle("seq-roomy", roomy);
    showFns();
  }
  function showFns() {
    const on = SEQ.wide || SEQ.adding;
    SEQ.el.fns.hidden = !on;
    SEQ.el.pane.classList.toggle("seq-adding", !SEQ.wide && SEQ.adding);
    SEQ.el.add.setAttribute("aria-expanded", String(!SEQ.wide && SEQ.adding));
    SEQ.el.add.classList.toggle("on", !SEQ.wide && SEQ.adding);
    if (on) renderFns();
  }

  // ------------------------------------------------------------------ drawing
  function render() {
    if (!SEQ.el.list || !SEQ.q) return;
    renderHead();
    renderBar();
    SEQ.sig = sigOf(SEQ.q);
    SEQ.pending = !renderList();
    SEQ.drawn = true;
    tick();
  }
  function redraw() {   // the blocks again (an editor opened, a confirmation's time out, the library's names)
    if (SEQ.q && !renderList()) SEQ.pending = true;
  }
  function stateChip(q) {
    const all = q.items || [];
    if (!all.length && q.state !== "running" && q.state !== "pausing") return ["Idle", "line", ""];   // nothing to resume
    if (q.state === "running") {
      const at = all.findIndex((i) => i.status === "running");
      return [at >= 0 ? "Running " + (at + 1) + " of " + all.length : "Running", "run", ""];
    }
    if (q.state === "pausing") return ["Pausing", "warn", q.reason || ""];
    if (q.state === "paused") return ["Paused", "warn", q.reason || ""];
    if (all.length && !q.waiting && !q.running) return ["Done", "ok", q.reason && q.reason !== "finished" ? q.reason : ""];
    return ["Idle", "line", ""];
  }
  function renderHead() {
    const q = SEQ.q, n = SEQ.el.name;
    if (document.activeElement !== n && n.value !== (q.name || "")) { n.value = q.name || ""; sizeName(); }
    const [label, cls, title] = stateChip(q);
    const chip = SEQ.el.chip;
    chip.className = "chip seq-state " + cls;
    if (chip.textContent !== label) chip.textContent = label;
    chip.title = title;
  }
  // what Start does now, offered only when it can act: Start; Resume after a pause; Retry after a pause at a failed
  // block (that block again, then on). Its name stays while it works (a turning mark instead of ▶)
  function startKind(q) {
    const all = q.items || [];
    if (q.state !== "paused" || !all.length) return { label: "Start" };
    const last = all.filter((i) => FINISHED.has(i.status) && i.finished).sort((a, b) => a.finished - b.finished).pop();
    const asked = /^(paused by|stopped by|paused after|Kelvin restarted)/.test(q.reason || "");   // a person's pause, or a restart
    if (last && last.status === "failed" && !asked) return { label: "Retry", item: last };
    return { label: "Resume" };
  }
  function renderBar() {
    const q = SEQ.q, st = q.state || "idle", busy = st === "running" || st === "pausing";
    const { start, pause, stop } = SEQ.el;
    start.hidden = busy;
    const kind = SEQ.starting ? SEQ.starting : startKind(q);
    const can = !!kind.item || !!q.waiting;
    start.disabled = !!SEQ.starting || !can;
    const n = kind.item ? items().indexOf(kind.item) + 1 : 0;
    const tip = SEQ.starting ? "" : kind.item ? "Run block " + n + " again, then go on"
      : can ? "" : (q.items || []).length ? "Nothing waiting: ↺ on a block puts it back in line" : "Nothing to run yet: ＋ Add";
    if (start.title !== tip) start.title = tip;
    if (start.getAttribute("aria-label") !== kind.label) start.setAttribute("aria-label", kind.label);
    const text = start.lastChild && start.lastChild.textContent;
    if (text !== kind.label || start.classList.contains("busy") !== !!SEQ.starting) {
      start.classList.toggle("busy", !!SEQ.starting);
      start.replaceChildren(SEQ.starting ? K.h("span", { class: "seq-spin", "aria-hidden": "true" }) : ico(kind.label === "Retry" ? "again" : "play", kind.label !== "Retry"),
        K.h("span", {}, kind.label));
    }
    pause.hidden = st !== "running";
    stop.hidden = !busy;
  }
  function cardSig(it, i, look) {
    return JSON.stringify([i, it.status, it.name, it.summary, it.note, it.message, it.then_pause, it.text, it.params,
      it.settable, it.notes, it.started, it.wait, it.set, it.problem, look.name, look.kind, look.color, look.entry && look.entry.settable,
      look.entry && look.entry.id, SEQ.edit.has(it.id), SEQ.confirm.has(it.id)]);
  }
  // The blocks, keyed by item id: each is filled again only when what it shows changed, and moved only when its place
  // did. Nothing is redrawn under a drag, and a block with a field the person is typing in is left as it is (nor is
  // any block moved meanwhile): false then, and it is done once the field lets go (focusout).
  function renderList() {
    const list = SEQ.el.list, all = items();
    if (SEQ.drag && SEQ.drag.active) return false;
    const stood = flipFrom(list);   // where each block stood: those that move glide from there
    const act = document.activeElement;
    const typing = fieldIn(list) ? act.closest(".seq-card") : null;
    const focusCard = act && act.closest ? act.closest(".seq-card") : null;
    const focusId = focusCard && list.contains(focusCard) ? focusCard.dataset.id : null;
    const focusAt = focusId ? [...list.children].indexOf(focusCard) : -1;
    const focusKey = focusCard && act !== focusCard ? act.getAttribute("data-k") : null;
    let whole = true;
    const keep = new Set();
    all.forEach((it, i) => {
      keep.add(it.id);
      const look = lookOf(it);
      let c = SEQ.cards.get(it.id);
      if (!c) { c = { el: makeCard(it.id), sig: "" }; SEQ.cards.set(it.id, c); }
      const sig = cardSig(it, i, look);
      if (c.sig !== sig) {
        if (typing && typing === c.el) whole = false;
        else { fillCard(c.el, it, i, look); c.sig = sig; }
      }
      if (list.children[i] !== c.el) {
        if (typing) whole = false;
        else list.insertBefore(c.el, list.children[i] || null);
      }
    });
    if (!typing) {
      for (const [id, c] of SEQ.cards) if (!keep.has(id)) { c.el.remove(); SEQ.cards.delete(id); }
      while (list.children.length > all.length) list.lastElementChild.remove();
    } else if (list.children.length !== all.length) whole = false;
    for (const id of [...SEQ.edit]) if (!keep.has(id)) SEQ.edit.delete(id);
    list.hidden = !all.length;
    SEQ.el.add.classList.toggle("empty", !all.length);
    if (focusId && !typing) {   // the block that had the keyboard keeps it, wherever it went
      const c = SEQ.cards.get(focusId);
      if (c) {
        const target = (focusKey && c.el.querySelector('[data-k="' + focusKey + '"]')) || c.el;
        if (document.activeElement !== target && !c.el.contains(document.activeElement)) target.focus({ preventScroll: true });
      } else if (document.activeElement === document.body || !document.activeElement) {   // gone: the one now in its place
        const next = list.children[Math.min(focusAt, list.children.length - 1)];
        if (next) next.focus({ preventScroll: true });
      }
    }
    flipTo(stood);
    return whole;
  }
  // ---------- motion, as labMaster moves (app.css): a block that moves glides from where it stood, a new one rises in;
  // none when the sequence is first drawn, nor when the person asks for less motion
  const calm = () => !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  function flipFrom(list) {
    if (!SEQ.drawn || calm()) return null;
    const at = new Map();
    for (const [id, c] of SEQ.cards) if (c.el.parentNode === list) at.set(id, c.el.getBoundingClientRect().top);
    return at;
  }
  function flipTo(at, skip) {
    if (!at) return;
    const z = zoom();
    for (const [id, c] of SEQ.cards) {
      if (id === skip || !c.el.isConnected) continue;
      if (!at.has(id)) { rise(c.el); continue; }
      const dy = (at.get(id) - c.el.getBoundingClientRect().top) / z;   // the window's pixels, as the page's
      if (Math.abs(dy) > 1 && c.el.animate) {
        c.el.animate([{ transform: "translateY(" + dy + "px)" }, { transform: "none" }],
                     { duration: 340, easing: "cubic-bezier(.2, .75, .2, 1)" });
      }
    }
  }
  function rise(el) {
    el.classList.add("seq-in");
    const done = (e) => {
      if (e.target !== el) return;
      el.classList.remove("seq-in");
      el.removeEventListener("animationend", done);
    };
    el.addEventListener("animationend", done);
  }
  function makeCard(id) {
    const li = K.h("li", { class: "kcard seq-card", tabindex: "0" });
    li.dataset.id = id;
    li.addEventListener("click", (e) => guard(() => {
      if (SEQ.dragged || e.target.closest("button, input, select, textarea, label, a, .seq-ed")) return;
      toggleEd(id);
    }));
    li.addEventListener("keydown", (e) => guard(cardKey, e, li));
    li.addEventListener("pointerdown", (e) => guard(() => {
      if (e.target.closest("button, input, select, textarea, label, a, .seq-ed")) return;
      armDrag(e, { kind: "item", id, el: li });
    }));
    return li;
  }
  function fillCard(li, it, i, look) {
    const h = K.h;
    li.className = "kcard seq-card " + look.kind + " " + it.status + (SEQ.edit.has(it.id) ? " open" : "") +
      (SEQ.confirm.has(it.id) ? " confirming" : "");
    if (look.color) li.style.setProperty("--kind", look.color); else li.style.removeProperty("--kind");
    const chip = CHIP[it.status];
    li.setAttribute("aria-label", (i + 1) + ". " + look.name + (chip ? ", " + chip[0] : ""));
    const r1 = h("div", { class: "seq-r1" },
      h("span", { class: "seq-grip", "aria-hidden": "true" }, ico("grip", true)),
      h("span", { class: "seq-num" }, String(i + 1)),
      h("span", { class: "seq-title", title: look.name }, look.name),
      it.then_pause && !FINISHED.has(it.status)
        ? h("span", { class: "seq-flag", title: "Pause after", "aria-label": "Pause after" }, ico("hold")) : null,
      chip ? h("span", { class: "chip " + chip[1] }, chip[0]) : null);
    // the second line: what it runs; a failed block's reason, or a script whose file is gone, in its place
    const sub = summaryOf(it, look), what = [sub, it.note].filter(Boolean).join(" · ");
    const why = it.status === "failed" && it.message ? it.message
      : it.kind === "script" && it.problem && !FINISHED.has(it.status) ? it.problem : "";
    const line = why
      ? h("span", { class: "seq-sub seq-err", title: why + (what ? "\n" + what : "") }, firstLine(why))
      : h("span", { class: "seq-sub", title: what || null }, sub, it.note ? h("span", { class: "seq-note" }, (sub ? " · " : "") + it.note) : null);
    const running = it.status === "running";
    const r2 = h("div", { class: "seq-r2" }, line, running ? h("span", { class: "seq-ptext" }) : null);
    const parts = [r1, r2, acts(it)];   // its tools at its right, across both lines (seq.css)
    if (running) parts.push(h("span", { class: "seq-edge", "aria-hidden": "true" }));
    if (SEQ.edit.has(it.id)) {
      const ed = editor(it, look);
      if (SEQ.opening === it.id) { ed.classList.add("seq-ed-in"); SEQ.opening = null; }
      parts.push(ed);
    }
    li.replaceChildren(...parts);
    if (running) updateProgress(li, it);
    else { li.style.removeProperty("--seq-p"); li.classList.remove("indef"); }
  }
  function acts(it) {
    const h = K.h, box = h("span", { class: "seq-acts" }), id = it.id;
    const b = (k, svg, label, fn) => {
      const x = h("button", { class: "seq-act", type: "button", title: label, "aria-label": label, "data-k": k }, svg);
      x.addEventListener("click", (e) => { e.stopPropagation(); guard(fn); });
      box.append(x);
    };
    b("dup", K.icon("copy"), "Duplicate", () => op({ op: "duplicate", item: id }));
    if (it.run_id) b("run", ico("run"), "Open run", () => K.openRun(it.run_id, it.name));   // what it measured
    if (FINISHED.has(it.status)) b("again", ico("again"), "Again", () => op({ op: "again", item: id }));
    if (it.status === "waiting" || it.status === "running") b("skip", ico("skip"), "Skip", () => op({ op: "skip", item: id }));
    if (it.status !== "running") {
      if (SEQ.confirm.has(id)) {
        const x = h("button", { class: "seq-act ask", type: "button", "data-k": "rm" }, "Remove?");
        x.addEventListener("click", (e) => { e.stopPropagation(); guard(removeItem, id); });
        box.append(x);
      } else b("rm", K.icon("x"), "Remove", () => armRemove(id));
    }
    return box;
  }
  function armRemove(id) {
    clearTimeout(SEQ.confirm.get(id));
    SEQ.confirm.set(id, setTimeout(() => { SEQ.confirm.delete(id); guard(redraw); }, 3000));
    redraw();
  }
  function removeItem(id) {
    clearTimeout(SEQ.confirm.get(id));
    SEQ.confirm.delete(id);
    SEQ.edit.delete(id);
    op({ op: "remove", item: id });
  }
  function toggleEd(id, on) {
    const now = on === undefined ? !SEQ.edit.has(id) : on;
    if (now) SEQ.edit.add(id); else SEQ.edit.delete(id);
    SEQ.opening = now ? id : null;
    redraw();
  }
  function cardKey(e, li) {
    const id = li.dataset.id, all = items(), i = all.findIndex((x) => x.id === id);
    if (i < 0) return;
    const onCard = e.target === li || (e.target.matches && e.target.matches("button"));
    if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown") && onCard) {
      e.preventDefault();
      const to = e.key === "ArrowUp" ? i - 1 : i + 1;
      if (to < 0 || to >= all.length) return;
      const list = SEQ.el.list, stood = flipFrom(list);   // moved now; the server's answer confirms it
      list.insertBefore(li, e.key === "ArrowUp" ? list.children[to] : list.children[to + 1] || null);
      flipTo(stood);
      li.focus({ preventScroll: false });
      op({ op: "move", item: id, position: to + 1 });
      return;
    }
    if (e.target !== li) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleEd(id); }
    else if (e.key === "Escape" && SEQ.edit.has(id)) { e.preventDefault(); toggleEd(id, false); }
    else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      if (all[i].status === "running") return;
      if (SEQ.confirm.has(id)) removeItem(id); else armRemove(id);
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const next = SEQ.el.list.children[e.key === "ArrowDown" ? i + 1 : i - 1];
      if (next) { e.preventDefault(); next.focus(); }
    }
  }

  // ---------- the running block's progress, in place on every tick: its tint (--seq-p) and how far, in words
  function etaNow(id, eta) {   // the time left the server last said, counted down since
    if (eta == null || !isFinite(eta)) return null;
    const s = SEQ.seen.get(id);
    if (!s || s.eta !== eta) { SEQ.seen.set(id, { eta, at: Date.now() }); return eta; }
    return Math.max(0, eta - (Date.now() - s.at) / 1000);
  }
  function progressOf(it) {
    const now = Date.now() / 1000, p = it.progress || null, d = parsed(it);
    let done = p ? p.done : null, total = p ? p.total : null, eta = p ? p.eta_s : null;
    if (!p && it.kind === "plan" && SEQ.live && SEQ.live.status === "running") {
      done = SEQ.live.done; total = SEQ.live.total; eta = SEQ.live.eta_s;
    }
    if (total) {
      const left = etaNow(it.id, eta);
      return { frac: (done || 0) / total, text: (done || 0) + "/" + total + (left != null ? " · " + clock(left) + " left" : "") };
    }
    if (it.kind === "step") {
      const tot = Number(it.wait != null ? it.wait : d.wait) || 0;
      if (p && p.phase === "wait" && p.until) {
        const left = Math.max(0, p.until - now);
        return { frac: tot ? 1 - left / tot : null, text: clock(left) + " left" };
      }
      if (p && p.phase === "stabilize") return { frac: null, text: "Settling" };
      if (!p && waitOnly(d) && it.started) {
        const left = Math.max(0, it.started + tot - now);
        return { frac: 1 - left / tot, text: clock(left) + " left" };
      }
    }
    return { frac: null, text: it.started ? clock(now - it.started) : "" };
  }
  function updateProgress(card, it) {
    if (!card) return;
    const p = progressOf(it), text = card.querySelector(".seq-ptext");
    card.classList.toggle("indef", p.frac == null);
    const at = p.frac == null ? "" : Math.max(0, Math.min(100, 100 * p.frac)).toFixed(1) + "%";
    if (card.style.getPropertyValue("--seq-p") !== at) {
      if (at) card.style.setProperty("--seq-p", at); else card.style.removeProperty("--seq-p");
    }
    if (text && text.textContent !== p.text) text.textContent = p.text;
  }
  function tick() {
    const it = items().find((i) => i.status === "running");
    if (!it) return;
    const c = SEQ.cards.get(it.id);
    if (c) updateProgress(c.el, it);
  }

  // ---------- a block's editor, in place
  function typed(was, raw) {   // what was typed, as the kind of value it replaces
    if (typeof was === "boolean") return raw;
    if (typeof was === "string") return raw;
    const t = String(raw).trim();
    if (t !== "" && isFinite(Number(t))) return Number(t);
    if (/^(true|false)$/i.test(t)) return /^true$/i.test(t);
    try { return JSON.parse(t); } catch (e) { return raw; }
  }
  const shown = (v) => (typeof v === "string" ? v : v == null ? "" : typeof v === "number" ? num(v) : JSON.stringify(v));
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  function field(label, input, note) {
    return K.h("label", { class: "seq-field", title: note || null }, K.h("span", {}, label), input);
  }
  function textInput(value, label, numeric, locked) {
    const input = K.h("input", { class: "kfield", type: "text", value: shown(value), spellcheck: "false", autocomplete: "off",
                                 inputmode: numeric ? "decimal" : null, "aria-label": label });
    input.disabled = !!locked;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); input.blur(); } });
    return input;
  }
  function editor(it, look) {
    const h = K.h, box = h("div", { class: "seq-ed" }), d = look.d, locked = it.status === "running";
    if (it.kind === "script") {
      const lib = look.entry || {};
      const settable = it.settable || lib.settable || {};
      const params = it.params || (d.params && typeof d.params === "object" ? d.params : {});
      const notes = it.notes || lib.notes || {};
      const names = [...new Set([...Object.keys(settable), ...Object.keys(params)])];
      if (!names.length) box.append(h("span", { class: "chip seq-none" }, "No settings"));
      for (const k of names) {
        const fileValue = settable[k], value = k in params ? params[k] : fileValue;
        const kind = fileValue !== undefined ? fileValue : value;
        let input;
        if (typeof kind === "boolean") {
          input = h("input", { type: "checkbox", "aria-label": k });
          input.checked = !!value;
          input.disabled = locked;
        } else input = textInput(value, k, typeof kind === "number", locked);
        input.addEventListener("change", () => guard(() => {
          let v = input.type === "checkbox" ? input.checked : typed(kind, input.value);
          if (typeof kind !== "string" && input.type !== "checkbox" && !input.value.trim()) v = null;   // emptied
          if (fileValue !== undefined && same(v, fileValue)) v = null;   // the file's own value again
          op({ op: "params", item: it.id, params: { [k]: v } });
        }));
        box.append(field(k, input, notes[k]));
      }
    } else if (it.kind === "step") {
      const wait = Number(it.wait != null ? it.wait : d.wait) || 0;
      const sets = it.set && typeof it.set === "object" ? it.set : d.set && typeof d.set === "object" ? d.set : {};
      const w = textInput(num(Number((wait / 60).toFixed(3))), "Wait, minutes", true, locked);
      w.addEventListener("change", () => guard(() => {
        const m = Number(w.value.trim() || 0);
        if (!isFinite(m) || m < 0) { K.toast("Wait: minutes, 0 or more"); w.value = num(Number((wait / 60).toFixed(3))); return; }
        op({ op: "params", item: it.id, wait: Math.round(m * 60 * 1000) / 1000 });
      }));
      box.append(field("Wait", h("span", { class: "seq-unit" }, w, h("span", {}, "min"))));
      for (const [k, v] of Object.entries(sets)) {
        const input = textInput(v, k, typeof v === "number", locked);
        input.addEventListener("change", () => guard(() => op({ op: "params", item: it.id,   // emptied: no longer set
          set: { [k]: input.value.trim() ? typed(v, input.value) : null } })));
        box.append(field(k, input));
      }
    } else if (it.kind === "plan") {   // this block itself, in the scan editor (Save writes it back into the block)
      const b = h("button", { class: "btn small", type: "button" }, K.icon("pencil"), h("span", {}, "Edit scan"));
      b.addEventListener("click", (e) => { e.stopPropagation(); guard(() => K.openEditor(null, it.id)); });
      box.append(b);
    } else box.append(h("pre", { class: "seq-yaml", "data-region": "seq-blocks" }, String(it.text || "").trim()));
    // Pause after: the one switch (app.css .switch). It turns at once, busy while the server answers; a refusal puts
    // it back as the server has it (op draws the blocks again) and says why
    const sw = h("button", { class: "switch", type: "button", role: "switch", "aria-checked": String(!!it.then_pause),
                             "aria-label": "Pause after", "data-k": "then-pause" }, h("span", { "aria-hidden": "true" }));
    sw.addEventListener("click", (e) => {
      e.stopPropagation();
      if (sw.getAttribute("aria-busy") === "true") return;
      const want = sw.getAttribute("aria-checked") !== "true";
      sw.setAttribute("aria-checked", String(want));
      sw.setAttribute("aria-busy", "true");
      guard(() => op({ op: "then_pause", item: it.id, value: want }).finally(() => sw.removeAttribute("aria-busy")));
    });
    box.append(h("label", { class: "seq-switch" }, sw, h("span", {}, "Pause after")));
    return box;
  }

  // ------------------------------------------------------------------ the control bar
  async function startSeq() {
    if (SEQ.starting || !SEQ.q) return;
    const kind = startKind(SEQ.q);
    SEQ.starting = kind;
    renderBar();
    const pending = setTimeout(() => { if (SEQ.starting) say("Waiting for approval"); }, 1200);
    try {
      // Retry: the failed block back in line where it is (the first waiting), then the queue on from there
      if (kind.item && !(await op({ op: "again", item: kind.item.id }))) return;
      await op({ op: "start" });
    } finally {
      clearTimeout(pending);
      SEQ.starting = false;
      if (SEQ.el.msg.textContent === "Waiting for approval") say("");
      if (SEQ.q) renderBar();
      K.pollState();
    }
  }
  async function stopSeq() {
    SEQ.el.stop.disabled = true;
    try { await K.api("/api/runs/stop", { body: { queue: true } }); }
    catch (e) { K.toast(e.message); }
    finally { SEQ.el.stop.disabled = false; K.pollState(); }
  }
  function renameSeq() {   // Enter, or the field let go: once for each new name
    const n = SEQ.el.name, now = (SEQ.q && SEQ.q.name) || "", want = n.value.trim();
    if (!want) { n.value = now; sizeName(); return; }
    if (want === now || want === SEQ.renameSent) return;
    SEQ.renameSent = want;
    op({ op: "rename", name: want }).finally(() => { SEQ.renameSent = null; });
  }
  function moreMenu(anchor) {
    const q = SEQ.q || {}, all = q.items || [], cont = q.on_error === "continue";
    // a choice of two (radio items, a dot on the chosen one) and a setting that acts at once (a checkbox item, its box
    // drawn ticked or empty): popMenu draws them by their role
    const menu = [{ heading: "On a failure" },
      { label: "Pause", role: "menuitemradio", checked: !cont, onClick: () => { if (cont) op({ op: "on_error", value: "pause" }); } },
      { label: "Go on", role: "menuitemradio", checked: cont, onClick: () => { if (!cont) op({ op: "on_error", value: "continue" }); } },
      { sep: true }];
    if (all.length) menu.push({ label: "Sweep…", icon: "sliders", onClick: () => guard(openSweep) });
    if (all.length) menu.push({ label: "Save as…", icon: "download", onClick: () => guard(openSave) });
    const saved = q.saved || [];
    if (saved.length) {
      menu.push({ heading: "Load" });
      for (const s of saved) menu.push({ label: s, icon: "doc", onClick: () => op({ op: "load", name: s }) });
    }
    if (all.some((i) => FINISHED.has(i.status))) {
      menu.push({ sep: true }, { label: "Clear finished", icon: "trash", onClick: () => op({ op: "clear" }) });
    }
    // the person's switch (notices.py): a stop or a failed block continues the chat it concerns at once
    const react = !!(K.S.settings && K.S.settings.sequence_react);
    menu.push({ sep: true }, { heading: "Stops and failures" },
      { label: "Kelvin replies at once", role: "menuitemcheckbox", checked: react, onClick: () => guard(() => setReact(!react)) });
    K.popMenu(anchor, menu);
  }
  async function setReact(on) {
    try {
      const r = await K.api("/api/settings", { body: { sequence_react: on ? "on" : "off" } });
      if (r && r.settings) K.S.settings = r.settings;
    } catch (e) { K.toast(e.message); }
  }

  // ------------------------------------------------------------------ the functions: what can go into the sequence
  function fnGroups() {
    const lib = SEQ.lib || {}, f = SEQ.filter.trim().toLowerCase();
    const scriptSub = (p) => String(p || "").split(/[\\/]/).slice(-2).join("/");
    const groups = [
      ["Scripts", (lib.scripts || []).map((e) => ({ type: "script", key: e.id, entry: e, name: e.name, sub: scriptSub(e.path),
                                                   kind: "k-script", color: e.color, missing: e.exists === false }))],
      ["Experiments", (SEQ.exps || []).map((x) => ({ type: "exp", key: "exp:" + x.name, exp: x, name: x.name,
                                                    sub: "v" + x.version, kind: "k-exp" }))],
      ["Scans", (lib.scans || []).map((e) => ({ type: "scan", key: e.id, entry: e, name: e.name, sub: e.summary || "",
                                               kind: "k-scan", color: e.color }))],
    ];
    return groups.map(([title, fns]) => [title, f ? fns.filter((x) => (x.name + " " + x.sub).toLowerCase().includes(f)) : fns])
      .filter(([, fns]) => fns.length);
  }
  function renderFns() {
    const g = SEQ.el.groups;
    if (!g || SEQ.el.fns.hidden) return;
    if (fieldIn(g) || (SEQ.drag && SEQ.drag.active)) { SEQ.fnsPending = true; return; }
    SEQ.fnsPending = false;
    const h = K.h;
    g.replaceChildren(...fnGroups().map(([title, fns]) =>
      h("div", { class: "seq-grp", role: "group", "aria-label": title }, h("div", { class: "seq-gh" }, title), ...fns.map(fnCard))));
  }
  function fnCard(fn) {
    const h = K.h;
    let tail;
    if (SEQ.fnConfirm === fn.key) {
      tail = h("button", { class: "seq-act ask", type: "button" }, "Remove?");
      tail.addEventListener("click", (e) => { e.stopPropagation(); guard(removeFn, fn); });
    } else {
      tail = h("button", { class: "icon-btn seq-fn-more", type: "button", title: "More", "aria-label": "More: " + fn.name,
                           "aria-haspopup": "menu" }, K.icon("dots"));
      tail.addEventListener("click", (e) => { e.stopPropagation(); guard(fnMenu, tail, fn); });
    }
    let nameEl;
    if (SEQ.renaming === fn.key) {
      nameEl = h("input", { class: "kfield seq-fn-rename", value: fn.name, maxlength: "80", spellcheck: "false", "aria-label": "Name" });
      let done = false;
      const finish = async (save) => {
        if (done) return;
        done = true;
        SEQ.renaming = null;
        const v = nameEl.value.trim(), had = document.activeElement === nameEl;
        if (had) nameEl.blur();   // a field with the keyboard holds the list as it is
        if (save && v && v !== fn.name) await libOp({ op: "rename", id: fn.entry.id, name: v });
        else renderFns();
        const back = had && SEQ.el.groups.querySelector('.seq-fn[data-key="' + CSS.escape(fn.key) + '"]');
        if (back) back.focus();
      };
      nameEl.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Enter") { e.preventDefault(); finish(true); }
        else if (e.key === "Escape") { e.preventDefault(); finish(false); }
      });
      nameEl.addEventListener("blur", () => guard(finish, true));
      nameEl.addEventListener("click", (e) => e.stopPropagation());
    } else nameEl = h("span", { class: "seq-fn-name" }, fn.name);
    // one line: its name, then where it is while there is room; the whole of it in its tip
    const tip = [fn.name, fn.sub, fn.entry && fn.entry.note].filter(Boolean).join("\n");
    const card = h("div", { class: "kcard seq-fn " + fn.kind + (SEQ.fresh === fn.key ? " fresh" : "") + (fn.missing ? " missing" : ""),
                            tabindex: "0", role: "button", "aria-label": fn.name, title: tip },
      nameEl, fn.sub ? h("span", { class: "seq-fn-sub" }, fn.sub) : null, tail);
    card.dataset.key = fn.key;
    if (fn.color) card.style.setProperty("--kind", fn.color);
    card.addEventListener("click", (e) => guard(() => {
      if (SEQ.dragged || SEQ.renaming === fn.key || e.target.closest("button, input")) return;
      appendFn(fn, card);
    }));
    card.addEventListener("keydown", (e) => guard(() => {
      if (e.target !== card) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); appendFn(fn, card); }
    }));
    card.addEventListener("pointerdown", (e) => guard(() => {
      if (e.target.closest("button, input") || SEQ.renaming === fn.key) return;
      armDrag(e, { kind: "fn", fn, el: card });
    }));
    return card;
  }
  function yq(s) { return "'" + String(s).replace(/'/g, "''") + "'"; }
  function fnText(fn) {   // the item a function makes, as op add takes it
    if (fn.type === "exp") return "script: " + fn.exp.name;
    if (fn.type === "scan") return String(fn.entry.text || "");
    const e = fn.entry, params = e.params && typeof e.params === "object" && Object.keys(e.params).length ? e.params : null;
    return "script: " + yq(e.path) + (params ? "\nparams: " + JSON.stringify(params) : "");
  }
  async function appendFn(fn, card) {
    if (card) { card.classList.add("sent"); setTimeout(() => card.classList.remove("sent"), 400); }
    if (fn.type === "exp") {
      return viaQueue(() => K.api("/api/experiments/" + encodeURIComponent(fn.exp.name) + "/run", { body: { queue: true } }));
    }
    return viaQueue(() => K.api("/api/library", { body: { op: "run", id: fn.entry.id, queue: true } }));
  }
  function fnMenu(anchor, fn) {
    const menu = [];
    if (fn.entry) {
      menu.push({ label: "Rename", icon: "pencil", onClick: () => {
        SEQ.renaming = fn.key;
        renderFns();
        const f = SEQ.el.groups.querySelector(".seq-fn-rename");
        if (f) { f.focus(); f.select(); }
      } });
      menu.push({ label: "Colour", onClick: () => guard(swatchPop, anchor, fn) });
    }
    if (fn.type === "scan") menu.push({ label: "Edit", icon: "doc", onClick: () => K.openEditor(fn.entry.id) });
    if (fn.type === "exp") menu.push({ label: "Versions", icon: "doc", onClick: () => K.openVersions(fn.exp.name) });
    if (fn.entry) menu.push({ sep: true }, { label: "Remove", icon: "trash", danger: true, onClick: () => {
      SEQ.fnConfirm = fn.key;
      clearTimeout(SEQ.fnConfirmTimer);
      SEQ.fnConfirmTimer = setTimeout(() => { SEQ.fnConfirm = null; guard(renderFns); }, 3000);
      renderFns();
      const ask = SEQ.el.groups.querySelector(".seq-fn .ask");
      if (ask) ask.focus();
    } });
    if (menu.length) K.popMenu(anchor, menu);
  }
  async function removeFn(fn) {
    clearTimeout(SEQ.fnConfirmTimer);
    SEQ.fnConfirm = null;
    await libOp({ op: "remove", id: fn.entry.id });
  }
  async function libMeta(id, patch) {
    const r = await libOp(Object.assign({ op: "meta", id }, patch));
    if (r && !r.library) loadLib();
    return r;
  }

  // ---------- the colour swatches (a small pop-over)
  function closePop() {
    if (SEQ.pop) { SEQ.pop.remove(); SEQ.pop = null; }
  }
  function swatchRow(current, pick) {
    const h = K.h, row = h("div", { class: "seq-swatches", role: "group", "aria-label": "Colour" });
    const draw = (now) => {
      row.replaceChildren(...SWATCHES.map(([label, c]) => {
        const on = String(now || "").toLowerCase() === c;
        const b = h("button", { class: "seq-swatch" + (on ? " on" : ""), type: "button", title: label, "aria-label": label,
                                "aria-pressed": String(on) });
        b.style.background = c;
        b.addEventListener("click", (e) => { e.stopPropagation(); const next = on ? "" : c; guard(pick, next); draw(next); });
        return b;
      }));
    };
    draw(current);
    return row;
  }
  function swatchPop(anchor, fn) {
    closePop();
    const pop = K.h("div", { class: "seq-pop", role: "dialog", "aria-label": "Colour" },
      swatchRow(fn.entry.color, (c) => { closePop(); libMeta(fn.entry.id, { color: c }); }));
    pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); closePop(); anchor.focus(); } });
    document.body.append(pop);
    if (anchor.isConnected) anchor.scrollIntoView({ block: "nearest" });
    // placed in the window's pixels (the anchor's rect, the window), written in the page's (÷ the page size)
    const z = zoom(), r = anchor.getBoundingClientRect(), ph = pop.offsetHeight * z, pw = pop.offsetWidth * z;
    const top = r.bottom + 6 + ph + 8 <= window.innerHeight ? r.bottom + 6 : r.top - ph - 6;
    pop.style.top = Math.max(8, Math.min(top, window.innerHeight - ph - 8)) / z + "px";
    pop.style.left = Math.max(8, Math.min(r.right - pw, window.innerWidth - pw - 8)) / z + "px";
    SEQ.pop = pop;
    const first = pop.querySelector("button");
    if (first) first.focus();
  }

  // ------------------------------------------------------------------ dragging: a block to its place, a function in
  // Pointer events (mouse, pen, touch): a press that moves 6 px is a drag; on touch only the grip drags (the rest
  // scrolls). Nothing is redrawn while it is under way; the drop is one op (move, or add at that place).
  function armDrag(e, src) {
    if (e.button !== 0 || SEQ.drag) return;
    if (e.pointerType === "touch" && !(e.target.closest && e.target.closest(".seq-grip"))) return;
    SEQ.drag = { src, x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, pid: e.pointerId, active: false, to: null };
    window.addEventListener("pointermove", dragMove, { passive: false });
    window.addEventListener("pointerup", dragEnd);
    window.addEventListener("pointercancel", dragCancel);
    window.addEventListener("keydown", dragKey, true);
  }
  function dragBegin(d) {
    d.active = true;
    const el = d.src.el, r = el.getBoundingClientRect();
    d.dx = d.x0 - r.left;
    d.dy = d.y0 - r.top;
    const g = el.cloneNode(true);
    for (const n of g.querySelectorAll(".seq-ed, .seq-acts, .seq-fn-more")) n.remove();
    g.classList.add("seq-ghost");
    g.classList.remove("open", "fresh", "dragging");
    g.removeAttribute("tabindex");
    g.setAttribute("aria-hidden", "true");
    d.z = zoom();   // the pointer and the rect are the window's pixels; the ghost's lengths the page's
    g.style.width = r.width / d.z + "px";
    document.body.append(g);
    d.ghost = g;
    if (d.src.kind === "item") el.classList.add("dragging");
    document.body.classList.add("seq-dragging");
    closePop();
    K.closeMenu();
    try { window.getSelection().removeAllRanges(); } catch (x) { /* nothing selected */ }
    d.scroll = setInterval(() => guard(autoScroll, d), 30);
  }
  function dragMove(e) {
    const d = SEQ.drag;
    if (!d || e.pointerId !== d.pid) return;
    d.x = e.clientX;
    d.y = e.clientY;
    if (!d.active) {
      if (Math.hypot(d.x - d.x0, d.y - d.y0) < 6) return;
      guard(dragBegin, d);
    }
    e.preventDefault();
    d.ghost.style.transform = "translate(" + Math.round((d.x - d.dx) / d.z) + "px," + Math.round((d.y - d.dy) / d.z) + "px)";
    guard(dragTarget, d);
  }
  function clearMarks() {
    for (const n of SEQ.el.cols.querySelectorAll(".drop-before, .drop-after, .drop-on")) n.classList.remove("drop-before", "drop-after", "drop-on");
  }
  function dragTarget(d) {
    clearMarks();
    // over the blocks (a little beyond them, except where the functions lie just under them)
    const list = SEQ.el.list, mr = SEQ.el.main.getBoundingClientRect(), under = SEQ.wide || SEQ.el.fns.hidden ? 24 : 0;
    const over = d.x >= mr.left && d.x <= mr.right && d.y >= mr.top - 24 && d.y <= mr.bottom + under;
    if (d.src.kind === "fn" && !over) { d.to = null; return; }
    const cards = [...list.children].filter((c) => c !== d.src.el);
    let at = cards.length;
    for (let k = 0; k < cards.length; k++) {
      const r = cards[k].getBoundingClientRect();
      if (d.y < r.top + r.height / 2) { at = k; break; }
    }
    d.to = at;
    if (d.src.kind === "item" && at === items().findIndex((x) => x.id === d.src.id)) return;   // where it is
    if (!cards.length) SEQ.el.add.classList.add("drop-on");
    else if (at < cards.length) cards[at].classList.add("drop-before");
    else cards[cards.length - 1].classList.add("drop-after");
  }
  function autoScroll(d) {   // near the top or the foot of the blocks' region, it scrolls (the window's pixels ÷ the page size)
    if (!d.active) return;
    const sc = SEQ.el.list, r = sc.getBoundingClientRect();
    if (!r.height || d.x < r.left || d.x > r.right || d.y < r.top - 44 || d.y > r.bottom + 44) return;
    let dy = 0;
    if (d.y < r.top + 44) dy = -Math.ceil((r.top + 44 - d.y) / 4);
    else if (d.y > r.bottom - 44) dy = Math.ceil((d.y - r.bottom + 44) / 4);
    if (!dy) return;
    const was = sc.scrollTop;
    sc.scrollTop += Math.max(-24, Math.min(24, dy)) / (d.z || 1);
    if (sc.scrollTop !== was) dragTarget(d);
  }
  function dragStop() {
    const d = SEQ.drag;
    window.removeEventListener("pointermove", dragMove, { passive: false });
    window.removeEventListener("pointerup", dragEnd);
    window.removeEventListener("pointercancel", dragCancel);
    window.removeEventListener("keydown", dragKey, true);
    SEQ.drag = null;
    if (!d) return null;
    clearInterval(d.scroll);
    if (d.ghost) d.ghost.remove();
    d.src.el.classList.remove("dragging");
    document.body.classList.remove("seq-dragging");
    clearMarks();
    if (SEQ.fnsPending) guard(renderFns);   // the functions as they came meanwhile
    return d;
  }
  function dragEnd(e) {
    const d0 = SEQ.drag;
    if (!d0 || e.pointerId !== d0.pid) return;
    const d = dragStop();
    if (!d.active) return;
    SEQ.dragged = true;
    setTimeout(() => { SEQ.dragged = false; }, 0);
    guard(() => {
      if (d.to != null) {
        if (d.src.kind === "item") {
          const from = items().findIndex((x) => x.id === d.src.id);
          if (from >= 0 && d.to !== from) {
            const list = SEQ.el.list, others = [...list.children].filter((c) => c !== d.src.el), stood = flipFrom(list);
            list.insertBefore(d.src.el, others[d.to] || null);   // there now; the server's answer confirms it
            flipTo(stood, d.src.id);
            [...list.children].forEach((c, k) => { const n = c.querySelector(".seq-num"); if (n) n.textContent = String(k + 1); });
            op({ op: "move", item: d.src.id, position: d.to + 1 });
          }
        } else {
          const text = fnText(d.src.fn);
          if (text) op({ op: "add", text, position: d.to + 1 });
        }
      }
      if (SEQ.pending) render();
    });
  }
  function dragCancel() {
    const d = dragStop();
    if (d && d.active && SEQ.pending) guard(render);
  }
  function dragKey(e) {
    if (e.key !== "Escape" || !SEQ.drag) return;
    e.preventDefault();
    e.stopPropagation();
    if (SEQ.drag.active) { SEQ.dragged = true; setTimeout(() => { SEQ.dragged = false; }, 0); }
    dragCancel();
  }

  // ------------------------------------------------------------------ dialogs: New function, Sweep, Save as
  function dialog(title, body, foot, submit) {
    const h = K.h;
    const form = h("form", { class: "seq-dlg", novalidate: true },
      h("div", { class: "dialog-head" }, h("h2", {}, title)), ...body, h("div", { class: "dialog-foot" }, ...foot));
    const dlg = h("dialog", { class: "dialog seq-dialog" }, form);
    form.addEventListener("submit", (e) => { e.preventDefault(); guard(submit); });
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
    return dlg;
  }
  function cancelButton(get) {
    const b = K.h("button", { class: "btn", type: "button" }, "Cancel");
    b.addEventListener("click", () => { const d = get(); if (d) d.close(); });
    return b;
  }

  // how many of each of several lists to show, at most n in all: one of each in turn while there is room, so a long list
  // leaves the others room (scanlibrary.shares)
  function shares(sizes, n) {
    const room = sizes.map(() => 0);
    let left = n;
    while (left > 0 && room.some((r, i) => r < sizes[i])) {
      room.forEach((r, i) => { if (left > 0 && r < sizes[i]) { room[i] += 1; left -= 1; } });
    }
    return room;
  }

  // ---------- New function: one of the lab's scripts (or any file) into the library, named and coloured; the scripts
  // of several folders each under their folder's name
  function newFunction() {
    const h = K.h;
    const st = { path: "", touched: false, color: "", files: [], folders: [], labels: null, seq: 0, busy: false };
    const filter = h("input", { class: "kfield", type: "search", placeholder: "Filter", spellcheck: "false", autocomplete: "off",
                                "aria-label": "Filter files" });
    const files = h("div", { class: "seq-files", role: "listbox", "aria-label": "Files", "data-region": "dialog" });
    const moreFiles = h("div", { class: "seq-files-more", hidden: true });   // how many more: the filter finds them
    const filesBox = h("div", { class: "seq-lab", hidden: true }, filter, files, moreFiles);
    const path = h("input", { class: "kfield", type: "text", placeholder: "Path", spellcheck: "false", autocomplete: "off",
                              "aria-label": "Path" });
    const choose = K.hasFilePicker() ? h("button", { class: "btn", type: "button" }, "Choose…") : null;
    const found = h("div", { class: "seq-found", "aria-live": "polite" });
    const name = h("input", { class: "kfield", type: "text", maxlength: "80", spellcheck: "false", autocomplete: "off", "aria-label": "Name" });
    const add = h("button", { class: "btn primary", type: "submit", disabled: true }, "Add");
    let dlg = null;
    const drawFiles = () => {
      const f = filter.value.trim().toLowerCase();
      const match = st.files.filter((x) => !f || String(x.rel || x.name).toLowerCase().includes(f));
      const option = (x) => {
        const on = x.path === st.path;
        const b = h("button", { class: "seq-file" + (on ? " on" : ""), type: "button", role: "option", "aria-selected": String(on),
                                title: x.path }, x.rel || x.name);
        b.addEventListener("click", () => guard(choosePath, x.path));
        return b;
      };
      // at most 300 shown; from several folders, each under its name with its share (a big one hides none of the others)
      const groups = !st.labels ? [["", match]]
        : st.folders.map((d) => [d, match.filter((x) => x.folder === d)]).filter(([, xs]) => xs.length);
      const room = shares(groups.map(([, xs]) => xs.length), 300);
      const left = match.length - room.reduce((a, n) => a + n, 0);
      moreFiles.textContent = left + " more";
      moreFiles.hidden = left <= 0;
      if (!st.labels) { files.replaceChildren(...match.slice(0, room[0]).map(option)); return; }
      files.replaceChildren(...groups.map(([d, xs], i) => h("div", { class: "seq-folder", role: "group", "aria-label": d },
        h("div", { class: "seq-folder-head", title: d, "aria-hidden": "true" }, st.labels.get(d)),
        ...xs.slice(0, room[i]).map(option))));
    };
    async function choosePath(p) {
      st.path = String(p || "").trim();
      path.value = st.path;
      drawFiles();
      found.replaceChildren();
      add.disabled = !st.path;
      if (!st.path) return;
      if (!st.touched) name.value = base(st.path).replace(/\.(py|ipynb)$/i, "");
      const ask = ++st.seq;
      let r = null;
      try { r = await K.api("/api/library", { body: { op: "inspect", path: st.path } }); }
      catch (e) { if (ask === st.seq) found.replaceChildren(h("span", { class: "seq-bad" }, e.message)); return; }
      if (ask !== st.seq || !r) return;
      if (r.problem) { found.replaceChildren(h("span", { class: "seq-bad" }, r.problem)); add.disabled = true; return; }
      const set = Object.entries(r.settable || {});
      const notes = r.notes || {};
      found.replaceChildren(...(set.length
        ? set.map(([k, v]) => h("span", { class: "chip", title: notes[k] || null }, k + " = " + shown(v)))
        : [h("span", { class: "chip" }, "No settings")]));
    }
    path.addEventListener("change", () => guard(choosePath, path.value));
    path.addEventListener("keydown", (e) => {   // Enter on a new path reads it; on the one read, adds it
      if (e.key !== "Enter" || path.value.trim() === st.path) return;
      e.preventDefault();
      guard(choosePath, path.value);
    });
    filter.addEventListener("input", () => guard(drawFiles));
    filter.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
    name.addEventListener("input", () => { st.touched = !!name.value.trim(); });
    if (choose) choose.addEventListener("click", async () => {
      let p = "";
      try { p = (await window.pywebview.api.pick_file(K.store("lh.scriptFolder") || "")) || ""; } catch (e) { return; }
      if (p) guard(choosePath, p);
    });
    const submit = async () => {
      if (st.busy) return;
      if (path.value.trim() !== st.path) await choosePath(path.value);
      if (!st.path || add.disabled) return;
      st.busy = true;
      add.disabled = true;
      try {
        const r = await K.api("/api/library", { body: { op: "import", path: st.path } });
        const entry = r && r.entry;
        if (r && r.library && Array.isArray(r.library.scripts)) SEQ.lib = r.library;
        K.store("lh.scriptFolder", st.path.replace(/[\\/][^\\/]*$/, ""));
        if (entry) {
          const want = name.value.trim();
          const patch = {};
          if (want && want !== entry.name) patch.name = want;
          if (st.color) patch.color = st.color;
          if (Object.keys(patch).length) {
            try { await K.api("/api/library", { body: Object.assign({ op: "meta", id: entry.id }, patch) }); }
            catch (e) { K.toast(e.message); }
          }
          SEQ.fresh = entry.id;
          setTimeout(() => {
            if (SEQ.fresh !== entry.id) return;
            SEQ.fresh = null;
            const c = SEQ.el.groups.querySelector('.seq-fn[data-key="' + CSS.escape(entry.id) + '"]');
            if (c) c.classList.remove("fresh");
          }, 2000);
        }
        if (dlg) dlg.close();
        if (!SEQ.wide) { SEQ.adding = true; showFns(); }
        await loadLib();
        const c = entry && SEQ.el.groups.querySelector('.seq-fn[data-key="' + CSS.escape(entry.id) + '"]');
        if (c) c.scrollIntoView({ block: "nearest" });
      } catch (e) {
        K.toast(e.message);
        add.disabled = false;
      } finally { st.busy = false; }
    };
    dlg = dialog("New function", [
      filesBox,
      h("div", { class: "seq-line" }, path, choose),
      found,
      h("label", { class: "seq-lab" }, h("span", {}, "Name"), name),
      h("div", { class: "seq-lab" }, h("span", {}, "Colour"), swatchRow("", (c) => { st.color = c; })),
    ], [h("span"), cancelButton(() => dlg), add], submit);
    (async () => {
      try {
        const r = await K.api("/api/library", { body: { op: "files" } });
        st.files = (r && Array.isArray(r.files) ? r.files : []);
        st.folders = (r && Array.isArray(r.folders) ? r.folders : []);
        st.labels = st.folders.length > 1 ? K.folderLabels(st.folders) : null;
      } catch (e) { st.files = []; }
      filesBox.hidden = !st.files.length;
      drawFiles();
      (st.files.length ? filter : path).focus();
    })();
    path.focus();
  }

  // ---------- Sweep…: the ticked blocks again for each value of one setting (op ladder)
  function openSweep() {
    const h = K.h, all = items();
    if (!all.length) return;
    const ticks = all.map((it, i) => {
      const look = lookOf(it);
      const cb = h("input", { type: "checkbox" });
      cb.checked = it.status === "waiting";
      return { it, look, cb, row: h("label", { class: "seq-tick" }, cb, h("span", { class: "seq-tick-name" }, (i + 1) + ". " + look.name)) };
    });
    const key = h("input", { class: "kfield", type: "text", spellcheck: "false", autocomplete: "off", "aria-label": "Setting" });
    const unit = h("span", { class: "seq-unit-tag", hidden: true }, "min");   // a wait is in minutes, as in its block
    const options = h("datalist", { id: "seq-sweep-keys-" + Date.now() });
    key.setAttribute("list", options.id);
    const from = h("input", { class: "kfield", type: "text", inputmode: "decimal", "aria-label": "From" });
    const to = h("input", { class: "kfield", type: "text", inputmode: "decimal", "aria-label": "To" });
    const step = h("input", { class: "kfield", type: "text", inputmode: "decimal", "aria-label": "Step" });
    const listed = h("input", { class: "kfield", type: "text", spellcheck: "false", "aria-label": "List" });
    const count = h("span", { class: "seq-count", "aria-live": "polite" });
    const add = h("button", { class: "btn primary", type: "submit", disabled: true }, "Add");
    let keyTouched = false, dlg = null;
    const keysOf = (t) => {
      const it = t.it, d = t.look.d;
      if (it.kind === "script") {
        const lib = t.look.entry || {};
        const own = it.params || (d.params && typeof d.params === "object" ? d.params : {});
        return Object.keys(Object.assign({}, it.settable || lib.settable || {}, own));
      }
      if (it.kind === "step") {
        const sets = it.set && typeof it.set === "object" ? it.set : d.set && typeof d.set === "object" ? d.set : {};
        return [...Object.keys(sets), "wait"];
      }
      return [];
    };
    const values = () => {
      const raw = listed.value.trim();
      if (raw) return raw.split(/[,;\s]+/).filter(Boolean).map((v) => (isFinite(Number(v)) ? Number(v) : v));
      const a = Number(from.value), b = Number(to.value), s = Math.abs(Number(step.value));
      if (from.value.trim() === "" || to.value.trim() === "" || !isFinite(a) || !isFinite(b)) return [];
      if (a === b) return [a];
      if (!isFinite(s) || s === 0) return [];
      const n = Math.floor(Math.abs(b - a) / s + 1e-9) + 1;
      if (n > MAX_COPIES + 1) return new Array(n).fill(0);
      const dir = b > a ? 1 : -1;
      return Array.from({ length: n }, (_, k) => Number((a + dir * k * s).toPrecision(12)));
    };
    const update = () => {
      const picked = ticks.filter((t) => t.cb.checked);
      const rank = (t) => (t.it.kind === "script" ? 0 : 1);   // a script's settings first: what a sweep is mostly of
      const keys = [...new Set([...picked].sort((a, b) => rank(a) - rank(b)).flatMap(keysOf))];
      keys.sort((a, b) => (a === "wait") - (b === "wait"));
      options.replaceChildren(...keys.map((k) => h("option", { value: k })));
      if (!keyTouched) key.value = keys[0] || "";
      unit.hidden = key.value.trim() !== "wait";
      const n = values().length * picked.length;
      count.textContent = n + (n === 1 ? " block" : " blocks");
      count.classList.toggle("bad", n > MAX_COPIES);
      add.disabled = !(n > 0 && n <= MAX_COPIES && key.value.trim());
    };
    for (const t of ticks) t.cb.addEventListener("change", () => guard(update));
    for (const f of [from, to, step, listed]) f.addEventListener("input", () => guard(update));
    key.addEventListener("input", () => { keyTouched = !!key.value.trim(); guard(update); });
    const submit = async () => {
      const picked = ticks.filter((t) => t.cb.checked).map((t) => t.it.id), k = key.value.trim();
      let vals = values();
      if (!picked.length || !vals.length || !k) return;
      if (k === "wait") {   // minutes here, seconds to the queue
        if (vals.some((v) => typeof v !== "number" || v < 0)) { K.toast("Wait: minutes, 0 or more"); return; }
        vals = vals.map((v) => Math.round(v * 60 * 1000) / 1000);
      }
      add.disabled = true;
      const r = await op({ op: "ladder", items: picked, key: k, values: vals });
      if (r) { if (dlg) dlg.close(); } else update();
    };
    const lab = (text, el) => h("label", { class: "seq-lab" }, h("span", {}, text), el);
    dlg = dialog("Sweep", [
      h("div", { class: "seq-ticks", role: "group", "aria-label": "Blocks", "data-region": "dialog" }, ...ticks.map((t) => t.row)),
      h("label", { class: "seq-lab" }, h("span", {}, "Setting"), h("span", { class: "seq-line" }, key, unit)), options,
      h("div", { class: "seq-range" }, lab("From", from), lab("To", to), lab("Step", step)),
      lab("List", listed),
    ], [count, cancelButton(() => dlg), add], submit);
    update();
    key.focus();
  }

  // ---------- Save as…: the sequence kept under a name, to Load again (it appends)
  function openSave() {
    const h = K.h;
    const fit = (s) => String(s || "").replace(/[^A-Za-z0-9 _.-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 61).trim();
    const name = h("input", { class: "kfield", type: "text", maxlength: "61", spellcheck: "false", autocomplete: "off",
                              value: fit(SEQ.q && SEQ.q.name) || "sequence", "aria-label": "Name" });
    const save = h("button", { class: "btn primary", type: "submit" }, "Save");
    let dlg = null;
    name.addEventListener("input", () => { save.disabled = !name.value.trim(); });
    dlg = dialog("Save as", [name], [h("span"), cancelButton(() => dlg), save], async () => {
      const v = name.value.trim();
      if (!v) return;
      save.disabled = true;
      const r = await op({ op: "save", name: v });
      if (r) { if (dlg) dlg.close(); say("Saved"); } else save.disabled = false;
    });
    name.focus();
    name.select();
  }

  document.addEventListener("kelvin:ready", () => guard(init));
})();
