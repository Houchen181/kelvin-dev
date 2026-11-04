"use strict";
// The first start (welcome.js). A lab with nothing in it yet (no lab.yaml, no instruments) opens on this page, over the
// app, in AlphainZ's wizard form: three steps, each one card of rows (a title, its value under it, the control at the
// right), with no sentences. Workspace: the lab's scripts folders, the data folder, and the scripts to import as
// functions. Model: an API key, ChatGPT, Claude Code, a local model. Instruments: a scan, and the ones found added to
// the lab. What a step asks for is done by Next (Done: by every step); Back and the dots (to a step already seen) only
// move. Done or Skip (a quiet link at the top right): never again in this browser (lh.welcome), and Home shows. The
// page fits the window: a list too long for it (the scripts, the instruments found) scrolls in its own region. While it
// shows, body has the class welcome-on (app.js's setupWait waits for it) and the app behind it is inert.
(() => {
  let K = null;   // window.Kelvin, from kelvin:ready
  const SETUP_BUSY = new Set(["connecting", "scanning", "drafting", "reading"]);
  const STEPS = [["workspace", "Workspace"], ["model", "Model"], ["instruments", "Instruments"]];
  const KEYS = [["anthropic_api_key", "Claude"], ["openai_api_key", "OpenAI"], ["gemini_api_key", "Gemini"]];
  const W = {
    boot: null, state: null, gated: false, open: false, step: 0, seen: 0, busy: 0, moving: false, els: {}, setupKey: "",
    changed: false,   // a key, the local model or ChatGPT is new: the page's list of models is redone at the end
    scripts: { folders: [], counts: null, msg: "", full: "", note: "" },
    data: { path: null, msg: "", full: "" },
    files: { list: null, ticked: new Set(), have: new Set(), failed: new Map(), done: 0, todo: 0, running: false,
             labels: null },   // labels: each folder's name in the list, when its scripts come from several
    keys: {}, typed: {}, keyMsg: {},
    gpt: { signed: null, waiting: false, timer: 0, msg: "" },
    cc: null,
    local: { url: "", typed: "", msg: "" },
    scan: { phase: "idle", found: [], ticked: new Set(), added: new Set(), msg: "", full: "" },
  };

  // ------------------------------------------------------------------ helpers
  const warn = (e) => { try { console.warn("welcome:", e); } catch (x) { /* nowhere to say it */ } };
  function safe(fn, ...args) {   // what a person does here never throws into the page
    try {
      const r = fn(...args);
      if (r && typeof r.catch === "function") r.catch(warn);
      return r;
    } catch (e) { warn(e); return null; }
  }
  const plural = (n, one) => n + " " + one + (n === 1 ? "" : "s");
  const norm = (p) => String(p || "").trim().replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
  const unquote = (p) => String(p || "").trim().replace(/^"(.*)"$/, "$1").trim();
  const short = (e) => {   // a refusal in a few words where it can be (the whole of it is the line's title)
    const text = String((e && e.message) || e || "");
    return /does not exist|no such file|cannot find/i.test(text) ? "Not found" : text;
  };
  const setupBusy = () => SETUP_BUSY.has((((W.state || {}).setup) || {}).state);
  const ownKeys = () => !(W.boot && W.boot.settings && W.boot.settings.own_keys === false);

  function btn(label, cls, onclick, opts) {
    const o = opts || {};
    const b = K.h("button", { class: "btn" + (cls ? " " + cls : ""), type: "button", "data-f": o.f || null }, label);
    b.disabled = !!o.disabled;
    if (onclick) b.addEventListener("click", (e) => safe(onclick, b, e));
    return b;
  }
  const chip = (text, tone, title) => K.h("span", { class: "chip" + (tone ? " " + tone : ""), title: title || null }, text);
  const spinner = () => K.h("span", { class: "spinner", "aria-hidden": "true" });
  const value = (text, tone, title, wrap) =>
    K.h("div", { class: "wl-value" + (tone ? " " + tone : "") + (wrap ? " wrap" : ""), title: title || null }, text);
  function row(title, val, ctl, cls) {   // a title, its value under it, the controls at the right
    const kids = (ctl || []).filter(Boolean);
    return K.h("div", { class: "wl-row" + (cls ? " " + cls : "") },
      K.h("div", { class: "wl-text" }, K.h("div", { class: "wl-title" }, title), val || null),
      kids.length ? K.h("div", { class: "wl-ctl" }, ...kids) : null);
  }
  function block(key, ...rows) {   // the rows redrawn together, by key; hidden when there are none
    const kids = rows.filter(Boolean);
    return K.h("div", { class: "wl-block", "data-row": key, hidden: !kids.length }, ...kids);
  }
  function item(box, label, sub, end, title) {   // a ticked line of a list: a script to import, an instrument found
    return K.h("label", { class: "wl-row wl-item" + (box.disabled ? " off" : ""), title: title || null },
      box, K.h("span", { class: "wl-label" }, label), K.h("span", { class: "wl-sub" }, sub || ""), end || null);
  }
  function tick(checked, disabled, label, onchange) {
    const box = K.h("input", { type: "checkbox", class: "wl-check", "aria-label": label });
    box.checked = !!checked;
    box.disabled = !!disabled;
    if (onchange) box.addEventListener("change", () => safe(onchange, box.checked));
    return box;
  }
  // one block drawn again in place; the person's focus stays on the same control when it is still there
  function redraw(key) {
    const group = W.els.group;
    const old = group && group.querySelector('[data-row="' + key + '"]');
    if (!old || !BLOCKS[key]) return;
    let fresh;
    try { fresh = BLOCKS[key](); } catch (e) { warn(e); return; }
    const active = document.activeElement;
    const had = old.contains(active) ? (active.getAttribute("data-f") || "") : null;
    old.replaceWith(fresh);
    if (had === null) return;
    const same = had && fresh.querySelector('[data-f="' + had + '"]');
    const to = same && !same.disabled ? same : W.els.next;
    if (to && !to.disabled) to.focus({ preventScroll: true });
  }
  // after an action whose button waited (a disabled button loses the focus): the focus back on this page, on the
  // control named (when it is there) or on Next; never taken from where the person has put it meanwhile
  function refocus(f) {
    if (!W.open || !W.els.root || document.querySelector("dialog[open]")) return;
    const a = document.activeElement;
    if (a && a !== document.body) return;
    const el = (f && W.els.group.querySelector('[data-f="' + f + '"]')) || W.els.next;
    if (el && !el.disabled) el.focus({ preventScroll: true });
  }
  function field(key) { return W.els.group && W.els.group.querySelector('[data-f="' + key + '-field"]'); }
  function markBad(key) {
    const el = field(key);
    if (el) { el.classList.add("bad"); el.focus(); }
  }
  function begin(button) {   // an action under way: its button waits, and so does the way on
    W.busy += 1;
    footer();
    if (button) { button.disabled = true; button.before(spinner()); }
  }
  function hold(key) {   // and the other buttons of its block wait too, until the block is drawn again
    const el = W.els.group && W.els.group.querySelector('[data-row="' + key + '"]');
    if (el) el.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  }
  function end() { W.busy = Math.max(0, W.busy - 1); footer(); }

  // ------------------------------------------------------------------ step 1: Workspace
  // the lab's scripts folders: Add folder… (the computer's folder picker in the Kelvin app, Kelvin's folder browser in
  // a browser), then a row for each with Remove
  function scriptsBlock() {
    const st = W.scripts;
    let val;
    if (st.msg) val = value(st.msg, "bad", st.full);
    else if (st.note) val = value(st.note, "", st.note, true);   // what was folded: a folder there already, or in another
    else if (st.folders.length) {   // their scripts (the runnable ones listed, else the survey's) · the instruments in them
      const parts = [], listed = W.files.list;
      if (listed || st.counts) parts.push(plural(listed ? listed.length : st.counts.scripts, "script"));
      if (st.counts && st.counts.instruments) parts.push(plural(st.counts.instruments, "instrument"));
      val = parts.length ? value(parts.join(" · ")) : null;
    } else val = value("—");
    const ctl = [btn("Add folder…", "small", chooseScripts, { f: "scripts-pick" })];
    return block("scripts", row("Scripts", val, ctl, ""), ...st.folders.map(folderRow));
  }
  function folderRow(path, i) {   // one of them: its name, its whole path, Remove (drawn as Settings draws it)
    const name = String(path).replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
    const remove = K.h("button", { class: "link danger", type: "button", "data-f": "scripts-remove-" + i,
                                   "aria-label": "Remove " + path }, "Remove");
    remove.addEventListener("click", () => safe(removeScripts, path, remove));
    return K.h("div", { class: "wl-row wl-item off", title: path }, K.h("span", { class: "wl-label" }, name),
      K.h("span", { class: "wl-sub" }, path), remove);
  }
  async function chooseScripts(button) {
    const folders = W.scripts.folders;
    const path = await K.pickFolder(folders[folders.length - 1] || "", "Choose a folder of the lab's scripts");
    if (path) await useScripts(path, button);
  }
  // a folder of the lab's scripts added: Kelvin reads them all (never runs them) for its instruments; then the
  // runnable ones to import
  async function useScripts(typed, button) {
    const st = W.scripts, folder = unquote(typed);
    if (!folder) return false;
    let ok = false;
    begin(button);
    hold("scripts");
    try {
      const r = await K.api("/api/instruments/scripts-folder", { body: { folder } });
      Object.assign(st, { folders: r.folders || [], counts: { scripts: r.scripts || 0, instruments: r.instruments || 0 },
                          msg: "", full: "", note: (r.folded || []).join("; ") });
      W.files.list = null;
      W.files.failed.clear();
      ok = true;
    } catch (e) {
      Object.assign(st, { msg: short(e), full: e.message, note: "" });
    } finally {
      end();
      redraw("scripts");
      refocus(ok ? "" : "scripts-pick");
    }
    if (ok) await loadFiles();
    return ok;
  }
  async function removeScripts(path, button) {
    const st = W.scripts;
    let ok = false;
    begin(button);
    hold("scripts");
    try {
      const r = await K.api("/api/instruments/scripts-folder", { body: { remove: path } });
      Object.assign(st, { folders: r.folders || [], counts: null, msg: "", full: "", note: "" });
      ok = true;
    } catch (e) {
      Object.assign(st, { msg: short(e), full: e.message, note: "" });
    } finally {
      end();
      redraw("scripts");
      refocus("scripts-pick");
    }
    if (ok) await loadFiles();
    return ok;
  }
  function dataBlock() {
    const st = W.data;
    const val = st.msg ? value(st.msg, "bad", st.full)
      : value(st.path === null ? "…" : st.path || "—", "", st.path || null, true);
    const ctl = st.path === null ? [] : [btn("Change…", "small", chooseData, { f: "data-pick" })];
    return block("data", row("Data", val, ctl, ""));
  }
  async function loadData() {
    const st = W.data;
    try {
      const r = await K.api("/api/data-folder");
      st.path = r.path || "";
      if (r.problem) Object.assign(st, { msg: String(r.problem), full: String(r.problem) });
    } catch (e) { Object.assign(st, { path: "", msg: short(e), full: e.message }); }
    redraw("data");
  }
  async function chooseData(button) {
    const path = await K.pickFolder(W.data.path || "", "Choose the data folder");
    if (path) await useData(path, button);
  }
  async function useData(typed, button) {
    const st = W.data, path = unquote(typed);
    if (!path) return false;
    if (norm(path) === norm(st.path)) { Object.assign(st, { msg: "", full: "" }); redraw("data"); return true; }
    let ok = false;
    begin(button);
    try {
      const r = await K.api("/api/data-folder", { body: { path, move: false } });
      Object.assign(st, { path: r.path || path, msg: "", full: "" });
      ok = true;
    } catch (e) {
      Object.assign(st, { msg: short(e), full: e.message });
    } finally {
      end();
      redraw("data");
      refocus(ok ? "" : "data-pick");
    }
    return ok;
  }
  // the runnable scripts under the scripts folders (library op files), all ticked; Next imports the ticked ones
  async function loadFiles() {
    const f = W.files, seq = (f.seq = (f.seq || 0) + 1);   // only the latest folders' list is kept
    if (!W.scripts.folders.length) {
      f.list = null;
    } else {
      try {
        const [r, lib] = await Promise.all([K.api("/api/library", { body: { op: "files" } }),
          K.api("/api/library").catch(() => null)]);
        if (seq !== f.seq) return;
        f.list = Array.isArray(r.files) ? r.files : [];
        f.labels = Array.isArray(r.folders) && r.folders.length > 1 ? K.folderLabels(r.folders) : null;
        f.have = new Set(((lib && lib.scripts) || []).map((s) => norm(s.path)));
        f.ticked = new Set(f.list.filter((x) => !f.have.has(norm(x.path))).map((x) => x.path));
      } catch (e) {   // no list from this Kelvin: nothing offered to import here
        if (seq !== f.seq) return;
        f.list = null;
        warn(e);
      }
    }
    redraw("files");
    redraw("scripts");   // its count of scripts is the list's now
  }
  function filesCount() {   // the Import row's value: what Next imports, or what came in
    const f = W.files, list = f.list || [];
    if (f.running) return [f.done + " / " + f.todo, ""];
    if (!list.length) return ["None", ""];
    const fresh = list.filter((x) => !f.have.has(norm(x.path)));
    const n = fresh.filter((x) => f.ticked.has(x.path)).length, added = list.length - fresh.length;
    if (!n && added) return [added + " added", "ok"];
    return [n === fresh.length ? plural(n, "file") : n + " of " + fresh.length, ""];
  }
  function setFilesCount() {
    const el = W.els.group && W.els.group.querySelector('[data-row="files"] [data-v="count"]');
    if (!el) return;
    const [text, tone] = filesCount();
    el.textContent = text;
    el.className = "wl-value" + (tone ? " " + tone : "");
  }
  // a list in the card, every line of it: it takes the room left under the rows above it, and scrolls there when longer
  const list = (...items) => K.h("div", { class: "wl-list", "data-region": "welcome-list" }, ...items);
  function filesBlock() {
    const f = W.files;
    if (!W.scripts.folders.length || !f.list) return block("files");
    const [text, tone] = filesCount();
    const count = value(text, tone);
    count.setAttribute("data-v", "count");
    return block("files", row("Import", count), f.list.length ? list(...f.list.map(fileItem)) : null);
  }
  function fileItem(x) {   // its name, then where it is in its folder (after the folder's name, from several folders)
    const f = W.files, done = f.have.has(norm(x.path)), failed = f.failed.get(x.path);
    const rel = String(x.rel || x.name || x.path || "").replace(/\\/g, "/");
    const cut = rel.lastIndexOf("/");
    const name = x.name || rel.slice(cut + 1);
    const where = [f.labels && f.labels.get(x.folder), cut > 0 ? rel.slice(0, cut) : ""].filter(Boolean).join(" · ");
    const box = tick(done || f.ticked.has(x.path), done || f.running, name, (on) => {
      if (on) f.ticked.add(x.path); else f.ticked.delete(x.path);
      setFilesCount();
    });
    return item(box, name, where, done ? chip("Added", "ok") : failed ? chip("Failed", "bad", failed) : null, x.path);
  }
  async function importTicked() {   // false: one failed, shown on its line (Next again goes on)
    const f = W.files;
    if (!W.scripts.folders.length || !f.list || f.running) return !f.running;
    const todo = f.list.filter((x) => f.ticked.has(x.path) && !f.have.has(norm(x.path)));
    if (!todo.length) return true;
    Object.assign(f, { running: true, done: 0, todo: todo.length });
    f.failed.clear();
    W.busy += 1;
    footer();
    redraw("files");
    for (const x of todo) {
      try {
        await K.api("/api/library", { body: { op: "import", path: x.path } });
        f.have.add(norm(x.path));
      } catch (e) {
        f.failed.set(x.path, e.message || "Failed");
        f.ticked.delete(x.path);
      }
      f.done += 1;
      setFilesCount();
    }
    f.running = false;
    end();
    redraw("files");
    return !f.failed.size;
  }

  // ------------------------------------------------------------------ step 2: Model
  function keyBlock(name, label) {
    const key = "key-" + name;
    if (!ownKeys()) return block(key);
    if (W.keys[name]) return block(key, row(label, null, [chip("Set ✓", "ok")]));
    const input = K.h("input", { class: "kfield", type: "password", autocomplete: "off", spellcheck: "false",
                                 placeholder: "API key", "aria-label": label + " API key", "data-f": key + "-field" });
    input.value = W.typed[name] || "";
    const save = btn("Save", "small", (b) => saveKey(name, b), { f: key + "-save" });
    input.addEventListener("input", () => { W.typed[name] = input.value; input.classList.remove("bad"); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); e.stopPropagation(); safe(saveKey, name, save); }
    });
    const msg = W.keyMsg[name];
    return block(key, row(label, msg ? value(msg, "bad", msg) : null, [input, save], "wide"));
  }
  async function saveKey(name, button, quiet) {
    const text = String(W.typed[name] || "").trim();
    if (!text) { if (!quiet) markBad("key-" + name); return true; }
    let ok = false;
    begin(button);
    try {
      const r = await K.api("/api/settings", { body: { [name]: text } });
      W.keys = Object.assign({}, W.keys, (r.settings && r.settings.keys) || {});
      if (!W.keys[name]) throw new Error("Not saved");   // this Kelvin takes no keys of its own
      delete W.typed[name];
      delete W.keyMsg[name];
      W.changed = true;
      ok = true;
    } catch (e) {
      W.keyMsg[name] = e.message;
    } finally {
      end();
      redraw("key-" + name);
      if (!quiet) refocus(ok ? "" : "key-" + name + "-field");
    }
    return ok;
  }
  function gptBlock() {
    const g = W.gpt;
    if (g.signed) return block("gpt", row("ChatGPT", null, [chip("Signed in ✓", "ok")]));
    return block("gpt", row("ChatGPT", g.msg ? value(g.msg, "bad", g.msg) : null,
      [g.waiting ? spinner() : null, btn("Continue", "small", gptStart, { f: "gpt-go" })]));
  }
  async function gptStatus() {
    try { W.gpt.signed = !!(await K.api("/api/chatgpt")).signed_in; } catch (e) { W.gpt.signed = false; }
    redraw("gpt");
  }
  // Kelvin opens OpenAI's sign-in page in this computer's browser itself (app.py chatgpt_signin): a tab opened here
  // as well would be a second one. Then the sign-in is asked after every 2 s, for up to 10 minutes.
  async function gptStart() {
    const g = W.gpt;
    g.msg = "";
    try { await K.api("/api/chatgpt/signin", { body: {} }); } catch (e) { g.msg = e.message; redraw("gpt"); return; }
    g.waiting = true;
    redraw("gpt");
    clearInterval(g.timer);
    const until = Date.now() + 10 * 60 * 1000;
    g.timer = setInterval(async () => {
      if (!W.open || Date.now() > until) {
        clearInterval(g.timer);
        g.waiting = false;
        redraw("gpt");
        return;
      }
      let st;
      try { st = await K.api("/api/chatgpt"); } catch (e) { return; }
      if (!st.signed_in || !W.open) return;
      clearInterval(g.timer);
      Object.assign(g, { waiting: false, signed: true });
      W.changed = true;
      redraw("gpt");
    }, 2000);
  }
  function ccBlock() {
    if (!(W.boot && W.boot.claude_code)) return block("cc");
    const c = W.cc;
    const state = !c ? null : c.signed_in ? chip("Signed in ✓", "ok") : c.enabled === false ? chip("Off", "line")
      : !c.cli ? chip("Not found", "line") : chip("Not signed in", "warn");
    return block("cc", row("Claude Code", null, [state]));
  }
  async function ccStatus() {
    try { W.cc = await K.api("/api/claude-code"); } catch (e) { W.cc = { enabled: true, cli: null, signed_in: false }; }
    redraw("cc");
  }
  function localBlock() {
    const l = W.local;
    if (l.url) return block("local", row("Local", value(l.url, "", l.url), [chip("Set ✓", "ok")]));
    const input = K.h("input", { class: "kfield", type: "url", autocomplete: "off", spellcheck: "false",
                                 placeholder: "http://localhost:11434/v1", "aria-label": "Local model address",
                                 "data-f": "local-field" });
    input.value = l.typed;
    const save = btn("Save", "small", (b) => saveLocal(b), { f: "local-save" });
    input.addEventListener("input", () => { l.typed = input.value; input.classList.remove("bad"); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); e.stopPropagation(); safe(saveLocal, save); }
    });
    return block("local", row("Local", l.msg ? value(l.msg, "bad", l.msg) : null, [input, save], "wide"));
  }
  async function saveLocal(button, quiet) {
    const l = W.local, url = String(l.typed || "").trim();
    if (!url) { if (!quiet) markBad("local"); return true; }
    let ok = false;
    begin(button);
    try {
      const r = await K.api("/api/settings", { body: { local_base_url: url } });
      Object.assign(l, { url: (r.settings && r.settings.local_base_url) || url, typed: "", msg: "" });
      W.changed = true;
      ok = true;
    } catch (e) {
      l.msg = e.message;
    } finally {
      end();
      redraw("local");
      if (!quiet) refocus(ok ? "" : "local-field");
    }
    return ok;
  }

  // ------------------------------------------------------------------ step 3: Instruments
  const canAdd = (f) => !!f.card_id && !f.in_lab;
  const toAdd = () => W.scan.found.filter((f) => canAdd(f) && W.scan.ticked.has(f.address) && !W.scan.added.has(f.address));
  function setupBlock() {   // Kelvin's own setup at work (connecting, scanning, writing a card, reading settings)
    const st = ((W.state || {}).setup) || {};
    if (!SETUP_BUSY.has(st.state)) return block("setup");
    const msg = String(st.message || "");
    return block("setup", row("Setting up", msg ? value(msg, "", msg) : null, [spinner()]));
  }
  function scanBlock() {
    const s = W.scan, busy = s.phase === "scanning" || s.phase === "adding", setup = setupBusy();
    let val = null;
    if (s.msg) val = value(s.msg, "bad", s.full);
    else if (s.phase === "none") val = value("None found");
    else if (s.phase === "found") val = value(s.found.length + " found");
    else if (s.phase === "added") val = value(s.added.size + " added", "ok");
    const ctl = [busy ? spinner() : null];
    if (s.phase === "idle" || s.phase === "scanning") {
      ctl.push(btn("Scan", "small primary", scan, { f: "scan", disabled: busy || setup }));
    } else ctl.push(btn("Scan again", "small", scan, { f: "scan", disabled: busy || setup }));
    if (s.phase !== "scanning" && s.found.some((f) => canAdd(f) && !s.added.has(f.address))) {
      ctl.push(btn("Add", "small primary", (b) => addFound(b), { f: "add", disabled: busy || !toAdd().length }));
    }
    return block("scan", row("Instruments", val, ctl));
  }
  function foundBlock() {
    const s = W.scan;
    if (s.phase === "scanning" || !s.found.length) return block("found");
    return block("found", list(...s.found.map((f) => {
      const added = s.added.has(f.address);
      const label = f.in_lab || (f.card_id ? f.model || f.card_id
        : String(f.idn || "").split(",").slice(0, 2).join(" ").trim() || "Unknown");
      const box = tick(added || !!f.in_lab || (canAdd(f) && s.ticked.has(f.address)),
        added || !canAdd(f) || s.phase === "adding", label + " " + f.address, (on) => {
          if (on) s.ticked.add(f.address); else s.ticked.delete(f.address);
          const add = W.els.group.querySelector('[data-f="add"]');
          if (add) add.disabled = !toAdd().length;
        });
      return item(box, label, f.address, added ? chip("Added", "ok") : f.in_lab ? chip("In lab", "line") : null,
        f.idn || f.how || null);
    })));
  }
  async function scan() {
    const s = W.scan;
    Object.assign(s, { phase: "scanning", found: [], added: new Set(), msg: "", full: "" });
    W.busy += 1;
    footer();
    redraw("scan");
    redraw("found");
    try {
      const r = await K.api("/api/instruments/scan", { body: {} });
      s.found = Array.isArray(r.found) ? r.found : [];
      s.ticked = new Set(s.found.filter(canAdd).map((f) => f.address));
      s.phase = s.found.length ? "found" : "none";
    } catch (e) {   // 503: no VISA on this computer, so nothing to find
      s.phase = e.status === 503 ? "none" : "error";
      if (e.status !== 503) Object.assign(s, { msg: short(e), full: e.message });
    }
    end();
    redraw("scan");
    redraw("found");
    refocus(toAdd().length ? "add" : "scan");
  }
  // the ticked ones into the lab config (draft), saved and connected (POST /api/lab, the owner's PIN asked if set)
  async function addFound(button) {
    const s = W.scan, chosen = toAdd();
    if (!chosen.length) return true;
    const items = chosen.map((f) => ({ name: f.suggested || "instr", card: f.card_id, address: f.address,
                                       note: String(f.idn || "").slice(0, 60) }));
    s.phase = "adding";
    Object.assign(s, { msg: "", full: "" });
    begin(button);
    redraw("scan");
    redraw("found");
    try {
      const lab = await K.api("/api/lab");
      const draft = await K.api("/api/instruments/draft", { body: { items, base: lab.yaml || "" } });
      await K.api("/api/lab", { body: { yaml: draft.yaml, why: "first start" } });
      for (const f of chosen) s.added.add(f.address);
      s.phase = "added";
      safe(K.pollState);
      return true;
    } catch (e) {
      Object.assign(s, { phase: "found", msg: short(e), full: e.message });
      return false;
    } finally { end(); redraw("scan"); redraw("found"); refocus(button ? (s.phase === "added" ? "" : "add") : null); }
  }

  // ------------------------------------------------------------------ the page
  const BLOCKS = {
    scripts: scriptsBlock, data: dataBlock, files: filesBlock,
    gpt: gptBlock, cc: ccBlock, local: localBlock,
    setup: setupBlock, scan: scanBlock, found: foundBlock,
  };
  for (const [name, label] of KEYS) BLOCKS["key-" + name] = () => keyBlock(name, label);
  const ORDER = {
    workspace: ["scripts", "data", "files"],
    model: KEYS.map(([name]) => "key-" + name).concat(["gpt", "cc", "local"]),
    instruments: ["setup", "scan", "found"],
  };
  function build(root) {
    const h = K.h;
    const dots = STEPS.map(([, title], i) => h("button", { class: "wl-dot", type: "button", role: "tab", title,
                                                          "aria-label": title, onclick: () => safe(goTo, i) }));
    const where = h("span", { class: "wl-where" });
    const group = h("div", { class: "wl-group", role: "group" });
    const skip = h("button", { class: "wl-skip", type: "button", "data-f": "skip", title: "Settings has all of this",
                               onclick: () => safe(finish) }, "Skip");
    const back = btn("Back", "ghost", () => goTo(W.step - 1), { f: "back" });
    const next = btn("Next", "primary", () => forward(), { f: "next" });
    const spin = spinner();
    spin.hidden = true;
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-labelledby", "wl-title");
    root.replaceChildren(h("div", { class: "wl-col" },
      h("header", { class: "wl-head" },
        h("span", { class: "logo", "aria-hidden": "true", html: K.MARK }),
        h("div", { class: "wl-titles" }, h("h1", { class: "display wl-h1", id: "wl-title" }, "Welcome to Kelvin"),
          W.boot && W.boot.version ? chip("v" + W.boot.version, "line") : null), skip),
      h("div", { class: "wl-steps" }, h("div", { class: "wl-dots", role: "tablist", "aria-label": "Steps" }, ...dots), where),
      group,
      h("footer", { class: "wl-foot" }, back, h("span", { class: "spacer" }), spin, next)));
    W.els = { root, dots, where, group, skip, back, next, spin };
  }
  function footer() {
    const e = W.els;
    if (!e.next) return;
    const busy = W.busy > 0 || W.moving;
    e.back.hidden = W.step === 0;   // nothing before the first step
    e.next.textContent = W.step === STEPS.length - 1 ? "Done" : "Next";
    e.back.disabled = e.next.disabled = e.skip.disabled = busy;
    e.dots.forEach((d, i) => { d.disabled = busy || i > W.seen; });   // only to a step already seen
    e.spin.hidden = !W.moving;   // an action in the card shows its own
  }
  function render() {
    const [id, title] = STEPS[W.step];
    W.els.dots.forEach((d, i) => {
      d.classList.toggle("on", i === W.step);
      d.setAttribute("aria-selected", String(i === W.step));
    });
    W.els.where.textContent = (W.step + 1) + " / " + STEPS.length + " · " + title;
    W.els.group.setAttribute("aria-label", title);
    W.els.group.replaceChildren(...ORDER[id].map((key) => BLOCKS[key]()));
    footer();
    if (id === "model") {
      if (W.gpt.signed === null) gptStatus();
      if (W.boot && W.boot.claude_code && !W.cc) ccStatus();
    }
  }
  // what a step asks for, done: the ticked scripts imported, what was typed saved, the ticked instruments added.
  // false: stay (a refusal on its row)
  async function apply(id) {
    if (id === "workspace") return importTicked();   // its folders were kept as they were chosen
    if (id === "model") {
      let ok = true;
      for (const [name] of KEYS) if (!W.keys[name]) ok = (await saveKey(name, null, true)) && ok;
      if (!W.local.url) ok = (await saveLocal(null, true)) && ok;
      return ok;
    }
    return toAdd().length ? addFound(null) : true;
  }
  // Next: this step's, then the next step. Done: every step's (one reached by its dot was never left by Next), then
  // the end; a step that refuses is shown, its refusal on its row
  async function forward() {
    if (!W.open || W.moving || W.busy) return;
    const last = W.step === STEPS.length - 1;
    W.moving = true;
    footer();
    let ok = true, at = W.step;
    try {
      for (const i of last ? STEPS.map((s, k) => k) : [W.step]) {
        at = i;
        ok = await apply(STEPS[i][0]);
        if (!ok) break;
      }
    } catch (e) { ok = false; warn(e); }
    W.moving = false;
    footer();
    if (!W.open) return;
    if (ok && last) { finish(); return; }
    toStep(ok ? W.step + 1 : at);
    refocus();
  }
  // Back and the dots: a step already seen, as it was left; nothing is imported or saved on the way
  function goTo(target) {
    if (!W.open || W.moving || W.busy || target === W.step || target < 0 || target > W.seen) return;
    toStep(target);
    refocus();
  }
  function toStep(step) {
    if (step === W.step) return;
    W.step = step;
    W.seen = Math.max(W.seen, step);
    render();
    W.els.root.scrollTop = 0;
  }
  function show() {
    const root = document.getElementById("welcome");
    if (!root) return;
    const set = (W.boot && W.boot.settings) || {};
    W.keys = Object.assign({}, set.keys || {});
    W.local.url = set.local_base_url || "";
    W.scripts.folders = [].concat(set.lab_scripts || []);   // a list; one folder as text from an older Kelvin
    W.open = true;
    build(root);
    document.body.classList.add("welcome-on");
    // the same state opened the setup's own dialog a moment ago (pollState asks setupWait before it tells the parts
    // kept apart): closed in the same task, so it is never seen, and closed it stays closed; this page asks instead
    const wait = document.getElementById("setup-wait");
    if (wait && wait.open) wait.close();
    const app = document.getElementById("app");
    if (app) app.inert = true;   // the app behind the page: no focus, no clicks
    root.hidden = false;
    render();
    W.els.next.focus({ preventScroll: true });
    loadData();
    if (W.scripts.folders.length) loadFiles();
  }
  function hide() {
    W.open = false;
    clearInterval(W.gpt.timer);
    const root = W.els.root || document.getElementById("welcome");
    if (root) { root.hidden = true; root.replaceChildren(); }
    document.body.classList.remove("welcome-on");
    const app = document.getElementById("app");
    if (app) app.inert = false;
  }
  // Done or Skip: not again in this browser; the chat. With no scripts folder added, the first setup's question about
  // the lab's scripts is answered "none", as here, so it doesn't come back as a dialog. A new key, local model or
  // ChatGPT sign-in: the page's list of models is redone (Kelvin.refreshModels when the page lends it, else a reload)
  async function finish() {
    if (!W.open || W.finishing) return;
    W.finishing = true;
    K.store("lh.welcome", "done");
    W.typed = {};
    W.local.typed = "";
    clearInterval(W.gpt.timer);
    if (W.state && W.state.ask_scripts && !W.scripts.folders.length) {
      try { await K.api("/api/instruments/scripts-folder", { body: { folder: "" } }); } catch (e) { warn(e); }
    }
    safe(K.setMode, "chat");
    if (W.changed && typeof K.refreshModels !== "function") { location.reload(); return; }
    hide();
    if (W.changed) K.api("/api/bootstrap").then((b) => K.refreshModels(b)).catch(warn);
  }
  function wanted(s) {
    const info = s.lab_info || {};
    return info.kind === "none" && !info.has_file && !(s.instruments || []).length && K.store("lh.welcome") !== "done";
  }
  function onState(s) {
    W.state = s;
    if (!K) return;   // before kelvin:ready: kept for it
    if (!W.gated) {
      W.gated = true;
      if (wanted(s)) show();
      return;
    }
    if (!W.open) return;
    const st = s.setup || {};
    const key = [st.state, st.message, st.step].join("|");
    if (key === W.setupKey) return;
    W.setupKey = key;
    redraw("setup");   // in place; nothing else is drawn again
    redraw("scan");    // a scan waits while Kelvin's own setup runs
  }

  document.addEventListener("kelvin:ready", (e) => safe(() => {
    K = window.Kelvin;
    W.boot = e.detail || {};
    if (W.state && !W.gated) onState(W.state);
  }));
  document.addEventListener("kelvin:state", (e) => safe(onState, e.detail || {}));
  document.addEventListener("keydown", (e) => {   // Enter: Next, wherever the person is not typing or on a button
    if (!W.open || e.key !== "Enter" || e.isComposing || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const t = e.target;
    if (t && t.closest && t.closest("button, a, textarea, select, [contenteditable], input:not([type=checkbox])")) return;
    if (document.querySelector("dialog[open]")) return;   // a PIN asked meanwhile
    e.preventDefault();
    safe(forward);
  });
})();
