"use strict";
(() => {
  const token = decodeURIComponent(location.hash.slice(1));
  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  const S = { compare: [], lastReply: null, chats: [], projects: [], incognito: false, chatId: null, groups: [], labels: {}, labOpen: false, settings: null, approvals: new Map(),
    // the chats answering in this window, by chat id: {reply, from, steers (what the person wrote meanwhile, until
    // Kelvin reads it), title}; answering: every chat the portal says answers now (from any window, or Kelvin's own);
    // unread: answers that came while another chat was open; asking: chats with an approval waiting
    live: new Map(), answering: new Set(), unread: new Set(), asking: new Set() };
  Object.defineProperty(S, "streaming", { get: () => !!S.chatId && S.live.has(S.chatId) });   // this chat is answering
  const liveHere = () => S.live.get(S.chatId);

  // ------------------------------------------------------------------ helpers
  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v; // only ever given escaped or constant markup
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids) if (kid != null) node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return node;
  }
  const ICONS = {
    check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
    warn: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L1.8 18.1A2 2 0 0 0 3.5 21h17a2 2 0 0 0 1.7-2.9L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    chev: '<path d="M9 6l6 6-6 6"/>',
    up: '<path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    incognito: '<path d="M3 11h18"/><path d="M6 11l1.6-5.2h8.8L18 11"/><circle cx="7.5" cy="16" r="2.6"/><circle cx="16.5" cy="16" r="2.6"/><path d="M10.1 16h3.8"/>',
    memory: '<path d="M7 3h10a1 1 0 0 1 1 1v17l-6-4-6 4V4a1 1 0 0 1 1-1z"/>',
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
    dots: '<circle cx="5" cy="12" r="1.1"/><circle cx="12" cy="12" r="1.1"/><circle cx="19" cy="12" r="1.1"/>',
    folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z"/>',
    "folder-open": '<path d="M3 7.5V5.5A1.5 1.5 0 0 1 4.5 4H9l2 2h6.5A1.5 1.5 0 0 1 19 7.5V9"/><path d="M2.6 10.2A1 1 0 0 1 3.6 9h16.8a1 1 0 0 1 1 1.2l-1.5 7.5a2 2 0 0 1-2 1.6H6a2 2 0 0 1-2-1.6z"/>',
    pencil: '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    hand: '<path d="M12 22c4.4 0 8-3.6 8-8V9a2 2 0 0 0-4 0V5a2 2 0 0 0-4 0v-1a2 2 0 0 0-4 0v9l-1.6-1.6a2 2 0 0 0-2.9 2.8L8 18c1 2.4 2.4 4 4 4z"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>',
    // instrument kinds
    "source-meter": '<path d="M13 3L5 14h6l-1 7 8-11h-6l1-7z"/>',
    "lock-in": '<path d="M2 12c2-6 4-6 6 0s4 6 6 0 4-6 6 0"/><path d="M2 20h20"/>',
    "temperature-controller": '<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/><path d="M12 9v8"/>',
    "magnet-supply": '<path d="M6 3v8a6 6 0 0 0 12 0V3h-4v8a2 2 0 0 1-4 0V3z"/><path d="M6 7h4M14 7h4"/>',
    vna: '<path d="M5 13a7 7 0 0 1 14 0"/><path d="M8.5 13a3.5 3.5 0 0 1 7 0"/><path d="M12 13v8"/>',
    laser: '<rect x="2" y="9" width="9" height="6" rx="1.5"/><path d="M11 12h11"/><path d="M16 7l1.5 2M16 17l1.5-2"/>',
    "motion-stage": '<path d="M3 12h18M6 9l-3 3 3 3M18 9l3 3-3 3"/><path d="M9 16h6v4H9z"/>',
    instrument: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 15h4M15 10h2"/>',
  };
  // Kelvin's mark: a sleeping cat and a snowflake, white on the ice-blue tile
  const MARK = '<svg viewBox="0 0 100 100" aria-hidden="true"><g transform="translate(9.8 4.9) scale(0.82)">' +
    '<path d="M24 20V38M16 24.5L32 33.5M16 33.5L32 24.5" stroke="#bfe0fb" stroke-width="3.4" stroke-linecap="round" fill="none"/>' +
    '<path d="M18 66Q16 86 40 86Q60 86 68 76" fill="none" stroke="#f4f9fe" stroke-width="7" stroke-linecap="round"/>' +
    '<ellipse cx="46" cy="62" rx="30" ry="19" fill="#f4f9fe" stroke="none"/>' +
    '<path d="M60 43L62 30L69 38ZM82 43L80 30L73 38Z" fill="#f4f9fe" stroke="none"/>' +
    '<circle cx="71" cy="50" r="14" fill="#f4f9fe" stroke="none"/>' +
    '<path d="M63 51q3 2.6 6 0M73 51q3 2.6 6 0" stroke="#2f6fb0" stroke-width="2.4" stroke-linecap="round" fill="none"/></g></svg>';
  function mark() {
    const s = h("span", { class: "logo", "aria-hidden": "true" });
    s.innerHTML = MARK;
    return s;
  }
  function icon(name) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    s.innerHTML = ICONS[name];
    return s;
  }
  function svg(tag, attrs) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  }
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function store(key, value) {
    try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; }
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || (opts.body ? "POST" : "GET"),
      headers: { "X-Kelvin-Token": token, "Content-Type": "application/json" },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,   // an AbortController's: a request no longer wanted is dropped
    });
    if (!res.ok) {
      let msg = "HTTP " + res.status, data = null;
      try { data = await res.json(); msg = data.error || msg; } catch (e) { /* not JSON */ }
      if (res.status === 401 && data && data.signin) setTimeout(() => location.reload(), 300);   // signed out elsewhere
      if (res.status === 403 && data && data.pin && opts.body && !opts.noPin) {   // the lab owner's PIN: ask, try again
        const pin = await askPin(msg);
        if (pin) return api(path, Object.assign({}, opts, { body: Object.assign({}, opts.body, { pin }) }));
      }
      if (res.status === 403 && data && data.approver && opts.body) {   // a trainee's yes: an operator confirms it
        const given = await askApprover(msg, data.approvers || [], data.pin_set);
        if (given) return api(path, Object.assign({}, opts, { body: Object.assign({}, opts.body, given) }));
      }
      const err = new Error(msg);
      err.data = data;
      err.status = res.status;
      throw err;
    }
    return res.json();
  }
  // the lab owner's PIN (accounts.py OwnerPin), for what widens Kelvin's reach: limits, the lab config, auto mode,
  // unlocking Hands on the sample. "" when the person cancels.
  function askPin(why, label) {
    return new Promise((resolve) => {
      const input = h("input", { type: "password", inputmode: "numeric", autocomplete: "off", maxlength: "12",
                                 "aria-label": label || "Lab owner's PIN" });
      const cancel = h("button", { class: "btn", type: "button" }, "Cancel");
      const form = h("form", { class: "pin-form" }, h("p", {}, why), input,
        h("div", { class: "pin-actions" }, cancel, h("button", { class: "btn primary", type: "submit" }, "OK")));
      const dlg = h("dialog", { class: "pin-dialog" }, form);
      document.body.append(dlg);
      const done = (v) => { if (dlg.open) dlg.close(); dlg.remove(); resolve(v); };
      form.addEventListener("submit", (e) => { e.preventDefault(); done(input.value.trim()); });
      cancel.addEventListener("click", () => done(""));
      dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(""); });
      dlg.showModal();
      input.focus();
    });
  }
  // With the lab's roles (accounts.py LabRoles), a trainee's yes on a request needs an operator or the owner to
  // confirm it here: their name and their account's password, or the owner's PIN. {approver} or {pin}; "" cancels.
  function askApprover(why, names, pinSet) {
    return new Promise((resolve) => {
      const who = h("select", { "aria-label": "Who confirms" },
        ...names.map((n) => h("option", { value: n }, n)),
        pinSet ? h("option", { value: "" }, "The lab owner's PIN") : null);
      const secret = h("input", { type: "password", autocomplete: "off", "aria-label": "Their password" });
      const label = () => { secret.placeholder = who.value ? who.value + "'s password" : "Lab owner's PIN"; };
      who.addEventListener("change", label);
      label();
      const cancel = h("button", { class: "btn", type: "button" }, "Cancel");
      const form = h("form", { class: "pin-form" }, h("p", {}, why), who, secret,
        h("div", { class: "pin-actions" }, cancel, h("button", { class: "btn primary", type: "submit" }, "Approve")));
      const dlg = h("dialog", { class: "pin-dialog" }, form);
      document.body.append(dlg);
      const done = (v) => { if (dlg.open) dlg.close(); dlg.remove(); resolve(v); };
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const value = secret.value;
        if (!value) return;
        done(who.value ? { approver: { name: who.value, password: value } } : { pin: value.trim() });
      });
      cancel.addEventListener("click", () => done(""));
      dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(""); });
      dlg.showModal();
      secret.focus();
    });
  }
  async function ownerPinState() {
    try {
      const r = await api("/api/owner-pin");
      $("owner-pin-state").textContent = r.set
        ? "Set: changing limits or the lab config, turning on auto mode and unlocking Hands on the sample ask for it."
        : "Not set: anyone at this computer can change limits, turn on auto mode and unlock Hands on the sample.";
      $("owner-pin-set").textContent = r.set ? "Change…" : "Set a PIN…";
      $("owner-pin-remove").hidden = !r.set;
      return r.set;
    } catch (e) { $("owner-pin-state").textContent = e.message; return false; }
  }
  async function ownerPinChange(remove) {
    const isSet = await ownerPinState();
    const old = isSet ? await askPin("The lab owner's PIN now:") : "";
    if (isSet && !old) return;
    const pin = remove ? "" : await askPin("A new PIN for the lab's owner, 4 to 12 digits:", "New PIN");
    if (!remove && !pin) return;
    try { await api("/api/owner-pin", { body: { pin, old }, noPin: true }); toast(remove ? "PIN removed." : "PIN set."); }
    catch (e) { toast(e.message); }
    ownerPinState();
    rolesState();
  }

  // ------------------------------------------------------------------ the lab's roles, the owner told, the history
  // web/governance.py serves them; the rules are accounts.py's LabRoles and configversions.py's.
  const ROLESUI = { data: null };
  const AN = { owner: "an owner", operator: "an operator", trainee: "a trainee" };
  async function rolesState() {
    let r;
    try { r = await api("/api/roles"); } catch (e) { $("roles-state").textContent = e.message; return; }
    ROLESUI.data = r;
    $("roles-box").hidden = !r.accounts;   // roles are people's: they need accounts
    $("roles-state").textContent = r.on
      ? "On" + (r.my_role ? ": you are " + AN[r.my_role] : "") + ". " + r.people.map((p) => p.name + ": " + p.role
          + (p.given ? "" : " (no role given)")).join("; ") + "."
      : "Off: everyone at this computer is equal." + (r.pin ? "" : " Roles need the lab owner's PIN.");
    $("roles-edit").textContent = r.on ? "Change…" : "Give roles…";
    $("roles-edit").disabled = !r.pin;
    $("owner-notify-state").textContent = r.owner_notify.length
      ? "To " + r.owner_notify.join(" and ") + ", and the list in the app."
      : "Only to the list in the app.";
    $("owner-notify-off").hidden = !r.owner_notify.length;
  }
  function editRoles() {
    const r = ROLESUI.data;
    if (!r) return;
    const rows = r.people.map((p) => {
      const sel = h("select", { "aria-label": "Role of " + p.name }, h("option", { value: "" }, "no role"),
        ...r.roles_list.map((x) => h("option", { value: x }, x)));
      sel.value = r.roles[p.name] || "";
      return { name: p.name, sel, el: h("li", { class: "role-row" }, h("span", {}, p.name), sel) };
    });
    $("roles-list").replaceChildren(...rows.map((x) => x.el));
    $("roles-list").hidden = $("roles-actions").hidden = false;
    $("roles-save").onclick = async () => {
      const roles = {};
      for (const x of rows) if (x.sel.value) roles[x.name] = x.sel.value;
      try {
        await api("/api/roles", { body: { roles } });
        toast(Object.keys(roles).length ? "Roles saved: anyone without one is a trainee." : "Roles off: everyone is equal again.");
        $("roles-list").hidden = $("roles-actions").hidden = true;
      } catch (e) { toast(e.message); }
      rolesState();
    };
  }
  async function ownerNotify(use) {
    try {
      await api("/api/roles/notify", { body: { use } });
      toast(use === "mine" ? "Safety changes now go where your notifications go." : "Safety changes go only to the list in the app.");
    } catch (e) { toast(e.message); }
    rolesState();
  }

  async function openHistory() {
    $("history-dialog").showModal();
    await loadHistory();
  }
  async function loadHistory() {
    const q = new URLSearchParams();
    for (const [key, id] of [["subject", "history-subject"], ["who", "history-who"]]) if ($(id).value) q.set(key, $(id).value);
    if ($("history-safety").checked) q.set("safety", "1");
    let r;
    try { r = await api("/api/config/history?" + q); } catch (e) { $("history-msg").textContent = e.message; return; }
    const who = $("history-who"), chosen = who.value;
    who.replaceChildren(h("option", { value: "" }, "Anyone"), ...r.people.map((p) => h("option", { value: p }, p)));
    who.value = chosen;
    $("history-msg").textContent = r.entries.length ? "Kept in " + r.folder : "Nothing kept yet.";
    $("history-list").replaceChildren(...r.entries.map(historyRow));
  }
  function historyRow(e) {
    const person = e.account ? e.account + (e.role ? ", " + e.role : "") + " (" + e.by + ")" : e.by;
    const what = e.type === "event" ? e.what
      : e.title + (e.kind === "first" ? ", as Kelvin found it" : e.gone ? ", deleted" : "");
    const li = h("li", { class: "hist" + (e.safety ? " widens" : "") },
      h("div", { class: "hist-head" }, h("b", {}, "#" + e.n), h("time", {}, String(e.at || "").replace("T", " ").slice(0, 16)),
        h("span", { class: "hist-what" }, what),
        e.safety ? h("span", { class: "tag", title: "This widened Kelvin's reach: the lab owner hears of it" }, "widens") : null,
        e.rollback ? h("span", { class: "tag" }, "back to #" + e.rollback) : null),
      h("div", { class: "muted small" }, person),
      (e.changes || []).length ? h("ul", { class: "hist-changes" },
        ...e.changes.map((c) => h("li", { class: c.safety ? "widens" : "" }, c.text))) : null,
      e.why ? h("div", { class: "muted small" }, "Why: " + e.why) : null);
    if (e.type !== "version") return li;
    const diff = h("pre", { class: "diff mono", hidden: true });
    const show = h("button", { class: "link soft", type: "button" }, "Diff");
    show.addEventListener("click", async () => {
      if (!diff.hidden) { diff.hidden = true; return; }
      try {
        const d = await api("/api/config/history/" + e.n);
        diff.replaceChildren(...(d.diff || "(no earlier version of it is kept)\n").split("\n").map((line) => h("span", {
          class: /^\+(?!\+\+)/.test(line) ? "add" : /^-(?!--)/.test(line) ? "del" : line.startsWith("@@") ? "hunk" : null }, line + "\n")));
        diff.hidden = false;
      } catch (err) { toast(err.message); }
    });
    const back = h("button", { class: "link soft", type: "button" }, "Roll back to this…");
    back.addEventListener("click", () => rollBack(e));
    li.append(h("div", { class: "hist-actions" }, show, back), diff);
    return li;
  }
  async function rollBack(e) {
    const why = await askText("Put #" + e.n + " of the " + e.title + " back?\nIt is kept as a new version. Why?",
      { placeholder: "The reason, for the lab's record", ok: "Put back" });
    if (why === null) return;
    try {
      const r = await api("/api/config/rollback", { body: { n: e.n, why } });
      toast("Put #" + e.n + " back" + ((r.versions || []).length ? ", kept as #" + r.versions.join(", #") : "")
        + ((r.reconnect || []).length ? ". Reconnect " + andList(r.reconnect) + " to use it." : "."));
      pollState();
    } catch (err) { toast(err.message); }
    loadHistory();
  }

  // ------------------------------------------------------------------ who is using Kelvin (accounts)
  const initials = (name) => String(name).split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";

  function hubLink(session, text, edit) {   // which Kelvin hub people sign in to on this computer ("" = Kelvin's own)
    $(text).textContent = session.central ? "Kelvin hub: " + String(session.hub).replace(/^https?:\/\//, "")
      : "Accounts on this computer only.";
    $(edit).textContent = session.central ? "change" : "Use a Kelvin hub";
    $(edit).onclick = async () => {
      const lost = $("settings-dialog").open ? settingsWhere() : [];   // in Settings: changes waiting for Save go with the reload
      const url = await askText("The Kelvin hub people sign in to on this computer\nLeave it empty for Kelvin's own hub. " +
        "Changing it takes the lab owner's PIN; with no PIN set, only while nobody else has an account here." +
        (lost.length ? "\nThe page then reloads, and your changes on " + andList(lost) + " are lost." : ""),
        { value: session.hub || "", placeholder: "https://…", ok: "Use this hub" });
      if (url === null) return;
      try { await api("/api/session/hub", { body: { url: url.trim() } }); location.reload(); } catch (e) { toast(e.message); }
    };
  }

  function showCentral(session) {   // central Kelvin accounts: one account, every computer (central.py)
    const say = (text, bad) => { $("signin-msg").textContent = text || ""; $("signin-msg").classList.toggle("bad", !!bad); };
    const parts = ["signin-people", "signin-pass", "signin-new", "signin-add", "central-signin", "central-new", "central-link",
      "central-rekey", "central-create", "signin-remove", "signin-remove-link"];
    const view = (mode) => {   // signin | new | link | rekey | remove
      const shown = { signin: ["signin-people", "central-signin", "central-create"].concat((session.accounts || []).length ? ["signin-remove-link"] : []),
        new: ["central-new"], link: ["central-link"], rekey: ["central-rekey"], remove: ["signin-remove"] }[mode];
      for (const id of parts) $(id).hidden = !shown.includes(id);
      $("signin-title").textContent = { signin: "Sign in to Kelvin", new: "Create a Kelvin account", link: "Welcome to this computer",
        rekey: "Keep your API keys?", remove: "Remove someone from this computer" }[mode];
      $("signin-sub").textContent = "";
      if (mode === "signin") (($("c-email").value ? $("c-password") : $("c-email"))).focus();
      if (mode === "new") $("cn-name").focus();
      if (mode === "rekey") $("rekey-old").focus();
    };
    $("cn-terms").href = session.hub + "/terms";
    $("cn-privacy").href = session.hub + "/privacy";
    const known = (session.accounts || []).filter((a) => a.email);
    $("signin-people").replaceChildren(...known.map((p) => {
      const name = p.display || p.name;
      const b = h("button", { class: "person", type: "button" }, h("span", { class: "avatar" }, initials(name)), h("span", {}, name));
      b.addEventListener("click", () => { $("c-email").value = p.email; $("c-password").value = ""; $("c-password").focus(); });
      return b;
    }));
    let pending = null;   // what was sent, to send again with the chosen account from before
    const after = (r) => {
      if (r.link) {   // made on the hub (or signed in there): which account here is theirs is all that's left
        pending = { path: "/api/session", body: { email: pending.body.email, password: pending.body.password } };
        showLink(r.link);
        return;
      }
      if (r.registered) { say(r.message || "Check your email to confirm your address, then sign in.", false); view("signin"); return; }
      if (r.keys_sealed) { view("rekey"); return; }
      location.reload();
    };
    const send = async (extra) => {
      say(pending.path === "/api/accounts" ? "Making your account…" : "Signing in…");
      try { after(await api(pending.path, { body: Object.assign({}, pending.body, extra || {}) })); } catch (e) { say(e.message, true); }
    };
    const showLink = (names) => {
      let chosen = null;
      say("");
      $("link-people").replaceChildren(...names.map((n) => {
        const b = h("button", { class: "person", type: "button" }, h("span", { class: "avatar" }, initials(n)), h("span", {}, n));
        b.addEventListener("click", () => {
          chosen = n;
          for (const x of $("link-people").children) x.classList.toggle("on", x === b);
          const p = (session.accounts || []).find((a) => a.name === n);
          $("link-pass-label").hidden = $("link-password").hidden = !(p && p.password);
          $("link-go").hidden = false;
          if (p && p.password) $("link-password").focus();
        });
        return b;
      }));
      $("link-go").onclick = () => chosen && send({ link: chosen, link_password: $("link-password").value });
      $("link-none").onclick = () => send({ link: "" });
      view("link");
    };
    $("central-signin").onsubmit = (e) => {
      e.preventDefault();
      pending = { path: "/api/session", body: { email: $("c-email").value, password: $("c-password").value } };
      send();
    };
    $("central-new").onsubmit = (e) => {
      e.preventDefault();
      if (!$("cn-consent").checked) { say("Agree to the hub's terms to make an account.", true); return; }
      pending = { path: "/api/accounts", body: { name: $("cn-name").value, email: $("cn-email").value, password: $("cn-password").value, consent: true } };
      send();
    };
    $("central-create").onclick = () => { say(""); view("new"); };
    $("cn-back").onclick = () => { say(""); view("signin"); };
    $("c-forgot").onclick = async () => {
      const email = $("c-email").value.trim();
      if (!email) { say("Type your email first.", true); return; }
      try { await api("/api/session/reset", { body: { email } }); say("If there's a Kelvin account for " + email + ", the hub sent it a link to choose a new password."); }
      catch (e) { say(e.message, true); }
    };
    $("central-rekey").onsubmit = async (e) => {
      e.preventDefault();
      try {
        const r = await api("/api/account/rekey", { body: { old: $("rekey-old").value } });
        if (r.rekeyed) location.reload(); else say("That isn't your old password, or there was nothing to keep.", true);
      } catch (err) { say(err.message, true); }
    };
    $("rekey-skip").onclick = () => location.reload();
    setupRemove(session, () => view("signin"), say);
    $("signin-remove-link").onclick = () => { say(""); view("remove"); };
    view("signin");
  }

  // before signing in: take someone off this computer, with their password (app.py removal); their measurements stay
  function setupRemove(session, back, say) {
    const people = session.accounts || [];
    let chosen = null;
    const pick = (p, b) => {
      chosen = p;
      for (const x of $("remove-people").children) x.classList.toggle("on", x === b);
      const name = p.display || p.name;
      const kelvinAccount = !!(p.email && session.central);
      $("remove-what").hidden = false;
      $("remove-what").textContent = "Takes " + name + " off this computer: their settings, API keys, ChatGPT sign-in, chats, " +
        "projects and memory. Their measurements (runs, reports and record) stay on disk." +
        (kelvinAccount ? " Their Kelvin account stays for their other computers, unless you also delete it." : "");
      $("remove-everywhere-row").hidden = !kelvinAccount;
      $("remove-everywhere").checked = false;
      $("remove-pass-label").hidden = $("remove-password").hidden = !p.password && !kelvinAccount;
      $("remove-pass-label").textContent = name + "'s password";
      $("remove-password").value = "";
      $("remove-go").disabled = false;
      if (!$("remove-password").hidden) $("remove-password").focus();
    };
    $("remove-people").replaceChildren(...people.map((p) => {
      const name = p.display || p.name;
      const b = h("button", { class: "person", type: "button" }, h("span", { class: "avatar" }, initials(name)), h("span", {}, name),
        h("span", { class: "person-note" }, p.email || (p.password ? "on this computer" : "on this computer, no password")));
      b.addEventListener("click", () => pick(p, b));
      return b;
    }));
    for (const id of ["remove-what", "remove-everywhere-row", "remove-pass-label", "remove-password"]) $(id).hidden = true;
    $("remove-go").disabled = true;
    $("remove-back").onclick = () => { say(""); back(); };
    $("signin-remove").onsubmit = async (e) => {
      e.preventDefault();
      if (!chosen) return;
      const everywhere = $("remove-everywhere").checked;
      const name = chosen.display || chosen.name;
      if (!(await askYes(everywhere ? "Delete " + name + "'s Kelvin account on every computer?\nThey are removed from this one too. This can't be undone."
        : "Remove " + name + " from this computer?\nTheir measurements stay.", { ok: everywhere ? "Delete account" : "Remove", danger: true }))) return;
      say(everywhere ? "Deleting the Kelvin account…" : "Removing…");
      try {
        const r = await api("/api/accounts/remove", { body: { name: chosen.name, password: $("remove-password").value, everywhere } });
        showSignIn(r);
        say(removedText(r));
      } catch (err) { say(err.message, true); }
    };
  }

  function removedText(r) {
    const folder = (r.kept || [])[0] ? String(r.kept[0]).replace(/[\\/][^\\/]+$/, "") : "";
    return (r.everywhere ? "Deleted " + r.removed + "'s Kelvin account and removed them from this computer."
      : "Removed " + r.removed + " from this computer.") + (folder ? " Their measurements stay in " + folder + "." : "") +
      ((r.failed || []).length ? " Kelvin couldn't delete: " + r.failed.join(", ") + "." : "");
  }

  function showSignIn(session) {
    $("signin").hidden = false;
    for (const slot of $("signin").querySelectorAll("[data-mark]")) slot.innerHTML = MARK;
    hubLink(session, "central-hub-text", "central-hub-edit");
    // signed out, the hub can only change until someone has signed in here with a central account (app.py session_hub)
    $("central-hub-edit").hidden = (session.accounts || []).some((a) => a.email);
    if (session.central) {
      showCentral(session);
    } else {
      showLocal(session);
    }
    const say = (text, bad) => { $("signin-msg").textContent = text || ""; $("signin-msg").classList.toggle("bad", !!bad); };
    const busy = (s) => {
      $("signin-busy").hidden = !s.busy;
      $("signin-busy-text").textContent = s.busy ? (s.busy_user ? s.busy_user + "'s work: " : "") + s.busy : "";
    };
    busy(session);
    const setupNote = (s) => {   // the instruments set up meanwhile: signing in doesn't wait for them
      $("signin-setup").hidden = !s.setup;
      if (s.setup) $("signin-setup-text").textContent = s.setup.message + "… You can sign in meanwhile.";
      return !!s.setup;
    };
    if (setupNote(session)) {
      const watch = setInterval(async () => {
        if ($("signin").hidden) { clearInterval(watch); return; }
        try { if (!setupNote(await api("/api/session"))) clearInterval(watch); } catch (e) { /* keep what is shown */ }
      }, 3000);
    }
    let note = "";
    try { note = sessionStorage.getItem("kelvin-signin-msg") || ""; sessionStorage.removeItem("kelvin-signin-msg"); } catch (e) { /* no storage */ }
    if (note) say(note);
    $("signin-stop").onclick = async () => {   // anyone may stop what runs: safety never waits for a password
      try { await api("/api/runs/stop", { body: { queue: true } }); say("Stopping: the step in progress ends, then everything goes safe."); }
      catch (e) { say(e.message, true); }
      setTimeout(async () => { try { busy(await api("/api/session")); } catch (e) { /* keep what is shown */ } }, 4000);
    };
  }

  function showLocal(session) {   // accounts on this computer only (no Kelvin hub set)
    const people = session.accounts || [];
    let chosen = null;
    const say = (text, bad) => { $("signin-msg").textContent = text || ""; $("signin-msg").classList.toggle("bad", !!bad); };
    const view = (mode) => {   // people | password | new | remove
      $("signin-people").hidden = mode !== "people";
      $("signin-pass").hidden = mode !== "password";
      $("signin-new").hidden = mode !== "new";
      $("signin-add").hidden = mode !== "people";
      $("signin-remove").hidden = mode !== "remove";
      $("signin-remove-link").hidden = mode !== "people" || !people.length;
      $("new-back").hidden = !people.length;
      $("signin-title").textContent = mode === "new" ? (people.length ? "Add a person" : "Welcome to Kelvin")
        : mode === "remove" ? "Remove someone from this computer" : "Who's using Kelvin?";
      $("signin-sub").textContent = mode === "new" && !people.length && session.legacy
        ? "Your chats, runs and API keys from before come with it." : "";
      say("");
      if (mode === "password") { $("signin-pass-label").textContent = chosen.name + "'s password"; $("signin-password").value = ""; $("signin-password").focus(); }
      if (mode === "new") { $("new-name").value = people.length ? "" : session.suggest || ""; $("new-password").value = ""; $("new-name").focus(); }
    };
    const enter = async (name, password) => {
      say("Signing in…");
      try { await api("/api/session", { body: { name, password } }); location.reload(); } catch (e) { say(e.message, true); }
    };
    $("signin-people").replaceChildren(...people.map((p) => {
      const b = h("button", { class: "person", type: "button" }, h("span", { class: "avatar" }, initials(p.name)), h("span", {}, p.name));
      if (p.password) { const lock = icon("lock"); lock.classList.add("lock"); b.append(lock); }
      b.addEventListener("click", () => { chosen = p; if (p.password) view("password"); else enter(p.name, ""); });
      return b;
    }));
    $("signin-pass").onsubmit = (e) => { e.preventDefault(); enter(chosen.name, $("signin-password").value); };
    $("signin-new").onsubmit = async (e) => {
      e.preventDefault();
      say("Making your account…");
      try { await api("/api/accounts", { body: { name: $("new-name").value, password: $("new-password").value } }); location.reload(); }
      catch (err) { say(err.message, true); }
    };
    $("signin-back").onclick = () => view("people");
    $("new-back").onclick = () => view("people");
    $("signin-add").onclick = () => view("new");
    setupRemove(session, () => view("people"), say);
    $("signin-remove-link").onclick = () => view("remove");
    view(people.length ? "people" : "new");
  }

  function renderAccount() {
    const a = S.account || {};
    const on = !!(a.accounts_on && a.user);
    $("account-row").hidden = !on;
    $("account-field").hidden = !on;
    $("nav-account").hidden = false;   // also holds the Kelvin hub, which works without accounts
    $("activity-who").hidden = !a.accounts_on;   // the Lab's Activity: Mine | Everyone, with people to tell apart
    if (!on) return;
    $("account-avatar").textContent = initials(a.user.name);
    $("account-name").textContent = a.user.name;
    $("account-status").textContent = a.user.central ? a.user.email + " · Kelvin account"
      : a.user.name + (a.user.password ? " · password set" : " · no password");
    $("pw-old").hidden = !a.user.password && !a.user.central;
    $("pw-new").placeholder = a.user.central ? "New password (8 characters or more)" : "New password (empty: none)";
    $("account-hub-link").hidden = !(a.user.central && a.account_url);
    if (a.account_url) $("account-hub-link").href = a.account_url;
    hubLink(a, "account-hub-text", "account-hub-edit");
    const relay = a.relay;
    $("relay-field").hidden = !(a.user.central && relay);
    if (relay) {
      swSet($("relay-on"), relay.on);
      $("relay-status").textContent = !relay.on ? "off" : ({ online: "on: your phone can reach this Kelvin",
        connecting: "connecting…", offline: "on, but the hub can't be reached now",
        "signed out": "the hub signed this computer out: sign in again", off: "off" })[relay.state] || relay.state;
      $("relay-status").title = relay.problem || "";
    }
    const kelvinAccount = !!a.user.central;
    $("account-delete-everywhere-row").hidden = !kelvinAccount;
    $("account-delete-everywhere").checked = false;
    $("account-delete-password").hidden = !a.user.password && !kelvinAccount;
    $("account-delete-what").textContent = "Removes your settings, API keys, chats, projects and memory from this computer. " +
      "Runs and reports stay.";
    $("account-hint").textContent = a.user.central
      ? ""
      : "A forgotten password can't be reset.";
  }

  async function setRelay(on) {   // its switch (swFlip): a refusal turns it back, the reason beside it
    try {
      await api("/api/settings", { body: { phone_relay: on ? "on" : "off" } });
      setTimeout(async () => { try { S.account = await api("/api/session"); renderAccount(); } catch (e) { /* keep */ } }, 1500);
    } catch (e) { $("relay-status").textContent = e.message; throw e; }
  }

  async function signOut() {
    try { await api("/api/session", { method: "DELETE" }); } catch (e) { /* the sign-in screen shows either way */ }
    location.reload();
  }

  async function deleteAccount() {
    const everywhere = $("account-delete-everywhere").checked;
    if (!(await askYes(everywhere ? "Delete your Kelvin account on every computer?\nIt is removed from this one too. This can't be undone."
      : "Remove your account from this computer?\nYour measurements stay.", { ok: everywhere ? "Delete account" : "Remove", danger: true }))) return;
    $("settings-msg").textContent = everywhere ? "Deleting your Kelvin account…" : "Removing…";
    try {
      const r = await api("/api/account", { method: "DELETE", body: { password: $("account-delete-password").value, everywhere } });
      try { sessionStorage.setItem("kelvin-signin-msg", removedText(r)); } catch (e) { /* the sign-in screen shows either way */ }
      location.reload();
    } catch (e) { $("settings-msg").textContent = e.message; }
  }

  async function changePassword() {   // Settings → Account's Change; an empty new password takes the password off: asked first
    const user = (S.account && S.account.user) || {};
    if (!$("pw-new").value && user.password && !user.central && !(await askYes("Remove your password?\n" +
      "Anyone at this computer can then sign in as you, and your API keys are no longer encrypted.", { ok: "Remove", danger: true }))) return;
    $("settings-msg").textContent = "Changing…";
    try {
      S.account = await api("/api/account", { body: { old: $("pw-old").value, new: $("pw-new").value } });
      renderAccount();
      $("pw-old").value = ""; $("pw-new").value = "";
      $("settings-msg").textContent = S.account.user.password ? "Password changed. Your API keys are encrypted with it." : "Password removed.";
    } catch (e) {
      $("settings-msg").textContent = e.message;
    }
  }

  let toastTimer = null;
  function toast(text, onClick) {   // onClick: what a click on it does (open the chat it is about)
    let t = document.querySelector(".toast");
    if (!t) { t = h("div", { class: "toast", role: "status" }); document.body.append(t); }
    t.textContent = text;
    t.classList.toggle("link", !!onClick);
    t.onclick = onClick ? () => { t.classList.remove("show"); onClick(); } : null;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), onClick ? 8000 : 4000);
  }

  // ------------------------------------------------------------------ markdown (safe subset)
  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    s = esc(s);
    s = s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => "<code>" + esc(codes[+i]) + "</code>");
  }
  const LIST = /^\s*([-*+]|\d+[.)])\s+(.*)$/;
  const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  function md(src) {
    const lines = String(src).replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;
    const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    while (i < lines.length) {
      const line = lines[i];
      let m;
      if (/^```/.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push("<pre><code>" + esc(buf.join("\n")) + "</code></pre>");
      } else if (!line.trim()) {
        i++;
      } else if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        const lvl = Math.min(5, m[1].length + 2);
        out.push("<h" + lvl + ">" + inline(m[2]) + "</h" + lvl + ">");
        i++;
      } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        out.push("<hr>");
        i++;
      } else if (line.includes("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        out.push("<table><thead><tr>" + head.map((c) => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>" +
          rows.map((r) => "<tr>" + r.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>").join("") + "</tbody></table>");
      } else if (/^>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
        out.push("<blockquote>" + md(buf.join("\n")) + "</blockquote>");
      } else if ((m = line.match(LIST))) {
        const ordered = /\d/.test(m[1]);
        const items = [];
        while (i < lines.length) {
          const mm = lines[i].match(LIST);
          if (mm && /\d/.test(mm[1]) === ordered) { items.push(mm[2]); i++; }
          else if (items.length && lines[i].trim() && /^\s{2,}/.test(lines[i])) { items[items.length - 1] += " " + lines[i].trim(); i++; }
          else break;
        }
        const tag = ordered ? "ol" : "ul";
        out.push("<" + tag + ">" + items.map((t) => "<li>" + inline(t) + "</li>").join("") + "</" + tag + ">");
      } else {
        const buf = [];
        while (i < lines.length && lines[i].trim() && !/^```|^#{1,6}\s|^>\s?/.test(lines[i]) && !LIST.test(lines[i]) &&
               !(buf.length && lines[i].includes("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]))) buf.push(lines[i++]);
        if (!buf.length) buf.push(lines[i++]);
        out.push("<p>" + buf.map(inline).join("<br>") + "</p>");
      }
    }
    return out.join("");
  }

  // ------------------------------------------------------------------ numbers and units
  const SI = ["V", "A", "Hz", "W", "s", "Ω", "T", "K", "F", "H", "S"];
  const PREFIXES = [[1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"], [1e-15, "f"]];
  function prefixFor(maxAbs, unit) {
    if (!SI.includes(unit) || !isFinite(maxAbs) || maxAbs === 0) return [1, ""];
    for (const p of PREFIXES) if (maxAbs >= p[0] * 0.9995) return p;
    return PREFIXES[PREFIXES.length - 1];
  }
  function fmt(v, unit) {
    if (typeof v !== "number" || !isFinite(v)) return String(v);
    const [f, p] = prefixFor(Math.abs(v), unit);
    const n = Number((v / f).toPrecision(4));
    return n.toString() + (unit ? " " + p + unit : "");
  }
  function niceTicks(lo, hi, count) {
    if (!(hi > lo)) { const d = Math.abs(lo) * 0.1 || 1; lo -= d; hi += d; }
    const raw = (hi - lo) / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    const start = Math.floor(lo / step + 1e-9) * step;
    const end = Math.ceil(hi / step - 1e-9) * step;
    const ticks = [];
    for (let v = start; v <= end + step / 2; v += step) ticks.push(Number(v.toPrecision(12)));
    return { ticks, lo: start, hi: end, step };
  }
  function tickText(v, f, step) {
    const d = Math.max(0, Math.min(6, -Math.floor(Math.log10(step / f) + 1e-9)));
    return (v / f).toFixed(d);
  }

  // ------------------------------------------------------------------ charts
  const SERIES = 8;
  let END_CTX = null;
  function endWidth(text) {   // a line's end label (.plot .endlabel: 12 px, 600, the page's font), in the page's pixels
    if (!END_CTX) END_CTX = document.createElement("canvas").getContext("2d");
    END_CTX.font = "600 12px " + getComputedStyle(document.body).fontFamily;
    return Math.ceil(END_CTX.measureText(String(text)).width);
  }
  function lineChart(opts) {
    const { series, xKey, xUnit, yUnit } = opts;
    const fit = series.length === 1 ? opts.fit : null;  // fitted curve over a single measured series
    const xs = series.flatMap((s) => s.points.map((p) => p.x));
    const fits = opts.fits && opts.fits.some(Boolean) ? opts.fits : null;   // each curve's own fit (the Analysis pane)
    const ys = series.flatMap((s) => s.points.map((p) => p.y)).concat(fit ? fit.y.filter((v) => typeof v === "number") : [])
      .concat(fits ? fits.flatMap((f) => (f ? f.y.filter((v) => typeof v === "number") : [])) : []);
    const X = niceTicks(Math.min(...xs), Math.max(...xs), 5);
    const Y = niceTicks(Math.min(...ys), Math.max(...ys), 4);
    const xRange = [Math.min(...xs), Math.max(...xs)];
    if (!(xRange[1] > xRange[0])) { xRange[0] = X.lo; xRange[1] = X.hi; }
    const [xf, xp] = prefixFor(Math.max(...X.ticks.map(Math.abs)), xUnit);
    const [yf, yp] = prefixFor(Math.max(...Y.ticks.map(Math.abs)), yUnit);
    const box = h("div", { class: "plot" });
    const holder = h("div", { class: "plot-svg" });
    const tip = h("div", { class: "tooltip", hidden: true });
    const unitText = (u, p) => (u ? " (" + p + u + ")" : "");
    box.append(h("div", { class: "plot-head" }, h("span", { class: "plot-title" }, opts.title + unitText(yUnit, yp)),
      h("span", { class: "plot-sub" }, "vs " + xKey + unitText(xUnit, xp))), holder, tip);
    if (series.length > 1) {
      box.append(h("div", { class: "legend" }, ...series.map((s, k) =>
        h("span", { class: "key" }, h("i", { style: "background:var(--s" + (k + 1) + ")" }), s.name))));
    } else if (fit) {
      box.append(h("div", { class: "legend" },
        h("span", { class: "key" }, h("i", { style: "background:var(--series-1)" }), "measured"),
        h("span", { class: "key" }, h("i", { style: "background:var(--fit)" }), fit.label)));
    }
    // drawn at the real pixel width so its text keeps its size; with opts.fill as tall as its place too, never under the
    // least app.css gives that place (120 in a run's view, 140 elsewhere), so it is drawn inside its card
    if (opts.fill) box.classList.add("fill");
    let drawn = "";
    const draw = () => {
      const W = Math.max(240, Math.round(holder.clientWidth));
      const H = opts.fill ? Math.max(120, Math.round(holder.clientHeight)) : opts.height || 200;
      if (W + "x" + H === drawn) return;
      drawn = W + "x" + H;
      holder.replaceChildren(build(W, H));
    };
    if (window.ResizeObserver) new ResizeObserver(draw).observe(holder);
    setTimeout(draw, 0);   // once it is on the page (a timer, not a frame: a pane out of sight gets none)
    return box;

    function build(W, H) {
    // each line's last value said at its end: the right margin as wide as the longest of them, measured in its font
    const ends = series.length <= 4 && !fit && !fits
      ? series.map((s) => (series.length > 1 ? s.name.split(" = ").pop() : fmt(s.points[s.points.length - 1].y, yUnit))) : null;
    const M = { l: 48, r: ends ? Math.max(62, Math.min(Math.round(W * 0.4), 16 + Math.max(...ends.map(endWidth)))) : 14,
                t: 10, b: 26 };
    const sx = (x) => M.l + (x - xRange[0]) / (xRange[1] - xRange[0]) * (W - M.l - M.r);
    const sy = (y) => H - M.b - (y - Y.lo) / (Y.hi - Y.lo) * (H - M.t - M.b);
    const root = svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": opts.title });
    for (const t of Y.ticks) {
      root.append(svg("line", { class: t === Y.lo ? "base" : "grid", x1: M.l, x2: W - M.r, y1: sy(t), y2: sy(t) }));
      const label = svg("text", { class: "tick", x: M.l - 8, y: sy(t) + 4, "text-anchor": "end" });
      label.textContent = tickText(t, yf, Y.step);
      root.append(label);
    }
    for (const t of X.ticks) {
      if (t < xRange[0] - 1e-12 * Math.abs(t) || t > xRange[1] + 1e-12 * Math.abs(t)) continue;
      const label = svg("text", { class: "tick", x: sx(t), y: H - M.b + 18, "text-anchor": "middle" });
      label.textContent = tickText(t, xf, X.step);
      root.append(label);
    }
    const taken = [];  // y positions of end labels already placed, so converging lines don't stack text
    series.forEach((s, k) => {
      const color = series.length > 1 ? "var(--s" + (k + 1) + ")" : "var(--series-1)";
      const d = s.points.map((p, i) => (i ? "L" : "M") + sx(p.x).toFixed(1) + "," + sy(p.y).toFixed(1)).join("");
      root.append(svg("path", { class: fit || fits ? "line thin" : "line", d, style: "stroke:" + color }));
      if ((fit || fits) && s.points.length <= 120) {
        for (const p of s.points) root.append(svg("circle", { class: "datapt", cx: sx(p.x), cy: sy(p.y), r: 2.6, style: "fill:" + color }));
      }
      const last = s.points[s.points.length - 1];
      if (!fit && !fits) root.append(svg("circle", { class: "end", cx: sx(last.x), cy: sy(last.y), r: 4, style: "fill:" + color }));
      const ly = sy(last.y) + 4;
      if (series.length <= 4 && !fit && !fits && !taken.some((t) => Math.abs(t - ly) < 13)) {
        taken.push(ly);
        const said = ends[k], x = sx(last.x) + 8, over = x + endWidth(said) > W - 2;   // too long for its margin: inside, above
        const label = svg("text", over ? { class: "endlabel", x: W - 2, y: ly - 12, "text-anchor": "end" } : { class: "endlabel", x, y: ly });
        label.textContent = said;
        root.append(label);
      }
    });
    if (fit) {
      const inRange = fit.x.map((x, i) => [x, fit.y[i]]).filter(([x, y]) => typeof y === "number" && x >= xRange[0] && x <= xRange[1]);
      root.append(svg("path", { class: "fitline", d: inRange.map(([x, y], i) => (i ? "L" : "M") + sx(x).toFixed(1) + "," + sy(y).toFixed(1)).join("") }));
    }
    if (fits) {
      fits.forEach((f, k) => {
        if (!f || k >= series.length) return;
        const pts = f.x.map((x, i) => [x, f.y[i]]).filter(([x, y]) => typeof y === "number" && x >= xRange[0] && x <= xRange[1]);
        root.append(svg("path", { class: "fitline", style: "stroke:" + (series.length > 1 ? "var(--s" + (k + 1) + ")" : "var(--fit)") + ";stroke-dasharray:6 3",
          d: pts.map(([x, y], i) => (i ? "L" : "M") + sx(x).toFixed(1) + "," + sy(y).toFixed(1)).join("") }));
      });
    }
    // hover layer: crosshair + tooltip
    const cross = svg("line", { class: "cross", y1: M.t, y2: H - M.b, visibility: "hidden" });
    const dots = series.map((s, k) => svg("circle", { class: "hoverdot", r: 4, visibility: "hidden",
      style: "fill:" + (series.length > 1 ? "var(--s" + (k + 1) + ")" : "var(--series-1)") }));
    const hit = svg("rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent" });
    root.append(cross, ...dots, hit);
    hit.addEventListener("pointermove", (e) => {
      const r = root.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width * W;
      const x = xRange[0] + (px - M.l) / (W - M.l - M.r) * (xRange[1] - xRange[0]);
      tip.replaceChildren();
      let xShown = null;
      series.forEach((s, k) => {
        let best = s.points[0];
        for (const p of s.points) if (Math.abs(p.x - x) < Math.abs(best.x - x)) best = p;
        if (xShown === null) { xShown = best.x; tip.append(h("div", {}, xKey + " = " + fmt(best.x, xUnit))); }
        dots[k].setAttribute("cx", sx(best.x)); dots[k].setAttribute("cy", sy(best.y)); dots[k].setAttribute("visibility", "visible");
        tip.append(h("div", {}, series.length > 1 ? s.name + ": " : "", h("b", {}, fmt(best.y, yUnit))));
      });
      cross.setAttribute("x1", sx(xShown)); cross.setAttribute("x2", sx(xShown)); cross.setAttribute("visibility", "visible");
      tip.hidden = false;
      const bx = box.getBoundingClientRect(), z = (Kelvin.zoom && Number(Kelvin.zoom())) || 1;   // window px ÷ page size
      const left = e.clientX - bx.left + 14;
      tip.style.left = Math.min(left, bx.width - tip.offsetWidth * z - 8) / z + "px";
      tip.style.top = (e.clientY - bx.top - 10) / z + "px";
    });
    hit.addEventListener("pointerleave", () => {
      tip.hidden = true; cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    });
    return root;
    }
  }

  function fitFor(analysis, key, trace) {
    if (!analysis) return null;
    const matches = analysis.series.filter((s) => s.y === key && s.fit && (trace || !s.group));
    const s = matches[matches.length - 1];
    return s ? { x: s.fit.x, y: s.fit.y, label: s.fit.model + " fit" } : null;
  }

  function runCharts(data, maxScalars) {
    // data: {axes, scalars, rows, traces, analysis}; outer axis -> one line per value, inner axis -> x
    const grid = h("div", { class: "plots" });
    const axes = data.axes;
    if (axes.length && data.rows.length > 1) {
      const xa = axes[axes.length - 1];
      const outer = axes.length > 1 ? axes[0] : null;
      for (const sc of data.scalars.slice(0, maxScalars || 99)) {
        let series;
        if (outer) {
          const groups = new Map();
          for (const r of data.rows) {
            const key = r[outer.key];
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push({ x: r[xa.key], y: r[sc.key] });
          }
          series = [...groups.entries()].slice(0, SERIES).map(([k, pts]) => ({ name: outer.key + " = " + fmt(k, outer.unit), points: pts }));
        } else {
          series = [{ name: sc.key, points: data.rows.map((r) => ({ x: r[xa.key], y: r[sc.key] })) }];
        }
        series = series.map((s) => ({ name: s.name, points: s.points.filter((p) => typeof p.x === "number" && typeof p.y === "number") }))
          .filter((s) => s.points.length);
        if (series.length) grid.append(lineChart({ title: sc.key, series, xKey: xa.key, xUnit: xa.unit, yUnit: sc.unit, fit: fitFor(data.analysis, sc.key), fill: true }));
      }
    }
    for (const tr of data.traces) {
      const points = tr.x.map((x, i) => ({ x, y: tr.y[i] })).filter((p) => typeof p.x === "number" && typeof p.y === "number");
      if (!points.length) continue;   // a point the instrument could not measure (NaN) is left out, not drawn at 0
      grid.append(lineChart({ title: tr.key + (data.rows.length > 1 ? " (latest)" : ""), xKey: tr.x_unit === "Hz" ? "frequency" : "x",
        xUnit: tr.x_unit, yUnit: tr.unit, fit: fitFor(data.analysis, tr.key, true), fill: true,
        series: [{ name: tr.key, points }] }));
    }
    return grid;
  }

  const KIND_LABEL = { peak: "peak", dip: "dip", linear: "linear", relaxation: "relaxation", flat: "no signal", steady: "steady",
    monotonic: "monotonic", structured: "structure", value: "value", none: "too few points" };

  // a finished run's automatic analysis: {box: its assessment, two lines and the rest a press away (a disclosure: its
  // chevron, or a click on it), table: its key results (or null), rows: how many, whole(on): the whole assessment
  // shown or folded without a press, when there is more than its two lines}; the run view folds the table
  function analysisBlock(a) {
    const box = h("div", { class: "analysis" });
    const ok = !a.issues.length;
    const body = h("div", { class: "assess-body", id: "rv-assess-" + (++RV_PARTS) },
      h("p", { class: "assess-text" }, a.assessment), h("p", { class: "assess-next" }, h("b", {}, "Next: "), a.next),
      a.issues.length > 1 ? h("ul", { class: "issues" }, ...a.issues.slice(1, 6).map((i) => h("li", {}, i))) : null);
    const more = h("button", { class: "assess-more", type: "button", "aria-expanded": "false", "aria-controls": body.id,
                               title: "The whole assessment", "aria-label": "The whole assessment", hidden: true }, icon("chev"));
    const as = h("div", { class: "assess" + (ok ? "" : " warn") }, h("span", { class: "assess-icon" }, icon(ok ? "check" : "warn")), body, more);
    const show = (on) => {
      more.setAttribute("aria-expanded", String(on));
      as.classList.toggle("open", on);
    };
    const flip = () => show(more.getAttribute("aria-expanded") !== "true");
    more.addEventListener("click", (e) => { e.stopPropagation(); flip(); });
    as.addEventListener("click", (e) => { if (!more.hidden && !e.target.closest("a, button") && !String(getSelection()).trim()) flip(); });
    // closed: the assessment's first two lines; the chevron is there while there is more (Next, an issue, a third line)
    const text = body.firstChild, more2 = !!a.next || a.issues.length > 1;
    const cut = () => { more.hidden = !as.classList.contains("open") && !more2 && text.scrollHeight <= text.clientHeight + 1; };
    if (window.ResizeObserver) new ResizeObserver(cut).observe(text);
    cut();
    const whole = (on) => {
      if (on && !more2 && text.scrollHeight <= text.clientHeight + 1) return false;   // two lines say it all
      show(on);
      cut();
      return true;
    };
    box.append(as);
    const rows = [];
    for (const s of a.series.slice(0, 6)) {
      if (!s.results.length) continue;
      const fitText = s.fit ? s.fit.model + " fit, R² " + Number(s.fit.r2).toFixed(3) : "";
      const what = s.fit && s.fit.model === s.kind ? fitText : (KIND_LABEL[s.kind] || s.kind) + (fitText ? " (" + fitText + ")" : "");
      const title = s.y + (s.group ? " · " + s.group : "") + " — " + what;
      rows.push(h("tr", { class: "res-head" }, h("th", { colspan: "2" }, title)));
      for (const r of s.results) rows.push(h("tr", {}, h("td", {}, r.name), h("td", {}, r.value)));
    }
    const table = rows.length ? h("div", { class: "results" }, h("table", {}, h("tbody", {}, ...rows))) : null;
    return { box, table, rows: rows.length, whole };
  }
  // a part of the run view a press away: a small button that keeps its name, its chevron turning while the part shows
  // under the row of these buttons (aria-expanded); onOpen each time it opens
  let RV_PARTS = 0;
  function rvFold(label, part, onOpen) {
    part.id = part.id || "rv-part-" + (++RV_PARTS);
    part.hidden = true;
    const b = h("button", { class: "rv-fold", type: "button", "aria-expanded": "false", "aria-controls": part.id },
      icon("chev"), h("span", {}, label));
    b.addEventListener("click", () => rvOpen(b, part.hidden, onOpen));
    return b;
  }
  function rvOpen(b, on, onOpen, still) {   // still: opened as the view is drawn, not by a press (nothing moves)
    const part = $(b.getAttribute("aria-controls"));
    if (!part || part.hidden === !on) return;
    part.hidden = !on;
    b.setAttribute("aria-expanded", String(on));
    if (on) { if (!still) easeIn([part]); if (onOpen) onOpen(); }
  }

  // ------------------------------------------------------------------ runs pane (beside the chat, like a terminal pane)
  // Two views: the Scan (the measurement now, above the scans, scripts and experiments to run or put in the sequence,
  // with New scan and Import: seq.js draws those) and the Sequence (seq.js: the queue as blocks; the "schedule" view,
  // as 0.3.39 named the queue's). What showed the queue opens the pane on the Sequence (showQueue); what starts a run,
  // on the Scan (showScan). Earlier runs are the Analysis pane's (openRun). 0.3.39's Schedule (the scans beside the
  // queue) stays on the page, never shown. A running run is polled from /api/live (so runs started by any agent show
  // up); once it ended the Scan shows its assessment, key results, plots with fits, table and Run again, till the next.
  const RUNS = { open: false, tabs: new Map(), active: null, polling: false, cache: new Map(), dismissed: new Set(),
                 view: "scan", seqOpen: false, scanOpen: false };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const etaText = (s) => (s ? " · about " + (s < 90 ? Math.max(1, Math.round(s)) + " s" : Math.round(s / 60) + " min") + " left" : "");
  const durText = (s) => (s < 90 ? Math.round(s) + " s" : s < 5400 ? (s / 60).toFixed(1) + " min" : (s / 3600).toFixed(1) + " h");

  function getRun(runId) {  // finished runs never change, so fetch each once
    if (!RUNS.cache.has(runId)) {
      const p = api("/api/runs/" + encodeURIComponent(runId)).then((d) => {
        if (!d.meta || !d.meta.status) RUNS.cache.delete(runId);  // not finished yet: fetch again next time
        return d;
      }, (e) => { RUNS.cache.delete(runId); throw e; });
      RUNS.cache.set(runId, p);
    }
    return RUNS.cache.get(runId);
  }
  function statusIcon(status) {
    if (!status || status === "running" || status === "finishing") return h("span", { class: "spinner" });
    return icon(status === "done" ? "check" : "warn");
  }
  function setState(el, status) {
    el.className = "state" + (status === "done" ? " ok" : ["running", "finishing", null, undefined].includes(status) ? "" : " warn");
    el.replaceChildren(statusIcon(status));
  }

  // a run in the Scan: its view (buildRunView), shown while it runs and, once it ended, until the next one comes
  function runTab(runId, plan) {
    let tab = RUNS.tabs.get(runId);
    if (tab) return tab;
    const body = h("div", { class: "run-view", hidden: true, "data-region": "run" });
    tab = { runId, plan: plan || runId, status: null, body, rows: [], axes: [], measures: [], trace: null,
            done: 0, total: 0, eta: null, final: null, view: null, timer: 0 };
    RUNS.tabs.set(runId, tab);
    $("scan-live").append(body);
    for (const old of [...RUNS.tabs.values()]) {   // one run at a time: the ones that ended before it go
      if (old !== tab && old.status !== "running") closeRunTab(old.runId);
    }
    return tab;
  }
  function setTabStatus(tab, status) {
    tab.status = status;
    liveDots();
  }
  // a run running: a dot on the Runs button (its sequence's count in the accent instead, where it shows one), and on
  // Scan while the Sequence is shown
  function liveDots() {
    const live = [...RUNS.tabs.values()].some((t) => t.status === "running");
    $("runs-dot").hidden = !live;
    $("toggle-runs").classList.toggle("run-live", live);
    $("open-scan").classList.toggle("live", live);
  }
  function selectRun(runId) {
    const tab = RUNS.tabs.get(runId);
    if (!tab) return;
    RUNS.active = runId;
    showView("scan");
    if (tab.status !== "running" && tab.status !== "finishing" && !tab.final) loadFinal(tab);
    else renderRunView(tab);
  }
  // a run's view put away (its ×, or the next run coming): the Scan shows the one still running, else none
  function closeRunTab(runId) {
    const tab = RUNS.tabs.get(runId);
    if (!tab) return;
    clearTimeout(tab.timer);
    RUNS.dismissed.add(runId);  // closing a running run's view must not bring it back
    tab.body.remove();
    RUNS.tabs.delete(runId);
    if (RUNS.active === runId) {   // the one still running, never an older run that ended (it went when this came)
      const live = [...RUNS.tabs.values()].find((t) => t.status === "running" || t.status === "finishing");
      RUNS.active = live ? live.runId : null;
      if (RUNS.view === "scan") showView("scan");
    }
    liveDots();
  }
  // the pane's view: "scan" (the measurement now, and what to run) or "schedule" (the Sequence)
  const VIEWS = { scan: ["open-scan", "scan-view"], schedule: ["open-schedule", "seq-view"] };
  function showView(view) {
    if (!VIEWS[view]) view = "scan";
    RUNS.view = view;
    store("lh.runsView", view);   // kept per browser: the pane opens on it again
    for (const [v, [seg, panel]] of Object.entries(VIEWS)) {
      const on = v === view;
      $(seg).classList.toggle("on", on);
      $(seg).setAttribute("aria-selected", String(on));
      $(seg).tabIndex = on ? 0 : -1;
      $(panel).hidden = !on;
    }
    let active = RUNS.tabs.get(RUNS.active);
    if (!active) {   // none asked for: the run that runs
      active = [...RUNS.tabs.values()].find((t) => t.status === "running") || null;
      RUNS.active = active ? active.runId : null;
    }
    for (const t of RUNS.tabs.values()) t.body.hidden = t !== active;
    $("scan-idle").hidden = !!active;
    if (view === "schedule") loadQueue();
    seqSight();
  }
  // seq.js told what of it is in sight (the pane open on the Sequence, or on the Scan and its functions), and what to
  // say there (message)
  function seqSight(message) {
    const open = RUNS.open && RUNS.view === "schedule", scan = RUNS.open && RUNS.view === "scan";
    if (open === RUNS.seqOpen && scan === RUNS.scanOpen && !message) return;
    emit("seq", { open, opened: open && !RUNS.seqOpen, scan, message: message || "" });
    RUNS.seqOpen = open;
    RUNS.scanOpen = scan;
  }
  function openRunList() {   // Home's Runs: every run is the Analysis pane's
    openAnalysis({});
  }
  // the segmented control is a tab list: the arrow keys, Home and End move along it (the Lab's choices have segKeys)
  function runsSegKeys(e) {
    const order = Object.keys(VIEWS), i = order.indexOf(RUNS.view);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: order.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    const view = order[(next + order.length) % order.length];
    showView(view);
    $(VIEWS[view][0]).focus();
  }


  function dayText(t) {   // today, or the day: 30 Oct; another year's: 30 Oct 2025
    const now = new Date();
    if (t.toDateString() === now.toDateString()) return "today";
    return t.toLocaleDateString(undefined, t.getFullYear() === now.getFullYear()
      ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", year: "numeric" });
  }
  function toggleRuns(open) {
    const was = RUNS.open;
    RUNS.open = open === undefined ? !RUNS.open : open;
    $("runpane").hidden = !RUNS.open;
    $("toggle-runs").setAttribute("aria-pressed", String(RUNS.open));
    $("toggle-runs").classList.toggle("on", RUNS.open);
    if (RUNS.open && !was) showView(RUNS.view);   // opened: its lists as they are now
    fitLayout();
    seqSight();
  }
  // a run asked for: the one the Scan shows (running, or the last to end) there; any other in the Analysis pane
  function openRun(runId, plan) {
    if (!RUNS.tabs.has(runId)) { openAnalysis({ run: runId }); return; }
    selectRun(runId);
    toggleRuns(true);
  }

  // a run started (seen in the chat stream or in the lab state): its view, the Scan's from now on, shown and followed;
  // while the pane shows the Sequence the Sequence stays, and Scan's dot says a run is on. Runs opens by itself only
  // where it stands beside the panes open now (fitsBeside): it never puts one away, nor takes the chat's place. Else
  // the screen stays as it is, the dot on Runs says a run is on, and Runs' button shows the run in the Scan
  function noticeRun(runId, plan) {
    if (RUNS.tabs.has(runId) || RUNS.dismissed.has(runId)) return;
    const tab = runTab(runId, plan);
    setTabStatus(tab, "running");
    RUNS.active = runId;
    if (!scheduling()) {
      if (fitsBeside("runpane")) openRun(runId, plan);
      else selectRun(runId);
    }
    pollLive();
  }
  function scheduling() {
    return RUNS.open && RUNS.view === "schedule";
  }
  async function pollLive() {
    if (RUNS.polling) return;
    RUNS.polling = true;
    try {
      for (;;) {
        const tab = [...RUNS.tabs.values()].find((t) => t.status === "running");
        if (!tab) return;
        let v = null;
        try { v = (await api("/api/live?since=" + tab.rows.length)).live; } catch (e) { await sleep(2000); continue; }
        if (!v || v.run_id !== tab.runId) {  // we missed its end: read the record instead
          setTabStatus(tab, "finishing");
          await loadFinal(tab);
          if (v && v.status === "running" && !RUNS.tabs.has(v.run_id)) noticeRun(v.run_id, v.plan);
          continue;
        }
        if (v.since === tab.rows.length) tab.rows.push(...v.rows);
        tab.axes = v.axes; tab.measures = v.measures;
        if (v.trace) tab.trace = v.trace;
        tab.done = v.done; tab.total = v.total;
        if (v.eta_s !== tab.etaRaw || v.phase !== tab.phase) { tab.etaRaw = v.eta_s; tab.etaAt = Date.now(); }  // count down between updates
        tab.phase = v.phase;
        tab.inputs = v.inputs || null;
        if (v.status !== "running") { setTabStatus(tab, "finishing"); await loadFinal(tab); continue; }
        renderRunView(tab);
        await sleep(500);
      }
    } finally { RUNS.polling = false; }
  }
  async function loadFinal(tab) {
    const ended = tab.status === "finishing";   // it just ended (not an earlier run's tab opened)
    let data;
    try { data = await getRun(tab.runId); } catch (e) { setTabStatus(tab, "missing"); renderRunView(tab); return; }
    tab.final = data;
    tab.plan = data.plan || tab.plan;
    setTabStatus(tab, (data.meta && data.meta.status) || "unfinished");
    renderRunView(tab);
    if (ended && RUNS.active !== tab.runId && RUNS.tabs.has(RUNS.active)) closeRunTab(tab.runId);   // a newer one shows
    if (ended) refreshAnalysisRuns();   // the Analysis pane's list has it now
  }

  function buildRunView(tab) {
    const v = {};
    v.state = h("span", { class: "state" });
    v.stop = h("button", { class: "btn small", type: "button" }, "Stop run");
    v.stop.addEventListener("click", async () => {   // at once: a stop never waits behind a dialog
      try { await api("/api/runs/stop", { body: {} }); toast("Stopping the run: its safe-stop steps run now."); }
      catch (e) { toast(e.message); }
    });
    v.count = h("span", { class: "rv-count" });
    v.inputs = h("div", { class: "rv-inputs", hidden: true });   // a running experiment's live inputs
    v.bar = h("i");
    v.barBox = h("div", { class: "bar" }, v.bar);
    v.analysis = h("div", { class: "rv-analysis" });
    v.charts = h("div", { class: "rv-charts" });
    // what is a press away, each part under the one row of its buttons (disclosures): the key results, the plan as it
    // ran, Run again (the lab read each time it opens) and the table
    v.results = h("div", { class: "rv-results" });
    v.table = h("div", { class: "table-wrap", "data-region": "run" });
    v.tbl = rvFold("Table", v.table, () => v.table.replaceChildren(runTable(runViewData(tab))));
    v.planBox = h("div", { class: "rv-plan" }, h("pre", {}));   // as it ran
    v.againBox = h("div", { class: "rv-again" }, h("div", { class: "ra-body" }));
    v.keys = rvFold("Key results", v.results);
    v.plan = rvFold("Plan", v.planBox);
    v.again = rvFold("Run again", v.againBox, () => loadAgain(tab));
    v.keys.hidden = v.plan.hidden = v.again.hidden = true;
    // its files and downloads: one row of buttons, what does not fit in its ⋯; a script's text read under the row
    v.files = h("div", { class: "report-files rv-files run-downloads", hidden: true });
    v.fileView = h("div", { class: "rv-file-view", hidden: true });
    v.close = h("button", { class: "icon-btn rv-close", type: "button", hidden: true, title: "Close", "aria-label": "Close: " + tab.plan }, icon("x"));
    v.close.addEventListener("click", () => closeRunTab(tab.runId));
    // the plots fill the height left (at least 160 px); the analysis and the row of what is a press away above them, or
    // beside them from 900 px (app.css)
    v.side = h("div", { class: "rv-side", "data-region": "run" }, v.analysis,
      h("div", { class: "rv-folds" }, v.keys, v.plan, v.again, v.tbl), v.results, v.planBox, v.againBox, v.table);
    tab.body.replaceChildren(
      h("div", { class: "rv-head" }, v.state, h("code", { class: "rv-plan-name", title: tab.runId }, tab.plan), h("div", { class: "spacer" }),
        v.stop, v.close),
      h("div", { class: "rv-progress" }, v.count, h("span", { class: "rv-id k-minor" }, tab.runId)), v.inputs, v.barBox, v.files,
      v.fileView, h("div", { class: "rv-main" }, v.charts, v.side));
    tab.view = v;
  }

  // A request that starts the queue answers once the person has answered its approval. Meanwhile no other Run starts
  // (what is approved is what runs) and no button sends its request twice, however often the lists are redrawn: a
  // button names what it waits for in data-busy ("start", its entry's or its run's key).
  const BUSY = new Set();
  function syncBusy() {
    for (const b of document.querySelectorAll("[data-busy]")) b.disabled = b.dataset.busy.split(" ").some((k) => BUSY.has(k));
    if (ED.parts) edButtons();   // the scan editor's Run waits too
    schedButtons();              // and the schedule's ⇒ and Start
    queueButtons();
  }
  async function busyWhile(keys, fn) {
    if (keys.some((k) => BUSY.has(k))) return null;   // a second click while one is on its way does nothing
    keys.forEach((k) => BUSY.add(k));
    syncBusy();
    try { return await fn(); } finally { keys.forEach((k) => BUSY.delete(k)); syncBusy(); }
  }

  // Run again: what ran (the script and its settings; a scan's plan is the run's Plan), the start settings that differ
  // from the lab now, and whether to set those back first (GET/POST /api/runs/{id}/again)
  async function loadAgain(tab) {
    const box = tab.view.againBox.querySelector(".ra-body");
    if (!box.firstChild) box.append(h("p", { class: "hint" }, "Reading the lab…"));
    let d;
    try { d = await api("/api/runs/" + encodeURIComponent(tab.runId) + "/again"); }
    catch (e) { box.replaceChildren(h("p", { class: "hint bad" }, e.message)); return; }
    box.replaceChildren(...againParts(tab, d));
    syncBusy();
  }
  function againParts(tab, d) {
    const parts = [];
    const pick = { version: "then" };
    let cannot = "";   // why it can't run again at all
    if (d.script) {
      const s = d.script;
      const thenOk = s.as_ran !== false;   // Kelvin kept the text that ran (not for older runs, nor a copy that differs)
      if (!thenOk) pick.version = "now";
      if (!s.exists && !thenOk) cannot = s.as_ran_why || "The script is gone, and this run kept no copy Kelvin can run.";
      const tag = !s.exists ? h("span", { class: "tag warn" }, "missing") : s.changed ? h("span", { class: "tag" }, "changed") : null;
      parts.push(h("div", { class: "ra-script" }, h("code", { title: s.path }, s.path), tag));
      const set = paramText(s.params, s.args);
      if (set) parts.push(h("div", { class: "ra-params" }, set));
      if (s.exists && s.changed) {   // the file was edited since: as it ran then (the run's copy), or as it is now
        const seg = h("div", { class: "seg", role: "group", "aria-label": "Which version of the script" });
        for (const [v, label] of [["then", "As it ran then"], ["now", "As it is now"]]) {
          const b = h("button", { type: "button", "aria-pressed": String(v === pick.version), class: v === pick.version ? "on" : "" }, label);
          if (v === "then" && !thenOk) { b.disabled = true; b.title = s.as_ran_why || "This run kept no copy Kelvin can run."; }
          b.addEventListener("click", () => {
            pick.version = v;
            for (const x of seg.children) { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", String(x === b)); }
          });
          seg.append(b);
        }
        parts.push(seg);
      }
    }
    const differ = (d.conditions || []).filter((c) => c.differs && c.settable);
    const unread = (d.conditions || []).filter((c) => c.why);
    if (!d.read_now) parts.push(h("p", { class: "hint", title: unread.slice(0, 12).map((c) => c.key + ": " + c.why).join("\n") },
      "Couldn't read " + (unread.length ? unread.length + " of the settings" : "the lab") + " now."));
    else if (!differ.length) parts.push(h("p", { class: "hint" }, "Every setting is as it was."));
    if (differ.length) {
      parts.push(h("div", { class: "ra-conds" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, h("span", { class: "sr-only" }, "Setting")), h("th", {}, "then"), h("th", {}, "now"))),
        h("tbody", {}, ...differ.map((c) => {
          const [then, now] = condValues(c);
          return h("tr", {}, h("td", {}, h("code", {}, c.key), c.drive ? h("span", { class: "tag", title: "It drives the sample" }, "drive") : null),
            h("td", {}, then), h("td", {}, now));
        })))));
    }
    const n = d.differ || 0;
    const restore = h("input", { type: "checkbox" });
    restore.checked = n > 0;
    if (n) parts.push(h("label", { class: "check" }, restore, n === 1 ? "Restore this setting first" : "Restore these " + n + " settings first"));
    const msg = h("p", { class: "hint said", role: "status" });
    const go = (queue, keys) => busyWhile(keys, async () => {
      const body = { restore: n > 0 && restore.checked };
      if (d.script && d.script.exists && d.script.changed) body.version = pick.version;
      if (d.script && !d.script.exists) body.version = "then";
      if (queue) body.queue = true;
      msg.classList.remove("bad");
      msg.textContent = queue ? "" : "Starting…";   // the queue's approval is asked first
      try {
        const r = await api("/api/runs/" + encodeURIComponent(tab.runId) + "/again", { body });
        queueFromState(r.queue);
        renderQueue();
        msg.textContent = r.message || "";
        if (!queue) { showScan((r.message || "").split("\n")[0]); pollState(); }   // the Scan shows it as it runs
      } catch (e) { msg.classList.add("bad"); msg.textContent = e.message; }
    });
    const key = "again:" + encodeURIComponent(tab.runId);
    const run = h("button", { class: "btn small primary", type: "button", "data-busy": "start " + key }, "Run again");
    const later = h("button", { class: "btn small", type: "button", "data-busy": key }, "Add to sequence");
    run.addEventListener("click", () => go(false, ["start", key]));
    later.addEventListener("click", () => go(true, [key]));
    if (cannot) { msg.textContent = cannot; parts.push(msg); return parts; }
    parts.push(h("div", { class: "ra-actions" }, run, later), msg);
    return parts;
  }
  // a setting's start and present values, in its unit; told apart even when they round alike
  function condValues(c) {
    const one = (v, digits) => v == null ? "—" : typeof v === "number" ? (digits ? Number(v.toPrecision(digits)) + (c.unit ? " " + c.unit : "") : fmt(v, c.unit))
      : typeof v === "object" ? JSON.stringify(v) : String(v);
    const a = one(c.then), b = one(c.now);
    return a === b && typeof c.then === "number" && typeof c.now === "number" ? [one(c.then, 10), one(c.now, 10)] : [a, b];
  }
  function paramText(params, args) {   // V_MAX = 3 · N = 61 · args: --fast
    const out = Object.entries(params || {}).map(([k, v]) => k + " = " + (typeof v === "string" ? v : JSON.stringify(v)));
    if (args && args.length) out.push("args: " + args.join(" "));
    return out.join(" · ");
  }
  function runViewData(tab) {
    if (tab.final) return tab.final;
    return { axes: tab.axes.map((a) => ({ key: a.key, unit: a.unit })), scalars: tab.measures.filter((m) => m.kind === "scalar"),
             rows: tab.rows, traces: tab.trace ? [tab.trace] : [], analysis: null };
  }
  function liveInputs(tab) {   // a running experiment's live inputs: changed here as the chat's set_live changes them
    const v = tab.view, inputs = tab.status === "running" ? tab.inputs : null;
    if (!v || !v.inputs) return;
    v.inputs.hidden = !inputs || !Object.keys(inputs).length;
    if (v.inputs.hidden) { delete v.inputs.dataset.names; return; }
    const names = Object.keys(inputs).join("\n");
    if (v.inputs.dataset.names === names) {   // the same inputs: their values, unless someone is editing one
      for (const [name, value] of Object.entries(inputs)) {
        const f = [...v.inputs.querySelectorAll("input")].find((x) => x.dataset.name === name);
        if (f && document.activeElement !== f && !f.dataset.dirty) f.value = String(value);
      }
      return;
    }
    v.inputs.dataset.names = names;
    v.inputs.replaceChildren(h("span", { class: "muted small" }, "Live inputs:"), ...Object.entries(inputs).map(([name, value]) => {
      const field = h("input", { class: "mono", "data-name": name, value: String(value), "aria-label": name, size: "8" });
      field.addEventListener("input", () => { field.dataset.dirty = "1"; });
      const said = h("span", { class: "muted small" });
      const set = h("button", { class: "btn small", type: "button", title: "The running experiment reads it next time it asks; a drive it moves still passes the safety gate" }, "Set");
      set.addEventListener("click", async () => {
        try {
          const r = await api("/api/runs/live", { body: { name, value: field.value } });
          said.textContent = r.said; said.classList.remove("bad"); delete field.dataset.dirty;
        } catch (e) { said.textContent = e.message; said.classList.add("bad"); }
      });
      return h("label", { class: "rv-input" }, name + " ", field, set, said);
    }));
  }
  function renderRunView(tab) {
    liveInputs(tab);
    if (!tab.view) buildRunView(tab);
    const v = tab.view;
    setState(v.state, tab.status);
    const running = tab.status === "running";
    v.stop.hidden = !running;
    v.close.hidden = running || tab.status === "finishing";
    const m = tab.final ? tab.final.meta || {} : {};
    const total = tab.final ? m.planned_points || tab.final.rows.length : tab.total;
    const done = tab.final ? (m.points != null ? m.points : tab.final.rows.length) : tab.done;
    const open = !tab.final && !total;   // a lab script's run: as many points as it takes
    v.bar.style.width = (total ? Math.round(100 * done / total) : open ? 100 : 0) + "%";
    v.barBox.classList.toggle("open", open && running);
    v.barBox.classList.toggle("done", !running);
    v.barBox.classList.toggle("warn", !!tab.final && m.status !== "done");
    if (running) {
      const eta = tab.etaRaw == null ? null : Math.max(0, tab.etaRaw - (Date.now() - tab.etaAt) / 1000);
      v.count.textContent = tab.phase === "setup" ? "Setting up" + etaText(eta)
        : tab.phase === "after" ? "All " + done + " points measured · setting the final values" + etaText(eta)
        : tab.phase === "stopping" ? "Stopping · sending the drives it touched back to safe"
        : done + (total ? " / " + total : "") + " points" + etaText(eta);
    }
    else if (!tab.final) v.count.textContent = tab.status === "missing" ? "This run's data could not be read." : "Finished · analysing…";
    else if (m.status === "done") v.count.textContent = "Finished · " + done + " points" + (m.duration_s != null ? " in " + durText(m.duration_s) : "");
    else if (m.status === "aborted") v.count.textContent = "Stopped after " + done + (m.planned_points ? " of " + m.planned_points : "") + " points" + (m.reason ? ": " + m.reason : "");
    else if (m.status === "interrupted") v.count.textContent = "Cut short after " + done + (m.planned_points ? " of " + m.planned_points : "") +
      " points: Kelvin stopped while it measured (a crash or a power cut). Every point up to then is kept.";
    else v.count.textContent = "Not finished (" + done + " points recorded)";
    if (tab.final && !v.finalDrawn) {  // a finished run is drawn once
      v.finalDrawn = true;
      clearTimeout(tab.timer); tab.timer = 0;
      const a = tab.final.analysis ? analysisBlock(tab.final.analysis) : null;
      v.analysis.replaceChildren(...(a ? [a.box] : []));
      v.results.replaceChildren(...(a && a.table ? [a.table] : []));
      v.keys.hidden = !(a && a.table);
      if (a) { v.keysPending = true; v.keysRows = a.table ? a.rows : 0; v.whole = a.whole; }
      v.charts.replaceChildren(runCharts(tab.final));
      v.plan.hidden = !tab.final.plan_text;
      v.planBox.querySelector("pre").textContent = tab.final.plan_text || "";
      v.again.hidden = false;
      runFiles(tab, v.files, v.fileView);
      if (!v.table.hidden) v.table.replaceChildren(runTable(tab.final));
      keysOnce(tab);   // drawn whole first: judged by what it takes
      return;
    }
    if (running && !tab.timer) {  // live plots: redraw at most a few times a second
      tab.timer = setTimeout(() => {
        tab.timer = 0;
        if (tab.final) return;
        v.charts.replaceChildren(runCharts(runViewData(tab), 6));
        if (!v.table.hidden) v.table.replaceChildren(runTable(runViewData(tab)));
      }, 300);
    }
  }
  // a finished run's key results (14 rows or fewer) and its whole assessment (Next, the issues) open by themselves only
  // where there is room for them. Judged once, when its view first has a size: a run that finished out of sight (the
  // Sequence shown, Runs closed) is judged when it is first shown. Beside the plots (the view 900 px or wider, a window
  // not short): the key results, and the whole assessment when the side then holds it without scrolling. Above the
  // plots: both, or else the key results alone, when the plots keep at least 2/5 of their width in height (no flatter
  // than 5:2) and stay above their least (so the view needs no scrolling). Each is opened, measured and folded again in
  // one go, before anything is drawn: nothing moves. (A plot is drawn again at its new height a moment later, so its
  // picture is not what is measured: its place is)
  function keysOnce(tab) {
    const v = tab && tab.view;
    if (!v || !v.keysPending) return;
    const w = tab.body.getBoundingClientRect().width / layoutZoom();
    if (!w) return;
    v.keysPending = false;
    const keys = v.keysRows > 0 && v.keysRows <= 14;
    const open = (k, all) => { if (keys) rvOpen(v.keys, k, null, true); v.whole(all); };
    if (w >= 900) {
      if (document.documentElement.classList.contains("fit-tight")) return;
      open(true, true);
      if (v.side.scrollHeight > v.side.clientHeight + 1) v.whole(false);
      return;
    }
    const least = parseFloat(getComputedStyle(v.charts).minHeight) || 0;   // the page's pixels, as w
    const room = () => {
      const c = v.charts.getBoundingClientRect(), z = layoutZoom();
      return c.height >= c.width * 0.4 && c.height / z > least + 1;
    };
    for (const [k, all] of [[true, true], [true, false]]) {
      open(k, all);
      if (room()) return;
    }
    open(false, false);
  }
  // shown, full screen, or the Scan shown again: then once the layout that shows it is done (Runs opening lays out
  // after it says what is in sight), still before anything is drawn
  const keysNow = () => keysOnce(RUNS.tabs.get(RUNS.active));
  document.addEventListener("kelvin:layout", keysNow);
  document.addEventListener("kelvin:seq", () => queueMicrotask(keysNow));

  // The side panels each open and close on their own, and each keeps the width it was left at (drag its left edge).
  // Beside the main column (the chat or Home), which keeps at least CHAT_MIN pixels, they stand side by side in the
  // order of the top bar's buttons: Team chat, Analysis, Runs with the scan editor beside it, then Lab. Short of room,
  // they shrink alike down to their least widths; when one more will not fit even so, the pane looked at least
  // recently (opened, pressed or focused: LOOK) is put away, its button flat and what it showed kept for next time.
  // A pane that cannot stand beside the main column even alone takes the content area by itself (.solo), and the main
  // column waits out of sight until Chat or Home brings it back. Nothing stands over or under anything. They never
  // hide the sidebar: only its button does (SIDE.hidden), and while it stands beside the page they take the room it
  // leaves; where it cannot stand beside the main column it takes the content area as a pane alone does (.side-solo).
  // Every width here is in the page's own pixels: the window's divided by the page size (layoutZoom), so a panel keeps
  // its width whatever the page size.
  // A least width is the narrowest each pane's head and rows hold at, at 85, 90 and 100 %: Runs' is its head on History
  // (Sequence | History with a two-digit count, Report…, ⤢, ×), 362 on a touch screen, whose head buttons are 40 px.
  // So Runs stands beside Lab or Analysis at 1280×720 (90 %: 1422 page px) with the list shown, and on an iPad's 1180
  // with the list hidden. The scan editor does not fit beside Runs there (it holds down to 318) and takes its place.
  // On a touch screen the editor's fields are 40 px, and its body fits whole only with its two-column rows (a pane of
  // 790 or more): it needs 800 there, so on an iPad it takes the content area alone rather than scroll inside
  const CHAT_MIN = 520, SIDEBAR_W = 264;
  const PANES = {   // id: [open?, its column, its least width, where its width is kept, its least on a touch screen]
    teampanel: [() => TEAM.open, "workcol", 280, "lh.teamWidth"],
    anpane: [() => ANA.open, "workcol", 292, "lh.anWidth"],
    runpane: [() => RUNS.open, "workcol", 346, "lh.runsWidth", 362],
    edpane: [() => ED.open, "workcol", 360, "lh.edWidth", 800],
    labpanel: [() => S.labOpen, "sidecol", 292, "lh.labWidth"],
  };
  const COARSE = window.matchMedia("(pointer: coarse)");   // a touch screen
  const PANE_W = {};   // each pane's width as the person left it
  const sum = (xs) => xs.reduce((a, b) => a + b, 0);
  const openPanes = () => Object.keys(PANES).filter((id) => PANES[id][0]());
  const least = (id) => (COARSE.matches && PANES[id][4]) || PANES[id][2];
  const paneWant = (id) => Math.max(PANE_W[id] || 0, least(id));   // as left, never under its least (a touch screen's)
  // when each pane was last looked at (opened, or a press or the focus inside it), as a count that only grows; the
  // panes open at the last layout; whether the window is still changing size (wait); and whether the pane opening now
  // was asked for by the person or a waiting request (asked: paneShow)
  const LOOK = {}, ROOM = { looks: 0, shown: [], wait: false, timer: 0, away: null, asked: false };
  const look = (id) => { LOOK[id] = ++ROOM.looks; };
  const byLook = (ids) => [...ids].sort((a, b) => (LOOK[a] || 0) - (LOOK[b] || 0));   // least recently looked at first
  // the pane being typed in: the one that holds the focus in a field
  function typedIn(open) {
    const a = document.activeElement;
    if (!a || !(a.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName))) return null;
    return open.find((id) => $(id).contains(a)) || null;
  }
  // the chat and Home out of sight: under full screen, a pane alone or the list in the main column's place
  const mainOut = (app) => ["full", "solo", "side-solo"].some((c) => app.classList.contains(c));
  // the page size (Kelvin.zoom: 1, .9, .85 or the person's choice), and the window in the page's own pixels; pointer
  // and rect lengths are the screen's, divided by it before they become a width
  function layoutZoom() {
    const k = window.Kelvin, z = k && typeof k.zoom === "function" ? Number(k.zoom()) : 1;
    return z > 0 && isFinite(z) ? z : 1;
  }
  const layoutW = () => window.innerWidth / layoutZoom();
  const layoutH = () => window.innerHeight / layoutZoom();
  const sideBeside = () => (SIDE.hidden || sideNarrow() ? 0 : SIDEBAR_W);   // the sidebar's width beside the page
  // widths that fit in `room`: each as wanted, or, when they don't fit, shrunk alike, none below its least (lo), in
  // whole pixels that fill the room to its last whole pixel (never past it: a width shown, read back and given again
  // stays as it is)
  function share(want, lo, room) {
    if (sum(want) <= room) return want;
    const slack = want.map((w, i) => w - lo[i]);
    const k = Math.max(0, room - sum(lo)) / Math.max(1, sum(slack));
    const out = want.map((w, i) => Math.floor(lo[i] + slack[i] * k));
    out[out.length - 1] += Math.max(0, Math.floor(room - sum(out)));
    return out;
  }
  // the width the panes may take beside the main column: the window's, less the sidebar's while it stands beside them
  function paneRoom() {
    return layoutW() - sideBeside() - CHAT_MIN;
  }
  // the one pane open, when it cannot stand beside the main column even alone: it takes the content area by itself
  function soloPane() {
    const open = openPanes();
    return open.length === 1 && paneRoom() < least(open[0]) ? open[0] : null;
  }
  // a pane would stand beside the main column and the panes open now, all at their least widths: opening it puts
  // none of them away and leaves the main column in its place (under full screen too, for when it ends)
  function fitsBeside(id) {
    return isOpen(id) || sum([...openPanes(), id].map(least)) <= paneRoom();
  }
  // the layout for the panels open now, the sidebar and the window, after anything that opens, closes or sizes one
  // and on every resize; the classes say how app.css places them, in the page's own pixels (no media query: they
  // follow the page size)
  function fitLayout() {
    if (FULL.length && !isOpen(FULL[0])) FULL.length = 0;   // the pane in full screen closed: full screen ends with it
    const app = $("app"), open = openPanes(), out = mainOut(app);
    for (const id of open) if (!ROOM.shown.includes(id)) look(id);   // opened since the last layout: looked at now
    ROOM.shown = open;
    // One more will not fit beside the main column even at the least widths: the pane looked at least recently is put
    // away, and its toggle lays out again. Never the pane being typed in, unless the room is for one the person or a
    // waiting request asked for (paneShow): a run that starts, or Kelvin's analyze, never takes it away mid-sentence
    // (with no other to go, the newcomer goes, and waits for its button). Not while full screen covers them (they wait
    // under it), nor while the window is still changing size or out of sight: a window being sized, or minimized,
    // closes nothing
    const typing = ROOM.asked ? null : typedIn(open);
    const away = byLook(open.filter((id) => id !== typing))[0];
    if (open.length > 1 && !FULL.length && !ROOM.wait && !document.hidden && ROOM.away !== away &&
        sum(open.map(least)) > paneRoom()) {
      ROOM.away = away;   // once: a toggle that kept it open would come back here
      try { TOGGLE[away](false); } finally { ROOM.away = null; }
      return;
    }
    const solo = FULL.length ? null : soloPane();
    for (const col of ["workcol", "sidecol"]) $(col).hidden = !open.some((id) => PANES[id][1] === col);
    app.classList.toggle("side-hidden", SIDE.hidden);
    app.classList.toggle("side-solo", sideSolo());
    app.classList.toggle("solo", !!solo);
    for (const id of ["termpane", ...Object.keys(PANES)]) if ($(id).hidden) $(id).style.animation = "";   // closed: comes in again next time
    if (!solo) {   // side by side, each as wide as it was left, shrunk alike to fit, none below its least
      const got = share(open.map(paneWant), open.map(least), paneRoom());
      open.forEach((id, i) => { $(id).style.width = got[i] + "px"; });
    }
    syncFull();
    syncSide();
    // the chat or Home in sight again (full screen ended, a pane alone or the list gone): there already, not coming in
    // (setMode lets the next switch of Chat | Home come in)
    if (out && !mainOut(app)) for (const v of ["scroller", "homeview"]) $(v).style.animation = "none";
    layoutSaid();
  }
  // a pane's left edge, dragged or moved with the arrow keys: its width. Only the main column gives or takes the room;
  // the other panes keep the widths they show, so the edge follows the pointer and what is seen when it is let go stays
  function sizePane(id, want) {
    const shown = (p) => Math.round($(p).getBoundingClientRect().width / layoutZoom());   // in the page's pixels
    const others = openPanes().filter((p) => p !== id);
    for (const p of others) PANE_W[p] = shown(p);
    const most = Math.floor(paneRoom() - sum(others.map((p) => PANE_W[p])));   // whole pixels: no width creeps
    PANE_W[id] = Math.round(Math.min(Math.max(least(id), most), Math.max(least(id), want || 0)));
    fitLayout();
  }
  // a side panel's left edge, dragged or moved with the arrow keys, sets its width: set(w) takes the width and keeps it
  // in bounds, done() keeps it for next time
  function edgeResize(handle, pane, set, done) {
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const r = pane.getBoundingClientRect(), z = layoutZoom(), grab = e.clientX - r.left;   // the edge keeps its place
      handle.setPointerCapture(e.pointerId);                                                // under the pointer
      document.body.classList.add("resizing");
      const move = (ev) => set((r.right - ev.clientX + grab) / z);   // the screen's pixels, as the page's
      let ended = false;
      const up = () => {   // let go, or the pointer lost (the handle hidden mid-drag)
        if (ended) return;
        ended = true;
        handle.removeEventListener("pointermove", move);
        document.body.classList.remove("resizing");
        done();
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up, { once: true });
      handle.addEventListener("lostpointercapture", up, { once: true });
    });
    handle.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      set(pane.getBoundingClientRect().width / layoutZoom() + (e.key === "ArrowLeft" ? 32 : -32));
      done();
    });
  }
  // Full screen, from a panel's own button: that panel alone takes the window below the top bar, whose buttons, badges
  // and notice row stay; the chat, the conversation list and the other panels stay as they were, out of sight. Its own
  // button again, Esc, or closing it ends it, and the layout under it comes back. A panel the person asks for meanwhile
  // (another top-bar button, Analyze, Edit on a scan) or one with a request waiting for them takes the screen in its
  // place, and full screen stays. Chat or Home ends it (either side, even the one chosen), as does whatever needs the
  // chat: an approval asked there, another chat, Ask Kelvin. A pane alone (.solo) is the same picture without asking:
  // the one pane that cannot stand beside the main column, with nothing under it.
  const FULL = [];   // the panel in full screen: one at most
  const isOpen = (id) => (id === "termpane" ? TERM.open : PANES[id][0]());
  // the panels' top-bar buttons, each raised exactly when its panel is on the screen; and how each panel opens or closes
  const PILLS = { termpane: "toggle-term", teampanel: "toggle-team", anpane: "toggle-analysis", runpane: "toggle-runs",
                  labpanel: "toggle-lab" };
  const TOGGLE = { termpane: toggleTerm, teampanel: toggleTeam, anpane: toggleAnalysis, runpane: toggleRuns,
                   edpane: toggleEditor, labpanel: toggleLab };
  // a panel on the screen now: open, and not out of sight: in full screen the one shown; else not under the sidebar
  // that takes the content area, and the terminal (under the chat) not while a pane alone has the main column's place
  const paneVisible = (id) => !!TOGGLE[id] && isOpen(id) && (FULL.length ? FULL[0] === id
    : !sideSolo() && !(id === "termpane" && soloPane()));
  function syncFull() {
    if (FULL.length && !isOpen(FULL[0])) FULL.length = 0;   // closed: full screen ends with it
    const top = FULL[0] || null, app = $("app");
    app.classList.toggle("full", !!top);
    app.classList.toggle("full-term", top === "termpane");
    for (const id of ["termpane", ...Object.keys(PANES)]) $(id).classList.toggle("full-on", id === top);
    for (const col of ["workcol", "sidecol"]) {
      $(col).classList.toggle("full-in", !!top && top !== "termpane" && PANES[top][1] === col);
    }
    for (const b of document.querySelectorAll(".full-btn")) {
      const on = b.dataset.pane === top;
      b.setAttribute("aria-pressed", String(on));
      b.title = on ? "Exit full screen (Esc)" : "Full screen";
    }
    for (const [id, pill] of Object.entries(PILLS)) {   // after each toggle's own lines, which say only "open"
      const on = paneVisible(id);
      $(pill).setAttribute("aria-pressed", String(on));
      $(pill).classList.toggle("on", on);
    }
  }
  function toggleFull(id) {   // a panel's own button
    if (FULL[0] === id) FULL.length = 0; else FULL[0] = id;
    fitLayout();
  }
  // an open panel to the screen, wherever it waits: in full screen in place of the one there (full screen stays); under
  // the sidebar that takes the content area (that hidden for now: its button keeps the person's choice); and the
  // terminal, under the chat, in place of a pane alone (that put away: the main column back)
  function fullShow(id) {
    if (!isOpen(id) || paneVisible(id)) return;
    look(id);
    if (FULL.length) FULL[0] = id;
    else if (sideSolo()) SIDE.hidden = true;
    fitLayout();   // the panes make room first; then a pane left alone in the terminal's way goes
    const solo = !FULL.length && id === "termpane" ? soloPane() : null;
    if (solo) TOGGLE[solo](false);
  }
  // the main column back on the screen, as Chat or Home brings it (so do another chat, an approval asked in the chat and
  // Ask Kelvin): full screen ended, the sidebar that takes the content area hidden for now, the panes that waited under
  // full screen making room as ever, and then a pane left alone in the main column's place put away, what it showed
  // kept for next time
  function exitFull() {
    FULL.length = 0;
    if (sideSolo()) SIDE.hidden = true;
    fitLayout();
    const solo = soloPane();
    if (solo) TOGGLE[solo](false);
  }
  // One click model for every way of opening a panel. Its own top-bar button (and Ctrl+` the terminal's) puts it away
  // when it is on the screen, else brings it there (pillClick). Anything else that asks for a panel (a Home tile,
  // Analyze, Edit on a scan, a waiting request) only ever shows it (paneShow): closed, it opens; in full screen it takes
  // the screen; already on the screen it stays, and its head rings. view: Runs on its Scan ("scan") or its Sequence
  // ("schedule")
  function pillClick(id, toggle) {
    if (paneVisible(id)) toggle(false);
    else paneShow(id);
  }
  function paneShow(id, view) {
    if (!TOGGLE[id]) return false;
    const there = id !== "runpane" || !view ? true : RUNS.view === view;
    if (isOpen(id)) look(id);   // asked for: looked at (one that opens is looked at as it opens)
    if (paneVisible(id) && there) { ring($(id).querySelector(":scope > header")); return true; }
    const was = ROOM.asked;
    ROOM.asked = true;   // asked for: it may take the room of the pane being typed in (fitLayout)
    try {
      if (id === "runpane" && view === "schedule") toggleSeq(true);
      else if (id === "runpane" && view === "scan") showScan();
      else if (!isOpen(id) && (id !== "edpane" || ED.parts)) TOGGLE[id](true);   // the editor only with a scan in it
      fullShow(id);
    } finally { ROOM.asked = was; }
    return isOpen(id);
  }
  function paneHide(id) {   // closed, wherever it is; what it showed is kept for next time
    if (TOGGLE[id] && isOpen(id)) TOGGLE[id](false);
  }
  // a place already on the screen, asked for again: it stays, and rings once (app.css .k-ring, colour only, so the
  // same with less motion) for the eye to find it
  const RINGS = new WeakMap();
  function ring(el) {
    if (!el) return;
    clearTimeout(RINGS.get(el));
    el.classList.remove("k-ring");
    void el.offsetWidth;   // rung again while ringing: from the start
    el.classList.add("k-ring");
    RINGS.set(el, setTimeout(() => el.classList.remove("k-ring"), 650));
  }
  // The layout as this browser leaves it (per browser, never in the link, whose hash holds the key): the panels open,
  // in the order they were looked at, full screen and Scan | Sequence, beside what each part keeps itself (the
  // widths above, the sidebar, the terminal and its height; never Chat | Home, as Kelvin opens on the chat). After
  // every change the parts kept apart hear of it (kelvin:layout: the panels on the screen, the one in full screen,
  // Chat | Home, the sidebar shown).
  const LAYOUT = { back: false, said: "", kept: "" };
  function layoutSaid() {
    const now = { panes: Object.keys(TOGGLE).filter(paneVisible), full: FULL[0] || null,
                  mode: S.mode === "home" ? "home" : "chat", side: !SIDE.hidden };
    const panes = byLook(openPanes()).filter((id) => id !== "edpane");   // the one looked at last, last
    const kept = JSON.stringify([panes, now.full, RUNS.view]);
    if (LAYOUT.back && kept !== LAYOUT.kept) {   // never before what was kept is back
      LAYOUT.kept = kept;
      store("lh.panes", JSON.stringify(panes));
      store("lh.full", now.full && now.full !== "edpane" ? now.full : "");
      store("lh.runsView", RUNS.view);
    }
    const said = JSON.stringify(now);
    if (said === LAYOUT.said) return;
    LAYOUT.said = said;
    emit("layout", now);
  }
  // once the page is ready, what was kept comes back: the panes looked at last first, as many as stand beside the main
  // column at their least widths; never one that cannot stand there even alone (it would hide the main column, as on a
  // phone), nor the scan editor
  function restoreLayout() {
    let kept = [];
    try { kept = JSON.parse(store("lh.panes") || "[]"); } catch (e) { /* none kept */ }
    if (!Array.isArray(kept)) kept = [];
    const view = store("lh.runsView");
    if (view === "schedule" || view === "scan") RUNS.view = view;
    let room = paneRoom() - sum(openPanes().map(least));   // a pane open already (a waiting request) keeps its place
    const back = [];
    for (const id of [...kept].reverse()) {
      if (!PANES[id] || id === "edpane" || isOpen(id) || back.includes(id) || least(id) > room) continue;
      room -= least(id);
      back.unshift(id);
    }
    for (const id of back) {   // in the order they were looked at: the last one is the newest look
      TOGGLE[id](true);
      $(id).style.animation = "none";   // there already, not coming in (cleared when it closes: fitLayout)
    }
    const full = store("lh.full");
    if (full && full !== "edpane" && TOGGLE[full] && isOpen(full)) FULL[0] = full;
    LAYOUT.back = true;
    LAYOUT.said = LAYOUT.kept = "";
    fitLayout();
  }
  function initPanes() {
    const kept = (key) => Number(store(key)) || 0;
    // the first time, Runs and Analysis open at 42 % of the window, never under 420: Runs up to 960 (on a big monitor
    // a run's analysis then stands beside its plots), Analysis up to 720 (until it has its own width: the one it
    // shared with Runs). A width dragged is kept
    const wide = Math.max(420, layoutW() * 0.42);
    PANE_W.runpane = kept("lh.runsWidth") || Math.round(Math.min(960, wide));
    PANE_W.anpane = kept("lh.anWidth") || kept("lh.runsWidth") || Math.round(Math.min(720, wide));
    PANE_W.edpane = kept("lh.edWidth") || 460;
    PANE_W.labpanel = kept("lh.labWidth") || 380;
    PANE_W.teampanel = kept("lh.teamWidth") || PANE_W.labpanel;
    if (sideNarrow()) SIDE.hidden = true;   // a narrow window opens on the chat or Home, never on the list (not kept)
    for (const id of Object.keys(PANES)) {
      edgeResize($(id + "-resize"), $(id), (w) => sizePane(id, w), () => {
        for (const p of Object.keys(PANES)) store(PANES[p][3], String(PANE_W[p]));
      });
      // looking at a pane: a press or the focus inside it (opening it is one too: fitLayout); kept with the layout
      const looked = () => { look(id); layoutSaid(); };
      $(id).addEventListener("pointerdown", looked, true);
      $(id).addEventListener("focusin", looked);
    }
    // a pane comes in once: in sight again later (full screen ended, the sidebar or a pane alone gone) it is just there
    for (const id of ["termpane", ...Object.keys(PANES)]) {
      const el = $(id);
      el.addEventListener("animationend", (e) => { if (e.target === el) el.style.animation = "none"; });
    }
    // while the window changes size the panes only fit; once it rests (and the page size has followed it, 50 ms),
    // the least recently looked at makes room. A page brought back to sight fits again too
    window.addEventListener("resize", () => {
      ROOM.wait = true;
      clearTimeout(ROOM.timer);
      ROOM.timer = setTimeout(() => { ROOM.wait = false; fitLayout(); }, 160);
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) fitLayout(); });
    for (const b of document.querySelectorAll(".full-btn")) b.addEventListener("click", () => toggleFull(b.dataset.pane));
    document.addEventListener("keydown", (e) => {   // Esc ends full screen, unless a field, a menu or a dialog takes it
      if (e.key !== "Escape" || !FULL.length || e.defaultPrevented) return;
      if (document.querySelector("dialog[open], .pane-menu:not([hidden])")) return;
      if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
      toggleFull(FULL[0]);   // as its own button: the layout under it back
    });
    Object.assign(window.Kelvin, { paneShow, paneHide, paneVisible, ring });   // the click model, lent to home.js, seq.js
    document.addEventListener("kelvin:seq", layoutSaid);   // Scan | Sequence, kept as the person leaves it
    document.addEventListener("kelvin:ready", restoreLayout, { once: true });
  }
  function initRunsPane() {
    if (store("lh.runsView") === "schedule") RUNS.view = "schedule";   // Scan | Sequence as this browser left it
    $("toggle-runs").addEventListener("click", () => pillClick("runpane", toggleRuns));
    $("close-runs").addEventListener("click", () => toggleRuns(false));
    $("open-scan").addEventListener("click", () => showView("scan"));
    $("open-schedule").addEventListener("click", () => showView("schedule"));
    $("runs-seg").addEventListener("keydown", runsSegKeys);
    $("scans-filter").addEventListener("input", renderScans);
    $("scans-import").addEventListener("click", importScript);
    $("scans-new").addEventListener("click", () => openEditor(null));
    $("scans-edit").addEventListener("click", () => { const picked = pickedScans(); if (picked.length === 1) editScan(picked[0]); });
    $("sched-add").addEventListener("click", () => queuePicked(insertAt()));
    $("scans-path").addEventListener("submit", (e) => { e.preventDefault(); if ($("scans-path-input").value.trim()) importPath($("scans-path-input").value.trim()); });
    $("scans-path-input").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); $("scans-path").hidden = true; $("scans-import").focus(); } });
    initSchedule();
    window.addEventListener("resize", fitLayout);
  }

  // the compact card a run leaves in the conversation; its data live in the runs pane
  function runCard(plan) {
    const state = h("span", { class: "state" }, h("span", { class: "spinner" }));
    const title = h("span", { class: "rc-title" }, "Measuring ", h("code", {}, plan || "plan"));
    const count = h("span", { class: "rc-count" });
    const open = h("button", { class: "rc-open", type: "button" }, "Show data");
    const bar = h("i");
    const barBox = h("div", { class: "bar thin" }, bar);
    const text = h("div", { class: "rc-text", hidden: true });
    const el = h("div", { class: "runcard" }, h("div", { class: "rc-head" }, state, title, count, open), barBox, text);
    const card = { el, runId: null, plan };
    open.addEventListener("click", () => {
      if (!card.runId) return;
      openRun(card.runId, card.plan);
    });
    card.progress = (ev) => {
      card.runId = ev.run_id; card.plan = ev.plan;
      title.replaceChildren("Measuring ", h("code", {}, ev.plan));
      bar.style.width = (ev.total ? Math.round(100 * ev.done / ev.total) : 100) + "%";
      barBox.classList.toggle("open", !ev.total && ev.status === "running");   // a lab script's: no total
      count.textContent = ev.total ? ev.done + " / " + ev.total : ev.done + " points";  // the time left is in the runs pane
    };
    card.finish = async (runId) => {
      card.runId = runId;
      let data;
      try { data = await getRun(runId); } catch (e) { setState(state, "missing"); count.textContent = "data unavailable"; return; }
      const m = data.meta || {};
      card.plan = data.plan;
      setState(state, m.status || "unfinished");
      title.replaceChildren(m.status === "aborted" ? "Stopped " : "Measured ", h("code", {}, data.plan));
      count.textContent = (m.points != null ? m.points : data.rows.length) + " points";
      barBox.hidden = true;
      const a = data.analysis;
      if (a) {
        text.hidden = false;
        text.replaceChildren(h("p", {}, a.assessment), h("p", { class: "rc-next" }, h("b", {}, "Next: "), a.next));
      }
    };
    return card;
  }

  function runTable(data) {
    const cols = [...data.axes, ...data.scalars];
    const head = h("tr", {}, ...cols.map((c) => h("th", {}, c.key + (c.unit ? " (" + c.unit + ")" : ""))));
    const rows = data.rows.slice(0, 500).map((r) => h("tr", {}, ...cols.map((c) =>
      h("td", {}, typeof r[c.key] === "number" ? Number(r[c.key].toPrecision(6)).toString() : String(r[c.key])))));
    return h("table", {}, h("thead", {}, head), h("tbody", {}, ...rows));
  }

  // the files Kelvin keeps in a run's folder: its report, slides and data, opened on this computer; a script run's
  // own account of what ran (view: script.py as it ran, its output, its commands), read here as text, never opened
  const RUN_FILE_LABEL = { "report.pdf": "Report (PDF)", "report.pptx": "Slides", "run.h5": "Data (HDF5)",
                           "script.py": "Script", "output.txt": "Output", "commands.jsonl": "Commands" };
  // box: the row of buttons (what does not fit goes, from its end, into its ⋯); shown: where a text file is read
  function runFiles(tab, box, shown) {
    const files = (tab.final && tab.final.files) || [];
    const base = "/api/runs/" + encodeURIComponent(tab.runId);
    const openIt = async (path, what) => {
      try { await api(path, { body: {} }); toast("Opening " + what + "…"); } catch (e) { toast(e.message); }
    };
    const text = h("pre", { class: "rv-file-text", "data-region": "run" });
    const cut = h("p", { class: "hint rv-file-cut", hidden: true });
    shown.replaceChildren(cut, text);
    shown.hidden = true;
    const readIt = async (name) => {   // a long one comes as its last 256 KB (X-Kelvin-Cut: its whole size)
      if (!shown.hidden && shown.dataset.name === name) { shown.hidden = true; return; }
      try {
        const res = await fetch(base + "/files/" + encodeURIComponent(name), { headers: { "X-Kelvin-Token": token } });
        if (!res.ok) throw new Error(((await res.json().catch(() => ({}))).error) || "HTTP " + res.status);
        const got = await res.arrayBuffer();
        const whole = Number(res.headers.get("X-Kelvin-Cut")) || 0;
        text.textContent = new TextDecoder().decode(got);
        cut.textContent = whole ? "The last " + bytesText(got.byteLength) + " of " + bytesText(whole) + "." : "";
        cut.hidden = !whole;
        shown.dataset.name = name;
        shown.hidden = false;
      } catch (e) { toast(e.message); }
    };
    const fileBtn = (f) => runButton(RUN_FILE_LABEL[f.name] || f.name, f.name, f.view ? "doc" : null, () => (f.view ? readIt(f.name)
      : openIt(base + "/files/" + encodeURIComponent(f.name) + "/open", f.name)));
    const named = new Map(files.map((f) => [f.name, f]));
    const file = (name) => (named.has(name) ? fileBtn(named.get(name)) : null);
    const dl = runDownloads(tab.runId);
    const folder = runButton("Open folder", "The run's folder, on this computer", "folder", () => openIt(base + "/open", "the run's folder"));
    // the most used first: its report, Analyze, a script's output and text, the data
    const row = [file("report.pdf"), dl.analyze, file("output.txt"), file("script.py"), dl.csv, file("report.pptx"), file("run.h5"),
      file("commands.jsonl"), dl.xlsx, dl.npz, dl.eln, ...files.filter((f) => !RUN_FILE_LABEL[f.name]).map(fileBtn), folder].filter(Boolean);
    const more = h("button", { class: "btn small rv-files-more", type: "button", title: "More", "aria-label": "More", "aria-haspopup": "menu",
                               hidden: true }, icon("dots"));
    more.addEventListener("click", (e) => {
      e.stopPropagation();
      popMenu(more, row.filter((b) => b.hidden).map((b) => ({ label: b.dataset.label, icon: b.dataset.icon || null, onClick: () => b.click() })));
    });
    box.replaceChildren(...row, more);
    box.hidden = false;
    fitRow(box);
    if (window.ResizeObserver && !box.fitted) { box.fitted = new ResizeObserver(() => fitRow(box)); box.fitted.observe(box); }
  }
  // a button of a run's row: its name, its tip, the icon it has in the ⋯ menu (and beside its name for a download)
  function runButton(label, tip, iconName, onClick) {
    const b = h("button", { class: "btn small", type: "button", title: tip }, iconName === "download" ? icon("download") : null, label);
    b.dataset.label = label;
    if (iconName) b.dataset.icon = iconName;
    b.addEventListener("click", onClick);
    return b;
  }
  // one row of buttons: those that do not fit go, from its end, into its ⋯ (measured, so it holds in every language)
  function fitRow(row) {
    const more = row.querySelector(".rv-files-more"), btns = [...row.children].filter((b) => b !== more);
    if (!more || !row.clientWidth) return;
    for (const b of btns) b.hidden = false;
    more.hidden = true;
    if (row.scrollWidth <= row.clientWidth + 1) return;
    more.hidden = false;
    for (let k = btns.length - 1; k > 0 && row.scrollWidth > row.clientWidth + 1; k--) btns[k].hidden = true;
  }
  const bytesText = (n) => (n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB"
    : n < 1073741824 ? (n / 1048576).toFixed(1) + " MB" : (n / 1073741824).toFixed(1) + " GB");

  // the run's data written by Kelvin's code (/api/runs/{id}/export): CSV, Excel or NumPy, never copied by hand
  function runDownloads(runId) {
    const get = async (fmt, label) => {
      try {
        const res = await fetch("/api/runs/" + encodeURIComponent(runId) + "/export?format=" + fmt,
          { headers: { "X-Kelvin-Token": token } });
        if (!res.ok) throw new Error(((await res.json().catch(() => ({}))).error) || "HTTP " + res.status);
        const named = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
        const url = URL.createObjectURL(await res.blob());
        const a = h("a", { href: url, download: named ? named[1] : runId + "." + fmt });
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
      } catch (e) { toast("Could not download the " + label + ": " + e.message); }
    };
    // {csv, xlsx, npz, eln, analyze}: buttons for runFiles' row (a download with its icon)
    const out = {};
    for (const [fmt, label] of [["csv", "CSV"], ["xlsx", "Excel"], ["npz", "NumPy"]]) {
      out[fmt] = runButton(label, "Download the data as " + label, "download", () => get(fmt, label + " file"));
    }
    out.eln = runButton("Lab notebook", "The run's record as Markdown with its plots, for a lab notebook (ELN)", "download",
      () => fetchDownload("/api/runs/" + encodeURIComponent(runId) + "/eln", runId + ".zip", "lab notebook record"));
    out.analyze = runButton("Analyze", "Open this run in the Analysis pane", null,
      () => openAnalysis({ run: runId, asked: true }));   // Kelvin's analysis subagent works on it there
    return out;
  }
  // a file Kelvin's code writes, downloaded with the access key (its name from its Content-Disposition)
  function fileName(res, fallback) {   // filename*=UTF-8''… first: a run's own name in any script
    const cd = res.headers.get("Content-Disposition") || "";
    const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
    if (star) { try { return decodeURIComponent(star[1]); } catch (e) { /* the plain one */ } }
    const named = /filename="([^"]+)"/.exec(cd);
    return named ? named[1] : fallback;
  }
  async function fetchDownload(url, fallback, what, body) {
    try {
      const res = await fetch(url, body ? { method: "POST", headers: { "X-Kelvin-Token": token, "Content-Type": "application/json" },
        body: JSON.stringify(body) } : { headers: { "X-Kelvin-Token": token } });
      if (!res.ok) throw new Error(((await res.json().catch(() => ({}))).error) || "HTTP " + res.status);
      const name = fileName(res, fallback);
      const href = URL.createObjectURL(await res.blob());
      const a = h("a", { href, download: name });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 30000);
    } catch (e) { toast("Could not download the " + what + ": " + e.message); }
  }

  // ------------------------------------------------------------------ reports
  function reportNames(content) {
    return [...String(content || "").matchAll(/^report: ([\w./-]+\.(?:pdf|pptx))/gm)].map((m) => m[1]);
  }
  async function downloadReport(name) {
    try {
      const res = await fetch("/api/reports/" + encodeURIComponent(name), { headers: { "X-Kelvin-Token": token } });
      if (!res.ok) throw new Error(res.status === 404 ? "it is no longer in the reports folder" : "HTTP " + res.status);
      const url = URL.createObjectURL(await res.blob());
      const a = h("a", { href: url, download: name });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (e) { toast("Could not download the report: " + e.message); }
  }
  function reportFiles(names) {
    return h("div", { class: "report-files" }, ...names.map((name) => {
      const open = h("button", { class: "btn small", type: "button" }, "Open");
      open.addEventListener("click", async () => {
        try { await api("/api/reports/" + encodeURIComponent(name) + "/open", { body: {} }); toast("Opening " + name + "…"); }
        catch (e) { toast(e.message); }
      });
      const save = h("button", { class: "link soft", type: "button" }, "Download");
      save.addEventListener("click", () => downloadReport(name));
      return h("div", { class: "report-file" }, icon("doc"),
        h("span", { class: "rf-name" }, h("b", {}, name.endsWith(".pdf") ? "PDF" : "Slides"), h("span", {}, name)), open, save);
    }));
  }
  function openReport() {
    const sel = $("report-scope");
    const chat = S.chats.find((c) => c.id === S.chatId);
    const opts = [];
    const shown = ANA.runs.find((r) => r.run_id === ANA.run);   // the dataset the Analysis pane shows
    if (shown && shown.status !== "running") opts.push(["run", "This run: " + (shown.plan || shown.run_id)]);
    if (chat && chat.project) opts.push(["project", "Runs in project " + (projectName(chat.project) || "")]);
    if (S.chatId && !S.incognito) opts.push(["chat", "Runs in this chat"]);
    opts.push(["today", "Today's runs"], ["last", "The last 5 runs"]);
    sel.replaceChildren(...opts.map(([v, label]) => new Option(label, v)));
    $("report-files").hidden = true;
    $("report-msg").textContent = "";
    if (!$("eln-day").value) { const d = new Date(); $("eln-day").value = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
    $("report-dialog").showModal();
    loadLibrary();
  }
  // the reports Kelvin keeps: per project its master report, its devices' reports and their history
  async function loadLibrary() {
    const box = $("report-library");
    let t;
    try { t = await api("/api/reports/tree"); } catch (e) { box.replaceChildren(h("p", { class: "hint" }, e.message)); return; }
    const kind = (name) => (name.endsWith(".pdf") ? "PDF" : "Slides");
    const pdfFirst = (names) => [...names].sort((a, b) => (a.endsWith(".pdf") ? 0 : 1) - (b.endsWith(".pdf") ? 0 : 1));
    const dated = (name) => {   // history/2026-10-19_master.pdf -> Oct 19 PDF
      const m = name.match(/(\d{4})-(\d{2})-(\d{2})_master\.(pdf|pptx)$/);
      return m ? new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + kind(name) : name;
    };
    const link = (name, label) => {
      const b = h("button", { class: "btn small", type: "button", title: name }, label);   // a button that looks one
      b.addEventListener("click", async () => {
        try { await api("/api/reports/" + encodeURIComponent(name) + "/open", { body: {} }); toast("Opening " + name + "…"); }
        catch (e) { toast(e.message); }
      });
      return b;
    };
    const rows = [];
    for (const p of t.projects) {
      const update = h("button", { class: "link soft", type: "button", hidden: !p.project }, "Update now");
      update.addEventListener("click", async () => {
        update.disabled = true;
        try { await api("/api/reports/refresh", { body: { project: p.project } }); await loadLibrary(); }
        catch (e) { toast(e.message); } finally { update.disabled = false; }
      });
      const devices = new Map();
      for (const d of p.devices) {
        const n = d.split("/").pop().replace(/\.(pdf|pptx)$/, "");
        if (!devices.has(n)) devices.set(n, []);
        devices.get(n).push(d);
      }
      rows.push(h("div", { class: "lib-project" },
        h("div", { class: "lib-head" }, h("b", {}, p.name), h("div", { class: "spacer" }), update),
        h("div", { class: "lib-row" }, h("span", { class: "lib-label" }, "Master report"),
          ...(p.master.length ? pdfFirst(p.master).map((n) => link(n, kind(n))) : [h("span", { class: "hint" }, "not written yet")])),
        ...[...devices.entries()].map(([n, files]) => h("div", { class: "lib-row" },
          h("span", { class: "lib-label" }, "Device " + n.replace(/_/g, " ")), ...pdfFirst(files).map((f) => link(f, kind(f))))),
        p.history.length ? h("div", { class: "lib-row" }, h("span", { class: "lib-label muted" }, "Earlier versions"),
          ...pdfFirst(p.history.slice(0, 6)).filter((f) => f.endsWith(".pdf")).slice(0, 3).map((f) => link(f, dated(f)))) : null));
    }
    if (t.queues.length) rows.push(h("div", { class: "lib-project" }, h("div", { class: "lib-head" }, h("b", {}, "Queues")),
      ...t.queues.slice(0, 6).map((n) => h("div", { class: "lib-row" }, h("span", { class: "lib-label" }, n.split("/").pop()), link(n, "Open")))));
    if (t.custom.length) rows.push(h("div", { class: "lib-project" }, h("div", { class: "lib-head" }, h("b", {}, "Made on request")),
      ...t.custom.slice(0, 8).map((n) => h("div", { class: "lib-row" }, h("span", { class: "lib-label" }, n.split("/").pop()), link(n, "Open")))));
    box.replaceChildren(...(rows.length ? rows : [h("p", { class: "hint" },
      "No reports yet.")]));
  }
  // ------------------------------------------------------------------ the sample in the setup
  const MOUNT = { choices: [], key: "" };
  async function loadMount() {
    let r;
    try { r = await api("/api/mount"); } catch (e) { return; }
    MOUNT.choices = r.choices;
    const sel = $("mount-select");
    const groups = r.choices.filter((c) => c.devices.some((d) => !d.retired)).map((c) => {
      const g = document.createElement("optgroup");
      g.label = c.name;
      for (const d of c.devices.filter((d) => !d.retired)) g.append(new Option(d.name + (d.material ? " · " + d.material : ""),
        JSON.stringify([c.project, d.id])));
      return g;
    });
    sel.replaceChildren(new Option(groups.length ? "Nothing mounted" : "No devices", ""), ...groups);
    MOUNT.key = null;   // new options: choose the mounted one again
    showMounted(r.mounted);
  }
  function showMounted(m) {
    showProtection(m);
    const key = m ? JSON.stringify([m.project || null, m.device]) : "";
    $("mount-select").title = m ? m.device_name + " · " + m.project_name : "Nothing mounted";   // its project, which the list's group shows
    $("watch-sample-row").hidden = !m;   // a new watch is the sample's (it ends when the sample is taken out) unless unticked
    if (m) $("watch-sample-name").textContent = "Only while " + m.device_name + " is mounted";
    if (key === MOUNT.key) return;
    const sel = $("mount-select");
    if ([...sel.options].some((o) => o.value === key)) sel.value = key;
    else if (m && !MOUNT.loading) {   // a device added elsewhere (a conversation, the phone): load the list once
      MOUNT.loading = true;
      loadMount().finally(() => { MOUNT.loading = false; });
      return;
    } else if (!m) sel.value = "";
    MOUNT.key = key;
  }
  async function mountChosen() {
    const v = $("mount-select").value;
    const [project, device] = v ? JSON.parse(v) : [null, null];
    try {
      const r = await api("/api/mount", { body: device ? { project, device } : {} });
      MOUNT.key = "x"; showMounted(r.mounted);
      if ((r.watches || []).length) toast(r.watches.join(" "));   // the watches made for the sample taken out ended
    } catch (e) { toast(e.message); }
  }
  async function newDevice() {
    const chat = S.chats.find((c) => c.id === S.chatId);
    const pid = (chat && chat.project) || "general";
    const where = pid === "general" ? "work outside projects" : projectName(pid);
    const name = ((await askText("New device (sample) in " + where, { placeholder: "Its name, e.g. D3", ok: "Next" })) || "").trim();
    if (!name) return;
    // the material is optional: Skip (or Esc) adds the device without one
    const material = ((await askText("The material of " + name + "\nOptional.", { placeholder: "e.g. graphene/hBN", ok: "Add",
      cancel: "Skip" })) || "").trim();
    try {
      const r = await api("/api/projects/" + pid + "/devices", { body: { name, material } });
      const d = r.devices.find((x) => x.name.toLowerCase() === name.trim().toLowerCase());
      await api("/api/mount", { body: { project: pid === "general" ? null : pid, device: d.id } });
      await loadMount();
      toast(d.name + " is mounted: scans from now on are filed under it.");
    } catch (e) { toast(e.message); }
  }
  // ------------------------------------------------------------------ protecting the sample
  const PROT = { opts: null, mounted: null, hands: null, busy: null };
  const numText = (v) => (v == null || v === "" || isNaN(+v) ? "" : String(+(+v).toPrecision(6)));
  function showProtection(m) {
    PROT.mounted = m || null;
    const box = $("mount-protection");
    box.hidden = !m;
    if (!m) return;
    const lines = m.protection_lines || [];
    box.classList.toggle("on", lines.length > 0);
    $("mp-state").replaceChildren(icon(lines.length ? "lock" : "warn"),
      lines.length ? "Protected" : "Not protected: only the lab's limits hold");
    $("mount-protect").textContent = lines.length ? "Change…" : "Protect this device…";
    $("mp-lines").replaceChildren(...lines.map((l) => h("li", {}, l)),
      ...(m.protection_missing || []).map((k) => h("li", { class: "bad" }, k + " is not in this lab")));
  }
  function showHands(hands) {
    PROT.hands = hands || null;
    $("hands-banner").hidden = !hands;
    $("hands-locked").hidden = !hands;
    $("hands-on").hidden = !!hands;
    if (!hands) return;
    const at = String(hands.since || "").slice(11, 16);
    const who = String(hands.by || "").replace(/^human \((.*)\)$/, "$1");
    $("hands-text").textContent = "Locked" + (at ? " at " + at : "") + (who && who !== "portal" ? " from " + who : "")
      + ". Drives stay at safe until you unlock.";
  }
  async function handsOn() {   // at once, also while Kelvin works: it stops that work (no dialog on the stop path)
    $("hands-on").disabled = true;
    try {
      const r = await api("/api/hands", { body: { on: true } });
      showHands(r.hands_on);
      const said = r.lines.join("; ");
      toast("Locked. " + said.charAt(0).toUpperCase() + said.slice(1));
    } catch (e) { toast(e.message); }
    finally { $("hands-on").disabled = false; }
    pollState();
  }
  function openUnlock() {
    const sel = $("hands-device");
    sel.replaceChildren(...[...$("mount-select").children].map((n) => n.cloneNode(true)));
    sel.value = $("mount-select").value;
    for (const c of document.querySelectorAll(".hands-check")) c.checked = false;
    $("hands-unlock").disabled = true;
    $("hands-msg").textContent = "";
    $("hands-dialog").showModal();
  }
  async function bringToSafe() {   // a person's press is the approval: every instrument to its declared safe values
    $("lab-safe").disabled = true;   // no dialog: a browser can suppress one, and this only ever moves toward safe
    try { const r = await api("/api/safe", { body: {} }); toast(r.lines.join("; ")); }
    catch (e) { toast(e.message); }
    finally { $("lab-safe").disabled = false; }
    pollState();
  }
  async function unlock() {
    const v = $("hands-device").value, body = { on: false };
    if (v !== $("mount-select").value) {
      const [project, device] = v ? JSON.parse(v) : [null, ""];
      Object.assign(body, { project, device: device || "" });
    }
    $("hands-unlock").disabled = true;
    try {
      const r = await api("/api/hands", { body });
      $("hands-dialog").close();
      showHands(r.hands_on);
      MOUNT.key = "x";
      showMounted(r.mounted);
      toast("Unlocked." + (r.lines.length ? " " + r.lines.join("; ") : ""));
    } catch (e) {
      $("hands-msg").textContent = e.message;
      $("hands-unlock").disabled = false;
    }
    pollState();
  }
  function pf(label, control) {
    const name = h("span", {}, label);
    return h("label", { class: "pf" }, name, control);
  }
  function driveSelect(value, keep) {
    const s = h("select", { "data-k": "drive" });
    for (const d of PROT.opts.drives.filter(keep)) s.append(new Option(d.label + (d.unit ? " (" + d.unit + ")" : ""), d.key));
    if (value && ![...s.options].some((o) => o.value === value)) s.append(new Option(value + " (not in this lab)", value));
    if (value) s.value = value;
    return s;
  }
  const driveInfo = (key) => PROT.opts.drives.find((d) => d.key === key) || {};
  function gateRow(g) {
    const drive = driveSelect(g.drive, (d) => d.gate || d.key === g.drive);
    const diel = h("select", { "data-k": "dielectric" }, ...PROT.opts.dielectrics.map((d) => new Option(d.name, d.name)),
      new Option("Other", "other"));
    diel.value = PROT.opts.dielectrics.some((d) => d.name === g.dielectric) ? g.dielectric : "other";
    const thick = h("input", { type: "number", step: "any", min: "0", "data-k": "thickness_nm", value: numText(g.thickness_nm), placeholder: "nm" });
    const limit = h("input", { type: "number", step: "any", min: "0", "data-k": "limit" });
    const suggested = () => {
      const f = (PROT.opts.dielectrics.find((d) => d.name === diel.value) || {}).field;
      return f && +thick.value > 0 ? +(f * thick.value).toPrecision(6) : null;
    };
    if (g.limit != null && g.limit !== suggested()) limit.value = numText(g.limit);   // the person's own limit
    const leak = h("select", { "data-k": "leak" }, new Option("No leakage reading", ""),
      ...PROT.opts.readings.map((r) => new Option(r.key, r.key)));
    leak.value = g.leak || "";
    const leakMax = h("input", { type: "number", step: "any", min: "0", "data-k": "leak_nA", value: numText((g.leak_max || PROT.opts.leak_max) * 1e9) });
    const rate = h("input", { type: "number", step: "any", min: "0", "data-k": "rate", value: numText(g.rate), placeholder: "the lab's" });
    const note = h("span", { class: "pf-note" });
    const limitLabel = pf("Limit ±", limit), rateLabel = pf("Ramp at most", rate);
    const update = () => {
      const s = suggested(), u = driveInfo(drive.value).unit || "V";
      limit.placeholder = s != null ? String(s) : "needed";
      limitLabel.firstChild.textContent = "Limit ± (" + u + ")";
      rateLabel.firstChild.textContent = "Ramp at most (" + u + "/s)";
      const mine = limit.value.trim();
      note.classList.toggle("bad", s == null && !mine);
      note.textContent = s != null ? "From " + numText(thick.value) + " nm " + diel.value + ": ±" + s + " " + u
        + (mine ? "; yours holds: ±" + mine + " " + u : "")
        : mine ? "" : "Give a limit, or a dielectric and its thickness";
    };
    for (const el of [diel, thick, limit]) el.addEventListener("input", update);
    drive.addEventListener("change", () => { const d = driveInfo(drive.value); if (d.leak && !leak.value) leak.value = d.leak; update(); });
    const row = h("div", { class: "prot-row gate" }, pf("Gate", drive), pf("Dielectric", diel), pf("Thickness (nm)", thick),
      limitLabel, rowButton("x", "Remove this gate", () => row.remove()),
      pf("Leakage reading", leak), pf("Stop above (nA)", leakMax), rateLabel, note);
    update();
    return row;
  }
  function limitRow(key, pair) {
    const drive = driveSelect(key, () => true);
    const lo = h("input", { type: "number", step: "any", "data-k": "lo", value: pair ? numText(pair[0]) : "" });
    const hi = h("input", { type: "number", step: "any", "data-k": "hi", value: pair ? numText(pair[1]) : "" });
    const loLabel = pf("From", lo), hiLabel = pf("To", hi);
    const update = (fill) => {
      const d = driveInfo(drive.value), u = d.unit ? " (" + d.unit + ")" : "";
      loLabel.firstChild.textContent = "From" + u;
      hiLabel.firstChild.textContent = "To" + u;
      if (fill && d.limits) { lo.value = numText(d.limits[0]); hi.value = numText(d.limits[1]); }
    };
    drive.addEventListener("change", () => update(true));
    const row = h("div", { class: "prot-row limit" }, pf("Drive", drive), loLabel, hiLabel,
      rowButton("x", "Remove this limit", () => row.remove()));
    update(!pair);
    return row;
  }
  async function openProtection() {
    const m = PROT.mounted;
    if (!m) return;
    try { PROT.opts = await api("/api/protection"); } catch (e) { toast(e.message); return; }
    const p = m.protection || {};
    $("protect-device").textContent = m.device_name;
    $("protect-gates").replaceChildren(...(p.gates || []).map(gateRow));
    $("protect-limits").replaceChildren(...Object.entries(p.limits || {}).map(([k, v]) => limitRow(k, v)));
    const empty = !(p.gates || []).length && !Object.keys(p.limits || {}).length;
    const first = PROT.opts.drives.find((d) => d.gate);
    if (empty && first) $("protect-gates").append(gateRow({ drive: first.key, dielectric: "hBN", leak: first.leak }));
    $("protect-clear").hidden = empty;
    $("protect-msg").textContent = "";
    $("protect-dialog").showModal();
  }
  function readProtection() {
    const val = (row, k) => row.querySelector('[data-k="' + k + '"]').value.trim();
    const gates = [...$("protect-gates").children].map((row) => {
      const g = { drive: val(row, "drive"), dielectric: val(row, "dielectric") };
      if (val(row, "thickness_nm")) g.thickness_nm = +val(row, "thickness_nm");
      if (val(row, "limit")) g.limit = +val(row, "limit");
      if (val(row, "leak")) g.leak = val(row, "leak");
      if (val(row, "leak_nA")) g.leak_max = +val(row, "leak_nA") * 1e-9;
      if (val(row, "rate")) g.rate = +val(row, "rate");
      return g;
    });
    const limits = {};
    for (const row of $("protect-limits").children) limits[val(row, "drive")] = [val(row, "lo"), val(row, "hi")];
    return { gates, limits };
  }
  async function saveProtection(remove) {
    const m = PROT.mounted;
    if (remove && !(await askYes("Remove " + m.device_name + "'s protection?\nOnly the lab's limits will hold.",
      { ok: "Remove", danger: true }))) return;
    $("protect-save").disabled = true;
    try {
      const r = await api("/api/projects/" + (m.project || "general") + "/devices/" + m.device,
        { method: "PATCH", body: { protection: remove ? {} : readProtection() } });
      $("protect-dialog").close();
      toast(remove ? m.device_name + " is not protected any more."
        : m.device_name + " is protected." + (r.caps.length ? " " + r.caps.join("; ") : ""));
      pollState();
    } catch (e) { $("protect-msg").textContent = e.message; }
    finally { $("protect-save").disabled = false; }
  }
  async function makeReport() {
    const fmt = (document.querySelector('input[name="report-format"]:checked') || {}).value || "pdf";
    const body = { scope: $("report-scope").value, chat_id: S.chatId, run: ANA.run || null, last: 5,
      title: $("report-title").value.trim(), summary: $("report-summary").value.trim(), format: fmt };
    $("make-report").disabled = true;
    $("report-msg").textContent = "Writing…";
    try {
      const r = await api("/api/reports", { body });
      $("report-msg").textContent = r.runs + (r.runs === 1 ? " run" : " runs") + " · done"
        + (r.left ? " · " + r.left + " of unshared work (another project's or chat's) left out" : "");
      const box = $("report-files");
      box.replaceChildren(...reportFiles(r.files.map((f) => f.name)).childNodes,
        h("p", { class: "report-folder" }, "Saved in " + r.folder));
      box.hidden = false;
    } catch (e) {
      $("report-msg").textContent = e.message;
    } finally {
      $("make-report").disabled = false;
    }
  }

  // ------------------------------------------------------------------ tool steps
  function planName(input) {
    const m = String((input && input.plan) || "").match(/plan:\s*([^\n]+)/);
    return m ? m[1].trim() : "";
  }
  // a written pulse sequence's name: from its text (sequence: xy8), or the name it was saved under
  function seqName(text) {
    const s = String(text || "").trim();
    const m = s.match(/sequence:\s*([a-z][a-z0-9_]*)/);
    return m ? m[1] : (/^[a-z][a-z0-9_]*$/.test(s) ? s : "");
  }
  function stepLabel(name, input, done) {
    const a = input || {};
    const code = (s) => "<code>" + esc(s) + "</code>";
    const v = (now, past) => (done ? past : now);
    switch (name) {
      case "lab_status": return v("Checking", "Checked") + " the lab status";
      case "describe_instrument": return v("Reading", "Read") + " the card of " + code(a.instrument);
      case "instrument_info": return v("Reading", "Read") + " the folder of " + code(a.instrument);
      case "lab_config": return v("Reading", "Read") + " the lab config" + (a.instrument ? " for " + code(a.instrument) : "");
      case "search_manual": return v("Searching", "Searched") + " the " + esc(a.instrument || "") + " manual for " + code(a.query || "");
      case "show_sequence": return v("Drawing", "Drew") + " the " + code(a.protocol || seqName(a.sequence) || "written") + " pulse sequence";
      case "save_sequence": return v("Saving", "Saved") + " the pulse sequence " + code(seqName(a.text) || "");
      case "read_examples": return v("Reading", "Read") + " the maker's examples in " + code(a.path || "");
      case "try_card": return v("Trying", "Tried") + " the card on " + code(a.instrument || "");
      case "experiment_guide": return a.experiment ? v("Reading", "Read") + " the experiment " + code(a.experiment)
        : v("Looking for", "Looked for") + " experiments" + (a.query ? " on " + code(a.query) : "") + " in the instrument collection";
      case "get_value": return v("Reading", "Read") + " " + code(a.instrument + "." + a.parameter);
      case "set_value": return v("Setting", "Set") + " " + code(a.instrument + "." + a.parameter) + " → " + esc(String(a.value));
      case "send_raw": return v("Sending", "Sent") + " " + code(a.command) + " to " + esc(a.instrument);
      case "undo_last": return v("Undoing", "Undid") + " the last change";
      case "safe_state": return v("Sending", "Sent") + " every drive to safe";  // older chats
      case "health_check": return v("Running", "Ran") + " health checks";
      case "validate_plan": return v("Checking", "Checked") + " plan " + code(planName(a));
      case "simulate_plan": return v("Dry-running", "Dry-ran") + " plan " + code(planName(a));
      case "run_plan": return v("Running", "Ran") + " plan " + code(planName(a));
      case "list_runs": return v("Listing", "Listed") + " recent runs";
      case "get_run_data": return v("Reading", "Read") + " the data of run " + code(a.run_id);
      case "find_runs": return v("Searching", "Searched") + " the runs" + (a.text ? " for " + code(a.text) : "");
      case "query_runs": return v("Querying", "Queried") + " the catalog of runs";
      case "describe_run": return v("Reading", "Read") + " the card of run " + code(a.run_id);
      case "compute": return v("Computing", "Computed") + " " + code(a.expression || "") + " from run " + code(a.run_id);
      case "get_trace": return v("Reading", "Read") + " a trace from run " + code(a.run_id);
      case "recent_events": return v("Reading", "Read") + " the event log";
      case "wait": return v("Waiting", "Waited") + " " + esc(String(a.seconds)) + " s";
      case "save_note": return v("Saving", "Saved") + " a note to the project";  // older chats
      case "search_papers": return v("Searching", "Searched") + " the literature for " + code(a.query || "");
      case "read_paper": return v("Reading", "Read") + " " + code(a.paper || "") + (a.start ? " from character " + esc(String(a.start)) : "");
      case "save_paper": return v("Adding", "Added") + " " + code(a.paper || "") + " to the reading list";
      case "web_search": return v("Searching", "Searched") + " the web for " + code(a.query || "");
      case "read_web_page": return v("Reading", "Read") + " " + code(a.url || "") + (a.start ? " from character " + esc(String(a.start)) : "");
      case "list_folder": return v("Looking", "Looked") + " in " + (a.path ? code(a.path) : "the chat's folder");
      case "write_file": return v("Saving", "Saved") + " " + code(a.path || "a file") + " in the chat's folder";
      case "read_file": return v("Reading", "Read") + " " + code(a.path || "") + (a.pages ? ", pages " + esc(String(a.pages)) : "") +
        (a.start ? " from character " + esc(String(a.start)) : "") + (a.figures ? " (the pages as drawn)" : "");
      case "import_data": return v("Importing", "Imported") + " " + code(String(a.paths || "").split(";").map((p) => p.trim().split(/[\\/]/).pop()).join(", ")) +
        (a.name ? " as " + code(a.name) : "");
      case "save_manual": return v("Keeping", "Kept") + " the manual " + code(a.source || "") +
        (a.model ? " for the " + esc(String((a.maker ? a.maker + " " : "") + a.model)) : "");
      case "make_report": return v("Writing", "Wrote") + " a report" + (a.title ? ": " + esc(String(a.title)) : "");
      case "operate_computer": return v("Operating", "Operated") + " the computer: " + esc(String(a.task || ""));
      case "remember": return v("Remembering", "Remembered") + (a.scope === "global" ? " for every chat"
        : a.scope === "lab" ? " for the lab (everyone here)" : " for this project") + (a.text ? ": " + esc(String(a.text)) : "");
      case "shared_memory": return v("Reading", "Read") + " the lab's memory" + (a.instrument ? " on " + code(a.instrument)
        : a.query ? " for " + code(a.query) : "");
      default: return code(name);
    }
  }
  function summaryOf(content) {   // the marks around text from outside the lab are for the model
    const lines = String(content || "").split("\n").filter((l) => l.trim() && !/^<\/?outside_text\b/.test(l));
    return lines.find((l) => !/^dry run:/.test(l)) || lines[0] || "";
  }
  function outcome(content, isError) {
    if (isError) return "err";
    const head = String(content || "").split("\n").slice(0, 2).join(" ").toLowerCase();
    if (/^(refused|declined|invalid plan|not sent|nothing to undo|not run)/.test(head) ||
        /\b(refused|declined|tripped|aborted|timeout|failed|error)\b/.test(head)) return "warn";
    return "ok";
  }
  // a part shown or hidden in place, in the chat (a tool step's input and result, a decided request's details): its
  // toggle says aria-expanded; the part (.k-fold, a grid row going from 0fr to 1fr) opens by height, at once from the
  // keyboard (an event's detail 0: the step or request .still, so neither the part nor the chevron moves) or with less
  // motion, and is inert as soon as it shuts. A second press closes it
  const FOLDS = { n: 0 };
  function foldOpen(el, toggle, fold, open, e) {
    el.classList.toggle("still", !!e && !e.detail);
    el.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    fold.inert = !open;
  }
  function makeStep(name, input) {
    const state = h("span", { class: "state" }, h("span", { class: "spinner" }));
    const summary = h("span", { class: "step-summary" });
    const chev = icon("chev"); chev.classList.add("chev");
    const label = h("span", { class: "step-label", html: stepLabel(name, input, false) });
    // its head a disclosure: aria-expanded, its label kept, its chevron turning; what it holds opens by height
    const id = "step-" + ++FOLDS.n;
    const head = h("button", { class: "step-head", type: "button", "aria-expanded": "false", "aria-controls": id }, state, label, summary, chev);
    const inputText = name === "run_plan" || name === "simulate_plan" || name === "validate_plan"
      ? String((input && input.plan) || "") : JSON.stringify(input || {}, null, 1);
    const result = h("pre", {}, "…");
    const body = h("div", { class: "step-body" }, h("div", { class: "k" }, "Input"), h("pre", {}, inputText), h("div", { class: "k" }, "Result"), result);
    const fold = h("div", { class: "k-fold", id, inert: true }, h("div", { class: "k-fold-in" }, body));
    const el = h("div", { class: "step" }, head, fold);
    head.addEventListener("click", (e) => foldOpen(el, head, fold, !el.classList.contains("open"), e));
    return {
      el,
      finish(content, isError) {
        const o = outcome(content, isError);
        state.className = "state " + o;
        state.replaceChildren(icon(o === "ok" ? "check" : o === "warn" ? "warn" : "x"));
        label.innerHTML = stepLabel(name, input, true);
        summary.textContent = summaryOf(content);
        result.textContent = content || "(empty)";
      },
      unknown() {
        state.className = "state warn";
        state.replaceChildren(icon("warn"));
        summary.textContent = "no result recorded";
      },
    };
  }

  // ------------------------------------------------------------------ approvals
  // a request's points; one of several lines (code to run or save) as it is written, scrolling when long
  const approvalItem = (i) => h("li", {}, String(i).includes("\n") ? h("pre", { class: "approval-code" }, i) : i);
  // a request's pictures (a pulse generator's timing): an SVG shown as an image, which can't run anything; its lines
  // above say the same numbers
  const approvalFigures = (figs) => (figs || []).map((f) => h("figure", { class: "approval-figure" },
    h("img", { src: "data:image/svg+xml;charset=utf-8," + encodeURIComponent(String(f.svg || "")), alt: f.caption || "" }),
    f.caption ? h("figcaption", { class: "muted small" }, f.caption) : null));
  // auto mode approved this without asking: show what ran and why, like an answered request
  function autoApproval(ev) {
    const text = h("span", {}, ev.title), list = h("ul", {}, ...ev.items.map(approvalItem));
    const title = h("div", { class: "approval-title" }, icon("check"), text);
    const el = h("div", { class: "approval done auto", role: "group", "aria-label": ev.title }, title, list,
      ...approvalFigures(ev.figures),
      h("div", { class: "approval-actions" }, h("span", { class: "verdict auto" }, cardSay("Auto-approved")),
        h("span", { class: "muted small" }, (ev.reasons || []).join("; "))));
    foldable(el, title);
    localizeCard(ev, { title: text, list, where: null });
    return el;
  }
  // a card in the language the person reads cards in (Settings → Display; cardlang.py), translated as it is shown:
  // the card itself, what is approved and the record stay as Kelvin wrote them, and English is one click away
  const CARD_UI = { "zh-Hans": { "Approve": "批准", "Decline": "拒绝", "Approved": "已批准", "Declined": "已拒绝",
    "Already closed": "已关闭", "Closed": "已关闭", "Auto-approved": "自动批准", english: "English", shown: "中文",
    declines: (n) => (n < 90 ? n + " 秒" : n < 5400 ? Math.round(n / 60) + " 分钟" : (n / 3600).toFixed(1) + " 小时") + "后自动拒绝" } };
  function cardWords() { return CARD_UI[(S.settings && S.settings.card_language) || ""] || null; }
  function cardSay(text) { const w = cardWords(); return (w && typeof w[text] === "string") ? w[text] : text; }
  async function localizeCard(ev, parts) {
    const lang = S.settings && S.settings.card_language;
    if (!lang) return;
    let shown = ev.shown;
    if (!shown) {
      try { shown = await api("/api/cardtext", { body: { title: ev.title, items: ev.items, where: ev.where, language: lang } }); }
      catch (e) { return; }   // it stays as Kelvin wrote it
    }
    const english = { title: ev.title, items: ev.items, where: ev.where };
    const put = (c) => {
      parts.title.textContent = c.title;
      parts.list.replaceChildren(...c.items.map(approvalItem));
      if (parts.where && c.where) parts.where.textContent = c.where;
    };
    const words = cardWords() || {};
    // which language it shows: a choice of two (the kit's .seg), its words kept, the chosen one aria-checked; beside
    // the title, never in it once the title is a decided card's disclosure (foldable moves it out then)
    const pick = h("div", { class: "seg card-lang", role: "radiogroup", "aria-label": "Card language" });
    const choose = (v) => {
      put(v === "en" ? english : shown);
      for (const b of pick.querySelectorAll(":scope > button")) {
        const on = b.dataset.v === v;
        b.classList.toggle("on", on);
        b.setAttribute("aria-checked", String(on));
        b.tabIndex = on ? 0 : -1;
      }
    };
    for (const [v, label, tip] of [["shown", words.shown || lang, null], ["en", words.english || "English", "The card as Kelvin wrote it"]]) {
      const b = h("button", { type: "button", role: "radio", title: tip }, label);
      b.dataset.v = v;
      b.addEventListener("click", () => choose(v));
      pick.append(b);
    }
    segKeys(pick, choose);
    choose("shown");
    const head = parts.title.closest(".approval-title");
    (head && head.getAttribute("role") === "button" ? head : parts.title).after(pick);
    thumb(pick);
  }
  // a decided request shows one line; its title is a disclosure (aria-expanded, its words kept, a chevron that turns)
  // for the details under it (what it asked, its pictures, the lab's lessons), which open by height (foldOpen)
  function foldable(el, title) {
    const id = "appr-" + ++FOLDS.n, chev = icon("chev");
    chev.classList.add("chev");
    const fold = h("div", { class: "k-fold", id, inert: true }, h("div", { class: "k-fold-in" },
      ...el.querySelectorAll(":scope > ul, :scope > .approval-figure, :scope > .approval-lessons")));
    title.after(fold);
    const pick = title.querySelector(".card-lang");   // the card's language: a control of its own, not in the button
    if (pick) title.after(pick);
    title.append(chev);
    title.setAttribute("role", "button");
    title.setAttribute("tabindex", "0");
    title.setAttribute("aria-expanded", "false");
    title.setAttribute("aria-controls", id);
    title.title = "Details";
    const flip = (e) => foldOpen(el, title, fold, !el.classList.contains("open"), e);
    title.addEventListener("click", flip);
    title.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(e); } });
  }

  // where a request acts (runtime.where): a request on the simulator once looked just like one on the instruments
  const whereTag = (where) => where ? h("span", { class: "where " + (where === "SIMULATOR" ? "sim" : "real"),
    title: where === "SIMULATOR" ? "Only simulated instruments: nothing real moves" : "Real instruments" }, where) : null;
  // the lab's lessons on the instruments a request names (labmemory.py): what the lab learned, where it is decided
  const lessonsOf = (ev) => (ev.lessons || []).length ? h("div", { class: "approval-lessons" }, icon("memory"),
    h("div", {}, h("b", {}, "From the lab's memory"), ...ev.lessons.map((l) => h("span", {}, l)))) : null;
  function makeApproval(ev) {
    const verdict = h("span", { class: "verdict" });
    const count = h("span", { class: "muted small" });
    const yes = h("button", { class: "btn primary", type: "button" }, cardSay("Approve"));
    const no = h("button", { class: "btn", type: "button" }, cardSay("Decline"));
    const actions = h("div", { class: "approval-actions" }, yes, no, count);
    const text = h("span", {}, ev.title), where = whereTag(ev.where), list = h("ul", {}, ...ev.items.map(approvalItem));
    const title = h("div", { class: "approval-title" }, icon("hand"), text, where);
    const el = h("div", { class: "approval", role: "group", "aria-label": ev.title }, title, list,
      ...approvalFigures(ev.figures), lessonsOf(ev), actions);
    localizeCard(ev, { title: text, list, where });
    let left = ev.seconds_left;
    const words = cardWords();
    // its time left in seconds under 90, in whole minutes under 90 min, else in hours (inTime), in either language: a
    // request escalated to a phone may wait up to 12 hours
    const tick = () => {
      const t = left > 0 ? (words ? words.declines(left) : "Declines itself in " + inTime(left)) : "";
      if (count.textContent !== t) count.textContent = t;
      left -= 1;
    };
    tick();
    const timer = setInterval(tick, 1000);
    const card = {
      el, decided: false,
      close(text, good) {
        clearInterval(timer);
        if (!card.decided) { verdict.textContent = cardSay(text); verdict.className = "verdict " + (good ? "yes" : "no"); }
        actions.replaceChildren(verdict);
        if (!el.classList.contains("done")) foldable(el, title);
        el.classList.add("done");
        card.decided = true;
        S.approvals.delete(ev.id);
        refreshTitle();
      },
    };
    async function answer(approve) {
      card.choice = approve;  // the server's "closed" notice can arrive before this request returns
      yes.disabled = no.disabled = true;
      try {
        await api("/api/approvals/" + encodeURIComponent(ev.id), { body: { approve } });
        card.close(approve ? "Approved" : "Declined", approve);
      } catch (e) {
        if (e.status === 409) { card.close("Already closed", false); return; }   // answered or closed meanwhile
        // the request is still open (not confirmed by an operator, locked while Claude operates, no connection):
        // the card stays, saying why, to answer again
        yes.disabled = no.disabled = false;
        card.choice = undefined;
        toast(e.status ? e.message : "Your answer didn't reach Kelvin (" + e.message + "). Try again.");
      }
    }
    yes.addEventListener("click", () => answer(true));
    no.addEventListener("click", () => answer(false));
    S.approvals.set(ev.id, card);
    refreshTitle();
    return card;
  }
  function refreshTitle() {
    const n = S.approvals.size;
    document.title = (n ? "(" + n + ") Approval needed · " : "") + "Kelvin";
  }

  // ------------------------------------------------------------------ one assistant reply
  class Reply {
    constructor(modelKey) {
      this.el = h("div", { class: "msg assistant" });
      this.who = h("span", {}, "Kelvin");   // Kelvin answers, whichever model it uses (Compare's lanes say theirs)
      this.el.append(h("div", { class: "who" }, mark(), this.who));
      this.text = null; this.buf = ""; this.raf = 0; this.thinking = null; this.steps = new Map();
      this.setModel(modelKey);
    }
    setModel(key) { if (key) this.model = key; }   // kept, never shown: the header says only Kelvin
    addText(delta) {
      if (!this.text) { this.endThinking(); this.text = h("div", { class: "md cursor" }); this.buf = ""; this.el.append(this.text); }
      this.buf += delta;
      if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; if (this.text) this.text.innerHTML = md(this.buf); });
    }
    resetText() { if (this.text) { this.text.remove(); this.text = null; this.buf = ""; } }
    endText() {
      if (this.text) {
        cancelAnimationFrame(this.raf); this.raf = 0; this.text.innerHTML = md(this.buf); this.text.classList.remove("cursor");
        this.text.dataset.src = this.buf;   // what Copy copies: the answer as written (Markdown)
        this.last = { el: this.text, buf: this.buf };
      }
      this.text = null;
    }
    shorten(last, short, full, fullWords) {   // an answer condensed by an earlier version: the full one a click away
      const shortHtml = md(short), fullHtml = md(full);
      let open = false;
      const label = () => (open ? "Show the short answer" : "Full answer (" + fullWords + " words)");
      const toggle = h("button", { class: "link soft", type: "button" }, label());
      toggle.addEventListener("click", () => {
        open = !open; last.el.innerHTML = open ? fullHtml : shortHtml; last.el.dataset.src = open ? full : short; toggle.textContent = label();
      });
      last.el.innerHTML = shortHtml;
      last.el.dataset.src = short;
      last.el.after(h("div", { class: "condense-note" }, h("span", {}, "Condensed by Kelvin"), toggle));
    }
    keptWhole(words, why) {
      this.el.append(h("div", { class: "condense-note" },
        "A long answer (" + (words ? words + " words" : "kept whole") + ")" + (why ? ": " + why : "") + "."));
    }
    addThinking(delta) {
      if (!this.thinking) {
        this.endText();
        const body = h("div", { class: "body" });
        const summary = h("summary", {}, "Thinking…");
        this.thinking = { el: h("details", { class: "thinking" }, summary, body), body, summary };
        this.el.append(this.thinking.el);
      }
      this.thinking.body.textContent += delta;
    }
    endThinking() { if (this.thinking) { this.thinking.summary.textContent = "Thought process"; this.thinking = null; } }
    toolStart(id, name, input) {
      this.endText(); this.endThinking();
      const step = makeStep(name, input);
      this.steps.set(id, step);
      this.el.append(step.el);
    }
    toolEnd(id, content, isError, runId, images, live) {   // live: as it happens, not a chat read back
      const step = this.steps.get(id);
      if (!step) return;
      step.finish(content, isError);
      step.done = true;
      if (images && images.length && !step.pics) {   // what look_at showed the model
        step.pics = h("div", { class: "tool-pics" }, ...images.map((f) => mediaImg(f, "What Kelvin looked at")));
        (step.last || step.el).after(step.pics);
        step.last = step.pics;
      }
      const files = reportNames(content);
      if (files.length && !step.files) {
        step.files = reportFiles(files);
        (step.last || step.el).after(step.files);
        step.last = step.files;
      }
      runId = runId || (step.card && step.card.runId);   // one its answer doesn't name: the run its card followed
      if (!runId) return;
      if (!step.card) { step.card = runCard(); (step.last || step.el).after(step.card.el); }
      step.card.finish(runId);
      const fresh = live && !RUNS.tabs.has(runId) && !RUNS.dismissed.has(runId);   // over before the Runs pane saw it
      const tab = fresh ? runTab(runId, step.card.plan || runId.replace(/^\d{8}-\d{6}-/, "")) : RUNS.tabs.get(runId);
      if (tab && !tab.final) { setTabStatus(tab, "finishing"); loadFinal(tab); }   // the Analysis pane hears of it
      if (fresh) openRun(runId, tab.plan);
    }
    runProgress(ev) {  // the data go to the runs pane; the conversation keeps a compact card
      const step = this.steps.get(ev.call_id);
      if (!step) return;
      if (!step.card) { step.card = runCard(ev.plan); (step.last || step.el).after(step.card.el); }
      step.card.progress(ev);
      if (ev.status === "running") noticeRun(ev.run_id, ev.plan);
    }
    computerStep(ev) {   // what Claude did on the screen, listed under its operate_computer step
      const step = this.steps.get(ev.call_id);
      if (!step) return;
      if (!step.log) { step.log = h("ol", { class: "cu-log" }); (step.last || step.el).after(step.log); step.last = step.log; }
      step.log.append(h("li", {}, ev.text));
    }
    approval(ev) {
      const el = ev.auto ? autoApproval(ev) : makeApproval(ev).el;
      const step = this.steps.get(ev.call_id);
      if (step) { (step.last || step.el).after(el); step.last = el; } else this.el.append(el);
    }
    note(text) { this.endText(); this.el.append(h("div", { class: "note" }, text)); }
    memory(notes) {  // the saved notes recalled for this message: what Kelvin knew from earlier work
      if (!notes || !notes.length) return;
      this.endText();
      this.el.append(recallBox(notes));
    }
    place(callId, el) {
      const step = this.steps.get(callId);
      if (step) { (step.last || step.el).after(el); step.last = el; } else this.el.append(el);
    }
    reviewStart(ev) {
      this.pendingReview = h("div", { class: "review pending" }, h("span", { class: "spinner" }),
        h("span", {}, "Asking " + (S.labels[ev.model] || ev.model) + " for a second opinion on this plan…"));
      this.place(ev.call_id, this.pendingReview);
    }
    review(ev) {  // a second opinion on a plan, under its run_plan step
      const card = reviewCard(ev);
      const pending = this.pendingReview;
      this.pendingReview = null;
      if (pending && pending.isConnected) {
        pending.replaceWith(card);
        const step = this.steps.get(ev.call_id);
        if (step && step.last === pending) step.last = card;
      } else this.place(ev.call_id, card);
    }
    answerReview(r) {
      this.endText();
      this.reviewEl = reviewCard(r);
      this.reviewData = r;
      this.el.append(this.reviewEl);
    }
    error(text) { this.endText(); this.el.append(h("div", { class: "callout bad" }, text)); }
    finish() {
      this.endText(); this.endThinking();
      for (const step of this.steps.values()) if (!step.done && step.unknown) { step.unknown(); step.done = true; }
      if (!this.tools && this.el.querySelector(":scope > .md")) {   // Copy, its icon alone: the answer's text, as shown
        const btn = h("button", { class: "link soft copy-btn", type: "button", title: "Copy", "aria-label": "Copy this answer" }, icon("copy"));
        btn.addEventListener("click", () => copyText([...this.el.querySelectorAll(":scope > .md")]
          .map((e) => e.dataset.src || e.innerText).join("\n\n"), btn));
        this.tools = h("div", { class: "msg-tools" }, btn);
        this.el.append(this.tools);
      }
    }
  }

  // ------------------------------------------------------------------ thread
  const thread = $("thread");
  const scroller = $("scroller");
  const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
  const toBottom = () => { scroller.scrollTop = scroller.scrollHeight; };

  function userBubble(text, attachments) {
    const files = attachments && attachments.length ? h("div", { class: "user-files" }, ...attachments.map((a) =>
      String(a.mime).startsWith("image/")
        ? mediaImg(a.file, a.name)
        : h("span", { class: "file-chip", title: a.run ? a.name + ": run " + a.run : a.name }, svgIcon("M7 3h7l5 5v13H7zM14 3v5h5"),
            a.name + attachedSize(a)))) : null;
    const copy = h("button", { class: "link soft copy-btn", type: "button", title: "Copy", "aria-label": "Copy your message" }, icon("copy"));
    copy.addEventListener("click", () => copyText(text, copy));
    return h("div", { class: "msg user" }, files, h("div", { class: "bubble" }, text), h("div", { class: "msg-tools user-tools" }, copy));
  }
  function attachedSize(a) {   // a PDF's pages; a data file's points or frames (it became a run as it was attached)
    if (a.pages) return " · " + a.pages + " pages";
    if (!a.run) return "";
    const n = a.points || 0;
    return " · " + n + (a.frames ? (n === 1 ? " frame" : " frames") : (n === 1 ? " point" : " points"));
  }
  function svgIcon(d) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg"), path = document.createElementNS(ns, "path");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
    path.setAttribute("d", d); path.setAttribute("fill", "none"); path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.8"); path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
    return svg;
  }
  // pictures kept with a chat (a plot look_at drew, a photo the person attached), fetched once with the token
  const MEDIA = new Map();
  function mediaImg(file, alt) {
    const img = h("img", { class: "media-img", alt: alt || "A picture", title: "Click to enlarge" });
    const key = S.chatId + "/" + file;
    if (!MEDIA.has(key)) {
      MEDIA.set(key, fetch("/api/chats/" + S.chatId + "/media/" + encodeURIComponent(file), { headers: { "X-Kelvin-Token": token } })
        .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.blob(); })
        .then((b) => URL.createObjectURL(b)));
    }
    MEDIA.get(key).then((url) => { img.src = url; }).catch(() => { MEDIA.delete(key); img.alt = "(the picture is no longer there)"; });
    img.addEventListener("click", () => img.classList.toggle("big"));
    return img;
  }
  function wakeLine(m) {   // Kelvin continuing the chat itself (wake_me), not a message from the person
    const why = typeof m.wake === "string" ? m.wake : String(m.text || "").replace(/^<wake>|<\/wake>$/g, "");
    return h("div", { class: "msg wake note" }, "Kelvin continued this chat: " + why);
  }

  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {   // no clipboard access here: copy through a hidden text box
      const box = h("textarea", { class: "copy-box" });
      box.value = text;
      document.body.append(box);
      box.select();
      try { document.execCommand("copy"); } finally { box.remove(); }
    }
    if (btn.classList.contains("copy-btn")) {   // its icon alone (a message's Copy): a tick for a moment, named Copied
      clearTimeout(btn.copied);
      btn.replaceChildren(icon("check"));
      btn.title = "Copied";
      btn.classList.add("copied");
      btn.copied = setTimeout(() => { btn.replaceChildren(icon("copy")); btn.title = "Copy"; btn.classList.remove("copied"); }, 1500);
      return;
    }
    const said = btn.lastChild;
    said.textContent = "Copied";
    setTimeout(() => { said.textContent = "Copy"; }, 1500);
  }
  function showEmpty(show) { $("empty").hidden = !show; thread.hidden = show; }

  // the last run a run tool's answer names (a recipe's last stage), read as the server does (web/agent.py runs_named)
  const RUN_TOOLS = ["run_plan", "run_script", "run_recipe"];
  function runNamed(tool, content) {
    if (!RUN_TOOLS.includes(tool)) return null;
    const ids = [...String(content).matchAll(/run_id='([^']+)'|\brun (\d{8}-\d{6}-[\w.-]*\w)/g)].map((m) => m[1] || m[2]);
    return ids.length ? ids[ids.length - 1] : null;
  }
  function replayMessage(reply, m) {
    if (m.role === "assistant") {
      for (const b of m.blocks) {
        if (b.type === "text") { reply.addText(b.text); reply.endText(); }
        else if (b.type === "thinking") { reply.addThinking(b.text); reply.endThinking(); }
        else if (b.type === "tool_call") reply.toolStart(b.id, b.name, b.input);
      }
      if (m.full && reply.last) reply.shorten(reply.last, reply.last.buf, m.full, m.full_words);
      else if (m.kept) reply.keptWhole(0, m.kept);
      if (m.review) reply.answerReview(m.review);
    } else if (m.role === "tool") {
      for (const r of m.results) {
        if (r.review) reply.review(Object.assign({ call_id: r.id }, r.review));
        reply.toolEnd(r.id, r.content, r.is_error, runNamed(r.name, r.content), (r.images || []).map((i) => i.file));
      }
    }
  }
  // a person's change to the sequence or Home, told to this chat (notices.py): a quiet line in the thread
  function eventLine(m) {
    const when = m.at ? new Date(m.at * 1000) : null;
    return h("div", { class: "event-line", title: when ? when.toLocaleString() : "" },
      when ? h("time", {}, when.toTimeString().slice(0, 5)) : null, h("span", {}, m.text));
  }
  function renderChat(chat) {
    thread.replaceChildren();
    let reply = null, recalled = null, skip = 0;
    const lastUser = chat.messages.map((m) => m.role).lastIndexOf("user");
    S.lastReply = null;
    S.chatUpdated = chat.updated || 0;
    chat.messages.forEach((m, at) => {
      if (skip > 0) { skip--; return; }
      if (m.role === "event") {
        if (reply) reply.finish();
        reply = null;
        thread.append(eventLine(m));
        return;
      }
      if (m.role === "user" && m.notice) return;   // the same changes, as the model read them while it answered
      if (m.role === "user") {
        if (reply) reply.finish();
        reply = null;
        recalled = m.memory || null;
        thread.append(m.wake ? wakeLine(m) : userBubble(m.text, m.attachments));
        const comp = m.compare;
        if (comp && comp.lanes && comp.lanes.length) {   // several models answered this message side by side
          if (recalled) { thread.append(recallBox(recalled)); recalled = null; }
          const view = new CompareView(comp.models);
          thread.append(view.el);
          const n = comp.lanes[comp.chosen].messages.length;
          comp.lanes.forEach((lane, i) => {   // the chosen answer from the chat itself: it carries any second opinion
            const msgs = i === comp.chosen ? chat.messages.slice(at + 1, at + 1 + n) : lane.messages;
            for (const lm of msgs) replayMessage(view.lanes[i].reply, lm);
            view.lanes[i].reply.finish();
          });
          view.setChosen(comp.chosen, at === lastUser);
          skip = comp.lanes[comp.chosen].messages.length;
          S.lastReply = view.lanes[comp.chosen].reply;
        }
        return;
      }
      if (!reply && m.role === "assistant") {
        reply = new Reply(m.provider + ":" + m.model);
        thread.append(reply.el);
        reply.memory(recalled);
        recalled = null;
      }
      if (reply) { replayMessage(reply, m); S.lastReply = reply; }
    });
    if (reply) reply.finish();
    offerRecheck(S.lastReply);   // an earlier second opinion on the latest answer: another model can re-check it
    showEmpty(!chat.messages.length);
    requestAnimationFrame(toBottom);
  }

  // ------------------------------------------------------------------ chats sidebar: projects, then unfiled chats
  // hidden: its button hid it where it stands beside the page (kept for next time); slide: the timer its slide ends on
  const SIDE = { collapsed: new Set(), creating: false, moveAfterCreate: null, renaming: null, newProject: null, editor: null,
                 hidden: store("lh.sideHidden") === "1", slide: 0 };
  try { for (const id of JSON.parse(store("lh.collapsed") || "[]")) SIDE.collapsed.add(id); } catch (e) { /* fresh start */ }
  const projectName = (id) => (S.projects.find((p) => p.id === id) || {}).name;

  function setTitle(title, projectId) {
    if (S.incognito) {
      $("chat-title").replaceChildren(h("span", { class: "crumb incog" }, icon("incognito"), h("span", {}, "Incognito chat")));
      return;
    }
    const name = projectName(projectId);
    const crumb = name ? [h("span", { class: "crumb" }, icon("folder"), h("span", {}, name)), title ? h("span", { class: "crumb-sep" }, "/") : null] : [];
    $("chat-title").replaceChildren(...crumb.filter(Boolean), title || "");
  }
  function currentProject() {
    const c = S.chats.find((x) => x.id === S.chatId);
    return c ? c.project : SIDE.newProject;
  }

  function rowButton(name, label, onClick) {   // ⋯ opens a menu, and says so (aria-haspopup; popMenu marks it open)
    const b = h("button", { class: "row-btn", type: "button", "aria-label": label, title: label,
      "aria-haspopup": name === "dots" ? "menu" : null }, icon(name));
    b.addEventListener("click", (e) => { e.stopPropagation(); onClick(e); });
    return b;
  }
  // a row's name tip: its whole name, only while the row cuts it short (an ellipsis), read as the pointer comes
  function nameTip(row, name) {
    row.addEventListener("pointerenter", () => {
      const t = row.querySelector(".t");
      if (t && t.scrollWidth > t.clientWidth) row.title = name; else row.removeAttribute("title");
    });
  }
  // an inline name field; the same element survives re-renders of the list, so typing is never lost
  function nameInput(key, value, placeholder, onSubmit, onCancel) {
    if (SIDE.editor && SIDE.editor.key === key) return SIDE.editor.el;
    const input = h("input", { class: "side-input", value, placeholder, maxlength: "60", spellcheck: "false", "aria-label": placeholder });
    const ed = { key, input, el: h("div", { class: "side-edit" }, input), fresh: true };
    SIDE.editor = ed;
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      if (SIDE.editor === ed) SIDE.editor = null;
      const v = input.value.trim();
      if (save && v && v !== value) onSubmit(v); else onCancel();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => setTimeout(() => {  // a re-render moves the field; only a real leave counts
      if (input.isConnected && document.activeElement === input) return;
      finish(true);
    }, 0));
    input.addEventListener("click", (e) => e.stopPropagation());
    return ed.el;
  }
  function dropTarget(el, projectId) {
    el.addEventListener("dragover", (e) => {
      if (![...e.dataTransfer.types].includes("text/x-kelvin-chat")) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      el.classList.add("drop-on");
    });
    el.addEventListener("dragleave", () => el.classList.remove("drop-on"));
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      el.classList.remove("drop-on");
      const id = e.dataTransfer.getData("text/x-kelvin-chat");
      if (id) moveChat(id, projectId);
    });
  }

  function chatItem(c) {
    if (SIDE.renaming === c.id) {
      return nameInput("chat:" + c.id, c.title || "New chat", "Conversation name", (t) => renameChat(c, t), () => { SIDE.renaming = null; renderChats(); });
    }
    const more = rowButton("dots", "Options for this conversation", (e) => chatMenu(e.currentTarget, c));
    const answering = S.live.has(c.id) || S.answering.has(c.id);
    const mark = S.asking.has(c.id) ? ["asking", "Kelvin asks for your approval here"]
      : answering ? ["answering", "Kelvin is answering here"] : S.unread.has(c.id) ? ["unread", "A new answer"] : null;
    const item = h("div", { class: "chat-item" + (c.id === S.chatId ? " active" : "") + (mark ? " " + mark[0] : ""),
      role: "button", tabindex: "0", draggable: "true", "data-key": "chat:" + c.id },
      mark ? h("span", { class: "chat-dot", title: mark[1], "aria-label": mark[1] }) : null,
      h("span", { class: "t" }, c.title || "New chat"), lockMark(chatPrivacy(c)), more);
    nameTip(item, c.title || "New chat");
    item.addEventListener("click", () => openChat(c.id));
    // its own Enter or Space (not its ⋯'s), used up here: the key goes on to nothing, not even the question it may ask
    item.addEventListener("keydown", (e) => {
      if (e.target !== item || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault();
      openChat(c.id);
    });
    item.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/x-kelvin-chat", c.id);
      e.dataTransfer.effectAllowed = "move";
      item.classList.add("dragging");
      $("chat-list").classList.add("dragging-chat");
    });
    item.addEventListener("dragend", () => { item.classList.remove("dragging"); $("chat-list").classList.remove("dragging-chat"); });
    return item;
  }
  // a project folds under its head, the one holding the open chat too (its head then looks chosen, as the chat's row
  // would); kept per browser (lh.collapsed). A disclosure: aria-expanded and aria-controls, its chevron turning; what
  // it opens comes in by height (.k-open, once; at once from the keyboard)
  function unfoldProject(id) {   // a chat of a folded project opened (a toast's Open): its project shows it
    if (!id || !SIDE.collapsed.delete(id)) return;
    store("lh.collapsed", JSON.stringify([...SIDE.collapsed]));
  }
  function projectGroup(p, chats) {
    const holdsActive = currentProject() === p.id;
    const open = !SIDE.collapsed.has(p.id), listId = "proj-chats-" + p.id;
    let head;
    if (SIDE.renaming === p.id) {
      head = nameInput("project:" + p.id, p.name, "Project name", (name) => renameProject(p, name), () => { SIDE.renaming = null; renderChats(); });
    } else {
      head = h("div", { class: "proj-row" + (holdsActive && (!S.chatId || !open) ? " active" : ""), role: "button", tabindex: "0",
        "aria-expanded": String(open), "aria-controls": listId, "data-key": "project:" + p.id },
        h("span", { class: "chev-s" + (open ? " open" : "") }, icon("chev")), icon("folder"),
        h("span", { class: "t" }, p.name), lockMark(privacyOf(p.rules, p)),
        h("span", { class: "count" }, chats.length ? String(chats.length) : ""),
        rowButton("plus", "New chat in " + p.name, () => newChat(p.id, true)),
        rowButton("dots", "Options for " + p.name, (e) => projectMenu(e.currentTarget, p, chats)));
      const toggle = (e) => {
        if (open) SIDE.collapsed.add(p.id); else SIDE.collapsed.delete(p.id);
        SIDE.opened = !open && e && e.detail > 0 ? p.id : null;   // a pointer's: it opens by height
        store("lh.collapsed", JSON.stringify([...SIDE.collapsed]));
        renderChats();
      };
      nameTip(head, p.name);
      head.addEventListener("click", toggle);
      // its own Enter or Space: those of its ＋ and ⋯ are theirs (a new chat in it, its menu), never a fold
      head.addEventListener("keydown", (e) => {
        if (e.target !== head || (e.key !== "Enter" && e.key !== " ")) return;
        e.preventDefault();
        toggle();
      });
      dropTarget(head, p.id);
    }
    const list = h("div", { class: "proj-chats" }, ...(chats.length ? chats.map(chatItem)
      : [h("div", { class: "side-empty" }, "No conversations yet")]));
    return h("div", { class: "proj" }, head,
      h("div", { class: "proj-fold" + (open && SIDE.opened === p.id ? " k-open" : ""), id: listId, hidden: !open }, list));
  }
  function renderChats() {
    const byProject = new Map(S.projects.map((p) => [p.id, []]));
    const loose = [];
    for (const c of S.chats) (c.project && byProject.has(c.project) ? byProject.get(c.project) : loose).push(c);
    const kids = [h("div", { class: "side-label row" }, h("span", {}, "Projects"),
      rowButton("plus", "New project", () => startNewProject()))];
    if (SIDE.creating) {
      kids.push(nameInput("new-project", "", "Project name", createProject, () => { SIDE.creating = false; SIDE.moveAfterCreate = null; renderChats(); }));
    } else if (!S.projects.length) {
      const hint = h("button", { class: "side-hint", type: "button" }, icon("folder"), "New project");
      hint.addEventListener("click", () => startNewProject());
      kids.push(hint);
    }
    for (const p of S.projects) kids.push(projectGroup(p, byProject.get(p.id)));
    const label = h("div", { class: "side-label" }, "Conversations");
    dropTarget(label, null);
    kids.push(label, ...loose.map(chatItem));
    if (!loose.length) kids.push(h("div", { class: "side-empty" }, S.chats.length ? "Every conversation is in a project." : "No conversations yet."));
    const ed = SIDE.editor;
    const typing = ed && document.activeElement === ed.input;
    const had = document.activeElement && document.activeElement.closest && document.activeElement.closest("#chat-list [data-key]");
    $("chat-list").replaceChildren(...kids);
    SIDE.opened = null;   // a project opened by a pointer comes in by height once, not at every redraw
    if (had) {   // the row that had the focus (a fold pressed from the keyboard) keeps it, drawn again
      const again = [...$("chat-list").querySelectorAll("[data-key]")].find((el) => el.dataset.key === had.dataset.key);
      if (again) again.focus({ preventScroll: true });
    }
    if (ed && ed.el.isConnected && (ed.fresh || typing)) {
      ed.input.focus();
      if (ed.fresh) ed.input.select();
      ed.fresh = false;
    }
  }

  // a small menu under a button: [{label, icon, onClick, danger, role, checked} | {heading} | {sep: true}]. A checkable
  // item says its state (aria-checked) and draws it in both states: a setting that acts at once (role
  // "menuitemcheckbox") a box, ticked when on; a choice (role "menuitemradio", or an item given `checked` without a
  // role) a ring, with a dot in the chosen one, its items a group named by the heading over them. Choosing an item
  // closes the menu, whatever its kind; the arrows, Home and End move along it; Esc and Tab close it. Its button says
  // it opens a menu (aria-haspopup) and, while it is open, aria-expanded
  let openMenu = null, menuFrom = null;
  function closeMenu() {
    if (openMenu) { openMenu.remove(); openMenu = null; }
    if (menuFrom) { menuFrom.setAttribute("aria-expanded", "false"); menuFrom = null; }
  }
  function popMenu(anchor, items) {
    closeMenu();
    anchor.setAttribute("aria-haspopup", "menu");
    anchor.setAttribute("aria-expanded", "true");
    menuFrom = anchor;
    const menu = h("div", { class: "menu", role: "menu" });
    let head = null, group = null, n = 0;   // the heading just over, and the choice being filled
    for (const it of items) {
      if (it.sep) { menu.append(h("div", { class: "menu-sep", role: "separator" })); head = group = null; continue; }
      if (it.heading) { head = h("div", { class: "menu-head", id: "menu-head-" + ++n }, it.heading); menu.append(head); group = null; continue; }
      const role = it.role || ("checked" in it ? "menuitemradio" : "menuitem"), checks = role !== "menuitem";
      const lead = checks ? h("span", { class: "menu-check " + (role === "menuitemradio" ? "menu-radio" : "menu-box"), "aria-hidden": "true" },
        role === "menuitemcheckbox" ? icon("check") : null) : it.icon ? icon(it.icon) : h("span", { class: "menu-gap" });
      const b = h("button", { class: "menu-item" + (it.danger ? " danger" : ""), type: "button", role,
        "aria-checked": checks ? String(!!it.checked) : null }, lead, h("span", { class: "menu-label" }, it.label));
      b.addEventListener("click", (e) => { e.stopPropagation(); closeMenu(); it.onClick(); });
      if (role !== "menuitemradio") { menu.append(b); head = group = null; continue; }
      if (!group) { group = h("div", { class: "menu-group", role: "group", "aria-labelledby": head ? head.id : null }); menu.append(group); }
      group.append(b);
    }
    menu.addEventListener("keydown", (e) => {
      const buttons = [...menu.querySelectorAll(".menu-item")];
      const i = buttons.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); buttons[(i + 1) % buttons.length].focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); buttons[(i - 1 + buttons.length) % buttons.length].focus(); }
      if (e.key === "Home" || e.key === "End") { e.preventDefault(); buttons[e.key === "Home" ? 0 : buttons.length - 1].focus(); }
      if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); closeMenu(); anchor.focus(); }
    });
    document.body.append(menu);
    const z = pageZoom(), a = anchor.getBoundingClientRect(), W = window.innerWidth / z, H = window.innerHeight / z;
    const r = { left: a.left / z, top: a.top / z, bottom: a.bottom / z };   // the screen's pixels, as the page's
    const below = r.bottom + 4, above = r.top - menu.offsetHeight - 4;   // above when it doesn't fit below (the composer)
    const top = below + menu.offsetHeight + 8 <= H ? below : above >= 8 ? above : H - menu.offsetHeight - 8;
    menu.style.top = Math.max(8, top) + "px";
    menu.style.left = Math.max(8, Math.min(r.left, W - menu.offsetWidth - 8)) + "px";
    openMenu = menu;
    const first = menu.querySelector(".menu-item");
    if (first) first.focus();
  }
  document.addEventListener("click", (e) => { if (openMenu && !openMenu.contains(e.target)) closeMenu(); });
  window.addEventListener("resize", closeMenu);

  // a chat's project, a choice with one always chosen: a project, or No project (under Conversations)
  function chatMenu(anchor, c) {
    const items = [{ label: "Rename", icon: "pencil", onClick: () => { SIDE.renaming = c.id; renderChats(); } }];
    items.push({ heading: "Move to project" });
    if (S.projects.length) {
      for (const p of S.projects) {
        items.push({ label: p.name, role: "menuitemradio", checked: c.project === p.id, onClick: () => { if (c.project !== p.id) moveChat(c.id, p.id); } });
      }
      const loose = !S.projects.some((p) => p.id === c.project);   // a project gone meanwhile: under Conversations too
      items.push({ label: "No project", role: "menuitemradio", checked: loose, onClick: () => { if (c.project) moveChat(c.id, null); } });
    }
    items.push({ label: "New project…", icon: "plus", onClick: () => startNewProject(c.id) });
    items.push({ sep: true }, { label: "Delete", icon: "trash", danger: true, onClick: () => deleteChat(c) });
    popMenu(anchor, items);
  }
  function projectMenu(anchor, p, chats) {
    popMenu(anchor, [
      { label: "New chat in this project", icon: "plus", onClick: () => newChat(p.id, true) },
      { label: "Rename", icon: "pencil", onClick: () => { SIDE.renaming = p.id; renderChats(); } },
      { label: "Project settings", icon: "sliders", onClick: () => openProjectSettings(p) },
      { sep: true },
      { label: "Delete project", icon: "trash", danger: true, onClick: () => deleteProject(p, chats) },
    ]);
  }

  // project memory: the user's instructions and the notes Kelvin saved, seen only by this project's chats
  let editingProject = null;
  function freshProject(p, fresh) {   // the server's copy; a mark it no longer has must not linger here
    delete p.unpublished;
    delete p.confidential;
    return Object.assign(p, fresh);
  }
  async function openProjectSettings(p) {
    try { freshProject(p, await api("/api/projects/" + p.id)); } catch (e) { toast(e.message); return; }  // notes may be new
    editingProject = p;
    $("project-dialog-name").textContent = p.name;
    $("project-instructions").value = p.instructions || "";
    const rules = p.rules || { providers: null, literature: true };
    $("project-providers").replaceChildren(...providerChoices(rules.providers).map(([id, label]) => {
      const box = h("input", { type: "checkbox", value: id });
      box.checked = !rules.providers || rules.providers.includes(id);
      return h("label", { class: "check" }, box, " " + label);
    }));
    $("project-literature").checked = rules.literature !== false;
    $("project-sharing").value = p.unpublished ? "unpublished" : p.confidential ? "confidential" : "";
    $("project-confidential").checked = !!p.confidential;
    $("rules-summary").textContent = rulesText(rules);
    $("project-msg").textContent = "";
    renderNotes(p);
    renderPapers(p);
    loadKnow(p.id, $("project-know"), $("know-count"), $("project-msg"));
    $("project-summary").value = p.summary || "";
    renderDevices(p);
    $("project-dialog").showModal();
    loadProjectReports(p);
  }
  async function loadProjectReports(p) {
    try {
      const r = await api("/api/projects/" + p.id + "/devices");
      p.master = r.master;
      $("master-files").textContent = r.master.length ? "in reports/" + r.folder : "written after the first scan";
      $("master-open").disabled = !r.master.length;
    } catch (e) { $("master-files").textContent = ""; }
  }
  function renderDevices(p) {
    const devices = p.devices || [];
    $("devices-count").textContent = devices.length ? devices.length + (devices.length === 1 ? " device" : " devices") : "";
    if (!devices.length) {
      $("project-devices").replaceChildren(h("li", { class: "memory-empty" }, "No devices yet. Add the samples you measure in this project."));
      return;
    }
    $("project-devices").replaceChildren(...devices.map((d) => {
      const edit = rowButton("pencil", "Describe it", async () => {
        const text = await askText("Describe " + d.name + "\nGeometry, contacts, fabrication.", { value: d.description || "", ok: "Save" });
        if (text === null) return;
        try { Object.assign(d, await api("/api/projects/" + p.id + "/devices/" + d.id, { method: "PATCH", body: { description: text } })); renderDevices(p); }
        catch (e) { $("project-msg").textContent = e.message; }
      });
      const retire = h("button", { class: "link soft", type: "button" }, d.retired ? "Restore" : "Retire");
      retire.addEventListener("click", async () => {
        try { Object.assign(d, await api("/api/projects/" + p.id + "/devices/" + d.id, { method: "PATCH", body: { retired: !d.retired } })); renderDevices(p); loadMount(); }
        catch (e) { $("project-msg").textContent = e.message; }
      });
      return h("li", { class: "memory-note" + (d.retired ? " muted" : "") }, h("span", { class: "memory-kind" }, d.retired ? "retired" : "device"),
        h("div", { class: "memory-body" }, h("span", {}, d.name + (d.material ? " · " + d.material : "")),
          h("span", { class: "memory-meta" }, d.description || "no description yet")), edit, retire);
    }));
  }
  async function addDevice() {
    const p = editingProject;
    const name = $("device-name").value.trim();
    if (!name) { $("project-msg").textContent = "Give the device a name."; return; }
    try {
      const r = await api("/api/projects/" + p.id + "/devices", { body: { name, material: $("device-material").value.trim() } });
      p.devices = r.devices;
      $("device-name").value = $("device-material").value = "";
      renderDevices(p);
      loadMount();
    } catch (e) { $("project-msg").textContent = e.message; }
  }
  async function writeSummary() {
    const p = editingProject;
    $("project-msg").textContent = "Kelvin is writing the opening from the project's scans and notes…";
    $("summary-write").disabled = true;
    try {
      const r = await api("/api/projects/" + p.id + "/summary", { method: "POST", body: { model: $("model").value } });
      $("project-summary").value = r.summary;
      $("project-msg").textContent = "Written. Edit it if you like, then Save; the master report uses it.";
    } catch (e) { $("project-msg").textContent = e.message; } finally { $("summary-write").disabled = false; }
  }
  async function updateMaster(open) {
    const p = editingProject;
    try {
      await api("/api/projects/" + p.id + "/summary", { method: "PUT", body: { text: $("project-summary").value } });
      if (!open || !(p.master || []).length) {
        $("project-msg").textContent = "Updating the master and device reports…";
        await api("/api/reports/refresh", { body: { project: p.id } });
        await loadProjectReports(p);
        $("project-msg").textContent = "Updated.";
      }
      if (open && (p.master || []).length) {
        const pdf = p.master.find((n) => n.endsWith(".pdf")) || p.master[0];
        await api("/api/reports/" + encodeURIComponent(pdf) + "/open", { body: {} });
      }
    } catch (e) { $("project-msg").textContent = e.message; }
  }
  function renderNotes(p) {
    const notes = p.notes || [];
    $("notes-count").textContent = notes.length ? notes.length + (notes.length === 1 ? " note" : " notes") : "";
    if (!notes.length) {
      $("project-notes").replaceChildren(h("li", { class: "memory-empty" },
        "No notes yet."));
      return;
    }
    $("project-notes").replaceChildren(...notes.map((n) => {
      const chat = S.chats.find((c) => c.id === n.chat);
      const when = new Date(n.created * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
      return h("li", { class: "memory-note" }, h("span", { class: "memory-kind" }, n.kind),
        h("div", { class: "memory-body" }, h("span", {}, n.text), h("span", { class: "memory-meta" }, when + (chat ? " · " + chat.title : ""))),
        rowButton("trash", "Delete this note", () => deleteNote(p, n)));
    }));
  }
  function renderPapers(p) {
    const papers = p.papers || [];
    $("papers-count").textContent = papers.length ? papers.length + (papers.length === 1 ? " paper" : " papers") : "";
    if (!papers.length) {
      $("project-papers").replaceChildren(h("li", { class: "memory-empty" },
        "No papers yet."));
      return;
    }
    $("project-papers").replaceChildren(...papers.map((e) => {
      const names = e.authors || [];
      const who = names.length ? names[0] + (names.length > 2 ? " et al." : names.length === 2 ? " and " + names[1] : "") : "";
      const title = e.url ? h("a", { href: e.url, target: "_blank", rel: "noopener noreferrer" }, e.title) : h("span", {}, e.title);
      return h("li", { class: "memory-note" }, h("span", { class: "memory-kind" }, e.year ? String(e.year) : "paper"),
        h("div", { class: "memory-body" }, title,
          h("span", { class: "memory-meta" }, [who, e.venue].filter(Boolean).join(" · ") + (e.why ? " — " + e.why : ""))),
        rowButton("trash", "Remove from the reading list", () => deletePaper(p, e)));
    }));
  }
  async function deletePaper(p, e) {
    try { await api("/api/projects/" + p.id + "/papers/" + e.id, { method: "DELETE" }); } catch (err) { $("project-msg").textContent = err.message; return; }
    p.papers = p.papers.filter((x) => x.id !== e.id);
    renderPapers(p);
  }
  async function deleteNote(p, n) {
    try { await api("/api/projects/" + p.id + "/notes/" + n.id, { method: "DELETE" }); } catch (e) { $("project-msg").textContent = e.message; return; }
    p.notes = p.notes.filter((x) => x.id !== n.id);
    renderNotes(p);
  }
  async function saveProjectSettings() {
    const p = editingProject;
    $("project-msg").textContent = "Saving…";
    try {
      const boxes = [...document.querySelectorAll("#project-providers input")];
      const chosen = boxes.filter((c) => c.checked).map((c) => c.value);
      if (!chosen.length) { $("project-msg").textContent = "Allow at least one kind of model."; return; }
      const rules = { providers: chosen.length === boxes.length ? null : chosen, literature: $("project-literature").checked };
      freshProject(p, await api("/api/projects/" + p.id, { method: "PATCH", body: { instructions: $("project-instructions").value, rules, sharing: $("project-sharing").value } }));
      if (($("project-summary").value || "") !== (p.summary || "")) {
        await api("/api/projects/" + p.id + "/summary", { method: "PUT", body: { text: $("project-summary").value } });
        p.summary = $("project-summary").value;
      }
      applyRules();
      renderChats();   // its lock in the sidebar, and its chats', at once
      $("project-dialog").close();
      toast("Saved. Conversations in " + p.name + " use it from their next message.");
    } catch (e) {
      $("project-msg").textContent = e.message;
    }
  }

  // ------------------------------------------------------------------ who may see a project
  const PROVIDER_NAMES = { anthropic: "Claude", openai: "GPT", gemini: "Gemini", local: "local models" };
  const providerName = (id) => PROVIDER_NAMES[id] || ((S.groups || []).find((g) => g.provider === id) || {}).label || id;
  function providerChoices(chosen) {   // the built-in kinds, every service set up, and any the rules still name
    const ids = Object.keys(PROVIDER_NAMES);
    for (const g of S.groups || []) if (!ids.includes(g.provider)) ids.push(g.provider);
    for (const p of chosen || []) if (!ids.includes(p)) ids.push(p);
    return ids.map((id) => [id, id === "local" ? "Local models" : providerName(id)]);
  }
  // unshared work at a glance: a confidential, unpublished or model-limited project, and the chats that keep its marks
  // (its runs stay inside it; it never goes to the Kelvin hub)
  function mergeRules(...sets) {   // the stricter of several rule sets, as Kelvin combines them (rules.py)
    let providers = null, literature = true;
    for (const r of sets) {
      if (!r) continue;
      if (r.providers) providers = providers === null ? [...r.providers] : providers.filter((x) => r.providers.includes(x));
      literature = literature && r.literature !== false;
    }
    return { providers, literature };
  }
  function privacyOf(rules, marks) {   // {label, why}, or null for shared work
    const r = rules || {};
    const m = marks || {};
    const why = [];
    if (r.providers) why.push("only " + r.providers.map(providerName).join(" and ") + " may see it");
    if (r.literature === false) why.push("no literature or web search");
    if (m.confidential) why.push("never through the Kelvin hub");
    if (!why.length && !m.unpublished) return null;
    why.push("not shared on the hub, and its runs stay in its own chats");
    const label = m.confidential || r.providers ? "Confidential" : m.unpublished ? "Unpublished" : "No search";
    return { label, why: label + ": " + why.join("; ") };
  }
  function chatPrivacy(c) {   // its own marks and rules (it left such a project, or ran under them), and its project's
    if (!c) return null;
    const p = c.project && S.projects.find((x) => x.id === c.project);
    return privacyOf(mergeRules(c.rules, p && p.rules), { confidential: c.confidential || (p && p.confidential),
      unpublished: c.unpublished || (p && p.unpublished) });
  }
  function lockMark(privacy) {
    return privacy ? h("span", { class: "lock-mark", title: privacy.why, "aria-label": privacy.why }, icon("lock")) : null;
  }
  function rulesText(rules) {
    if (!rules) return "";
    const parts = [];
    if (rules.providers) parts.push("only " + rules.providers.map(providerName).join(" and "));
    if (rules.literature === false) parts.push("no literature search");
    return parts.join("; ");
  }
  function currentRules() {
    if (S.chatId) return S.chatRules || null;
    const p = SIDE.newProject && S.projects.find((x) => x.id === SIDE.newProject);
    return p ? p.rules || null : null;
  }
  const modelAllowed = (key, rules) => !rules || !rules.providers || rules.providers.includes(key.split(":")[0]);
  function applyRules() {   // grey out the models this chat may not use, and say why
    const rules = currentRules();
    const sel = $("model");
    for (const o of sel.querySelectorAll("option")) {
      if (!o.value) continue;
      o.disabled = !modelAllowed(o.value, rules);
      o.textContent = o.textContent.replace(/ — not allowed here$/, "") + (o.disabled ? " — not allowed here" : "");
    }
    if (sel.selectedOptions[0] && sel.selectedOptions[0].disabled) {
      const ok = [...sel.options].find((o) => o.value && !o.disabled);
      sel.value = ok ? ok.value : "";
    }
    S.compare = S.compare.filter((k) => modelAllowed(k, rules));
    compareLabel();
    showPlanChip();
    const chat = S.chats.find((c) => c.id === S.chatId);   // a new chat: the project it starts in
    const privacy = S.incognito ? null : chatPrivacy(Object.assign({ project: SIDE.newProject }, chat,
      { rules: mergeRules(chat && chat.rules, rules) }));
    $("rules-chip").hidden = !privacy;
    $("rules-chip").title = privacy ? privacy.why : "";
    $("rules-chip-text").textContent = privacy ? privacy.label : "";
    updateSend();
  }

  function startNewProject(moveChatId) {
    SIDE.creating = true;
    SIDE.moveAfterCreate = moveChatId || null;
    renderChats();
  }
  async function createProject(name) {
    SIDE.creating = false;
    let p;
    try { p = await api("/api/projects", { body: { name } }); } catch (e) { toast(e.message); renderChats(); return; }
    S.projects.push(p);
    const move = SIDE.moveAfterCreate;
    SIDE.moveAfterCreate = null;
    if (move) await moveChat(move, p.id); else renderChats();
  }
  async function renameProject(p, name) {
    SIDE.renaming = null;
    try { freshProject(p, await api("/api/projects/" + p.id, { method: "PATCH", body: { name } })); } catch (e) { toast(e.message); }
    renderChats();
    setTitle(currentTitle(), currentProject());
  }
  async function deleteProject(p, chats) {
    const n = chats.length;
    const kept = n ? "\nIts " + (n === 1 ? "conversation stays" : n + " conversations stay") + ", under Conversations." : "";
    if (!(await askYes("Delete the project “" + p.name + "”?" + kept, { ok: "Delete", danger: true }))) return;
    try { await api("/api/projects/" + p.id, { method: "DELETE" }); } catch (e) { toast(e.message); return; }
    S.projects = S.projects.filter((x) => x.id !== p.id);
    if (SIDE.newProject === p.id) SIDE.newProject = null;
    await refreshChats();
  }
  async function moveChat(id, projectId) {
    try { await api("/api/chats/" + id, { method: "PATCH", body: { project: projectId } }); } catch (e) { toast(e.message); return; }
    const c = S.chats.find((x) => x.id === id);
    if (c) c.project = projectId;
    if (projectId) { SIDE.collapsed.delete(projectId); store("lh.collapsed", JSON.stringify([...SIDE.collapsed])); }
    renderChats();
    if (id === S.chatId) setTitle(currentTitle(), projectId);
    toast(projectId ? "Moved to " + projectName(projectId) + "." : "Removed from its project.");
  }
  async function renameChat(c, title) {
    SIDE.renaming = null;
    try { await api("/api/chats/" + c.id, { method: "PATCH", body: { title } }); c.title = title; } catch (e) { toast(e.message); }
    renderChats();
    if (c.id === S.chatId) setTitle(c.title, c.project);
  }
  async function deleteChat(c) {
    if (!(await askYes("Delete “" + (c.title || "New chat") + "”?\nThe lab's event log and run data are kept.",
      { ok: "Delete", danger: true }))) return;
    try { await api("/api/chats/" + c.id, { method: "DELETE" }); } catch (err) { toast(err.message); return; }
    if (S.chatId === c.id) newChat(c.project);
    await refreshChats();
  }
  function currentTitle() {
    const c = S.chats.find((x) => x.id === S.chatId);
    return c ? c.title : "";
  }

  async function refreshChats() {
    try { S.chats = await api("/api/chats"); } catch (e) { return; }
    renderChats();
    setTitle(currentTitle(), currentProject());
    // the open chat changed while it wasn't answering (a change told to it, a phone): drawn again
    const id = S.chatId, open = S.chats.find((c) => c.id === id);
    if (open && !S.streaming && open.updated > (S.chatUpdated || 0)) {
      try {
        const chat = await api("/api/chats/" + id);
        if (S.chatId === id && !S.streaming) { renderChat(chat); S.chatUpdated = open.updated; }
      } catch (e) { /* the next change tries again */ }
    }
  }
  // Incognito, a mode of the chat: a toggle button (aria-pressed) with a look of its own, dashed as the composer is
  // then. On, from anywhere (Home too), an incognito chat takes the chat's place; the chat open before is kept to come
  // back to. Off, or another chat opened meanwhile, forgets it: asked first ("Forget this chat?") when it holds
  // messages, and Cancel keeps it. Off goes back to the chat before. Never remembered: a reload starts without it
  const INCOG = { before: null };   // the chat open when it began: { id } or, a new one, { project }
  const incogSaid = () => S.incognito && !!S.chatId && !!thread.querySelector(".msg");
  async function mayLeaveIncognito() {   // true: nothing in it to lose, or the person agreed to forget it
    if (!incogSaid()) return true;
    return (await askYes("Forget this chat?\nIt is incognito: nothing of it is kept once you leave it.",
      { ok: "Forget", danger: true })) === true;
  }
  // an incognito chat is forgotten as soon as you leave it
  function leaveIncognito() {
    if (S.incognito && S.chatId) api("/api/chats/" + S.chatId, { method: "DELETE" }).catch(() => {});
    if (S.incognito) { S.incognito = false; INCOG.before = null; applyIncognito(); }
  }
  const askHint = () => S.incognito ? "Ask anything; nothing here is remembered" : "Ask Kelvin…";
  function applyIncognito() {
    document.body.classList.toggle("incognito", S.incognito);
    const b = $("toggle-incognito");
    b.setAttribute("aria-pressed", String(S.incognito));   // its own on look (app.css "fit I"), never an open pane's .on
    if (b.dataset.tip !== "Incognito chat") b.title = "Incognito chat";   // its name, on or off (the top bar's tip)
    $("greeting").textContent = S.incognito ? "Incognito chat" : "What are we measuring today?";
    $("empty-sub").textContent = S.incognito
      ? "No memory, and forgotten when you leave. Instrument commands and runs are still logged."
      : S.labSub || "";
    $("input").placeholder = askHint();
  }
  async function toggleIncognito() {
    exitFull();
    setMode("chat", true);   // the chat is where it shows, on or off (from Home too)
    if (S.incognito) {   // off: forgotten (asked first when it holds messages), then the chat before again
      if (!(await mayLeaveIncognito())) return;   // kept: the focus goes back to the button
      const back = INCOG.before || {};
      leaveIncognito();
      // the chat or the project before, unless deleted meanwhile (then a new chat); a chat made just before it began
      // may not be listed yet, so the list is read once more before giving it up
      if (back.id && !S.chats.some((c) => c.id === back.id)) await refreshChats();
      const id = S.chats.some((c) => c.id === back.id) ? back.id : null;
      if (!id || !(await openChat(id))) newChat(S.projects.some((p) => p.id === back.project) ? back.project : null);
      return;
    }
    INCOG.before = S.chatId ? { id: S.chatId } : { project: SIDE.newProject };
    if (S.chatId) newChat();
    S.incognito = true;
    SIDE.newProject = null;
    applyIncognito();
    setTitle("", null);
    renderChats();
    $("input").focus();
  }

  async function openChat(id) {   // another chat may go on answering meanwhile: its answer waits for you there
    if (!(await mayLeaveIncognito())) return false;   // leaving an incognito chat forgets it: asked first
    let chat;
    try { chat = await api("/api/chats/" + id); } catch (e) { toast(e.message); return false; }
    exitFull();
    setMode("chat", true);
    leaveIncognito();
    S.chatId = id;
    S.unread.delete(id);
    SIDE.newProject = null;
    unfoldProject(chat.project);   // its project shows it, though folded before
    S.chatRules = chat.rules || null;
    showChat(chat);
    setTitle(chat.title, chat.project);
    showFolder(chat.folder, chat.own_folder);
    if (chat.model && S.labels[chat.model]) $("model").value = chat.model;
    applyRules();
    renderChats();
    refreshComposer();
    closeSidebar();
    refreshAnalysisRuns();   // the runs this chat's model may see
    return true;
  }
  function showChat(chat) {   // a chat still answering in this window: what it said before, then its answer as it goes
    const live = S.live.get(chat.id);
    if (!live) { renderChat(chat); return; }
    const upTo = live.from === null ? chat.messages.length : live.from + 1;   // up to the message it answers
    renderChat(Object.assign({}, chat, { messages: chat.messages.slice(0, upTo) }));
    thread.append(live.box);
    requestAnimationFrame(toBottom);
  }
  const titleOf = (id) => (S.chats.find((c) => c.id === id) || {}).title || "a chat";
  // ask: the person started it, so they are asked for its folder. A promise of whether it started one: false when the
  // person kept the incognito chat they were asked about; else at once (the new chat is there as it returns)
  function newChat(projectId, ask) {
    if (incogSaid()) {   // leaving an incognito chat forgets it: asked first, and Cancel keeps it
      return mayLeaveIncognito().then((ok) => ok && (leaveIncognito(), newChat(projectId, ask)));
    }
    exitFull();
    setMode("chat", true);
    leaveIncognito();
    S.chatId = null;
    if (S.defaultModel && S.labels[S.defaultModel]) { $("model").value = S.defaultModel; updateSend(); }
    S.chatRules = null;
    SIDE.newProject = projectId || null;
    applyRules();
    thread.replaceChildren();
    showEmpty(true);
    setTitle("", SIDE.newProject);
    renderChats();
    refreshComposer();
    closeSidebar();
    applyIncognito();
    if (ask) askFolder(); else loadNewFolder();
    loadStartChecklist();
    refreshAnalysisRuns();   // the runs a new chat's model may see
    $("input").focus();
    return Promise.resolve(true);
  }

  // ------------------------------------------------------------------ the chat's folder: where its runs go
  const FOLDER = { path: "", own: false };
  function folderName(path) { const parts = path.split(/[\\/]+/).filter(Boolean); return parts[parts.length - 1] || path; }
  // folders by their names, and by their whole paths where two have the same name (the lab's scripts folders)
  function folderLabels(paths) {
    const same = (a, b) => a.toLowerCase() === b.toLowerCase();
    return new Map(paths.map((p) => [p, paths.some((q) => q !== p && same(folderName(q), folderName(p))) ? p : folderName(p)]));
  }
  function showFolder(path, own) {
    FOLDER.path = path || "";
    FOLDER.own = !!own;
    $("folder-label").textContent = FOLDER.own ? folderName(FOLDER.path) : "Data folder";
    $("folder-btn").title = "This chat saves its runs in " + (FOLDER.own ? "" : "the data folder, ") + FOLDER.path;
    $("folder-btn").classList.toggle("own", FOLDER.own);
    $("folder-btn").hidden = !FOLDER.path;
  }
  async function loadNewFolder() {   // where a new chat (in this project) saves its runs
    let r;
    try { r = await api("/api/folder" + (SIDE.newProject ? "?project=" + encodeURIComponent(SIDE.newProject) : "")); } catch (e) { return; }
    if (!S.chatId) showFolder(r.folder, r.folder !== r.data);
  }
  function hasPicker() { return !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder); }
  // the computer's own folder picker in the Kelvin app; in a browser, Kelvin's. opts.create: a folder that isn't there
  // yet may be chosen too, to be made where it is used (a chat's folder, the data folder)
  async function pickFolder(start, question, opts = {}) {
    if (hasPicker()) {
      try { return (await window.pywebview.api.pick_folder(start || "")) || ""; } catch (e) { return ""; }
    }
    return browseFolder(start || "", question, !!opts.create);
  }
  // Kelvin's folder browser, for a browser on the Kelvin computer, which can't give a folder's path itself: the
  // computer's folders as Kelvin lists them (GET /api/browse: names only), gone into with a click or Enter, up with Up
  // or Backspace, to a drive or Home; a path typed or pasted at the top goes there with Enter. The folder chosen, or ""
  // when cancelled. create: a path typed that isn't there can be used all the same (Use it anyway), and is made then.
  function browseFolder(start, question, create) {
    return new Promise((resolve) => {
      // the listing shown, the latest one asked for and the one still coming, its folders' buttons, and whether the
      // path field holds what the person typed since a listing filled it
      const B = { at: null, seq: 0, asked: null, items: [], edited: false };
      const unquote = (s) => String(s || "").trim().replace(/^"(.*)"$/, "$1").trim();   // Explorer's Copy as path
      const bare = (p) => p.replace(/[\\/]+$/, "") || p;   // without its last separator: C:\ is C:
      const path = h("input", { class: "kfield folder-path", type: "text", spellcheck: "false", autocomplete: "off",
                                placeholder: "Type or paste a folder's path", "aria-label": "Folder path" });
      const places = h("div", { class: "folder-places", role: "group", "aria-label": "Drives" });
      const up = h("button", { class: "btn small", type: "button", title: "Up (Backspace)", disabled: true }, icon("up"), "Up");
      const crumbs = h("nav", { class: "folder-crumbs", "aria-label": "Path" });
      const list = h("div", { class: "folder-items", role: "group", "aria-label": "Folders", tabindex: "-1", "data-region": "dialog" });
      const note = h("p", { class: "hint folder-note", role: "status" });
      const count = h("span", { class: "folder-count" });
      const cancel = h("button", { class: "btn", type: "button" }, "Cancel");
      const choose = h("button", { class: "btn primary", type: "button", disabled: true }, "Choose this folder");
      const form = h("form", {},
        h("header", { class: "dialog-head" },
          h("h2", { id: "folder-title" }, String(question || "Choose a folder").trim().replace(/\s*:$/, "")),
          h("button", { class: "icon-btn", type: "button", "aria-label": "Cancel", onclick: () => done("") }, icon("x"))),
        path, places, h("div", { class: "folder-where" }, up, crumbs), list, note,
        h("footer", { class: "dialog-foot" }, count, cancel, choose));
      const dlg = h("dialog", { class: "dialog folder-dialog", "aria-labelledby": "folder-title" }, form);
      const done = (v) => { B.seq++; if (B.asked) B.asked.stop.abort(); if (dlg.open) dlg.close(); dlg.remove(); resolve(v); };
      function focusItem(i) {   // one folder in the Tab order at a time: the one the keys are on
        B.items.forEach((b, k) => { b.tabIndex = k === i ? 0 : -1; });
        B.items[i].focus();
      }
      // a listing: its path, the drives, the way to it, its folders, its scripts (how: as go was asked)
      function show(r, how) {
        B.at = r;
        const typing = B.edited && !how.typed && document.activeElement === path;   // what the person types stays
        if (!typing) {
          path.value = r.missing || r.path;   // a path that isn't there stays in the field, to be fixed
          path.classList.toggle("bad", !!r.missing);
          B.edited = false;
        }
        places.replaceChildren(...[["Home", r.home]].concat(r.roots.map((p) => [bare(p), p])).map(([label, p]) =>
          h("button", { class: "btn small", type: "button", onclick: () => go(p) }, label)));
        up.disabled = !r.parent;
        crumbs.replaceChildren(...r.parts.flatMap((p, i) => {
          const part = i === r.parts.length - 1 ? h("span", { "aria-current": "location" }, p.name)
            : h("button", { type: "button", onclick: () => go(p.path) }, p.name);
          return i ? [h("span", { class: "folder-sep", "aria-hidden": "true" }, "›"), part] : [part];
        }));
        // a double-click, as in Explorer, goes in once: its second click would land in the folder's own list
        B.items = r.folders.map((f) => h("button", { class: "folder-item", type: "button", tabindex: "-1", "data-path": f.path,
          onclick: (e) => { if (e.detail < 2) go(f.path); } }, icon("folder"), h("span", {}, f.name)));
        list.replaceChildren(...B.items.concat([B.items.length ? null : h("p", { class: "hint" }, "No folders here."),
          r.partial ? h("p", { class: "hint" }, "More not shown (a large or slow folder)")
            : r.more ? h("p", { class: "hint" }, r.more + " more not shown") : null].filter(Boolean)));
        list.scrollTop = 0;
        note.replaceChildren(...(r.note ? [r.note] : []), ...(r.missing && create
          ? [h("button", { class: "btn small", type: "button", onclick: () => done(r.missing) }, "Use it anyway")] : []));
        const s = r.scripts;   // of a folder read in part, at least so many
        count.replaceChildren(...(s ? [h("span", { class: "chip blue" }, s + (r.partial ? "+ scripts" : s === 1 ? " script" : " scripts"))] : []));
        choose.disabled = !typing && !!r.missing;   // a folder that isn't there: Use it anyway, or another path
        const into = !how.typed && document.activeElement !== path;
        if (!B.items.length) { if (into) list.focus(); return; }
        const i = Math.max(0, B.items.findIndex((b) => b.dataset.path === how.from));   // up: the folder come from
        B.items[i].tabIndex = 0;
        if (into) focusItem(i);
      }
      // the listing of where, shown unless something newer was asked for meanwhile: it, or null. how.typed: a path from
      // the path field, where the keys stay; how.from: the folder gone up from. The folder still coming, asked for
      // again, is the same answer; one no longer wanted is dropped, its request too
      function go(where, how = {}) {
        where = where || "";
        if (B.asked && B.asked.where === where) return B.asked.answer;
        if (B.asked) B.asked.stop.abort();
        const seq = ++B.seq, stop = new AbortController();
        B.asked = { where, stop, answer: read(where, how, seq, stop.signal) };
        return B.asked.answer;
      }
      async function read(where, how, seq, signal) {
        list.setAttribute("aria-busy", "true");
        const slow = setTimeout(() => { if (seq === B.seq) note.textContent = "Reading…"; }, 300);
        try {
          const r = await api("/api/browse?path=" + encodeURIComponent(where), { signal });
          if (seq !== B.seq) return null;
          show(r, how);
          return r;
        } catch (e) {
          if (seq !== B.seq) return null;
          note.textContent = e.message;
          path.classList.toggle("bad", !!how.typed);
          return null;
        } finally {
          clearTimeout(slow);
          if (seq === B.seq) { list.removeAttribute("aria-busy"); B.asked = null; }
        }
      }
      const goUp = () => { if (B.at && B.at.parent) go(B.at.parent, { from: B.at.path }); };
      dlg.addEventListener("keydown", (e) => {   // Backspace goes up, but where a path is typed; a key held down acts once
        if (e.target === path) return;
        if (e.repeat && (e.key === "Enter" || e.key === "Backspace")) e.preventDefault();
        else if (e.key === "Backspace") { e.preventDefault(); goUp(); }
      });
      list.addEventListener("keydown", (e) => {   // ↑ ↓ Home End move, a letter goes to the next folder so named
        const n = B.items.length, at = B.items.indexOf(document.activeElement);
        let to = -1;
        if (e.key === "ArrowDown") to = Math.min(at + 1, n - 1);
        else if (e.key === "ArrowUp") to = Math.max(at - 1, 0);
        else if (e.key === "Home") to = 0;
        else if (e.key === "End") to = n - 1;
        else if (e.key.length === 1 && e.key.trim() && !e.ctrlKey && !e.metaKey && !e.altKey) {
          const k = e.key.toLowerCase();
          for (let s = 1; s <= n && to < 0; s++) if (B.items[(at + s) % n].textContent.toLowerCase().startsWith(k)) to = (at + s) % n;
        } else return;
        e.preventDefault();
        if (to >= 0 && n) focusItem(to);
      });
      path.addEventListener("keydown", (e) => {   // Enter goes to the path typed; ↓ to the folders
        if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault();
          if (!e.repeat && unquote(path.value)) go(unquote(path.value), { typed: true });
        } else if (e.key === "ArrowDown" && B.items.length) {
          e.preventDefault();
          focusItem(Math.max(0, B.items.findIndex((b) => b.tabIndex === 0)));
        }
      });
      path.addEventListener("input", () => {   // the person's own path: kept while they type, and Choose goes there
        B.edited = true;
        path.classList.remove("bad");
        choose.disabled = !B.at;
      });
      form.addEventListener("submit", (e) => e.preventDefault());
      up.addEventListener("click", goUp);
      cancel.addEventListener("click", () => done(""));
      choose.addEventListener("click", async () => {
        const typed = unquote(path.value);
        if (B.at && typed && bare(typed) !== bare(B.at.path)) {   // typed or pasted and not gone to yet: there first
          const r = await go(typed, { typed: true });
          if (!r || r.note) return;
        }
        if (B.at) done(B.at.path);
      });
      dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(""); });   // Escape
      document.body.append(dlg);
      dlg.showModal();
      list.focus();   // the keys start in the folders
      const opened = go(start), first = B.seq;
      opened.then((r) => {   // a start that can't be read at all: the home folder, saying why (unless the person went
        if (r || B.at || !dlg.open || B.seq !== first || !start) return;   // elsewhere meanwhile)
        const why = note.textContent;
        go("").then((home) => { if (home && !home.note) note.textContent = why; });
      });
    });
  }
  async function useFolder(path) {   // path null: the data folder
    if (S.chatId) {
      const chat = await api("/api/chats/" + S.chatId, { method: "PATCH", body: { folder: path } });
      showFolder(chat.folder, chat.own_folder);
    } else {   // kept until the chat's first message makes it
      const r = await api("/api/folder", { body: { path, project: SIDE.newProject } });
      showFolder(r.folder, !!path);
    }
  }
  async function changeFolder() {
    const path = await pickFolder(FOLDER.path, "Choose where this chat saves its runs", { create: true });
    if (!path) return;
    try { await useFolder(path); toast("This chat saves its runs in " + FOLDER.path); } catch (e) { toast(e.message); }
  }
  async function askFolder() {   // a chat the person starts: they say where its runs go (Settings can turn it off)
    await loadNewFolder();
    if (S.settings && S.settings.ask_folder === false) return;
    const path = await pickFolder(FOLDER.path, "Choose where this chat saves its runs", { create: true });
    if (path && !S.chatId) { try { await useFolder(path); } catch (e) { toast(e.message); } }
  }
  // where the chat saves its runs is a choice with one always chosen: the data folder, or one of the recent folders (the
  // chat's own among them, though it fell off the list)
  async function folderMenu() {   // like a code editor's folder menu: the whole path, recent folders, another one
    let recent = [];
    try { recent = (await api("/api/folder" + (SIDE.newProject ? "?project=" + encodeURIComponent(SIDE.newProject) : ""))).recent || []; } catch (e) { /* the rest still works */ }
    const same = (a, b) => a.toLowerCase() === b.toLowerCase();
    if (FOLDER.own && !recent.some((p) => same(p, FOLDER.path))) recent.unshift(FOLDER.path);
    const items = [{ heading: FOLDER.path }];
    if (recent.length) {
      items.push({ heading: "Recent folders" }, { label: "Data folder", role: "menuitemradio", checked: !FOLDER.own,
        onClick: () => { if (FOLDER.own) useFolder(null).catch((e) => toast(e.message)); } });
      for (const p of recent) {
        const twin = recent.some((q) => q !== p && same(folderName(q), folderName(p)));   // two "data" folders: say which
        items.push({ label: twin ? p : folderName(p), role: "menuitemradio", checked: FOLDER.own && same(p, FOLDER.path),
          onClick: () => { if (!(FOLDER.own && same(p, FOLDER.path))) useFolder(p).catch((e) => toast(e.message)); } });
      }
      items.push({ sep: true });
    }
    items.push({ label: "Choose folder…", icon: "folder-open", onClick: changeFolder },
      { label: "Open folder", icon: "doc", onClick: () => api("/api/folder", { body: { open: true, chat: S.chatId, project: SIDE.newProject } }).catch((e) => toast(e.message)) });
    popMenu($("folder-btn"), items);
  }

  // ------------------------------------------------------------------ sending
  function refreshComposer() {   // this chat answering: what you write adds to what Kelvin is doing; else it asks
    $("input").placeholder = S.streaming ? "Add to what Kelvin is doing: it reads this after the step it is on" : askHint();
    updateSend();
  }
  function updateSend() {   // while Kelvin works: Stop, or Send once something is typed (Kelvin reads it next step)
    const send = $("send");
    const typed = !!$("input").value.trim();
    const stop = S.streaming && !typed;
    send.classList.toggle("stop", stop);
    send.setAttribute("aria-label", stop ? "Stop" : "Send");
    send.disabled = !!S.sending || (!S.streaming && ((!typed && !ATT.items.length) || !$("model").value));
  }
  // ------------------------------------------------------------------ /name: start a skill or a recipe
  const SLASH = { items: null, shown: [], at: 0, for: null };
  async function slashMenu() {
    const m = /^\/([a-z0-9-]*)$/i.exec($("input").value);
    if (!m) { closeSlash(); return; }
    const project = currentProject() || "";
    if (SLASH.items === null || SLASH.for !== project) {
      SLASH.for = project;
      try { SLASH.items = (await api("/api/know/menu?project=" + encodeURIComponent(project) + (S.incognito ? "&incognito=1" : ""))).items; }
      catch (e) { SLASH.items = []; }
    }
    const typed = m[1].toLowerCase();
    SLASH.shown = SLASH.items.filter((i) => i.name.startsWith(typed)).concat(
      SLASH.items.filter((i) => !i.name.startsWith(typed) && i.name.includes(typed))).slice(0, 12);
    SLASH.at = 0;
    renderSlash();
  }
  function renderSlash() {
    const menu = $("slash-menu");
    if (!SLASH.shown.length) { menu.hidden = true; return; }
    menu.replaceChildren(...SLASH.shown.map((it, k) => {
      const b = h("button", { class: "slash-item" + (k === SLASH.at ? " on" : ""), type: "button", role: "option" },
        h("b", {}, "/" + it.name), h("span", {}, (it.kind === "recipe" ? "recipe: " : "") + it.description));
      b.addEventListener("mousedown", (e) => { e.preventDefault(); pickSlash(it); });
      return b;
    }));
    menu.hidden = false;
  }
  function pickSlash(it) {
    $("input").value = "/" + it.name + " ";
    closeSlash(); autosize(); updateSend();
    $("input").focus();
  }
  function closeSlash() { $("slash-menu").hidden = true; SLASH.shown = []; SLASH.items = null; }   // fresh next time
  function slashKey(e) {   // the arrows, Enter, Tab and Esc work the menu while it is open
    if ($("slash-menu").hidden || !SLASH.shown.length) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      SLASH.at = (SLASH.at + (e.key === "ArrowDown" ? 1 : SLASH.shown.length - 1)) % SLASH.shown.length;
      renderSlash();
    } else if (e.key === "Enter" || e.key === "Tab") pickSlash(SLASH.shown[SLASH.at]);
    else if (e.key === "Escape") closeSlash();
    else return false;
    e.preventDefault();
    return true;
  }
  async function steerMessage(text) {   // written while Kelvin works: it reads it at its next step
    const live = liveHere(), chatId = S.chatId;
    if (!text || !live) return;
    $("input").value = ""; autosize(); updateSend();
    const el = userBubble(text, []);
    el.classList.add("pending");
    el.title = "Kelvin reads this after the step it is on";
    live.box.append(el);
    toBottom();
    const one = { text, el };
    live.steers.push(one);
    try { await api("/api/chats/" + chatId + "/steer", { body: { text } }); } catch (e) { one.late = true; }   // sent after
  }
  function steered(live, texts, model) {   // Kelvin read them: they stand in the chat, and its answer goes on below
    live.reply.finish();
    for (const text of texts) {
      const i = live.steers.findIndex((s) => s.text === text && !s.late);
      if (i < 0) continue;
      live.steers[i].el.classList.remove("pending");
      live.steers[i].el.removeAttribute("title");
      live.steers.splice(i, 1);
    }
    const next = new Reply(model);
    const waiting = live.steers.find((s) => s.el.parentNode === live.box);
    if (waiting) live.box.insertBefore(next.el, waiting.el); else live.box.append(next.el);
    return next;
  }

  // ------------------------------------------------------------------ attached pictures, PDFs, data and text files
  const ATT = { items: [] };
  // data files: each becomes a run as it is attached, which Kelvin's tools read (imports.py)
  const DATA_FILE = /\.(csv|tsv|txt|dat|asc|xlsx|xlsm|mpt|mpr|dta|npy|npz|tiff?|spe)$/i;
  const isData = (f) => DATA_FILE.test(f.name || "");
  // text files: scripts, notebooks, notes and settings, given to the model as text (agent.py TEXT_FILES)
  const TEXT_FILE = /\.(pyw?|ipynb|m|jl|r|c|h|cpp|hpp|cs|java|js|ts|sh|ps1|bat|cmd|vb|bas|ino|pro|ipf|tex|bib|md|rst|log|json|ya?ml|toml|ini|cfg|conf|xml|html?)$/i;
  const isText = (f) => TEXT_FILE.test(f.name || "");
  function addFiles(list) {
    for (const f of list) {
      if (!isData(f) && !isText(f) && !/^image\//.test(f.type) && f.type !== "application/pdf") { toast((f.name || "That file") + ": Kelvin takes pictures, PDFs, data files (CSV, Excel, NumPy, TIFF…) and text files (scripts, notebooks, notes)."); continue; }
      if (ATT.items.length >= 10) { toast("Up to 10 files with one message."); break; }
      ATT.items.push(f);
    }
    renderAttachments();
  }
  function renderAttachments() {
    $("attach-list").replaceChildren(...ATT.items.map((f, i) => {
      const x = h("button", { class: "attach-x", type: "button", "aria-label": "Remove " + (f.name || "the picture") }, "×");
      x.addEventListener("click", () => { ATT.items.splice(i, 1); renderAttachments(); });
      return h("span", { class: "attach-chip", title: f.name || "" }, h("span", { class: "attach-name" }, f.name || "Pasted picture"), x);
    }));
    $("attach-list").hidden = !ATT.items.length;
    updateSend();
  }
  async function shrink(file) {   // a phone's photo is megabytes; models read about 1600 pixels across
    if (isData(file) || isText(file) || file.type === "application/pdf" || file.type === "image/gif") return file;   // a TIFF's numbers stay as they are
    let bmp;
    try { bmp = await createImageBitmap(file); } catch (e) { throw new Error((file.name || "The picture") + ": Kelvin can't read this picture. Save it as JPEG or PNG."); }
    const scale = Math.min(1, 1568 / Math.max(bmp.width, bmp.height));
    if (scale === 1 && file.size <= 3.5e6 && /^image\/(png|jpeg|webp)$/.test(file.type)) { if (bmp.close) bmp.close(); return file; }
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(bmp.width * scale)); c.height = Math.max(1, Math.round(bmp.height * scale));
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    if (bmp.close) bmp.close();
    return await new Promise((ok) => c.toBlob(ok, file.type === "image/png" ? "image/png" : "image/jpeg", 0.88));
  }
  async function uploadAttachments(items) {   // kept with the chat; the message names them
    const out = [];
    for (const f of items) {
      const res = await fetch("/api/chats/" + S.chatId + "/attachments", {
        method: "POST", body: await shrink(f),
        headers: { "X-Kelvin-Token": token, "Content-Type": "application/octet-stream",
                   "X-File-Name": encodeURIComponent(f.name || "pasted-picture.png") },
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* not JSON */ }
      if (!res.ok) throw new Error((f.name ? f.name + ": " : "") + ((data && data.error) || "HTTP " + res.status));
      out.push(data);
    }
    return out;
  }
  async function takeAttachments() {   // null when an upload failed (said in a toast; the files stay attached)
    if (!ATT.items.length) return [];
    try {
      const done = await uploadAttachments(ATT.items);
      ATT.items = [];
      renderAttachments();
      return done;
    } catch (e) { toast(e.message); return null; }
  }

  // a message that only says stop (web/agent.py typed_stop): Kelvin's answer and the lab's work stop now
  const TYPED_STOP = /^\s*\/?(?:stop|abort|halt)(?:\s+(?:it|now|everything|all|stop|abort))*\s*[.!]*\s*$/i;
  async function hardStop() {
    $("input").value = ""; autosize(); updateSend();
    try { const r = await api("/api/stop", { body: { chat_id: S.chatId, lab: true } }); toast(r.message || "Stopped."); }
    catch (e) { toast(e.message); }
    pollState();
  }
  // a message on its way: the input empties at once, and S.sending holds off another Enter until its chat is made
  // and its files are up (two Enters in a new chat made two chats, and sent the message twice). The attachments sent,
  // or null: not sent (a toast says why), and what was typed is back in the input
  async function startSend(model) {
    const typed = $("input").value;
    $("input").value = ""; autosize();
    S.sending = true;
    updateSend();
    let attachments = null;
    try {
      if (!S.chatId) {
        const rules = currentRules();
        try {
          const made = await api("/api/chats", { body: { model, project: SIDE.newProject, incognito: S.incognito } });
          S.chatId = made.id;
          showFolder(made.folder, made.own_folder);
        } catch (e) { toast(e.message); return null; }
        S.chatRules = rules;
        applyIncognito();
      }
      attachments = await takeAttachments();
      return attachments;
    } finally {
      S.sending = false;
      if (attachments === null && !$("input").value) { $("input").value = typed; autosize(); }
      updateSend();
    }
  }
  async function sendMessage(text) {
    text = text.trim();
    if (TYPED_STOP.test(text)) return hardStop();
    if (S.streaming) return steerMessage(text);   // attachments wait for the next message
    if (S.sending) return;   // the message before is still on its way
    if (!text && ATT.items.length) text = ATT.items.length > 1 ? "Look at the attached files." : "Look at the attached file.";
    if (!text) return;
    const model = $("model").value;
    if (!model) { openSettings(); return; }
    const extra = compareOn() ? S.compare.filter((k) => k !== model) : [];   // Compare, when Settings has it on
    if (extra.length) return sendCompare(text, [model, ...extra]);
    const attachments = await startSend(model);
    if (attachments === null) return;
    $("input").placeholder = askHint();
    showEmpty(false);
    for (const el of document.querySelectorAll(".msg-actions")) el.remove();   // the answer before is no longer the latest
    thread.append(userBubble(text, attachments));
    const chatId = S.chatId;
    const live = { reply: new Reply(model), box: h("div", { class: "turn-live" }), from: null, steers: [] };
    live.box.append(live.reply.el);   // this answer's own place: it goes on there while you are in another chat
    thread.append(live.box);
    S.live.set(chatId, live);
    toBottom();
    refreshComposer();
    renderChats();
    try {
      const res = await fetch("/api/chats/" + chatId + "/turn", {
        method: "POST",
        headers: { "X-Kelvin-Token": token, "Content-Type": "application/json" },
        body: JSON.stringify({ text, model, attachments }),
      });
      if (!res.ok) {
        let msg = "HTTP " + res.status;
        try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
        live.reply.error(msg);
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl);
          buf = buf.slice(nl + 1);
          if (!line.trim()) continue;
          const ev = JSON.parse(line);
          if (ev.type === "turn_start" && Number.isInteger(ev.from)) live.from = ev.from;
          if (ev.type === "steered") live.reply = steered(live, ev.texts || [], model);
          else handle(live.reply, ev, chatId);
        }
      }
    } catch (e) {
      live.reply.error("Lost the connection to the portal (" + e.message + "). The lab keeps its own log.");
    } finally {
      live.reply.finish();
      finishLive(chatId, live);
    }
  }
  function finishLive(chatId, live) {   // a chat's answer ended: here, or in a chat you left (then it says so)
    S.live.delete(chatId);
    S.asking.delete(chatId);
    const here = S.chatId === chatId;
    if (!here) {
      S.unread.add(chatId);
      toast("Kelvin answered in “" + titleOf(chatId) + "”", () => openChat(chatId));
    }
    refreshComposer();
    refreshChats();
    pollState();
    const left = live.steers.splice(0);   // written too late for this answer: the next message
    for (const s of left) s.el.remove();
    if (!left.length) return;
    if (here) sendMessage(left.map((s) => s.text).join("\n\n"));
    else toast("What you added in “" + titleOf(chatId) + "” came after its answer ended: send it there again.");
  }

  // ------------------------------------------------------------------ several models at once
  async function readEvents(res, onEvent) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (line.trim()) onEvent(JSON.parse(line));
      }
    }
  }
  class CompareView {
    constructor(keys) {
      this.lanes = keys.map((key, i) => {
        const reply = new Reply(key);
        const pick = h("button", { class: "btn small", type: "button" }, "Continue with this answer");
        pick.addEventListener("click", () => chooseLane(i));
        // which model answered: comparing them is the point here (every other answer says only Kelvin)
        const who = h("span", { class: "lane-model" }, S.labels[key] || key.split(":").pop());
        const el = h("div", { class: "lane" }, reply.el, h("div", { class: "lane-foot" }, who, pick));
        return { reply, el, pick };
      });
      this.el = h("div", { class: "compare", style: "--lanes:" + keys.length }, ...this.lanes.map((l) => l.el));
      this.setChosen(0, false);
    }
    setChosen(index, open) {
      this.lanes.forEach((l, k) => {
        l.el.classList.toggle("chosen", k === index);
        l.pick.textContent = k === index ? "Continuing with this" : "Continue with this answer";
        l.pick.disabled = k === index || !open;
      });
    }
  }
  async function chooseLane(index) {
    if (S.streaming) return;
    try {
      const r = await api("/api/chats/" + S.chatId + "/choose", { body: { index } });
      if (S.labels[r.model]) $("model").value = r.model;
      const chat = await api("/api/chats/" + S.chatId);
      renderChat(chat);
      const last = [...chat.messages].reverse().find((m) => m.role === "user");
      if (compareOn() && last && last.compare) { S.compare = last.compare.models.filter((k) => k !== r.model).slice(0, 2); compareLabel(); }
      toast("Continuing with " + (S.labels[r.model] || r.model) + ".");
    } catch (e) { toast(e.message); }
  }
  async function sendCompare(text, models) {
    const attachments = await startSend(models[0]);
    if (attachments === null) return;
    $("input").placeholder = askHint();
    showEmpty(false);
    for (const el of document.querySelectorAll(".msg-actions")) el.remove();
    thread.append(userBubble(text, attachments));
    const view = new CompareView(models);
    const chatId = S.chatId;
    const live = { reply: view.lanes[0].reply, box: h("div", { class: "turn-live" }), from: null, steers: [] };
    live.box.append(view.el);
    thread.append(live.box);
    S.live.set(chatId, live);
    toBottom();
    refreshComposer();
    renderChats();
    try {
      const res = await fetch("/api/chats/" + chatId + "/compare", {
        method: "POST",
        headers: { "X-Kelvin-Token": token, "Content-Type": "application/json" },
        body: JSON.stringify({ text, models, attachments }),
      });
      if (!res.ok) {
        let msg = "HTTP " + res.status;
        try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
        view.lanes[0].reply.error(msg);
        return;
      }
      await readEvents(res, (ev) => {
        const here = S.chatId === chatId;
        const stick = here && nearBottom();
        if (ev.lane !== undefined) {
          const lane = view.lanes[ev.lane];
          if (lane && ev.type === "lane_end") lane.reply.finish();
          else if (lane) handle(lane.reply, ev, chatId);
        } else if (ev.type === "compare_start") {
          if (Number.isInteger(ev.from)) live.from = ev.from;
          if (here) setTitle(ev.title, currentProject());
          refreshChats();
        }
        else if (ev.type === "memory") view.el.before(recallBox(ev.notes));
        else if (ev.type === "compare_end") { view.setChosen(ev.chosen, true); if (here) S.lastReply = view.lanes[ev.chosen].reply; }
        else if (ev.type === "error") view.lanes[0].reply.error(ev.message);
        if (stick) toBottom();
      });
    } catch (e) {
      view.lanes[0].reply.error("Lost the connection to the portal (" + e.message + ").");
    } finally {
      for (const l of view.lanes) l.reply.finish();
      finishLive(chatId, live);   // models being compared read nothing meanwhile: what you added is the next message
    }
  }
  // Compare stands under the message box only while Settings → Models has Compare models on (off by default). Off, a
  // message goes to the chosen model alone: the models ticked before are let go, and its menu closes
  const compareOn = () => !!(S.settings && S.settings.compare_models);
  function applyCompare() {
    const on = compareOn();
    $("compare-wrap").hidden = !on;
    if (on) return;
    $("compare-menu").hidden = true;
    $("compare-btn").setAttribute("aria-expanded", "false");
    if (S.compare.length) { S.compare = []; compareLabel(); }
  }
  async function setCompare(on) {   // its switch (swFlip): at once; a refusal turns it back, the reason beside it
    $("compare-status").textContent = "";
    try {
      const r = await api("/api/settings", { body: { compare_models: on ? "on" : "off" } });
      S.settings = r.settings;
      applyCompare();
    } catch (e) { $("compare-status").textContent = e.message; throw e; }
  }
  function compareLabel() {
    const n = S.compare.length;
    $("compare-label").textContent = n ? "Compare " + (n + 1) : "Compare";
    $("compare-btn").classList.toggle("on", n > 0);
    $("compare-btn").setAttribute("aria-pressed", String(n > 0));
  }
  function toggleCompareMenu() {
    const menu = $("compare-menu");
    if (!menu.hidden) { menu.hidden = true; $("compare-btn").setAttribute("aria-expanded", "false"); return; }
    const main = $("model").value;
    const others = (S.groups || []).flatMap((g) => g.models.map((m) => ({ key: m.key, label: m.label, provider: g.label })))
      .filter((m) => m.key !== main && modelAllowed(m.key, currentRules()));
    const items = others.map((m) => {
      const box = h("input", { type: "checkbox" });
      box.checked = S.compare.includes(m.key);
      box.addEventListener("change", () => {
        if (box.checked && S.compare.length >= 2) { box.checked = false; toast("Up to three models at once."); return; }
        S.compare = box.checked ? [...S.compare, m.key] : S.compare.filter((k) => k !== m.key);
        compareLabel();
      });
      return h("label", { class: "pane-menu-item" }, box, h("span", { class: "pm-name" }, m.label), h("span", { class: "pm-meta" }, m.provider));
    });
    const off = h("button", { class: "link soft", type: "button" }, "Ask one model");
    off.addEventListener("click", () => { S.compare = []; compareLabel(); menu.hidden = true; });
    menu.replaceChildren(
      h("div", { class: "pane-menu-note" }, (S.labels[main] || "The chosen model") + " answers, and so do the ones you tick here (up to two)."),
      ...(items.length ? items : [h("div", { class: "pane-menu-note" }, "Add another provider's API key in Settings to compare models.")]),
      h("div", { class: "pane-menu-note" }, "Models can't change the lab while comparing. ", off));
    menu.hidden = false;
    $("compare-btn").setAttribute("aria-expanded", "true");
  }

  // ------------------------------------------------------------------ Sign in with ChatGPT
  const GPT = { timer: 0 };
  function showChatGPT(st) {
    $("chatgpt-status").textContent = st.signed_in ? "Signed in as " + (st.email || "you") + (st.plan ? "" : " (plan usage not allowed)") : "Not signed in";
    $("chatgpt-signin").hidden = st.signed_in && st.plan;
    $("chatgpt-usage").hidden = $("chatgpt-signout").hidden = !st.signed_in;
  }
  async function loadChatGPT() {
    try { showChatGPT(await api("/api/chatgpt")); } catch (e) { $("chatgpt-status").textContent = e.message; }
  }
  async function signInChatGPT() {
    try { await api("/api/chatgpt/signin", { body: {} }); }
    catch (e) { $("chatgpt-status").textContent = e.message; return; }
    $("chatgpt-status").textContent = "Finish signing in in your browser…";
    clearInterval(GPT.timer);
    const until = Date.now() + 10 * 60 * 1000;
    GPT.timer = setInterval(async () => {
      if (Date.now() > until) { clearInterval(GPT.timer); loadChatGPT(); return; }
      let st;
      try { st = await api("/api/chatgpt"); } catch (e) { return; }
      if (!st.signed_in) return;
      clearInterval(GPT.timer);
      showChatGPT(st);
      try { const b = await api("/api/bootstrap"); applyModels(b.models.groups, b.default_model, b.auto_model, b.models.hub); } catch (e) { /* the list updates on reload */ }
      if (st.plan && !st.welcomed) { $("chatgpt-dialog").showModal(); api("/api/chatgpt/welcomed", { body: {} }).catch(() => {}); }
    }, 2000);
  }
  async function signOutChatGPT() {
    if (!(await askYes("Sign out of ChatGPT in Kelvin?\nChatGPT models will no longer be available here.", { ok: "Sign out" }))) return;
    try {
      const st = await api("/api/chatgpt/signout", { body: {} });
      showChatGPT(st);
      toast(st.revoked ? "Signed out of ChatGPT." : "Signed out here; OpenAI did not confirm the sign-out, so you can also disconnect Kelvin in ChatGPT's settings.");
      const b = await api("/api/bootstrap");
      applyModels(b.models.groups, b.default_model, b.auto_model, b.models.hub);
    } catch (e) { $("chatgpt-status").textContent = e.message; }
  }
  // ------------------------------------------------------------------ the Kelvin hub (accounts across labs)
  const HUB = { timer: 0, st: null };
  function hubSyncText(st) {
    if (st.syncing) return "Sharing now…";
    const s = st.sync;
    if (!s) return st.connected ? "Kelvin shares what changes every few minutes." : "";
    const at = (t) => new Date(t * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    if (s.error) return s.error + (st.synced_at ? " Last shared " + at(st.synced_at) + "." : "");
    const n = (k, one, many) => (k ? k + " " + (k === 1 ? one : many) : "");
    const sent = [n(s.sent.projects, "project", "projects"), n(s.sent.chats, "chat", "chats"), n(s.sent.runs, "run", "runs"),
      n(s.files, "run file", "run files")].filter(Boolean);
    const more = [n(s.removed, "item removed", "items removed"),
      n((s.refused || []).length, "item the hub didn't take", "items the hub didn't take"),
      n(s.older_runs, "older run stays here", "older runs stay here")].filter(Boolean);
    return "Up to date, " + at(s.at) + (sent.length ? ": sent " + sent.join(", ") : "") + (more.length ? "; " + more.join("; ") : "") + ".";
  }
  function hubSkipped(st) {   // what the hub didn't take, for the status line's tooltip
    return ((st.sync && st.sync.refused) || []).map((r) =>
      (r.chat ? "chat " + r.chat : r.project ? "project " + r.project : "run " + r.run + (r.file ? ", " + r.file : "")) + ": " + r.why).join("\n");
  }
  function showHub(st) {
    HUB.st = st;
    const a = st.account;
    let text;
    if (!st.connected) text = st.problem || "Not connected";
    else if (a) text = "Connected as " + a.name + " (" + a.email + "), sharing " + (a.shares_into ? "into " + a.shares_into : "with only you (no lab)");
    else text = "Connected to " + st.url + (st.problem ? ". " + st.problem : "");
    if (a && a.needs_consent) text += ". The hub's terms changed: agree to them there to go on sharing";
    $("hub-status").textContent = text;
    if (st.url) $("hub-url").value = st.url;
    $("hub-url").disabled = !!st.connected;
    $("hub-connect").hidden = !!st.connected;
    $("hub-actions").hidden = !st.connected;
    $("hub-account").href = a ? (a.needs_consent ? a.consent_url : a.account_url) : (st.url ? st.url + "/account" : "#");
    $("hub-sync-status").textContent = hubSyncText(st);
    $("hub-sync-status").title = hubSkipped(st);
  }
  async function loadHub() {
    $("hub-status").textContent = "…";
    try { showHub(await api("/api/hub")); } catch (e) { $("hub-status").textContent = e.message; }
  }
  async function connectHub() {
    const url = $("hub-url").value.trim();
    if (!url) { $("hub-status").textContent = "Enter the hub's address first."; $("hub-url").focus(); return; }
    try { await api("/api/hub/connect", { body: { url } }); }
    catch (e) { $("hub-status").textContent = e.message; return; }
    $("hub-status").textContent = "Finish connecting in your browser…";
    clearInterval(HUB.timer);
    const until = Date.now() + 10 * 60 * 1000;
    HUB.timer = setInterval(async () => {
      if (Date.now() > until) { clearInterval(HUB.timer); loadHub(); return; }
      let st;
      try { st = await api("/api/hub"); } catch (e) { return; }
      if (!st.connected) return;
      clearInterval(HUB.timer);
      showHub(st);
      toast("Kelvin is connected to the hub.");
    }, 2000);
  }
  async function disconnectHub() {
    if (!(await askYes("Disconnect Kelvin from the hub?\nIt stops sharing. What it shared stays on the hub until you delete it there.",
      { ok: "Disconnect" }))) return;
    try {
      const st = await api("/api/hub/disconnect", { body: {} });
      showHub(st);
      toast(st.revoked ? "Disconnected from the hub." : "Disconnected here. The hub could not be reached, so also disconnect this Kelvin on your hub account page.");
    } catch (e) { $("hub-status").textContent = e.message; }
  }
  async function syncHub() {
    $("hub-sync-status").textContent = "Sharing now…";
    try {
      const st = await api("/api/hub/sync", { body: {} });
      showHub(Object.assign({}, st, { account: st.connected ? (HUB.st || {}).account : null }));
    } catch (e) { $("hub-sync-status").textContent = e.message; }
  }

  function showPlanChip() {   // OpenAI asks apps to say when a request uses the ChatGPT plan
    $("plan-chip").hidden = !String($("model").value || "").startsWith("chatgpt:");
  }

  // ------------------------------------------------------------------ other model services
  const SVC = { presets: [] };
  async function loadServices() {
    let r;
    try { r = await api("/api/services"); } catch (e) { $("svc-hint").textContent = e.message; return; }
    if (!SVC.presets.length) {
      SVC.presets = r.presets;
      $("svc-preset").replaceChildren(...r.presets.map((p) => new Option(p.name, p.id)));
    }
    renderServices(r.services);
    servicePreset();
  }
  function servicePreset() {
    const p = SVC.presets.find((x) => x.id === $("svc-preset").value) || SVC.presets[0];
    if (!p) return;
    const custom = p.id === "custom";
    $("svc-name").hidden = !custom;
    $("svc-url").value = p.base_url;
    $("svc-url").readOnly = !custom;
    $("svc-hint").textContent = p.hint;
  }
  function renderServices(list) {
    $("service-list").replaceChildren(...list.map((s) => {
      const test = h("button", { class: "link soft", type: "button" }, "Test");
      const out = h("span", { class: "memory-meta" }, s.base_url + (s.models ? " · " + s.models : "") +
        (s.key ? "" : " · no API key"));
      test.addEventListener("click", async () => {
        out.textContent = "Asking " + s.name + "…";
        try { const r = await api("/api/services/" + s.id + "/test", { body: {} }); out.textContent = (r.ok ? "Works: " : "Problem: ") + r.message; }
        catch (e) { out.textContent = e.message; }
      });
      // do its models see pictures: Auto (Kelvin finds out, model by model: web/vision.py), or On or Off for the whole
      // service. It acts at once, so it is a choice of three (the kit's .seg) on a line of its own in the service's row,
      // as a skill's switch acts in its row: busy while it is sent, the newest choice sent once the one on its way is
      // answered, and a refusal turning it back, the reason under the list
      const pictures = h("div", { class: "seg svc-pics", role: "radiogroup", "aria-label": "Pictures for " + s.name });
      const mark = (v) => {
        for (const b of pictures.querySelectorAll(":scope > button")) {
          const on = b.dataset.v === v;
          b.classList.toggle("on", on);
          b.setAttribute("aria-checked", String(on));
          b.tabIndex = on ? 0 : -1;
        }
      };
      let want = s.pictures || "", sending = false;
      const choose = async (v) => {
        want = v;
        mark(v);
        if (sending) return;
        sending = true;
        pictures.setAttribute("aria-busy", "true");
        while (want !== (s.pictures || "")) {
          const v1 = want;
          try { await api("/api/services/" + s.id, { method: "PATCH", body: { pictures: v1 } }); s.pictures = v1; }
          catch (e) { $("svc-hint").textContent = e.message; want = s.pictures || ""; }
        }
        sending = false;
        pictures.removeAttribute("aria-busy");
        mark(want);
      };
      for (const [v, label, tip] of [["", "Auto", "Kelvin finds out which of its models see pictures"],
        ["on", "On", "Its models see pictures"], ["off", "Off", "Its models see no pictures"]]) {
        const b = h("button", { type: "button", role: "radio", title: tip }, label);
        b.dataset.v = v;
        b.addEventListener("click", () => choose(v));
        pictures.append(b);
      }
      segKeys(pictures, choose);
      mark(want);
      thumb(pictures);
      return h("li", { class: "memory-note" }, h("span", { class: "memory-kind" }, "service"),
        h("div", { class: "memory-body" }, h("span", {}, s.name), out,
          h("div", { class: "field-actions svc-pics-row" }, h("span", { class: "svc-pics-label", "aria-hidden": "true" }, "Pictures"), pictures)),
        h("span", { class: "card-actions" }, test),
        rowButton("trash", "Remove " + s.name, async () => {
          if (!(await askYes("Remove " + s.name + " and its key from Kelvin?", { ok: "Remove", danger: true }))) return;
          try { const r = await api("/api/services/" + s.id, { method: "DELETE" }); renderServices(r.services); refreshModels(r); }
          catch (e) { $("svc-hint").textContent = e.message; }
        }));
    }));
  }
  function refreshModels(r) {   // r: the models, with the default (Settings' choice, else Claude Opus)
    applyModels(r.models.groups, r.default_model, r.auto_model, r.models.hub);
    const waiting = $("settings-dialog").open ? settingsDiff() : [];
    const picked = { default_model: $("default-model").value, reviewer: $("reviewer").value };
    fillDefaultModel();
    fillModels($("reviewer"), S.groups, (S.settings && S.settings.reviewer) || "", true);
    $("reviewer").options[0].textContent = "Off";
    // the lists filled again are no change of the person's; a choice of theirs waiting for Save stays while it is offered
    settingsFilled(Object.keys(picked));
    for (const [k, v] of Object.entries(picked)) {
      if (waiting.includes(k) && [...settingField(k).options].some((o) => o.value === v)) settingField(k).value = v;
    }
    settingsPending();
  }
  async function addService() {
    const body = { preset: $("svc-preset").value, name: $("svc-name").value.trim(), base_url: $("svc-url").value.trim(),
      api_key: $("svc-key").value.trim(), models: $("svc-models").value.trim() };
    $("svc-hint").textContent = "Adding…";
    try {
      const r = await api("/api/services", { body });
      $("svc-key").value = $("svc-models").value = $("svc-name").value = "";
      renderServices(r.services);
      refreshModels(r);
      const added = r.services[r.services.length - 1];
      const group = r.models.groups.find((g) => g.provider === added.id);
      // r.names: the models named for it that it doesn't offer, with the nearest it does, or that it turned the key
      // down when asked for its list (added all the same)
      $("svc-hint").textContent = group && group.error ? "Added, but " + group.error
        : r.names ? "Added, but " + r.names
        : "Added " + added.name + (group ? " with " + group.models.length + " models." : ".");
    } catch (e) { $("svc-hint").textContent = e.message; }
  }

  // ------------------------------------------------------------------ phones
  const PHONE = { view: null };
  async function loadPhone() {
    try { renderPhone(await api("/api/phone")); }
    catch (e) { $("phone-status").textContent = e.message; }
  }
  function renderPhone(v) {
    PHONE.view = v;
    swSet($("phone-on"), v.on);
    const where = v.addresses.map((a) => a.url + (a.kind === "tailscale" ? " (Tailscale)" : a.kind === "network" ? " (this network)" : ""));
    $("phone-status").textContent = v.error ? v.error
      : v.listening ? "Phones can reach Kelvin at " + (where.join(", ") || "this computer's address, port " + v.port) + "."
      : v.on ? "Starting…" : "Off: phones can't connect directly.";
    $("firewall-row").hidden = !v.firewall;
    $("firewall-status").textContent = v.firewall === "allowed" ? "Windows Firewall lets phones reach Kelvin."
      : "Windows Firewall may stop phones from reaching Kelvin.";
    $("firewall-allow").hidden = v.firewall === "allowed";
    $("device-list").replaceChildren(...v.devices.map((d) => h("li", { class: "memory-note" },
      h("span", { class: "memory-kind" }, d.role),
      h("div", { class: "memory-body" }, h("span", {}, d.name),
        h("span", { class: "memory-meta" }, d.last_seen ? "last seen " + new Date(d.last_seen * 1000).toLocaleString() : "not used yet")),
      rowButton("trash", "Remove " + d.name, async () => {
        try { await api("/api/devices/" + d.id, { method: "DELETE" }); } catch (e) { toast(e.message); }
        loadPhone();
      }))));
  }
  async function switchPhone(on) {   // its switch (swFlip): a refusal turns it back, the reason beside it
    $("phone-status").textContent = on ? "Starting…" : "Stopping…";
    try { renderPhone(await api("/api/phone", { body: { on, port: PHONE.view ? PHONE.view.port : undefined } })); }
    catch (e) { $("phone-status").textContent = e.message; throw e; }
  }
  async function allowFirewall() {
    $("firewall-allow").disabled = true;
    $("firewall-status").textContent = "Windows asks for permission now…";
    try { renderPhone(await api("/api/phone/firewall", { body: {} })); toast("Phones can reach Kelvin through Windows Firewall now."); }
    catch (e) { $("firewall-status").textContent = e.message; }
    $("firewall-allow").disabled = false;
  }
  // ------------------------------------------------------------------ second opinions
  const VERDICTS = { go: ["ok", "Go"], caution: ["warn", "Caution"], stop: ["bad", "Stop"],
    agree: ["ok", "Agrees"], partly: ["warn", "Partly agrees"], disagree: ["bad", "Disagrees"] };
  const SURE = 0.7;   // as web/review.py: findings the reviewer is at least this sure of are passed on to Kelvin
  function reviewCard(r) {
    const [tone, word] = VERDICTS[r.verdict] || ["muted", "No verdict"];
    const who = S.labels[r.model] || r.label || r.model;
    const card = h("div", { class: "review " + tone },
      h("div", { class: "review-head" }, h("span", { class: "verdict-chip" }, word),
        h("span", { class: "review-who" }, "Second opinion" + (r.kind === "plan" ? " on the plan" : "") + " · " + who +
          (r.lookups ? " · looked up " + r.lookups + (r.lookups === 1 ? " record" : " records") : "") +
          (r.rechecked ? " · re-checked by " + r.rechecked : ""))));
    if (!r.findings) { card.append(h("div", { class: "md", html: md(r.text || "") })); return card; }   // words only
    const sure = r.findings.filter((f) => f.confidence >= SURE && !f.refuted);
    const unsure = r.findings.filter((f) => f.confidence < SURE || f.refuted);
    card.append(sure.length ? h("ul", { class: "findings" }, ...sure.map(findingItem))
      : h("div", { class: "review-checked" }, r.verdict === "go" || r.verdict === "agree" ? "No problems found." : "No problem it is sure of."));
    if (r.checked && r.checked.length) card.append(h("div", { class: "review-checked" }, "Checked and right: " + r.checked.join("; ") + "."));
    if (unsure.length) {
      card.append(h("details", { class: "review-unsure" },
        h("summary", {}, unsure.length + " less certain " + (unsure.length === 1 ? "note" : "notes") + ", not passed on to Kelvin"),
        h("ul", { class: "findings" }, ...unsure.map(findingItem))));
    }
    return card;
  }
  function findingItem(f) {
    const meta = Math.round(f.confidence * 100) + "% sure" + (f.recheck ? " · confirmed by " + f.recheck.label : "") +
      (f.refuted ? " · not confirmed by " + f.refuted.label + (f.refuted.evidence ? ": " + f.refuted.evidence : "") : "");
    return h("li", { class: "finding " + f.severity + (f.refuted ? " refuted" : "") }, h("span", { class: "sev" }, f.severity),
      h("span", { class: "finding-body" }, h("span", { class: "finding-problem" }, f.problem),
        f.evidence ? h("span", { class: "finding-evidence" }, f.evidence) : null, h("span", { class: "finding-meta" }, meta)));
  }
  function offerRecheck(reply) {   // the latest answer's second opinion: another model can check its findings
    const r = reply && reply.reviewData, card = reply && reply.reviewEl;
    if (!r || !card || r.kind !== "answer" || !(r.findings || []).length || r.rechecked || !card.isConnected) return;
    const label = "Re-check the findings with another model";
    const btn = h("button", { class: "link soft", type: "button", "aria-haspopup": "true" }, icon("check"), label);
    const menu = h("div", { class: "pane-menu review-menu", hidden: true });
    btn.addEventListener("click", () => {
      if (!menu.hidden) { menu.hidden = true; return; }
      const models = (S.groups || []).flatMap((g) => g.models.map((m) => ({ key: m.key, label: m.label, provider: g.label })))
        .filter((m) => m.key !== r.model && modelAllowed(m.key, S.chatRules));
      menu.replaceChildren(h("div", { class: "pane-menu-note" }, models.length ? "Which model should check these findings?"
        : "Add another model in Settings to re-check."), ...models.map((m) => {
        const item = h("button", { class: "pane-menu-item", type: "button" }, h("span", {}),
          h("span", { class: "pm-name" }, m.label), h("span", { class: "pm-meta" }, m.provider));
        item.addEventListener("click", async () => {
          menu.hidden = true;
          btn.disabled = true;
          btn.lastChild.textContent = "Re-checking with " + m.label + "…";
          try {
            const checked = await api("/api/chats/" + S.chatId + "/review", { body: { model: m.key, recheck: true } });
            const fresh = reviewCard(checked);
            card.replaceWith(fresh);
            reply.reviewEl = fresh; reply.reviewData = checked;
          } catch (e) { toast(e.message); btn.disabled = false; btn.lastChild.textContent = label; }
        });
        return item;
      }));
      menu.hidden = false;
    });
    card.append(h("div", { class: "msg-actions pane-menu-wrap review-recheck" }, btn, menu));
  }
  function recallBox(notes) {
    return h("details", { class: "recall" },
      h("summary", {}, icon("memory"), "Recalled " + notes.length + (notes.length === 1 ? " note" : " notes") + " from earlier work"),
      h("ul", {}, ...notes.map((n) => h("li", {}, h("span", { class: "memory-kind" }, n.kind),
        h("span", {}, n.text), h("span", { class: "memory-meta" }, n.scope === "global" ? "every chat" : "this project")))));
  }
  // (an answer has no Second opinion button under it any more, the user's call of 2026-11-03: Copy alone stays there.
  // Plans still get one from the reviewer chosen in Settings → Models, and an answer's second opinion given before
  // still shows, with its re-check: offerRecheck)

  function handle(reply, ev, chatId) {   // chatId: the chat answering (it may not be the one on screen)
    const here = chatId === undefined || chatId === S.chatId;
    const stick = here && nearBottom();
    switch (ev.type) {
      case "turn_start": reply.setModel(ev.model); if (here) setTitle(ev.title, currentProject()); refreshChats(); break;
      case "text": reply.addText(ev.delta); break;
      case "thinking": reply.addThinking(ev.delta); break;
      case "reset": reply.resetText(); break;
      case "tool_start": reply.toolStart(ev.id, ev.name, ev.input); break;
      case "approval":
        reply.approval(ev);
        if (here && !ev.auto) exitFull();   // its card is in this chat, and declines itself unless someone answers
        if (!here) {   // asked in a chat you left: say where, and take you there
          S.asking.add(chatId);
          renderChats();
          toast("Kelvin asks for your approval in “" + titleOf(chatId) + "”", () => openChat(chatId));
        }
        break;
      case "approval_closed": {
        const card = S.approvals.get(ev.id);
        if (card) card.close(card.choice === undefined ? "Closed" : card.choice ? "Approved" : "Declined", !!card.choice);
        if (chatId !== undefined && S.asking.delete(chatId)) renderChats();
        break;
      }
      case "tool_end": reply.toolEnd(ev.id, ev.content, ev.is_error, ev.run_id, ev.images, true); pollState(); break;
      case "run_progress": reply.runProgress(ev); break;
      case "computer_step": reply.computerStep(ev); break;
      case "analysis": if (here) openAnalysis(ev); break;   // Kelvin's analyze: its spec, result and steps
      case "note": reply.note(ev.text); break;
      case "memory": reply.memory(ev.notes); break;
      case "review_start": reply.reviewStart(ev); break;
      case "review": reply.review(ev); break;
      case "error": reply.error(ev.message); break;
      default: break;
    }
    if (stick) toBottom();
  }

  // ------------------------------------------------------------------ lab panel
  // switching: an instrument's switch on its way to the server (name -> the state asked for), drawn busy in that state;
  // refused: why, shown under its row until it is switched again. everyone: Activity's Mine | Everyone (lh.labEveryone).
  // switchedAt: when each switch's answer came (a poll sent before that says what was before). rows: each instrument's
  // row as drawn, by name, with what it draws (rowSig); order: the names in the list. pressed: when a pointer went down
  // on the list (0: none), later: a redraw waiting for it to be let go
  // rename: the instrument whose name is being changed ({name, value typed, msg, bad, busy})
  const OPEN = new Set(), SHOW_SETTINGS = new Set();
  const PANEL = { filter: "", activeOnly: store("lh.activeOnly") === "1", instruments: [], loose: new Map(),
                  everyone: store("lh.labEveryone") === "1", switching: new Map(), refused: new Map(), eventsShown: "",
                  switchedAt: new Map(), rows: new Map(), order: "", pressed: 0, later: false,
                  rename: null };
  const instId = (name) => "inst-" + String(name).replace(/[^\w-]/g, "_");
  // a choice of two as a radio group (.seg): the chosen side raised, checked and the one tab stop
  function segMark(seg, v) {
    for (const b of seg.querySelectorAll(":scope > button")) {
      const on = b.dataset.v === v;
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on ? 0 : -1;
    }
  }
  // its arrows choose the side before or after, at once: the thumb jumps there (still), as nothing moves for the keyboard
  function segKeys(seg, choose) {
    seg.addEventListener("keydown", (e) => {
      const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
      if (!step) return;
      e.preventDefault();
      const bs = [...seg.querySelectorAll(":scope > button:not([hidden])")];
      const b = bs[(bs.indexOf(seg.querySelector(":scope > button.on")) + step + bs.length) % bs.length];
      const t = seg.querySelector(":scope > .seg-thumb");
      if (t) { t.classList.add("still"); setTimeout(() => t.classList.remove("still"), 80); }
      choose(b.dataset.v);
      b.focus();
    });
  }

  function splitValue(text) {  // "9.42 fA" -> ["9.42", "fA"]; words stay whole
    const m = String(text).match(/^(-?[\d.]+(?:e[-+]?\d+)?)\s+(\S+)$/i);
    return m ? [m[1], m[2]] : [String(text), ""];
  }
  function kv(item) {
    let title = item.safe !== undefined ? "Safe value: " + item.safe : null;
    if (item.armed) title = "Set, but not reaching the sample while " + item.switch + " is off. Safe value: " + item.safe;
    return h("div", { class: "kv" + (item.away ? " away" : "") + (item.armed ? " armed" : "") + (item.error ? " err" : ""), title },
      h("span", { class: "k" }, item.label),
      h("span", { class: "v" }, item.armed ? h("span", { class: "tag-armed" }, "armed") : null, item.value));
  }
  // an instrument's headline reading (its value and unit) and its row's tip, which repeats it: the part of a row that
  // moves at every poll, written in place (renderInstruments)
  function instHead(inst) {
    const head = inst.readings.find((r) => r.name === inst.headline);
    const [hv, hu] = head ? splitValue(head.value) : ["", ""];
    const state = inst.state === "unavailable" ? "not connected" : inst.enabled === false ? "switched off"
      : inst.state === "error" ? "not responding" : inst.state === "active" ? inst.active + " active" : "safe";
    return { hv, hu, tip: inst.model + " · " + state + (head ? " · " + head.label + " " + head.value : "") };
  }
  // everything else a row draws: it is drawn again only when this changes, so a press on it is not lost to a redraw
  function rowSig(inst) {
    const open = OPEN.has(inst.name), rn = PANEL.rename && PANEL.rename.name === inst.name ? PANEL.rename : null;
    return JSON.stringify([open, SHOW_SETTINGS.has(inst.name), inst.enabled, PANEL.switching.get(inst.name), inst.state,
      inst.active, PANEL.refused.get(inst.name) || "", inst.why || "", inst.install || null, inst.model, inst.kind, inst.headline,
      open ? [inst.readings.filter((r) => r.name !== inst.headline), inst.drives, inst.settings, inst.address || "", rn ? [rn.busy, rn.msg, rn.bad] : null]
        : inst.drives.filter((d) => d.away)]);
  }
  function instrumentRow(inst) {
    const open = OPEN.has(inst.name);
    const { hv, hu, tip } = instHead(inst);
    const off = inst.enabled === false;
    const missing = inst.state === "unavailable";   // could not be connected (its package, the instrument itself)
    const chev = icon("chev"); chev.classList.add("chev");
    const count = inst.state === "active" ? h("span", { class: "inst-count" }, String(inst.active))
      : inst.state === "error" || missing ? h("span", { class: "inst-count err" }, "!") : h("span", { class: "inst-count none" });
    const id = instId(inst.name);
    const row = h("button", { class: "inst-row", type: "button", "aria-expanded": String(open), "aria-controls": open ? id + "-body" : null,
      "data-k": "row " + inst.name, title: tip },
      h("span", { class: "inst-icon" }, icon(ICONS[inst.kind] ? inst.kind : "instrument")),
      h("span", { class: "inst-id" }, inst.name),
      h("span", { class: "inst-model" }, inst.model),
      h("span", { class: "inst-headline" }, hv, hu ? h("span", { class: "hu" }, hu) : null),
      count, chev);
    row.addEventListener("click", () => {
      if (open) OPEN.delete(inst.name); else OPEN.add(inst.name);
      renderInstruments();
    });
    // on/off for this experiment: off, Kelvin sends it nothing at all until a person switches it on (lab.py). The one
    // switch (.switch): it turns at once and shows busy while the server saves; refused, it turns back and says why
    const asked = PANEL.switching.get(inst.name);
    const sw = h("button", { class: "switch inst-switch" + (asked === undefined ? "" : " busy"), type: "button", role: "switch",
      "aria-checked": String(asked === undefined ? !off : asked), "aria-busy": asked === undefined ? null : "true",
      "aria-label": "Use " + inst.name, "data-k": "switch " + inst.name,
      title: missing ? "Not connected: " + inst.why + " Switch it on to try again."
        : off ? "Switched off: Kelvin sends " + inst.name + " nothing. Switch it on to use it."
        : "On: Kelvin may use " + inst.name + ". Switch it off to keep Kelvin from sending it anything." },
      h("span", { class: "switch-thumb", "aria-hidden": "true" }));
    sw.addEventListener("click", (e) => switchInstrument(inst.name, off, sw, !e.detail));
    const item = h("div", { class: "inst " + inst.state + (open ? " open" : "") }, h("div", { class: "inst-head" }, row, sw));
    const on = inst.drives.filter((d) => d.away);
    const refused = PANEL.refused.get(inst.name);
    if (refused) item.append(h("div", { class: "inst-sub refused" }, "Not switched: " + refused));
    else if (missing) item.append(h("div", { class: "inst-sub" }, "Not connected: " + inst.why, ...installOffer(inst.install, pollState)));
    else if (off) item.append(h("div", { class: "inst-sub" }, "Off: Kelvin sends it nothing"));
    else if (inst.state === "error") item.append(h("div", { class: "inst-sub" }, "Not responding"));
    else if (on.length && !open) item.append(h("div", { class: "inst-sub" }, on.map((d) => d.label + " " + d.value).join(" · ")));
    if (!open) return item;
    const card = item;
    const body = h("div", { class: "inst-body", id: id + "-body" });
    if (missing) body.append(h("div", { class: "hint" }, "Not connected: plans that use it are refused. Switch it on once it is fixed."));
    else if (off) body.append(h("div", { class: "hint" }, "Off: no commands, readings or alarms."));
    const others = inst.readings.filter((r) => r.name !== inst.headline);
    if (others.length) body.append(h("div", { class: "group" }, h("div", { class: "group-title" }, "Readings"), ...others.map((r) => kv(r))));
    if (inst.drives.length) body.append(h("div", { class: "group" }, h("div", { class: "group-title" }, "Outputs"), ...inst.drives.map((d) => kv(d))));
    if (inst.settings.length) {   // a disclosure: one label, a chevron that turns, pressed while open
      const shown = SHOW_SETTINGS.has(inst.name);
      const toggle = h("button", { class: "link settings-toggle disclose", type: "button", "aria-expanded": String(shown),
        "aria-controls": id + "-settings", "data-k": "settings " + inst.name }, "Settings (" + inst.settings.length + ")", icon("chev"));
      toggle.addEventListener("click", () => {
        if (shown) SHOW_SETTINGS.delete(inst.name); else SHOW_SETTINGS.add(inst.name);
        renderInstruments();
      });
      body.append(h("div", { class: "group settings" }, toggle,
        h("div", { class: "inst-settings", id: id + "-settings", hidden: !shown }, ...(shown ? inst.settings.map((x) => kv(x)) : []))));
    }
    body.append(instTools(inst));
    card.append(body);
    return card;
  }
  // an opened instrument's address (two of one model told apart by it) and Rename…: a name of the person's own, which
  // the lab config, the scans, the sequence and the watches take (POST /api/instruments/{name}/rename); its past runs
  // keep the name they were measured with
  function instTools(inst) {
    const r = PANEL.rename && PANEL.rename.name === inst.name ? PANEL.rename : null;
    const where = h("code", { class: "inst-where", title: "Its address" }, inst.address || "");
    if (!r) {
      const b = h("button", { class: "link soft", type: "button", "data-k": "rename " + inst.name,
        title: "A name of your own: the lab config, your scans, the sequence and the watches take it" }, "Rename…");
      b.addEventListener("click", () => {
        PANEL.rename = { name: inst.name, value: inst.name, msg: "", bad: false, busy: false };
        renderInstruments();
        const f = $("lab-status").querySelector(".inst-rename");
        if (f) { f.focus(); f.select(); }
      });
      return h("div", { class: "inst-tools" }, where, b);
    }
    const input = h("input", { class: "kfield inst-rename", value: r.value, maxlength: "40", spellcheck: "false", autocomplete: "off",
      "aria-label": "New name for " + inst.name, "data-k": "rename-field " + inst.name, disabled: r.busy });
    input.addEventListener("input", () => { r.value = input.value; });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); renameInstrument(inst.name); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); PANEL.rename = null; renderInstruments(); }
    });
    const ok = h("button", { class: "btn small primary", type: "button", disabled: r.busy, "data-k": "rename-ok " + inst.name }, "Rename");
    ok.addEventListener("click", () => renameInstrument(inst.name));
    const no = h("button", { class: "btn small", type: "button", disabled: r.busy, "data-k": "rename-no " + inst.name }, "Cancel");
    no.addEventListener("click", () => { PANEL.rename = null; renderInstruments(); });
    for (const b of [ok, no]) b.addEventListener("mousedown", (e) => e.preventDefault());   // the box keeps the focus
    return h("div", { class: "inst-tools renaming" }, h("div", { class: "inst-rename-row" }, input, ok, no),
      r.msg ? h("p", { class: "hint" + (r.bad ? " bad" : ""), role: "status" }, r.msg) : null);
  }
  async function renameInstrument(name) {
    const r = PANEL.rename;
    if (!r || r.busy) return;
    const to = r.value.trim();
    if (!to || to === name) { PANEL.rename = null; renderInstruments(); return; }
    r.busy = true;
    r.msg = "";
    renderInstruments();
    let d;
    try { d = await api("/api/instruments/" + encodeURIComponent(name) + "/rename", { body: { to } }); }
    catch (e) {
      Object.assign(r, { busy: false, msg: e.message, bad: true });
      renderInstruments();
      const f = $("lab-status").querySelector(".inst-rename");
      if (f) f.focus();
      return;
    }
    PANEL.rename = null;
    if (OPEN.delete(name)) OPEN.add(d.name);
    if (SHOW_SETTINGS.delete(name)) SHOW_SETTINGS.add(d.name);
    if (Array.isArray(d.instruments)) PANEL.instruments = d.instruments;
    renderInstruments();
    toast((d.notes || []).join(" ") || name + " is " + d.name + " now.");
    edRenamed(name, d.name);
    emit("renamed", { from: name, to: d.name });   // the Scan's list, as the library has it now
    pollState();
  }
  async function switchInstrument(name, on, sw, still) {
    if (PANEL.switching.has(name)) return;
    PANEL.switching.set(name, on);
    PANEL.refused.delete(name);
    sw.classList.toggle("still", still);   // from the keyboard: it turns at once, without the slide
    sw.classList.add("busy");
    sw.setAttribute("aria-checked", String(on));
    sw.setAttribute("aria-busy", "true");
    try {
      const r = await api("/api/instruments/" + encodeURIComponent(name) + "/switch", { body: { on } });
      PANEL.instruments = r.instruments;
      PANEL.switchedAt.set(name, Date.now());
    } catch (e) {
      PANEL.refused.set(name, e.message);
      setTimeout(() => { if (PANEL.refused.get(name) === e.message) { PANEL.refused.delete(name); renderInstruments(); } }, 15000);
    }
    PANEL.switching.delete(name);
    renderInstruments();
  }

  function renderInstruments() {
    const all = PANEL.instruments;
    const q = PANEL.filter.trim().toLowerCase();
    const shown = all.filter((i) => (!PANEL.activeOnly || i.state === "active" || i.state === "error") &&
      (!q || (i.name + " " + i.model + " " + i.kind).toLowerCase().includes(q)));
    const active = all.filter((i) => i.state === "active").length;
    const errors = all.filter((i) => i.state === "error").length;
    const off = all.filter((i) => i.state === "off").length;
    const unconnected = all.filter((i) => i.state === "unavailable").length;
    $("inst-summary").textContent = all.length + " · " + active + " active" + (errors ? " · " + errors + " not responding" : "")
      + (unconnected ? " · " + unconnected + " not connected" : "") + (off ? " · " + off + " off" : "");
    $("seg-active").textContent = "Active" + (active + errors ? " (" + (active + errors) + ")" : "");
    segMark($("inst-seg"), PANEL.activeOnly ? "active" : "all");
    // a pointer down on the list: nothing in it is replaced until it is let go (holdInstruments), or its click is lost
    if (PANEL.pressed && Date.now() - PANEL.pressed < 5000) { PANEL.later = true; return; }
    const list = $("lab-status"), had = list.contains(document.activeElement) ? document.activeElement.dataset.k : null;
    // never under a new name being typed (drawn once the box lets go)
    PANEL.drawLater = !!had && had.startsWith("rename-field ") && !!PANEL.rename && !PANEL.rename.busy;
    if (PANEL.drawLater) return;
    if (!shown.length) {
      const text = !all.length ? "None yet." : PANEL.activeOnly && !q ? "Nothing is active." : "No instrument matches.";
      if (PANEL.order !== "\n" + text) list.replaceChildren(h("div", { class: "inst-empty" }, text));
      PANEL.order = "\n" + text;
      return;
    }
    // at every poll the readings move: a row's headline and tip are written in place, and a row is drawn again only when
    // something else in it changed; the row, switch or Settings that had the focus keeps it
    const names = new Set(all.map((i) => i.name));
    for (const name of PANEL.rows.keys()) if (!names.has(name)) PANEL.rows.delete(name);
    const rows = shown.map((inst) => {
      const sig = rowSig(inst), kept = PANEL.rows.get(inst.name);
      if (!kept || kept.sig !== sig) {
        const el = instrumentRow(inst);
        PANEL.rows.set(inst.name, { el, sig });
        return el;
      }
      const { hv, hu, tip } = instHead(inst), btn = kept.el.querySelector(".inst-row"), line = btn.querySelector(".inst-headline");
      if (btn.title !== tip) btn.title = tip;
      if (line.textContent !== hv + hu) line.replaceChildren(hv, ...(hu ? [h("span", { class: "hu" }, hu)] : []));
      return kept.el;
    });
    const order = shown.map((i) => i.name).join("\n");
    if (order !== PANEL.order) { PANEL.order = order; list.replaceChildren(...rows); }
    else rows.forEach((el, i) => { if (list.children[i] !== el) list.children[i].replaceWith(el); });
    const back = had && !list.contains(document.activeElement) && [...list.querySelectorAll("[data-k]")].find((el) => el.dataset.k === had);
    if (back) back.focus({ preventScroll: true });
  }
  // a press on the instrument list holds its redraws until the click that follows (a tap's comes a moment after its
  // pointerup), or 400 ms after it is let go; then the list is drawn as it is now
  function holdInstruments() {
    const list = $("lab-status");
    let letGo = 0;
    const release = () => {
      clearTimeout(letGo);
      PANEL.pressed = 0;
      if (PANEL.later) { PANEL.later = false; renderInstruments(); }
    };
    list.addEventListener("pointerdown", () => { clearTimeout(letGo); PANEL.pressed = Date.now(); });
    document.addEventListener("pointerup", () => { if (PANEL.pressed) letGo = setTimeout(release, 400); }, true);
    document.addEventListener("pointercancel", () => { if (PANEL.pressed) release(); }, true);
    list.addEventListener("click", () => { if (PANEL.pressed) setTimeout(release, 0); });
  }
  // a poll sent before an instrument's switch was answered says what it was before: that instrument keeps the switch's
  // answer (its on or off, its state) until a poll sent after it
  function freshInstruments(list, sent) {
    const was = new Map(PANEL.instruments.map((i) => [i.name, i]));
    return list.map((i) => (PANEL.switchedAt.get(i.name) > sent && was.has(i.name)
      ? { ...i, enabled: was.get(i.name).enabled, state: was.get(i.name).state } : i));
  }

  // ------------------------------------------------------------------ chats Kelvin continues later (wake_me)
  const WAKE = { done: undefined, count: -1 };
  function inTime(s) { return s < 90 ? s + " s" : s < 5400 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h"; }
  async function wakeState(s) {
    const answering = s.answering || (s.active ? [s.active] : []), woke = s.woke || {};
    const ids = new Set(answering.map((t) => t.chat_id));   // the chats answering now, from any window: marked
    if (ids.size !== S.answering.size || [...ids].some((id) => !S.answering.has(id))) { S.answering = ids; renderChats(); }
    const here = !!S.chatId && !S.streaming;
    const running = answering.some((t) => t.wake && t.chat_id === S.chatId);
    const mine = (s.wakes || []).find((w) => w.chat_id === S.chatId);
    if (answering.some((t) => t.wake) && !s.busy) {
      $("agent-state").textContent = "Continuing “" + (woke.title || "a chat") + "”";
      $("agent-state").classList.add("busy");
    }
    $("wake-text").textContent = running ? "Kelvin is continuing this chat: " + (woke.why || "")
      : mine ? "Kelvin continues this chat when " + mine.text + " (at most " + inTime(mine.left_s) + " from now)." : "";
    $("wake-stop").hidden = !running;
    $("wake-cancel").hidden = running || !mine;
    $("wake-bar").hidden = !(here && (running || mine));
    const finished = woke.done && woke.done !== WAKE.done;
    if (WAKE.done === undefined) WAKE.done = woke.done || 0;   // the page just opened: nothing new yet
    else if (finished) { WAKE.done = woke.done; refreshChats(); }
    if (here && (running || (finished && woke.chat_id === S.chatId))) {   // show what Kelvin does as it does it
      const id = S.chatId;
      let chat;
      try { chat = await api("/api/chats/" + id); } catch (e) { return; }
      if (S.chatId !== id || S.streaming || (running && chat.messages.length === WAKE.count)) return;
      WAKE.count = chat.messages.length;
      renderChat(chat);
    }
  }
  async function pollState() {
    let s;
    const sent = Date.now();
    try { s = await api("/api/state"); } catch (e) { return; }
    $("agent-state").textContent = s.busy ? String(s.busy).charAt(0).toUpperCase() + String(s.busy).slice(1) : "Idle";
    $("agent-state").classList.toggle("busy", !!s.busy);
    s.instruments = PANEL.instruments = freshInstruments(s.instruments, sent);   // Home reads the same
    renderInstruments();
    let lines = s.events.map((e) => ({ text: e, user: "" }));
    if (PANEL.everyone && !$("activity-who").hidden) { try { lines = (await api("/api/lab-log?n=25")).events; } catch (e) { /* keep this person's */ } }
    const said = JSON.stringify(lines);
    if (said !== PANEL.eventsShown) {   // redrawn only when it changed, so what is being read keeps its place
      PANEL.eventsShown = said;
      $("lab-events").replaceChildren(...lines.map((e) => {
        const m = e.text.match(/^(\d\d:\d\d:\d\d)\s+(.*)$/);
        const who = e.user ? e.user + ": " : "";
        return m ? h("li", {}, h("time", {}, m[1]), h("span", {}, who + m[2])) : h("li", {}, who + e.text);
      }));
      const newest = lines.length && String(lines[0].text).match(/^(\d\d:\d\d)/);   // newest first: its time on the head
      $("activity-summary").textContent = newest ? newest[1] : "";
      LAB_TABS.top = lines.length ? String(lines[0].text) : "";   // Activity's newest line, for its tab's dot
    }
    labTabSeen();   // seen if its part is on the screen now, else a dot on its tab
    wakeState(s);
    termState(s);   // the terminal first: an approval its own command needs is asked there
    // requests not already shown in a conversation (e.g. after a page reload)
    // requests no conversation shows (a queue started here, another agent's run): asked in the Lab panel, and kept
    // there until they are answered or closed, not redrawn away on the next poll
    const open = new Set(s.pending.map((p) => p.id));
    let fresh = 0;
    for (const p of s.pending) {
      if (PANEL.loose.has(p.id) || S.approvals.has(p.id) || TERM.asked.has(p.id)) continue;
      PANEL.loose.set(p.id, makeApproval(p));
      fresh += 1;
    }
    for (const [id, card] of PANEL.loose) {
      if (open.has(id)) continue;
      if (!card.decided) card.close("Closed", false);
      PANEL.loose.delete(id);
    }
    // changed in place, only as a request comes or goes: one not touched keeps its place, and a focused Approve its focus
    const pend = $("lab-pending"), cards = new Set([...PANEL.loose.values()].map((c) => c.el));
    for (const el of [...pend.children]) if (!cards.has(el)) el.remove();
    for (const el of cards) if (el.parentNode !== pend) pend.append(el);
    const badge = $("lab-badge");
    badge.textContent = String(s.pending.length);
    badge.hidden = !s.pending.length;
    // a request no conversation shows: the Lab panel to the screen, even in full screen; there already, its head rings
    if (fresh) Kelvin.paneShow("labpanel");
    showMode(s.approval_mode, s.approval_rule);
    renderAlerts(s.alarms, s.incident);
    renderWatches(s.watches || [], s.status, s.alarms);
    if (s.lab_memory && s.lab_memory !== LABMEM.stamp) { LABMEM.stamp = s.lab_memory; loadLabMemory(); }
    if (s.chats && s.chats !== S.chatsStamp) { S.chatsStamp = s.chats; refreshChats(); }   // a chat made or changed elsewhere (a phone, another window)
    teamState(s.team);
    notifySystem(s.notices);
    $("lab-name").textContent = s.lab;
    const problem = s.lab_info && s.lab_info.error;
    const missing = problem ? [] : (s.unavailable || []);
    $("lab-problem").hidden = !problem && !missing.length;
    if (problem || missing.length) {
      const text = problem ? problem
        : "Not connected: " + missing.map((m) => m.name + " (" + m.why + ")").join("; ") + " The rest of your lab is connected.";
      if ($("lab-problem").dataset.text !== text) {   // the install button keeps its state between polls
        $("lab-problem").dataset.text = text;
        const offers = problem ? installOffer(s.lab_info.install)
          : missing.filter((m) => m.install).flatMap((m) => installOffer(m.install, pollState));
        $("lab-problem").replaceChildren(h("span", {}, text), ...offers);
      }
    } else delete $("lab-problem").dataset.text;
    const unreviewed = s.unreviewed || [];
    $("lab-observe").hidden = !unreviewed.length;
    if (unreviewed.length) $("lab-observe-who").textContent = andList(unreviewed) + ".";
    const drafts = s.drafts || [];   // real instruments whose card nobody has checked yet
    $("lab-drafts").hidden = !drafts.length;
    if (drafts.length) {
      $("lab-drafts-who").textContent = andList(drafts) + ".";
    }
    showWhy(s);
    const busySetup = s.setup && SETUP_BUSY.has(s.setup.state);
    $("lab-setting-up").hidden = !busySetup;
    if (busySetup) $("lab-setting-up").textContent = "Setting up instruments: " + (s.setup.message || "") + "…";
    if (S.setupState && SETUP_BUSY.has(S.setupState) && s.setup && !busySetup && $("lab-dialog").open) refreshLabSetup();
    S.setupState = s.setup && s.setup.state;
    setupWait(s);
    if (s.live && s.live.status === "running") noticeRun(s.live.run_id, s.live.plan);  // e.g. a run an external agent started
    queueFromState(s.queue);
    showMounted(s.mounted);
    showHands(s.hands_on);
    PROT.busy = s.busy;
    S.last = s;
    emit("state", s);
  }
  // ------------------------------------------------------------------ alarms, watches, notifications
  function incidentCard(i) {   // what the last stop left behind: outputs on, runs or a queue cut short (runtime.py)
    const act = (label, action, primary) => {
      const b = h("button", { class: "btn small" + (primary ? " primary" : ""), type: "button" }, label);
      b.addEventListener("click", async () => {
        b.disabled = true;
        try { const r = await api("/api/incident", { body: { action } }); if (r.lines) toast(r.lines.join("; ")); }
        catch (e) { toast(e.message); }
        pollState();
      });
      return b;
    };
    const stop = i.kind === "stop";   // after a person's Stop: what it left on, and Bring to safe
    const on = (stop || i.outputs.length) && !i.safe.length;
    const at = i.at ? " at " + String(i.at).slice(11, 16) : "";
    return h("div", { class: "alert incident" }, icon("warn"), h("div", { class: "alert-body" },
      h("b", {}, stop ? "Stopped" + at + ". Check what it left on:" : "Kelvin restarted" + at + ". Check the lab before you go on:"),
      ...i.lines.map((l) => h("span", {}, l))),
      on ? act(stop ? "Bring to safe" : "Bring the outputs to safe", "safe", true) : null, act("Dismiss", "dismiss", false));
  }
  const hhmm = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
  function alarmWhen(a) {   // "02:13" or "3 times, last 02:20", and when it cleared
    return (a.count > 1 ? a.count + " times, last " + hhmm(a.last) : hhmm(a.raised))
      + (a.cleared ? " · cleared " + hhmm(a.cleared) + (a.cleared_text ? ": " + a.cleared_text : "") : "");
  }
  function renderAlerts(alarms, incident) {
    // every alarm until a person says Got it: one that cleared while nobody looked (overnight) stays, marked cleared
    const open = (alarms || []).filter((a) => !a.acknowledged);
    const box = $("alerts");
    box.hidden = !open.length && !incident;
    box.replaceChildren(...(incident ? [incidentCard(incident)] : []), ...open.map((a) => {
      const ok = h("button", { class: "btn small", type: "button" }, "Got it");
      ok.addEventListener("click", async () => { try { await api("/api/alarms/" + a.id + "/ack", { body: {} }); } catch (e) { /* gone */ } pollState(); });
      return h("div", { class: "alert" + (a.cleared ? " cleared" : "") }, icon("warn"), h("div", { class: "alert-body" }, h("b", {}, a.text),
        a.actions.length ? h("span", {}, "Kelvin " + a.actions.join("; ")) : null, h("span", { class: "alert-when" }, alarmWhen(a))), ok);
    }));
    document.title = (open.length || incident ? "(!) " : "") + document.title.replace(/^\(!\) /, "");
    renderAlarmHistory(alarms || []);
  }
  function renderAlarmHistory(alarms) {   // the Lab panel: each alarm with when it went up, cleared, and who said Got it
    $("alarm-history").hidden = !alarms.length;
    const sig = JSON.stringify(alarms.map((a) => [a.id, a.count, a.cleared, a.acknowledged]));
    if (sig === S.alarmsShown) return;   // redrawn only when it changes
    S.alarmsShown = sig;
    $("alarm-history-list").replaceChildren(...alarms.map((a) => h("li", {}, h("b", {}, a.text),
      h("span", {}, alarmWhen(a) + " · " + (a.acknowledged ? "Got it: " + (a.acknowledged_by || "a person")
        + (a.acknowledged_at ? " " + hhmm(a.acknowledged_at) : "") : "nobody has said Got it yet")))));
  }
  function notifySystem(notices) {
    if (!notices || !notices.length) return;
    const newest = notices[notices.length - 1].id;
    if (S.noticeSeen === undefined) { S.noticeSeen = newest; return; }   // not the ones from before the page opened
    for (const n of notices) {
      if (n.id <= S.noticeSeen) continue;
      const wanted = n.kind === "alarm" || document.hidden;
      if (wanted && "Notification" in window && Notification.permission === "granted") {
        try { new Notification(n.title, { body: n.text, tag: "kelvin-" + n.id }); } catch (e) { /* not supported here */ }
      }
    }
    S.noticeSeen = newest;
  }
  function renderWatches(watches, status, alarms) {
    // the alarms on, of every kind (a reading out of range, an instrument gone silent, an interlock, a stalled set
    // point; Alarm history below has each), as Home counts them: a Got it does not end one, only its clearing does
    const on = (alarms || []).filter((a) => a && !a.cleared).length, paused = watches.filter((w) => w.paused).length;
    $("watch-summary").textContent = [on ? on + (on === 1 ? " alarm" : " alarms") : "",
      watches.length ? watches.length - paused + " watched" : "", paused ? paused + " paused" : ""].filter(Boolean).join(" · ");
    $("watch-summary").classList.toggle("bad", on > 0);   // said on the part's head
    labTabMark("watches", on);   // and counted on its tab, seen whichever part is chosen
    const sig = JSON.stringify(watches);
    if (sig !== PANEL.watchesShown) {   // redrawn only when it changed, so a focused × keeps its focus
      PANEL.watchesShown = sig;
      $("watch-list").replaceChildren(...watchRows(watches));
    }
    const sel = $("watch-key");
    if (document.activeElement !== sel && status) {
      const keep = sel.value;
      const keys = [];
      for (const [inst, params] of Object.entries(status)) {
        for (const p of params) if (/^-?[\d.]/.test(String(p.value))) keys.push(inst + "." + p.name);
      }
      if (keys.join() !== S.watchKeys) {
        S.watchKeys = keys.join();
        sel.replaceChildren(...keys.map((k) => new Option(k, k)));
        if (keys.includes(keep)) sel.value = keep;
      }
    }
  }
  function watchRows(watches) {
    return (watches.length ? watches.map((w) => {
      const remove = w.source === "lab" ? h("span", { class: "tag", title: "From the lab config; change it there" }, "lab")
        : rowButton("x", "Stop watching", async () => { try { await api("/api/watches/" + w.id, { method: "DELETE" }); } catch (e) { toast(e.message); } pollState(); });
      return h("li", { class: "watch" + (w.alarm ? " alarm" : ""), title: w.paused ? "Paused: its instrument is switched off, so nothing is read" : null },
        h("span", { class: "dot" + (w.alarm ? " bad" : w.paused ? "" : " on") }),
        h("span", { class: "watch-rule" }, w.rule, w.paused ? h("span", { class: "watch-then" }, " (paused: instrument off)")
          : w.then.length ? h("span", { class: "watch-then" }, " → " + w.then.join(", ")) : null),
        h("span", { class: "watch-src" }, w.source === "agent" ? "Kelvin" : ""), remove);
    }) : [h("li", { class: "inst-empty" }, "None")]);
  }
  async function addWatch(e) {
    e.preventDefault();
    const body = { key: $("watch-key").value, above: $("watch-above").value, below: $("watch-below").value, for_s: $("watch-for").value,
      lab_wide: !$("watch-sample").checked };
    try {
      await api("/api/watches", { body });
      $("watch-form").reset();
      partForm("watch-form", false);
      toast("Watching " + body.key + ".");
      pollState();
    } catch (err) { toast(err.message); }
  }
  // Watches' and Memory's Add…: a disclosure for the form at the top of the part (its tab chosen to show it)
  function partForm(id, open) {
    const form = $(id), btn = document.querySelector('#labpanel [aria-controls="' + id + '"]');
    // shut from inside (Watch, Keep, Cancel, Enter): the focus goes back to its Add…, not to the page
    if (!open && btn && form.contains(document.activeElement)) btn.focus({ preventScroll: true });
    form.hidden = !open;
    if (btn) btn.setAttribute("aria-expanded", String(open));
    if (!open) return;
    chooseLabPart(form.closest(".lab-part").dataset.part);
    form.closest(".lab-part-body").scrollTop = 0;   // the form is at its top (no scrollIntoView: the panel itself never scrolls)
  }

  // ------------------------------------------------------------------ the lab's memory (labmemory.py)
  // What people and Kelvin keep about this lab, for every chat and everyone here: wiring, references, excitations,
  // lessons. Anyone at the computer adds, changes or deletes a note here; each says who wrote it and when.
  const LABMEM = { stamp: null, editing: null };
  async function loadLabMemory() {
    let r;
    try { r = await api("/api/lab-memory"); } catch (e) { return; }
    renderLabMemory(r);
  }
  function renderLabMemory(r) {
    LABMEM.stamp = r.stamp;
    const day = (iso) => { const d = new Date(iso); return isNaN(d) ? String(iso || "").slice(0, 10) : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }); };
    $("labmem-summary").textContent = r.notes.length ? r.notes.length + (r.notes.length === 1 ? " note" : " notes") : "";
    $("labmem-list").replaceChildren(...(r.notes.length ? r.notes.map((n) => {
      const said = n.who + ", " + day(n.at) + (n.edited ? " · changed by " + n.edited_who + ", " + day(n.edited.at) : "");
      const before = (n.before || []).map((b) => b.text).join("\n");
      return h("li", { class: "memory-note labmem-note" }, h("span", { class: "memory-kind" }, n.kind),
        h("div", { class: "memory-body" }, h("span", {}, n.text),
          h("span", { class: "memory-meta", title: before ? "Before:\n" + before : null },
            (n.instruments.length ? n.instruments.join(", ") + " · " : "") + said)),
        rowButton("pencil", "Change this note", () => editLabNote(n)),
        rowButton("trash", "Delete this note", async () => {
          if (!(await askYes("Delete this note from the lab's memory?\nEvery chat and everyone here loses it.",
            { ok: "Delete", danger: true }))) return;
          try { renderLabMemory(await api("/api/lab-memory/" + encodeURIComponent(n.id), { method: "DELETE" })); }
          catch (e) { toast(e.message); }
        }));
    }) : [h("li", { class: "inst-empty" }, "None")]));
  }
  function editLabNote(n) {   // null: a new note
    LABMEM.editing = n ? n.id : null;
    $("labmem-kind").value = n ? n.kind : "lesson";
    $("labmem-instruments").value = n ? n.instruments.join(", ") : "";
    $("labmem-text").value = n ? n.text : "";
    $("labmem-keep").textContent = n ? "Save" : "Keep";
    partForm("labmem-form", true);
    $("labmem-text").focus();
  }
  function closeLabNote() {
    $("labmem-form").reset();
    LABMEM.editing = null;
    partForm("labmem-form", false);
  }
  async function keepLabNote(e) {
    e.preventDefault();
    const body = { kind: $("labmem-kind").value, instruments: $("labmem-instruments").value, text: $("labmem-text").value };
    try {
      const r = await api("/api/lab-memory" + (LABMEM.editing ? "/" + encodeURIComponent(LABMEM.editing) : ""), { body });
      if (r.said && r.said.startsWith("already")) toast("That is in the lab's memory already.");
      closeLabNote();
      renderLabMemory(r);
    } catch (err) { toast(err.message); }
  }

  // ------------------------------------------------------------------ the lab Kelvin controls (people only)
  const KIND_NAME = { "source-meter": "smu", "lock-in": "lockin", "temperature-controller": "tc", "magnet-supply": "magnet",
    "laser": "laser", "laser-driver": "laser", "power-meter": "pm", "preamp": "preamp", "multimeter": "dmm",
    "nanovoltmeter": "nvm", "function-generator": "fg", "voltage-source": "vsrc", "vna": "vna" };
  const LAB = { cards: [], found: [], scripts: null, scriptsFolder: "" };   // scripts: the last survey, of scripts or LabVIEW

  function cardSelect(selected, placeholder) {
    const sel = h("select", { "aria-label": "Card" });
    sel.append(new Option(placeholder || "Choose its card…", ""));
    const kinds = new Map();
    for (const c of LAB.cards) {
      if (!kinds.has(c.kind)) { const g = document.createElement("optgroup"); g.label = c.kind; kinds.set(c.kind, g); sel.append(g); }
      kinds.get(c.kind).append(new Option(c.model, c.id));
    }
    sel.value = selected || "";
    return sel;
  }
  function showLabState(info, name) {
    const mine = info && info.kind === "file";
    $("lab-now").textContent = mine ? "Kelvin controls " + (name || "the lab") + "." : "No instruments yet.";
  }
  // ------------------------------------------------------------------ the user's own instrument cards
  const CARDS = { editing: null, version: false };   // version: the lab's version of one of Kelvin's cards
  async function openCards() {
    fillModels($("card-model"), S.groups, S.defaultModel || $("model").value);
    showCardEditor(null);
    $("cards-msg").textContent = "";
    $("cards-dialog").showModal();
    await loadCards();
  }
  async function loadCards() {
    let r;
    try { r = await api("/api/cards"); } catch (e) { $("cards-msg").textContent = e.message; return; }
    $("card-list").replaceChildren(...(r.cards.length ? r.cards.map((c) => {
      const reviewed = c.status === "lab-verified";
      const flip = h("button", { class: "link soft", type: "button" }, reviewed ? "Back to draft" : "Mark reviewed");
      flip.addEventListener("click", () => setCardStatus(c, reviewed ? "draft" : "lab-verified"));
      const edit = h("button", { class: "link soft", type: "button" }, "Edit");
      edit.addEventListener("click", () => showCardEditor(c.yaml, c.id, c.problems, c.warnings));
      const remove = async () => {
        if (!(await askYes(c.kelvins ? "Go back to Kelvin's own card " + c.id + "?\nYour lab's version is deleted."
          : "Delete the card " + (c.id || c.file) + "?", { ok: c.kelvins ? "Go back" : "Delete", danger: true }))) return;
        let r = {};
        try { r = await api("/api/cards/delete", { body: { id: c.id || c.file.replace(/\.yaml$/, "") } }); } catch (e) { $("cards-msg").textContent = e.message; }
        if (r.reconnect && r.reconnect.length) $("cards-msg").textContent = andList(r.reconnect) + (r.reconnect.length === 1 ? " uses" : " use") + " Kelvin's card again once the lab reconnects (Set up…, Save and connect).";
        loadCards();
      };
      const back = c.kelvins ? h("button", { class: "link soft", type: "button" }, "Back to Kelvin's card") : null;
      if (back) back.addEventListener("click", remove);
      return h("li", { class: "memory-note" + (c.problems.length ? " broken" : "") },
        h("span", { class: "memory-kind" + (reviewed ? " ok" : "") }, c.problems.length ? "broken" : reviewed ? "reviewed"
          : c.status === "draft" ? "draft" : c.status),
        h("div", { class: "memory-body" }, h("span", {}, c.model),
          h("span", { class: "memory-meta" }, (c.id || c.file) + (c.kelvins ? " · your lab's version of Kelvin's card" : "") +
            (c.problems.length ? " · " + c.problems[0] : ""))),
        h("span", { class: "card-actions" }, edit, c.problems.length ? null : flip, back),
        c.kelvins ? null : rowButton("trash", "Delete this card", remove));
    }) : [h("li", { class: "memory-empty" }, "No cards of your own yet.")]), ...(r.kelvins || []).map(kelvinsCard));
  }
  function kelvinsCard(c) {   // one of Kelvin's own cards that came as a draft and that this lab uses
    const reviewed = c.status === "lab-verified";
    const flip = h("button", { class: "link soft", type: "button" }, reviewed ? "Back to draft" : "Mark reviewed");
    flip.addEventListener("click", () => setCardStatus(c, reviewed ? "draft" : "lab-verified"));
    const view = h("button", { class: "link soft", type: "button" }, "View");
    view.addEventListener("click", () => {
      const open = item.querySelector(".know-body");
      if (open) { open.remove(); view.textContent = "View"; return; }
      item.querySelector(".memory-body").append(h("pre", { class: "know-body" }, c.yaml));
      view.textContent = "Hide";
    });
    const yours = h("button", { class: "link soft", type: "button", title: "Change it for your lab's instrument (another firmware, a wider range, a different command); Kelvin's card stays for other labs" }, "Make your version");
    yours.addEventListener("click", () => {
      showCardEditor(c.yaml, c.id, [], [], "Your lab's version of " + c.id + ": change what differs, then Save card.");
      CARDS.version = true;
    });
    const draft = c.status === "draft", kept = c.status === "lab-verified" && c.reviewable;
    const item = h("li", { class: "memory-note" },
      h("span", { class: "memory-kind" + (reviewed ? " ok" : "") }, draft ? "draft" : reviewed ? "reviewed" : "Kelvin's"),
      h("div", { class: "memory-body" }, h("span", {}, c.model),
        h("span", { class: "memory-meta" }, c.id + " · comes with Kelvin · used by " + c.instruments.join(", "))),
      h("span", { class: "card-actions" }, view, yours, draft || kept ? flip : null));
    return item;
  }
  function showCardEditor(text, editing, problems, warnings, notes) {
    CARDS.editing = editing || null;
    CARDS.version = false;   // set after this when the card is the lab's version of one of Kelvin's
    $("card-edit").hidden = text == null;
    $("card-check").hidden = $("card-save").hidden = text == null;
    if (text == null) return;
    $("card-yaml").value = text;
    $("card-editing").textContent = editing ? "editing " + editing : "new card";
    showCardProblems(problems || [], warnings || []);
    $("card-notes").textContent = notes || "";
    $("card-try-results").replaceChildren();
    $("card-yaml").focus();
  }
  function showCardProblems(problems, warnings) {
    $("card-problems").replaceChildren(
      ...problems.map((p) => h("li", { class: "bad" }, icon("warn"), h("span", {}, p))),
      ...warnings.map((w) => h("li", { class: "bad" }, icon("warn"), h("span", { class: "muted" }, w))),
      ...(problems.length ? [] : [h("li", { class: "good" }, icon("check"), h("span", {}, "The card is valid."))]));
  }
  async function tryCard() {
    const address = $("card-try-address").value.trim();
    if (!address) { $("cards-msg").textContent = "Give the address to try it on, as in the lab config."; return; }
    const btn = $("card-try");
    btn.disabled = true;
    $("cards-msg").textContent = "Trying it on " + address + "…";
    $("card-try-results").replaceChildren();
    try {
      const r = await api("/api/cards/try", { body: { yaml: $("card-yaml").value, address } });
      const rows = [];
      const idOk = r.matches !== false && !r.identity_error;
      rows.push(h("li", { class: idOk ? "good" : "bad" }, icon(idOk ? "check" : "warn"),
        h("span", {}, r.identity_error ? "Identity: " + r.identity_error
          : "Identity: " + (r.identity || "(empty)") + (r.matches === false ? " (does not match the card)" : ""))));
      for (const x of r.readings) {   // a reading not tried is said so, never shown as checked
        const text = x.error ? x.name + ": " + x.error : x.skipped ? x.name + ": not tried, " + x.skipped
          : x.name + " = " + x.value + (x.unit ? " " + x.unit : "");
        rows.push(h("li", { class: x.error ? "bad" : x.skipped ? "" : "good" }, icon(x.error ? "warn" : x.skipped ? "dots" : "check"),
          h("span", { class: x.skipped ? "muted" : "" }, text)));
      }
      const w = r.write_check;   // the card's set command, written with the value the setting had
      if (w) rows.push(h("li", { class: w.ok ? "good" : "bad" }, icon(w.ok ? "check" : "warn"),
        h("span", {}, "Set command " + w.name + ": " + (w.ok ? "takes " + w.sent + " (no change)" : w.error))));
      if (r.instrument_error) rows.push(h("li", { class: "bad" }, icon("warn"), h("span", {}, "Error queue: " + r.instrument_error)));
      $("card-try-results").replaceChildren(...rows);
      const bad = r.readings.filter((x) => x.error).length + (w && !w.ok ? 1 : 0);
      const untried = r.readings.filter((x) => x.skipped).length;
      $("cards-msg").textContent = bad ? bad + " checks failed: fix them in the card and try again."
        : (untried ? "Every reading tried answered (" + untried + " not tried)." : "Every reading answered.")
          + " Compare the values with the front panel.";
    } catch (e) { $("cards-msg").textContent = e.message; }
    finally { btn.disabled = false; }
  }
  async function draftCard() {
    const manual = $("card-manual").value.trim();
    if (manual.length < 200) { $("cards-msg").textContent = "Paste the manual's command reference or the maker's example scripts first."; return; }
    const btn = $("card-draft");
    btn.disabled = true;
    $("cards-msg").textContent = "Drafting with " + (S.labels[$("card-model").value] || "the model") + "… this can take a minute.";
    try {
      const r = await api("/api/cards/draft", { body: { manual, name: $("card-name").value.trim(), model: $("card-model").value } });
      showCardEditor(r.yaml, null, r.problems, r.warnings, r.notes);
      $("cards-msg").textContent = r.problems.length ? "Drafted, with problems to fix below." : "Drafted. Read it against the manual, then save.";
    } catch (e) { $("cards-msg").textContent = e.message; }
    finally { btn.disabled = false; }
  }
  async function checkCard() {
    try {
      const r = await api("/api/cards/check", { body: { yaml: $("card-yaml").value } });
      showCardProblems(r.problems, r.warnings);
      $("cards-msg").textContent = r.problems.length ? "Not valid yet." : "Valid: " + r.id;
    } catch (e) { $("cards-msg").textContent = e.message; }
  }
  async function saveCard() {
    try {
      const r = await api("/api/cards", { body: { yaml: $("card-yaml").value, replace: CARDS.editing, version: CARDS.version } });
      $("cards-msg").textContent = (r.kelvins ? "Saved your lab's version of " + r.id : "Saved " + r.id) + " (" + r.status + "). " +
        (r.reconnect && r.reconnect.length ? andList(r.reconnect) + " use" + (r.reconnect.length === 1 ? "s" : "") +
          " it once the lab reconnects (Set up…, Save and connect)." : "Add it to the lab under Instruments.");
      showCardEditor(null);
      await loadCards();
      if ($("lab-dialog").open) openLabSetup();
    } catch (e) { $("cards-msg").textContent = e.message; }
  }
  async function setCardStatus(c, status) {
    // what lets outputs reach real hardware starts on Cancel: an Enter alone never marks a card reviewed
    if (status === "lab-verified" && !(await askYes("Mark " + c.model + " as reviewed?\nYou have checked its commands, limits and safe values against the manual and the instrument. Its outputs can then reach real hardware, still within your lab limits and approvals.",
      { ok: "Mark reviewed", focus: "cancel" }))) return;
    try { await api("/api/cards/status", { body: { id: c.id, status } }); } catch (e) { $("cards-msg").textContent = e.message; return; }
    loadCards();
  }

  // ------------------------------------------------------------------ the schedule: the scans and the queue
  // Special Measure's window: the scans, ⇒, the queue (one above the other when the pane is narrow, ⇓ between them:
  // app.css). The queue: plans, steps and scripts lined up to run one after another; approved once, run in the
  // background, paused on a failure. ⇒ puts the selected scans after the selected queue item (last when none is), in
  // the order selected; a scan dragged onto the queue goes where it is dropped, a queue item dragged moves there. A
  // click selects an item (again: none); u / d move it, Delete removes it, Enter edits it: a scan in the scan editor,
  // bound to that item, a step in the step row, anything else as written.
  const QUEUE = { open: false, data: null, editing: null, sel: null, dragging: false, keys: null, units: new Map() };
  const QUEUE_STEP = "step: cool to 2 K\nset: { tc.T_set: 2 }\nstabilize: { read: tc.T, within: 0.02, for: 120, timeout: 7200 }\nwait: 600";
  const Q_STATE = { idle: "Idle", running: "Running", pausing: "Pausing after this item", paused: "Paused" };
  const Q_STATUS = { waiting: "waiting", running: "running…", done: "done", failed: "failed", stopped: "stopped", skipped: "skipped" };
  const Q_KIND = { plan: "scan", step: "step", script: "script" };
  const DRAG_SCANS = "text/x-kelvin-scans", DRAG_ITEM = "text/x-kelvin-item";

  function initSchedule() {
    $("queue-start").addEventListener("click", () => busyWhile(["start"], () => {
      $("queue-msg").textContent = "Checking every waiting item… then approve the queue once.";
      return queueOp({ op: "start" });
    }));
    $("queue-pause").addEventListener("click", () => queueOp({ op: "pause" }));
    $("queue-more").addEventListener("click", (e) => { e.stopPropagation(); queueMenu($("queue-more")); });
    $("queue-edit").addEventListener("click", () => { const i = queueSelected(); if (i) editQueued(i); });
    $("queue-up").addEventListener("click", () => moveSelected(-1));
    $("queue-down").addEventListener("click", () => moveSelected(1));
    $("queue-remove").addEventListener("click", () => { const i = queueSelected(); if (i) removeItem(i); });
    queueDrops($("queue-list"));
    $("step-kind").addEventListener("change", () => { stepShow(); stepChanged(); });
    for (const id of ["step-key", "step-value", "step-time", "step-read", "step-within", "step-for", "step-timeout"]) $(id).addEventListener("input", stepChanged);
    $("step-time-unit").addEventListener("change", stepChanged);
    for (const id of ["step-key", "step-read"]) $(id).addEventListener("focus", labKeys);   // the lab's settings and readings
    $("step-row").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.matches("input")) { e.preventDefault(); stepGo(); }
      else if (e.key === "Escape" && STEP.editing) { e.preventDefault(); stepReset(); }
    });
    $("step-add").addEventListener("click", stepGo);
    $("step-save").addEventListener("click", stepGo);
    $("step-cancel").addEventListener("click", stepReset);
    $("queue-text").placeholder = QUEUE_STEP;
    $("queue-paste-open").addEventListener("click", () => pasteOpen($("queue-paste").hidden));
    $("queue-paste").addEventListener("submit", (e) => { e.preventDefault(); queueAddOrSave(); });
    $("queue-paste-cancel").addEventListener("click", () => pasteOpen(false));
    $("queue-text").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); pasteOpen(false); $("queue-paste-open").focus(); } });
    for (const ev of ["dragend", "drop"]) document.addEventListener(ev, () => { if (QUEUE.dragging) dragDone(); });
  }
  // the queue is the Sequence (seq.js), the Runs pane's first view: what showed the queue opens the pane on it, with
  // a line to say there (message)
  function showQueue(message) {
    RUNS.view = "schedule";
    if (RUNS.open) showView("schedule"); else toggleRuns(true);
    seqSight(message);
  }
  function showScan(message) {   // the Scan: what was just started shows there as it runs
    RUNS.view = "scan";
    if (RUNS.open) showView("scan"); else toggleRuns(true);
    seqSight(message);
  }
  function toggleSeq(open, message) {   // the Sequence shown (true), the pane closed if it shows it (false), or either
    const shown = RUNS.open && RUNS.view === "schedule";
    if (open === undefined ? !shown : open) showQueue(message);
    else if (shown) toggleRuns(false);
  }
  function loadQueue() {
    api("/api/queue").then((q) => { QUEUE.data = q; renderQueue(); }, () => {});
  }
  async function queueOp(body, quiet) {
    try {
      const r = await api("/api/queue", { body });
      QUEUE.data = r;
      renderQueue();
      if (!quiet) $("queue-msg").textContent = (r.message || "").split("\n")[0];
      emit("queue", { queue: r, message: quiet ? "" : r.message || "" });   // the Sequence draws it now
      return r;
    } catch (e) {
      $("queue-msg").textContent = e.message;
      emit("queue", { message: e.message });
      return null;
    }
  }
  const queueItems = () => (QUEUE.data && QUEUE.data.items) || [];
  const queueSelected = () => queueItems().find((i) => i.id === QUEUE.sel) || null;
  // where ⇒ puts what it adds: after the selected item (its place in the queue), else null: last
  function insertAt() {
    const n = queueItems().findIndex((i) => i.id === QUEUE.sel);
    return n >= 0 ? n + 2 : null;
  }
  function followInsert(q, last) {   // the selection moves to the last one put after it, so the next goes after that
    const it = q && q.items && q.items[last - 1];
    if (it && QUEUE.sel) QUEUE.sel = it.id;
  }
  function pickItem(id) {
    QUEUE.sel = id || null;
    renderQueue();
    const row = id && $("queue-list").querySelector('li[data-id="' + CSS.escape(id) + '"]');
    if (row) row.focus();
  }
  function moveSelected(by) {
    const n = queueItems().findIndex((i) => i.id === QUEUE.sel) + 1;
    if (n && n + by >= 1 && n + by <= queueItems().length) queueOp({ op: "move", item: QUEUE.sel, position: n + by }, true);
  }
  async function removeItem(i) {   // out of the queue; the item after it (else before it) is selected in its place
    const items = queueItems(), k = items.findIndex((x) => x.id === i.id);
    const next = items[k + 1] || items[k - 1] || null;
    if (await queueOp({ op: "remove", item: i.id }, true)) pickItem(next && next.id);
  }
  // Paste a plan: any item as written (a plan, a step, a script item), after the selected item; and Edit on an item the
  // scan editor and the step row don't show, as written
  function pasteOpen(on, item) {
    $("queue-paste").hidden = !on;
    $("queue-paste-open").setAttribute("aria-expanded", String(!!on));
    QUEUE.editing = on && item ? item.id : null;
    const n = item ? queueItems().findIndex((i) => i.id === item.id) + 1 : 0;
    $("queue-add").textContent = QUEUE.editing ? "Save to #" + n : "Add";
    if (!on) { $("queue-text").value = ""; return; }
    if (item) $("queue-text").value = item.text;
    $("queue-text").focus();
  }
  async function queueAddOrSave() {
    const text = $("queue-text").value.trim();
    if (!text) { $("queue-text").focus(); return; }
    const at = insertAt();
    const r = QUEUE.editing
      ? await queueOp({ op: "edit", item: QUEUE.editing, text })
      : await queueOp({ op: "add", text, position: at });
    if (!r) return;
    if (!QUEUE.editing && at) followInsert(r, at);
    pasteOpen(false);
    renderQueue();
  }
  // Edit on a queue item: a scan in the scan editor, bound to it; a step in the step row; anything else as written. A
  // running item is fixed, and a restore step stays as Run again made it
  function editQueued(i) {
    if (i.status === "running" || i.restores) return;
    if (i.kind === "plan") openEditor(null, i.id);
    else if (i.kind === "step") editStep(i);
    else pasteOpen(true, i);
  }
  function queueItem(i, n, count) {
    const buttons = [];
    const act = (label, title, fn) => {
      const b = h("button", { type: "button", title, tabindex: "-1" }, label);
      b.addEventListener("click", fn);
      buttons.push(b);
    };
    const idle = i.status !== "running";
    if (idle && i.status !== "waiting") act("Again", "Put it back in line", () => queueOp({ op: "again", item: i.id }, true));
    if (i.run_id) act("Open", "Open its run", () => openRun(i.run_id, i.name));
    const st = i.status === "running" ? h("span", { class: "spinner" }) : i.status === "done" ? icon("check")
      : ["failed", "stopped"].includes(i.status) ? icon("warn") : h("span", { class: "muted" }, "·");
    const sub = [h("span", { class: "q-status" }, Q_STATUS[i.status] || i.status)];
    if (i.then_pause && i.status === "waiting") sub.push(h("span", { title: "The queue pauses after this item" }, "then pauses "));
    if (i.note) sub.push(h("span", {}, i.note + " "));
    if (i.message) sub.push(h("span", {}, "— " + i.message));
    const on = QUEUE.sel === i.id;
    const li = h("li", { class: "q-item " + i.status + (on ? " sel" : ""), role: "row", "aria-selected": String(on), tabindex: "-1",
        draggable: "true", "data-id": i.id, title: i.text },
      h("span", { class: "q-num", role: "gridcell" }, st), h("span", { class: "q-kind", role: "gridcell" }, Q_KIND[i.kind] || i.kind),
      h("span", { class: "q-name", role: "gridcell" }, n + ". " + i.name), h("span", { class: "q-acts", role: "gridcell" }, ...buttons),
      h("div", { class: "q-sub", role: "gridcell" }, ...sub));
    li.addEventListener("click", (e) => { if (!e.target.closest("button")) pickItem(on ? null : i.id); });
    li.addEventListener("keydown", (e) => queueKeys(e, li, i, n, count));
    li.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData(DRAG_ITEM, i.id);
      e.dataTransfer.effectAllowed = "move";
      QUEUE.dragging = true;
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", dragDone);
    return li;
  }
  // Special Measure's keys on a queue item: u / d move it, Delete removes it, Enter edits it; the arrows (Home, End)
  // select along the list, Space selects or not, Escape selects none, → reaches its buttons (← and Escape come back)
  function queueKeys(e, li, i, n, count) {
    if (e.target !== li) {
      const btns = [...li.querySelectorAll(".q-acts button")], k = btns.indexOf(e.target);
      const to = e.key === "ArrowRight" ? btns[k + 1] : e.key === "ArrowLeft" ? btns[k - 1] || li : e.key === "Escape" ? li : undefined;
      if (k < 0 || to === undefined) return;
      if (to) to.focus();
      e.preventDefault();
      return;
    }
    const rows = [...$("queue-list").querySelectorAll("li.q-item")], k = rows.indexOf(li);
    if (e.key === "u" && n > 1) queueOp({ op: "move", item: i.id, position: n - 1 }, true);
    else if (e.key === "d" && n < count) queueOp({ op: "move", item: i.id, position: n + 1 }, true);
    else if ((e.key === "Delete" || e.key === "Backspace") && i.status !== "running") removeItem(i);
    else if (e.key === "Enter") editQueued(i);
    else if (e.key === " ") pickItem(QUEUE.sel === i.id ? null : i.id);
    else if (e.key === "Escape" && QUEUE.sel) pickItem(null);
    else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      const next = e.key === "Home" ? rows[0] : e.key === "End" ? rows[rows.length - 1] : rows[k + (e.key === "ArrowDown" ? 1 : -1)];
      if (!next) return;
      pickItem(next.dataset.id);
    } else if (e.key === "ArrowRight" && li.querySelector(".q-acts button")) li.querySelector(".q-acts button").focus();
    else return;
    e.preventDefault();
  }
  function renderQueue() {
    const q = QUEUE.data;
    queueBadge(q);
    if (ED.item) edTitle();   // the editor names its item by its place, which moves
    if (!QUEUE.open || !q) return;
    const items = q.items || [];
    if (QUEUE.sel && !items.some((i) => i.id === QUEUE.sel)) QUEUE.sel = null;   // gone: ⇒ puts at the end again
    const pos = items.findIndex((i) => i.status === "running") + 1;
    $("queue-state").textContent = (Q_STATE[q.state] || q.state) + (pos ? " · item " + pos + " of " + items.length : "");
    $("queue-reason").textContent = $("queue-reason").title = q.reason && q.reason !== "finished" ? q.reason
      : q.waiting ? q.waiting + " waiting" : q.reason === "finished" ? "Finished" : "";
    $("queue-start").textContent = q.state === "paused" ? "Resume" : "Start";
    queueButtons();
    if (QUEUE.dragging) return;   // the list as it was until the drop: what is dragged stays where it was picked up
    const at = document.activeElement, focused = at && at.closest && at.closest("#queue-list li.q-item");
    $("queue-list").replaceChildren(...(items.length ? items.map((i, n) => queueItem(i, n + 1, items.length))
      : [h("li", { class: "muted small q-none" }, "Nothing queued.")]));
    const rows = [...$("queue-list").querySelectorAll("li.q-item")];
    const stop = rows.find((li) => li.dataset.id === QUEUE.sel) || rows[0];   // the list's one tab stop
    if (stop) stop.tabIndex = 0;
    const back = focused && rows.find((li) => li.dataset.id === focused.dataset.id);   // the item, wherever it went
    if (back) back.focus();
  }
  function queueButtons() {   // Start and Pause; Edit, ↑ ↓ and ✕ for the selected item
    const q = QUEUE.data || {}, items = q.items || [], i = queueSelected(), n = i ? items.indexOf(i) + 1 : 0;
    $("queue-start").disabled = q.state === "running" || q.state === "pausing" || !q.waiting || BUSY.has("start");
    $("queue-pause").disabled = q.state !== "running";
    $("queue-edit").disabled = !i || i.status === "running" || !!i.restores;
    $("queue-up").disabled = n <= 1;
    $("queue-down").disabled = !n || n >= items.length;
    $("queue-remove").disabled = !i || i.status === "running";
  }
  function queueBadge(q) {
    if (!q) return;
    const n = (q.waiting || 0) + (q.running ? 1 : 0);
    $("queue-count").textContent = String(n);
    $("queue-count").hidden = !n;
    $("open-schedule").classList.toggle("live", q.state === "running" || q.state === "pausing");
  }
  function queueFromState(q) {
    if (!q) return;
    const old = QUEUE.data;
    QUEUE.data = Object.assign({}, q);
    if (!old || old.changed !== q.changed || old.state !== q.state || old.reason !== q.reason) renderQueue();
    else queueBadge(q);
  }
  // the queue's smaller matters, in a menu: what a failure does, clearing what finished, keeping it, loading one kept
  function queueMenu(anchor) {
    const q = QUEUE.data || {}, onError = q.on_error || "pause", saved = q.saved || [];
    const items = [{ heading: "On a failure" },
      { label: "Pause", checked: onError === "pause", onClick: () => queueOp({ op: "on_error", value: "pause" }, true) },
      { label: "Go on", checked: onError === "continue", onClick: () => queueOp({ op: "on_error", value: "continue" }, true) },
      { sep: true },
      { label: "Clear finished", onClick: () => queueOp({ op: "clear" }, true) },
      { label: "Save as…", onClick: async () => {
        const name = ((await askText("Save the waiting items as", { value: "overnight", ok: "Save" })) || "").trim();
        if (name) queueOp({ op: "save", name });
      } }];
    if (saved.length) items.push({ heading: "Load" }, ...saved.map((s) => ({ label: s, onClick: () => queueOp({ op: "load", name: s }) })));
    popMenu(anchor, items);
  }

  // ---------- dragging onto the queue: scans go where they are dropped, an item moves there
  function dropIndex(list, y) {   // before the first row whose middle is below y; the rows' count: last
    const rows = [...list.querySelectorAll("li.q-item")];
    const k = rows.findIndex((li) => { const r = li.getBoundingClientRect(); return y < r.top + r.height / 2; });
    return k < 0 ? rows.length : k;
  }
  function dropMark(list, k) {   // the line where it would go; null: none
    const rows = [...list.querySelectorAll("li.q-item")];
    rows.forEach((li, j) => li.classList.toggle("drop-before", j === k));
    list.classList.toggle("drop-end", k !== null && k === rows.length);
  }
  function queueDrops(list) {
    const ours = (e) => [...e.dataTransfer.types].some((t) => t === DRAG_SCANS || t === DRAG_ITEM);
    list.addEventListener("dragover", (e) => {
      if (!ours(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = [...e.dataTransfer.types].includes(DRAG_ITEM) ? "move" : "copy";
      dropMark(list, dropIndex(list, e.clientY));
    });
    list.addEventListener("dragleave", (e) => { if (!list.contains(e.relatedTarget)) dropMark(list, null); });
    list.addEventListener("drop", (e) => {
      if (!ours(e)) return;
      e.preventDefault();
      const k = dropIndex(list, e.clientY), item = e.dataTransfer.getData(DRAG_ITEM);
      let ids = [];
      try { ids = JSON.parse(e.dataTransfer.getData(DRAG_SCANS) || "[]"); } catch (err) { ids = []; }
      dragDone();
      if (item) moveTo(item, k);
      else if (ids.length) queuePicked(k + 1, ids);
    });
  }
  function dragDone() {
    QUEUE.dragging = false;
    for (const el of document.querySelectorAll(".dragging")) el.classList.remove("dragging");
    dropMark($("queue-list"), null);
    renderQueue();
  }
  function moveTo(id, k) {   // the item dragged to before row k (0-based; the rows' count: last)
    const from = queueItems().findIndex((i) => i.id === id);
    if (from < 0) return;
    const position = (k > from ? k - 1 : k) + 1;
    if (position !== from + 1) queueOp({ op: "move", item: id, position }, true);
  }

  // ---------- the step row: Special Measure's command box, typed and checked
  // Set (a parameter of the lab and its value), Wait (a time) or Wait until settled (a reading, within, for, a timeout),
  // written as the queue's step (scanqueue.py, as QUEUE_STEP) and checked by Kelvin as it is added; its ⇒ puts it
  // after the selected item. Edit on a step the row can show fills it, and Save puts it back
  const STEP = { editing: null };   // the step item the row edits now
  const STEP_FIELDS = { set: "step-set", wait: "step-wait", settle: "step-settle" };
  const yamlWord = (s) => (/^[\w.+-]+$/.test(s) ? s : JSON.stringify(s));   // a number or a word as typed, else quoted
  const yamlName = (s) => (/^[A-Za-z][\w .+\-\/()%°µ]*$/.test(s) && !/\s$/.test(s) ? s : JSON.stringify(s));
  const stepNumber = (s) => { const t = String(s).trim(); return t !== "" && isFinite(Number(t)) && Number(t) > 0 ? t : null; };
  function stepShow() {
    for (const [kind, id] of Object.entries(STEP_FIELDS)) $(id).hidden = $("step-kind").value !== kind;
  }
  // the step the row makes, in the queue's step format: what it does in words, then the value it sets, the seconds it
  // waits, or the reading it waits on to settle; null while a field it needs is empty, or not a number
  function stepText() {
    const v = (id) => $(id).value.trim(), kind = $("step-kind").value;
    if (kind === "set") {
      const key = v("step-key"), value = v("step-value"), unit = QUEUE.units.get(key) || "";
      if (!key || !value) return null;
      const said = "set " + key + " to " + value + (unit && isFinite(Number(value)) ? " " + unit : "");
      return "step: " + yamlName(said) + "\nset: { " + yamlWord(key) + ": " + yamlWord(value) + " }";
    }
    if (kind === "wait") {
      const t = stepNumber(v("step-time")), unit = $("step-time-unit");
      if (!t) return null;
      return "step: " + yamlName("wait " + t + " " + unit.selectedOptions[0].textContent)
        + "\nwait: " + String(Number((Number(t) * Number(unit.value)).toPrecision(12)));
    }
    const read = v("step-read"), within = stepNumber(v("step-within"));
    const more = [["for", v("step-for")], ["timeout", v("step-timeout")]];
    if (!read || !within || more.some(([, x]) => x && !stepNumber(x))) return null;
    const keys = ["read: " + yamlWord(read), "within: " + within].concat(more.filter(([, x]) => x).map(([k, x]) => k + ": " + x));
    return "step: " + yamlName("wait until " + read + " settles") + "\nstabilize: { " + keys.join(", ") + " }";
  }
  function stepSay(text, bad) {
    $("step-msg").textContent = text;
    $("step-msg").classList.toggle("bad", !!bad);
  }
  function stepChanged() {
    const ok = !!stepText();
    $("step-add").disabled = $("step-save").disabled = !ok;
    $("step-unit").textContent = QUEUE.units.get($("step-key").value.trim()) || "";
    $("step-within-unit").textContent = QUEUE.units.get($("step-read").value.trim()) || "";
    stepSay("");
  }
  async function stepGo() {
    const text = stepText();
    if (!text) return;
    const at = STEP.editing ? null : insertAt();
    let r;
    try { r = await api("/api/queue", { body: STEP.editing ? { op: "edit", item: STEP.editing, text } : { op: "add", text, position: at } }); }
    catch (e) { stepSay(e.message, true); return; }
    QUEUE.data = r;
    if (STEP.editing) stepReset();
    else if (at) followInsert(r, at);
    renderQueue();
    emit("queue", { queue: r });   // the Sequence draws it now
  }
  function stepReset() {
    STEP.editing = null;
    for (const id of ["step-key", "step-value", "step-time", "step-read", "step-within", "step-for", "step-timeout"]) $(id).value = "";
    $("step-add").hidden = false;
    $("step-save").hidden = $("step-cancel").hidden = true;
    stepChanged();
  }
  // a step's mapping as the row's fields: one value set, a wait, or a settle; null when it does more than one of them,
  // or more than the row shows (a note, a settle's every or near): it opens as written
  function stepFields(f) {
    const keys = Object.keys(f).filter((k) => k !== "step"), one = f[keys[0]];
    const plain = (x) => x !== null && typeof x !== "object";
    if (keys.length !== 1 || one == null) return null;
    if (keys[0] === "set" && typeof one === "object" && Object.keys(one).length === 1 && plain(Object.values(one)[0])) {
      const [key, value] = Object.entries(one)[0];
      return { kind: "set", key, value: edShow(value) };
    }
    if (keys[0] === "wait" && plain(one) && stepNumber(one)) return { kind: "wait", seconds: Number(one) };
    if (keys[0] === "stabilize" && typeof one === "object" && "read" in one && Object.values(one).every(plain)
        && Object.keys(one).every((k) => ["read", "within", "for", "timeout"].includes(k))) {
      return { kind: "settle", read: String(one.read), within: edShow(one.within), for: edShow(one.for), timeout: edShow(one.timeout) };
    }
    return null;
  }
  async function editStep(i) {
    let r;
    try { r = await api("/api/queue", { body: { op: "fields", item: i.id } }); } catch (e) { $("queue-msg").textContent = e.message; return; }
    const f = stepFields(r.fields || {});
    if (!f) { pasteOpen(true, i); return; }
    labKeys();
    stepReset();
    $("step-kind").value = f.kind;
    if (f.kind === "set") { $("step-key").value = f.key; $("step-value").value = f.value; }
    else if (f.kind === "wait") {
      const unit = f.seconds % 3600 === 0 ? 3600 : f.seconds % 60 === 0 ? 60 : 1;
      $("step-time-unit").value = String(unit);
      $("step-time").value = String(f.seconds / unit);
    } else {
      $("step-read").value = f.read;
      $("step-within").value = f.within; $("step-for").value = f.for; $("step-timeout").value = f.timeout;
    }
    STEP.editing = i.id;
    $("step-add").hidden = true;
    $("step-save").hidden = $("step-cancel").hidden = false;
    $("step-save").textContent = "Save to #" + (queueItems().findIndex((x) => x.id === i.id) + 1);
    stepShow();
    stepChanged();
    $("step-row").querySelector(".step-f:not([hidden]) input").focus();
  }
  // the lab's settings and readings for the step row's lists, as the scan editor has them (POST /api/library op parts),
  // asked for once each time the schedule is shown
  function labKeys() {
    if (QUEUE.keys) return QUEUE.keys;
    QUEUE.keys = api("/api/library", { body: { op: "parts", id: null } }).then((d) => {
      const lab = d.lab || { set: [], read: [] };
      QUEUE.units = new Map(lab.set.concat(lab.read).map((p) => [p.key, p.unit || ""]));
      $("step-set-keys").replaceChildren(...lab.set.filter((p) => !p.action).map((p) => h("option", { value: p.key }, p.unit || "")));
      $("step-read-keys").replaceChildren(...lab.read.map((p) => h("option", { value: p.key }, p.unit || "")));
      stepChanged();
    }, () => { QUEUE.keys = null; });
    return QUEUE.keys;
  }

  // ------------------------------------------------------------------ scans and scripts, kept to run again
  // Each scan Kelvin runs and each script a person imports (GET/POST /api/library): Run puts it first in the queue,
  // which pauses after it; Queue puts it last. A script runs with the settings last used and those changed in Show.
  // A row is selected with a click (Ctrl adds one, Shift the rows from the last one picked) or the keyboard (Space,
  // the arrows); ⇒ adds the selected to the queue in the order they were selected, and Edit (Enter) edits a scan, or
  // shows a script's settings. The list is a grid with one tab stop (at: the row that has it); a row the filter hides
  // leaves the selection.
  const SCANS = { data: null, shown: new Set(), params: new Map(), renaming: null, fresh: null, empty: "", sel: [], anchor: null, at: null };
  const SCAN_ROWS = "#scans-view li.lib[data-id]";   // the scans and scripts (a lab experiment's row isn't one)

  async function loadScans() {   // a fresh visit: the last import's check and message go
    $("scans-check").hidden = true;
    scansSay("");
    SCANS.fresh = null;
    QUEUE.keys = null;   // the step row asks for the lab's settings again
    try { SCANS.data = await api("/api/library"); } catch (e) { scansSay(e.message, true); }
    renderScans();
    loadExperiments();
  }

  // ---------- the lab's experiments: translated from its VIs, kept with every version (Runs → Lab experiments)
  const EXP = { data: null, open: null };

  async function loadExperiments() {
    try { EXP.data = await api("/api/experiments"); } catch (e) { EXP.data = null; }
    renderExperiments();
  }
  function renderExperiments() {
    const box = $("scans-experiments"), d = EXP.data || {};
    const rows = (d.experiments || []).map(experimentRow).concat((d.drafts || []).map(draftRow));
    box.hidden = !rows.length;
    box.querySelector("ul").replaceChildren(...rows);
  }
  function experimentRow(e) {
    const key = "exp:" + e.name;
    const pick = h("select", { class: "exp-action", "aria-label": "Action of " + e.name },
      ...(e.actions.length ? e.actions : [{ name: "", main: true }]).map((a) =>
        h("option", { value: a.name }, a.name ? a.name + (a.main ? " (main)" : "") : "the whole experiment")));
    const act = (label, title, queue) => {   // a version whose file changed on disk never runs: no Run, no Queue
      const b = h("button", { type: "button", title: e.standing === "changed" ? "Its file changed on disk: save it as a new version" : title,
        "data-busy": key, disabled: BUSY.has(key) || e.standing === "changed" }, label);
      b.addEventListener("click", () => runExperiment(e, pick.value, queue));
      return b;
    };
    const versions = h("button", { type: "button", title: "Its versions, code and checks" }, e.standing === "new" ? "Approve…" : "Versions");
    versions.addEventListener("click", () => openVersions(e.name));
    const tag = e.standing === "new" ? h("span", { class: "tag warn", title: "A new version: its first run, or Approve…, asks a person" }, "new")
      : e.standing === "changed" ? h("span", { class: "tag warn", title: "Its file changed on disk: it won't run" }, "changed") : null;
    return h("li", { class: "q-item lib" },
      h("span", { class: "lib-name" }, h("span", { class: "q-name" }, e.name), tag,
        e.vi ? h("span", { class: "tag", title: "From " + e.vi }, "LabVIEW") : null),
      h("span", { class: "q-acts" }, pick, act("Run", "First in the queue, which pauses after it", false),
        act("Queue", "Last in the queue", true), versions),
      h("span", { class: "q-sub" }, "version " + e.version + (e.approved_by ? " · approved by " + e.approved_by : "")),
      h("span", { class: "lib-used" }, Object.keys(e.live || {}).length ? "live: " + Object.keys(e.live).join(", ") : ""));
  }
  function draftRow(dr) {
    const review = h("button", { type: "button", title: "Read it, then keep it or not" }, "Review");
    review.addEventListener("click", () => openReview(dr.id));
    return h("li", { class: "q-item lib" },
      h("span", { class: "lib-name" }, h("span", { class: "q-name" }, dr.name || dr.vi || dr.id), h("span", { class: "tag" }, "draft")),
      h("span", { class: "q-acts" }, review),
      h("span", { class: "q-sub" }, (dr.vi ? "from " + dr.vi + " · " : "") + (dr.ok_to_keep ? "nothing blocks keeping it" : "something blocks it")),
      h("span", { class: "lib-used" }, dr.by || ""));
  }
  function runExperiment(e, action, queue) {
    return busyWhile(["exp:" + e.name].concat(queue ? [] : ["start"]), async () => {
      scansSay(queue ? "" : "Starting…");   // the queue's approval (or its own) is asked first
      let r;
      try { r = await api("/api/experiments/" + encodeURIComponent(e.name) + "/run", { body: { action, queue } }); }
      catch (err) { scansSay(err.message, true); return; }
      queueFromState(r.queue);
      renderQueue();
      scansSay(r.message || "");
      if (!queue) { showScan((r.message || "").split("\n")[0]); pollState(); }
    });
  }
  function translateButton(v) {
    const b = h("button", { type: "button", class: "btn small", title: "The AI writes it as one of the lab's experiments; Kelvin checks it on the simulated lab; you read it, then keep it or not" }, "Translate");
    b.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); openTranslate(v); });
    return b;
  }
  function expDialog(title) {
    EXP.shown = (EXP.shown || 0) + 1;   // what the dialog shows now: an answer for what it showed before isn't put in it
    $("exp-title").textContent = title;
    $("exp-msg").textContent = "";
    $("exp-keep").hidden = $("exp-approve").hidden = $("exp-name-row").hidden = true;
    if (!$("exp-dialog").open) $("exp-dialog").showModal();
    return $("exp-body");
  }
  // a VI translated (minutes) under v.as, the name given (else the VI's): its draft shown to read and keep, or, when the
  // person closed the dialog meanwhile or opened another thing in it, a note that opens the draft when clicked. The
  // Sequence's experiments list it meanwhile (kelvin:translate), and why after a failure, and its draft once done
  // (kelvin:library) with its Review, so it is a press away there however long it took and after a reload.
  // from: the dialog it was asked in (New function): a refusal within a second (a mistyped path) goes back to it, still
  // open, as its words; closed in that second (Cancel, Esc), the translation goes on quietly, with no dialog of its own:
  // its row among the experiments and a note when it ends. Else whether it was translated
  async function openTranslate(v, from) {
    const asked = api("/api/labview/translate", { body: v.as ? { path: v.path, name: v.as } : { path: v.path } });
    let quiet = false;
    if (from) {
      const early = await Promise.race([asked.then(() => null, (e) => e), new Promise((r) => setTimeout(() => r(null), 1000))]);
      if (early && from.open) return early.message;
      if (from.open) from.close(); else quiet = true;
    }
    let body = null, mine = -1;
    if (!quiet) {
      body = expDialog("Translate " + v.name);
      mine = EXP.shown;
      body.replaceChildren(h("div", { class: "review pending" }, h("span", { class: "spinner" }),
        "Translating " + v.name + ": the AI writes it, and Kelvin checks each attempt on the simulated lab (a few minutes). " +
        "You can close this: the translation goes on, and its draft waits in Sequence → Experiments."));
      EXP.open = null;
    }
    emit("translate", { path: v.path, name: v.as || v.name, state: "running" });
    const here = () => !quiet && EXP.shown === mine && $("exp-dialog").open;
    let d;
    try { d = await asked; }
    catch (e) {
      emit("translate", { path: v.path, name: v.as || v.name, state: "failed", message: e.message });
      if (here()) body.replaceChildren(h("p", { class: "bad" }, e.message));
      else toast(v.name + " wasn't translated: " + e.message);
      return false;
    }
    emit("translate", { path: v.path, state: "done" });
    emit("library");   // its draft among the Sequence's experiments
    if (here()) showDraft(d);
    else toast(v.name + " is translated: click to review it", () => { expDialog("Translate " + v.name); showDraft(d); });
    return true;
  }
  async function openReview(id) {
    const body = expDialog("Review a translation");
    body.replaceChildren(h("span", { class: "spinner" }));
    try { showDraft(await api("/api/experiments/drafts/" + encodeURIComponent(id))); }
    catch (e) { body.replaceChildren(h("p", { class: "bad" }, e.message)); }
  }
  function fill(box, ...items) {   // a box's content: what's there, the nothing left out
    box.replaceChildren(...items.flat().filter(Boolean));
  }
  function findingLists(report) {
    const all = (report && report.findings) || [];
    const blocks = all.filter((f) => f.blocks), warns = all.filter((f) => !f.blocks);
    const rehearsed = Object.entries((report && report.rehearsals) || {}).map(([a, r]) => h("li", {}, (a || "the experiment") + ": " + r.message));
    return [
      blocks.length ? h("h3", {}, "What blocks it") : null, blocks.length ? h("ul", { class: "exp-blocks" }, ...blocks.map((f) => h("li", {}, f.text))) : null,
      rehearsed.length ? h("h3", {}, "Rehearsed on the simulated lab") : null, rehearsed.length ? h("ul", {}, ...rehearsed) : null,
      report && report.trace ? h("p", { class: "muted small" }, report.trace) : null,
      warns.length ? h("details", {}, h("summary", {}, warns.length + " warning" + (warns.length === 1 ? "" : "s") + " (keeping it accepts them)"),
        h("ul", {}, ...warns.map((f) => h("li", {}, f.text)))) : null];
  }
  function codeBlock(code) {
    const copy = h("button", { type: "button", class: "btn small" }, "Copy");
    copy.addEventListener("click", () => copyText(code, copy));
    return h("details", { open: true }, h("summary", {}, "The experiment ", copy), h("pre", { class: "exp-code" }, code));
  }
  function showDraft(d) {
    EXP.open = { draft: d.draft };
    const ok = d.report && d.report.ok_to_keep;
    const left = d.not_translated || [];
    $("exp-title").textContent = "Keep " + ((d.vi && d.vi.name) || "this translation") + "?";
    fill($("exp-body"),
      d.vi && d.vi.name ? h("p", { class: "muted small" }, "From " + (d.vi.path || d.vi.name) + (d.model ? ", translated by " + d.model : "")) : null,
      d.actions && d.actions.length ? h("p", {}, "Its actions: " + d.actions.map((a) => a.name + (a.main ? " (main)" : "")).join(", ")) : null,
      ...findingLists(d.report),
      left.length ? h("details", {}, h("summary", {}, left.length + " not translated, as " + d.notes_by + " says"),
        h("ul", {}, ...left.map((x) => h("li", {}, (x.what || x.id) + (x.why ? ": " + x.why : ""))))) : null,
      (d.proposed || []).length ? h("div", {}, h("h3", {}, "Keeping it also adds to the lab's LabVIEW map"),
        h("ul", {}, ...d.proposed.map((m) => h("li", {}, m.call + " → " + m.words + (m.replaces ? " (in place of Kelvin's own: " + m.replaces + ")" : ""))))) : null,
      codeBlock(d.code || ""));
    $("exp-name").value = d.name || "";
    $("exp-name-row").hidden = false;
    $("exp-keep").hidden = false;
    $("exp-keep").textContent = "Keep";
    $("exp-keep").disabled = !ok;
    $("exp-keep").title = ok ? "Keep it in the lab's experiments, approved by you" : "Something blocks it: see What blocks it";
    $("exp-msg").textContent = ok ? "" : "It can't be kept as it is: a corrected one goes through the chat (save_experiment).";
  }
  function keepDraft() {
    return busyWhile(["exp-keep"], async () => {
      if (!EXP.open || !EXP.open.draft) return;
      const open = EXP.open, name = $("exp-name").value.trim();
      try {
        const r = await api("/api/experiments/keep", { body: { draft: open.draft, name, new_version: open.newVersion === name } });
        $("exp-msg").textContent = "Kept as " + r.name + ", version " + r.version + ", in Sequence → Experiments.";
        $("exp-keep").hidden = true;
        EXP.open = null;
        emit("library");   // the Sequence's experiments with it now, its draft gone
      } catch (e) {
        if (e.status === 409 && e.data && e.data.diff) {   // a name kept already: what changed, read before the yes
          open.newVersion = name;
          const shown = $("exp-body").querySelector(".exp-kept-diff");
          if (shown) shown.remove();
          $("exp-body").prepend(h("details", { class: "exp-kept-diff", open: true },
            h("summary", {}, "What changed from version " + (e.data.version - 1) + " of " + name),
            h("pre", { class: "exp-code" }, e.data.diff.join("\n") || "(nothing)")));
          $("exp-keep").textContent = "Keep as version " + e.data.version;
        }
        $("exp-msg").textContent = e.message;
      }
    });
  }
  async function openVersions(name, version) {
    const body = expDialog(name);
    body.replaceChildren(h("span", { class: "spinner" }));
    let d;
    try { d = await api("/api/experiments/" + encodeURIComponent(name) + (version ? "?version=" + version : "")); }
    catch (e) { body.replaceChildren(h("p", { class: "bad" }, e.message)); return; }
    const v = d.version;
    EXP.open = { name, version: v.number };
    $("exp-title").textContent = name + ", version " + v.number;
    const pick = h("select", { "aria-label": "Version" }, ...d.versions.map((x) => h("option", { value: String(x.number), selected: x.number === v.number },
      "version " + x.number + " · " + x.standing + (x.approved_by ? " by " + x.approved_by : "") + (x.note ? " · " + x.note : ""))));
    pick.addEventListener("change", () => openVersions(name, Number(pick.value)));
    fill(body,
      h("p", {}, pick, " made by " + v.made_by + ", " + v.made_at + (v.edited_from ? ", edited from version " + v.edited_from : "")),
      v.vi && v.vi.name ? h("p", { class: "muted small" }, "From " + (v.vi.path || v.vi.name)) : null,
      v.diff ? h("details", { open: v.standing === "new" }, h("summary", {}, "What changed from version " + v.edited_from), h("pre", { class: "exp-code" }, v.diff)) : null,
      ...findingLists(v.report), codeBlock(v.code || ""));
    $("exp-approve").hidden = v.standing !== "new";
    $("exp-approve").textContent = "Approve version " + v.number;
    $("exp-msg").textContent = v.standing === "changed" ? "Its file changed on disk after it was written: it won't run. Save it as a new version." : "";
  }
  function approveVersion() {
    return busyWhile(["exp-approve"], async () => {
      if (!EXP.open || !EXP.open.version) return;
      try {
        const r = await api("/api/experiments/" + encodeURIComponent(EXP.open.name) + "/approve", { body: { version: EXP.open.version } });
        $("exp-msg").textContent = "Version " + r.version + " approved by " + r.approved_by + ".";
        $("exp-approve").hidden = true;
        emit("library");   // the Sequence's experiments as they stand now
      } catch (e) { $("exp-msg").textContent = e.message; }
    });
  }
  function scansSay(text, bad) {
    $("scans-msg").textContent = text;
    $("scans-msg").classList.toggle("bad", !!bad);
  }
  async function scansOp(body) {
    try {
      const r = await api("/api/library", { body });
      if (r.library) SCANS.data = r.library;
      return r;
    } catch (e) { scansSay(e.message, true); return null; }
  }
  function renderScans() {
    const lib = SCANS.data || {};
    const q = $("scans-filter").value.trim().toLowerCase();
    const match = (e) => !q || [e.name, e.summary, e.path].some((s) => s && String(s).toLowerCase().includes(q));
    const lists = [["scans-scripts", lib.scripts || []], ["scans-plans", lib.scans || []]].map(([id, list]) => [id, list, list.filter(match)]);
    const seen = new Set(lists.flatMap(([, , kept]) => kept.map((e) => e.id)));
    SCANS.sel = SCANS.sel.filter((id) => seen.has(id));   // a row out of sight is never acted on
    if (!seen.has(SCANS.anchor)) SCANS.anchor = null;
    let any = 0, shown = 0;
    for (const [id, list, kept] of lists) {
      any += list.length; shown += kept.length;
      $(id).hidden = !kept.length;
      $(id).querySelector("ul").replaceChildren(...kept.map(scanEntry));
    }
    SCANS.empty = SCANS.empty || $("scans-empty").textContent;
    $("scans-filter").hidden = !any;
    $("scans-empty").hidden = !SCANS.data || shown > 0;
    $("scans-empty").textContent = any ? "Nothing matches." : SCANS.empty;
    renderPick();
    scanStop();
    const field = document.querySelector(".lib-rename");
    if (field && document.activeElement !== field) { field.focus(); field.select(); }
  }
  // the list's one tab stop: the row the keyboard was last on, else the first selected, else the first
  function scanStop() {
    const rows = [...document.querySelectorAll(SCAN_ROWS)];
    const stop = rows.find((li) => li.dataset.id === SCANS.at) || rows.find((li) => SCANS.sel.includes(li.dataset.id)) || rows[0];
    for (const li of rows) li.tabIndex = li === stop ? 0 : -1;
  }
  function scanEntry(e) {
    const script = e.kind === "script";
    const key = "lib:" + encodeURIComponent(e.id);
    const act = (label, title, keys, queue) => {   // in the row: reached with → from it (the list is one tab stop)
      const b = h("button", { type: "button", title, tabindex: "-1", "data-busy": keys.join(" "), disabled: keys.some((k) => BUSY.has(k)) }, label);
      b.addEventListener("click", () => runScan(e, queue, keys));
      return b;
    };
    const more = h("button", { type: "button", tabindex: "-1", "aria-label": "More: " + e.name, "aria-haspopup": "menu" }, icon("dots"));
    more.addEventListener("click", (ev) => { ev.stopPropagation(); scanMenu(more, e); });
    const tag = !script ? (e.source === "labview" || e.source === "specialmeasure" ? h("span", { class: "tag", title: "From " + (e.path || e.vi) }, e.source === "labview" ? "LabVIEW" : "Special Measure") : null)
      : !e.exists ? h("span", { class: "tag warn", title: "The file is gone" }, "missing")
      : e.changed ? h("span", { class: "tag", title: "Edited since its last run" }, "changed") : null;
    const used = e.count ? "ran " + e.count + "×" + (e.used ? " · last " + dayText(new Date(e.used * 1000)) : "") : "not run yet";
    const what = script ? e.path : e.summary || "";
    const from = e.edited_from && !e.named ? "edited from " + e.edited_from + " · " : "";   // until renamed
    const at = SCANS.sel.indexOf(e.id);
    const li = h("li", { class: "q-item lib" + (SCANS.fresh === e.id ? " fresh" : "") + (at >= 0 ? " sel" : ""), "data-id": e.id,
        role: "row", "aria-selected": String(at >= 0), tabindex: "-1" },
      h("span", { class: "lib-name", role: "gridcell" }, SCANS.sel.length > 1 && at >= 0 ? h("span", { class: "lib-order", title: "Queued in this order" }, String(at + 1)) : null,
        SCANS.renaming === e.id ? renameField(e) : h("span", { class: "q-name" }, e.name), tag),
      h("span", { class: "q-acts", role: "gridcell" }, act("Run", "First in the queue, which pauses after it", ["start", key], false),
        act("Queue", "Last in the queue", [key], true), more),
      h("span", { class: "q-sub", role: "gridcell", title: what }, what), h("span", { class: "lib-used", role: "gridcell" }, from + used));
    li.addEventListener("click", (ev) => {
      if (ev.target.closest("button, input, select, textarea, label, a, pre")) return;   // its own controls, its detail
      pickScan(e.id, ev.shiftKey ? "range" : ev.ctrlKey || ev.metaKey ? "toggle" : "only");
    });
    li.addEventListener("keydown", (ev) => scanKeys(ev, li, e));
    li.addEventListener("focusin", () => { if (SCANS.at !== e.id) { SCANS.at = e.id; scanStop(); } });
    dragScan(li, e);
    if (SCANS.shown.has(e.id)) li.append(scanDetail(e));
    return li;
  }
  // a row dragged onto the queue: it, or with it the rest of the selection in the order picked (never from its fields)
  function dragScan(li, e) {
    li.draggable = true;
    li.addEventListener("pointerdown", (ev) => { li.draggable = !ev.target.closest("input, textarea, select, pre, .lib-detail"); });
    li.addEventListener("dragstart", (ev) => {
      ev.dataTransfer.setData(DRAG_SCANS, JSON.stringify(SCANS.sel.includes(e.id) ? SCANS.sel : [e.id]));
      ev.dataTransfer.effectAllowed = "copy";
      QUEUE.dragging = true;
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", dragDone);
  }
  // the selection: "only" this one (a click), "toggle" it (Ctrl+click, Space), a "range": the rows from the last one
  // picked to this one, those alone (Shift+click: it grows and shrinks); null clears it. In the order picked, which is
  // the order ⇒ adds them in
  function pickScan(id, how) {
    const ids = [...document.querySelectorAll(SCAN_ROWS)].map((li) => li.dataset.id);
    if (id === null) SCANS.sel = [];
    else if (how === "range" && SCANS.anchor && ids.includes(SCANS.anchor)) {
      const a = ids.indexOf(SCANS.anchor), b = ids.indexOf(id);
      SCANS.sel = a <= b ? ids.slice(a, b + 1) : ids.slice(b, a + 1).reverse();
    } else if (how === "toggle") {
      SCANS.sel = SCANS.sel.includes(id) ? SCANS.sel.filter((x) => x !== id) : SCANS.sel.concat(id);
      SCANS.anchor = id;
    } else {
      SCANS.sel = [id];
      SCANS.anchor = id;
    }
    const focused = document.activeElement && document.activeElement.closest && document.activeElement.closest(SCAN_ROWS);
    renderScans();
    const back = focused && document.querySelector('#scans-view li[data-id="' + CSS.escape(focused.dataset.id) + '"]');
    if (back) back.focus();
  }
  // on a row: Space toggles it, Enter edits it (Shift+Enter picks the rows from the last one picked), the arrows (Home,
  // End) move, Shift with them picks the rows from the last one picked, Escape clears, → goes to its buttons. On a
  // button: ← → move among them (← from the first and Escape: back to the row), ↑ ↓ to the row above or below. A
  // field's keys are its own
  function scanKeys(ev, li, e) {
    const rows = [...document.querySelectorAll(SCAN_ROWS)], i = rows.indexOf(li);
    const btns = [...li.querySelectorAll(".q-acts button")].filter((b) => !b.disabled);
    if (ev.target !== li) {
      const k = btns.indexOf(ev.target);
      if (k < 0) return;
      const to = ev.key === "ArrowRight" ? btns[k + 1] : ev.key === "ArrowLeft" ? btns[k - 1] || li : ev.key === "Escape" ? li
        : ev.key === "ArrowDown" || ev.key === "ArrowUp" ? rows[i + (ev.key === "ArrowDown" ? 1 : -1)] : undefined;
      if (to === undefined) return;
      if (to) to.focus();
      ev.preventDefault();
      return;
    }
    if (ev.key === " ") pickScan(e.id, "toggle");
    else if (ev.key === "Enter" && ev.shiftKey) pickScan(e.id, "range");
    else if (ev.key === "Enter") editScan(e);
    else if (ev.key === "ArrowDown" || ev.key === "ArrowUp" || ev.key === "Home" || ev.key === "End") {
      const next = ev.key === "Home" ? rows[0] : ev.key === "End" ? rows[rows.length - 1] : rows[i + (ev.key === "ArrowDown" ? 1 : -1)];
      if (!next) return;
      next.focus();
      if (ev.shiftKey) pickScan(next.dataset.id, "range");
    } else if (ev.key === "ArrowRight" && btns.length) btns[0].focus();
    else if (ev.key === "Escape" && SCANS.sel.length) pickScan(null);
    else return;
    ev.preventDefault();
  }
  function pickedScans(ids) {   // the entries selected (or these ids), in the order picked
    const lib = SCANS.data || {};
    const all = new Map([...(lib.scripts || []), ...(lib.scans || [])].map((e) => [e.id, e]));
    return (ids || SCANS.sel).map((id) => all.get(id)).filter(Boolean);
  }
  function renderPick() {
    SCANS.sel = pickedScans().map((e) => e.id);   // what is no longer in the library goes
    syncBusy();
  }
  function schedButtons() {   // ⇒ for what is selected (not while it is on its way), Edit for one
    const picked = pickedScans();
    $("sched-add").disabled = !picked.length || picked.some((e) => BUSY.has("lib:" + encodeURIComponent(e.id)));
    $("scans-edit").disabled = picked.length !== 1;
    $("scans-edit").title = picked.length === 1 && picked[0].kind === "script" ? "Its settings (Enter)" : "Edit (Enter)";
  }
  // Edit: a scan in the scan editor; a script's settings, shown in its row
  function editScan(e) {
    if (e.kind === "plan") { openEditor(e.id); return; }
    SCANS.shown.add(e.id);
    renderScans();
    const field = document.querySelector('#scans-view li[data-id="' + CSS.escape(e.id) + '"] .lib-params input');
    if (field) field.focus();
  }
  // ⇒ (or a drop): each selected scan or script (or those dragged: ids) into the queue in the order they were selected,
  // as its Queue does: at that place (at: from insertAt, after the selected item, or where they were dropped), else
  // last. The queue says what it took, and why it took no more
  function queuePicked(at, ids) {
    const picked = pickedScans(ids);
    return busyWhile(picked.map((e) => "lib:" + encodeURIComponent(e.id)), async () => {
      const names = [];
      let r = null;
      scansSay("");
      for (const e of picked) {
        const body = { op: "run", id: e.id, queue: true };
        if (at) body.position = at + names.length;
        const params = e.kind === "script" ? scanParams(e) : null;
        if (params) body.params = params;
        try { r = await api("/api/library", { body }); }
        catch (err) { $("queue-msg").textContent = err.message; break; }   // those before it are in the queue
        names.push(e.name);
        queueFromState(r.queue);
      }
      if (!names.length) return;
      if (at && !ids) followInsert(r.queue, at + names.length - 1);
      renderQueue();
      if (names.length === picked.length) $("queue-msg").textContent = names.length === 1 ? r.message || "" : "Added " + andList(names) + ".";
    });
  }
  function scanMenu(anchor, e) {
    const shown = SCANS.shown.has(e.id);
    const items = [{ label: shown ? "Hide" : "Show", icon: e.kind === "script" ? "sliders" : "doc", onClick: () => {
      if (shown) SCANS.shown.delete(e.id); else SCANS.shown.add(e.id);
      renderScans();
      focusMore(e.id);
    } }];
    if (e.kind === "plan") items.push({ label: "Edit", icon: "pencil", onClick: () => openEditor(e.id) });
    if (e.last_run) items.push({ label: "Open last run", icon: "chev", onClick: () => openRun(e.last_run, e.last_run.split("-").slice(2).join("-") || e.name) });
    items.push({ label: "Rename", icon: "pencil", onClick: () => { SCANS.renaming = e.id; renderScans(); } },
      { sep: true }, { label: "Remove", icon: "trash", danger: true, onClick: async () => {
        if (await scansOp({ op: "remove", id: e.id })) { SCANS.shown.delete(e.id); SCANS.params.delete(e.id); renderScans(); }
      } });
    popMenu(anchor, items);
  }
  function focusMore(id) {   // after a redraw, the keyboard goes on from the entry's ⋯
    const more = document.querySelector('#scans-view li[data-id="' + CSS.escape(id) + '"] .q-acts button:last-child');
    if (more) more.focus();
  }
  function renameField(e) {
    const input = h("input", { class: "side-input lib-rename", value: e.name, maxlength: "80", spellcheck: "false", "aria-label": "Name" });
    let done = false;
    const finish = async (save, keys) => {
      if (done || !input.isConnected) return;   // redrawn meanwhile: the new field goes on
      done = true;
      SCANS.renaming = null;
      const name = input.value.trim();
      if (save && name && name !== e.name) await scansOp({ op: "rename", id: e.id, name });
      renderScans();
      if (keys) focusMore(e.id);
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); finish(true, true); }
      else if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); finish(false, true); }
    });
    input.addEventListener("blur", () => finish(true, false));
    return input;
  }
  // a script's settings, as fields (settable: {name: value in the file}, notes: {name: its comment}): the values last
  // used, else the file's; with the file unreadable, those last used
  function scriptFields(e) {
    const params = e.params || {}, notes = e.notes || {};
    const unread = e.exists === false || !!e.problem;
    return Object.entries(unread ? params : e.settable || {}).map(([name, value]) =>
      ({ name, value: name in params ? params[name] : value, note: notes[name] || "" }));
  }
  const paramRaw = (v) => (typeof v === "boolean" ? v : typeof v === "string" ? v : JSON.stringify(v));
  function paramValue(base, raw) {   // what was typed, as the kind of value the script has there
    if (raw === undefined) return base;
    if (typeof base === "boolean" || typeof base === "string") return raw;
    if (typeof base === "number") return raw.trim() !== "" && isFinite(Number(raw)) ? Number(raw) : raw;
    try { return JSON.parse(raw); } catch (err) { return raw; }
  }
  // what Run sends: nothing while the person has changed no field in Show (Kelvin runs it with the values last used);
  // else the values a person set, now or for its last run, never a field nobody touched, and none the file has as it
  // is now: those follow the file
  function scanParams(e) {
    const edited = SCANS.params.get(e.id) || {};
    const fields = new Map(scriptFields(e).map((f) => [f.name, f]));
    const changed = Object.keys(edited).filter((name) => fields.has(name));
    if (!changed.length) return null;
    const file = e.exists === false || e.problem ? {} : e.settable || {};
    const out = {};
    for (const [name, f] of fields) {
      const v = changed.includes(name) ? paramValue(f.value, edited[name]) : (e.params || {})[name];
      if (v !== undefined && !(name in file && JSON.stringify(v) === JSON.stringify(file[name]))) out[name] = v;
    }
    return out;
  }
  function scanDetail(e) {
    if (e.kind !== "script") return h("pre", { class: "lib-detail lib-plan", role: "gridcell" }, e.text || "");
    const fields = scriptFields(e);
    if (!SCANS.params.has(e.id)) SCANS.params.set(e.id, {});
    const edited = SCANS.params.get(e.id);   // only the fields the person changed, as typed
    const rows = fields.map((f) => {
      const was = paramRaw(f.value);
      const keep = (v) => { if (v === was) delete edited[f.name]; else edited[f.name] = v; };
      const input = typeof f.value === "boolean" ? h("input", { type: "checkbox" })
        : h("input", { value: f.name in edited ? edited[f.name] : was, spellcheck: "false", autocomplete: "off",
                       inputmode: typeof f.value === "number" ? "decimal" : null });
      if (input.type === "checkbox") {
        input.checked = f.name in edited ? !!edited[f.name] : f.value;
        input.addEventListener("change", () => keep(input.checked));
      } else input.addEventListener("input", () => keep(input.value));
      return h("label", { class: "lib-param", title: f.note || null }, h("span", {}, f.name), input);
    });
    const args = e.args && e.args.length ? h("span", { class: "lib-args" }, "args: " + e.args.join(" ")) : null;
    const problem = e.problem ? h("span", { class: "lib-problem hint bad" }, e.problem) : null;
    return h("div", { class: "lib-detail lib-params", role: "gridcell" }, problem,
      ...(rows.length ? rows : problem ? [] : [h("span", { class: "hint" }, "No settings to change.")]), args);
  }
  function runScan(e, queue, keys) {
    return busyWhile(keys, async () => {
      const body = { op: "run", id: e.id };
      const params = e.kind === "script" ? scanParams(e) : null;
      if (params) body.params = params;
      if (queue) body.queue = true;
      scansSay(queue ? "" : "Starting…");   // the queue's approval is asked first
      const r = await scansOp(body);
      if (!r) return;
      queueFromState(r.queue);
      renderQueue();
      scansSay(r.message || "");
      if (!queue) { showScan((r.message || "").split("\n")[0]); pollState(); }   // the Scan shows it as it runs
    });
  }
  // Import a script or VI…: the computer's own file picker in the Kelvin app; in a browser, its path typed. A script
  // goes into the person's library; a LabVIEW VI is translated at once into one of the lab's experiments, a draft to
  // read and keep (openTranslate), as Set up's Translate does
  function hasFilePicker() { return !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_file); }
  async function importScript() {
    if (!hasFilePicker()) {
      $("scans-path").hidden = false;
      $("scans-path-input").focus();
      return;
    }
    let path = "";
    try { path = (await window.pywebview.api.pick_file(store("lh.scriptFolder") || LAB.scriptsFolder || "")) || ""; } catch (e) { return; }
    if (path) importPath(path);
  }
  const unquoted = (path) => String(path).trim().replace(/^"(.*)"$/, "$1").trim();   // as Windows' Copy as path gives it
  function isVI(path) { return /\.vi$/i.test(unquoted(path)); }
  // as: the name given for it (New function's Name); from: the dialog it was asked in, which a refusal at once goes back
  // to (openTranslate): its words are returned, as whether it was translated is
  async function importVI(path, as, from) {
    const vi = unquoted(path);
    scansSay("");
    const done = await openTranslate({ name: vi.replace(/^.*[\\/]/, ""), path: vi, as: String(as || "").trim() }, from);
    if (done !== true) return done;   // a typed path stays to fix
    store("lh.scriptFolder", vi.replace(/[\\/][^\\/]*$/, ""));
    $("scans-path").hidden = true;
    $("scans-path-input").value = "";
    return true;
  }
  async function importPath(path) {
    if (isVI(path)) return importVI(path);
    scansSay("");
    const r = await scansOp({ op: "import", path });
    if (!r) return;
    store("lh.scriptFolder", path.replace(/[\\/][^\\/]*$/, ""));
    $("scans-path").hidden = true;
    $("scans-path-input").value = "";
    SCANS.fresh = r.entry ? r.entry.id : null;
    $("scans-filter").value = "";
    const check = r.check || {};
    const problems = check.problems || [];
    const close = h("button", { class: "icon-btn", type: "button", "aria-label": "Close" }, icon("x"));
    close.addEventListener("click", () => { $("scans-check").hidden = true; });
    $("scans-check").className = "callout small scans-check" + (problems.length ? " bad" : " calm");
    $("scans-check").replaceChildren(...[h("div", { class: "sc-head" }, h("b", {}, r.entry ? r.entry.name : path), close),
      problems.length ? h("ul", {}, ...problems.map((p) => h("li", {}, p))) : null,
      check.text ? h("div", { class: "sc-text" }, check.text) : null].filter(Boolean));
    $("scans-check").hidden = false;
    renderScans();
  }

  // ------------------------------------------------------------------ the scan editor, Special Measure style
  // A scan of the library as its loops (outer first: what each steps, from, to, step or points, the values, a ramp's
  // legs), what it records at each point and what it sets first and at the end, in a panel of its own beside the others
  // (opening it never closes one). Each change is checked (POST /api/library op check: its points, its time as its card
  // would say it, what load_plan and the gate find; no instrument is asked). Save keeps it as a new scan of the library,
  // "edited from" the one it came from, which stays; Run and Add to queue run it as any scan runs, through the queue,
  // its gate and its approval. What the editor doesn't show as fields is kept as written, read-only. A scan waiting in
  // the queue (its Edit) is "Queue item #n" (item: its id): Save puts it back into that item, which the queue checks
  // again; Save as scan keeps it in the library instead (lib: the entry its runs are noted under, edited from).
  const ED = { open: false, id: null, item: null, lib: null, from: "", parts: null, base: "", units: new Map(),
               setKeys: new Set(), readKeys: new Set(), check: null, timer: 0, gen: 0, busy: false, asking: null,
               insts: new Map(), unitOf: new Map(),   // the lab's instruments for the menus; each key's unit
               vars: new WeakSet(),   // the loops stepping a variable (a name of their own), named or not yet
               focus: null,           // what gets the focus once the editor is drawn again (its data-f)
               key: Math.random().toString(36).slice(2) };   // this editor, to Kelvin: of its checks, only the newest runs
  const ED_MODES = [["range", "Range"], ["values", "List"], ["ramp", "Ramp"]];

  function toggleEditor(open) {
    ED.open = open === undefined ? !ED.open : open;
    $("edpane").hidden = !ED.open;
    if (!ED.open) edStopCheck();
    fitLayout();
  }
  function edStopCheck() {   // the check about to be asked, and the one asked: nobody waits for them now
    clearTimeout(ED.timer);
    if (ED.asking) ED.asking.abort();
    ED.asking = null;
  }
  // the scan with this library id in the editor, the scan waiting in the queue as this item (item: its id), or a new
  // scan (neither); the one open there with changes stays open when asked for again, and goes only when the person
  // says so. The editor comes to the screen (even in full screen); there already, its head rings (Kelvin.paneShow)
  async function openEditor(id, item) {
    const same = !!ED.parts && ED.id === (id || null) && ED.item === (item || null);
    if (same && edDirty()) { Kelvin.paneShow("edpane"); $("ed-name").focus(); return; }
    if (ED.parts && !same && edDirty() && !(await edLeave())) return;
    let d;
    try { d = await api("/api/library", { body: Object.assign({ op: "parts", id: id || null }, item ? { item } : {}) }); }
    catch (e) { if (item) $("queue-msg").textContent = e.message; else scansSay(e.message, true); return; }
    edStopCheck();   // a check of the scan open before is no answer for this one
    ED.gen++;
    ED.id = d.id || null;
    ED.item = d.item || null;
    ED.lib = d.library || null;
    ED.from = d.edited_from || "";
    ED.parts = d.parts;
    ED.base = JSON.stringify(d.parts);
    ED.check = d.check;
    edInstruments(d.lab || { set: [], read: [], instruments: [] });
    ED.vars = new WeakSet();
    ED.focus = null;
    const fresh = !ED.id && !ED.item && Array.isArray(d.parts.loops) && !d.parts.loops.length && !d.parts.time;
    if (fresh) {   // a new scan starts with its first loop, to choose what it changes
      ED.parts.loops.push(blankLoop());
      ED.base = JSON.stringify(ED.parts);
    }
    edPlan(false);
    edSay("");
    renderEditor();
    Kelvin.paneShow("edpane");
    $("ed-name").focus();
    if (fresh) edCheck();
  }
  const edDirty = () => !!ED.parts && JSON.stringify(ED.parts) !== ED.base;
  // an instrument renamed in the Lab panel: the scan open here says it by its new name (and that is no change of the
  // person's), its menus as the lab is now
  async function edRenamed(from, to) {
    if (!ED.parts) return;
    const token = new RegExp("(?<![\\w.:/\\\\-])" + from.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\.(?=[A-Za-z_])", "g");
    const dirty = edDirty(), was = Array.isArray(ED.parts.loops) ? ED.parts.loops : [];
    ED.parts = JSON.parse(JSON.stringify(ED.parts).replace(token, to + "."));
    (ED.parts.loops || []).forEach((loop, i) => { if (ED.vars.has(was[i])) ED.vars.add(loop); });   // still a variable
    ED.base = dirty ? ED.base.replace(token, to + ".") : JSON.stringify(ED.parts);
    edRedraw();   // at once: what is typed from now on goes into the scan as it is now
    try {
      edInstruments((await api("/api/library", { body: { op: "parts", id: null } })).lab || {});
      edRedraw();
    } catch (e) { /* the menus as they were */ }
    edCheck();
  }
  // the scan open with changes, before it goes (×, or another scan opened): Save, Discard or Keep editing (Discard or
  // Keep editing when Save can't keep it now). true: it may go (saved, or its changes dropped)
  async function edLeave() {
    const name = ED.parts.name || "this scan", save = !$("ed-save").disabled;
    const said = await Kelvin.askYes("Save your changes to " + name + "?",
      save ? { ok: "Save", alt: "Discard", altDanger: true, cancel: "Keep editing" } : { ok: "Discard", cancel: "Keep editing", danger: true });
    if (said === "alt" || (said === true && !save)) return true;
    if (said !== true) return false;
    await edSave(false);
    return !edDirty();   // a refusal keeps it open, said under it
  }
  // ×: the editor closes; with changes, once the person has said what becomes of them
  async function closeEditor() {
    if (edDirty() && !(await edLeave())) return;
    toggleEditor(false);
  }
  // what a box holds: a number when it is one, else the text as typed (the check says what is wrong), empty: none
  const edParse = (s) => { const t = String(s).trim(); if (t === "") return null; const x = Number(t); return isFinite(x) ? x : t; };
  const edShow = (v) => (v == null ? "" : v === true ? "on" : v === false ? "off" : String(v));
  const edList = (s) => String(s).split(",").map((t) => t.trim()).filter((t) => t !== "").map(edParse);
  const edNum = (v) => typeof v === "number" && isFinite(v);
  // a range's points as plans.py counts them: from, from ± step, … and `to` itself
  function rangeCount(a, b, step) {
    if (!edNum(a) || !edNum(b) || !edNum(step) || step === 0) return null;
    const n = Math.floor(Math.abs(b - a) / Math.abs(step) + 1e-9);
    if (n >= 1e6) return null;
    const last = a + (b >= a ? 1 : -1) * Math.abs(step) * n;
    return n + 1 + (Math.abs(last - b) <= Math.max(1e-9 * Math.max(Math.abs(last), Math.abs(b)), 1e-12) ? 0 : 1);
  }
  const rangeStep = (loop) => (edNum(loop.from) && edNum(loop.to) && Number.isInteger(loop.points) && loop.points > 1
    ? Number((Math.abs(loop.to - loop.from) / (loop.points - 1)).toPrecision(6)) : null);
  function loopCount(loop) {   // points along a loop, there and back counted; a ramp's: the check's estimate
    let n = null;
    if (loop.mode === "range") n = loop.by === "points" ? (Number.isInteger(loop.points) ? loop.points : null) : rangeCount(loop.from, loop.to, loop.step);
    else if (loop.mode === "values") n = (loop.values || []).length;
    else {
      const c = ED.check && ED.check.counts ? ED.check.counts[loop.key] : null;
      return c == null ? "" : (loop.mode === "ramp" ? "~" : "") + c + (c === 1 ? " point" : " points");
    }
    if (n == null) return "";
    if (loop.back && n > 1) n = 2 * n - 1;
    return n + (n === 1 ? " point" : " points");
  }
  // a loop's values in the order it takes them (a range as plans.py makes it, at most 200), to start another kind with
  function loopValues(loop) {
    if (loop.mode === "values") return (loop.values || []).slice();
    if (loop.mode === "ramp") return (loop.legs || []).map((l) => l.to).filter((v) => v != null);
    const a = loop.from, b = loop.to, round = (x) => Number(x.toPrecision(10));
    const n = loop.by === "points" ? loop.points : rangeCount(a, b, loop.step);
    if (!Number.isInteger(n) || n < 1 || n > 200 || !edNum(a) || !edNum(b)) return [];
    if (loop.by !== "points") {   // from, from ± step, … and `to` itself where the steps miss it
      const s = (b >= a ? 1 : -1) * Math.abs(loop.step);
      const out = Array.from({ length: Math.floor(Math.abs(b - a) / Math.abs(loop.step) + 1e-9) + 1 }, (_, i) => round(a + s * i));
      return out.length < n ? out.concat(b) : out;
    }
    if (loop.log && a > 0 && b > 0 && n > 1) return Array.from({ length: n }, (_, i) => (i === n - 1 ? b : round(a * Math.pow(b / a, i / (n - 1)))));
    return Array.from({ length: n }, (_, i) => round(n > 1 ? a + (b - a) * i / (n - 1) : a));
  }
  function edMode(loop, mode) {
    const had = loopValues(loop);
    loop.mode = mode;
    if (!had.length) return;
    if (mode === "values" && !(loop.values || []).length) loop.values = had;
    if (mode === "range" && loop.from == null && loop.to == null && had.every(edNum)) {
      Object.assign(loop, { from: had[0], to: had[had.length - 1], points: had.length, by: "points" });
    }
    if (mode === "ramp" && !(loop.legs || []).length) Object.assign(loop, { legs: [{ to: had[had.length - 1], rate: null, hold: null, bare: true, order: [] }], one: true });
  }
  function blankLoop() {
    return { key: "", mode: "range", from: null, to: null, step: null, points: null, by: "points", log: false, back: false,
             values: [], form: "", legs: [], one: false, every: null, settle: null, first_settle: null, with: [],
             measure: [], kept: "", fixed: [], order: [] };
  }

  // a labelled box of the editor: set(value) on each change
  function edBox(name, value, set, unit, attrs) {
    const input = h("input", Object.assign({ class: "ed-in", value: edShow(value), inputmode: "decimal", spellcheck: "false",
      autocomplete: "off" }, attrs || {}));
    input.addEventListener("input", () => set(edParse(input.value)));
    const box = h("label", { class: "ed-f" }, name ? h("span", {}, name) : null, input, unit ? h("i", {}, unit) : null);
    box.input = input;
    return box;
  }
  function edCheckBox(name, on, set, attrs) {
    const input = h("input", Object.assign({ type: "checkbox" }, attrs || {}));
    input.checked = !!on;
    input.addEventListener("change", () => set(input.checked));
    const box = h("label", { class: "check ed-check" }, input, name);
    box.input = input;
    return box;
  }
  // the editor's title, and what its Save does: a scan waiting in the queue is "Queue item #n" (its place now, which
  // moves), saved back into it, beside Save as scan; with no Run or Add to queue (it is in the queue)
  function edTitle() {
    const n = queueItems().findIndex((i) => i.id === ED.item) + 1;
    $("ed-title").textContent = ED.item ? "Queue item " + (n ? "#" + n : "(removed)") : ED.id ? "Edit scan" : "New scan";
    $("ed-queue").hidden = $("ed-run").hidden = !!ED.item;
    $("ed-save-as").hidden = !ED.item;
    $("ed-save").classList.toggle("primary", !!ED.item);
    $("ed-save").title = ED.item ? "Back into the queue" + (n ? " as #" + n : "") : "A new scan in your library; the one it came from stays";
  }
  function renderEditor() {
    const p = ED.parts, fixed = new Set(p.fixed || []);
    edTitle();
    $("ed-from").textContent = ED.from ? "edited from " + ED.from : "";
    const field = (id, value, kept) => {   // a part kept as written shows there, not here
      if (!("hint" in $(id).dataset)) $(id).dataset.hint = $(id).placeholder;
      $(id).value = kept ? "" : edShow(value);
      $(id).disabled = kept;
      $(id).placeholder = kept ? "as written below" : $(id).dataset.hint;
    };
    field("ed-name", p.name, p.name === null || fixed.has("plan"));
    field("ed-desc", p.description, p.description === null || fixed.has("description"));
    field("ed-repeat", p.repeat, fixed.has("repeat"));
    field("ed-settle", p.settle, fixed.has("settle"));
    $("ed-settle-f").hidden = p.settle == null && !fixed.has("settle");   // each loop has its own wait
    renderLoops();
    edRows("sets");
    edRows("setup");
    edRows("after");
    $("ed-kept").textContent = p.kept || "";
    $("ed-kept-sec").hidden = !p.kept;
    renderEdCheck();
  }
  // ------------------------------------------------------------------ the loops, as Special Measure's scan editor
  // Each loop is a card, the innermost first: Loop 1 steps fastest (ED.parts.loops is outer first, as the plan writes
  // them; a time series, time:, is the innermost loop when the scan has one). A card says what its loop changes, an
  // instrument and then one of its parameters (more than one move in step: with:), from, to and its step, its waits,
  // and what it records: the innermost loop's at each point (measure:), an outer loop's once per value of it.
  const ED_VAR = "\u0001var", ED_TIME = "\u0001time";
  // the lab's instruments for the menus, by name (lab.instruments); two of one model are told apart by address
  function edInstruments(lab) {
    const list = lab.instruments || [], models = new Map();
    for (const i of list) models.set(i.model, (models.get(i.model) || 0) + 1);
    ED.insts = new Map(list.map((i) => [i.name, Object.assign({}, i, { twin: models.get(i.model) > 1 })]));
    ED.unitOf = new Map((lab.set || []).concat(lab.read || []).map((p) => [p.key, p.unit || ""]));
  }
  const edSplit = (key) => { const k = String(key || ""), dot = k.indexOf("."); return dot < 0 ? [null, k] : [k.slice(0, dot), k.slice(dot + 1)]; };
  const edInstText = (i) => [i.name, i.model, i.twin ? i.address : ""].filter(Boolean).join(" · ") + (i.off ? " (off)" : "");
  const edParamText = (p) => p.label + (p.unit ? " (" + p.unit + ")" : "") + (p.name ? " · " + p.name : "") + (p.action ? " · action" : "");
  function edParams(name, what) {   // what of an instrument a row may take: "read", "setup" (actions too) or "set"
    const i = ED.insts.get(name);
    return !i ? [] : what === "read" ? i.read : what === "setup" ? i.set : i.set.filter((p) => !p.action);
  }
  // what a row changes or records: an instrument, then one of its parameters. opts: f (what the row is: data-f, for the
  // focus), vars (a variable may be chosen instead, a name of your own that Computed works settings out from), time
  // (or a time series), mode ("var" or "time": the row is one). Another instrument keeps the parameter of the same
  // name (two of one model), else takes its headline reading or its first output; pick(key, how, typing) on a choice
  function edPick(key, what, pick, label, opts) {
    opts = opts || {};
    const [iname, pname] = edSplit(key);
    const mode = opts.mode || (iname !== null ? "inst" : key ? "var" : "none");
    const opt = (value, text) => h("option", { value }, text);
    const insts = [...ED.insts.values()].filter((i) => edParams(i.name, what).length || i.name === iname);
    const inst = h("select", { class: "ed-in ed-inst", "aria-label": label + ": instrument", "data-f": opts.f + "-i" },
      ...(mode === "none" ? [opt("", "Instrument…")] : []),
      ...insts.map((i) => opt(i.name, edInstText(i))),
      ...(mode === "inst" && !ED.insts.has(iname) ? [opt(iname, iname + " (not in this lab)")] : []),
      ...(opts.time ? [opt(ED_TIME, "Time")] : []),
      ...(opts.vars ? [opt(ED_VAR, "Variable…")] : []));
    inst.value = mode === "var" ? ED_VAR : mode === "time" ? ED_TIME : mode === "inst" ? iname : "";
    if (mode === "inst" && ED.insts.has(iname)) {   // chosen, it says its name; its model and address in the menu, and here
      inst.selectedOptions[0].label = iname;
      inst.title = edInstText(ED.insts.get(iname));
    }
    inst.addEventListener("change", () => {
      const v = inst.value;
      if (v === ED_VAR) return pick(mode === "var" ? key : "", "var");
      if (v === ED_TIME) return pick("", "time");
      const list = edParams(v, what), i = ED.insts.get(v) || {};
      const p = list.find((q) => q.param === pname) || list.find((q) => (what === "read" ? q.param === i.headline : q.drive)) || list[0];
      pick(p ? v + "." + p.param : "", "inst");
    });
    const out = [inst];
    if (mode === "inst") {
      const list = edParams(iname, what);
      const par = h("select", { class: "ed-in ed-par", "aria-label": label + ": parameter", "data-f": opts.f + "-p" },
        ...list.map((p) => opt(p.param, edParamText(p))),
        ...(list.some((p) => p.param === pname) ? [] : [opt(pname, pname + (ED.insts.has(iname) ? (what === "read" ? " (not a reading)" : " (can't be set)") : ""))]));
      par.value = pname;
      par.addEventListener("change", () => pick(iname + "." + par.value, "param"));
      out.push(par);
    } else if (mode === "var") {
      const name = h("input", { class: "ed-in ed-var", value: key, placeholder: "a name: n", spellcheck: "false", autocomplete: "off",
        "aria-label": label + ": variable", "data-f": opts.f + "-v", title: "A name of your own, which Computed works settings out from" });
      name.addEventListener("input", () => pick(name.value.trim(), "var", true));
      out.push(name);
    }
    return h("span", { class: "ed-pick" + (mode === "none" ? " unpicked" : "") }, ...out);
  }
  // a row's choice made: the editor drawn again, the focus where the person goes next (its parameter, once its
  // instrument is chosen; a variable's name)
  function edPicked(f, how) {
    ED.focus = f + (how === "inst" || how === "param" ? "-p" : how === "var" ? "-v" : "-i");
    edChanged(true);
  }
  // the loops' cards, the innermost first, and how many there are; none: the scan measures once
  function renderLoops() {
    const p = ED.parts, loops = Array.isArray(p.loops) ? p.loops : null, time = p.time && !(p.fixed || []).includes("time");
    $("ed-stepper").hidden = !loops;
    if (!loops) {   // the loops as written; a time series, when it has one, still its own card
      $("ed-loops").replaceChildren(h("li", { class: "hint" }, "As written below."), ...(time ? [edTimeCard(p.time, 0, 1)] : []));
      return;
    }
    const cards = loops.map((loop, i) => ({ loop, i })).reverse();
    if (time) cards.unshift({ time: p.time });
    const n = cards.length;
    $("ed-n").textContent = String(n);
    $("ed-fewer").disabled = !n;
    $("ed-loops").replaceChildren(...(n ? cards.map((c, k) => (c.time ? edTimeCard(c.time, k, n) : edLoop(c.loop, c.i, k, n))) : [edPointCard()]));
  }
  // a loop's card: Loop n (inner, outer), its points, ↑ Inner, ↓ Outer, ✕ Remove; what it changes, its waits, and
  // what it records. A loop the editor can't show as fields is kept as written
  function edLoop(loop, i, k, n) {
    const p = ED.parts, name = "Loop " + (k + 1), f = "l" + i;
    const acts = h("span", { class: "ed-acts" });
    const act = (text, title, fn) => {
      const b = h("button", { class: "icon-btn", type: "button", title, "aria-label": title + ": " + name, "data-f": f + "-" + title }, text);
      b.addEventListener("click", fn);
      acts.append(b);
    };
    if (i < p.loops.length - 1) act("↑", "Inner", () => edMoveLoop(i, i + 1));
    if (i > 0) act("↓", "Outer", () => edMoveLoop(i, i - 1));
    act("✕", "Remove", () => { p.loops.splice(i, 1); ED.focus = "ed-add-loop"; edChanged(true); });
    const where = n > 1 ? (k === 0 ? "inner" : k === n - 1 ? "outer" : "") : "";
    const head = h("div", { class: "ed-loop-head" }, h("h4", {}, name), where ? h("span", { class: "ed-where" }, where) : null,
      h("span", { class: "ed-count" }, loopCount(loop)), acts);
    const parts = [];
    if (loop.mode === "kept") {
      parts.push(h("div", { class: "ed-line" }, h("code", { class: "ed-key-ro" }, loop.key),
        h("span", { class: "tag", title: "Kept as written: the editor doesn't show this loop as fields" }, "as written")),
        h("pre", { class: "ed-kept" }, loop.text || ""));
    } else {
      parts.push(edChange(loop, i, k, name));
      if (loop.mode !== "ramp") parts.push(edWaits(loop, name, f));
      if (loop.kept) parts.push(h("pre", { class: "ed-kept", title: "Kept as written" }, loop.kept));
    }
    if (k === 0) parts.push(edInnerRecord(loop, name));
    else if (loop.mode !== "kept") parts.push(edRecord([[loop.measure || (loop.measure = []), f + "r"]], name, true));
    return h("li", { class: "ed-loop" + (loop.mode === "kept" ? " kept" : ""), "data-i": String(i), "data-key": loop.key },
      head, ...parts, h("ul", { class: "ed-problems ed-loop-problems" }));
  }
  // what a loop changes: its own setting (a variable, or the time) and how it steps, its values, and the settings that
  // move in step with it (with:), each from → to over its points
  function edChange(loop, i, k, name) {
    const f = "l" + i, also = loop.with || (loop.with = []);
    const main = edPick(loop.key, "set", (key, how, typing) => edLoopKey(loop, i, key, how, typing), name + ": what it changes",
      { f, vars: true, time: k === 0 && !(ED.parts.fixed || []).includes("time"), mode: ED.vars.has(loop) ? "var" : undefined });
    const mode = h("select", { class: "ed-in ed-mode", "aria-label": name + ": how it steps", "data-f": f + "-m" },
      ...ED_MODES.map(([v, t]) => h("option", { value: v }, t)));
    mode.value = loop.mode;
    mode.addEventListener("change", () => { edMode(loop, mode.value); ED.focus = f + "-m"; edChanged(true); });
    const rows = [h("div", { class: "ed-chan" }, main, mode), ...edModeFields(loop, name, f)];
    if (loop.mode !== "ramp") {
      also.forEach((row, j) => rows.push(...edWithRow(loop, row, j, name, f)));
      const add = h("button", { class: "link soft", type: "button", "data-f": f + "-add",
        title: "Another setting that moves in step with this one, from → to over the same points" }, "+ Change another");
      add.addEventListener("click", () => {
        also.push({ key: "", from: null, to: null, values: [], form: "range", order: [] });
        ED.focus = f + "w" + (also.length - 1) + "-i";
        edChanged(true);
      });
      rows.push(h("div", { class: "ed-line" }, add));
    }
    return h("div", { class: "ed-part" }, h("span", { class: "ed-part-h" }, "Change"), ...rows);
  }
  // a loop's own setting chosen: an instrument's parameter, a variable, or the time (the innermost loop becomes a time
  // series, and what it read once per value it reads at each point)
  function edLoopKey(loop, i, key, how, typing) {
    const p = ED.parts;
    if (how === "time") {
      p.loops.splice(i, 1);
      if (Array.isArray(p.measure)) for (const m of loop.measure || []) if (m && !p.measure.includes(m)) p.measure.push(m);
      p.time = { every: null, for: null, points: null, order: [] };
      ED.focus = "t-every";
      edChanged(true);
      return;
    }
    if (how === "var") ED.vars.add(loop); else ED.vars.delete(loop);
    loop.key = key;
    if (typing) edChanged();
    else edPicked("l" + i, how);
  }
  // a setting moving in step with its loop's own: from → to over the loop's points (its step shown), or its own values
  function edWithRow(loop, row, j, name, f) {
    const label = name + ", also " + (j + 1), g = f + "w" + j;
    const x = h("button", { class: "icon-btn", type: "button", "aria-label": label + ": remove", title: "Remove", "data-f": g + "-x" }, icon("x"));
    x.addEventListener("click", () => { loop.with.splice(j, 1); ED.focus = f + "-add"; edChanged(true); });
    const pick = edPick(row.key, "set", (key, how) => { row.key = key; edPicked(g, how); }, label, { f: g });
    let vals;
    if (row.form === "list" || row.form === "values") {
      const b = edBox("", (row.values || []).map(edShow).join(", "), (v) => v, "",
        { class: "ed-in ed-values", inputmode: "text", "aria-label": label + ": values", "data-f": g + "-vals" });
      b.input.addEventListener("input", () => { row.values = edList(b.input.value); edChanged(); });
      vals = [b];
    } else {
      const step = h("span", { class: "ed-wstep", "data-j": String(j) }, withStep(loop, row));
      const box = (key) => edBox(key, row[key], (v) => { row[key] = v; step.textContent = withStep(loop, row); edChanged(); }, "",
        { class: "ed-in ed-short", "aria-label": label + ": " + key, "data-f": g + "-" + key });
      vals = [box("from"), box("to"), step];
    }
    return [h("div", { class: "ed-chan ed-with" }, pick, x), h("div", { class: "ed-line ed-vals" }, ...vals)];
  }
  function withStep(loop, row) {   // a setting's step, moving from → to over its loop's points
    const n = loop.mode === "values" ? (loop.values || []).length : loop.by === "points" ? loop.points : rangeCount(loop.from, loop.to, loop.step);
    if (!row || !edNum(row.from) || !edNum(row.to) || !Number.isInteger(n) || n < 2) return "";
    return "step " + Number((Math.abs(row.to - row.from) / (n - 1)).toPrecision(4));
  }
  // a loop's points and its settings' steps, as its values are typed (no redraw: the box keeps its caret)
  function edLoopShown(loop) {
    const li = $("ed-loops").querySelector('[data-i="' + ED.parts.loops.indexOf(loop) + '"]');
    if (!li) return;
    li.querySelector(".ed-count").textContent = loopCount(loop);
    li.querySelectorAll(".ed-wstep").forEach((s) => { s.textContent = withStep(loop, (loop.with || [])[+s.dataset.j]); });
  }
  // a loop's values by how it steps: a range's from, to, step and points (each following the other: the one typed is
  // the one kept), a list of values, or a ramp's legs and how often it measures on the way
  function edModeFields(loop, name, f) {
    const fixed = new Set(loop.fixed || []), unit = ED.unitOf.get(loop.key) || "";
    const changed = () => { edLoopShown(loop); edChanged(); };
    if (loop.mode === "values") {
      const vals = edBox("", (loop.values || []).map(edShow).join(", "), (v) => v, "",
        { class: "ed-in ed-values", inputmode: "text", "aria-label": name + ": values", placeholder: "0, 0.5, 1", "data-f": f + "-vals" });
      vals.input.addEventListener("input", () => { loop.values = edList(vals.input.value); changed(); });
      return [h("div", { class: "ed-line ed-vals" }, vals)];
    }
    if (loop.mode === "ramp") {
      const legs = (loop.legs || []).map((leg, j) => {
        const x = h("button", { class: "icon-btn", type: "button", "aria-label": name + ": remove leg " + (j + 1), title: "Remove", "data-f": f + "g" + j + "-x" }, icon("x"));
        x.addEventListener("click", () => { loop.legs.splice(j, 1); ED.focus = f + "-leg"; edChanged(true); });
        return h("div", { class: "ed-line ed-leg" },
          edBox("to", leg.to, (v) => { leg.to = v; changed(); }, unit, { class: "ed-in ed-short", "aria-label": name + ": leg " + (j + 1) + " to", "data-f": f + "g" + j + "-to" }),
          edBox("rate", leg.rate, (v) => { leg.rate = v; changed(); }, "", { class: "ed-in ed-short", "aria-label": name + ": leg " + (j + 1) + " rate", "data-f": f + "g" + j + "-rate", title: "In its rate setting's unit; empty: as the instrument is set" }),
          edBox("hold", leg.hold, (v) => { leg.hold = v; changed(); }, "s", { class: "ed-in ed-short", "aria-label": name + ": leg " + (j + 1) + " hold (s)", "data-f": f + "g" + j + "-hold", title: "Measuring on once it is there" }),
          x);
      });
      const add = h("button", { class: "link soft", type: "button", "data-f": f + "-leg" }, "+ Leg");
      add.addEventListener("click", () => { loop.legs.push({ to: null, rate: null, hold: null, bare: false, order: [] }); loop.one = false; ED.focus = f + "g" + (loop.legs.length - 1) + "-to"; edChanged(true); });
      const every = edBox("measure every", loop.every, (v) => { loop.every = v; changed(); }, "s", { class: "ed-in ed-short", "aria-label": name + ": measure every (s)", "data-f": f + "-every" });
      return legs.concat([h("div", { class: "ed-line" }, add, every)]);
    }
    const from = edBox("from", loop.from, (v) => { loop.from = v; follow(); }, "", { class: "ed-in ed-short", "aria-label": name + ": from", "data-f": f + "-from" });
    const to = edBox("to", loop.to, (v) => { loop.to = v; follow(); }, "", { class: "ed-in ed-short", "aria-label": name + ": to", "data-f": f + "-to" });
    const step = edBox("step", loop.by === "points" ? rangeStep(loop) : loop.step, (v) => { loop.step = v; loop.by = "step"; follow(); }, "",
      { class: "ed-in ed-short", "aria-label": name + ": step", "data-f": f + "-step", disabled: fixed.has("step") || !!loop.log, title: loop.log ? "A log sweep is given by its points" : null });
    const pts = edBox("points", loop.by === "points" ? loop.points : rangeCount(loop.from, loop.to, loop.step), (v) => { loop.points = v; loop.by = "points"; follow(); }, "",
      { class: "ed-in ed-short", inputmode: "numeric", "aria-label": name + ": points", "data-f": f + "-pts" });
    function follow() {
      if (document.activeElement !== step.input) step.input.value = edShow(loop.by === "points" ? rangeStep(loop) : loop.step);
      if (document.activeElement !== pts.input) pts.input.value = edShow(loop.by === "points" ? loop.points : rangeCount(loop.from, loop.to, loop.step));
      changed();
    }
    return [h("div", { class: "ed-line ed-vals" }, from, to, step, pts)];
  }
  // a loop's waits: after each step (settle), and after its first value each time the loop starts again (first_settle,
  // Special Measure's start wait); there and back; a range's log spacing
  function edWaits(loop, name, f) {
    const fixed = new Set(loop.fixed || []);
    const wait = edBox("Wait", loop.settle, (v) => { loop.settle = v; edChanged(); }, "s",
      { class: "ed-in ed-short", "aria-label": name + ": wait after each step (s)", "data-f": f + "-wait", disabled: fixed.has("settle"),
        title: fixed.has("settle") ? "A hold per value, as written below" : "After each step, before it records" });
    const start = edBox("Start wait", loop.first_settle, (v) => { loop.first_settle = v; edChanged(); }, "s",
      { class: "ed-in ed-short", "aria-label": name + ": wait after its first value (s)", "data-f": f + "-first",
        title: "After its first value, each time the loop starts again (in place of Wait)" });
    const back = edCheckBox("and back", loop.back, (on) => { loop.back = on; edLoopShown(loop); edChanged(); },
      { "aria-label": name + ": there and back", "data-f": f + "-back" });
    const log = loop.mode !== "range" ? null : edCheckBox("log", loop.log, (on) => {
      loop.log = on;
      if (on && loop.by !== "points") Object.assign(loop, { points: rangeCount(loop.from, loop.to, loop.step) || 2, by: "points" });
      ED.focus = f + "-log";
      edChanged(true);
    }, { "aria-label": name + ": log spaced", "data-f": f + "-log" });
    return h("div", { class: "ed-line ed-waits" }, wait, start, back, log);
  }
  // what a card records, a reading a row (lists: [list, what its rows are], new ones going into the first); outer:
  // once per value of its loop, else at each point
  function edRecord(lists, name, outer, note) {
    const rows = [];
    let n = 0;
    for (const [list, g] of lists) {
      list.forEach((m, j) => {
        const label = name + ": record " + (++n), f = g + j;
        const x = h("button", { class: "icon-btn", type: "button", "aria-label": label + ": remove", title: "Remove", "data-f": f + "-x" }, icon("x"));
        x.addEventListener("click", () => { list.splice(j, 1); ED.focus = lists[0][1] + "-add"; edChanged(true); });
        rows.push(h("div", { class: "ed-chan" }, edPick(m, "read", (key, how) => { list[j] = key; edPicked(f, how); }, label, { f }), x));
      });
    }
    const add = lists.length ? h("button", { class: "link soft", type: "button", "data-f": lists[0][1] + "-add",
      title: outer ? "A reading taken once per value of this loop" : "A reading taken at each point" }, "+ Record") : null;
    if (add) add.addEventListener("click", () => { const [list, g] = lists[0]; list.push(""); ED.focus = g + (list.length - 1) + "-i"; edChanged(true); });
    return h("div", { class: "ed-part" }, h("span", { class: "ed-part-h", title: outer ? "Once per value of this loop" : "At each point" }, "Record"),
      ...rows, note ? h("p", { class: "hint" }, note) : null, add ? h("div", { class: "ed-line" }, add) : null);
  }
  // the innermost card's readings: the plan's, at each point, where new ones go, and any its loop has of its own (the
  // same for the innermost loop); the plan's kept as written: they are below, and new ones go to the loop
  function edInnerRecord(loop, name) {
    const p = ED.parts, lists = [], i = loop ? p.loops.indexOf(loop) : -1;
    const own = loop && loop.mode !== "kept" && loop.mode !== "ramp";
    if (Array.isArray(p.measure)) lists.push([p.measure, "pm"]);
    if (own && ((loop.measure || []).length || !lists.length)) lists.push([loop.measure || (loop.measure = []), "l" + i + "r"]);
    return edRecord(lists, name, false, p.measure === null ? "And as written below." : "");
  }
  // a time series, the innermost loop: a point every so often, for so long (or so many points). Another choice in its
  // menu (an instrument, a variable) makes it a loop stepping that, in its place
  function edTimeCard(time, k, n) {
    const p = ED.parts, name = "Loop " + (k + 1), byPoints = time.points != null && time.for == null;
    const pick = !Array.isArray(p.loops) ? h("code", { class: "ed-key-ro" }, "time") : edPick("", "set", (key, how) => {
      if (how === "time") return;
      p.time = null;
      const loop = Object.assign(blankLoop(), { key });
      if (how === "var") ED.vars.add(loop);
      p.loops.push(loop);
      edPicked("l" + (p.loops.length - 1), how);
    }, name + ": what it changes", { f: "t", vars: true, time: true, mode: "time" });
    const count = h("span", { class: "ed-count" }, timeCount(time));
    const box = (key, label, unit) => edBox(label, time[key], (v) => { time[key] = v; count.textContent = timeCount(time); edChanged(); }, unit,
      { class: "ed-in ed-short", "aria-label": "Time series: " + label + (unit ? " (" + unit + ")" : ""), "data-f": "t-" + key });
    const x = h("button", { class: "icon-btn", type: "button", title: "Remove", "aria-label": "Remove: " + name, "data-f": "t-Remove" }, "✕");
    x.addEventListener("click", () => { p.time = null; ED.focus = "ed-add-loop"; edChanged(true); });
    const head = h("div", { class: "ed-loop-head" }, h("h4", {}, name), n > 1 ? h("span", { class: "ed-where" }, "inner") : null,
      count, h("span", { class: "ed-acts" }, x));
    return h("li", { class: "ed-loop ed-time", "data-i": "time" }, head,
      h("div", { class: "ed-part" }, h("span", { class: "ed-part-h" }, "Change"), h("div", { class: "ed-chan" }, pick),
        h("div", { class: "ed-line ed-vals" }, box("every", "every", "s"), byPoints ? box("points", "points", "") : box("for", "for", "s"))),
      edInnerRecord(null, name), h("ul", { class: "ed-problems ed-loop-problems" }));
  }
  function timeCount(t) {   // a time series' points, as plans.py counts them
    const n = Number.isInteger(t.points) ? t.points : edNum(t.for) && edNum(t.every) && t.every > 0 ? Math.floor(t.for / t.every + 1e-9) + 1 : null;
    return n == null ? "" : n + (n === 1 ? " point" : " points");
  }
  // no loops: the scan measures once, what it records
  function edPointCard() {
    return h("li", { class: "ed-loop ed-point", "data-i": "none" },
      h("div", { class: "ed-loop-head" }, h("h4", {}, "No loops"), h("span", { class: "ed-count" }, "1 point")), edInnerRecord(null, "The point"));
  }
  function edMoveLoop(i, j) {
    const loops = ED.parts.loops;
    loops.splice(j, 0, loops.splice(i, 1)[0]);
    ED.focus = "l" + j + "-" + (j > i ? "Inner" : "Outer");
    edChanged(true);
    if (document.activeElement === document.body) {   // at the end it moved to, it has no such button: its others
      const b = $("ed-loops").querySelector('[data-i="' + j + '"] .ed-acts button');
      if (b) b.focus();
    }
  }
  function edRows(which) {   // setup, after or sets (Computed): an instrument's setting = a value, or an expression
    const rows = ED.parts[which] === undefined ? (ED.parts[which] = []) : ED.parts[which], box = $("ed-" + which);
    $("ed-add-" + which).hidden = !Array.isArray(rows);
    const empty = Array.isArray(rows) && !rows.length;   // an empty part is only its button, in the row under the parts
    $("ed-" + which + "-sec").hidden = empty;
    $("ed-new-" + which).hidden = !empty;
    $("ed-adds").hidden = ["sets", "setup", "after"].every((w) => $("ed-new-" + w).hidden);
    if (!Array.isArray(rows)) { box.replaceChildren(h("p", { class: "hint" }, "As written below.")); return; }
    const what = which === "setup" ? "Setup" : which === "sets" ? "Computed" : "At the end", expr = which === "sets";
    box.replaceChildren(...rows.map((row, i) => {
      const f = which + i, label = what + " " + (i + 1), unit = ED.unitOf.get(row.key) || "";
      const pick = edPick(row.key, expr ? "set" : "setup", (key, how) => { row.key = key; edPicked(f, how); }, label, { f });
      const [iname, pname] = edSplit(row.key);
      const spec = edParams(iname, "setup").find((q) => q.param === pname) || {};
      let value;
      if (!expr && spec.choices) {   // a switch or a list setting: its words (a plan's on: or true, its first)
        const shown = row.value === true ? spec.choices[0] : row.value === false ? spec.choices[1] : edShow(row.value);
        value = h("select", { class: "ed-in ed-choice", "aria-label": label + ": its value", "data-f": f + "-val" },
          ...(spec.choices.includes(shown) ? [] : [h("option", { value: shown }, shown || "…")]),
          ...spec.choices.map((c) => h("option", { value: c }, c)));
        value.value = shown;
        value.addEventListener("change", () => { row.value = value.value; edChanged(); });
      } else {
        value = h("input", { class: "ed-in " + (expr ? "ed-expr" : "ed-short"), value: edShow(row.value), spellcheck: "false",
          autocomplete: "off", "aria-label": label + (expr ? ": its expression" : ": its value"), "data-f": f + "-val",
          placeholder: expr ? "1.2*n - 8*E" : "", inputmode: expr ? "text" : "decimal" });
        value.addEventListener("input", () => { row.value = expr ? value.value.trim() : edParse(value.value); edChanged(); });
      }
      const x = h("button", { class: "icon-btn", type: "button", "aria-label": label + ": remove", title: "Remove", "data-f": f + "-x" }, icon("x"));
      x.addEventListener("click", () => {
        rows.splice(i, 1);
        ED.focus = rows.length ? which + Math.min(i, rows.length - 1) + "-i" : "ed-new-" + which;
        edChanged(true);
      });
      return h("div", { class: "ed-line ed-set" }, pick, h("span", { class: "ed-eq" }, "="), value, h("span", { class: "ed-unit" }, unit), x);
    }));
  }
  // a change: the buttons now, the check a moment after typing stops (each newer one replaces the one before: the one
  // asked before it is dropped, here and by Kelvin)
  function edChanged(redraw) {
    if (redraw) edRedraw();
    edSay("");   // what the last Save or Run said is about the scan as it was
    edButtons();
    clearTimeout(ED.timer);
    ED.timer = setTimeout(edCheck, 350);
  }
  // the editor drawn again: what had the focus keeps it (data-f says what a box is, not where), or what ED.focus
  // names takes it (the parameter once its instrument is chosen, a new row's instrument)
  function edRedraw() {
    const had = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.f : null;
    const want = ED.focus || had;
    ED.focus = null;
    renderEditor();
    const el = want && ($("ed-body").querySelector('[data-f="' + want + '"]') || document.getElementById(want));
    if (el && !el.disabled) el.focus();
  }
  async function edCheck() {
    const gen = ++ED.gen;
    if (ED.asking) ED.asking.abort();
    const asking = ED.asking = new AbortController();
    let c;
    try { c = await api("/api/library", { body: { op: "check", parts: ED.parts, editor: ED.key }, signal: asking.signal }); }
    catch (e) { c = asking.signal.aborted ? null : { loads: false, problems: [e.message] }; }
    if (gen !== ED.gen || !ED.parts || !c || c.superseded) return;
    ED.asking = null;
    ED.check = c;
    renderEdCheck();
  }
  // which loop a problem is about (load_plan's sweep.<key>: …, the gate's sweep: <key> …, the editor's loop n: …)
  function loopOf(problem, loops) {
    return loops.findIndex((l, i) => l.key && (problem.includes("sweep." + l.key + ":") || problem.includes("sweep." + l.key + ".")
      || problem.startsWith("sweep: " + l.key + " ") || problem.startsWith(l.key + " is stepped")) || problem.startsWith("loop " + (i + 1) + ":"));
  }
  function renderEdCheck() {
    const c = ED.check || {}, loops = Array.isArray(ED.parts.loops) ? ED.parts.loops : [];
    const bits = (c.loads && c.points != null ? [(c.estimate ? "~" : "") + c.points + (c.points === 1 ? " point" : " points")] : [])
      .concat(c.loads && c.time ? [c.time] : []);
    // each bit whole ("~5.8 min" never split): a narrow footer breaks only after the dot
    $("ed-status").textContent = bits.map((b) => b.replace(/ /g, "\u00a0")).join(" · ");
    const mine = new Map(), rest = [];
    for (const problem of c.problems || []) {
      const at = loopOf(problem, loops);
      const card = at >= 0 ? String(at) : /^time[:.]/.test(problem) && $("ed-loops").querySelector('[data-i="time"]') ? "time" : null;
      if (card === null) { rest.push(problem); continue; }
      if (!mine.has(card)) mine.set(card, []);
      mine.get(card).push(problem.replace(/^loop \d+: choose what it steps \(instrument\.parameter\)$/, "Choose what it changes.")
        .replace(/^loop \d+: /, ""));
    }
    const lines = (list) => list.map((t) => h("li", t === "Choose what it changes." ? { class: "ed-todo" } : {}, t));
    $("ed-problems").replaceChildren(...lines(rest));
    for (const li of $("ed-loops").children) {   // each card's own, and its points (a ramp's: the check's estimate)
      const list = li.querySelector(".ed-loop-problems");
      if (list) list.replaceChildren(...lines(mine.get(li.dataset.i) || []));
      const count = li.querySelector(".ed-count"), loop = loops[li.dataset.i];
      if (count && loop && li.dataset.i !== "time") count.textContent = loopCount(loop);
    }
    $("ed-plan-text").textContent = c.text || "";
    edButtons();
  }
  function edButtons() {
    const c = ED.check || {}, busy = ED.busy || BUSY.has("ed"), dirty = edDirty();
    $("ed-save").disabled = busy || !c.loads || !(ED.item ? dirty : dirty || !ED.id);   // an item saved unchanged would run again
    $("ed-save-as").disabled = busy || !c.loads;
    $("ed-run").disabled = busy || BUSY.has("start") || !c.loads;
    $("ed-queue").disabled = busy || !c.loads;
    $("ed-revert").disabled = busy || !dirty;
  }
  function edSay(text, bad) {
    $("ed-msg").textContent = text;
    $("ed-msg").classList.toggle("bad", !!bad);
  }
  // the scan the editor now edits: the entry it was saved as (the one it came from stays in the library; a queue item
  // stays as it was), as it was sent
  function edSaved(entry, library, sent) {
    ED.id = entry.id;
    ED.item = ED.lib = null;
    ED.from = entry.edited_from && !entry.named ? entry.edited_from : "";
    ED.base = sent;   // what was typed meanwhile is still a change
    edTitle();
    $("ed-from").textContent = ED.from ? "edited from " + ED.from : "";
    if (library) { SCANS.data = library; SCANS.fresh = entry.id; renderScans(); }
    edButtons();
  }
  // Save: a queued scan back into its item (its plan's text from Kelvin, op text, then the queue's edit, which checks it
  // as it checks any change); else, and Save as scan, a new scan in the library, edited from the one it came from
  async function edSave(asScan) {
    clearTimeout(ED.timer);
    ED.busy = true;
    edButtons();
    try {
      const sent = JSON.stringify(ED.parts);
      if (ED.item && !asScan) {
        const { text } = await api("/api/library", { body: { op: "text", parts: ED.parts } });
        QUEUE.data = await api("/api/queue", { body: { op: "edit", item: ED.item, text } });
        ED.base = sent;
        renderQueue();
        edSay("Saved to the queue.");
      } else {
        const from = ED.item ? ED.lib : ED.id;
        const r = await api("/api/library", { body: { op: "save", parts: ED.parts, from } });
        edSaved(r.entry, r.library, sent);
        edSay(r.new ? "Saved." : "Already in your scans.");
      }
    } catch (e) { edSay(e.message, true); }
    ED.busy = false;
    edButtons();
  }
  // Run (first in the queue, which pauses after it) or Add to queue (last): the scan as it is in the library, or, changed,
  // kept as a new scan first; through the queue's check and approval, as any scan
  function edRun(queue) {
    return busyWhile(queue ? ["ed"] : ["ed", "start"], async () => {
      const body = edDirty() || !ED.id ? { op: "run", parts: ED.parts, from: ED.id } : { op: "run", id: ED.id };
      if (queue) body.queue = true;
      const sent = JSON.stringify(ED.parts);
      edSay(queue ? "" : "Starting…");   // the queue's approval is asked first
      let r;
      try { r = await api("/api/library", { body }); } catch (e) { edSay(e.message, true); return; }
      if (r.entry) edSaved(r.entry, r.library, sent);
      queueFromState(r.queue);
      renderQueue();
      edSay((r.message || "").split("\n")[0]);
      if (!queue) { showScan((r.message || "").split("\n")[0]); pollState(); }   // the Scan shows it as it runs
    });
  }
  function edRevert() {
    ED.parts = JSON.parse(ED.base);
    edSay("");
    edChanged(true);
  }
  // Plan text: the plan the editor makes, as YAML, opened and closed in place by its button, whose label stays
  function edPlan(show) {
    $("ed-plan").hidden = !show;
    $("ed-text").setAttribute("aria-expanded", String(show));
    if (show) edEase($("ed-plan"));
  }
  function initEditor() {
    $("close-editor").addEventListener("click", closeEditor);
    $("ed-name").addEventListener("input", () => { ED.parts.name = $("ed-name").value; edChanged(); });
    $("ed-desc").addEventListener("input", () => { ED.parts.description = $("ed-desc").value; edChanged(); });
    for (const key of ["repeat", "settle"]) $("ed-" + key).addEventListener("input", () => { ED.parts[key] = edParse($("ed-" + key).value); edChanged(); });
    // how many loops: + one more, outside the others; − the outermost goes (each card has its own ✕ too)
    $("ed-add-loop").addEventListener("click", () => {
      ED.parts.loops.unshift(blankLoop());
      ED.focus = "l0-i";
      edChanged(true);
    });
    $("ed-fewer").addEventListener("click", () => {
      const p = ED.parts;
      if (p.loops.length) p.loops.shift(); else p.time = null;
      ED.focus = "ed-fewer";
      edChanged(true);
    });
    for (const which of ["sets", "setup", "after"]) {
      const add = () => {   // its + Add, or its button in the row under the parts while it is empty
        ED.parts[which].push({ key: "", value: null });
        ED.focus = which + (ED.parts[which].length - 1) + "-i";
        edChanged(true);
      };
      $("ed-add-" + which).addEventListener("click", add);
      $("ed-new-" + which).addEventListener("click", add);
    }
    $("ed-text").addEventListener("click", () => edPlan($("ed-plan").hidden));
    $("ed-copy").addEventListener("click", () => copyText($("ed-plan-text").textContent, $("ed-copy")));
    $("ed-save").addEventListener("click", () => edSave(false));
    $("ed-save-as").addEventListener("click", () => edSave(true));
    $("ed-run").addEventListener("click", () => edRun(false));
    $("ed-queue").addEventListener("click", () => edRun(true));
    $("ed-revert").addEventListener("click", edRevert);
  }

  // ------------------------------------------------------------------ why the lab has no instruments yet
  function whyEmpty(s) {
    if ((s.instruments || []).length) return null;
    const setup = s.setup || {};
    if (s.lab_info && s.lab_info.error) return null;   // the lab-problem callout says why
    if (SETUP_BUSY.has(setup.state)) return null;        // "Setting up instruments…" shows instead
    const lines = (setup.lines && setup.lines.length ? setup.lines : (s.survey && s.survey.lines)) || [];
    const text = setup.state === "done" || setup.state === "failed" ? setup.message
      : s.survey ? s.survey.reason : "Kelvin hasn't looked for instruments yet.";
    return { text, lines };
  }
  function showWhy(s) {
    const why = whyEmpty(s);
    $("lab-why").hidden = !why;
    document.querySelector(".lab-chip").title = why ? "No instruments yet: " + why.text : "The lab this portal controls";
    if (!why) return;
    $("lab-why-text").textContent = why.text;
    $("lab-why-details").hidden = !why.lines.length;
    const list = $("lab-why-lines");
    const key = why.lines.join("\n");
    if (list.dataset.key !== key) {   // redraw only when the lines change, so an open list stays put
      list.dataset.key = key;
      list.replaceChildren(...why.lines.map((l) => h("li", {}, l)));
    }
    if (!why.lines.length) list.hidden = true;
  }
  function toggleWhyDetails() {
    const list = $("lab-why-lines");
    list.hidden = !list.hidden;
    $("lab-why-details").setAttribute("aria-expanded", String(!list.hidden));
  }

  // ------------------------------------------------------------------ waiting while the instruments are set up
  // shown when the portal opens during the setup (Kelvin starting), and for the first setup's question about the lab's
  // scripts; closed, it stays closed (the Lab panel still says what the setup is doing). later: a scripts folder
  // named too late to shape the first lab config, opened in Set up once the setup is done
  const WAIT = { shown: false, closed: false, later: "", said: "" };
  function setupWait(s) {
    if (document.body.classList.contains("welcome-on")) return;   // the first start's page (welcome.js) asks instead
    const setup = s.setup || {}, busy = SETUP_BUSY.has(setup.state), ask = !!s.ask_scripts, dlg = $("setup-wait");
    if (!busy && WAIT.later) {
      const folder = WAIT.later;
      WAIT.later = "";
      if (dlg.open) dlg.close();
      openLabSetup().then(() => surveyScripts(folder));
      return;
    }
    if (!dlg.open) {
      if (WAIT.closed || WAIT.shown || !(ask || (busy && performance.now() < 20000))) return;
      WAIT.shown = true;
      dlg.showModal();
    } else if (!busy && !ask && !WAIT.said) {   // done: say so, and get out of the way
      dlg.close();
      toast(setup.message || "Your instruments are ready.");
      return;
    }
    $("setup-wait-title").textContent = busy ? "Setting up your instruments" : "Your instruments are set up";
    $("setup-wait-spin").hidden = !busy;
    $("setup-wait-step").textContent = (setup.message || "Starting") + (busy ? "…" : "");
    $("setup-wait-bar").hidden = !(busy && setup.steps);
    if (busy && setup.steps) $("setup-wait-fill").style.width = Math.round(100 * (setup.step - 1) / setup.steps) + "%";
    $("setup-wait-hint").hidden = !busy;
    $("setup-scripts").hidden = !ask;
    $("setup-scripts-msg").hidden = !WAIT.said;
    $("setup-scripts-msg").textContent = WAIT.said;
    $("setup-wait-close").textContent = busy ? "Use Kelvin meanwhile" : "Done";
  }
  async function answerScripts(none) {   // the first setup's question: the folder of the lab's scripts, or none
    let folder = "";
    if (!none) {
      folder = await pickFolder(LAB.scriptsFolder || "", "The folder with your lab's scripts (Python, notebooks, MATLAB):");
      if (!folder) return;
    }
    const msg = $("setup-scripts-msg");
    msg.hidden = false;
    msg.textContent = none ? "" : "Reading the scripts in " + folder + " (never running them)…";
    try {
      const r = await api("/api/instruments/scripts-folder", { body: { folder } });
      $("setup-scripts").hidden = true;
      if (none) { msg.hidden = true; return; }
      LAB.scriptsFolder = folder;
      const n = r.instruments, k = r.scripts;
      WAIT.said = !n ? "No instrument work in the scripts there. Set up… reads another folder any time."
        : "Read " + k + " script" + (k === 1 ? "" : "s") + " using " + n + " instrument" + (n === 1 ? "" : "s") + ". " +
          (r.now ? "Kelvin sets them up as your scripts use them, for you to check."
            : "Set up shows what they add once the setup is done.");
      msg.textContent = WAIT.said;
      if (n && !r.now) WAIT.later = folder;
    } catch (e) { msg.textContent = e.message; }
  }

  // ------------------------------------------------------------------ setting up connected instruments
  const SETUP_BUSY = new Set(["scanning", "drafting", "connecting", "reading"]);
  function andList(names) {
    return names.length < 2 ? names.join("") : names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
  }
  function showSetup(setup, unreviewed) {
    const busy = setup && SETUP_BUSY.has(setup.state);
    const waiting = (unreviewed || []).length > 0;
    const text = waiting
      ? "Kelvin added " + andList(unreviewed) + ": check " + (unreviewed.length === 1 ? "its" : "their") + " limits (Limits… on the row)."
      : (setup && setup.message ? setup.message + (busy ? "…" : "") : "");
    $("lab-setup-box").hidden = !text;
    $("lab-setup-box").classList.toggle("locked", waiting);
    $("lab-setup-msg").textContent = text;
    $("lab-unlock").hidden = !waiting;
    $("lab-setup-again").disabled = !!busy;
  }
  function renderFolders(folders) {
    $("lab-folders-field").hidden = !folders.length;
    $("lab-folders").replaceChildren(...folders.map((f) => {
      const serial = f.parsed.serial && (/^s\/?n/i.test(f.parsed.serial) ? f.parsed.serial : "s/n " + f.parsed.serial);
      const who = [serial, f.parsed.firmware && "firmware " + f.parsed.firmware,
        f.answered_with].filter(Boolean).join(" · ");
      const manual = f.manual
        ? h("span", { title: f.manual_from || "added by hand" }, "Manual, " + (f.manual_pages || "?") + " pages")
        : h("span", { class: "muted" }, f.manual_url ? "No manual yet" : "Its card names no manual address");
      const acts = [];
      const act = (label, fn) => { const b = h("button", { class: "link soft", type: "button" }, label); b.addEventListener("click", fn); acts.push(b); return b; };
      if (f.manual) act("Open", () => openInstrument(f.name, "manual"));
      else if (f.manual_url) act("Fetch from the maker", () => fetchManual(f.name));
      act(f.manual ? "Use another PDF…" : "Choose PDF…", () => choosePdf(f.name));
      act("Folder", () => openInstrument(f.name, "folder"));
      const li = h("li", { class: "folder-row" },
        h("div", { class: "folder-who" }, h("b", {}, f.name), h("span", {}, f.model), who ? h("span", { class: "muted small" }, who) : null),
        h("div", { class: "folder-manual" }, manual, ...acts));
      const limitsBtn = act("Limits…", () => editLimits(f.name, li, limitsBtn));
      limitsBtn.classList.add("limits-btn");
      li.querySelector(".folder-manual").append(limitsBtn);
      const calTag = h("span", { class: "cal-state" });
      showCalibration(calTag, f.calibration);
      li.querySelector(".folder-who").append(calTag);
      const calBtn = act("Calibration…", () => editCalibration(f.name, li, calBtn, calTag));
      li.querySelector(".folder-manual").append(calBtn);
      return li;
    }));
  }
  // ------------------------------------------------------------------ an instrument's limits, set by a person
  async function editLimits(name, li, btn) {
    const open = li.querySelector(".limits-editor");
    if (open) { open.remove(); btn.setAttribute("aria-expanded", "false"); return; }
    let r;
    try { r = await api("/api/instruments/" + encodeURIComponent(name) + "/limits"); } catch (e) { $("lab-msg").textContent = e.message; return; }
    btn.setAttribute("aria-expanded", "true");
    const box = h("div", { class: "limits-editor" });
    const show = (v) => (v === null || v === undefined ? "" : String(Number(v.toPrecision ? v.toPrecision(8) : v)));
    const rows = r.rows.map((row) => {
      const lo = h("input", { class: "lim-in", type: "number", step: "any", value: row.lab ? show(row.lab[0]) : "",
        placeholder: row.vendor ? show(row.vendor[0]) : "", "aria-label": row.label + ": lowest", disabled: !r.editable });
      const hi = h("input", { class: "lim-in", type: "number", step: "any", value: row.lab ? show(row.lab[1]) : "",
        placeholder: row.vendor ? show(row.vendor[1]) : "", "aria-label": row.label + ": highest", disabled: !r.editable });
      row.ui = { lo, hi };
      return h("div", { class: "lim-row" + (row.drive ? " drive" : "") },
        h("span", { class: "lim-name", title: row.param }, row.label + (row.unit && !/\)$/.test(row.label) ? " (" + row.unit + ")" : "")),
        lo, h("span", { class: "muted small" }, "to"), hi,
        h("span", { class: "muted small lim-card" }, row.vendor ? "card: " + show(row.vendor[0]) + " to " + show(row.vendor[1]) : "no limit on its card"));
    });
    const msg = h("span", { class: "muted small" });
    box.append(h("p", { class: "hint" }, r.rows.length
      ? "What " + name + " may reach. Empty: the card's limits." +
        (r.editable ? "" : " They are set in the lab config, which there isn't yet.")
      : name + " has no numeric setting to limit."), ...rows);
    if (r.editable && r.rows.length) {
      const reviewed = h("input", { type: "checkbox" });   // the person ticks it themselves
      const save = h("button", { class: "btn small primary", type: "button" }, "Save limits");
      save.addEventListener("click", async () => {
        const limits = {};
        for (const row of r.rows) {
          const a = row.ui.lo.value.trim(), b = row.ui.hi.value.trim();
          if (!a && !b) { if (row.lab) limits[row.param] = null; continue; }
          if (!a || !b) { msg.textContent = row.label + ": give both the lowest and the highest."; return; }
          limits[row.param] = [Number(a), Number(b)];
        }
        save.disabled = true;
        try {
          const saved = await api("/api/instruments/" + encodeURIComponent(name) + "/limits", { body: { limits, reviewed: !r.reviewed && reviewed.checked, why: why.value.trim() } });
          const now = new Map(saved.rows.map((x) => [x.param, x.lab]));
          for (const row of r.rows) row.lab = now.get(row.param) || null;
          const checked = saved.reviewed && !r.reviewed;
          if (checked) { r.reviewed = true; tick.remove(); }
          msg.textContent = "Saved: Kelvin holds " + name + " to these from now on" + (checked ? ", and treats it like your other instruments." : ".");
          refreshLabSetup(false);   // the lab config and the notices; this editor stays open
        } catch (e) { msg.textContent = e.message; }
        finally { save.disabled = false; }
      });
      const foot = h("div", { class: "lim-foot" });
      const tick = h("label", { class: "lim-review" }, reviewed, " I checked these for my sample: Kelvin may treat " + name + " like my other instruments");
      if (!r.reviewed) foot.append(tick);
      const why = h("input", { class: "lim-why", placeholder: "Why? (kept in the lab config's history)", "aria-label": "Why" });
      foot.append(why, save, msg);
      box.append(foot);
    }
    li.append(box);
  }
  // an instrument's calibration as a person records it: due date and certificate (health_check and cards warn)
  function showCalibration(tag, cal) {
    const st = (cal && cal.state) || { state: "none" };
    tag.className = "cal-state " + st.state;
    tag.textContent = st.state === "none" ? "No calibration on record" : "Calibration " + st.words;
    tag.title = st.serial || "";
  }
  async function editCalibration(name, li, btn, tag) {
    const open = li.querySelector(".cal-editor");
    if (open) { open.remove(); btn.setAttribute("aria-expanded", "false"); return; }
    let r;
    try { r = await api("/api/instruments/" + encodeURIComponent(name) + "/calibration"); } catch (e) { $("lab-msg").textContent = e.message; return; }
    btn.setAttribute("aria-expanded", "true");
    const rec = r.record || {};
    const field = (label, input) => h("label", { class: "cal-field" }, h("span", { class: "muted small" }, label), input);
    const calibrated = h("input", { type: "date" }), due = h("input", { type: "date" });
    const cert = h("input", { type: "text", maxlength: "200", placeholder: "Its number" });
    const by = h("input", { type: "text", maxlength: "200", placeholder: "Who calibrated it (optional)" });
    const file = h("input", { type: "file", accept: ".pdf,.png,.jpg,.jpeg" });
    const msg = h("span", { class: "muted small" });
    const save = h("button", { class: "btn small primary", type: "button" }, "Save calibration");
    const said = h("p", { class: "hint" }, r.state.state === "none" ? "No calibration on record yet. Kelvin warns in its health check and on approval cards once one is out of date. " :
      "On record: " + r.state.words + (rec.serial ? ", serial " + rec.serial : "") + (rec.by ? ", recorded by " + rec.by : "") +
      (r.earlier ? " (" + r.earlier + " earlier, kept)" : "") + ". ");
    if (rec.file) {
      const see = h("button", { class: "link soft", type: "button" }, "Open the certificate");
      see.addEventListener("click", () => fetchDownload("/api/instruments/" + encodeURIComponent(name) + "/calibration/certificate?file=" + encodeURIComponent(rec.file), rec.file, "certificate"));
      said.append(see);
    }
    save.addEventListener("click", async () => {
      if (!due.value) { msg.textContent = "Give the date it is due again."; return; }
      save.disabled = true;
      msg.textContent = "Saving…";
      try {
        let kept = "";
        const chosen = file.files && file.files[0];
        if (chosen) {
          const res = await fetch("/api/instruments/" + encodeURIComponent(name) + "/calibration/certificate",
            { method: "PUT", headers: { "X-Kelvin-Token": token, "X-File-Name": encodeURIComponent(chosen.name) }, body: chosen });
          const j = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(j.error || "HTTP " + res.status);
          kept = j.file;
        }
        const out = await api("/api/instruments/" + encodeURIComponent(name) + "/calibration",
          { body: { calibrated: calibrated.value, due: due.value, certificate: cert.value.trim(), provider: by.value.trim(), file: kept } });
        showCalibration(tag, out);
        said.firstChild.textContent = "On record: " + out.state.words + (out.record.serial ? ", serial " + out.record.serial : "") +
          (out.earlier ? " (" + out.earlier + " earlier, kept)" : "") + ". ";
        msg.textContent = "Saved: " + name + " calibration " + out.state.words + ".";
      } catch (e) { msg.textContent = e.message; }
      finally { save.disabled = false; }
    });
    li.append(h("div", { class: "cal-editor" }, said,
      h("div", { class: "cal-row" }, field("Calibrated on", calibrated), field("Due again", due), field("Certificate", cert), field("Calibrated by", by)),
      h("div", { class: "cal-row" }, field("Certificate file: a PDF, or a photo of it (optional)", file)),
      h("div", { class: "lim-foot" }, save, msg)));
  }
  async function openInstrument(name, what) {
    try { await api("/api/instruments/" + encodeURIComponent(name) + "/open", { body: { what } }); } catch (e) { $("lab-msg").textContent = e.message; }
  }
  async function fetchManual(name) {
    $("lab-msg").textContent = "Fetching the " + name + " manual from its maker…";
    try {
      const r = await api("/api/instruments/" + encodeURIComponent(name) + "/manual", { body: {} });
      renderFolders(r.folders);
      $("lab-msg").textContent = "Got the " + name + " manual (" + (r.manual.pages || "?") + " pages).";
    } catch (e) { $("lab-msg").textContent = e.message; }
  }
  function choosePdf(name) {
    const input = $("lab-pdf");
    input.value = "";
    input.onchange = async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      $("lab-msg").textContent = "Reading " + file.name + "…";
      try {
        const res = await fetch("/api/instruments/" + encodeURIComponent(name) + "/manual",
          { method: "PUT", headers: { "X-Kelvin-Token": token, "Content-Type": "application/pdf" }, body: file });
        const r = await res.json();
        if (!res.ok) throw new Error(r.error || "HTTP " + res.status);
        renderFolders(r.folders);
        $("lab-msg").textContent = "Kelvin will use " + file.name + " as the " + name + " manual (" + (r.manual.pages || "?") + " pages).";
      } catch (e) { $("lab-msg").textContent = e.message; }
    };
    input.click();
  }
  async function setupAgain() {
    try {
      const r = await api("/api/instruments/setup", { body: {} });
      showSetup(r, false);
      pollState();
    } catch (e) { $("lab-msg").textContent = e.message; }
  }
  async function unlockLab() {
    if (!(await askYes("Mark the limits reviewed?\n\nYou have checked the limits in the lab config of the instruments " +
        "Kelvin added: what each output may reach (voltage, current, field, temperature, power) is what your sample " +
        "and setup can take. Kelvin then treats them like your other instruments: it still asks before driving " +
        "harder, unless auto mode or the project envelope covers the change.", { ok: "Mark reviewed", focus: "cancel" }))) return;
    await connectLab({ reviewed: true });
    refreshLabSetup();
  }
  // what setting up changed (status, marks, folders, the lab config Kelvin wrote), without touching the rest
  // ------------------------------------------------------------------ the lab config, instrument by instrument
  const LABCFG = { view: store("lh.labView") === "file" ? "file" : "instruments", open: null, find: "" };
  const ENTRY_NAME = /^\s+(["']?)([^"':#\s][^"':#]*?)\1\s*:/;
  function labEntries(text) {   // each instrument's entry as written, by line: [{name, start, end}] (as discovery.py finds it)
    const lines = text.split("\n"), entries = [];
    const top = lines.findIndex((l) => /^instruments\s*:\s*(#.*)?$/.test(l));
    let indent = null;
    for (let i = top < 0 ? lines.length : top + 1; i < lines.length; i++) {
      const l = lines[i];
      if (!l.trim() || /^\s*#/.test(l)) continue;
      const depth = l.length - l.trimStart().length;
      if (depth === 0) break;   // the lab config's next part (interlocks, watches)
      if (indent === null) indent = depth;
      const m = depth === indent && l.match(ENTRY_NAME);
      if (!m) continue;
      let end = i + 1;
      while (end < lines.length && (!lines[end].trim() || lines[end].length - lines[end].trimStart().length > indent)) end++;
      while (end > i + 1 && !lines[end - 1].trim()) end--;
      entries.push({ name: m[2].trim(), start: i, end });
      i = end - 1;
    }
    return { lines, entries };
  }
  function entryHead(name, text) {   // an entry's name, card and address, written as a block or on one line
    const pick = (key) => {
      const m = text.match(new RegExp("(?:^|[\\s{,])" + key + "\\s*:\\s*(\"[^\"]*\"|'[^']*'|[^,}\\n#]+)", "m"));
      return m ? m[1].trim().replace(/^["']|["']$/g, "") : "";
    };
    const card = pick("card"), address = pick("address");
    return [h("b", {}, name), h("span", { class: "entry-card" }, card || "no card"),
      address ? h("code", { class: "entry-addr", title: address }, address) : null];
  }
  function renderLabConfig() {
    const { lines, entries } = labEntries($("lab-yaml").value);
    const byInstrument = LABCFG.view === "instruments" && entries.length > 0;
    for (const b of document.querySelectorAll("#lab-view button")) b.classList.toggle("on", b.dataset.view === (byInstrument ? "instruments" : "file"));
    $("lab-view").hidden = !entries.length;
    $("lab-yaml").hidden = byInstrument;
    $("lab-find").hidden = $("lab-entries").hidden = !byInstrument;
    if (!byInstrument) return;
    const q = LABCFG.find.trim().toLowerCase(), rows = [];
    for (const e of entries) {
      const own = lines.slice(e.start, e.end).join("\n");
      if (q && !(e.name + "\n" + own).toLowerCase().includes(q)) continue;
      const open = LABCFG.open === e.name;
      const head = h("button", { class: "entry-head", type: "button", "aria-expanded": String(open) }, ...entryHead(e.name, own));
      head.addEventListener("click", () => { LABCFG.open = open ? null : e.name; renderLabConfig(); });
      const row = h("div", { class: "lab-entry" + (open ? " open" : "") }, head);
      if (open) {
        const box = h("textarea", { class: "mono entry-text", spellcheck: "false", rows: String(Math.min(18, e.end - e.start + 1)),
          "aria-label": e.name + " in the lab config" });
        box.value = own;
        const at = { start: e.start, count: e.end - e.start };   // where this entry is in the whole file while it is edited
        box.addEventListener("input", () => {
          const all = $("lab-yaml").value.split("\n"), mine = box.value.split("\n");
          all.splice(at.start, at.count, ...mine);
          at.count = mine.length;
          $("lab-yaml").value = all.join("\n");
          const m = mine[0].match(ENTRY_NAME);
          if (m) LABCFG.open = m[2].trim();   // renamed: it stays open
          head.replaceChildren(...entryHead(m ? m[2].trim() : e.name, box.value));
        });
        row.append(box);
      }
      rows.push(row);
    }
    $("lab-entries").replaceChildren(...(rows.length ? rows
      : [h("div", { class: "muted small entry-none" }, "No instrument matches “" + LABCFG.find.trim() + "”.")]));
  }

  async function refreshLabSetup(folders = true) {
    let r;
    try { r = await api("/api/lab"); } catch (e) { return; }
    showLabState(r.info, r.name);
    showSetup(r.setup, r.unreviewed);
    if (folders) {
      renderFolders(r.folders || []);
      if (LAB.scripts) {
        const at = new Map((r.folders || []).map((f) => [f.address, f.name]));
        for (const it of LAB.scripts.instruments) it.in_lab = at.get(it.address) || null;
        renderScripts();
      }
    }
    const editing = document.activeElement && (document.activeElement === $("lab-yaml") || document.activeElement.classList.contains("entry-text"));
    if (!editing) { $("lab-yaml").value = r.yaml || ""; renderLabConfig(); }
  }

  async function openLabSetup() {
    let r;
    try { r = await api("/api/lab"); } catch (e) { toast(e.message); return; }
    LAB.cards = r.cards;
    showLabState(r.info, r.name);
    showSetup(r.setup, r.unreviewed);
    renderFolders(r.folders || []);
    $("lab-card").replaceWith(Object.assign(cardSelect("", "Card…"), { id: "lab-card" }));
    $("lab-yaml").value = r.yaml || "";
    LABCFG.open = null;
    $("lab-find").value = LABCFG.find = "";
    renderLabConfig();
    $("lab-found").hidden = $("lab-add-found").hidden = $("lab-survey").hidden = true;
    renderScripts();   // what the lab's scripts showed, kept while the portal is open
    $("lab-health").hidden = true;
    $("lab-msg").textContent = r.info && r.info.error ? r.info.error : "";
    $("lab-dialog").showModal();
  }
  async function scanLab() {
    const btn = $("lab-scan");
    btn.disabled = true; btn.textContent = "Scanning…";
    $("lab-msg").textContent = "Asking each instrument who it is (a few seconds per port)…";
    let r;
    try { r = await api("/api/instruments/scan", { body: {} }); } catch (e) { $("lab-msg").textContent = e.message; return; }
    finally { btn.disabled = false; btn.textContent = "Scan again"; }
    LAB.found = r.found;
    const known = r.found.some((f) => f.card_id && !f.in_lab);
    const why = r.survey && r.survey.reason;
    const lines = (r.survey && r.survey.lines) || [];
    $("lab-msg").textContent = known || why ? "" : "Nothing found.";
    $("lab-survey-why").textContent = why || "";
    $("lab-survey-lines").replaceChildren(...lines.map((l) => h("li", {}, l)));
    $("lab-survey").hidden = known || !(why || lines.length);
    renderFound();
  }
  function renderFound() {
    const box = $("lab-found");
    box.hidden = !LAB.found.length;
    $("lab-add-found").hidden = !LAB.found.some((f) => !f.in_lab);
    box.replaceChildren(...LAB.found.map((f) => {
      if (f.in_lab) {
        return h("div", { class: "found in-lab" }, h("span", { class: "found-name" }, f.in_lab), h("span", { class: "found-model" }, "already in this lab"),
          h("code", { class: "found-addr" }, f.address));
      }
      const check = h("input", { type: "checkbox", "aria-label": "Add " + f.address });
      check.checked = !!f.card_id;
      const name = h("input", { class: "found-input", value: f.suggested || "", placeholder: "name", spellcheck: "false", "aria-label": "Name" });
      const model = f.card_id ? h("span", { class: "found-model", title: f.idn || f.how }, f.model)
        : cardSelect("", f.idn ? "Answered, but no card matches: choose one…" : "No answer: choose its card…");
      if (!f.card_id) model.addEventListener("change", () => {
        check.checked = !!model.value;
        const card = LAB.cards.find((c) => c.id === model.value);
        if (card && !name.value) name.value = KIND_NAME[card.kind] || "instr";
      });
      f.ui = { check, name, model };
      const row = h("label", { class: "found" }, check, name, model, h("code", { class: "found-addr", title: f.settings || "" }, f.address));
      if (f.card_id || !f.idn) return row;
      // it said who it is, but Kelvin has no card: Kelvin can find its manual and write one (a draft)
      const write = h("button", { class: "link", type: "button" }, "Find its manual and write its card");
      write.addEventListener("click", (e) => { e.preventDefault(); writeCardFor(f, write); });
      return h("div", { class: "found-wrap" }, row, h("div", { class: "found-write" }, write,
        h("span", { class: "muted small", title: f.idn }, f.collection ? f.model + " (the instrument collection knows it)"
          : f.idn.split(",").slice(0, 2).join(" "))));
    }));
  }
  async function writeCardFor(f, btn) {
    btn.disabled = true;
    btn.textContent = "Finding its manual and writing the card (a minute or two)…";
    try {
      const r = await api("/api/instruments/card", { body: { address: f.address, idn: f.idn } });
      LAB.cards = (await api("/api/lab")).cards;
      Object.assign(f, { card_id: r.card_id, model: r.model, kind: r.kind, how: "card written by Kelvin" });
      if (!f.suggested) f.suggested = KIND_NAME[r.kind] || "instr";
      renderFound();
      $("lab-msg").textContent = "Kelvin wrote a draft card, " + r.card_id + ", from " + r.manual_url + ".";
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Find its manual and write its card";
      $("lab-msg").textContent = e.message;
    }
  }
  async function addToConfig(items, names) {
    if (!items.length) { $("lab-msg").textContent = "Nothing chosen."; return; }
    try {
      const r = await api("/api/instruments/draft", { body: { items, base: $("lab-yaml").value, names: names || null } });
      $("lab-yaml").value = r.yaml;
      renderLabConfig();
      const already = r.already || [], there = new Set(already.map((a) => a.item));
      const added = items.filter((i, k) => !there.has(k)).map((i) => i.name);
      $("lab-msg").textContent = [added.length ? "Added " + added.join(", ") + ". Review the limits, then save and connect." : "",
        already.length ? already.map((a) => a.name + " (" + a.address + ")").join(", ") + (already.length === 1 ? " is" : " are") + " in the lab config already." : ""].filter(Boolean).join(" ");
    } catch (e) { $("lab-msg").textContent = e.message; }
  }
  // ------------------------------------------------------------------ setting up from the lab's own scripts
  // the folder chosen joins the lab's scripts folders, and they are read together
  async function fromScripts() {
    const folder = await pickFolder(LAB.scriptsFolder || "", "The folder with your lab's scripts (Python, notebooks, MATLAB):");
    if (folder) await surveyScripts(folder);
  }
  async function surveyScripts(folder) {
    LAB.scriptsFolder = folder;
    const btn = $("lab-scripts"), label = btn.textContent;
    btn.disabled = true; btn.textContent = "Reading your scripts…";
    $("lab-msg").textContent = "Reading your scripts (never running them), then asking each instrument who it is…";
    try {
      LAB.scripts = await api("/api/instruments/scripts", { body: { folder } });
      renderScripts();
      const n = LAB.scripts.instruments.length, s = LAB.scripts.scripts.length;
      $("lab-msg").textContent = n ? "Found " + n + " instrument" + (n === 1 ? "" : "s") + " in " + s + " script" + (s === 1 ? "" : "s") +
        ". Check the cards and limits, then add the ones you want." : "No instrument work in the scripts there.";
    } catch (e) { $("lab-msg").textContent = e.message; }
    finally { btn.disabled = false; btn.textContent = label; }
  }
  // ------------------------------------------------------------------ setting up from the lab's LabVIEW
  // NI MAX and the folder's projects and VIs, read (no VI is run) into the same review as the scripts', and each VI
  // read translated into a scan to keep in the person's library
  async function fromLabview() {
    const folder = await pickFolder(store("lh.labviewFolder") || "", "Choose the LabVIEW folder");
    if (!folder) return;
    store("lh.labviewFolder", folder);
    const btn = $("lab-labview");
    btn.disabled = true; btn.textContent = "Reading your LabVIEW…";
    $("lab-msg").textContent = "Reading NI MAX and the LabVIEW in " + folder + " (never running a VI), then asking each instrument who it is…";
    try {
      LAB.scripts = await api("/api/instruments/labview", { body: { folder } });
      renderScripts();
      const n = LAB.scripts.instruments.length, k = LAB.scripts.vis.filter((v) => v.scan && v.scan.text).length;
      $("lab-msg").textContent = (n ? "Found " + n + " instrument" + (n === 1 ? "" : "s") + ". Check the cards and limits, then add the ones you want."
        : "No instruments in NI MAX or the LabVIEW there.") + (k === 1 ? " 1 VI can become a scan." : k ? " " + k + " VIs can become scans." : "");
    } catch (e) { $("lab-msg").textContent = e.message; }
    finally { btn.disabled = false; btn.textContent = "From your LabVIEW…"; }
  }
  // ------------------------------------------------------------------ setting up from the lab's Special Measure
  // A setup script's rack (its instruments, their named channels with limits and ramp rates), read (no MATLAB is run)
  // into the same review; its channels' names go into the lab config (names:), its scans become scans to keep, its
  // run files runs to import
  async function fromSpecialMeasure(setup) {
    const folder = typeof setup === "string" && LAB.scripts && LAB.scripts.kind === "specialmeasure" ? LAB.scripts.folder
      : await pickFolder(store("lh.smFolder") || "", "Choose the Special Measure folder");
    if (!folder) return;
    store("lh.smFolder", folder);
    const btn = $("lab-sm");
    btn.disabled = true; btn.textContent = "Reading Special Measure…";
    $("lab-msg").textContent = "Reading the setup scripts, drivers and scans in " + folder + " (never running MATLAB), then asking each instrument who it is…";
    try {
      LAB.scripts = await api("/api/instruments/specialmeasure", { body: { folder, setup: typeof setup === "string" ? setup : null } });
      renderScripts();
      const r = LAB.scripts, n = r.instruments.length, k = r.scans.filter((s) => s.text).length;
      $("lab-msg").textContent = (n ? "The rack of " + r.setup + ": " + n + " instrument" + (n === 1 ? "" : "s") + ". Check the cards and limits, then add the ones you want."
        : "No setup script (smloadinst, smaddchannel) there.") + (k === 1 ? " 1 scan translates." : k ? " " + k + " scans translate." : "");
    } catch (e) { $("lab-msg").textContent = e.message; }
    finally { btn.disabled = false; btn.textContent = "Special Measure…"; }   // its label, as the page has it
  }
  function smHead(r) {   // In C:\SM: smsetup_optics1.m ▾ · 31 instruments · 12 scans · 16484 runs; none run
    const runs = Object.values(r.runs || {}).reduce((a, b) => a + b, 0);
    const pick = h("select", { "aria-label": "The setup script" }, ...r.setups.map((s) => h("option", { value: s.path }, s.path)));
    pick.value = r.setup;
    pick.addEventListener("change", () => fromSpecialMeasure(pick.value));
    return h("p", { class: "hint sm-head" }, "In " + r.folder + ": ", r.setups.length > 1 ? pick : h("code", {}, r.setup || "no setup script"),
      " · " + r.scans.length + " scan" + (r.scans.length === 1 ? "" : "s") + " · " + runs + " run" + (runs === 1 ? "" : "s") + "; none run.");
  }
  function smChannels(it) {   // Vbg → V [-10, 10] at 1/s · NP2X → ao0 [0, 200] ×0.05 −5 · …
    return (it.channels || []).map((c) => {
      // what the instrument gets of the channel's value: sm's multiplier and its driver's (none: the card's own unit)
      const times = c.native || !c.param ? 1 : (c.factor == null ? 1 : c.factor) * (c.scale == null ? 1 : c.scale);
      const plus = c.native || !c.param ? 0 : c.offset || 0;
      return c.name + (c.param ? " → " + c.param : " (not on the card)") +
        (c.low != null || c.high != null ? " [" + (c.low == null ? "−∞" : c.low) + ", " + (c.high == null ? "∞" : c.high) + "]" : "") +
        (c.rate != null ? " at " + c.rate + "/s" : "") + (times !== 1 ? " ×" + +times.toPrecision(6) : "") +
        (plus ? (plus < 0 ? " −" : " +") + Math.abs(plus) : "");
    }).join(" · ");
  }
  // Kelvin's own words, never a lab's name (labconfig.NOT_NAMES)
  const NOT_NAMES = new Set(["i", "t", "time", "repeat", "stabilizing", "gap_s", "on", "off", "true", "false", "and", "or",
    "not", "if", "else", "pi", "col"]);
  function smNames() {   // each checked (or in-lab) instrument's channels: their sm names for instrument.parameter
    const r = LAB.scripts, names = {}, taken = new Set(NOT_NAMES);
    for (const it of r.instruments) taken.add(it.in_lab || (it.ui ? it.ui.name.value.trim() : it.name));
    for (const it of r.instruments) {
      const name = it.in_lab || (it.ui && it.ui.check.checked ? it.ui.name.value.trim() : "");
      if (!name || (!it.in_lab && it.ui.model.value !== (it.card || {}).id)) continue;   // another card: other parameters
      for (const c of it.channels || []) if (c.param && /^[A-Za-z_]\w*$/.test(c.name) && !taken.has(c.name)) names[c.name] = name + "." + c.param;
    }
    return names;
  }
  function renderSm() {   // the function channels, the scans to keep, the runs to import
    const r = LAB.scripts, box = $("lab-sm-extra");
    const sm = !!r && r.kind === "specialmeasure";
    box.hidden = !sm;
    if (!sm) return;
    const rows = [];
    if (r.functions.length) rows.push(h("p", { class: "hint" }, "Function channels: " + r.functions.map((f) =>
      f.variables.join(", ") + " → " + f.sets.join(", ")).join("; ") + " (their scans become scans with Computed settings)."));
    for (const s of r.scans) {
      const can = !!s.text;
      const check = h("input", { type: "checkbox", "aria-label": "Keep " + s.name + " as a scan", disabled: !can });
      check.checked = s.ui ? s.ui.check.checked : can && !s.gaps.length;
      s.ui = { check };
      const how = h("details", { class: "lv-how" }, h("summary", {}, can ? "Plan" : "Why no scan"),
        can ? h("pre", { class: "mono lv-plan" }, s.text) : h("p", { class: "muted small" }, s.gaps.join("; ")));
      rows.push(h("div", { class: "found-wrap" },
        h("label", { class: "found lv-vi" }, check, h("span", { class: "found-name", title: s.path }, s.name),
          h("span", { class: "found-model", title: s.path }, can ? s.summary : "no scan"),
          h("span", { class: "muted small" }, s.source === "run" ? (s.runs > 1 ? "ran " + s.runs + "×" : "ran") : "saved")),
        can && s.gaps.length ? h("div", { class: "found-write muted small" }, "Not translated: " + s.gaps.join("; ")) : null,
        h("div", { class: "found-write" }, how)));
    }
    const folders = Object.entries(r.runs || {});
    if (folders.length) {
      rows.push(h("details", { class: "found-wrap", open: folders.length <= 6 }, h("summary", {}, "Runs to import"),
        ...folders.map(([folder, n]) => {
          const go = h("button", { class: "btn small", type: "button" }, "Import");
          const said = h("span", { class: "muted small" });
          go.addEventListener("click", () => importSmRuns(folder, go, said));
          return h("div", { class: "found sm-runs" }, h("code", { class: "found-name" }, folder), h("span", { class: "found-model" }, n + " run" + (n === 1 ? "" : "s")), go, said);
        })));
    }
    box.replaceChildren(...rows);
  }
  async function importSmRuns(folder, btn, said) {
    btn.disabled = true;
    said.textContent = "Importing…";
    try {
      const k = await api("/api/instruments/specialmeasure/runs", { body: { folder: LAB.scripts.folder, runs: [folder] } });
      said.textContent = [k.imported.length ? k.imported.length + " imported" : "", k.left ? k.left + " already" : "",
        k.failed.length ? k.failed.length + " not (" + k.failed.slice(0, 2).map((f) => f.file + ": " + f.why).join("; ") + ")" : "",
        k.more ? k.more + " more: Import again" : ""].filter(Boolean).join(" · ") || "nothing new";
    } catch (e) { said.textContent = e.message; }
    finally { btn.disabled = false; }
  }
  async function keepSmScans() {   // the checked scans, translated with the names and cards chosen above
    const r = LAB.scripts;
    const scans = r.scans.filter((s) => s.ui && s.ui.check.checked).map((s) => ({ path: s.path, index: s.index || 0 }));
    if (!scans.length) { $("lab-msg").textContent = "No scan checked."; return; }
    const instruments = r.instruments.filter((it) => !it.in_lab && it.ui && it.ui.check.checked).map((it) => ({
      rack: it.rack_name, name: it.ui.name.value.trim(), card: it.ui.model.value }));
    const btn = $("lab-keep-scans");
    btn.disabled = true;
    try {
      const k = await api("/api/instruments/specialmeasure/scans", { body: { folder: r.folder, setup: r.setup, scans, instruments } });
      if (k.library) SCANS.data = k.library;
      $("lab-msg").textContent = [k.kept.length ? "Kept " + andList(k.kept.map((x) => x.name)) + " in Sequence → Scans." : "",
        ...k.not.map((x) => x.scan + ": " + x.gaps.join("; "))].filter(Boolean).join(" ");
    } catch (e) { $("lab-msg").textContent = e.message; }
    finally { btn.disabled = false; }
  }
  function labviewHead(r) {   // In C:\Lab: 2 projects, 14 VIs, 3 NI MAX aliases; none run.
    const count = (n, one) => n + " " + one + (n === 1 ? "" : one.endsWith("s") ? "es" : "s");
    const parts = [count(r.projects.length, "project"), count(r.vis.length, "VI")];
    const aliases = Object.keys((r.max && r.max.aliases) || {}).length, daq = ((r.max && r.max.daqmx) || []).length;
    if (aliases) parts.push(count(aliases, "NI MAX alias"));
    if (daq) parts.push(count(daq, "NI-DAQmx device"));
    if (!(r.max && r.max.files && r.max.files.length)) parts.push("no NI MAX file on this computer");
    return "In " + r.folder + ": " + parts.join(", ") + "; none run.";
  }
  function renderVis() {   // the VIs: each one read with its scan, to keep; the others by name
    const r = LAB.scripts, box = $("lab-vis");
    const vis = r && r.kind === "labview" ? r.vis || [] : [];
    box.hidden = !vis.length;
    if (!vis.length) return;
    const rows = vis.filter((v) => v.scan).map((v) => {
      const s = v.scan, can = !!s.text;
      const check = h("input", { type: "checkbox", "aria-label": "Keep " + v.name + " as a scan", disabled: !can });
      check.checked = v.ui ? v.ui.check.checked : can && !s.gaps.length;
      v.ui = { check };
      const how = h("details", { class: "lv-how" }, h("summary", {}, can ? "How it translates" : "Why no scan"),
        h("ul", { class: "why-lines" }, ...s.mapping.map((m) => h("li", {}, m.vi + " → " + m.plan))),
        can ? h("pre", { class: "mono lv-plan" }, s.text) : null);
      return h("div", { class: "found-wrap" },
        h("label", { class: "found lv-vi" }, check, h("span", { class: "found-name", title: v.path }, v.name),
          h("span", { class: "found-model", title: s.summary }, can ? s.summary : "no scan"), translateButton(v)),
        s.gaps.length ? h("div", { class: "found-write muted small" }, "Not translated: " + s.gaps.join("; ")) : null,
        h("div", { class: "found-write" }, how));
    });
    const unread = vis.filter((v) => v.read === "no"), plain = vis.filter((v) => !v.scan && v.read !== "no");
    for (const [list, said] of [[unread, "not read yet"], [plain, "read, no scan"]]) {
      if (!list.length) continue;
      rows.push(h("details", { class: "found-wrap lv-names" },
        h("summary", {}, list.length + " VI" + (list.length === 1 ? "" : "s") + ", " + said),
        h("ul", { class: "why-lines" }, ...list.map((v) => h("li", { title: v.path },
          v.name + (list === plain && v.unread.length ? " (" + v.unread.join("; ") + ")" : ""),
          list === plain ? translateButton(v) : null)))));
    }
    box.replaceChildren(...rows);
  }
  async function keepScans() {   // the checked VIs' scans, translated with the names and cards chosen above
    const r = LAB.scripts;
    const vis = (r.vis || []).filter((v) => v.ui && v.ui.check.checked).map((v) => v.path);
    if (!vis.length) { $("lab-msg").textContent = "No VI checked."; return; }
    const instruments = r.instruments.filter((it) => !it.in_lab && it.ui).map((it) => ({
      address: it.address || it.ui.addr.value.trim(), name: it.ui.name.value.trim(), card: it.ui.model.value }));
    const btn = $("lab-keep-scans");
    btn.disabled = true;
    try {
      const k = await api("/api/instruments/labview/scans", { body: { folder: r.folder, vis, instruments } });
      if (k.library) SCANS.data = k.library;
      $("lab-msg").textContent = [k.kept.length ? "Kept " + andList(k.kept.map((x) => x.name)) + " in Sequence → Scans." : "",
        ...k.not.map((x) => x.vi + ": " + x.gaps.join("; "))].filter(Boolean).join(" ");
    } catch (e) { $("lab-msg").textContent = e.message; }
    finally { btn.disabled = false; }
  }
  function renderScripts() {
    const r = LAB.scripts, box = $("lab-from-scripts");
    const labview = !!r && r.kind === "labview", sm = !!r && r.kind === "specialmeasure";
    const scans = labview ? r.vis.some((v) => v.scan && v.scan.text) : sm && r.scans.some((s) => s.text);
    box.hidden = !r || (!r.instruments.length && !labview && !sm);
    $("lab-scripts-actions").hidden = !r || (!r.instruments.length && !(labview || sm ? scans : r.scripts.length));
    $("lab-add-scripts").hidden = !r || !r.instruments.some((it) => !it.in_lab);
    $("lab-recipes").hidden = !r || labview || sm || !r.scripts.length;
    $("lab-keep-scans").hidden = !scans;
    $("lab-keep-scans").textContent = sm ? "Keep the checked scans" : "Keep the checked VIs as scans";
    renderVis();
    renderSm();
    if (!r) return;
    const span = (lim) => Object.entries(lim || {}).map(([p, v]) => p + " " + v[0] + " to " + v[1]).join(", ");
    const head = sm ? smHead(r) : h("p", { class: "hint" }, labview ? labviewHead(r) : "In " + r.folder + ": " + r.scripts.length + " script" + (r.scripts.length === 1 ? "" : "s") + " read, none run.");
    box.replaceChildren(head,
      ...r.instruments.map((it) => {
        if (it.in_lab) {
          const row = h("div", { class: "found in-lab" }, h("span", { class: "found-name" }, it.in_lab),
            h("span", { class: "found-model" }, "already in this lab"), h("code", { class: "found-addr" }, it.address));
          const lim = it.card && Object.keys(it.card.limits || {}).length ? it.card.limits : null;
          if (!lim) return row;
          const use = h("button", { class: "link soft", type: "button" }, "Use these limits");
          const note = h("div", { class: "found-write muted small" }, (sm ? "Limits from the rack: " : "Limits from the values your " + (labview ? "VIs" : "scripts") + " use: ") + span(lim) + " ", use);
          use.addEventListener("click", async () => {   // a person's choice, like Save limits in Limits…
            try {
              await api("/api/instruments/" + encodeURIComponent(it.in_lab) + "/limits", { body: { limits: lim } });
              note.textContent = "Kelvin holds " + it.in_lab + " to " + span(lim) + " from now on.";
              refreshLabSetup(false);
            } catch (e) { note.textContent = e.message; }
          });
          return h("div", { class: "found-wrap" }, row, note);
        }
        const card = it.card || null;
        const was = it.ui && { check: it.ui.check.checked, name: it.ui.name.value, card: it.ui.model.value,
          addr: it.address ? "" : it.ui.addr.value };   // what the person changed, kept when the list is drawn again
        const check = h("input", { type: "checkbox", "aria-label": "Add " + (it.address || it.name) });
        check.checked = was ? was.check : !!(card && card.id && it.address && it.used !== false);
        const name = h("input", { class: "found-input", value: was ? was.name : it.name || "", placeholder: "name", spellcheck: "false", "aria-label": "Name" });
        const model = cardSelect(was ? was.card : card ? card.id : "", "Choose its card…");
        model.addEventListener("change", () => { if (model.value && (it.address || it.ui.addr.value.trim())) check.checked = true; });
        const addr = it.address ? h("code", { class: "found-addr", title: it.where.join(", ") }, it.address)
          : h("input", { class: "found-input found-addr-in", placeholder: "its address", spellcheck: "false", "aria-label": "Address",
            title: labview ? "The VIs set it" : "The scripts set it as " + (it.expression || "?"), value: was ? was.addr : "" });
        it.ui = { check, name, model, addr };
        const why = [sm ? it.model + (it.used === false ? ", not loaded by " + r.setup : "") : "",
          card ? card.why : "no card of Kelvin's fits: choose one, or make one under Cards" + (it.driver ? " (from its driver " + it.driver.split(/[\\/]/).pop() + ")" : ""),
          it.idn && !(card && card.why.includes("answered")) ? "answered " + it.idn.slice(0, 60) : it.asked || "",
          card && Object.keys(card.limits || {}).length ? (sm ? "limits: " : "limits from the values used: ") + span(card.limits) : "",
          it.address ? "" : sm ? "no address in the rack" + ((it.helpers || []).length ? " (" + it.helpers.join(", ") + " connects it)" : "") + ": type it"
            : "address set at run time (" + (it.expression || "?") + "): type it",
          sm ? "" : "in " + it.where.slice(0, 2).join(", ")].filter(Boolean).join(" · ");
        return h("div", { class: "found-wrap" }, h("label", { class: "found" }, check, name, model, addr),
          h("div", { class: "found-write muted small" }, why),
          sm && (it.channels || []).length ? h("div", { class: "found-write muted small sm-chans" }, smChannels(it)) : null);
      }));
  }
  function addScripts() {
    const items = [], kind = (LAB.scripts || {}).kind, labview = kind === "labview", sm = kind === "specialmeasure";
    for (const it of (LAB.scripts || {}).instruments || []) {
      if (!it.ui || !it.ui.check.checked) continue;
      const address = it.address || it.ui.addr.value.trim(), card = it.ui.model.value;
      if (!address || !card) { $("lab-msg").textContent = (it.ui.name.value || "An instrument") + " needs its address and its card."; return; }
      const same = it.card && it.card.id === card;   // the rack's limits and rates are on the card it was read for
      items.push({ name: it.ui.name.value.trim() || "instr", card, address, connection: it.connection,
        limits: same ? it.card.limits : null, rates: sm && same ? it.card.rates : null,
        from: labview ? "labview" : sm ? "specialmeasure" : null,
        note: (it.idn || (sm ? it.model + " in your rack" : "") || (it.card && it.card.why) || (labview ? "from your LabVIEW" : "from your scripts")).slice(0, 60) });
    }
    addToConfig(items, sm ? smNames() : null);
  }
  async function recipesFromScripts() {   // in a new chat: Kelvin proposes a recipe per script, and keeps each one you agree to
    const folder = LAB.scripts && LAB.scripts.folder;
    if (!folder) return;
    const several = (LAB.scripts.folders || []).length > 1;   // the lab's scripts folders, read together
    $("lab-dialog").close();
    if (!(await newChat())) return;   // the person kept their incognito chat: the request is not put in it
    $("input").value = "Turn the measurement scripts in " + (several ? "my lab's scripts folders" : folder) + " into recipes: " +
      "read them with survey_scripts" + (several ? " (no path: every folder)" : "") + ", " +
      "then propose a recipe for each measurement script, with this lab's instruments, and keep each with " +
      "save_recipe once I agree. Say what a recipe can't carry over.";
    autosize();
    updateSend();
    $("input").focus();
  }
  function addFound() {
    addToConfig(LAB.found.filter((f) => f.ui && f.ui.check.checked).map((f) => ({
      name: f.ui.name.value.trim() || "instr", card: f.card_id || f.ui.model.value, address: f.address,
      note: (f.idn || "").slice(0, 60) })));
  }
  function addByAddress() {
    const address = $("lab-addr").value.trim(), cardId = $("lab-card").value;
    if (!address || !cardId) { $("lab-msg").textContent = "Give an address and choose its card."; return; }
    const card = LAB.cards.find((c) => c.id === cardId);
    addToConfig([{ name: KIND_NAME[card.kind] || "instr", card: cardId, address }]).then(() => { $("lab-addr").value = ""; });
  }
  async function connectLab(body) {
    const save = $("lab-save");
    save.disabled = true;
    $("lab-msg").textContent = "Connecting and checking every instrument…";
    $("lab-health").hidden = true;
    try {
      const r = await api("/api/lab", { body });
      showLabState(r.info, r.name);
      const bad = r.health.filter((line) => /FAIL/.test(line));
      $("lab-health").replaceChildren(...r.health.map((line) => h("li", { class: /FAIL/.test(line) ? "bad" : "good" },
        icon(/FAIL/.test(line) ? "warn" : "check"), h("span", {}, line))));
      $("lab-health").hidden = !r.health.length;
      $("lab-msg").textContent = bad.length ? bad.length + " instrument(s) need attention before use."
        : body.reviewed && !r.health.length ? "Marked reviewed: Kelvin treats them like your other instruments now."
        : "Connected to " + r.name + ". Every instrument answered as expected.";
      pollState();
      refreshLabSetup();   // each instrument's row (its Limits…), and the scripts' instruments now in the lab
    } catch (e) {
      $("lab-msg").replaceChildren(h("span", {}, e.message), ...installOffer(e.data && e.data.install));
    } finally { save.disabled = false; }
  }

  // A maker's package the lab's instruments need is missing (envs.py): install it (the person's click is their yes),
  // then connect the lab again.
  function installOffer(install, after) {
    if (!install || !install.packages || !install.packages.length) return [];
    const names = andList(install.packages);
    const button = h("button", { class: "link", type: "button" }, "Install " + names);
    const said = h("span", { class: "install-said" });
    button.addEventListener("click", async () => {
      button.disabled = true;
      said.textContent = " Installing…";
      try {
        const r = await api("/api/lab/install", { body: install });
        said.textContent = " " + r.message;
        if (r.ok) await (after ? after() : connectLab({ use: "mine" }));   // Kelvin connects what it was for
        else button.disabled = false;
      } catch (e) {
        said.textContent = " " + e.message;
        button.disabled = false;
      }
    });
    return [" ", button, said];
  }

  // ------------------------------------------------------------------ approval mode (a person's choice)
  function showMode(mode, rule) {
    const select = $("approval-mode");
    if (rule) S.approvalRule = rule;
    if (document.activeElement !== select) select.value = mode === "auto" ? "auto" : "ask";
    $("mode-box").classList.toggle("auto", mode === "auto");
    $("mode-box").title = mode === "auto" ? "Auto mode: " + (S.approvalRule || "low-risk changes run without asking")
      : "Kelvin asks you before anything drives the sample harder";
  }
  async function changeMode(mode) {
    if (mode === "auto" && !(await askYes("Turn on auto mode?\n\nKelvin then approves low-risk changes for you: " +
        (S.approvalRule || "drives that stay well inside the lab limits, after a clean dry run.") +
        "\n\nEverything else still waits for you. " +
        "Auto mode switches off when Kelvin restarts.", { ok: "Turn on", focus: "cancel" }))) {
      $("approval-mode").value = "ask";   // the question's close gives the select its focus back, where showMode leaves it
      showMode("ask");
      return;
    }
    try {
      const r = await api("/api/mode", { body: { mode } });
      showMode(r.mode, r.rule);
      toast(r.mode === "auto" ? "Auto mode on: low-risk changes run without asking." : "Kelvin will ask before every drive change.");
    } catch (e) { toast(e.message); pollState(); }
  }

  function toggleLab(open) {
    S.labOpen = open === undefined ? !S.labOpen : open;
    $("labpanel").hidden = !S.labOpen;
    $("toggle-lab").setAttribute("aria-pressed", String(S.labOpen));
    $("toggle-lab").classList.toggle("on", S.labOpen);
    fitLayout();
    if (S.labOpen) pollState();
  }

  // ------------------------------------------------------------------ terminal pane: a person's own commands
  // Type to Kelvin in text, or switch to an instrument and send it its own commands (SCPI...). They go through the
  // same safety gate as Kelvin's: limits, interlocks and blocked commands apply, and a drive change waits for an
  // approval, answered here with y/n. The server logs them as the person's, never as Kelvin's.
  const TERM = { open: false, target: null, chatId: null, history: [], hi: 0, busy: false, ask: null, askAt: 0,
                 asked: new Set(), reply: null, progress: new Map() };
  const TERM_HELP = [
    "Type to Kelvin, or switch to an instrument and type its own commands (*IDN?, SENS?, :SOUR:VOLT 1 ...).",
    "Instrument commands go through the safety gate: limits, interlocks and blocked commands apply,",
    "and a change that drives the sample harder asks you first (y/n).",
    "",
    "  /use <instrument>    send what you type to that instrument (or just /<instrument>)",
    "  /kelvin              talk to Kelvin again",
    "  /ls                  the instruments",
    "  /status [name]       readings, outputs and settings",
    "  /get <param>         read one, e.g. /get X or /get lockin.X",
    "  /set <param> <val>   set one, e.g. /set time_constant 300 ms or /set smu.V 0.5",
    "  /describe [name]     parameters, units, limits and safe values",
    "  /manual <words>      search the instrument's manual",
    "  /new                 start a new conversation with Kelvin",
    "  /stop                stop Kelvin's answer, any ramp or run, and the queue",
    "  /clear               clear the screen (Ctrl+L)",
    "",
    "Up/Down: history  ·  Tab: complete  ·  Esc: stop Kelvin's answer  ·  Ctrl+`: show or hide the terminal",
  ];
  const TERM_CMDS = ["/use", "/kelvin", "/ls", "/status", "/get", "/set", "/describe", "/manual", "/new", "/stop",
                     "/clear", "/help"];
  const termOut = () => $("term-out");
  const instNames = () => (PANEL.instruments || []).map((i) => i.name);
  const paramsOf = (name) => {
    const i = (PANEL.instruments || []).find((x) => x.name === name);
    return i ? [...i.readings, ...i.drives, ...i.settings] : [];
  };
  const plain = (html) => { const t = document.createElement("span"); t.innerHTML = html; return t.textContent; };

  function termLine(text, cls) {
    const out = termOut();
    const stick = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    const el = h("div", { class: "term-line" + (cls ? " " + cls.split(" ").map((c) => "t-" + c).join(" ") : "") }, text);
    out.append(el);
    while (out.childElementCount > 3000) out.firstElementChild.remove();
    if (stick) out.scrollTop = out.scrollHeight;
    return el;
  }
  function termPrompt() { return TERM.ask ? "approve? [y/N]" : (TERM.target || "kelvin") + ">"; }
  function termSync() {
    $("term-prompt").textContent = termPrompt();
    $("term-prompt").classList.toggle("asking", !!TERM.ask);
    $("term-input").placeholder = TERM.ask ? "y approves; anything else declines"
      : TERM.target ? "a command for " + TERM.target + ", or /help" : "ask Kelvin, or /help";
    const model = $("model").selectedOptions[0];
    $("term-target").textContent = TERM.target ? TERM.target + " · through the safety gate"
      : "Kelvin" + (model ? " · " + model.textContent : "");
  }
  function toggleTerm(open) {
    TERM.open = open === undefined ? !TERM.open : open;
    $("termpane").hidden = !TERM.open;
    $("toggle-term").setAttribute("aria-pressed", String(TERM.open));
    $("toggle-term").classList.toggle("on", TERM.open);
    store("lh.term", TERM.open ? "1" : "0");
    fitLayout();   // full screen, and the top-bar buttons, follow
    if (!TERM.open) return;
    if (!termOut().childElementCount) termLine("Kelvin terminal. Type to Kelvin, or /help for instrument commands.", "dim");
    termSync();
    $("term-input").focus();
  }
  function setTermHeight(px) {   // in the page's own pixels (the window's divided by the page size)
    const max = Math.max(160, layoutH() - 220);
    const hgt = Math.round(Math.min(max, Math.max(120, px)));
    $("termpane").style.height = hgt + "px";
    return hgt;
  }
  function initTerminal() {
    try { TERM.history = JSON.parse(store("lh.termHistory") || "[]").filter((x) => typeof x === "string"); } catch (e) { TERM.history = []; }
    TERM.hi = TERM.history.length;
    const saved = Number(store("lh.termHeight"));
    setTermHeight(saved > 0 ? saved : Math.round(Math.min(240, layoutH() * 0.3)));   // at first, 30 % up to 240 px
    const handle = $("term-resize");
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const bottom = $("termpane").getBoundingClientRect().bottom, z = layoutZoom();
      handle.setPointerCapture(e.pointerId);
      document.body.classList.add("resizing-v");
      const move = (ev) => setTermHeight((bottom - ev.clientY) / z);   // the screen's pixels, as the page's
      const up = () => {
        handle.removeEventListener("pointermove", move);
        document.body.classList.remove("resizing-v");
        store("lh.termHeight", String(parseInt($("termpane").style.height, 10)));
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up, { once: true });
    });
    handle.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      const cur = $("termpane").getBoundingClientRect().height / layoutZoom();
      store("lh.termHeight", String(setTermHeight(cur + (e.key === "ArrowUp" ? 32 : -32))));
    });
    $("toggle-term").addEventListener("click", () => pillClick("termpane", toggleTerm));
    $("close-term").addEventListener("click", () => toggleTerm(false));
    $("term-clear").addEventListener("click", () => { termOut().replaceChildren(); $("term-input").focus(); });
    $("term-input").addEventListener("keydown", termKey);
    termOut().addEventListener("mouseup", () => { if (!window.getSelection().toString()) $("term-input").focus(); });
    $("model").addEventListener("change", termSync);
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey && (e.key === "`" || e.code === "Backquote")) { e.preventDefault(); pillClick("termpane", toggleTerm); }
    });
    if (store("lh.term") === "1") toggleTerm(true);
  }

  function termKey(e) {
    const input = $("term-input");
    if (e.key === "Enter" && !e.isComposing) {
      e.preventDefault();
      const v = input.value;
      input.value = "";
      termRun(v);
    } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      if (!TERM.history.length || TERM.ask) return;
      e.preventDefault();
      TERM.hi = Math.max(0, Math.min(TERM.history.length, TERM.hi + (e.key === "ArrowUp" ? -1 : 1)));
      input.value = TERM.history[TERM.hi] || "";
    } else if (e.key === "Tab") {
      e.preventDefault();
      termComplete();
    } else if (e.ctrlKey && (e.key === "l" || e.key === "L")) {
      e.preventDefault();
      termOut().replaceChildren();
    } else if (e.key === "Escape" || (e.ctrlKey && (e.key === "c" || e.key === "C") &&
               input.selectionStart === input.selectionEnd && !window.getSelection().toString())) {
      if (e.key !== "Escape") e.preventDefault();
      if (TERM.busy && !TERM.ask) { api("/api/stop", { body: { chat_id: TERM.chatId } }).catch(() => {}); termLine("^C stopping Kelvin's answer", "dim"); }
      else if (e.key !== "Escape") { termLine(termPrompt() + " " + input.value + "^C", "cmd"); input.value = ""; }
    }
  }
  function termComplete() {
    const input = $("term-input");
    const words = input.value.split(" ");
    const last = words[words.length - 1];
    let options = [];
    if (words.length === 1 && last.startsWith("/")) options = [...TERM_CMDS, ...instNames().map((n) => "/" + n)];
    else if (words.length === 2 && ["/use", "/status", "/describe"].includes(words[0])) options = instNames();
    else if (words.length === 2 && ["/get", "/set"].includes(words[0])) {
      options = TERM.target ? paramsOf(TERM.target).map((p) => p.name) : [];
      for (const n of instNames()) options.push(...paramsOf(n).map((p) => n + "." + p.name));
    }
    const hits = [...new Set(options)].filter((o) => o.startsWith(last));
    if (!hits.length) return;
    let common = hits[0];
    for (const o of hits) while (!o.startsWith(common)) common = common.slice(0, -1);
    if (common.length > last.length || hits.length === 1) {
      words[words.length - 1] = common + (hits.length === 1 ? " " : "");
      input.value = words.join(" ");
    } else termLine(hits.join("   "), "dim");
  }

  async function termRun(raw) {
    const line = raw.trim();
    if (TERM.ask) { await termAnswer(line); return; }
    if (!line) { termLine(termPrompt(), "cmd"); return; }
    if (TERM.history[TERM.history.length - 1] !== line) TERM.history.push(line);
    TERM.history = TERM.history.slice(-200);
    TERM.hi = TERM.history.length;
    store("lh.termHistory", JSON.stringify(TERM.history.slice(-100)));
    termLine(termPrompt() + " " + line, "cmd");
    const cmd = line.split(/\s+/)[0];
    if (TERM.busy && !["/stop", "/clear", "/help", "/ls", "/status"].includes(cmd)) {
      termLine("still working on the last one; wait, or press Esc (or /stop) to stop it", "warn");
      return;
    }
    if (line.startsWith("/")) return termSlash(line);
    if (TERM.target) return termCall({ instrument: TERM.target, op: "raw", arg: line });
    return termChat(line);
  }
  function termUse(name) {
    if (!name) { termLine("instruments: " + (instNames().join(", ") || "none"), "dim"); return; }
    if (!instNames().includes(name)) { termLine("no instrument " + name + " (have: " + instNames().join(", ") + ")", "warn"); return; }
    TERM.target = name;
    termSync();
    termLine("typing to " + name + " now, through the safety gate; /kelvin to talk to Kelvin", "dim");
  }
  function splitKey(word) {   // "lockin.X", or "X" on the instrument in use
    const dot = word.indexOf(".");
    if (dot > 0 && instNames().includes(word.slice(0, dot))) return [word.slice(0, dot), word.slice(dot + 1)];
    return [TERM.target, word];
  }
  async function termSlash(line) {
    const [cmd, ...rest] = line.split(/\s+/);
    const arg = rest.join(" ");
    let inst = null;
    switch (cmd) {
      case "/help": case "/?": TERM_HELP.forEach((l) => termLine(l, "dim")); return;
      case "/clear": termOut().replaceChildren(); return;
      case "/kelvin": case "/chat": TERM.target = null; termSync(); termLine("talking to Kelvin", "dim"); return;
      case "/use": return termUse(arg);
      case "/ls": {
        const list = PANEL.instruments || [];
        if (!list.length) termLine("no instruments", "dim");
        for (const i of list) termLine(i.name.padEnd(10) + " " + i.model + "   [" + i.state + "]");
        return;
      }
      case "/status": {
        for (const n of arg ? [arg] : TERM.target ? [TERM.target] : instNames()) {
          const i = (PANEL.instruments || []).find((x) => x.name === n);
          if (!i) { termLine("no instrument " + n, "warn"); continue; }
          termLine(i.name + " · " + i.model, "step");
          for (const p of paramsOf(n)) {
            termLine("  " + p.name.padEnd(18) + " " + p.value + (p.away ? "   (away from safe " + p.safe + ")" : ""),
              p.error ? "err" : p.away ? "warn" : "");
          }
        }
        return;
      }
      case "/describe":
        inst = arg || TERM.target;
        if (inst) return termCall({ instrument: inst, op: "describe" });
        break;
      case "/manual":
        if (!arg) { termLine("say what to look for, e.g. /manual trigger buffer", "warn"); return; }
        if (TERM.target) return termCall({ instrument: TERM.target, op: "manual", arg });
        break;
      case "/get": case "/set": {
        if (!rest.length || (cmd === "/set" && rest.length < 2)) {
          termLine(cmd === "/get" ? "which parameter? e.g. /get X or /get lockin.X"
            : "e.g. /set time_constant 300 ms, or /set smu.V 0.5", "warn");
          return;
        }
        const [n, p] = splitKey(rest[0]);
        if (!n) break;
        if (cmd === "/get") return termCall({ instrument: n, op: "get", arg: p });
        const v = rest.slice(1).join(" ");
        return termCall({ instrument: n, op: "set", arg: p, value: /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(v) ? Number(v) : v });
      }
      case "/new": TERM.chatId = null; termLine("your next message starts a new conversation with Kelvin", "dim"); return;
      case "/stop":
        try { const r = await api("/api/stop", { body: { chat_id: TERM.chatId, lab: true } }); termLine(r.message || "stopped", "dim"); }
        catch (e) { termLine(e.message, "err"); }
        return;
      default:
        if (instNames().includes(cmd.slice(1))) return termUse(cmd.slice(1));
        termLine("unknown command " + cmd + "; /help lists them", "warn");
        return;
    }
    termLine("which instrument? /use <name> first, or e.g. " + cmd + " lockin", "warn");
  }

  async function termCall(body) {
    TERM.busy = true;
    const wait = termLine("…", "dim");
    const fast = setInterval(pollState, 700);   // an approval this command needs is asked here at once
    try {
      const r = await api("/api/terminal", { body });
      wait.remove();
      for (const l of String(r.message || "").split("\n")) termLine(l, r.ok ? "" : r.action === "error" ? "err" : "warn");
    } catch (e) {
      wait.remove();
      termLine(e.message, "err");
    } finally {
      clearInterval(fast);
      TERM.busy = false;
      termEndAsk();
      pollState();
    }
  }
  async function termChat(text) {
    const model = $("model").value;
    if (!model) { termLine("choose a model first (add a key in Settings)", "warn"); return; }
    TERM.busy = true;
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        if (!TERM.chatId) TERM.chatId = (await api("/api/chats", { body: { model } })).id;
        const res = await fetch("/api/chats/" + TERM.chatId + "/turn", {
          method: "POST", headers: { "X-Kelvin-Token": token, "Content-Type": "application/json" },
          body: JSON.stringify({ text, model }),
        });
        if (res.status === 404) { TERM.chatId = null; continue; }   // the conversation was deleted: a new one
        if (!res.ok) {
          let msg = "HTTP " + res.status;
          try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
          termLine(msg, "warn");
          return;
        }
        await readEvents(res, termEvent);
        return;
      }
    } catch (e) {
      termLine("lost the connection to Kelvin (" + e.message + ")", "err");
    } finally {
      termCloseReply();
      TERM.busy = false;
      TERM.progress.clear();
      termEndAsk();
      refreshChats();
      pollState();
    }
  }
  function termCloseReply() {   // a finished answer: Markdown bold read as plain text
    if (TERM.reply) TERM.reply.textContent = TERM.reply.textContent.replace(/\*\*([^*\n]+)\*\*/g, "$1");
    TERM.reply = null;
  }
  function termText(delta) {
    if (!TERM.reply) TERM.reply = termLine("", "reply");
    const out = termOut();
    const stick = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    TERM.reply.textContent += delta;
    if (stick) out.scrollTop = out.scrollHeight;
  }
  function termEvent(ev) {
    const cls = { ok: "ok", warn: "warn", err: "err" };
    switch (ev.type) {
      case "text": termText(ev.delta); break;
      case "reset": if (TERM.reply) TERM.reply.textContent = ""; break;
      case "tool_start": termCloseReply(); termLine("● " + plain(stepLabel(ev.name, ev.input, false)), "step"); break;
      case "tool_end":
        termCloseReply();
        termLine("  ⎿ " + (summaryOf(ev.content) || "done"), "step " + cls[outcome(ev.content, ev.is_error)]);
        break;
      case "run_progress": {
        let el = TERM.progress.get(ev.call_id);
        if (!el) { termCloseReply(); el = termLine("", "step"); TERM.progress.set(ev.call_id, el); }
        el.textContent = "  ⎿ " + (ev.plan || "run") + ": " + ev.done + (ev.total ? " / " + ev.total : "") + " points" +
          (ev.status && ev.status !== "running" ? " (" + ev.status + ")" : "");
        if (ev.status === "running") noticeRun(ev.run_id, ev.plan);   // its data show live in the Runs pane
        break;
      }
      case "approval":
        termCloseReply();
        if (ev.auto) termLine("  auto mode approved: " + ev.title, "dim");
        else termAsk(ev);
        break;
      case "approval_closed": if (TERM.ask && TERM.ask.id === ev.id) termEndAsk(); break;
      case "note": termCloseReply(); termLine(ev.text, "dim"); break;
      case "memory":
        if (ev.notes && ev.notes.length) termLine("recalled " + ev.notes.length + " note" + (ev.notes.length === 1 ? "" : "s") + " from earlier work", "dim");
        break;
      case "error": termCloseReply(); termLine(ev.message, "err"); break;
      default: break;
    }
  }
  function termAsk(p) {
    if (TERM.asked.has(p.id)) return;
    TERM.asked.add(p.id);
    TERM.ask = p;
    TERM.askAt = Date.now();
    termLine(p.title, "ask");
    for (const i of p.items || []) termLine("  - " + i, "ask");
    for (const l of p.lessons || []) termLine("  from the lab's memory: " + l, "dim");
    termSync();
    if (!TERM.open) toggleTerm(true);
    fullShow("termpane");
    $("term-input").focus();
  }
  function termEndAsk(note) {
    if (!TERM.ask) return;
    if (note) termLine(note, "dim");
    TERM.ask = null;
    termSync();
  }
  async function termAnswer(text) {
    const p = TERM.ask;
    const approve = /^y(es)?$/i.test(text);
    termLine(termPrompt() + " " + text, "cmd");
    TERM.ask = null;
    termSync();
    try {
      await api("/api/approvals/" + encodeURIComponent(p.id), { body: { approve } });
      termLine(approve ? "approved" : "declined", approve ? "ok" : "warn");
    } catch (e) {
      termLine(e.message, "err");
      if (e.status !== 409) TERM.asked.delete(p.id);   // still open (only a 409 says it closed): asked again
    }
  }
  function termState(s) {   // each poll: ask here for what the terminal is waiting on; notice answers given elsewhere
    if (TERM.ask && Date.now() - TERM.askAt > 2500 && !s.pending.some((p) => p.id === TERM.ask.id)) {
      termEndAsk("(answered elsewhere)");
    }
    if (TERM.busy && !TERM.ask) {
      const p = s.pending.find((x) => !TERM.asked.has(x.id) && !S.approvals.has(x.id));
      if (p) termAsk(p);
    }
    if (TERM.open) termSync();
  }

  // ------------------------------------------------------------------ the data folder (Settings)
  const DATA = { now: null };
  async function loadDataFolder() {
    let r;
    try { r = await api("/api/data-folder"); } catch (e) { $("data-msg").textContent = e.message; return; }
    DATA.now = r;
    $("data-path").value = r.path;
    showDataFolder(r.problem || (r.runs + " run" + (r.runs === 1 ? "" : "s") + " and " + r.reports + " report" +
      (r.reports === 1 ? "" : "s") + " in this folder" + (r.elsewhere ? "; " + r.elsewhere + " run" + (r.elsewhere === 1 ? "" : "s") +
      " in chats' own folders (they stay there)." : ".")), !!r.problem);
    dataPathChanged();
  }
  function showDataFolder(text, bad) {
    $("data-msg").textContent = text;
    $("data-msg").classList.toggle("bad", !!bad);
  }
  function dataPathChanged() {
    const now = DATA.now;
    const path = $("data-path").value.trim();
    const changed = !!now && !!path && path !== now.path;
    $("data-use").disabled = !changed;
    $("data-move-row").hidden = !changed || !(now.runs || now.reports);
    if (now) $("data-move-text").textContent = "Move what is saved now (" + now.runs + " run" + (now.runs === 1 ? "" : "s") +
      ", " + now.reports + " report" + (now.reports === 1 ? "" : "s") + ") to the new folder";
  }
  async function useDataFolder() {
    const path = $("data-path").value.trim();
    const move = !$("data-move-row").hidden && $("data-move").checked;
    $("data-use").disabled = true;
    showDataFolder(move ? "Moving the data…" : "Switching…");
    try {
      const r = await api("/api/data-folder", { body: { path, move } });
      const parts = ["Kelvin now saves to " + r.path + "."];
      if (r.moved) parts.push("Moved " + r.moved + " item" + (r.moved === 1 ? "" : "s") + ".");
      if (r.kept && r.kept.length) parts.push("Left in " + r.previous + " because the name is taken here: " + r.kept.join(", ") + ".");
      else if (!move && r.previous && (DATA.now.runs || DATA.now.reports)) parts.push("Earlier runs stay in " + r.previous + ".");
      DATA.now = r;
      $("data-path").value = r.path;
      showDataFolder(parts.join(" "));
      dataPathChanged();
      pollState();
    } catch (e) { showDataFolder(e.message, true); dataPathChanged(); }
  }

  // ------------------------------------------------------------------ Help and the first-run checklist (labops.py)
  const CHECK_ACTIONS = {
    lab: () => openLabSetup(),
    cards: () => openCards(),
    pin: () => { openSettings(); settingsPage("instruments"); },
    phone: () => { openSettings(); settingsPage("phone"); },
    data: () => { openSettings(); settingsPage("data"); },
  };
  function renderChecklist(box, r, onStart) {
    const head = h("div", { class: "check-head" }, h("b", {}, "Getting started: " + r.done + " of " + r.total + " done"));
    if (onStart) {
      const hide = h("button", { class: "link soft", type: "button" }, "Hide");
      hide.addEventListener("click", async () => {
        box.hidden = true;
        try { await api("/api/checklist", { body: { hidden: true } }); toast("Hidden. Help keeps the checklist."); } catch (e) { toast(e.message); }
      });
      head.append(hide);
    }
    box.replaceChildren(head, h("ul", { class: "check-list" }, ...r.items.map((it) => {
      const go = h("button", { class: "link soft", type: "button" }, it.done ? "Change" : "Do it");
      go.addEventListener("click", () => { if ($("help-dialog").open) $("help-dialog").close(); (CHECK_ACTIONS[it.action] || (() => {}))(); });
      return h("li", { class: "check-item" + (it.done ? " done" : "") },
        h("span", { class: "check-mark", "aria-hidden": "true" }, it.done ? "✓" : ""),
        h("div", { class: "check-body" }, h("b", {}, it.title), h("span", { class: "muted small" }, it.detail)), go);
    })));
  }
  async function loadStartChecklist() {   // on the start screen until it is all done, or hidden
    const box = $("checklist-box");
    let r;
    try { r = await api("/api/checklist"); } catch (e) { box.hidden = true; return; }
    box.hidden = r.hidden || r.done === r.total;
    if (!box.hidden) renderChecklist(box, r, true);
  }
  async function openHelp() {
    $("help-dialog").showModal();
    $("help-q").focus();
    try { renderChecklist($("help-checklist"), await api("/api/checklist"), false); }
    catch (e) { $("help-checklist").textContent = e.message; }
    if ($("help-topics").childElementCount) return;
    try {
      const groups = new Map();
      for (const t of (await api("/api/help")).topics) { if (!groups.has(t.doc)) groups.set(t.doc, []); groups.get(t.doc).push(t); }
      $("help-topics").replaceChildren(...[...groups].map(([doc, items]) => h("div", { class: "help-doc" }, h("b", {}, doc),
        h("div", { class: "help-links" }, ...items.map((t) => {
          const b = h("button", { class: "link soft", type: "button" }, t.title);
          b.addEventListener("click", () => helpShow("section=" + encodeURIComponent(t.where), t.where));
          return b;
        })))));
    } catch (e) { $("help-topics").textContent = e.message; }
  }
  async function helpShow(query, where) {
    const box = $("help-results");
    box.hidden = false;
    box.replaceChildren(h("p", { class: "muted small" }, "Looking…"));
    try {
      const r = await api("/api/help?" + query);
      const found = r.passages || [{ where, text: r.text }];
      box.replaceChildren(...(found.length ? found.map((p) => h("div", { class: "help-passage" },
        h("div", { class: "muted small" }, p.where), h("div", { class: "md", html: md(p.text) })))
        : [h("p", { class: "muted small" }, "Nothing in the help. Ask Kelvin in a chat.")]));
      box.scrollIntoView({ block: "nearest" });
    } catch (e) { box.replaceChildren(h("p", { class: "muted small" }, e.message)); }
  }
  function helpSearch() {
    const q = $("help-q").value.trim();
    if (q) helpShow("q=" + encodeURIComponent(q));
  }

  // ------------------------------------------------------------------ settings
  function fillModels(select, groups, selected, allowEmpty) {
    select.replaceChildren();
    if (allowEmpty) select.append(new Option("Automatic", ""));
    for (const g of groups) {
      const og = document.createElement("optgroup");
      og.label = g.label + (g.missing_key ? " — add an API key in Settings" : g.error ? " — " + g.error : "");
      if (!g.models.length) { const o = new Option(g.missing_key ? "No API key yet" : "Unavailable", ""); o.disabled = true; og.append(o); }
      for (const m of g.models) og.append(new Option(m.label, m.key));
      select.append(og);
    }
    const keys = groups.flatMap((g) => g.models.map((m) => m.key));
    select.value = keys.includes(selected) ? selected : allowEmpty ? "" : (keys[0] || "");
  }
  // preferred: the model a new chat starts with (Settings → Models → Default model for new chats; Automatic is
  // Claude Opus: settings.default_model); a chat that is open keeps its own
  function applyModels(groups, preferred, auto, hub) {
    const current = S.chatId && S.labels && S.labels[$("model").value] ? $("model").value : "";
    S.groups = groups;
    showHubUsage(hub);
    S.labels = {};
    for (const g of groups) for (const m of g.models) S.labels[m.key] = m.label;
    if (preferred !== undefined) S.defaultModel = preferred || "";
    if (auto !== undefined) S.autoModel = auto || "";
    fillModels($("model"), groups, (current && S.labels[current]) ? current : S.defaultModel);
    if ($("rules-chip")) applyRules();
    if (!$("model").value) $("input").placeholder = S.settings && S.settings.own_keys === false
      ? "Sign in with your Kelvin account, or set up a local model in Settings" : "Add an API key in Settings to start";
    else $("input").placeholder = "Ask Kelvin…";
    updateSend();
  }
  // Settings → Models: the month so far with the Kelvin hub's models (hub/llm.py), while signed in to the hub
  function showHubUsage(hub) {
    const el = $("hub-usage");
    if (!el) return;
    el.hidden = !hub;
    if (!hub) return;
    if (hub.needs_consent) {   // the hub's terms changed: its models wait until the person agrees (hub/llm.py)
      el.replaceChildren("Kelvin hub models wait until you ",
        h("a", { href: hub.consent_url, target: "_blank", rel: "noopener noreferrer" }, "agree to the hub's new terms"), ".");
      return;
    }
    el.textContent = "Kelvin hub models: $" + Number(hub.spent || 0).toFixed(2) + " of $" + Number(hub.budget).toFixed(2) +
      " this month" + (hub.renews ? " · renews " + hub.renews : "") + (hub.lab ? " · your lab: $" + Number(hub.lab.spent || 0).toFixed(2) +
      " of $" + Number(hub.lab.budget).toFixed(2) : "");
  }
  // Settings → Models → Default model for new chats: Automatic says which model it is now (Claude Opus when there)
  function fillDefaultModel() {
    fillModels($("default-model"), S.groups, (S.settings && S.settings.default_model) || "", true);
    const auto = S.autoModel && S.labels[S.autoModel];
    $("default-model").options[0].textContent = auto ? "Automatic: " + auto : "Automatic";
  }
  // Settings: one section at a time, chosen from the list on the left (the last one is remembered)
  function settingsPage(name) {
    const tabs = [...document.querySelectorAll(".set-nav button")].filter((b) => !b.hidden);
    const tab = tabs.find((b) => b.dataset.page === name) || tabs.find((b) => b.dataset.page === "models") || tabs[0];
    for (const b of tabs) { b.classList.toggle("on", b === tab); b.setAttribute("aria-current", b === tab ? "page" : "false"); }
    for (const p of document.querySelectorAll(".set-page")) p.hidden = p.dataset.page !== tab.dataset.page;
    $("set-pages").scrollTop = 0;
    store("lh.settingsPage", tab.dataset.page);
    if (tab.dataset.page === "instruments") loadEnvironments();
  }

  // what Kelvin installed for instruments (envs.py), each with Remove
  async function loadEnvironments() {
    const list = $("env-list");
    let rows = [];
    try { rows = (await api("/api/environments")).environments; } catch (e) { list.replaceChildren(h("li", { class: "muted small" }, e.message)); return; }
    if (!rows.length) { list.replaceChildren(h("li", { class: "muted small" }, "Nothing yet.")); return; }
    list.replaceChildren(...rows.map((r) => {
      const pkgs = Object.entries(r.packages).map(([n, v]) => n + " " + v).concat(r.files).join(", ") || "no packages";
      const remove = h("button", { class: "link danger", type: "button" }, "Remove");
      remove.addEventListener("click", async () => {
        if (!(await askYes("Remove " + r.name + "?\nIt holds " + pkgs + ". Instruments that use it stop connecting until it is installed again.",
          { ok: "Remove", danger: true }))) return;
        remove.disabled = true;
        try { toast((await api("/api/environments", { body: { remove: r.name } })).message); } catch (e) { toast(e.message); }
        loadEnvironments();
      });
      return h("li", {}, h("span", { class: "env-what" }, h("b", {}, r.name), " · Python " + r.python + " (" + r.bits + "-bit) · " + pkgs,
        h("div", { class: "muted" }, r.mb + " MB" + (r.for.length ? " · for " + r.for.join(", ") : ""))), remove);
    }));
  }

  // Settings → Instruments → Your lab's scripts: one row per folder (its path, Remove); Add folder… names another (the
  // computer's folder picker, or the path typed in a browser). Each change is kept at once, for everyone here
  function renderScriptFolders(folders, said) {
    $("scripts-folders").replaceChildren(...(folders.length ? folders.map((f) => {
      const remove = h("button", { class: "link danger", type: "button", "aria-label": "Remove " + f }, "Remove");
      remove.addEventListener("click", () => changeScriptFolders({ remove: f }));
      return h("li", {}, h("span", { class: "env-what", title: f }, f), remove);
    }) : [h("li", { class: "muted small" }, "No folders yet.")]));
    showScriptsMsg(said || "");
  }
  function showScriptsMsg(text, bad) {   // what was folded, or why a change was refused
    $("scripts-msg").textContent = text;
    $("scripts-msg").hidden = !text;
    $("scripts-msg").classList.toggle("bad", !!bad);
  }
  async function loadScriptFolders() {
    try { renderScriptFolders((await api("/api/instruments/scripts-folder")).folders); } catch (e) { /* as it was drawn */ }
  }
  async function changeScriptFolders(body) {   // {add} adds one (folded with the others, nothing read), {remove} takes one off
    const wait = [$("scripts-add"), ...$("scripts-folders").querySelectorAll("button")];   // until the rows are drawn again
    wait.forEach((b) => { b.disabled = true; });
    try {
      const r = await api("/api/instruments/scripts-folder", { body });
      if (S.settings) S.settings.lab_scripts = r.folders;
      renderScriptFolders(r.folders, (r.folded || []).join("; "));
    } catch (e) { showScriptsMsg(e.message, true); }
    finally { wait.forEach((b) => { b.disabled = false; }); }
  }
  async function addScriptFolder() {
    const now = (S.settings && S.settings.lab_scripts) || [];
    const folder = await pickFolder(now[now.length - 1] || LAB.scriptsFolder || "", "The folder with your lab's scripts (Python, notebooks, MATLAB):");
    if (folder) await changeScriptFolders({ add: folder });
  }

  // a select's option for a stored value, added when the value is one the list doesn't offer
  function pickOption(select, value, label) {
    if (![...select.options].some((o) => o.value === value)) select.append(h("option", { value }, label(value)));
    select.value = value;
  }

  // the one switch (.switch, app.css) for a setting that acts at once: swFlip(sw, save) turns it at a press, busy while
  // save(on) keeps it, and back when that fails (save says why, beside it)
  function swOn(sw) { return sw.getAttribute("aria-checked") === "true"; }
  function swSet(sw, on) { sw.setAttribute("aria-checked", String(!!on)); }
  function swFlip(sw, save) {
    sw.addEventListener("click", async () => {
      if (sw.getAttribute("aria-busy") === "true") return;
      const on = !swOn(sw);
      swSet(sw, on);
      sw.setAttribute("aria-busy", "true");
      try { await save(on); } catch (e) { swSet(sw, !on); } finally { sw.removeAttribute("aria-busy"); }
    });
  }
  function openSettings() {
    settingsPage(store("lh.settingsPage") || "models");
    showPage();   // Page size (fitTiers)
    for (const el of document.querySelectorAll(".own-keys")) el.hidden = !!(S.settings && S.settings.own_keys === false);   // the user version
    const keys = (S.settings && S.settings.keys) || {};
    for (const hint of document.querySelectorAll("[data-hint]")) {
      const src = keys[hint.dataset.hint];
      hint.textContent = src ? "Set (from " + src + ")" : "Not set";
      hint.classList.toggle("set", !!src);
      // its Remove only where there is something to remove (a key, a URL, a password): greyed (the row keeps its
      // width), the reason its tip
      const clr = document.querySelector('[data-clear="' + hint.dataset.hint + '"]');
      if (clr) { clr.disabled = !src; clr.title = src ? "" : "Nothing to remove"; }
    }
    for (const input of document.querySelectorAll("#settings-form input:not([type=checkbox])")) { input.value = ""; keyKept(input); }
    $("local-url").value = (S.settings && S.settings.local_base_url) || "";
    $("local-models").value = (S.settings && S.settings.local_models) || "";
    fillDefaultModel();
    fillModels($("reviewer"), S.groups, (S.settings && S.settings.reviewer) || "", true);
    $("reviewer").options[0].textContent = "Off";
    swSet($("compare-on"), compareOn());   // a switch: it acts at once, never waits for Save
    $("compare-status").textContent = "";
    const words = S.settings ? S.settings.answer_words : 200, length = $("answer-words"), chosen = words ? String(words) : "off";
    if (![...length.options].some((o) => o.value === chosen)) length.append(h("option", { value: chosen }, "Up to " + words + " words"));
    length.value = chosen;
    $("global-instructions").value = (S.settings && S.settings.instructions) || "";
    $("learn").checked = !S.settings || S.settings.learn !== false;
    $("card-language").value = (S.settings && S.settings.card_language) || "";
    $("hub-old-runs").checked = !!(S.settings && S.settings.hub_old_runs);
    $("literature").checked = !S.settings || S.settings.literature !== false;
    $("auto-setup").checked = !S.settings || S.settings.auto_setup !== false;
    $("scan-network").checked = !S.settings || S.settings.scan_network !== false;
    $("auto-cards").checked = !S.settings || S.settings.auto_cards !== false;
    $("manuals").checked = !S.settings || S.settings.manuals !== false;
    $("collection").checked = !S.settings || S.settings.collection !== false;
    loadCollection();
    $("scripts-python").value = (S.settings && S.settings.lab_scripts_python) || "";
    renderScriptFolders((S.settings && S.settings.lab_scripts) || []);
    loadScriptFolders();   // as they are now: Set up or the first setup may have added one meanwhile
    $("auto-report").value = (S.settings && S.settings.auto_report) || "both";
    ownerPinState();
    rolesState();
    $("roles-list").hidden = $("roles-actions").hidden = true;   // folded: roles picked but not kept by Save roles are let go
    const idle = S.settings && S.settings.idle_off !== undefined ? S.settings.idle_off : 60, idleSel = $("idle-off");
    const idleValue = idle ? String(idle) : "off";
    if (![...idleSel.options].some((o) => o.value === idleValue)) idleSel.append(h("option", { value: idleValue }, "After " + idle + " minutes"));
    idleSel.value = idleValue;
    $("idle-off-all").checked = !!(S.settings && S.settings.idle_off_all);
    $("ask-folder").checked = !S.settings || S.settings.ask_folder !== false;
    const reportKinds = (S.settings && S.settings.report_kinds) || ["scan", "device", "project"];
    for (const c of document.querySelectorAll("#report-kinds input")) c.checked = reportKinds.includes(c.value);
    loadPhone();
    loadServices();
    loadDataFolder();
    loadChatGPT();
    loadHub();
    loadClaudeCode();
    $("updates").value = (S.settings && S.settings.updates) || "auto";
    $("update-status").textContent = updateText(S.update);
    const kinds = (S.settings && S.settings.notify_kinds) || ["alarm", "run"];
    for (const c of document.querySelectorAll("#notify-kinds input")) c.checked = kinds.includes(c.value);
    const mail = (S.settings && S.settings.email) || {};   // email notices; the password stays among the keys
    $("smtp-host").value = mail.host || ""; $("smtp-port").value = mail.port || ""; $("smtp-user").value = mail.user || "";
    $("smtp-from").value = mail.from || ""; $("smtp-to").value = mail.to || ""; $("smtp-security").value = mail.security || "starttls";
    pickOption($("heartbeat-minutes"), String((S.settings && S.settings.heartbeat_minutes) || 5), (v) => "Every " + v + " minutes");
    const wait = S.settings && S.settings.approval_wait !== undefined ? S.settings.approval_wait : 60;
    pickOption($("approval-wait"), wait ? String(wait) : "off", (v) => "After 5 min, ask my phone: " + v + " min more");
    $("allow-notify").hidden = !("Notification" in window) || Notification.permission !== "default";
    $("settings-msg").textContent = "";
    renderGlobalNotes(null);
    $("settings-dialog").showModal();
    api("/api/memory").then((m) => renderGlobalNotes(m.notes)).catch(() => renderGlobalNotes([]));
    loadKnow("global", $("global-know"), $("global-know-count"), $("settings-msg"));
    settingsStart();   // every field filled (Claude Code's too, by loadClaudeCode above): what Save would send now is the start
  }
  // skills and recipes (web/skills.py): Kelvin's own, the person's, a project's
  async function loadKnow(scope, list, count, msg) {
    list.replaceChildren(h("li", { class: "memory-empty" }, "Loading…"));
    let items;
    try { items = (await api("/api/know?scope=" + encodeURIComponent(scope))).items; } catch (e) { items = []; }
    renderKnow(scope, items, list, count, msg);
  }
  function renderKnow(scope, items, list, count, msg) {
    const kept = items.filter((i) => i.scope !== "kelvin").length;
    count.textContent = kept ? kept + " kept" : "";
    if (!items.length) {
      list.replaceChildren(h("li", { class: "memory-empty" }, "Nothing yet."));
      return;
    }
    list.replaceChildren(...items.map((it) => {
      const body = h("pre", { class: "know-body", hidden: true }, it.body);
      const show = h("button", { class: "btn small", type: "button" }, "Show");
      show.addEventListener("click", () => { body.hidden = !body.hidden; show.textContent = body.hidden ? "Show" : "Hide"; });
      const when = (it.saved ? "kept " + new Date(it.saved * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "comes with Kelvin") +
        (it.files && it.files.length ? " · " + it.files.length + (it.files.length === 1 ? " file" : " files") : "");
      const on = h("button", { class: "switch", type: "button", role: "switch", "aria-checked": String(it.enabled !== false),
        "aria-label": "Kelvin uses " + it.name, title: "Kelvin sees it (off: kept, but not offered)" }, h("span"));
      swFlip(on, async (v) => {   // at once; a refusal turns it back, its reason under the skill's own line until it turns
        const note = on.closest(".memory-note");
        let why = note.querySelector(".memory-meta.bad");
        try { await api("/api/know/" + it.kind + "/" + it.scope + "/" + encodeURIComponent(it.name), { body: { enabled: v } }); it.enabled = v; }
        catch (e) {
          if (!why) note.querySelector(".memory-meta").after(why = h("span", { class: "memory-meta bad", role: "status" }));
          why.textContent = e.message;
          throw e;
        }
        if (why) why.remove();
        note.classList.toggle("off", !v);
      });
      const acts = h("div", { class: "field-actions" }, show);   // its buttons on one line under what it is
      if (it.kind === "skill") {
        const get = h("button", { class: "btn small", type: "button", title: "As a .zip for Claude or another Kelvin" }, "Download");
        get.addEventListener("click", () => downloadSkill(it));
        acts.append(get);
      }
      const row = h("li", { class: "memory-note" + (it.enabled === false ? " off" : "") }, h("span", { class: "memory-kind" }, it.kind),
        h("div", { class: "memory-body" }, h("span", {}, h("b", {}, it.name), it.description ? " — " + it.description : ""),
          h("span", { class: "memory-meta" }, when), acts, body),
        on);
      if (it.scope !== "kelvin") {
        row.append(rowButton("trash", "Delete this " + it.kind, async () => {
          try { await api("/api/know/" + it.kind + "/" + it.scope + "/" + encodeURIComponent(it.name), { method: "DELETE" }); }
          catch (e) { msg.textContent = e.message; return; }
          renderKnow(scope, items.filter((x) => x !== it), list, count, msg);
        }));
      }
      return row;
    }));
  }
  async function downloadSkill(it) {
    try {
      const res = await fetch("/api/skills/export/" + it.scope + "/" + encodeURIComponent(it.name), { headers: { "X-Kelvin-Token": token } });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const url = URL.createObjectURL(await res.blob());
      const a = h("a", { href: url, download: it.name + ".zip" });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (e) { toast("Could not download the skill: " + e.message); }
  }
  async function addSkill(file, scope, after, msg, replace) {   // a skill packed for Claude: .zip, .skill or SKILL.md
    if (!file) return;
    msg.textContent = "Adding " + file.name + "…";
    try {
      const res = await fetch("/api/skills/import?scope=" + encodeURIComponent(scope) + (replace ? "&replace=1" : ""), {
        method: "POST", body: file,
        headers: { "X-Kelvin-Token": token, "X-File-Name": encodeURIComponent(file.name) } });
      const r = await res.json().catch(() => ({}));
      if (res.status === 409 && !replace) {
        const said = String(r.error || "there is a skill by that name here already");
        if (await askYes("Replace the skill?\n" + said.charAt(0).toUpperCase() + said.slice(1) + ".", { ok: "Replace", danger: true }))
          return addSkill(file, scope, after, msg, true);
        msg.textContent = ""; return;
      }
      if (!res.ok) throw new Error(r.error || "HTTP " + res.status);
      msg.textContent = "Added the skill " + r.name + (r.files && r.files.length ? " with " + r.files.length + (r.files.length === 1 ? " file" : " files") : "") + ".";
      after();
    } catch (e) { msg.textContent = "Could not add the skill: " + e.message; }
  }
  function renderGlobalNotes(notes) {
    const list = $("global-notes");
    $("global-notes-count").textContent = notes && notes.length ? notes.length + (notes.length === 1 ? " note" : " notes") : "";
    if (!notes) { list.replaceChildren(h("li", { class: "memory-empty" }, "Loading…")); return; }
    if (!notes.length) {
      list.replaceChildren(h("li", { class: "memory-empty" },
        "Nothing yet."));
      return;
    }
    list.replaceChildren(...notes.map((n) => {
      const when = new Date(n.created * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
      const chat = S.chats.find((c) => c.id === n.chat);
      return h("li", { class: "memory-note" }, h("span", { class: "memory-kind" }, n.kind),
        h("div", { class: "memory-body" }, h("span", {}, n.text), h("span", { class: "memory-meta" }, when + (chat ? " · " + chat.title : ""))),
        rowButton("trash", "Delete this note", async () => {
          try { await api("/api/memory/notes/" + n.id, { method: "DELETE" }); } catch (e) { $("settings-msg").textContent = e.message; return; }
          renderGlobalNotes(notes.filter((x) => x.id !== n.id));
        }));
    }));
  }
  // ------------------------------------------------------------------ keeping Kelvin up to date (updater.py)
  function updateText(u) {
    if (!u) return "";
    switch (u.state) {
      case "unsupported": return "Runs from source: update it with git.";
      case "checking": return "Looking for a newer Kelvin…";
      case "downloading": return "Downloading Kelvin " + u.latest + "…";
      case "ready": return "Kelvin " + u.latest + " is ready" + (u.mode === "auto" ? "; it installs when you close Kelvin." : ".");
      case "available": return "Kelvin " + u.latest + " is out" + (u.can_install ? "." : "; download it from GitHub.");
      case "installing": return "Installing Kelvin " + u.latest + "; Kelvin starts again when it is done.";
      case "up_to_date": return "Kelvin " + u.current + " is the newest.";
      case "error": return "Couldn't update: " + u.error;
      default: return "Kelvin " + u.current + (u.mode === "off" ? "; updates are off." : ".");
    }
  }
  function showUpdate(u) {
    S.update = u || null;
    if (!u) return;
    const bar = $("update-bar");
    $("update-status").textContent = updateText(u);
    if (u.other_copy) {   // there is only one Kelvin on a computer: this copy is not it
      bar.hidden = false; bar.classList.add("warn");
      $("update-text").textContent = "This copy of Kelvin isn't the installed one (that is in " + u.other_copy + "). Close it, use the installed Kelvin, and delete this copy.";
      $("update-go").hidden = true; $("update-notes").hidden = true;
      return;
    }
    bar.classList.remove("warn");
    const show = ["ready", "available", "installing"].includes(u.state) && u.mode !== "off";
    bar.hidden = !show;
    if (!show) return;
    $("update-text").textContent = updateText(u);
    $("update-notes").hidden = !u.url;
    if (u.url) $("update-notes").href = u.url;
    $("update-go").hidden = !(u.can_install && ["ready", "available"].includes(u.state));
    $("update-go").textContent = u.state === "ready" ? "Restart to update" : "Download and update";
  }
  async function installUpdate() {
    $("update-go").disabled = true;
    try {
      if (S.update && S.update.state === "available") showUpdate(await api("/api/update", { body: { op: "download" } }));
      showUpdate(await api("/api/update", { body: { op: "install" } }));
    } catch (e) { toast(e.message); }
    $("update-go").disabled = false;
  }
  async function checkUpdate() {
    $("update-status").textContent = "Looking for a newer Kelvin…";
    try { showUpdate(await api("/api/update", { body: { op: "check" } })); } catch (e) { $("update-status").textContent = e.message; }
  }
  // ------------------------------------------------------------------ the instrument collection (collection.py)
  function collectionText(c) {
    if (!c.commit) return c.error ? "No copy yet: Kelvin " + c.error + "." : c.on ? "No copy yet; Kelvin fetches it from GitHub." : "No copy on this computer.";
    const when = new Date(c.synced * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    const use = c.used.length ? "Kelvin uses " + c.used.length + " of its " + c.cards + " cards" +
      (c.newer.length ? " (newer than its own for " + c.newer.join(", ") + ")" : "") : "none of its " + c.cards + " cards is newer than Kelvin's own";
    return "From " + c.repository + " at " + c.commit.slice(0, 7) + " (" + when + "): " + c.instruments + " instruments and " +
      c.experiments + " experiments; " + use + "." + (c.left_out.length ? " Left out: " + c.left_out.join("; ") + "." : "") +
      (c.error ? " At the last check Kelvin " + c.error + "; it uses the copy it has." : "");
  }
  async function loadCollection(sync) {
    $("collection-status").textContent = sync ? "Looking for a newer collection…" : "…";
    try { $("collection-status").textContent = collectionText(await api("/api/collection", sync ? { body: { op: "sync" } } : undefined)); }
    catch (e) { $("collection-status").textContent = e.message; }
  }

  async function loadClaudeCode() {   // Claude Code bridge (temporary): found here? signed in?
    $("cc-field").hidden = !S.claudeCode;
    if (!S.claudeCode) return;
    $("cc-on").checked = !S.settings || S.settings.claude_code !== false;
    $("cc-path").value = (S.settings && S.settings.claude_code_path) || "";
    $("cc-status").textContent = "Checking…";
    try {
      const s = await api("/api/claude-code");
      $("cc-status").textContent = !s.cli ? "claude not found" : s.signed_in ? "Signed in" + (s.method ? " (" + s.method + ")" : "")
        : "Found, not signed in: run claude once and sign in";
      $("cc-status").classList.toggle("set", !!(s.cli && s.signed_in));
    } catch (e) { $("cc-status").textContent = e.message; }
  }

  // Settings keep Save and Cancel. What acts at once is kept already: a switch (each in a card of its
  // own at the top of its page, .set-now, or in its row, as a skill's), Page size, and every button. Everything else
  // waits for Save, and what waits is what Save would send now (settingsBody) that differs from what it would have sent
  // as the dialog opened (SETS.start). Save is offered only then, the other button reads Cancel (else Close), and each
  // section holding a change has a dot in the list on the left. Cancel lets go of what waits (the fields are filled from
  // S.settings again at the next opening), never of what acted. The head's × and Esc do as that button does, but while
  // something waits they ask first
  const SETS = { start: {}, saving: false, asking: false };
  // what Save sends: every setting the pages hold for Save, as its field holds it now (each field is named after its
  // setting: settingField); a key goes only when typed, or empty once its Remove was pressed
  function settingsBody() {
    const body = {};
    for (const input of document.querySelectorAll("#settings-form input[type=password][name]")) {
      if (input.dataset.clear) body[input.name] = "";
      else if (input.value.trim()) body[input.name] = input.value.trim();
    }
    body.local_base_url = $("local-url").value.trim();
    body.local_models = $("local-models").value.trim();
    body.default_model = $("default-model").value;
    body.reviewer = $("reviewer").value;
    body.answer_words = $("answer-words").value;
    body.instructions = $("global-instructions").value;
    body.card_language = $("card-language").value;
    body.learn = $("learn").checked ? "on" : "off";
    body.hub_old_runs = $("hub-old-runs").checked ? "on" : "off";
    body.literature = $("literature").checked ? "on" : "off";
    body.auto_setup = $("auto-setup").checked ? "on" : "off";
    body.scan_network = $("scan-network").checked ? "on" : "off";
    body.auto_cards = $("auto-cards").checked ? "on" : "off";
    body.manuals = $("manuals").checked ? "on" : "off";
    body.collection = $("collection").checked ? "on" : "off";
    body.lab_scripts_python = $("scripts-python").value.trim();
    body.auto_report = $("auto-report").value;
    body.idle_off = $("idle-off").value;
    body.idle_off_all = $("idle-off-all").checked ? "on" : "off";
    body.ask_folder = $("ask-folder").checked ? "on" : "off";
    body.report_kinds = [...document.querySelectorAll("#report-kinds input")].filter((c) => c.checked).map((c) => c.value).join(",") || "none";
    body.notify_kinds = [...document.querySelectorAll("#notify-kinds input")].filter((c) => c.checked).map((c) => c.value).join(",") || "none";
    body.smtp_host = $("smtp-host").value.trim(); body.smtp_port = $("smtp-port").value.trim();
    body.smtp_security = $("smtp-security").value; body.smtp_user = $("smtp-user").value.trim();
    body.smtp_from = $("smtp-from").value.trim(); body.smtp_to = $("smtp-to").value.trim();
    body.heartbeat_minutes = $("heartbeat-minutes").value;
    body.approval_wait = $("approval-wait").value;
    if (S.claudeCode) { body.claude_code = $("cc-on").checked ? "on" : "off"; body.claude_code_path = $("cc-path").value.trim(); }
    body.updates = $("updates").value;
    return body;
  }
  function settingField(name) { return document.querySelector('#settings-form [name="' + name + '"]'); }
  // the settings that wait for Save: those Save would now send otherwise than at the opening, by name
  function settingsDiff() {
    const now = settingsBody(), start = SETS.start;
    return [...new Set(Object.keys(now).concat(Object.keys(start)))].filter((k) => JSON.stringify(now[k]) !== JSON.stringify(start[k]));
  }
  // the dialog as its fields were filled at the opening: nothing waits
  function settingsStart() { SETS.start = settingsBody(); settingsPending(); }
  // a load that fills fields again after the opening (the model lists, once a service is added or removed): what it
  // fills is no change of the person's
  function settingsFilled(names) { const now = settingsBody(); for (const k of names) SETS.start[k] = now[k]; }
  // what waits, said where it is: Save offered (and the chosen look) only then, the other button Cancel or Close, and a
  // dot on each section holding a change (heard as ", changed"). While a Save is on its way nothing leaves: Save, Close
  // (or Cancel) and the head's × are greyed until the answer closes the dialog or says why not. Gives the settings waiting
  function settingsPending() {
    const names = settingsDiff(), pages = settingsPages(names), save = $("save-settings"), close = $("settings-close");
    save.disabled = !names.length || SETS.saving;
    save.classList.toggle("primary", names.length > 0);
    save.title = names.length ? "" : "Nothing to save";
    const word = names.length ? "Cancel" : "Close";
    if (close.textContent !== word) close.textContent = word;
    close.disabled = $("settings-x").disabled = SETS.saving;
    for (const b of document.querySelectorAll(".set-nav button")) {
      const dot = b.querySelector(".set-dot");
      if (dot) dot.hidden = !pages.has(b.dataset.page);
    }
    return names;
  }
  // the sections (data-page) holding the settings named
  function settingsPages(names) {
    const pages = new Set();
    for (const k of names) { const page = settingField(k) && settingField(k).closest(".set-page"); if (page) pages.add(page.dataset.page); }
    return pages;
  }
  // the names of the sections holding a change that waits for Save, as the list on the left gives them
  function settingsWhere() {
    const pages = settingsPages(settingsDiff());
    return [...document.querySelectorAll(".set-nav button")].filter((b) => pages.has(b.dataset.page)).map((b) => b.firstChild.textContent.trim());
  }
  // the head's × and Esc: as Close, but while something waits for Save they ask first, and No (Keep editing, or Esc)
  // keeps the dialog open; while a Save is on its way they do nothing, as its answer decides
  async function settingsLeave() {
    if (SETS.asking || SETS.saving || !$("settings-dialog").open) return;
    if (settingsPending().length) {
      const where = settingsWhere();
      SETS.asking = true;
      const yes = await askYes(where.length ? "Discard your changes on " + andList(where) + "?" : "Discard your changes?",
        { ok: "Discard", cancel: "Keep editing", danger: true });
      SETS.asking = false;
      if (!yes) return;
    }
    $("settings-dialog").close();
  }
  // a key's box as before its Remove: nothing to remove on Save
  function keyKept(input) {
    delete input.dataset.clear;
    if ("was" in input.dataset) { input.placeholder = input.dataset.was; delete input.dataset.was; }
  }
  // Enter in a box of one line never closes Settings, nor loses what waits: it presses the button on the box's own row
  // (Connect, Add, Change, Use this folder…), never a key's Remove, and with none does nothing. A box marked
  // enterkeyhint="next" (Current password, a service's name and address) goes on to the next box of its row instead, and
  // an empty New password presses nothing (Change from it would take the password off)
  function settingsEnter(e) {
    const box = e.target;
    if (e.key !== "Enter" || e.isComposing || e.defaultPrevented || box.tagName !== "INPUT" ||
      !/^(text|password|url|email|search|tel|number)$/.test(box.type)) return;
    e.preventDefault();
    const row = box.closest(".key-row, .service-form"), shown = (el) => !el.hidden && el.offsetWidth > 0;
    if (!row || (box.id === "pw-new" && !box.value)) return;
    if (box.enterKeyHint === "next") {
      const boxes = [...row.querySelectorAll("input")].filter(shown), next = boxes[boxes.indexOf(box) + 1];
      if (next) { next.focus(); return; }
    }
    const go = [...row.querySelectorAll("button:not([data-clear])")].filter(shown).pop();
    if (go && !go.disabled) go.click();
  }
  // Save: as before (Settings saved., the dialog closes); a refusal keeps the dialog open, says why, and keeps what waits
  async function saveSettings() {
    const body = settingsBody();
    SETS.saving = true;
    settingsPending();   // greyed while it is on its way
    $("settings-msg").textContent = "Saving…";
    try {
      const r = await api("/api/settings", { body });
      S.settings = r.settings;
      applyCompare();
      SETS.start = body;
      applyModels(r.models.groups, r.default_model, r.auto_model, r.models.hub);
      $("settings-dialog").close();
      toast("Settings saved.");
    } catch (e) {
      $("settings-msg").textContent = e.message;
    } finally {
      SETS.saving = false;
      settingsPending();
    }
  }

  // ------------------------------------------------------------------ page size: the whole page's scale, each browser's own
  // One CSS zoom on <html> (--zoom, app.css) is the only scaling, in a browser and in the desktop app. Auto follows the
  // real window, which the zoom does not change, so it never feeds back on itself: 100 % on a big window (1600 × 900 or
  // more), on a touch screen and under 1000 px wide; 85 % under 680 px tall; 90 % otherwise (the person's own "90 % on
  // a laptop"). A choice in Settings → Page size (80 to 150 %) overrides it on this browser (lh.pageSize). fitTiers also
  // sets the density tier on <html> by the layout's height, the window's divided by the page size: fit-roomy (900 or
  // more), fit-compact (720 to 899) or fit-tight.
  const SIZES = [0.8, 0.85, 0.9, 1, 1.1, 1.25, 1.5];
  const FIT = { zoom: 1, tier: "", timer: 0, still: 0, wheel: 0, native: false };
  try { localStorage.removeItem("lh.v"); } catch (e) { /* kept nowhere */ }   // a layout choice an earlier version kept
  const inApp = () => !!(window.pywebview && window.pywebview.api && window.pywebview.api.set_zoom);
  function pageChoice() { const z = Number(store("lh.pageSize")); return SIZES.includes(z) ? z : 0; }   // 0: Auto
  function autoZoom() {
    const w = window.innerWidth, h = window.innerHeight;
    if (w < 1000 || window.matchMedia("(pointer: coarse)").matches) return 1;
    return w >= 1600 && h >= 900 ? 1 : h < 680 ? 0.85 : 0.9;
  }
  const pageZoom = () => FIT.zoom;   // Kelvin.zoom(): divide pointer and rect lengths by it before writing them to CSS
  // the page size and the tier: on loading, 50 ms after the window changes size, and when a size is chosen. Never
  // animated (k-still, app.css). A new factor tells everything that measures the window (a resize event)
  function fitTiers() {
    const root = document.documentElement, z = pageChoice() || autoZoom();
    const h = window.innerHeight / z;
    const tier = h >= 900 ? "fit-roomy" : h >= 720 ? "fit-compact" : "fit-tight";
    const first = !FIT.tier, zoomed = z !== FIT.zoom;
    if (!zoomed && tier === FIT.tier) return;
    root.classList.add("k-still");
    if (zoomed) root.style.setProperty("--zoom", String(z));
    root.classList.remove("fit-roomy", "fit-compact", "fit-tight");
    root.classList.add(tier);
    FIT.zoom = z;
    FIT.tier = tier;
    void root.offsetWidth;   // laid out now, with nothing sliding
    clearTimeout(FIT.still);
    FIT.still = setTimeout(() => root.classList.remove("k-still"), 120);
    if (FIT.measure) FIT.measure();
    showPage();
    if (zoomed && !first) window.dispatchEvent(new Event("resize"));
    emit("fit", { zoom: z, tier });
  }
  function showPage() {   // Settings → Display → Page size
    if (!$("zoom-now")) return;
    const z = FIT.zoom, chosen = pageChoice(), pct = Math.round(z * 100) + "%";
    $("zoom-now").textContent = chosen ? pct : "Auto · " + pct;
    $("zoom-out").disabled = z <= SIZES[0];
    $("zoom-in").disabled = z >= SIZES[SIZES.length - 1];
    $("zoom-reset").hidden = !chosen;
    $("zoom-hint").hidden = !inApp();
  }
  function stepPage(dir) {   // +1 bigger, -1 smaller, 0 back to Auto
    let z = 0;
    if (dir) {
      z = dir > 0 ? SIZES.find((s) => s > FIT.zoom + 0.001) : [...SIZES].reverse().find((s) => s < FIT.zoom - 0.001);
      if (!z) return;
    }
    if (z) store("lh.pageSize", String(z));
    else try { localStorage.removeItem("lh.pageSize"); } catch (e) { /* kept nowhere */ }
    fitTiers();
    showPage();
  }
  // In the desktop app, Ctrl + / − / 0 and Ctrl with the wheel step the page size (the window's own zoom keys are off);
  // in a browser they stay the browser's zoom, which Auto follows. The buttons work in both.
  function pageKeys() {
    document.addEventListener("keydown", (e) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || !inApp()) return;
      const dir = e.key === "=" || e.key === "+" ? 1 : e.key === "-" || e.key === "_" ? -1 : e.key === "0" ? 0 : null;
      if (dir === null) return;
      e.preventDefault();
      stepPage(dir);
    });
    window.addEventListener("wheel", (e) => {
      if (!e.ctrlKey || !inApp()) return;
      e.preventDefault();
      FIT.wheel += e.deltaY;
      if (Math.abs(FIT.wheel) < 80) return;   // a touchpad sends many small steps
      stepPage(FIT.wheel < 0 ? 1 : -1);
      FIT.wheel = 0;
    }, { passive: false });
    $("zoom-in").addEventListener("click", () => stepPage(1));
    $("zoom-out").addEventListener("click", () => stepPage(-1));
    $("zoom-reset").addEventListener("click", () => stepPage(0));
    showPage();
  }
  // The desktop window's own zoom (the old Text size, kept with the person's settings as text_size) gives way to the
  // page size: once, that size becomes this window's page size, and the window's zoom goes back to 100 %
  async function appZoomOff() {
    if (FIT.native || !inApp() || !S.settings) return;
    FIT.native = true;
    const t = Number(S.settings.text_size);
    if (store("lh.pageSize") === null && store("lh.pageFrom") !== "text_size") {
      store("lh.pageFrom", "text_size");
      if (t > 0 && t !== 1) store("lh.pageSize", String(SIZES.reduce((a, b) => (Math.abs(b - t) < Math.abs(a - t) ? b : a))));
    }
    try { await window.pywebview.api.set_zoom(1); } catch (e) { /* stays as it was */ }
    fitTiers();
    showPage();
  }
  // the top bar's height, and the bar's with the notice row (--topbar-h, --chrome-h): the frame starts below them
  function measureChrome() {
    const root = document.documentElement, bar = document.querySelector(".topbar"), chrome = $("chrome");
    if (!bar || !chrome) return;
    const set = () => {   // as drawn, in the page's pixels: a hairline stays a whole screen pixel at any page size
      for (const [name, el] of [["--topbar-h", bar], ["--chrome-h", chrome]]) {
        const px = Math.ceil(el.getBoundingClientRect().height / pageZoom() * 100) / 100 + "px";
        if (root.style.getPropertyValue(name) !== px) root.style.setProperty(name, px);
      }
    };
    if (window.ResizeObserver) { const ro = new ResizeObserver(set); ro.observe(bar); ro.observe(chrome); }
    FIT.measure = set;   // again whenever the page size changes (fitTiers)
    set();
  }
  // A region cut by its edge (data-region, app.css) says where more waits, for its soft edge: data-cut t, b or tb. Kept
  // as it scrolls, and each second for what grew or shrank meanwhile.
  function cue(el) {
    const cut = (el.scrollTop > 1 ? "t" : "") + (el.scrollHeight - el.clientHeight - el.scrollTop > 1 ? "b" : "");
    if ((el.getAttribute("data-cut") || "") === cut) return;
    if (cut) el.setAttribute("data-cut", cut); else el.removeAttribute("data-cut");
  }
  function cueRegions() { if (!document.hidden) for (const el of document.querySelectorAll("[data-region]")) cue(el); }
  window.addEventListener("resize", () => { clearTimeout(FIT.timer); FIT.timer = setTimeout(() => { fitTiers(); cueRegions(); }, 50); });
  window.addEventListener("pywebviewready", appZoomOff);
  document.addEventListener("scroll", (e) => {
    if (e.target.nodeType === 1 && e.target.hasAttribute("data-region")) cue(e.target);
  }, { capture: true, passive: true });
  setInterval(cueRegions, 1000);
  // the one switch (.switch): moved with the keyboard (a click of detail 0), it jumps to its place instead of sliding
  document.addEventListener("click", (e) => {
    const sw = e.detail === 0 && e.target.closest && e.target.closest(".switch");
    if (!sw) return;
    sw.classList.add("still");
    setTimeout(() => sw.classList.remove("still"), 450);
  }, true);
  fitTiers();
  measureChrome();
  // ------------------------------------------------------------------ the in-page question (never the browser's confirm or prompt)
  // askYes(text, {title, ok, cancel, alt, altDanger, danger, focus}) gives true, false or "alt"; askText(text, {value,
  // placeholder, ok, cancel, title}) gives the words, or null. The first line is the heading and the rest the reason
  // under it (one long line is cut after its first sentence).
  function askParts(text, o, withText) {
    const lines = String(text == null ? "" : text).split("\n").map((s) => s.trim()).filter(Boolean);
    if (!o.title && lines.length === 1) {
      const cut = /^(.{4,80}?[?.!])\s+(\S.*)$/.exec(lines[0]);
      if (cut) lines.splice(0, 1, cut[1], cut[2]);
    }
    const head = o.title || lines.shift() || "Are you sure?", id = "ask-" + Math.random().toString(36).slice(2, 8);
    const input = withText ? h("input", { class: "kfield", type: "text", value: o.value == null ? "" : String(o.value),
      placeholder: o.placeholder || null, autocomplete: "off", "aria-labelledby": id }) : null;
    const no = h("button", { class: "btn", type: "button" }, o.cancel || "Cancel");
    const alt = o.alt ? h("button", { class: "btn" + (o.altDanger ? " danger-line" : ""), type: "button" }, o.alt) : null;
    const yes = h("button", { class: "btn " + (o.danger ? "danger" : "primary"), type: "button" }, o.ok || "OK");
    const body = h("div", { class: "ask-body", "data-region": "dialog", id: id + "-why" }, ...lines.map((l) => h("p", {}, l)), input);
    const form = h("form", { method: "dialog" }, h("header", { class: "dialog-head" }, h("h2", { id }, head)), body,
      h("footer", { class: "dialog-foot" }, no, alt, yes));
    const dlg = h("dialog", { class: "dialog ask-dialog", "aria-labelledby": id, role: o.danger ? "alertdialog" : null,
      "aria-describedby": lines.length ? id + "-why" : null }, form);
    return { dlg, form, input, no, alt, yes };
  }
  // A mouse click in the first 0.4 s, or the second of a double-click, is no answer (the double-click that asked lands
  // here); a key always is one. Esc means no, and so does the backdrop, only when the press started on it (a selection
  // that ends outside throws nothing away). Focus starts on Cancel when OK cannot be undone (danger, or focus: "cancel").
  function askDialog(text, o, withText) {
    return new Promise((resolve) => {
      const { dlg, form, input, no, alt, yes } = askParts(text, o, withText);
      const opened = Date.now(), none = withText ? null : false;
      const early = (e) => e.detail > 0 && (e.detail > 1 || Date.now() - opened < 400);
      const outside = (e) => {
        const r = dlg.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      let over = false, downOut = false;
      const finish = (v) => { if (over) return; over = true; if (dlg.open) dlg.close(); dlg.remove(); resolve(v); };
      yes.addEventListener("click", (e) => { if (!early(e)) finish(withText ? input.value : true); });
      no.addEventListener("click", (e) => { if (!early(e)) finish(none); });
      if (alt) alt.addEventListener("click", (e) => { if (!early(e)) finish("alt"); });
      form.addEventListener("submit", (e) => e.preventDefault());
      if (input) input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); finish(input.value); } });
      dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish(none); });
      dlg.addEventListener("close", () => finish(none));
      dlg.addEventListener("pointerdown", (e) => { downOut = e.target === dlg && outside(e); });
      dlg.addEventListener("click", (e) => { if (e.target === dlg && downOut && outside(e) && !early(e)) finish(none); downOut = false; });
      document.body.append(dlg);
      dlg.showModal();
      (input || (o.focus === "alt" && alt) || (o.focus === "cancel" || (o.danger && o.focus !== "ok") ? no : yes)).focus();
      if (input) input.select();
    });
  }
  function askYes(text, opts) { return askDialog(text, opts || {}, false); }
  function askText(text, opts) { return askDialog(text, opts || {}, true); }

  // ------------------------------------------------------------------ layout bits
  function autosize() {
    const t = $("input");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 220) + "px";
    updateSend();
  }
  // a chat chosen where the sidebar takes the content area: the chat back, the sidebar hidden for now (its button
  // keeps the person's choice)
  function closeSidebar() { if (sideSolo()) { SIDE.hidden = true; fitLayout(); } }
  // The sidebar's button, at the top bar's left at every width, and the only thing that hides or shows the sidebar
  // (the panels never do), as ChatGPT's: where the sidebar stands beside the page it slides away and the page and the
  // panels take its room; where it cannot stand beside the main column (under 784 page px: sideNarrow) it takes
  // the content area as a pane alone does, below the top bar (.side-solo), and what was there waits out of sight until
  // a chat, Chat or Home, a pane's button or the button again brings it back. The page keeps the choice
  // (lh.sideHidden). In full screen, where it is out of sight, the button ends full screen and shows it.
  const sideNarrow = () => layoutW() < SIDEBAR_W + CHAT_MIN;
  const sideSolo = () => !SIDE.hidden && !FULL.length && sideNarrow();
  function toggleSidebar() {
    const app = $("app");
    SIDE.hidden = FULL.length ? false : !SIDE.hidden;
    FULL.length = 0;
    store("lh.sideHidden", SIDE.hidden ? "1" : "0");
    if (!sideNarrow()) {   // beside the page it slides, for this change alone (app.css), never as the panels make room
      app.classList.add("side-slide");
      clearTimeout(SIDE.slide);
      SIDE.slide = setTimeout(() => app.classList.remove("side-slide"), 500);
    }
    fitLayout();
  }
  // the button's name never changes (Conversations); aria-expanded says whether the sidebar is on the screen. On hover
  // the name eases in under it as the top bar's other buttons' names do (app.css), never in the browser's tip
  function syncSide() {
    const b = $("toggle-sidebar"), say = "Conversations";
    b.setAttribute("aria-expanded", String(!SIDE.hidden && !FULL.length));
    b.setAttribute("aria-label", say);
    b.dataset.tip = say;
  }


  // ------------------------------------------------------------------ team chat: the lab's people, to each other
  // Everyone signed in here, and their phones, read the same chat (teamchat.py); Kelvin reads it with team_chat. The
  // name you write under is this browser's (lh.teamName), so people who share an account each write under their own.
  const TEAM = { open: false, msgs: [], shown: 0, seen: Number(store("lh.teamSeen")) || 0, told: null, more: false,
                 busy: false, drawn: false };
  const teamName = () => store("lh.teamName") || "";

  function toggleTeam(open) {
    TEAM.open = open === undefined ? !TEAM.open : open;
    $("teampanel").hidden = !TEAM.open;
    $("toggle-team").setAttribute("aria-pressed", String(TEAM.open));
    $("toggle-team").classList.toggle("on", TEAM.open);
    fitLayout();
    if (!TEAM.open) return;
    showTeamName();
    loadTeam();
    fitTeamInput();
    if (teamName()) $("team-input").focus();
  }
  function showTeamName(edit) {
    const name = teamName(), asking = !!edit || !name;
    $("team-name-form").hidden = !asking;
    $("team-form").hidden = asking;
    $("team-name").hidden = !name;
    $("team-name-text").textContent = name;
    if (!asking) return;
    $("team-name-input").value = name || (S.account && S.account.user ? S.account.user.name : "") || "";
    $("team-name-input").focus();
    $("team-name-input").select();
  }
  function saveTeamName(e) {
    e.preventDefault();
    const name = $("team-name-input").value.replace(/\s+/g, " ").trim().slice(0, 40);
    if (!name) { $("team-name-input").focus(); return; }
    store("lh.teamName", name);
    showTeamName();
    renderTeam();
    fitTeamInput();   // sized now it shows (it was hidden while the name was asked)
    $("team-input").focus();
  }
  function dayLabel(d) {
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const day = new Date(d); day.setHours(0, 0, 0, 0);
    const n = Math.round((today - day) / 86400000);
    if (n === 0) return "Today";
    if (n === 1) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric",
      year: day.getFullYear() === today.getFullYear() ? undefined : "numeric" });
  }
  function teamItems(m, prev) {
    const at = new Date(m.at), before = prev ? new Date(prev.at) : null;
    const out = [];
    const newDay = !before || before.toDateString() !== at.toDateString();
    if (newDay) out.push(h("div", { class: "tm-day" }, dayLabel(at)));
    const cont = !newDay && prev.name === m.name && at - before < 5 * 60000;
    const who = m.name + (m.account && m.account !== m.name ? " (" + m.account + ")" : "") +
      (m.via && m.via !== "portal" ? " · " + m.via : "");
    out.push(h("div", { class: "tm" + (cont ? " cont" : "") + (m.name === teamName() ? " mine" : ""), title: who + " · " + at.toLocaleString() },
      cont ? null : h("div", { class: "tm-head" }, h("b", {}, m.name),
        h("time", { datetime: m.at }, at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))),
      h("div", { class: "tm-text" }, m.text)));
    return out;
  }
  function renderTeam(older) {
    const list = $("team-list");
    const atEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
    const fromEnd = list.scrollHeight - list.scrollTop;
    const items = TEAM.msgs.flatMap((m, i) => teamItems(m, TEAM.msgs[i - 1]));
    list.replaceChildren(...(items.length ? items : [h("div", { class: "team-empty" }, "No messages yet")]));
    if (older) list.scrollTop = list.scrollHeight - fromEnd;   // what you were reading stays in place
    else if (atEnd || !TEAM.drawn) list.scrollTop = list.scrollHeight;
    TEAM.drawn = true;
  }
  function addTeam(messages, older) {
    const have = new Set(TEAM.msgs.map((m) => m.id));
    const fresh = messages.filter((m) => !have.has(m.id));
    if (!fresh.length && TEAM.drawn) return;
    TEAM.msgs = TEAM.msgs.concat(fresh).sort((a, b) => a.id - b.id);
    TEAM.shown = TEAM.msgs.length ? TEAM.msgs[TEAM.msgs.length - 1].id : 0;
    renderTeam(older);
  }
  async function loadTeam() {
    if (TEAM.busy) return;
    TEAM.busy = true;
    try {
      const first = !TEAM.drawn;
      const r = await api("/api/team" + (TEAM.shown ? "?after=" + TEAM.shown : ""));
      if (first) TEAM.more = r.messages.length >= 200;
      addTeam(r.messages);
      if (TEAM.open) markTeamSeen();
    } catch (e) { /* the next poll tries again */ }
    TEAM.busy = false;
  }
  async function loadOlderTeam() {
    if (!TEAM.more || TEAM.busy || !TEAM.msgs.length) return;
    TEAM.busy = true;
    try {
      const r = await api("/api/team?before=" + TEAM.msgs[0].id);
      TEAM.more = r.messages.length >= 200;
      addTeam(r.messages, true);
    } catch (e) { /* stays as it is */ }
    TEAM.busy = false;
  }
  function markTeamSeen() {
    TEAM.seen = Math.max(TEAM.seen, TEAM.shown);
    TEAM.told = Math.max(TEAM.told || 0, TEAM.shown);
    store("lh.teamSeen", String(TEAM.seen));
    $("team-dot").hidden = true;
  }
  async function sendTeam(e) {
    e.preventDefault();
    const input = $("team-input"), text = input.value.trim();
    if (!text) return;
    if (!teamName()) { showTeamName(true); return; }
    $("team-send").disabled = true;
    try {
      const r = await api("/api/team", { body: { name: teamName(), text } });
      input.value = "";
      fitTeamInput();
      addTeam([r.message]);
      markTeamSeen();
    } catch (err) { toast(err.message); }
    $("team-send").disabled = false;
    input.focus();
  }
  function fitTeamInput() {
    const t = $("team-input");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 160) + "px";
    t.style.overflowY = t.scrollHeight > 160 ? "auto" : "hidden";
  }
  // the state's poll says the newest message's id: a dot on the button, and who wrote what while the chat is closed
  async function teamState(last) {
    if (typeof last !== "number") return;
    if (TEAM.told === null) TEAM.told = last;   // what was there before this page opened is not news
    if (TEAM.open) { if (last > TEAM.shown) loadTeam(); return; }
    $("team-dot").hidden = !(last > TEAM.seen);
    if (last <= TEAM.told) return;
    const from = TEAM.told;
    TEAM.told = last;
    try {
      const theirs = (await api("/api/team?after=" + from)).messages.filter((m) => m.name !== teamName());
      const m = theirs[theirs.length - 1];
      if (m) toast(m.name + ": " + (m.text.length > 90 ? m.text.slice(0, 90) + "…" : m.text), () => { toggleTeam(true); fullShow("teampanel"); });
    } catch (e) { /* the dot still says */ }
  }
  function initTeam() {
    $("toggle-team").addEventListener("click", () => pillClick("teampanel", toggleTeam));
    $("close-team").addEventListener("click", () => toggleTeam(false));
    $("team-form").addEventListener("submit", sendTeam);
    $("team-name-form").addEventListener("submit", saveTeamName);
    $("team-name").addEventListener("click", () => showTeamName(true));
    $("team-name-input").addEventListener("keydown", (e) => {
      if (e.key === "Escape" && teamName()) { e.preventDefault(); showTeamName(); }
    });
    $("team-input").addEventListener("input", fitTeamInput);
    $("team-input").addEventListener("keydown", (e) => {   // Enter sends, Shift+Enter starts a new line
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) sendTeam(e);
    });
    $("team-list").addEventListener("scroll", () => { if ($("team-list").scrollTop < 40) loadOlderTeam(); });
  }

  // ------------------------------------------------------------------ analysis: a dataset, as Kelvin's subagent sees it
  // The pane holds a dataset picker and what Kelvin's analysis subagent made of the dataset (web/analyst.py): it reads
  // the run, decides by itself how to process the data and how to show them, and each figure it draws comes with the
  // processing steps it took, a few words each. A dataset is analyzed once: picked again, what was found shows (Analyze
  // again asks anew). Until the person picks one, the pane follows the newest run (or the one the Runs pane shows), and
  // a run still measuring waits for its end. Kelvin's analyze tool in the chat shows its own analysis here the same way.
  // (The routes that work out a spec by hand, /api/analysis with its columns, Keep and CSV, stay for the phones.)
  const ANA = { open: false, run: "", runs: [], follow: false, done: new Map(), job: null, ctl: null };
  const anWhen = (r) => startedText(r);   // a run's time in the History's words (this computer's time and day)
  const anRunLabel = (r) => r.plan + " · " + anWhen(r) + (r.status && r.status !== "done" ? " · " + r.status : "")
    + (r.unshared ? " · unshared" : "");

  function toggleAnalysis(open) {
    ANA.open = open === undefined ? !ANA.open : open;
    $("anpane").hidden = !ANA.open;
    $("toggle-analysis").setAttribute("aria-pressed", String(ANA.open));
    $("toggle-analysis").classList.toggle("on", ANA.open);
    fitLayout();
    if (!ANA.open) return;   // an analysis at work goes on: it shows when the pane is back
    if (!ANA.run || ANA.follow) startAnalysis();   // the newest run
    else loadAnalysisRuns();   // runs measured since it was last open
  }
  async function loadAnalysisRuns() {
    // the runs this chat's model may see: the subagent reads them as that model
    try { ANA.runs = (await api("/api/runs?limit=100&chat=" + encodeURIComponent(S.chatId || ""))).runs; } catch (e) { ANA.runs = []; }
    $("an-run").replaceChildren(...ANA.runs.map((r) => h("option", { value: r.run_id }, anRunLabel(r))));
    pickShown(ANA.run);
  }
  function pickShown(id) {   // the picker on the dataset shown (one older than the list's too)
    if (id && !ANA.runs.some((r) => r.run_id === id)) $("an-run").append(h("option", { value: id }, id));
    $("an-run").value = id || "";
  }
  // the picker as it is now, while the pane is open: a run ended, or another chat (whose model may see other runs)
  // opened. A pane that follows the newest run analyzes it; one showing the dataset the person picked keeps it
  async function refreshAnalysisRuns() {
    if (!ANA.open) return;
    if (!ANA.run || ANA.follow) return startAnalysis();
    await loadAnalysisRuns();
  }
  // the pane opened by a person: the run the Runs pane shows, else the newest, followed until the person picks one
  async function startAnalysis() {
    await loadAnalysisRuns();
    if (!ANA.runs.length) { drawAnalysis(anJob("", { notes: ["No runs yet."] })); return; }
    const shown = RUNS.active && ANA.runs.some((r) => r.run_id === RUNS.active) ? RUNS.active : ANA.runs[0].run_id;
    ANA.follow = true;
    analyzeRun(shown);
  }
  // a dataset in the pane: what its analysis said so far (its steps, figures, notes), and once done what it keeps
  function anJob(run, more) {
    const r = ANA.runs.find((x) => x.run_id === run);
    return Object.assign({ run, key: r ? (r.status || "") + "/" + (r.points || 0) : "", figures: [], kept: null,
      summary: "", notes: [], error: "", working: false, step: "" }, more || {});
  }
  // a dataset picked (by the person, the pane following the newest run, a run's Analyze): what was found before, or
  // Kelvin's analysis subagent at work on it. again: anew, whatever was found
  async function analyzeRun(id, again) {
    if (!again && ANA.ctl && ANA.job && ANA.job.run === id && ANA.job.working) { pickShown(id); return; }   // at work on it
    if (ANA.ctl) { ANA.ctl.abort(); ANA.ctl = null; }   // the dataset before: its analysis stops, and so does its model
    ANA.run = id;
    pickShown(id);
    const fresh = anJob(id), kept = ANA.done.get(id);
    if (!again && kept && kept.key === fresh.key) { drawAnalysis(kept); return; }
    const r = ANA.runs.find((x) => x.run_id === id);
    if (!again && ANA.follow && r && r.status === "running") {   // analyzed once it ends (refreshAnalysisRuns)
      drawAnalysis(anJob(id, { kept: [], notes: ["Still measuring: Kelvin analyzes it once it ends."] }));
      return;
    }
    const job = Object.assign(fresh, { working: true });
    drawAnalysis(job);
    const ctl = new AbortController();
    ANA.ctl = ctl;
    try {
      const res = await fetch("/api/analysis/auto", { method: "POST", signal: ctl.signal,
        headers: { "X-Kelvin-Token": token, "Content-Type": "application/json" },
        body: JSON.stringify({ run: id, chat: S.chatId || "", model: $("model").value || "", again: !!again }) });
      if (!res.ok) {
        let msg = "HTTP " + res.status;
        try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
        throw new Error(msg);
      }
      await readEvents(res, (ev) => anEvent(job, ev));
    } catch (e) {
      if (ctl.signal.aborted) return;   // another dataset was picked: it shows now
      job.error = job.error || "The analysis stopped: " + e.message;
    } finally {
      if (ANA.ctl === ctl) ANA.ctl = null;
    }
    job.working = false;
    if (!job.error && job.kept && job.complete) ANA.done.set(id, job);   // found: shown as it is when picked again
    if (ANA.job === job) drawAnalysis(job);
  }
  function anEvent(job, ev) {   // what the subagent says as it works, drawn as it comes while its dataset is shown
    const here = ANA.job === job;
    if (ev.type === "step") { job.step = ev.text; if (here) anStatus(job); }
    else if (ev.type === "figure") { job.figures.push(ev); if (here) $("an-figures").append(anFigureEl(ev)); }
    else if (ev.type === "note") { job.notes.push(ev.text); if (here) anNotes(job); }
    else if (ev.type === "error") { job.error = ev.message; if (here) anNotes(job); }
    else if (ev.type === "done") {
      job.kept = ev.figures || [];
      job.summary = ev.summary || "";
      job.complete = !!ev.complete;   // the model's whole analysis (not one cut short, nor the dataset as measured)
      job.working = false;
      if (here) drawAnalysis(job);
    }
  }
  function drawAnalysis(job) {
    ANA.job = job;
    anStatus(job);
    anNotes(job);
    $("an-summary").textContent = job.summary || "";
    $("an-summary").hidden = !job.summary;
    const shown = job.kept ? job.kept.map((n) => job.figures.find((f) => f.n === n)).filter(Boolean) : job.figures;
    $("an-figures").replaceChildren(...shown.map(anFigureEl));
    $("an-again").disabled = !job.run || job.working;
  }
  function anStatus(job) {   // at work: what it does now
    $("an-status").hidden = !job.working;
    if (job.working) $("an-status").replaceChildren(h("span", { class: "spinner" }), h("span", {}, (job.step || "Starting") + "…"));
  }
  function anNotes(job) {
    const said = job.notes.map((t) => h("p", {}, t)).concat(job.error ? [h("p", { class: "bad" }, job.error)] : []);
    $("an-note").replaceChildren(...said);
    $("an-note").hidden = !said.length;
  }
  // one figure: its title, the plot, the processing steps it took, its fit's results (drawn once, kept with it)
  function anFigureEl(f) {
    if (f.el) return f.el;
    const r = f.result || {}, a = r.axes || {}, all = r.curves || [];
    const curves = all.slice(0, SERIES);
    const many = new Set(curves.map((c) => c.run)).size > 1;
    for (const c of curves) {   // a curve's name: its group, and which run only when there are several
      const run = ANA.runs.find((x) => x.run_id === c.run);
      const rest = c.label.startsWith(c.run) ? c.label.slice(c.run.length).replace(/^ · /, "") : c.label;
      const who = run ? run.plan + " " + anWhen(run) : c.run;
      c.name = many ? who + (rest ? " · " + rest : "") : rest || a.y || who;
    }
    const series = curves.map((c) => ({ name: c.name, points: c.x.map((x, i) => ({ x, y: c.y[i] }))
      .filter((p) => typeof p.x === "number" && typeof p.y === "number") })).filter((s) => s.points.length);
    const fits = curves.map((c) => (c.fit && !c.fit.error ? c.fit.curve : null));
    const one = series.length === 1 && fits[0] ? { x: fits[0].x, y: fits[0].y, label: curves[0].fit.model + " fit" } : null;
    const notes = (r.notes || []).slice();
    if (all.length > SERIES) notes.push(all.length + " curves: the first " + SERIES + " are drawn");
    const steps = f.steps || [];
    f.el = h("figure", { class: "an-figure" },
      h("figcaption", { class: "an-fig-title" }, f.title || "Figure"),
      h("div", { class: "an-plot" }, series.length ? lineChart({ title: a.y || "y", series, xKey: a.x || "x", xUnit: a.x_unit || "",
        yUnit: a.y_unit || "", height: 230, fit: one, fits: one ? null : fits }) : h("div", { class: "pane-empty" }, "No points")),
      steps.length ? h("ol", { class: "an-taken", "aria-label": "Processing steps" }, ...steps.map((s) => h("li", {}, s))) : null,
      notes.length ? h("p", { class: "an-msg" }, notes.join(" · ")) : null,
      anFits(curves));
    return f.el;
  }
  function pm(v, err, unit) {   // 4.0012 ± 0.0003 GHz
    if (typeof v !== "number") return "—";
    if (typeof err !== "number" || !(err > 0)) return fmt(v, unit);
    const [f, p] = prefixFor(Math.abs(v) || err, unit);
    const d = Math.max(0, Math.min(8, 1 - Math.floor(Math.log10(err / f))));
    return (v / f).toFixed(d) + " ± " + (err / f).toFixed(d) + (unit ? " " + p + unit : "");
  }
  const sig = (v, n) => (typeof v === "number" ? String(Number(v.toPrecision(n || 3))) : "—");
  function anFits(curves) {   // each fitted curve's parameters with their errors, then χ²ᵣ and R²
    const fitted = curves.filter((c) => c.fit);
    if (!fitted.length) return null;
    return h("div", { class: "an-fit" }, ...fitted.map((c) => {
      const head = fitted.length > 1 ? h("div", { class: "an-fit-head" }, c.name || c.label) : null;
      if (c.fit.error) return h("div", { class: "an-fit-card bad" }, head, c.fit.error);
      const rows = c.fit.params.map((p) => h("tr", {}, h("th", {}, p.name), h("td", {}, pm(p.value, p.error, p.unit) + (p.held ? " (held)" : ""))));
      for (const d of c.fit.derived || []) rows.push(h("tr", { class: "derived" }, h("th", {}, d.name), h("td", {}, pm(d.value, d.error, d.unit))));
      return h("div", { class: "an-fit-card" }, head, h("table", { class: "an-params" }, ...rows),
        h("div", { class: "an-fit-foot" }, "χ²ᵣ " + sig(c.fit.chi2_red) + " · R² " + sig(c.fit.r2, 4) + (c.fit.converged ? "" : " · not converged")),
        c.fit.warnings && c.fit.warnings.length ? h("ul", { class: "an-warn" }, ...c.fit.warnings.map((w) => h("li", {}, w))) : null);
    }));
  }
  // the pane brought to the screen with a dataset: a run's Analyze (run: the subagent analyzes it), or Kelvin's
  // analyze tool in the chat (spec, result and steps: Kelvin's own analysis, shown as the subagent's figures are)
  // asked: the person's own Analyze, which brings the pane to the screen even in full screen or under the list;
  // Kelvin's analysis opens it where it stands, and under full screen or the list it waits, as Runs does when a run starts
  async function openAnalysis(ev) {
    ev = ev || {};
    ANA.follow = false;   // the dataset asked for, not the newest
    const seen = paneVisible("anpane");
    if (!ANA.open) {
      ANA.open = true;   // shown without starting the pane's own default
      $("anpane").hidden = false;
      $("toggle-analysis").setAttribute("aria-pressed", "true");
      $("toggle-analysis").classList.add("on");
      fitLayout();
    }
    if (seen) { look("anpane"); ring($("anpane").querySelector(":scope > header")); }   // asked for: looked at
    else if (ev.asked) fullShow("anpane");   // to the screen, even in full screen or under the list
    const result = ev.result, spec = ev.spec || {};
    const run = ev.run || (result && (result.curves || []).length ? result.curves[0].run : (spec.runs || [])[0]) || "";
    await loadAnalysisRuns();
    if (!result) { if (run) analyzeRun(run); else startAnalysis(); return; }
    if (ANA.ctl) { ANA.ctl.abort(); ANA.ctl = null; }
    ANA.run = run;
    pickShown(run);
    const a = result.axes || {};
    const job = anJob(run, { kept: [1], notes: ["Kelvin's analysis, from the chat."],
      figures: [{ n: 1, title: (a.y || spec.y || "y") + " against " + (a.x || spec.x || "x"), spec, result, steps: ev.steps || [] }] });
    if (run) ANA.done.set(run, job);
    drawAnalysis(job);
  }
  function initAnalysis() {
    $("toggle-analysis").addEventListener("click", () => pillClick("anpane", toggleAnalysis));
    $("close-analysis").addEventListener("click", () => toggleAnalysis(false));
    $("an-run").addEventListener("change", () => {
      ANA.follow = false;   // the person's pick
      analyzeRun($("an-run").value);
    });
    $("an-again").addEventListener("click", () => { if (ANA.run) analyzeRun(ANA.run, true); });
  }

  const SUGGESTIONS = [
    ["Check the lab", "What is on, and is anything away from safe?"],
    ["Set up my instruments", "Find the instruments connected to this computer and set them up."],
    ["Plan an experiment", "Help me plan an experiment: ask me what I want to find out, then propose a plan."],
    ["Look at my data", "Plot my latest run and tell me what you see."],
  ];

  // ------------------------------------------------------------------ start
  // ------------------------------------------------------------------ Chat | Home, and the parts kept apart
  // Home (home.js), the sequence (seq.js) and the first start (welcome.js) are files of their own: they hear the page
  // through kelvin:* events on document (ready, state, mode, seq) and use what it lends them on window.Kelvin
  function emit(name, detail) { document.dispatchEvent(new CustomEvent("kelvin:" + name, { detail })); }
  // the top bar's switch: Home takes the conversation's place; the panels beside them stay as they are. Either side,
  // even the one chosen, brings the main column back on the screen (exitFull): full screen ends, and a pane alone or
  // the sidebar in its place goes. The side isn't kept: Kelvin always opens on the chat (initMode)
  function setMode(mode, quiet) {
    const home = mode === "home", was = S.mode;
    const change = !was || (was === "home") !== home;
    if (change) {
      S.mode = home ? "home" : "chat";
      $("app").classList.toggle("home-on", home);
      $("homeview").hidden = !home;
      for (const v of ["scroller", "homeview"]) $(v).style.animation = "";   // a real switch: the view comes in (app.css)
      for (const [id, on] of [["mode-chat", !home], ["mode-home", home]]) {
        const b = $(id);
        b.classList.toggle("on", on);
        b.setAttribute("aria-selected", String(on));
        b.tabIndex = on ? 0 : -1;
      }
    }
    if (was) exitFull();   // not on loading: nothing is laid out yet
    if (change) emit("mode", { mode: S.mode });
    layoutSaid();
    if (!home && !quiet) $("input").focus();
  }
  // words for Kelvin from Home: the conversation, with them in the message box, or sent at once (send)
  function ask(text, send) {
    setMode("chat", true);
    if (send) { sendMessage(text); return; }
    $("input").value = text;
    autosize();
    updateSend();
    $("input").focus();
  }
  function initMode() {
    $("mode-chat").addEventListener("click", () => setMode("chat"));
    $("mode-home").addEventListener("click", () => setMode("home"));
    $("mode-seg").addEventListener("keydown", (e) => {   // a tab list: the arrows, Home and End move along it
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const t = $("mode-seg").querySelector(":scope > .seg-thumb");   // the keyboard's change: the thumb jumps (still)
      if (t) { t.classList.add("still"); setTimeout(() => t.classList.remove("still"), 80); }
      setMode(e.key === "ArrowLeft" || e.key === "Home" ? "chat" : "home");
      $(S.mode === "home" ? "mode-home" : "mode-chat").focus();
    });
    setMode("chat", true);   // entering Kelvin: the chat, never Home, whichever side was on when it was left
  }
  // ------------------------------------------------------------------ motion, as labMaster moves (app.css, the end):
  // what switches on one curve (quick out, a long settle), what appears on another; none when asked for less motion
  // a switch (.mode-seg, .seg): a raised thumb under the one that is on, which slides to the one turned on; placed
  // again whenever its buttons change or it changes size (a count in it, or shown at last), without sliding when it
  // was out of sight
  function thumb(seg) {
    if (!seg || seg.querySelector(":scope > .seg-thumb")) return;
    const t = h("span", { class: "seg-thumb", "aria-hidden": "true" });
    seg.prepend(t);
    seg.classList.add("has-thumb");
    let shown = false;
    const place = () => {
      const on = seg.querySelector(":scope > button.on");
      if (!on || !on.offsetWidth) { t.style.opacity = "0"; shown = false; return; }
      if (!shown) t.classList.add("still");
      t.style.width = on.offsetWidth + "px";
      t.style.height = on.offsetHeight + "px";
      t.style.transform = "translate(" + on.offsetLeft + "px, " + on.offsetTop + "px)";
      t.style.opacity = "1";
      if (!shown) { void t.offsetWidth; t.classList.remove("still"); shown = true; }
    };
    new MutationObserver(place).observe(seg, { subtree: true, childList: true, attributes: true, attributeFilter: ["class"] });
    if (window.ResizeObserver) new ResizeObserver(place).observe(seg);
    place();
  }
  // what a fold opens comes in on the reveal curve (.k-in), once
  function easeIn(els) {
    for (const el of els) {
      el.classList.remove("k-in");
      void el.offsetWidth;
      el.classList.add("k-in");
      const done = (e) => {
        if (e.target !== el) return;
        el.classList.remove("k-in");
        el.removeEventListener("animationend", done);
      };
      el.addEventListener("animationend", done);
    }
  }
  function initMotion() {
    for (const seg of document.querySelectorAll(".mode-seg, .seg")) thumb(seg);
    // a top-bar button's name eases in under it on hover (app.css), in place of the browser's tip: its title, whenever
    // one is set (Incognito's is its name, "Incognito chat", on or off)
    const tip = (p) => { if (p.title) { p.dataset.tip = p.title; p.removeAttribute("title"); } };
    for (const p of document.querySelectorAll(".topbar-right .pill")) {
      tip(p);
      new MutationObserver(() => tip(p)).observe(p, { attributes: true, attributeFilter: ["title"] });
    }
  }
  // the Lab panel's parts (Instruments, Watches, Memory, Activity), under what never gives way to them: the requests
  // waiting, the sample, Hands on the sample and Bring to safe. One part at a time, chosen on a strip of tabs above
  // them, the chosen part with all the height below and kept on this browser (lh.labPart; a part that shows nothing is
  // never chosen again). Each tab says what is new in its part, to the eye and to a screen reader: the alarms on
  // Watches, a dot on Activity for lines not seen there yet. A tab list as the APG's: the arrow keys, Home and End
  // choose along it at once, and the chosen tab is its one tab stop. Each tab is named as its part's heading. Wide (the
  // Lab 900 px or more: full screen, or dragged wide), every part stands in its column under its heading and the strip
  // steps aside ("fit D" in app.css); there the part used last (pressed, or holding the focus) is the chosen one, so
  // that when the Lab narrows again (Esc, ⤢, a narrower window) that part, and the focus in it, stay on the screen.
  // Every way into a part (its Add…, a note's pencil) chooses its tab
  const LAB_TABS = { tabs: new Map(), chosen: "", wide: null, top: null, seen: null };   // top: Activity's newest line
  const labTabShown = (id) => LAB_TABS.tabs.has(id) && !$("lab-sec-" + id).hidden;
  function initLabTabs() {
    const strip = $("lab-strip"), panel = $("labpanel");
    forgetLabFolds();
    for (const part of document.querySelectorAll("#labpanel .lab-part")) {
      const id = part.dataset.part, h3 = part.querySelector(".lab-part-head h3");
      part.id = "lab-sec-" + id;
      const tab = h("button", { type: "button", role: "tab", id: "lab-tab-" + id, "aria-controls": part.id },
        h("span", { class: "lab-tab-name" }, h3.textContent.trim()),
        h("span", { class: "lab-tab-mark", "aria-hidden": "true", hidden: true }), h("span", { class: "sr-only" }));
      tab.dataset.v = id;
      tab.addEventListener("click", () => chooseLabPart(id));
      strip.append(tab);
      LAB_TABS.tabs.set(id, tab);
    }
    const used = (e) => {   // a press or the focus in a part (narrow, only the chosen one can have either)
      const part = e.target.closest ? e.target.closest("#labpanel .lab-part") : null;
      if (part && part.dataset.part !== LAB_TABS.chosen) chooseLabPart(part.dataset.part);
    };
    panel.addEventListener("focusin", used);
    panel.addEventListener("pointerdown", used);
    strip.addEventListener("keydown", labTabKeys);
    if (window.ResizeObserver) new ResizeObserver(labTabRoles).observe($("labpanel"));
    labTabRoles();
    const kept = store("lh.labPart");
    chooseLabPart(labTabShown(kept) ? kept : [...LAB_TABS.tabs.keys()].find(labTabShown));
  }
  function chooseLabPart(id) {
    if (!labTabShown(id)) return;
    LAB_TABS.chosen = id;
    store("lh.labPart", id);
    for (const [k, tab] of LAB_TABS.tabs) {
      const on = k === id;
      tab.hidden = $("lab-sec-" + k).hidden;   // a part that shows nothing has no tab
      tab.classList.toggle("on", on);
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      $("lab-sec-" + k).classList.toggle("chosen", on);
    }
    if (id === "activity") labTabSeen();   // its new lines seen
  }
  // ← and → the part before or after (round), Home and End the first and the last: chosen at once, as the strip's thumb
  // jumps there (nothing moves for the keyboard)
  function labTabKeys(e) {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const tabs = [...LAB_TABS.tabs.values()].filter((t) => !t.hidden), i = tabs.indexOf(LAB_TABS.tabs.get(LAB_TABS.chosen));
    const to = { ArrowLeft: i - 1, ArrowRight: i + 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (to === undefined || !tabs.length) return;
    e.preventDefault();
    const tab = tabs[(to + tabs.length) % tabs.length], t = $("lab-strip").querySelector(":scope > .seg-thumb");
    if (t) { t.classList.add("still"); setTimeout(() => t.classList.remove("still"), 80); }
    chooseLabPart(tab.dataset.v);
    tab.focus();
  }
  // the parts are the tabs' panels while the strip shows; wide, where it has stepped aside, plain sections under their
  // headings. Read whenever the Lab changes size (its container query decides). Narrowing, the part holding the focus
  // is the one shown, even where no focus event said so (focused while the window itself was not)
  function labTabRoles() {
    const wide = getComputedStyle($("lab-strip")).display === "none";
    if (wide === LAB_TABS.wide) return;
    LAB_TABS.wide = wide;
    const at = !wide && document.activeElement && document.activeElement.closest("#labpanel .lab-part");
    if (at && at.dataset.part !== LAB_TABS.chosen) chooseLabPart(at.dataset.part);
    for (const [id, tab] of LAB_TABS.tabs) {
      const part = $("lab-sec-" + id);
      if (wide) { part.removeAttribute("role"); part.removeAttribute("tabindex"); }
      else { part.setAttribute("role", "tabpanel"); part.tabIndex = 0; }
      part.setAttribute("aria-labelledby", wide ? "lab-h-" + id : tab.id);
    }
    labTabSeen();
  }
  function labTabMark(id, mark) {   // "" or 0 none, a number (Watches' alarms) or true (a dot: Activity's new lines)
    const tab = LAB_TABS.tabs.get(id), m = tab && tab.querySelector(".lab-tab-mark");
    if (!m || m.dataset.mark === String(mark || "")) return;   // written only when it changed
    m.dataset.mark = String(mark || "");
    m.textContent = mark === true ? "" : String(mark || "");
    m.classList.toggle("dot", mark === true);
    m.hidden = !mark;
    // and said with the tab's name: "Watches, 2 alarms", "Activity, new lines"
    tab.querySelector(".sr-only").textContent = !mark ? "" : mark === true ? ", new lines"
      : ", " + mark + (Number(mark) === 1 ? " alarm" : " alarms");
  }
  // Activity's newest line is seen while its part is on the screen (chosen, or every part shown); else a dot on its tab
  function labTabSeen() {
    if (!LAB_TABS.tabs.size || LAB_TABS.top === null) return;
    if (LAB_TABS.seen === null || (paneVisible("labpanel") && (LAB_TABS.chosen === "activity" || LAB_TABS.wide))) {
      LAB_TABS.seen = LAB_TABS.top;
    }
    labTabMark("activity", LAB_TABS.top !== LAB_TABS.seen);
  }
  // what the Lab's folds once kept on this browser, by part (lh.labFold.<part>) or by heading (lh.fold.<heading>): let
  // go at the start, quietly, as nothing reads it. Settings' folds keep theirs (lh.fold.set.<id>)
  function forgetLabFolds() {
    try {   // the keys gathered first: a removal may reorder what is left (WebKit)
      const old = Object.keys(localStorage).filter((k) => /^lh\.(labFold|fold)\.(?!set\.)/.test(k));
      for (const k of old) localStorage.removeItem(k);
    } catch (e) { /* no storage */ }
  }
  // Settings: an item with more than two fields folds under its title, folded until the title is clicked. An item is a
  // title (a label, or the row a .field-label heads) and what follows it in its .field up to the next title: a whole
  // .field (Email, Heartbeat) or a part of one (Webhook, after the Notifications row). Its fields: the boxes, lists and
  // tick boxes in it, and a key's Remove. Folding only hides them: each keeps its id and its value, and Save reads every
  // one, folded or not. A title the page hides (its first item's, said by the page's own title) never folds.
  function initSettingFolds() {
    for (const head of document.querySelectorAll("#settings-dialog [data-fold]")) {
      const label = head.matches(".field-row") ? head.querySelector(".field-label") : head;
      if (!label || label.querySelector(".fold-btn")) continue;
      const part = [], key = "lh.fold.set." + head.dataset.fold;   // what folds: up to the next title
      for (let el = head.nextElementSibling; el && !el.hasAttribute("data-fold"); el = el.nextElementSibling) part.push(el);
      part.forEach((el, k) => { if (!el.id) el.id = "fold-" + head.dataset.fold + "-" + k; });
      const btn = h("button", { class: "fold-btn", type: "button", "aria-expanded": "false",
        "aria-controls": part.map((el) => el.id).join(" ") }, icon("chev"), label.textContent.trim());
      label.replaceChildren(btn);
      const fold = (folded) => {
        head.classList.toggle("folded", folded);
        btn.setAttribute("aria-expanded", String(!folded));
        for (const el of part) el.classList.toggle("fold-hide", folded);
      };
      fold(store(key) !== "1");
      btn.addEventListener("click", () => {
        const folded = !head.classList.contains("folded");
        fold(folded);
        store(key, folded ? "0" : "1");
        if (!folded) easeIn(part);
      });
    }
  }
  // what the parts kept apart may use: the page's helpers, its state, and the panels and dialogs they open
  window.Kelvin = {
    $, h, api, icon, md, toast, fmt, store, S, MARK,
    setMode, ask, sendMessage, newChat, openChat, pollState,
    toggleSeq, toggleRuns, toggleLab, toggleAnalysis, openRun, openRunList, showView, showScan, fitLayout, exitFull, thumb,
    openEditor, openVersions, openLabSetup, openSettings, settingsPage, openCards,
    pickFolder, hasPicker, hasFilePicker, isVI, importVI, openReview, folderLabels, popMenu, closeMenu, lineChart, getRun, runCharts,
    zoom: pageZoom, askYes, askText, fitTiers,   // the fit's contract (the page size, the in-page question)
  };

  async function init() {
    if (!token) { $("nokey").hidden = false; return; }
    let session = null;
    try { session = await api("/api/session"); } catch (e) { /* the bootstrap below says what is wrong */ }
    if (session && session.accounts_on && !session.user) { showSignIn(session); return; }
    let boot;
    try { boot = await api("/api/bootstrap"); } catch (e) {
      $("nokey").hidden = false;
      $("nokey").querySelector("p").textContent = "Kelvin did not accept this link (" + e.message + "). Open the link Kelvin printed when it started.";
      return;
    }
    $("app").hidden = false;
    for (const slot of document.querySelectorAll("[data-mark]")) slot.innerHTML = MARK;
    S.settings = boot.settings;
    appZoomOff();   // in the desktop app: its window's own zoom off, the old text size kept as the page size (once)
    S.claudeCode = !!boot.claude_code;   // Claude Code bridge (temporary)
    $("hub-field").hidden = $("project-sharing-field").hidden = !boot.hub_sharing;   // off for now: the hub is for accounts
    showUpdate(boot.update);
    $("update-go").addEventListener("click", installUpdate);
    $("update-check").addEventListener("click", checkUpdate);
    setInterval(async () => { try { showUpdate(await api("/api/update")); } catch (e) { /* next time */ } }, 10 * 60 * 1000);
    S.account = boot.account || null;
    renderAccount();
    $("sign-out").addEventListener("click", signOut);
    $("pw-change").addEventListener("click", changePassword);
    $("account-delete-go").addEventListener("click", deleteAccount);
    // Activity: this person's lines, or the lab's instrument log, everyone's by name (a view's filter, kept here)
    const labWho = (v) => {
      PANEL.everyone = v === "all";
      store("lh.labEveryone", PANEL.everyone ? "1" : "0");
      segMark($("activity-who"), v);
      pollState();
    };
    segMark($("activity-who"), PANEL.everyone ? "all" : "mine");
    for (const b of $("activity-who").querySelectorAll("button")) b.addEventListener("click", () => { if (!b.classList.contains("on")) labWho(b.dataset.v); });
    segKeys($("activity-who"), labWho);
    S.chats = boot.chats;
    S.projects = boot.projects || [];
    $("lab-name").textContent = boot.lab;
    S.labSub = boot.lab;
    $("empty-sub").textContent = S.labSub;
    $("toggle-incognito").addEventListener("click", toggleIncognito);
    applyIncognito();
    window.addEventListener("pagehide", () => {   // closing the page also forgets an incognito chat
      if (S.incognito && S.chatId) fetch("/api/chats/" + S.chatId, { method: "DELETE", keepalive: true, headers: { "X-Kelvin-Token": token } });
    });
    $("suggestions").replaceChildren(...SUGGESTIONS.map(([title, text]) => {
      const b = h("button", { class: "suggestion", type: "button", title: text }, h("b", {}, title));
      b.addEventListener("click", () => sendMessage(text));
      return b;
    }));
    applyModels(boot.models.groups, boot.default_model, boot.auto_model, boot.models.hub);
    initPanes();
    initMode();
    initLabTabs();
    initSettingFolds();
    initMotion();
    initRunsPane();
    initTeam();
    initAnalysis();
    initEditor();
    fitLayout();
    initTerminal();
    renderChats();
    showEmpty(true);
    loadNewFolder();
    loadStartChecklist();

    $("help-go").addEventListener("click", helpSearch);
    $("help-q").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); helpSearch(); } });
    $("eln-day-go").addEventListener("click", () => fetchDownload("/api/eln?day=" + encodeURIComponent($("eln-day").value || "today")
      + "&chat=" + encodeURIComponent(S.chatId || ""),   // this chat's view: never another project's unshared work
      ($("eln-day").value || "today") + "-lab-record.zip", "day's lab notebook record"));
    $("owner-pin-set").addEventListener("click", () => ownerPinChange(false));
    $("owner-pin-remove").addEventListener("click", () => ownerPinChange(true));
    $("roles-edit").addEventListener("click", editRoles);
    $("roles-cancel").addEventListener("click", () => { $("roles-list").hidden = $("roles-actions").hidden = true; });
    $("owner-notify-mine").addEventListener("click", () => ownerNotify("mine"));
    $("owner-notify-off").addEventListener("click", () => ownerNotify("off"));
    for (const id of ["history-open", "lab-history"]) $(id).addEventListener("click", openHistory);
    for (const id of ["history-subject", "history-who", "history-safety"]) $(id).addEventListener("change", loadHistory);
    $("composer").addEventListener("submit", (e) => {
      e.preventDefault();
      if (S.streaming && !$("input").value.trim()) api("/api/stop", { body: { chat_id: S.chatId } }).catch(() => {});
      else sendMessage($("input").value);
    });
    $("input").addEventListener("input", () => { autosize(); updateSend(); slashMenu(); });
    $("input").addEventListener("keydown", (e) => {
      if (slashKey(e)) return;
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage($("input").value); }
    });
    $("input").addEventListener("blur", () => setTimeout(closeSlash, 150));
    $("model").addEventListener("change", () => {
      updateSend(); showPlanChip();
      S.compare = S.compare.filter((k) => k !== $("model").value); compareLabel();
    });
    $("compare-btn").addEventListener("click", toggleCompareMenu);
    applyCompare();   // hidden until Settings → Models switches it on
    swFlip($("compare-on"), setCompare);
    swFlip($("phone-on"), switchPhone);
    $("svc-preset").addEventListener("change", servicePreset);
    $("chatgpt-signin").addEventListener("click", signInChatGPT);
    $("chatgpt-signout").addEventListener("click", signOutChatGPT);
    $("hub-connect").addEventListener("click", connectHub);
    $("hub-url").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); connectHub(); } });
    $("hub-sync").addEventListener("click", syncHub);
    $("collection-sync").addEventListener("click", () => loadCollection(true));
    $("hub-disconnect").addEventListener("click", disconnectHub);
    $("svc-add").addEventListener("click", addService);
    $("data-path").addEventListener("input", dataPathChanged);
    $("data-path").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); if (!$("data-use").disabled) useDataFolder(); } });
    $("data-use").addEventListener("click", useDataFolder);
    $("global-know-add").addEventListener("click", () => $("global-know-file").click());
    $("global-know-file").addEventListener("change", (e) => {
      const f = e.target.files[0]; e.target.value = "";
      addSkill(f, "global", () => loadKnow("global", $("global-know"), $("global-know-count"), $("settings-msg")), $("settings-msg"));
    });
    $("project-know-add").addEventListener("click", () => $("project-know-file").click());
    $("project-know-file").addEventListener("change", (e) => {
      const f = e.target.files[0]; e.target.value = "";
      const p = editingProject;
      if (p) addSkill(f, p.id, () => loadKnow(p.id, $("project-know"), $("know-count"), $("project-msg")), $("project-msg"));
    });
    $("data-choose").addEventListener("click", async () => {   // the computer's picker in the app, Kelvin's in a browser
      const path = await pickFolder($("data-path").value.trim(), "Choose the data folder", { create: true });
      if (path) { $("data-path").value = path; dataPathChanged(); }
    });
    $("scripts-add").addEventListener("click", addScriptFolder);
    $("data-open").addEventListener("click", () => api("/api/data-folder", { body: { open: true } }).catch((e) => showDataFolder(e.message, true)));
    $("firewall-allow").addEventListener("click", allowFirewall);
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".compare-wrap")) { $("compare-menu").hidden = true; $("compare-btn").setAttribute("aria-expanded", "false"); }
      if (!e.target.closest(".msg-actions")) for (const m of document.querySelectorAll(".review-menu")) m.hidden = true;
    });
    $("approval-mode").addEventListener("change", (e) => changeMode(e.target.value));
    $("open-lab-setup").addEventListener("click", openLabSetup);
    $("open-cards").addEventListener("click", openCards);
    $("card-draft").addEventListener("click", draftCard);
    $("card-check").addEventListener("click", checkCard);
    $("card-save").addEventListener("click", saveCard);
    $("card-try").addEventListener("click", tryCard);
    $("card-file").addEventListener("change", async (e) => {
      const files = [...e.target.files];
      if (!files.length) return;
      if (files.reduce((n, f) => n + f.size, 0) > 4e6) { $("cards-msg").textContent = "Those files are large; choose the command reference, the examples and the header only."; return; }
      const parts = [];
      for (const f of files) {
        let text = await f.text();
        if (/\.html?$/i.test(f.name)) text = new DOMParser().parseFromString(text, "text/html").body.innerText;
        if (/\.ipynb$/i.test(f.name)) {   // a notebook: its code and notes, not its outputs
          try { text = (JSON.parse(text).cells || []).filter((c) => c.cell_type !== "raw").map((c) => [].concat(c.source || []).join("")).join("\n\n"); } catch (err) { /* keep the raw text */ }
        }
        parts.push(files.length > 1 ? "=== " + f.name + " ===\n" + text : text);
      }
      $("card-manual").value = parts.join("\n\n");
      if (!$("card-name").value) $("card-name").value = files[0].name.replace(/\.[^.]+$/, "");
    });
    $("open-report").addEventListener("click", openReport);
    $("mount-select").addEventListener("change", mountChosen);
    $("mount-new").addEventListener("click", newDevice);
    $("mount-protect").addEventListener("click", openProtection);
    swFlip($("relay-on"), setRelay);
    $("protect-add-gate").addEventListener("click", () => $("protect-gates").append(gateRow({ dielectric: "hBN",
      drive: (PROT.opts.drives.find((d) => d.gate) || {}).key, leak: (PROT.opts.drives.find((d) => d.gate) || {}).leak })));
    $("protect-add-limit").addEventListener("click", () => $("protect-limits").append(limitRow((PROT.opts.drives[0] || {}).key)));
    $("protect-save").addEventListener("click", () => saveProtection(false));
    $("protect-clear").addEventListener("click", () => saveProtection(true));
    $("hands-on").addEventListener("click", handsOn);
    $("lab-safe").addEventListener("click", bringToSafe);
    $("hands-done").addEventListener("click", openUnlock);
    $("wake-cancel").addEventListener("click", async () => {
      if (!S.chatId) return;
      try { await api("/api/chats/" + S.chatId + "/wake", { method: "DELETE" }); } catch (e) { toast(e.message); return; }
      $("wake-bar").hidden = true;
      toast("Kelvin won't continue this chat on its own.");
    });
    $("wake-stop").addEventListener("click", () => api("/api/stop", { body: { chat_id: S.chatId } }).catch(() => {}));
    $("attach-btn").addEventListener("click", () => $("attach-input").click());
    $("attach-input").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
    $("input").addEventListener("paste", (e) => {   // a screenshot pasted into the message
      const files = [...((e.clipboardData && e.clipboardData.files) || [])];
      if (!files.length) return;
      if (!e.clipboardData.getData("text/plain")) e.preventDefault();
      addFiles(files);
    });
    $("composer").addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("Files")) e.preventDefault(); });
    $("composer").addEventListener("drop", (e) => {
      if (!e.dataTransfer.files.length) return;
      e.preventDefault();
      addFiles([...e.dataTransfer.files]);
    });
    $("hands-unlock").addEventListener("click", unlock);
    for (const c of document.querySelectorAll(".hands-check")) c.addEventListener("change", () => {
      $("hands-unlock").disabled = ![...document.querySelectorAll(".hands-check")].every((x) => x.checked);
    });
    $("device-add").addEventListener("click", addDevice);
    $("summary-write").addEventListener("click", writeSummary);
    $("master-update").addEventListener("click", () => updateMaster(false));
    $("master-open").addEventListener("click", () => updateMaster(true));
    $("report-folder").addEventListener("click", async () => {
      try { await api("/api/reports/open-folder", { body: {} }); } catch (e) { toast(e.message); }
    });
    loadMount();
    $("make-report").addEventListener("click", makeReport);
    $("watch-add-toggle").addEventListener("click", () => partForm("watch-form", $("watch-form").hidden));
    $("watch-form").addEventListener("submit", addWatch);
    $("alarm-history-btn").addEventListener("click", (e) => {   // a disclosure: a second press closes it
      const box = $("alarm-history");
      foldOpen(box, e.currentTarget, $("alarm-history-fold"), !box.classList.contains("open"), e);
    });
    // a second press closes the form, also while it changes a note (its pencil opened it)
    $("labmem-add").addEventListener("click", () => { if ($("labmem-form").hidden) editLabNote(null); else closeLabNote(); });
    $("labmem-form").addEventListener("submit", keepLabNote);
    $("labmem-cancel").addEventListener("click", closeLabNote);
    $("allow-notify").addEventListener("click", async () => {
      try { await Notification.requestPermission(); } catch (e) { /* not supported */ }
      $("allow-notify").hidden = Notification.permission !== "default";
    });
    $("notify-test").addEventListener("click", async () => {
      $("settings-msg").textContent = "Save first if you changed the address. Sending…";
      try { $("settings-msg").textContent = "Test: " + (await api("/api/notify/test", { body: {} })).result; }
      catch (e) { $("settings-msg").textContent = e.message; }
    });
    $("lab-scan").addEventListener("click", scanLab);
    $("lab-add-found").addEventListener("click", addFound);
    $("lab-scripts").addEventListener("click", fromScripts);
    $("lab-labview").addEventListener("click", fromLabview);
    $("exp-keep").addEventListener("click", () => keepDraft());
    $("exp-approve").addEventListener("click", approveVersion);
    $("lab-keep-scans").addEventListener("click", () => ((LAB.scripts || {}).kind === "specialmeasure" ? keepSmScans() : keepScans()));
    $("lab-sm").addEventListener("click", () => fromSpecialMeasure());
    $("setup-scripts-choose").addEventListener("click", () => answerScripts(false));
    $("setup-scripts-none").addEventListener("click", () => answerScripts(true));
    $("setup-wait").addEventListener("close", () => { WAIT.closed = true; });
    $("lab-add-scripts").addEventListener("click", addScripts);
    $("lab-recipes").addEventListener("click", recipesFromScripts);
    $("lab-add").addEventListener("click", addByAddress);
    $("lab-save").addEventListener("click", () => connectLab({ yaml: $("lab-yaml").value, why: $("lab-change-why").value.trim() }));
    for (const b of document.querySelectorAll("#lab-view button")) b.addEventListener("click", () => {
      LABCFG.view = b.dataset.view;
      store("lh.labView", LABCFG.view);
      renderLabConfig();
    });
    $("lab-find").addEventListener("input", () => { LABCFG.find = $("lab-find").value; renderLabConfig(); });
    $("lab-unlock").addEventListener("click", unlockLab);
    $("lab-setup-again").addEventListener("click", setupAgain);
    $("lab-observe-review").addEventListener("click", openLabSetup);
    $("lab-drafts-review").addEventListener("click", openCards);
    $("open-cards-lab").addEventListener("click", openCards);
    $("lab-why-details").addEventListener("click", toggleWhyDetails);
    $("lab-why-setup").addEventListener("click", openLabSetup);
    $("new-chat").addEventListener("click", () => newChat(null, true));
    $("folder-btn").addEventListener("click", (e) => { e.stopPropagation(); folderMenu(); });
    $("toggle-sidebar").addEventListener("click", toggleSidebar);
    $("toggle-lab").addEventListener("click", () => pillClick("labpanel", toggleLab));
    $("inst-filter").addEventListener("input", (e) => { PANEL.filter = e.target.value; renderInstruments(); });
    const instShown = (v) => {   // All | Active: a view's filter, kept here
      PANEL.activeOnly = v === "active";
      store("lh.activeOnly", PANEL.activeOnly ? "1" : "0");
      renderInstruments();
    };
    for (const b of document.querySelectorAll("#inst-seg button")) b.addEventListener("click", () => instShown(b.dataset.v));
    segKeys($("inst-seg"), instShown);
    holdInstruments();
    $("close-lab").addEventListener("click", () => toggleLab(false));
    $("open-settings").addEventListener("click", openSettings);
    for (const b of document.querySelectorAll(".set-nav button")) {   // each section's name, with its dot while it holds a change
      b.append(h("span", { class: "set-dot", hidden: true }, h("span", { class: "sr-only" }, ", changed")));
      b.addEventListener("click", () => settingsPage(b.dataset.page));
    }
    pageKeys();
    // Settings' Save and Cancel (settingsPending): what waits for Save is weighed at every change in the form; Close (or
    // Cancel) leaves without saving, × and Esc ask first while something waits; Enter in a box closes nothing
    $("save-settings").addEventListener("click", saveSettings);
    $("settings-close").addEventListener("click", () => $("settings-dialog").close());
    $("settings-x").addEventListener("click", settingsLeave);
    document.addEventListener("keydown", (e) => {   // Esc while Settings is the dialog on top, wherever the focus is in it
      const dlg = $("settings-dialog");
      if (e.key !== "Escape" || e.isComposing || e.defaultPrevented || !dlg.open) return;
      const over = [...document.querySelectorAll("dialog[open]")].filter((d) => d !== dlg).pop();
      if (over) {   // a dialog over it takes Esc; a question is answered No here, never by the browser's own close: an Esc
        // gives the page no activation, so after a few the browser would close Settings along with the question it asked
        if (over.classList.contains("ask-dialog")) { e.preventDefault(); over.close(); }
        return;
      }
      e.preventDefault();   // the dialog's own close (settingsLeave), not the browser's
      settingsLeave();
    });
    $("settings-dialog").addEventListener("cancel", (e) => {   // any other close request (a phone's Back)
      if (!e.cancelable) return;
      e.preventDefault();
      settingsLeave();
    });
    const settingsChanged = (e) => {
      if (e.target.dataset && e.target.dataset.clear && e.target.value) keyKept(e.target);   // a removed key typed again
      settingsPending();
    };
    $("settings-form").addEventListener("input", settingsChanged);
    $("settings-form").addEventListener("change", settingsChanged);
    $("settings-form").addEventListener("keydown", settingsEnter);
    $("settings-form").addEventListener("submit", (e) => e.preventDefault());   // nothing in Settings submits it
    $("save-project").addEventListener("click", saveProjectSettings);
    $("project-confidential").addEventListener("change", () => {   // one switch for the rules and the hub's mark
      const on = $("project-confidential").checked;
      if (on) {
        for (const b of document.querySelectorAll("#project-providers input")) b.checked = b.value === "local";
        $("project-literature").checked = false;
      }
      const sharing = $("project-sharing");
      sharing.value = on ? "confidential" : sharing.value === "confidential" ? "" : sharing.value;
    });
    $("project-sharing").addEventListener("change", () => {
      $("project-confidential").checked = $("project-sharing").value === "confidential";
    });
    $("quit-app").hidden = !boot.can_quit;
    $("quit-app").addEventListener("click", async () => {
      if (!(await askYes("Quit Kelvin?\nThe lab keeps its current settings.", { ok: "Quit" }))) return;
      try { await api("/api/quit", { body: {} }); } catch (e) { /* already gone */ }
      document.body.replaceChildren(h("div", { class: "nokey" }, h("h1", {}, "Kelvin has quit"), h("p", {}, "You can close this tab.")));
    });
    for (const b of document.querySelectorAll("[data-clear]")) {   // a key's Remove waits for Save, as any change
      b.addEventListener("click", () => {
        const input = document.querySelector('#settings-form input[name="' + b.dataset.clear + '"]');
        if (!("was" in input.dataset)) input.dataset.was = input.placeholder;
        input.value = ""; input.dataset.clear = "1"; input.placeholder = "Removed on Save";
        settingsPending();
      });
    }
    setInterval(() => { if (S.labOpen || TEAM.open || S.live.size || S.answering.size || !$("lab-badge").hidden) pollState(); }, 1500);
    setInterval(pollState, 4000);   // often enough that an alarm shows within seconds
    emit("ready", boot);
    pollState();
    if (S.mode !== "home") $("input").focus();
  }
  init();
})();
