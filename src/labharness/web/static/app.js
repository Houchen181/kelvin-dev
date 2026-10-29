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
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    incognito: '<path d="M3 11h18"/><path d="M6 11l1.6-5.2h8.8L18 11"/><circle cx="7.5" cy="16" r="2.6"/><circle cx="16.5" cy="16" r="2.6"/><path d="M10.1 16h3.8"/>',
    memory: '<path d="M7 3h10a1 1 0 0 1 1 1v17l-6-4-6 4V4a1 1 0 0 1 1-1z"/>',
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
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
    });
    if (!res.ok) {
      let msg = "HTTP " + res.status, data = null;
      try { data = await res.json(); msg = data.error || msg; } catch (e) { /* not JSON */ }
      if (res.status === 401 && data && data.signin) setTimeout(() => location.reload(), 300);   // signed out elsewhere
      if (res.status === 403 && data && data.pin && opts.body && !opts.noPin) {   // the lab owner's PIN: ask, try again
        const pin = await askPin(msg);
        if (pin) return api(path, Object.assign({}, opts, { body: Object.assign({}, opts.body, { pin }) }));
      }
      const err = new Error(msg);
      err.data = data;
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
  }

  // ------------------------------------------------------------------ who is using Kelvin (accounts)
  const initials = (name) => String(name).split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";

  function hubLink(session, text, edit) {   // which Kelvin hub people sign in to on this computer ("" = accounts here only)
    $(text).textContent = session.central ? "Kelvin hub: " + String(session.hub).replace(/^https?:\/\//, "")
      : "Accounts on this computer only.";
    $(edit).textContent = session.central ? "change" : "Use a Kelvin hub";
    $(edit).onclick = async () => {
      const url = prompt("The Kelvin hub people sign in to on this computer (https://…). Leave it empty for accounts on this computer only.", session.hub || "");
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
      $("signin-sub").textContent = mode === "signin" ? "Your Kelvin account works on every computer." : "";
      if (mode === "signin") (($("c-email").value ? $("c-password") : $("c-email"))).focus();
      if (mode === "new") $("cn-name").focus();
      if (mode === "rekey") $("rekey-old").focus();
    };
    $("signin-foot").textContent = "After you've signed in on a computer once, your password works there offline too. Your chats, data and API keys stay on each computer.";
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
      if (!confirm(everywhere ? "Delete " + name + "'s Kelvin account on every computer, and remove them from this one? This can't be undone."
        : "Remove " + name + " from this computer? Their measurements stay.")) return;
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
      $("signin-sub").textContent = mode !== "new" ? "" : people.length ? "Anyone can add themselves; nobody's account is above another's."
        : session.legacy ? "Make your account. Your chats, runs and API keys from before come with it." : "Make your account to start.";
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
    $("activity-all").hidden = !a.accounts_on;
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
      $("relay-on").checked = relay.on;
      $("relay-status").textContent = !relay.on ? "off" : ({ online: "on: your phone can reach this Kelvin",
        connecting: "connecting…", offline: "on, but the hub can't be reached now",
        "signed out": "the hub signed this computer out: sign in again", off: "off" })[relay.state] || relay.state;
      $("relay-status").title = relay.problem || "";
    }
    const kelvinAccount = !!a.user.central;
    $("account-delete-everywhere-row").hidden = !kelvinAccount;
    $("account-delete-everywhere").checked = false;
    $("account-delete-password").hidden = !a.user.password && !kelvinAccount;
    $("account-delete-what").textContent = "Takes you off this computer: your settings, API keys, ChatGPT sign-in, chats, projects " +
      "and memory. Your measurements (runs, reports and record) stay on disk." +
      (kelvinAccount ? " Your Kelvin account stays for your other computers, unless you also delete it." : "");
    $("account-hint").textContent = a.user.central
      ? "One Kelvin account, on the Kelvin hub, signs you in on every computer; after a first sign-in on a computer it works there offline too. Your settings, API keys, chats, projects, memory, data folder and record stay on each computer, and your API keys are encrypted with your password. The instruments, their cards, the lab switches below and the lab's instrument log are everyone's. Forgot your password? Sign out and choose \"Forgot your password?\"."
      : "Your settings, API keys, chats, projects, memory, data folder and record are yours; the instruments, their cards, the lab switches below and the lab's instrument log are everyone's. With a password, your API keys are encrypted with it. Nobody can reset a forgotten password: there is no administrator. Accounts keep people's work apart in Kelvin; they don't lock files: anyone who can open this computer's files can read the folders.";
  }

  async function setRelay() {
    try {
      await api("/api/settings", { body: { phone_relay: $("relay-on").checked ? "on" : "off" } });
      setTimeout(async () => { try { S.account = await api("/api/session"); renderAccount(); } catch (e) { /* keep */ } }, 1500);
    } catch (e) { toast(e.message); }
  }

  async function signOut() {
    try { await api("/api/session", { method: "DELETE" }); } catch (e) { /* the sign-in screen shows either way */ }
    location.reload();
  }

  async function deleteAccount() {
    const everywhere = $("account-delete-everywhere").checked;
    if (!confirm(everywhere ? "Delete your Kelvin account on every computer, and remove it from this one? This can't be undone."
      : "Remove your account from this computer? Your measurements stay.")) return;
    $("settings-msg").textContent = everywhere ? "Deleting your Kelvin account…" : "Removing…";
    try {
      const r = await api("/api/account", { method: "DELETE", body: { password: $("account-delete-password").value, everywhere } });
      try { sessionStorage.setItem("kelvin-signin-msg", removedText(r)); } catch (e) { /* the sign-in screen shows either way */ }
      location.reload();
    } catch (e) { $("settings-msg").textContent = e.message; }
  }

  async function changePassword() {
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
  function lineChart(opts) {
    const { series, xKey, xUnit, yUnit } = opts;
    const fit = series.length === 1 ? opts.fit : null;  // fitted curve over a single measured series
    const xs = series.flatMap((s) => s.points.map((p) => p.x));
    const ys = series.flatMap((s) => s.points.map((p) => p.y)).concat(fit ? fit.y : []);
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
    let drawnWidth = 0;
    const draw = () => {  // drawn at the real pixel width so text stays 11px
      const W = Math.max(240, Math.round(holder.clientWidth));
      if (W === drawnWidth) return;
      drawnWidth = W;
      holder.replaceChildren(build(W));
    };
    if (window.ResizeObserver) new ResizeObserver(draw).observe(holder);
    requestAnimationFrame(draw);
    return box;

    function build(W) {
    const H = 200, M = { l: 48, r: series.length <= 4 && !fit ? 62 : 14, t: 10, b: 26 };
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
      root.append(svg("path", { class: fit ? "line thin" : "line", d, style: "stroke:" + color }));
      if (fit && s.points.length <= 120) {
        for (const p of s.points) root.append(svg("circle", { class: "datapt", cx: sx(p.x), cy: sy(p.y), r: 2.6, style: "fill:" + color }));
      }
      const last = s.points[s.points.length - 1];
      if (!fit) root.append(svg("circle", { class: "end", cx: sx(last.x), cy: sy(last.y), r: 4, style: "fill:" + color }));
      const ly = sy(last.y) + 4;
      if (series.length <= 4 && !fit && !taken.some((t) => Math.abs(t - ly) < 13)) {
        taken.push(ly);
        const label = svg("text", { class: "endlabel", x: sx(last.x) + 8, y: ly });
        label.textContent = series.length > 1 ? s.name.split(" = ").pop() : fmt(last.y, yUnit);
        root.append(label);
      }
    });
    if (fit) {
      const inRange = fit.x.map((x, i) => [x, fit.y[i]]).filter(([x]) => x >= xRange[0] && x <= xRange[1]);
      root.append(svg("path", { class: "fitline", d: inRange.map(([x, y], i) => (i ? "L" : "M") + sx(x).toFixed(1) + "," + sy(y).toFixed(1)).join("") }));
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
      const bx = box.getBoundingClientRect();
      const left = e.clientX - bx.left + 14;
      tip.style.left = Math.min(left, bx.width - tip.offsetWidth - 8) + "px";
      tip.style.top = (e.clientY - bx.top - 10) + "px";
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
        if (series.length) grid.append(lineChart({ title: sc.key, series, xKey: xa.key, xUnit: xa.unit, yUnit: sc.unit, fit: fitFor(data.analysis, sc.key) }));
      }
    }
    for (const tr of data.traces) {
      const points = tr.x.map((x, i) => ({ x, y: tr.y[i] })).filter((p) => typeof p.x === "number" && typeof p.y === "number");
      if (!points.length) continue;   // a point the instrument could not measure (NaN) is left out, not drawn at 0
      grid.append(lineChart({ title: tr.key + (data.rows.length > 1 ? " (latest)" : ""), xKey: tr.x_unit === "Hz" ? "frequency" : "x",
        xUnit: tr.x_unit, yUnit: tr.unit, fit: fitFor(data.analysis, tr.key, true),
        series: [{ name: tr.key, points }] }));
    }
    return grid;
  }

  const KIND_LABEL = { peak: "peak", dip: "dip", linear: "linear", relaxation: "relaxation", flat: "no signal", steady: "steady",
    monotonic: "monotonic", structured: "structure", value: "value", none: "too few points" };

  function analysisBlock(a) {
    const box = h("div", { class: "analysis" });
    const ok = !a.issues.length;
    box.append(h("div", { class: "assess" + (ok ? "" : " warn") },
      h("span", { class: "assess-icon" }, icon(ok ? "check" : "warn")),
      h("div", {}, h("p", { class: "assess-text" }, a.assessment), h("p", { class: "assess-next" }, h("b", {}, "Next: "), a.next))));
    const rows = [];
    for (const s of a.series.slice(0, 6)) {
      if (!s.results.length) continue;
      const fitText = s.fit ? s.fit.model + " fit, R² " + Number(s.fit.r2).toFixed(3) : "";
      const what = s.fit && s.fit.model === s.kind ? fitText : (KIND_LABEL[s.kind] || s.kind) + (fitText ? " (" + fitText + ")" : "");
      const title = s.y + (s.group ? " · " + s.group : "") + " — " + what;
      rows.push(h("tr", { class: "res-head" }, h("th", { colspan: "2" }, title)));
      for (const r of s.results) rows.push(h("tr", {}, h("td", {}, r.name), h("td", {}, r.value)));
    }
    if (a.issues.length > 1) box.append(h("ul", { class: "issues" }, ...a.issues.slice(1, 6).map((i) => h("li", {}, i))));
    if (rows.length) {
      const wrap = h("div", { class: "results", hidden: rows.length > 14 }, h("table", {}, h("tbody", {}, ...rows)));
      const label = () => (wrap.hidden ? "Show key results" : "Hide key results");
      const more = h("button", { class: "link soft", type: "button" }, label());
      more.addEventListener("click", () => { wrap.hidden = !wrap.hidden; more.textContent = label(); });
      box.append(wrap, more);
    }
    return box;
  }

  // ------------------------------------------------------------------ runs pane (beside the chat, like a terminal pane)
  // One tab per run. A running run is polled from /api/live (so runs started by any agent show up);
  // a finished run shows its assessment, key results, plots with fits, table and plan.
  const RUNS = { open: false, tabs: new Map(), active: null, polling: false, cache: new Map(), dismissed: new Set() };
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

  function runTab(runId, plan) {
    let tab = RUNS.tabs.get(runId);
    if (tab) return tab;
    const state = h("span", { class: "state" });
    const close = h("button", { class: "run-tab-x", type: "button", "aria-label": "Close this tab" }, icon("x"));
    const btn = h("div", { class: "run-tab", role: "tab", tabindex: "0", title: runId, "aria-selected": "false" },
      state, h("span", { class: "run-tab-label" }, plan || runId), close);
    const body = h("div", { class: "run-view", role: "tabpanel", hidden: true });
    tab = { runId, plan: plan || runId, status: null, btn, state, body, rows: [], axes: [], measures: [], trace: null,
            done: 0, total: 0, eta: null, final: null, view: null, timer: 0 };
    btn.addEventListener("click", (e) => { if (!close.contains(e.target)) selectRun(runId); });
    btn.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectRun(runId); } });
    close.addEventListener("click", () => closeRunTab(runId));
    RUNS.tabs.set(runId, tab);
    $("run-tabs").append(btn);
    $("run-body").append(body);
    if (RUNS.tabs.size > 8) {  // keep the strip short: drop the oldest finished tab
      const old = [...RUNS.tabs.values()].find((t) => t.status !== "running" && t !== tab);
      if (old) closeRunTab(old.runId);
    }
    return tab;
  }
  function setTabStatus(tab, status) {
    tab.status = status;
    setState(tab.state, status);
    $("runs-dot").hidden = ![...RUNS.tabs.values()].some((t) => t.status === "running");
  }
  function selectRun(runId) {
    const tab = RUNS.tabs.get(runId);
    if (!tab) return;
    hideQueue();
    RUNS.active = runId;
    for (const t of RUNS.tabs.values()) {
      const on = t === tab;
      t.btn.classList.toggle("on", on);
      t.btn.setAttribute("aria-selected", String(on));
      t.body.hidden = !on;
    }
    $("run-body").querySelector(".pane-empty").hidden = true;
    tab.btn.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (tab.status !== "running" && tab.status !== "finishing" && !tab.final) loadFinal(tab);
    else renderRunView(tab);
  }
  function closeRunTab(runId) {
    const tab = RUNS.tabs.get(runId);
    if (!tab) return;
    clearTimeout(tab.timer);
    RUNS.dismissed.add(runId);  // closing a running run's tab must not bring it back
    tab.btn.remove(); tab.body.remove();
    RUNS.tabs.delete(runId);
    if (RUNS.active === runId) {
      RUNS.active = null;
      const next = [...RUNS.tabs.keys()].pop();
      if (next) selectRun(next); else if (!QUEUE.open) $("run-body").querySelector(".pane-empty").hidden = false;
    }
    $("runs-dot").hidden = ![...RUNS.tabs.values()].some((t) => t.status === "running");
  }
  function toggleRuns(open) {
    RUNS.open = open === undefined ? !RUNS.open : open;
    $("runpane").hidden = !RUNS.open;
    $("toggle-runs").setAttribute("aria-pressed", String(RUNS.open));
    $("toggle-runs").classList.toggle("on", RUNS.open);
    fitLayout();
  }
  function openRun(runId, plan) {
    runTab(runId, plan);
    selectRun(runId);
    toggleRuns(true);
  }

  // a run started (seen in the chat stream or in the lab state): give it a tab, show it, follow it
  function noticeRun(runId, plan) {
    if (RUNS.tabs.has(runId) || RUNS.dismissed.has(runId)) return;
    const tab = runTab(runId, plan);
    setTabStatus(tab, "running");
    openRun(runId, plan);
    pollLive();
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
        if (v.status !== "running") { setTabStatus(tab, "finishing"); await loadFinal(tab); continue; }
        renderRunView(tab);
        await sleep(500);
      }
    } finally { RUNS.polling = false; }
  }
  async function loadFinal(tab) {
    let data;
    try { data = await getRun(tab.runId); } catch (e) { setTabStatus(tab, "missing"); renderRunView(tab); return; }
    tab.final = data;
    tab.plan = data.plan || tab.plan;
    setTabStatus(tab, (data.meta && data.meta.status) || "unfinished");
    renderRunView(tab);
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
    v.bar = h("i");
    v.barBox = h("div", { class: "bar" }, v.bar);
    v.analysis = h("div", { class: "rv-analysis" });
    v.charts = h("div", { class: "rv-charts" });
    v.table = h("div", { class: "table-wrap", hidden: true });
    v.tableBtn = h("button", { class: "link soft", type: "button" }, "Show table");
    v.tableBtn.addEventListener("click", () => {
      v.table.hidden = !v.table.hidden;
      if (!v.table.hidden) v.table.replaceChildren(runTable(runViewData(tab)));
      v.tableBtn.textContent = v.table.hidden ? "Show table" : "Hide table";
    });
    const requeue = h("button", { class: "btn small ghost", type: "button" }, "Add to the queue");
    requeue.addEventListener("click", async () => {
      if (await queueOp({ op: "add", text: (tab.final && tab.final.plan_text) || "" })) showQueue();
    });
    v.plan = h("details", { class: "rv-plan", hidden: true }, h("summary", {}, "Plan"), h("pre", {}), requeue);
    v.files = h("div", { class: "report-files rv-files", hidden: true });
    v.foot = h("div", { class: "plots-foot" }, h("span", {}, tab.runId), v.tableBtn);
    tab.body.replaceChildren(
      h("div", { class: "rv-head" }, v.state, h("code", { class: "rv-plan-name" }, tab.plan), h("div", { class: "spacer" }), v.stop),
      h("div", { class: "rv-progress" }, v.count), v.barBox, v.analysis, v.files, v.charts, v.foot, v.table, v.plan);
    tab.view = v;
  }
  function runViewData(tab) {
    if (tab.final) return tab.final;
    return { axes: tab.axes.map((a) => ({ key: a.key, unit: a.unit })), scalars: tab.measures.filter((m) => m.kind === "scalar"),
             rows: tab.rows, traces: tab.trace ? [tab.trace] : [], analysis: null };
  }
  function renderRunView(tab) {
    if (!tab.view) buildRunView(tab);
    const v = tab.view;
    setState(v.state, tab.status);
    const running = tab.status === "running";
    v.stop.hidden = !running;
    const m = tab.final ? tab.final.meta || {} : {};
    const total = tab.final ? m.planned_points || tab.final.rows.length : tab.total;
    const done = tab.final ? (m.points != null ? m.points : tab.final.rows.length) : tab.done;
    v.bar.style.width = (total ? Math.round(100 * done / total) : 0) + "%";
    v.barBox.classList.toggle("done", !running);
    v.barBox.classList.toggle("warn", !!tab.final && m.status !== "done");
    if (running) {
      const eta = tab.etaRaw == null ? null : Math.max(0, tab.etaRaw - (Date.now() - tab.etaAt) / 1000);
      v.count.textContent = tab.phase === "setup" ? "Setting up" + etaText(eta)
        : tab.phase === "after" ? "All " + done + " points measured · setting the final values" + etaText(eta)
        : tab.phase === "stopping" ? "Stopping · sending the drives it touched back to safe"
        : done + " / " + total + " points" + etaText(eta);
    }
    else if (!tab.final) v.count.textContent = tab.status === "missing" ? "This run's data could not be read." : "Finished · analysing…";
    else if (m.status === "done") v.count.textContent = "Finished · " + done + " points" + (m.duration_s != null ? " in " + durText(m.duration_s) : "");
    else if (m.status === "aborted") v.count.textContent = "Stopped after " + done + " of " + total + " points" + (m.reason ? ": " + m.reason : "");
    else if (m.status === "interrupted") v.count.textContent = "Cut short after " + done + (m.planned_points ? " of " + m.planned_points : "") +
      " points: Kelvin stopped while it measured (a crash or a power cut). Every point up to then is kept.";
    else v.count.textContent = "Not finished (" + done + " points recorded)";
    if (tab.final && !v.finalDrawn) {  // a finished run is drawn once
      v.finalDrawn = true;
      clearTimeout(tab.timer); tab.timer = 0;
      v.analysis.replaceChildren(...(tab.final.analysis ? [analysisBlock(tab.final.analysis)] : []));
      v.charts.replaceChildren(runCharts(tab.final));
      v.plan.hidden = !tab.final.plan_text;
      v.plan.querySelector("pre").textContent = tab.final.plan_text || "";
      runFiles(tab, v.files);
      if (!v.table.hidden) v.table.replaceChildren(runTable(tab.final));
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

  async function showRunsMenu() {
    const menu = $("runs-menu");
    if (!menu.hidden) { menu.hidden = true; $("runs-history").setAttribute("aria-expanded", "false"); return; }
    menu.replaceChildren(h("div", { class: "pane-menu-note" }, "Loading…"));
    menu.hidden = false;
    $("runs-history").setAttribute("aria-expanded", "true");
    let runs = [];
    try { runs = (await api("/api/runs")).runs; } catch (e) { /* keep the note */ }
    menu.replaceChildren(...(runs.length ? runs.map((r) => {
      const st = h("span", { class: "state" });
      setState(st, r.status);
      const when = r.run_id.slice(9, 11) + ":" + r.run_id.slice(11, 13) + " · " + r.run_id.slice(4, 6) + "/" + r.run_id.slice(6, 8);
      const pts = r.points != null ? r.points + (r.planned_points && r.planned_points !== r.points ? "/" + r.planned_points : "") + " pts · " : "";
      const b = h("button", { class: "pane-menu-item", type: "button", role: "menuitem" }, st,
        h("span", { class: "pm-plan" }, r.plan), h("span", { class: "pm-meta" }, pts + when));
      b.addEventListener("click", () => {
        menu.hidden = true;
        $("runs-history").setAttribute("aria-expanded", "false");
        if (r.status === "running") noticeRun(r.run_id, r.plan); else openRun(r.run_id, r.plan);
      });
      return b;
    }) : [h("div", { class: "pane-menu-note" }, "No runs yet.")]));
  }

  // The conversation keeps at least CHAT_MIN pixels. When the side panels need the room, the sidebar steps aside (the
  // menu button brings it back over the page), and if that is not enough the lab panel floats over the runs pane.
  // Below 1100 px both panels float anyway (app.css), below 820 px the sidebar too.
  const CHAT_MIN = 520, SIDEBAR_W = 264, LAB_W = 380, RUNS_MIN = 340;
  function fitLayout() {
    const app = $("app"), w = window.innerWidth;
    const floating = w <= 1100;
    const runs = RUNS.open && !floating ? RUNS_MIN : 0;
    const lab = S.labOpen && !floating ? LAB_W : 0;
    const compact = w > 820 && SIDEBAR_W + CHAT_MIN + runs + lab > w;
    app.classList.toggle("compact", compact);
    app.classList.toggle("float-lab", lab > 0 && (compact ? 0 : SIDEBAR_W) + CHAT_MIN + runs + LAB_W > w);
    if (!compact && w > 820) app.classList.remove("side-open");
    if (RUNS.open) setPaneWidth($("runpane").getBoundingClientRect().width);
    syncScrim();
  }
  function setPaneWidth(w) {
    const app = $("app"), vw = window.innerWidth;
    const sidebar = vw <= 820 || app.classList.contains("compact") ? 0 : SIDEBAR_W;
    const lab = S.labOpen && vw > 1100 && !app.classList.contains("float-lab") ? LAB_W : 0;
    const max = Math.max(RUNS_MIN, vw - sidebar - lab - CHAT_MIN);
    const width = Math.round(Math.min(max, Math.max(RUNS_MIN, w || 0)));
    $("runpane").style.width = width + "px";
    return width;
  }
  function initRunsPane() {
    const saved = Number(store("lh.runsWidth"));
    setPaneWidth(saved > 0 ? saved : Math.min(720, Math.max(420, window.innerWidth * 0.42)));
    const handle = $("runpane-resize");
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const right = $("runpane").getBoundingClientRect().right;
      handle.setPointerCapture(e.pointerId);
      document.body.classList.add("resizing");
      const move = (ev) => setPaneWidth(right - ev.clientX);
      const up = () => {
        handle.removeEventListener("pointermove", move);
        document.body.classList.remove("resizing");
        store("lh.runsWidth", String(parseInt($("runpane").style.width, 10)));
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up, { once: true });
    });
    handle.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const w = $("runpane").getBoundingClientRect().width;
      store("lh.runsWidth", String(setPaneWidth(w + (e.key === "ArrowLeft" ? 32 : -32))));
    });
    $("toggle-runs").addEventListener("click", () => toggleRuns());
    $("close-runs").addEventListener("click", () => toggleRuns(false));
    $("runs-history").addEventListener("click", showRunsMenu);
    document.addEventListener("click", (e) => {
      if (!$("runs-menu").hidden && !e.target.closest(".pane-menu-wrap")) {
        $("runs-menu").hidden = true;
        $("runs-history").setAttribute("aria-expanded", "false");
      }
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("runs-menu").hidden = true; });
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
      if (RUNS.tabs.has(card.runId)) { selectRun(card.runId); toggleRuns(true); } else openRun(card.runId, card.plan);
    });
    card.progress = (ev) => {
      card.runId = ev.run_id; card.plan = ev.plan;
      title.replaceChildren("Measuring ", h("code", {}, ev.plan));
      bar.style.width = (ev.total ? Math.round(100 * ev.done / ev.total) : 0) + "%";
      count.textContent = ev.done + " / " + ev.total;  // the time left is in the runs pane
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

  // the files Kelvin keeps in a run's folder: its report, slides and data, opened on this computer
  const RUN_FILE_LABEL = { "report.pdf": "Report (PDF)", "report.pptx": "Slides", "run.h5": "Data (HDF5)" };
  function runFiles(tab, box) {
    const files = (tab.final && tab.final.files) || [];
    const base = "/api/runs/" + encodeURIComponent(tab.runId);
    const openIt = async (path, what) => {
      try { await api(path, { body: {} }); toast("Opening " + what + "…"); } catch (e) { toast(e.message); }
    };
    const buttons = files.map((f) => {
      const b = h("button", { class: f.name === "report.pdf" ? "btn small" : "btn small ghost", type: "button",
        title: f.name }, RUN_FILE_LABEL[f.name] || f.name);
      b.addEventListener("click", () => openIt(base + "/files/" + encodeURIComponent(f.name) + "/open", f.name));
      return b;
    });
    const folder = h("button", { class: "link soft", type: "button" }, "Open folder");
    folder.addEventListener("click", () => openIt(base + "/open", "the run's folder"));
    box.replaceChildren(...buttons, folder, runDownloads(tab.runId));
    box.hidden = false;
  }

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
    const choices = [["csv", "CSV"], ["xlsx", "Excel"], ["npz", "NumPy"]].map(([fmt, label]) => {
      const b = h("button", { class: "btn small ghost", type: "button", title: "Download the data as " + label },
        label);
      b.addEventListener("click", () => get(fmt, label + " file"));
      return b;
    });
    const eln = h("button", { class: "btn small ghost", type: "button", title: "The run's record as Markdown with its plots, for a lab notebook (ELN)" }, "Lab notebook");
    eln.addEventListener("click", () => fetchDownload("/api/runs/" + encodeURIComponent(runId) + "/eln", runId + ".zip", "lab notebook record"));
    return h("span", { class: "run-downloads" }, h("span", { class: "hint" }, "Download data:"), ...choices, eln);
  }
  // a file Kelvin's code writes, downloaded with the access key (its name from its Content-Disposition)
  async function fetchDownload(url, fallback, what) {
    try {
      const res = await fetch(url, { headers: { "X-Kelvin-Token": token } });
      if (!res.ok) throw new Error(((await res.json().catch(() => ({}))).error) || "HTTP " + res.status);
      const named = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
      const href = URL.createObjectURL(await res.blob());
      const a = h("a", { href, download: named ? named[1] : fallback });
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
    const active = RUNS.active && RUNS.tabs.get(RUNS.active);
    if (active && active.status !== "running") opts.push(["run", "This run: " + (active.plan || RUNS.active)]);
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
      const b = h("button", { class: "btn small ghost", type: "button", title: name }, label);
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
      "No reports yet. They appear after the first scan: mount a device in the Lab panel, then measure.")]));
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
    sel.replaceChildren(new Option(groups.length ? "Nothing mounted" : "No devices yet: add one with New device…", ""), ...groups);
    MOUNT.key = null;   // new options: choose the mounted one again
    showMounted(r.mounted);
  }
  function showMounted(m) {
    showProtection(m);
    const key = m ? JSON.stringify([m.project || null, m.device]) : "";
    $("mount-summary").textContent = m ? m.device_name + " · " + m.project_name : "nothing mounted";
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
    try { const r = await api("/api/mount", { body: device ? { project, device } : {} }); MOUNT.key = "x"; showMounted(r.mounted); }
    catch (e) { toast(e.message); }
  }
  async function newDevice() {
    const chat = S.chats.find((c) => c.id === S.chatId);
    const pid = (chat && chat.project) || "general";
    const where = pid === "general" ? "work outside projects" : projectName(pid);
    const name = prompt("Name of the new device (sample) in " + where + ", e.g. D3:");
    if (!name) return;
    const material = prompt("Its material (optional), e.g. graphene/hBN:") || "";
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
      + ". Everything that reaches the sample stays at safe until you unlock; Kelvin can still read and measure.";
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
    if (remove && !confirm("Remove " + m.device_name + "'s protection? Only the lab's limits will hold.")) return;
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
    const body = { scope: $("report-scope").value, chat_id: S.chatId, run: RUNS.active, last: 5,
      title: $("report-title").value.trim(), summary: $("report-summary").value.trim(), format: fmt };
    $("make-report").disabled = true;
    $("report-msg").textContent = "Writing…";
    try {
      const r = await api("/api/reports", { body });
      $("report-msg").textContent = r.runs + (r.runs === 1 ? " run" : " runs") + " · done";
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
      case "show_sequence": return v("Drawing", "Drew") + " the " + code(a.protocol || "") + " pulse sequence";
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
      case "remember": return v("Remembering", "Remembered") + (a.scope === "global" ? " for every chat" : " for this project") +
        (a.text ? ": " + esc(String(a.text)) : "");
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
  function makeStep(name, input) {
    const state = h("span", { class: "state" }, h("span", { class: "spinner" }));
    const summary = h("span", { class: "step-summary" });
    const chev = icon("chev"); chev.classList.add("chev");
    const label = h("span", { class: "step-label", html: stepLabel(name, input, false) });
    const head = h("button", { class: "step-head", type: "button" }, state, label, summary, chev);
    const inputText = name === "run_plan" || name === "simulate_plan" || name === "validate_plan"
      ? String((input && input.plan) || "") : JSON.stringify(input || {}, null, 1);
    const result = h("pre", {}, "…");
    const body = h("div", { class: "step-body", hidden: true }, h("div", { class: "k" }, "Input"), h("pre", {}, inputText), h("div", { class: "k" }, "Result"), result);
    const el = h("div", { class: "step" }, head, body);
    head.addEventListener("click", () => { body.hidden = !body.hidden; el.classList.toggle("open", !body.hidden); });
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
  // auto mode approved this without asking: show what ran and why, like an answered request
  function autoApproval(ev) {
    const text = h("span", {}, ev.title), list = h("ul", {}, ...ev.items.map(approvalItem));
    const title = h("div", { class: "approval-title" }, icon("check"), text);
    const el = h("div", { class: "approval done auto", role: "group", "aria-label": ev.title }, title, list,
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
    declines: (n) => n + " 秒后自动拒绝" } };
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
    put(shown);
    const words = cardWords() || {};
    let inEnglish = false;
    const flip = h("button", { class: "link soft card-lang", type: "button", title: "The card as Kelvin wrote it" }, words.english || "English");
    flip.addEventListener("click", (e) => {
      e.stopPropagation();
      inEnglish = !inEnglish;
      put(inEnglish ? english : shown);
      flip.textContent = inEnglish ? (words.shown || lang) : (words.english || "English");
    });
    flip.addEventListener("keydown", (e) => e.stopPropagation());
    parts.title.after(flip);
  }
  // a decided request shows one line; its title opens and closes the details
  function foldable(el, title) {
    title.setAttribute("role", "button");
    title.setAttribute("tabindex", "0");
    title.setAttribute("aria-expanded", "false");
    title.title = "Show the details";
    const flip = () => title.setAttribute("aria-expanded", String(el.classList.toggle("open")));
    title.addEventListener("click", flip);
    title.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
  }

  // where a request acts (runtime.where): a request on the simulator once looked just like one on the instruments
  const whereTag = (where) => where ? h("span", { class: "where " + (where === "SIMULATOR" ? "sim" : "real"),
    title: where === "SIMULATOR" ? "Only simulated instruments: nothing real moves" : "Real instruments" }, where) : null;
  function makeApproval(ev) {
    const verdict = h("span", { class: "verdict" });
    const count = h("span", { class: "muted small" });
    const yes = h("button", { class: "btn primary", type: "button" }, cardSay("Approve"));
    const no = h("button", { class: "btn", type: "button" }, cardSay("Decline"));
    const actions = h("div", { class: "approval-actions" }, yes, no, count);
    const text = h("span", {}, ev.title), where = whereTag(ev.where), list = h("ul", {}, ...ev.items.map(approvalItem));
    const title = h("div", { class: "approval-title" }, icon("hand"), text, where);
    const el = h("div", { class: "approval", role: "group", "aria-label": ev.title }, title, list, actions);
    localizeCard(ev, { title: text, list, where });
    let left = ev.seconds_left;
    const words = cardWords();
    const tick = () => { count.textContent = left > 0 ? (words ? words.declines(left) : "Declines itself in " + left + " s") : ""; left -= 1; };
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
        card.close("Already closed", false);
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
      this.who = h("span", {}, "Kelvin");
      this.el.append(h("div", { class: "who" }, mark(), this.who));
      this.text = null; this.buf = ""; this.raf = 0; this.thinking = null; this.steps = new Map();
      this.setModel(modelKey);
    }
    setModel(key) { if (key) this.who.textContent = "Kelvin · " + (S.labels[key] || key.split(":").pop()); }
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
    condensing(words) {   // a long answer is being shortened (brevity.py)
      this.endText();
      if (this.pending) return;
      this.pending = h("div", { class: "condense-note" }, h("span", { class: "spinner" }),
        h("span", {}, "Condensing a long answer (" + words + " words)…"));
      this.el.append(this.pending);
    }
    condensed(ev) {
      this.endText();
      if (this.pending) { this.pending.remove(); this.pending = null; }
      if (ev.text && this.last) this.shorten(this.last, ev.text, this.last.buf, ev.full_words);
      else if (ev.kept) this.keptWhole(ev.words, ev.kept);
    }
    shorten(last, short, full, fullWords) {
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
    toolEnd(id, content, isError, runId, images) {
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
      if (!runId) return;
      if (!step.card) { step.card = runCard(); (step.last || step.el).after(step.card.el); }
      step.card.finish(runId);
      const tab = RUNS.tabs.get(runId);
      if (tab && !tab.final) { setTabStatus(tab, "finishing"); loadFinal(tab); }
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
    error(text) { this.endText(); this.el.append(h("div", { class: "callout" }, text)); }
    finish() {
      this.endText(); this.endThinking();
      for (const step of this.steps.values()) if (!step.done && step.unknown) { step.unknown(); step.done = true; }
      if (!this.tools && this.el.querySelector(":scope > .md")) {   // Copy: the answer's text, as shown
        const btn = h("button", { class: "link soft", type: "button", title: "Copy this answer" }, icon("copy"), "Copy");
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
        : h("span", { class: "file-chip", title: a.name }, svgIcon("M7 3h7l5 5v13H7zM14 3v5h5"),
            a.name + (a.pages ? " · " + a.pages + " pages" : "")))) : null;
    const copy = h("button", { class: "link soft", type: "button", title: "Copy your message" }, icon("copy"), "Copy");
    copy.addEventListener("click", () => copyText(text, copy));
    return h("div", { class: "msg user" }, files, h("div", { class: "bubble" }, text), h("div", { class: "msg-tools user-tools" }, copy));
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
    const said = btn.lastChild;
    said.textContent = "Copied";
    setTimeout(() => { said.textContent = "Copy"; }, 1500);
  }
  function showEmpty(show) { $("empty").hidden = !show; thread.hidden = show; }

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
        const run = r.name === "run_plan" ? (String(r.content).match(/run_id='([^']+)'/) || [])[1] : null;
        reply.toolEnd(r.id, r.content, r.is_error, run, (r.images || []).map((i) => i.file));
      }
    }
  }
  function renderChat(chat) {
    thread.replaceChildren();
    let reply = null, recalled = null, skip = 0;
    const lastUser = chat.messages.map((m) => m.role).lastIndexOf("user");
    S.lastReply = null;
    chat.messages.forEach((m, at) => {
      if (skip > 0) { skip--; return; }
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
    offerReview(S.lastReply);
    offerRecheck(S.lastReply);
    showEmpty(!chat.messages.length);
    requestAnimationFrame(toBottom);
  }

  // ------------------------------------------------------------------ chats sidebar: projects, then unfiled chats
  const SIDE = { collapsed: new Set(), creating: false, moveAfterCreate: null, renaming: null, newProject: null, editor: null };
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

  function rowButton(name, label, onClick) {
    const b = h("button", { class: "row-btn", type: "button", "aria-label": label, title: label }, icon(name));
    b.addEventListener("click", (e) => { e.stopPropagation(); onClick(e); });
    return b;
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
      role: "button", tabindex: "0", draggable: "true" },
      mark ? h("span", { class: "chat-dot", title: mark[1], "aria-label": mark[1] }) : null,
      h("span", { class: "t" }, c.title || "New chat"), more);
    item.addEventListener("click", () => openChat(c.id));
    item.addEventListener("keydown", (e) => { if (e.key === "Enter") openChat(c.id); });
    item.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/x-kelvin-chat", c.id);
      e.dataTransfer.effectAllowed = "move";
      item.classList.add("dragging");
      $("chat-list").classList.add("dragging-chat");
    });
    item.addEventListener("dragend", () => { item.classList.remove("dragging"); $("chat-list").classList.remove("dragging-chat"); });
    return item;
  }
  function projectGroup(p, chats) {
    const holdsActive = currentProject() === p.id;
    const open = !SIDE.collapsed.has(p.id) || holdsActive;
    let head;
    if (SIDE.renaming === p.id) {
      head = nameInput("project:" + p.id, p.name, "Project name", (name) => renameProject(p, name), () => { SIDE.renaming = null; renderChats(); });
    } else {
      head = h("div", { class: "proj-row" + (holdsActive && !S.chatId ? " active" : ""), role: "button", tabindex: "0", "aria-expanded": String(open), title: p.name },
        h("span", { class: "chev-s" + (open ? " open" : "") }, icon("chev")), icon("folder"),
        h("span", { class: "t" }, p.name), h("span", { class: "count" }, chats.length ? String(chats.length) : ""),
        rowButton("plus", "New chat in " + p.name, () => newChat(p.id, true)),
        rowButton("dots", "Options for " + p.name, (e) => projectMenu(e.currentTarget, p, chats)));
      const toggle = () => {
        if (SIDE.collapsed.has(p.id) || !open) SIDE.collapsed.delete(p.id); else SIDE.collapsed.add(p.id);
        store("lh.collapsed", JSON.stringify([...SIDE.collapsed]));
        renderChats();
      };
      head.addEventListener("click", toggle);
      head.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      dropTarget(head, p.id);
    }
    const group = h("div", { class: "proj" }, head);
    if (open) {
      group.append(h("div", { class: "proj-chats" }, ...(chats.length ? chats.map(chatItem)
        : [h("div", { class: "side-empty" }, "No conversations yet")])));
    }
    return group;
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
    $("chat-list").replaceChildren(...kids);
    if (ed && ed.el.isConnected && (ed.fresh || typing)) {
      ed.input.focus();
      if (ed.fresh) ed.input.select();
      ed.fresh = false;
    }
  }

  // a small menu under a button: [{label, icon, onClick, danger, checked} | {heading} | {sep: true}]
  let openMenu = null;
  function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
  function popMenu(anchor, items) {
    closeMenu();
    const menu = h("div", { class: "menu", role: "menu" });
    for (const it of items) {
      if (it.sep) { menu.append(h("div", { class: "menu-sep", role: "separator" })); continue; }
      if (it.heading) { menu.append(h("div", { class: "menu-head" }, it.heading)); continue; }
      const b = h("button", { class: "menu-item" + (it.danger ? " danger" : ""), type: "button", role: "menuitem" },
        it.icon ? icon(it.icon) : h("span", { class: "menu-gap" }), h("span", { class: "menu-label" }, it.label),
        it.checked ? icon("check") : null);
      b.addEventListener("click", (e) => { e.stopPropagation(); closeMenu(); it.onClick(); });
      menu.append(b);
    }
    menu.addEventListener("keydown", (e) => {
      const buttons = [...menu.querySelectorAll(".menu-item")];
      const i = buttons.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); buttons[(i + 1) % buttons.length].focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); buttons[(i - 1 + buttons.length) % buttons.length].focus(); }
      if (e.key === "Escape") { e.preventDefault(); closeMenu(); anchor.focus(); }
    });
    document.body.append(menu);
    const r = anchor.getBoundingClientRect();
    const below = r.bottom + 4, above = r.top - menu.offsetHeight - 4;   // above when it doesn't fit below (the composer)
    const top = below + menu.offsetHeight + 8 <= window.innerHeight ? below
      : above >= 8 ? above : window.innerHeight - menu.offsetHeight - 8;
    menu.style.top = Math.max(8, top) + "px";
    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + "px";
    openMenu = menu;
    const first = menu.querySelector(".menu-item");
    if (first) first.focus();
  }
  document.addEventListener("click", (e) => { if (openMenu && !openMenu.contains(e.target)) closeMenu(); });
  window.addEventListener("resize", closeMenu);

  function chatMenu(anchor, c) {
    const items = [{ label: "Rename", icon: "pencil", onClick: () => { SIDE.renaming = c.id; renderChats(); } }];
    items.push({ heading: "Move to project" });
    for (const p of S.projects) {
      items.push({ label: p.name, icon: "folder", checked: c.project === p.id, onClick: () => { if (c.project !== p.id) moveChat(c.id, p.id); } });
    }
    items.push({ label: "New project…", icon: "plus", onClick: () => startNewProject(c.id) });
    if (c.project) items.push({ label: "Remove from project", icon: "x", onClick: () => moveChat(c.id, null) });
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
        const text = prompt("Describe " + d.name + ": geometry, contacts, fabrication.", d.description || "");
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
        "No notes yet. When a conversation settles something worth keeping (a decision, a procedure, an instrument quirk, a result), Kelvin saves it here."));
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
        "No papers yet. Ask Kelvin to look up the literature, then to keep the papers that matter."));
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
    for (const g of S.groups || []) if (g.provider !== "demo" && !ids.includes(g.provider)) ids.push(g.provider);
    for (const p of chosen || []) if (!ids.includes(p)) ids.push(p);
    return ids.map((id) => [id, id === "local" ? "Local models" : providerName(id)]);
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
  const modelAllowed = (key, rules) => !rules || !rules.providers || key.split(":")[0] === "demo" || rules.providers.includes(key.split(":")[0]);
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
    const text = rulesText(rules);
    $("rules-chip").hidden = !text;
    $("rules-chip").title = text ? "This chat's rules: " + text : "";
    $("rules-chip-text").textContent = text ? "Confidential" : "";
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
    const kept = n ? " Its " + (n === 1 ? "conversation stays" : n + " conversations stay") + ", under Conversations." : "";
    if (!confirm("Delete the project “" + p.name + "”?" + kept)) return;
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
    if (!confirm("Delete “" + (c.title || "New chat") + "”? The lab's event log and run data are kept.")) return;
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
  }
  // an incognito chat is forgotten as soon as you leave it
  function leaveIncognito() {
    if (S.incognito && S.chatId) api("/api/chats/" + S.chatId, { method: "DELETE" }).catch(() => {});
    if (S.incognito) { S.incognito = false; applyIncognito(); }
  }
  const askHint = () => S.incognito ? "Ask anything; nothing here is remembered" : "Ask Kelvin…";
  function applyIncognito() {
    document.body.classList.toggle("incognito", S.incognito);
    const b = $("toggle-incognito");
    b.setAttribute("aria-pressed", String(S.incognito));
    b.classList.toggle("on", S.incognito);
    // always there: from a saved chat it starts a new incognito one; in incognito it leaves (and forgets) it
    b.title = S.incognito ? "Leave incognito: this chat is forgotten and a normal chat starts"
      : S.chatId ? "New incognito chat: no memory is used or kept, and the chat is not saved"
        : "Make this chat incognito: no memory is used or kept, and the chat is not saved";
    $("greeting").textContent = S.incognito ? "Incognito chat" : "What are we measuring today?";
    $("empty-sub").textContent = S.incognito
      ? "Kelvin uses no memory here, keeps nothing from this chat, and forgets it when you leave. Instrument commands and runs are still logged."
      : S.labSub || "";
    $("input").placeholder = askHint();
  }
  function toggleIncognito() {
    if (S.incognito) { newChat(); return; }   // leaving: forget it and start a normal chat
    if (S.chatId) newChat();
    S.incognito = true;
    SIDE.newProject = null;
    applyIncognito();
    setTitle("", null);
    renderChats();
    $("input").focus();
  }

  async function openChat(id) {   // another chat may go on answering meanwhile: its answer waits for you there
    let chat;
    try { chat = await api("/api/chats/" + id); } catch (e) { toast(e.message); return; }
    leaveIncognito();
    S.chatId = id;
    S.unread.delete(id);
    SIDE.newProject = null;
    S.chatRules = chat.rules || null;
    showChat(chat);
    setTitle(chat.title, chat.project);
    showFolder(chat.folder, chat.own_folder);
    if (chat.model && S.labels[chat.model]) $("model").value = chat.model;
    applyRules();
    renderChats();
    refreshComposer();
    closeSidebar();
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
  function newChat(projectId, ask) {   // ask: the person started it, so the folder picker opens (in the app)
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
    $("input").focus();
  }

  // ------------------------------------------------------------------ the chat's folder: where its runs go
  const FOLDER = { path: "", own: false };
  function folderName(path) { const parts = path.split(/[\\/]+/).filter(Boolean); return parts[parts.length - 1] || path; }
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
  async function pickFolder(start, question) {   // the computer's own folder picker in the Kelvin app; in a browser, the path typed
    if (hasPicker()) {
      try { return (await window.pywebview.api.pick_folder(start || "")) || ""; } catch (e) { return ""; }
    }
    const typed = window.prompt(question || "The folder's full path, where this chat saves its runs:", start || "");
    return typed ? typed.trim() : "";
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
    const path = await pickFolder(FOLDER.path);
    if (!path) return;
    try { await useFolder(path); toast("This chat saves its runs in " + FOLDER.path); } catch (e) { toast(e.message); }
  }
  async function askFolder() {   // a chat the person starts: they say where its runs go (Settings can turn it off)
    await loadNewFolder();
    if (!hasPicker() || (S.settings && S.settings.ask_folder === false)) return;
    const path = await pickFolder(FOLDER.path);
    if (path && !S.chatId) { try { await useFolder(path); } catch (e) { toast(e.message); } }
  }
  async function folderMenu() {   // like a code editor's folder menu: the whole path, recent folders, another one
    let recent = [];
    try { recent = (await api("/api/folder" + (SIDE.newProject ? "?project=" + encodeURIComponent(SIDE.newProject) : ""))).recent || []; } catch (e) { /* the rest still works */ }
    const same = (a, b) => a.toLowerCase() === b.toLowerCase();
    const items = [{ heading: FOLDER.path }];
    if (recent.length) {
      items.push({ heading: "Recent folders" });
      for (const p of recent) {
        const twin = recent.some((q) => q !== p && same(folderName(q), folderName(p)));   // two "data" folders: say which
        items.push({ label: twin ? p : folderName(p), icon: "folder", checked: FOLDER.own && same(p, FOLDER.path),
          onClick: () => { if (!(FOLDER.own && same(p, FOLDER.path))) useFolder(p).catch((e) => toast(e.message)); } });
      }
      items.push({ sep: true });
    }
    items.push({ label: "Choose folder…", icon: "folder-open", onClick: changeFolder },
      { label: "Open folder", icon: "doc", onClick: () => api("/api/folder", { body: { open: true, chat: S.chatId, project: SIDE.newProject } }).catch((e) => toast(e.message)) });
    if (FOLDER.own) items.push({ label: "Use the data folder", icon: "x", onClick: () => useFolder(null).catch((e) => toast(e.message)) });
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
    send.disabled = !S.streaming && ((!typed && !ATT.items.length) || !$("model").value);
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

  // ------------------------------------------------------------------ attached pictures and PDFs
  const ATT = { items: [] };
  function addFiles(list) {
    for (const f of list) {
      if (!/^image\//.test(f.type) && f.type !== "application/pdf") { toast((f.name || "That file") + ": Kelvin takes pictures and PDFs."); continue; }
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
    if (file.type === "application/pdf" || file.type === "image/gif") return file;
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
  async function sendMessage(text) {
    text = text.trim();
    if (TYPED_STOP.test(text)) return hardStop();
    if (S.streaming) return steerMessage(text);   // attachments wait for the next message
    if (!text && ATT.items.length) text = ATT.items.length > 1 ? "Look at the attached files." : "Look at the attached file.";
    if (!text) return;
    const model = $("model").value;
    if (!model) { openSettings(); return; }
    const extra = S.compare.filter((k) => k !== model);
    if (extra.length) return sendCompare(text, [model, ...extra]);
    if (!S.chatId) {
      const rules = currentRules();
      try {
        const made = await api("/api/chats", { body: { model, project: SIDE.newProject, incognito: S.incognito } });
        S.chatId = made.id;
        showFolder(made.folder, made.own_folder);
      } catch (e) { toast(e.message); return; }
      S.chatRules = rules;
      applyIncognito();
    }
    const attachments = await takeAttachments();
    if (attachments === null) return;
    $("input").value = ""; autosize();
    $("input").placeholder = askHint();
    showEmpty(false);
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
      if (S.chatId === chatId) offerReview(live.reply);
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
        const el = h("div", { class: "lane" }, reply.el, h("div", { class: "lane-foot" }, pick));
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
      if (last && last.compare) { S.compare = last.compare.models.filter((k) => k !== r.model).slice(0, 2); compareLabel(); }
      toast("Continuing with " + (S.labels[r.model] || r.model) + ".");
    } catch (e) { toast(e.message); }
  }
  async function sendCompare(text, models) {
    if (!S.chatId) {
      const rules = currentRules();
      try {
        const made = await api("/api/chats", { body: { model: models[0], project: SIDE.newProject, incognito: S.incognito } });
        S.chatId = made.id;
        showFolder(made.folder, made.own_folder);
      } catch (e) { toast(e.message); return; }
      S.chatRules = rules;
      applyIncognito();
    }
    const attachments = await takeAttachments();
    if (attachments === null) return;
    $("input").value = ""; autosize();
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
      if (S.chatId === chatId) offerReview(S.lastReply);
      finishLive(chatId, live);   // models being compared read nothing meanwhile: what you added is the next message
    }
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
      h("div", { class: "pane-menu-note" }, "While comparing, models can read the lab but not change it. Pick one answer to continue with. ", off));
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
      try { const b = await api("/api/bootstrap"); applyModels(b.models.groups, b.default_model, b.auto_model); } catch (e) { /* the list updates on reload */ }
      if (st.plan && !st.welcomed) { $("chatgpt-dialog").showModal(); api("/api/chatgpt/welcomed", { body: {} }).catch(() => {}); }
    }, 2000);
  }
  async function signOutChatGPT() {
    if (!confirm("Sign out of ChatGPT in Kelvin? ChatGPT models will no longer be available here.")) return;
    try {
      const st = await api("/api/chatgpt/signout", { body: {} });
      showChatGPT(st);
      toast(st.revoked ? "Signed out of ChatGPT." : "Signed out here; OpenAI did not confirm the sign-out, so you can also disconnect Kelvin in ChatGPT's settings.");
      const b = await api("/api/bootstrap");
      applyModels(b.models.groups, b.default_model, b.auto_model);
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
    if (!confirm("Disconnect Kelvin from the hub? It stops sharing. What it shared stays on the hub until you delete it there.")) return;
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
      return h("li", { class: "memory-note" }, h("span", { class: "memory-kind" }, "service"),
        h("div", { class: "memory-body" }, h("span", {}, s.name), out),
        h("span", { class: "card-actions" }, test),
        rowButton("trash", "Remove " + s.name, async () => {
          if (!confirm("Remove " + s.name + " and its key from Kelvin?")) return;
          try { const r = await api("/api/services/" + s.id, { method: "DELETE" }); renderServices(r.services); refreshModels(r); }
          catch (e) { $("svc-hint").textContent = e.message; }
        }));
    }));
  }
  function refreshModels(r) {   // r: the models, with the default (Settings' choice, else Claude Sonnet)
    applyModels(r.models.groups, r.default_model, r.auto_model);
    fillDefaultModel();
    fillModels($("reviewer"), S.groups, (S.settings && S.settings.reviewer) || "", true);
    $("reviewer").options[0].textContent = "Off";
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
      $("svc-hint").textContent = group && group.error ? "Added, but " + group.error
        : "Added " + added.name + (group ? " with " + group.models.length + " models." : ".");
    } catch (e) { $("svc-hint").textContent = e.message; }
  }

  // ------------------------------------------------------------------ phones
  const PHONE = { view: null, pairTimer: 0 };
  async function loadPhone() {
    try { renderPhone(await api("/api/phone")); }
    catch (e) { $("phone-status").textContent = e.message; $("pair-start").disabled = true; }
  }
  function renderPhone(v) {
    PHONE.view = v;
    $("phone-on").checked = v.on;
    const where = v.addresses.map((a) => a.url + (a.kind === "tailscale" ? " (Tailscale)" : a.kind === "network" ? " (this network)" : ""));
    $("phone-status").textContent = v.error ? v.error
      : v.listening ? "Phones can reach Kelvin at " + (where.join(", ") || "this computer's address, port " + v.port) + "."
      : v.on ? "Starting…" : "Off: only this computer can use Kelvin.";
    $("pair-start").disabled = !v.listening;
    $("firewall-row").hidden = !v.firewall;
    $("firewall-status").textContent = v.firewall === "allowed" ? "Windows Firewall lets phones reach Kelvin."
      : "Windows Firewall may stop phones from reaching Kelvin.";
    $("firewall-allow").hidden = v.firewall === "allowed";
    $("device-list").replaceChildren(...v.devices.map((d) => h("li", { class: "memory-note" },
      h("span", { class: "memory-kind" }, d.role),
      h("div", { class: "memory-body" }, h("span", {}, d.name),
        h("span", { class: "memory-meta" }, d.last_seen ? "last seen " + new Date(d.last_seen * 1000).toLocaleString() : "not used yet")),
      rowButton("trash", "Unpair " + d.name, async () => {
        try { await api("/api/devices/" + d.id, { method: "DELETE" }); } catch (e) { toast(e.message); }
        loadPhone();
      }))));
  }
  async function switchPhone() {
    $("phone-status").textContent = $("phone-on").checked ? "Starting…" : "Stopping…";
    try { renderPhone(await api("/api/phone", { body: { on: $("phone-on").checked, port: PHONE.view ? PHONE.view.port : undefined } })); }
    catch (e) { $("phone-status").textContent = e.message; }
  }
  async function allowFirewall() {
    $("firewall-allow").disabled = true;
    $("firewall-status").textContent = "Windows asks for permission now…";
    try { renderPhone(await api("/api/phone/firewall", { body: {} })); toast("Phones can reach Kelvin through Windows Firewall now."); }
    catch (e) { $("firewall-status").textContent = e.message; }
    $("firewall-allow").disabled = false;
  }
  async function startPairing() {
    clearInterval(PHONE.pairTimer);
    let r;
    try { r = await api("/api/devices", { body: { name: $("pair-name").value.trim(), role: $("pair-role").value } }); }
    catch (e) { $("phone-status").textContent = e.message; return; }
    const state = h("p", { class: "pair-wait" }, "Waiting for the phone… the code works for 10 minutes, once.");
    $("pair-box").replaceChildren(
      r.qr ? h("div", { class: "qr", html: r.qr }) : null,
      h("div", { class: "pair-text" },
        h("p", {}, "On " + r.name + ", scan the code, or open"),
        ...r.urls.map((u) => h("p", { class: "pair-url" }, u.split("#")[0])),
        h("p", {}, "and type"), h("p", { class: "pair-code" }, r.code.slice(0, 3) + " " + r.code.slice(3)), state));
    $("pair-box").hidden = false;
    PHONE.pairTimer = setInterval(async () => {
      if (!$("settings-dialog").open) { clearInterval(PHONE.pairTimer); return; }
      let p;
      try { p = await api("/api/devices/pairing/" + r.code); } catch (e) { clearInterval(PHONE.pairTimer); state.textContent = "The code expired. Make a new one."; return; }
      if (p.device) { clearInterval(PHONE.pairTimer); state.textContent = "Paired. " + r.name + " can use Kelvin now."; loadPhone(); }
      else if (!p.expires_in) { clearInterval(PHONE.pairTimer); state.textContent = "The code expired. Make a new one."; }
    }, 2000);
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
  function offerReview(reply) {  // the latest answer can get a second opinion from another model
    for (const el of document.querySelectorAll(".msg-actions")) el.remove();
    if (!reply || !S.chatId || !reply.el.querySelector(".md")) return;
    const btn = h("button", { class: "link soft", type: "button", "aria-haspopup": "true" }, icon("check"), "Second opinion");
    const menu = h("div", { class: "pane-menu review-menu", hidden: true });
    const wrap = h("div", { class: "msg-actions pane-menu-wrap" }, btn, menu);
    btn.addEventListener("click", () => {
      if (!menu.hidden) { menu.hidden = true; return; }
      const preferred = (S.settings && S.settings.reviewer) || "";
      const models = (S.groups || []).flatMap((g) => g.models.map((m) => ({ key: m.key, label: m.label, provider: g.label })));
      models.sort((a, b) => (b.key === preferred) - (a.key === preferred));
      for (let i = models.length - 1; i >= 0; i--) if (!modelAllowed(models[i].key, S.chatRules)) models.splice(i, 1);
      menu.replaceChildren(h("div", { class: "pane-menu-note" }, "Which model should check this answer?"),
        ...models.map((m) => {
          const item = h("button", { class: "pane-menu-item", type: "button" }, h("span", {}),
            h("span", { class: "pm-name" }, m.label), h("span", { class: "pm-meta" }, m.key === preferred ? "your reviewer" : m.provider));
          item.addEventListener("click", () => { menu.hidden = true; askReview(reply, wrap, m.key); });
          return item;
        }));
      menu.hidden = false;
    });
    (reply.tools || reply.el).append(wrap);
  }
  async function askReview(reply, wrap, model) {
    const pending = h("div", { class: "review pending" }, h("span", { class: "spinner" }),
      h("span", {}, "Asking " + (S.labels[model] || model) + " for a second opinion…"));
    (reply.tools && reply.tools.contains(wrap) ? reply.tools : wrap).before(pending);   // above the answer's buttons
    wrap.hidden = true;
    try {
      const r = await api("/api/chats/" + S.chatId + "/review", { body: { model } });
      const card = reviewCard(r);
      pending.replaceWith(card);
      reply.reviewEl = card; reply.reviewData = r;
      offerRecheck(reply);
      $("input").placeholder = "Reply — Kelvin will see the second opinion";
    } catch (e) {
      pending.replaceWith(h("div", { class: "callout" }, e.message));
      wrap.hidden = false;
    }
  }

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
      case "tool_end": reply.toolEnd(ev.id, ev.content, ev.is_error, ev.run_id, ev.images); pollState(); break;
      case "run_progress": reply.runProgress(ev); break;
      case "computer_step": reply.computerStep(ev); break;
      case "note": reply.note(ev.text); break;
      case "condensing": reply.condensing(ev.words); break;
      case "condensed": reply.condensed(ev); break;
      case "memory": reply.memory(ev.notes); break;
      case "review_start": reply.reviewStart(ev); break;
      case "review": reply.review(ev); break;
      case "error": reply.error(ev.message); break;
      default: break;
    }
    if (stick) toBottom();
  }

  // ------------------------------------------------------------------ lab panel
  const OPEN = new Set(), SHOW_SETTINGS = new Set();
  const PANEL = { filter: "", activeOnly: store("lh.activeOnly") === "1", instruments: [], loose: new Map(), everyone: false };

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
  function instrumentRow(inst) {
    const open = OPEN.has(inst.name);
    const head = inst.readings.find((r) => r.name === inst.headline);
    const [hv, hu] = head ? splitValue(head.value) : ["", ""];
    const off = inst.enabled === false;
    const missing = inst.state === "unavailable";   // could not be connected (its package, the instrument itself)
    const stateText = missing ? "not connected" : off ? "switched off" : inst.state === "error" ? "not responding"
      : inst.state === "active" ? inst.active + " active" : "safe";
    const chev = icon("chev"); chev.classList.add("chev");
    const count = inst.state === "active" ? h("span", { class: "inst-count" }, String(inst.active))
      : inst.state === "error" || missing ? h("span", { class: "inst-count err" }, "!") : h("span", { class: "inst-count none" });
    const row = h("button", { class: "inst-row", type: "button", "aria-expanded": String(open),
      title: inst.model + " · " + stateText + (head ? " · " + head.label + " " + head.value : "") },
      h("span", { class: "inst-icon" }, icon(ICONS[inst.kind] ? inst.kind : "instrument")),
      h("span", { class: "inst-id" }, inst.name),
      h("span", { class: "inst-model" }, inst.model.replace(/^Virtual\s+/, "")),
      h("span", { class: "inst-headline" }, hv, hu ? h("span", { class: "hu" }, hu) : null),
      count, chev);
    row.addEventListener("click", () => {
      if (open) OPEN.delete(inst.name); else OPEN.add(inst.name);
      renderInstruments();
    });
    // on/off for this experiment: off, Kelvin sends it nothing at all until a person switches it on (lab.py)
    const sw = h("button", { class: "inst-switch", type: "button", role: "switch", "aria-checked": String(!off),
      "aria-label": "Use " + inst.name, title: missing ? "Not connected: " + inst.why + " Switch it on to try again."
        : off ? "Switched off: Kelvin sends " + inst.name + " nothing. Switch it on to use it."
        : "On: Kelvin may use " + inst.name + ". Switch it off to keep Kelvin from sending it anything." });
    sw.addEventListener("click", async () => {
      sw.disabled = true;
      try {
        const r = await api("/api/instruments/" + encodeURIComponent(inst.name) + "/switch", { body: { on: off } });
        PANEL.instruments = r.instruments;
      } catch (e) { toast(e.message); }
      renderInstruments();
    });
    const item = h("div", { class: "inst " + inst.state + (open ? " open" : "") }, h("div", { class: "inst-head" }, row, sw));
    const on = inst.drives.filter((d) => d.away);
    if (missing) item.append(h("div", { class: "inst-sub" }, "Not connected: " + inst.why, ...installOffer(inst.install, pollState)));
    else if (off) item.append(h("div", { class: "inst-sub" }, "Off: Kelvin sends it nothing"));
    else if (inst.state === "error") item.append(h("div", { class: "inst-sub" }, "Not responding"));
    else if (on.length && !open) item.append(h("div", { class: "inst-sub" }, on.map((d) => d.label + " " + d.value).join(" · ")));
    if (!open) return item;
    const card = item;
    const body = h("div", { class: "inst-body" });
    if (missing) body.append(h("div", { class: "hint" }, "Kelvin could not connect to it, so it sends it nothing, and plans "
      + "that use it are refused. Once that is fixed, switch it on to connect it; the rest of the lab works as usual."));
    else if (off) body.append(h("div", { class: "hint" }, "Kelvin sends it no command and reads nothing from it: no checks, "
      + "no alarms, and plans that use it are refused. Switch it on to use it again."));
    const others = inst.readings.filter((r) => r.name !== inst.headline);
    if (others.length) body.append(h("div", { class: "group" }, h("div", { class: "group-title" }, "Readings"), ...others.map((r) => kv(r))));
    if (inst.drives.length) body.append(h("div", { class: "group" }, h("div", { class: "group-title" }, "Outputs"), ...inst.drives.map((d) => kv(d))));
    if (inst.settings.length) {
      const shown = SHOW_SETTINGS.has(inst.name);
      const toggle = h("button", { class: "link settings-toggle", type: "button" },
        (shown ? "Hide settings" : "Show settings") + " (" + inst.settings.length + ")");
      toggle.addEventListener("click", () => {
        if (shown) SHOW_SETTINGS.delete(inst.name); else SHOW_SETTINGS.add(inst.name);
        renderInstruments();
      });
      const group = h("div", { class: "group settings" }, toggle);
      if (shown) group.append(...inst.settings.map((x) => kv(x)));
      body.append(group);
    }
    card.append(body);
    return card;
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
    for (const b of document.querySelectorAll("#inst-seg button")) {
      const on = (b.dataset.v === "active") === PANEL.activeOnly;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    }
    $("lab-status").replaceChildren(...(shown.length ? shown.map(instrumentRow)
      : [h("div", { class: "inst-empty" }, PANEL.activeOnly && !q ? "Nothing is active." : "No instrument matches.")]));
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
      $("agent-state").textContent = "Kelvin is continuing “" + (woke.title || "a chat") + "”";
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
    try { s = await api("/api/state"); } catch (e) { return; }
    $("agent-state").textContent = s.busy ? "Kelvin is " + s.busy : "Kelvin is idle";
    $("agent-state").classList.toggle("busy", !!s.busy);
    PANEL.instruments = s.instruments;
    renderInstruments();
    let lines = s.events.map((e) => ({ text: e, user: "" }));
    if (PANEL.everyone) { try { lines = (await api("/api/lab-log?n=25")).events; } catch (e) { /* keep this person's */ } }
    $("lab-events").replaceChildren(...lines.map((e) => {
      const m = e.text.match(/^(\d\d:\d\d:\d\d)\s+(.*)$/);
      const who = e.user ? e.user + ": " : "";
      return m ? h("li", {}, h("time", {}, m[1]), h("span", {}, who + m[2])) : h("li", {}, who + e.text);
    }));
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
    $("lab-pending").replaceChildren(...[...PANEL.loose.values()].map((c) => c.el));
    const badge = $("lab-badge");
    badge.textContent = String(s.pending.length);
    badge.hidden = !s.pending.length;
    if (fresh && !S.labOpen) toggleLab(true);
    showMode(s.approval_mode, s.approval_rule);
    renderAlerts(s.alarms, s.incident);
    renderWatches(s.watches || [], s.status);
    notifySystem(s.notices);
    $("lab-name").textContent = s.lab;
    $("sim-tag").hidden = !s.simulated;
    const problem = s.lab_info && s.lab_info.error;
    const missing = problem ? [] : (s.unavailable || []);
    $("lab-problem").hidden = !problem && !missing.length;
    if (problem || missing.length) {
      const text = problem ? problem + " Kelvin is using the virtual lab meanwhile."
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
    if (unreviewed.length) $("lab-observe-who").textContent = "Kelvin added " + andList(unreviewed) + " itself.";
    const drafts = s.drafts || [];   // real instruments whose card nobody has checked yet
    $("lab-drafts").hidden = !drafts.length;
    if (drafts.length) {
      $("lab-drafts-who").textContent = drafts.length === 1
        ? drafts[0] + " runs on a draft card: Kelvin asks you before each setting or output change on it until you check the card against the instrument and mark it reviewed."
        : andList(drafts) + " run on draft cards: Kelvin asks you before each setting or output change on them until you check their cards against the instruments and mark them reviewed.";
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
    const on = i.outputs.length && !i.safe.length;
    return h("div", { class: "alert incident" }, icon("warn"), h("div", { class: "alert-body" },
      h("b", {}, "Kelvin restarted" + (i.at ? " at " + String(i.at).slice(11, 16) : "") + ". Check the lab before you go on:"),
      ...i.lines.map((l) => h("span", {}, l))),
      on ? act("Bring the outputs to safe", "safe", true) : null, act("Dismiss", "dismiss", false));
  }
  function renderAlerts(alarms, incident) {
    const live = (alarms || []).filter((a) => !a.cleared && !a.acknowledged);
    const box = $("alerts");
    box.hidden = !live.length && !incident;
    box.replaceChildren(...(incident ? [incidentCard(incident)] : []), ...live.map((a) => {
      const ok = h("button", { class: "btn small", type: "button" }, "Got it");
      ok.addEventListener("click", async () => { try { await api("/api/alarms/" + a.id + "/ack", { body: {} }); } catch (e) { /* gone */ } pollState(); });
      return h("div", { class: "alert" }, icon("warn"), h("div", { class: "alert-body" }, h("b", {}, a.text),
        a.actions.length ? h("span", {}, "Kelvin " + a.actions.join("; ")) : null), ok);
    }));
    document.title = (live.length || incident ? "(!) " : "") + document.title.replace(/^\(!\) /, "");
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
  function renderWatches(watches, status) {
    const paused = watches.filter((w) => w.paused).length;
    $("watch-summary").textContent = watches.length ? watches.filter((w) => w.alarm).length + " alarm · "
      + (watches.length - paused) + " watched" + (paused ? " · " + paused + " paused" : "") : "";
    $("watch-list").replaceChildren(...(watches.length ? watches.map((w) => {
      const remove = w.source === "lab" ? h("span", { class: "tag", title: "From the lab config; change it there" }, "lab")
        : rowButton("x", "Stop watching", async () => { try { await api("/api/watches/" + w.id, { method: "DELETE" }); } catch (e) { toast(e.message); } pollState(); });
      return h("li", { class: "watch" + (w.alarm ? " alarm" : ""), title: w.paused ? "Paused: its instrument is switched off, so nothing is read" : null },
        h("span", { class: "dot" + (w.alarm ? " bad" : w.paused ? "" : " on") }),
        h("span", { class: "watch-rule" }, w.rule, w.paused ? h("span", { class: "watch-then" }, " (paused: instrument off)")
          : w.then.length ? h("span", { class: "watch-then" }, " → " + w.then.join(", ")) : null),
        h("span", { class: "watch-src" }, w.source === "agent" ? "Kelvin" : ""), remove);
    }) : [h("li", { class: "inst-empty" }, "Nothing is watched. Add a reading to be told when it leaves its range.")]));
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
  async function addWatch(e) {
    e.preventDefault();
    const body = { key: $("watch-key").value, above: $("watch-above").value, below: $("watch-below").value, for_s: $("watch-for").value };
    try {
      await api("/api/watches", { body });
      $("watch-form").reset(); $("watch-form").hidden = true;
      toast("Watching " + body.key + ".");
      pollState();
    } catch (err) { toast(err.message); }
  }

  // ------------------------------------------------------------------ the lab Kelvin controls (people only)
  const KIND_NAME = { "source-meter": "smu", "lock-in": "lockin", "temperature-controller": "tc", "magnet-supply": "magnet",
    "laser": "laser", "laser-driver": "laser", "power-meter": "pm", "preamp": "preamp", "multimeter": "dmm",
    "nanovoltmeter": "nvm", "function-generator": "fg", "voltage-source": "vsrc", "vna": "vna" };
  const LAB = { cards: [], found: [], scripts: null, scriptsFolder: "" };

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
    for (const b of document.querySelectorAll("#lab-seg button")) b.classList.toggle("on", (b.dataset.use === "mine") === mine);
    $("lab-now").textContent = "Kelvin now controls " + (name || "the lab") + (mine ? "" : " (simulated)") + ".";
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
        if (!confirm(c.kelvins ? "Go back to Kelvin's own card " + c.id + "? Your lab's version is deleted."
          : "Delete the card " + (c.id || c.file) + "?")) return;
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
      showCardEditor(c.yaml, c.id, [], [], "Your lab's version of Kelvin's card " + c.id + ": keep its id, change what differs on your instrument, then Save card. It replaces Kelvin's card in this lab.");
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
    if (status === "lab-verified" && !confirm("Mark " + c.model + " as reviewed? You have checked its commands, limits and safe values against the manual and the instrument. Its outputs can then reach real hardware, still within your lab limits and approvals.")) return;
    try { await api("/api/cards/status", { body: { id: c.id, status } }); } catch (e) { $("cards-msg").textContent = e.message; return; }
    loadCards();
  }

  // ------------------------------------------------------------------ the scan queue (like Special Measure's)
  // Plans and steps lined up to run one after another; approved once, run in the background, paused on a failure.
  const QUEUE = { open: false, data: null, editing: null, built: false };
  const QUEUE_STEP = "step: cool to 2 K\nset: { tc.T_set: 2 }\nstabilize: { read: tc.T, within: 0.02, for: 120, timeout: 7200 }\nwait: 600";
  const Q_STATE = { idle: "Idle", running: "Running", pausing: "Pausing after this item", paused: "Paused" };
  const Q_STATUS = { waiting: "waiting", running: "running…", done: "done", failed: "failed", stopped: "stopped", skipped: "skipped" };

  function buildQueueView() {
    const add = h("button", { id: "queue-add", class: "btn small", type: "button" }, "Add to the queue");
    const example = h("button", { class: "link", type: "button" }, "a step");
    const view = $("queue-view");
    view.replaceChildren(
      h("div", { class: "q-head" }, h("span", { id: "queue-state", class: "q-state" }), h("span", { id: "queue-reason", class: "q-reason" }),
        h("button", { id: "queue-start", class: "btn small primary", type: "button" }, "Start"),
        h("button", { id: "queue-pause", class: "btn small", type: "button" }, "Pause"),
        h("select", { id: "queue-onerror", "aria-label": "When an item fails" },
          h("option", { value: "pause" }, "On a failure: pause"), h("option", { value: "continue" }, "On a failure: go on"))),
      h("ul", { id: "queue-list", class: "q-list", "aria-label": "Queue" }),
      h("div", { class: "field q-add" },
        h("textarea", { id: "queue-text", class: "mono", spellcheck: "false", "aria-label": "A plan or a step to queue",
          placeholder: "A plan (as Kelvin writes them, or copied from a run's Plan), or a step between scans:\n" + QUEUE_STEP }),
        h("div", { class: "add-row" },
          h("input", { id: "queue-note", placeholder: "Note (kept with the run)", "aria-label": "Note" }),
          h("input", { id: "queue-pos", class: "q-pos", placeholder: "at #", inputmode: "numeric", "aria-label": "Position" }),
          add),
        h("span", { class: "hint" }, "Add a scan or ", example, " (set values, wait for a reading to settle, wait). Ask Kelvin in the chat to line up scans for you. Keys on an item: u / d move it, Delete removes it.")),
      h("div", { class: "q-foot" },
        h("button", { id: "queue-clear", class: "btn small ghost", type: "button" }, "Clear finished"),
        h("button", { id: "queue-save", class: "btn small ghost", type: "button" }, "Save as…"),
        h("select", { id: "queue-saved", "aria-label": "Saved queues" }),
        h("button", { id: "queue-load", class: "btn small ghost", type: "button" }, "Load")),
      h("p", { id: "queue-msg", class: "hint", role: "status" }));
    example.addEventListener("click", () => { $("queue-text").value = QUEUE_STEP; $("queue-text").focus(); });
    add.addEventListener("click", queueAddOrSave);
    $("queue-start").addEventListener("click", async () => {
      $("queue-msg").textContent = "Checking every waiting item… then approve the queue once.";
      $("queue-start").disabled = true;
      await queueOp({ op: "start" });
      $("queue-start").disabled = false;
    });
    $("queue-pause").addEventListener("click", () => queueOp({ op: "pause" }));
    $("queue-onerror").addEventListener("change", (e) => queueOp({ op: "on_error", value: e.target.value }, true));
    $("queue-clear").addEventListener("click", () => queueOp({ op: "clear" }, true));
    $("queue-save").addEventListener("click", () => {
      const name = prompt("Save the waiting items as:", "overnight");
      if (name) queueOp({ op: "save", name: name.trim() });
    });
    $("queue-load").addEventListener("click", () => { if ($("queue-saved").value) queueOp({ op: "load", name: $("queue-saved").value }); });
    QUEUE.built = true;
  }
  function showQueue() {
    if (!QUEUE.built) buildQueueView();
    toggleRuns(true);
    RUNS.active = null;
    for (const t of RUNS.tabs.values()) { t.btn.classList.remove("on"); t.btn.setAttribute("aria-selected", "false"); t.body.hidden = true; }
    $("run-body").querySelector(".pane-empty").hidden = true;
    $("queue-view").hidden = false;
    $("open-queue").classList.add("on");
    QUEUE.open = true;
    renderQueue();
    api("/api/queue").then((q) => { QUEUE.data = q; renderQueue(); }, () => {});
  }
  function hideQueue() {
    QUEUE.open = false;
    $("queue-view").hidden = true;
    $("open-queue").classList.remove("on");
  }
  async function queueOp(body, quiet) {
    try {
      const r = await api("/api/queue", { body });
      QUEUE.data = r;
      renderQueue();
      if (!quiet) $("queue-msg").textContent = (r.message || "").split("\n")[0];
      return r;
    } catch (e) {
      if (QUEUE.built) $("queue-msg").textContent = e.message; else toast(e.message);
      return null;
    }
  }
  async function queueAddOrSave() {
    const text = $("queue-text").value.trim();
    if (!text) { $("queue-msg").textContent = "Paste a plan or a step first."; return; }
    const r = QUEUE.editing
      ? await queueOp({ op: "edit", item: QUEUE.editing, text })
      : await queueOp({ op: "add", text, note: $("queue-note").value.trim(), position: $("queue-pos").value.trim() || null });
    if (r) { $("queue-text").value = $("queue-note").value = $("queue-pos").value = ""; queueEditing(null); }
  }
  function queueEditing(item) {
    QUEUE.editing = item ? item.id : null;
    $("queue-add").textContent = item ? "Save changes to #" + item.n : "Add to the queue";
    if (item) { $("queue-text").value = item.text; $("queue-text").focus(); }
  }
  function queueItem(i, n, count) {
    const buttons = [];
    const act = (label, title, fn) => {
      const b = h("button", { type: "button", title }, label);
      b.addEventListener("click", fn);
      buttons.push(b);
    };
    const idle = i.status !== "running";
    if (n > 1) act("↑", "Move up (u)", () => queueOp({ op: "move", item: i.id, position: n - 1 }, true));
    if (n < count) act("↓", "Move down (d)", () => queueOp({ op: "move", item: i.id, position: n + 1 }, true));
    if (idle) act("Edit", "Edit this item", () => queueEditing(Object.assign({ n }, i)));
    if (idle && i.status !== "waiting") act("Again", "Put it back in line", () => queueOp({ op: "again", item: i.id }, true));
    if (i.run_id) act("Open", "Open its run", () => openRun(i.run_id, i.name));
    if (idle) act("✕", "Remove (Delete)", () => queueOp({ op: "remove", item: i.id }, true));
    const st = i.status === "running" ? h("span", { class: "spinner" }) : i.status === "done" ? icon("check")
      : ["failed", "stopped"].includes(i.status) ? icon("warn") : h("span", { class: "muted" }, "·");
    const sub = [h("span", { class: "q-status" }, Q_STATUS[i.status] || i.status)];
    if (i.note) sub.push(h("span", {}, i.note + " "));
    if (i.message) sub.push(h("span", {}, "— " + i.message));
    const li = h("li", { class: "q-item " + i.status, tabindex: "0", title: i.text },
      h("span", { class: "q-num" }, st), h("span", { class: "q-kind" }, i.kind === "plan" ? "scan" : "step"),
      h("span", { class: "q-name" }, n + ". " + i.name), h("span", { class: "q-acts" }, ...buttons), h("div", { class: "q-sub" }, ...sub));
    li.addEventListener("keydown", (e) => {   // Special Measure's keys: u / d move the item, Delete removes it
      if (e.target !== li) return;
      if (e.key === "u" && n > 1) queueOp({ op: "move", item: i.id, position: n - 1 }, true);
      else if (e.key === "d" && n < count) queueOp({ op: "move", item: i.id, position: n + 1 }, true);
      else if ((e.key === "Delete" || e.key === "Backspace") && i.status !== "running") queueOp({ op: "remove", item: i.id }, true);
      else return;
      e.preventDefault();
    });
    return li;
  }
  function renderQueue() {
    const q = QUEUE.data;
    queueBadge(q);
    if (!QUEUE.open || !QUEUE.built || !q) return;
    const items = q.items || [];
    const running = q.state === "running" || q.state === "pausing";
    const pos = items.findIndex((i) => i.status === "running") + 1;
    $("queue-state").textContent = (Q_STATE[q.state] || q.state) + (pos ? " · item " + pos + " of " + items.length : "");
    $("queue-reason").textContent = q.reason && q.reason !== "finished" ? q.reason
      : q.waiting ? q.waiting + " waiting" : q.reason === "finished" ? "Finished" : "";
    $("queue-start").hidden = running || !q.waiting;
    $("queue-start").textContent = q.state === "paused" ? "Resume" : "Start";
    $("queue-pause").hidden = q.state !== "running";
    $("queue-onerror").value = q.on_error || "pause";
    const focused = document.activeElement && document.activeElement.closest && document.activeElement.closest(".q-item");
    const focusIdx = focused ? [...$("queue-list").children].indexOf(focused) : -1;
    $("queue-list").replaceChildren(...(items.length ? items.map((i, n) => queueItem(i, n + 1, items.length))
      : [h("li", { class: "muted small" }, "Nothing queued. Add scans below, or ask Kelvin to line them up.")]));
    if (focusIdx >= 0 && $("queue-list").children[focusIdx]) $("queue-list").children[focusIdx].focus();
    const saved = q.saved || [];
    $("queue-saved").replaceChildren(...(saved.length ? saved.map((s) => h("option", { value: s }, s)) : [h("option", { value: "" }, "No saved queues")]));
    $("queue-load").disabled = !saved.length;
  }
  function queueBadge(q) {
    if (!q) return;
    const n = (q.waiting || 0) + (q.running ? 1 : 0);
    $("queue-count").textContent = String(n);
    $("queue-count").hidden = !n;
    $("open-queue").classList.toggle("live", q.state === "running" || q.state === "pausing");
  }
  function queueFromState(q) {
    if (!q) return;
    const old = QUEUE.data;
    QUEUE.data = Object.assign({}, q);
    if (!old || old.changed !== q.changed || old.state !== q.state || old.reason !== q.reason) renderQueue();
    else queueBadge(q);
  }

  // ------------------------------------------------------------------ why Kelvin is on the virtual lab
  function whyVirtual(s) {
    if (!s.simulated) return null;
    const setup = s.setup || {};
    if (s.lab_info && s.lab_info.error) return null;   // the lab-problem callout says why
    if (SETUP_BUSY.has(setup.state)) return null;        // "Setting up instruments…" shows instead
    const lines = (setup.lines && setup.lines.length ? setup.lines : (s.survey && s.survey.lines)) || [];
    const text = setup.state === "done" || setup.state === "failed" ? setup.message
      : s.survey ? s.survey.reason : "Kelvin hasn't looked for instruments on this computer yet. Set up… scans for them.";
    return { text, lines };
  }
  function showWhy(s) {
    const why = whyVirtual(s);
    $("lab-why").hidden = !why;
    document.querySelector(".lab-chip").title = why ? "On the virtual lab: " + why.text : "The lab this portal controls";
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
          (r.now ? "Kelvin sets them up the way your scripts use them: names, settings, and limits from the values used, for you to check."
            : "When the setup is done, Set up shows what they add: instruments to add, and limits from the values they use.");
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
      ? "Kelvin added " + andList(unreviewed) + " itself. Check " + (unreviewed.length === 1 ? "its" : "their") +
        " limits with Limits… on " + (unreviewed.length === 1 ? "its row" : "each one's row") + " below: until you mark them reviewed, Kelvin asks you before each output change on " +
        (unreviewed.length === 1 ? "it." : "them.")
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
      ? "What " + name + " may reach. Kelvin refuses anything beyond these and asks before driving harder. Empty: the card's limits." +
        (r.editable ? "" : " The virtual lab's limits are fixed; your own lab's are set here.")
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
          const saved = await api("/api/instruments/" + encodeURIComponent(name) + "/limits", { body: { limits, reviewed: !r.reviewed && reviewed.checked } });
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
      foot.append(save, msg);
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
    if (!confirm("Mark the limits reviewed?\n\nYou have checked the limits in the lab config of the instruments " +
        "Kelvin added: what each output may reach (voltage, current, field, temperature, power) is what your sample " +
        "and setup can take. Kelvin then treats them like your other instruments: it still asks before driving " +
        "harder, unless auto mode or the project envelope covers the change.")) return;
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
    $("lab-find").hidden = $("lab-entries").hidden = $("lab-rest").hidden = !byInstrument;
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
      $("lab-msg").textContent = "Kelvin wrote " + r.card_id + " from " + r.manual_url + ". It is a draft: Kelvin " +
        "asks you before each setting or output change on it until you review it under Your instrument cards.";
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Find its manual and write its card";
      $("lab-msg").textContent = e.message;
    }
  }
  async function addToConfig(items) {
    if (!items.length) { $("lab-msg").textContent = "Nothing chosen."; return; }
    try {
      const r = await api("/api/instruments/draft", { body: { items, base: $("lab-yaml").value } });
      $("lab-yaml").value = r.yaml;
      renderLabConfig();
      const already = r.already || [], there = new Set(already.map((a) => a.item));
      const added = items.filter((i, k) => !there.has(k)).map((i) => i.name);
      $("lab-msg").textContent = [added.length ? "Added " + added.join(", ") + ". Review the limits, then save and connect." : "",
        already.length ? already.map((a) => a.name + " (" + a.address + ")").join(", ") + (already.length === 1 ? " is" : " are") + " in the lab config already." : ""].filter(Boolean).join(" ");
    } catch (e) { $("lab-msg").textContent = e.message; }
  }
  // ------------------------------------------------------------------ setting up from the lab's own scripts
  async function fromScripts() {
    const folder = await pickFolder(LAB.scriptsFolder || "", "The folder with your lab's scripts (Python, notebooks, MATLAB):");
    if (folder) await surveyScripts(folder);
  }
  async function surveyScripts(folder) {
    LAB.scriptsFolder = folder;
    const btn = $("lab-scripts");
    btn.disabled = true; btn.textContent = "Reading your scripts…";
    $("lab-msg").textContent = "Reading the scripts in " + folder + " (never running them), then asking each instrument who it is…";
    try {
      LAB.scripts = await api("/api/instruments/scripts", { body: { folder } });
      renderScripts();
      const n = LAB.scripts.instruments.length, s = LAB.scripts.scripts.length;
      $("lab-msg").textContent = n ? "Found " + n + " instrument" + (n === 1 ? "" : "s") + " in " + s + " script" + (s === 1 ? "" : "s") +
        ". Check the cards and limits, then add the ones you want." : "No instrument work in the scripts there.";
    } catch (e) { $("lab-msg").textContent = e.message; }
    finally { btn.disabled = false; btn.textContent = "From your scripts…"; }
  }
  function renderScripts() {
    const r = LAB.scripts, box = $("lab-from-scripts");
    box.hidden = !r || !r.instruments.length;
    $("lab-scripts-actions").hidden = !r || (!r.instruments.length && !r.scripts.length);
    $("lab-add-scripts").hidden = !r || !r.instruments.some((it) => !it.in_lab);
    $("lab-recipes").hidden = !r || !r.scripts.length;
    if (!r) return;
    const span = (lim) => Object.entries(lim || {}).map(([p, v]) => p + " " + v[0] + " to " + v[1]).join(", ");
    box.replaceChildren(h("p", { class: "hint" }, "In " + r.folder + ": " + r.scripts.length + " script" + (r.scripts.length === 1 ? "" : "s") + " read, none run."),
      ...r.instruments.map((it) => {
        if (it.in_lab) {
          const row = h("div", { class: "found in-lab" }, h("span", { class: "found-name" }, it.in_lab),
            h("span", { class: "found-model" }, "already in this lab"), h("code", { class: "found-addr" }, it.address));
          const lim = it.card && Object.keys(it.card.limits || {}).length ? it.card.limits : null;
          if (!lim) return row;
          const use = h("button", { class: "link soft", type: "button" }, "Use these limits");
          const note = h("div", { class: "found-write muted small" }, "Limits from the values your scripts use: " + span(lim) + " ", use);
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
        check.checked = was ? was.check : !!(card && card.id && it.address);
        const name = h("input", { class: "found-input", value: was ? was.name : it.name || "", placeholder: "name", spellcheck: "false", "aria-label": "Name" });
        const model = cardSelect(was ? was.card : card ? card.id : "", "Choose its card…");
        model.addEventListener("change", () => { if (model.value && (it.address || it.ui.addr.value.trim())) check.checked = true; });
        const addr = it.address ? h("code", { class: "found-addr", title: it.where.join(", ") }, it.address)
          : h("input", { class: "found-input found-addr-in", placeholder: "its address", spellcheck: "false", "aria-label": "Address",
            title: "The scripts set it as " + (it.expression || "?"), value: was ? was.addr : "" });
        it.ui = { check, name, model, addr };
        const why = [card ? card.why : "no card of Kelvin's fits: choose one, or make one under Cards",
          it.idn && !(card && card.why.includes("answered")) ? "answered " + it.idn.slice(0, 60) : it.asked || "",
          card && Object.keys(card.limits || {}).length ? "limits from the values used: " + span(card.limits) : "",
          it.address ? "" : "address set at run time (" + (it.expression || "?") + "): type it",
          "in " + it.where.slice(0, 2).join(", ")].filter(Boolean).join(" · ");
        return h("div", { class: "found-wrap" }, h("label", { class: "found" }, check, name, model, addr),
          h("div", { class: "found-write muted small" }, why));
      }));
  }
  function addScripts() {
    const items = [];
    for (const it of (LAB.scripts || {}).instruments || []) {
      if (!it.ui || !it.ui.check.checked) continue;
      const address = it.address || it.ui.addr.value.trim(), card = it.ui.model.value;
      if (!address || !card) { $("lab-msg").textContent = (it.ui.name.value || "An instrument") + " needs its address and its card."; return; }
      items.push({ name: it.ui.name.value.trim() || "instr", card, address, connection: it.connection,
        limits: it.card && it.card.id === card ? it.card.limits : null,
        note: (it.idn || (it.card && it.card.why) || "from your scripts").slice(0, 60) });
    }
    addToConfig(items);
  }
  function recipesFromScripts() {   // in a new chat: Kelvin proposes a recipe per script, and keeps each one you agree to
    const folder = LAB.scripts && LAB.scripts.folder;
    if (!folder) return;
    $("lab-dialog").close();
    newChat();
    $("input").value = "Turn the measurement scripts in " + folder + " into recipes: read them with survey_scripts, " +
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
    $("lab-msg").textContent = body.use === "virtual" ? "Switching to the virtual lab…" : "Connecting and checking every instrument…";
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

  // A maker's package the lab's instruments need is missing (envs.py): install it, once the person approves what is
  // downloaded (the request appears with the other approvals), then connect the lab again.
  function installOffer(install, after) {
    if (!install || !install.packages || !install.packages.length) return [];
    const names = andList(install.packages);
    const button = h("button", { class: "link", type: "button" }, "Install " + names);
    const said = h("span", { class: "install-said" });
    button.addEventListener("click", async () => {
      button.disabled = true;
      said.textContent = " Approve the download above to go on…";
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
    $("fineprint").textContent = mode === "auto"
      ? "Auto mode: low-risk changes run without asking; everything else waits for your approval."
      : "Measurements run right away. Anything that drives the sample harder waits for your approval.";
    $("mode-box").title = mode === "auto" ? "Auto mode: " + (S.approvalRule || "low-risk changes run without asking")
      : "Kelvin asks you before anything drives the sample harder";
  }
  async function changeMode(mode) {
    if (mode === "auto" && !confirm("Turn on auto mode?\n\nKelvin then approves low-risk changes for you: " +
        (S.approvalRule || "drives that stay well inside the lab limits, after a clean dry run.") +
        "\n\nEverything else still waits for you. " +
        "Auto mode switches off when Kelvin restarts.")) {
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
    if (!TERM.open) return;
    if (!termOut().childElementCount) termLine("Kelvin terminal. Type to Kelvin, or /help for instrument commands.", "dim");
    termSync();
    $("term-input").focus();
  }
  function setTermHeight(px) {
    const max = Math.max(160, window.innerHeight - 220);
    const hgt = Math.round(Math.min(max, Math.max(120, px)));
    $("termpane").style.height = hgt + "px";
    return hgt;
  }
  function initTerminal() {
    try { TERM.history = JSON.parse(store("lh.termHistory") || "[]").filter((x) => typeof x === "string"); } catch (e) { TERM.history = []; }
    TERM.hi = TERM.history.length;
    const saved = Number(store("lh.termHeight"));
    setTermHeight(saved > 0 ? saved : Math.round(window.innerHeight * 0.36));
    const handle = $("term-resize");
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const bottom = $("termpane").getBoundingClientRect().bottom;
      handle.setPointerCapture(e.pointerId);
      document.body.classList.add("resizing-v");
      const move = (ev) => setTermHeight(bottom - ev.clientY);
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
      const cur = $("termpane").getBoundingClientRect().height;
      store("lh.termHeight", String(setTermHeight(cur + (e.key === "ArrowUp" ? 32 : -32))));
    });
    $("toggle-term").addEventListener("click", () => toggleTerm());
    $("close-term").addEventListener("click", () => toggleTerm(false));
    $("term-clear").addEventListener("click", () => { termOut().replaceChildren(); $("term-input").focus(); });
    $("term-input").addEventListener("keydown", termKey);
    termOut().addEventListener("mouseup", () => { if (!window.getSelection().toString()) $("term-input").focus(); });
    $("model").addEventListener("change", termSync);
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey && (e.key === "`" || e.code === "Backquote")) { e.preventDefault(); toggleTerm(); }
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
        el.textContent = "  ⎿ " + (ev.plan || "run") + ": " + ev.done + " / " + ev.total + " points" +
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
    termSync();
    if (!TERM.open) toggleTerm(true);
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
    } catch (e) { termLine(e.message, "err"); }
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
        : [h("p", { class: "muted small" }, "Nothing in Kelvin's help about that. Ask Kelvin in a chat: it can look further.")]));
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
  // Claude Sonnet: settings.default_model); a chat that is open keeps its own
  function applyModels(groups, preferred, auto) {
    const current = S.chatId && S.labels && S.labels[$("model").value] ? $("model").value : "";
    S.groups = groups;
    S.labels = {};
    for (const g of groups) for (const m of g.models) S.labels[m.key] = m.label;
    if (preferred !== undefined) S.defaultModel = preferred || "";
    if (auto !== undefined) S.autoModel = auto || "";
    fillModels($("model"), groups, (current && S.labels[current]) ? current : S.defaultModel);
    if ($("rules-chip")) applyRules();
    if (!$("model").value) $("input").placeholder = "Add an API key in Settings to start";
    else $("input").placeholder = "Ask Kelvin…";
    updateSend();
  }
  // Settings → Models → Default model for new chats: Automatic says which model it is now (Claude Sonnet when there)
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
      const remove = h("button", { class: "link", type: "button" }, "Remove");
      remove.addEventListener("click", async () => {
        if (!confirm("Remove " + r.name + " (" + pkgs + ")? Instruments that use it stop connecting until it is installed again.")) return;
        remove.disabled = true;
        try { toast((await api("/api/environments", { body: { remove: r.name } })).message); } catch (e) { toast(e.message); }
        loadEnvironments();
      });
      return h("li", {}, h("span", { class: "env-what" }, h("b", {}, r.name), " · Python " + r.python + " (" + r.bits + "-bit) · " + pkgs,
        h("div", { class: "muted" }, r.mb + " MB" + (r.for.length ? " · for " + r.for.join(", ") : ""))), remove);
    }));
  }

  // a select's option for a stored value, added when the value is one the list doesn't offer
  function pickOption(select, value, label) {
    if (![...select.options].some((o) => o.value === value)) select.append(h("option", { value }, label(value)));
    select.value = value;
  }

  function openSettings() {
    settingsPage(store("lh.settingsPage") || "models");
    showZoom();
    const keys = (S.settings && S.settings.keys) || {};
    for (const hint of document.querySelectorAll("[data-hint]")) {
      const src = keys[hint.dataset.hint];
      hint.textContent = src ? "Set (from " + src + ")" : "Not set";
      hint.classList.toggle("set", !!src);
    }
    for (const input of document.querySelectorAll("#settings-form input:not([type=checkbox])")) { input.value = ""; delete input.dataset.clear; }
    $("local-url").value = (S.settings && S.settings.local_base_url) || "";
    $("local-models").value = (S.settings && S.settings.local_models) || "";
    fillDefaultModel();
    fillModels($("reviewer"), S.groups, (S.settings && S.settings.reviewer) || "", true);
    $("reviewer").options[0].textContent = "Off";
    const words = S.settings ? S.settings.answer_words : 200, length = $("answer-words"), chosen = words ? String(words) : "off";
    if (![...length.options].some((o) => o.value === chosen)) length.append(h("option", { value: chosen }, "At most " + words + " words"));
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
    $("auto-report").value = (S.settings && S.settings.auto_report) || "both";
    ownerPinState();
    const idle = S.settings && S.settings.idle_off !== undefined ? S.settings.idle_off : 60, idleSel = $("idle-off");
    const idleValue = idle ? String(idle) : "off";
    if (![...idleSel.options].some((o) => o.value === idleValue)) idleSel.append(h("option", { value: idleValue }, "After " + idle + " minutes"));
    idleSel.value = idleValue;
    $("idle-off-all").checked = !!(S.settings && S.settings.idle_off_all);
    $("ask-folder").checked = !S.settings || S.settings.ask_folder !== false;
    $("data-choose").hidden = !hasPicker();
    const reportKinds = (S.settings && S.settings.report_kinds) || ["scan", "device", "project"];
    for (const c of document.querySelectorAll("#report-kinds input")) c.checked = reportKinds.includes(c.value);
    $("pair-box").hidden = true;
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
    pickOption($("approval-wait"), wait ? String(wait) : "off", (v) => "After 5 minutes, ask your phone and wait " + v + " minutes more");
    $("allow-notify").hidden = !("Notification" in window) || Notification.permission !== "default";
    $("settings-msg").textContent = "";
    renderGlobalNotes(null);
    $("settings-dialog").showModal();
    api("/api/memory").then((m) => renderGlobalNotes(m.notes)).catch(() => renderGlobalNotes([]));
    loadKnow("global", $("global-know"), $("global-know-count"), $("settings-msg"));
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
      list.replaceChildren(h("li", { class: "memory-empty" }, "Nothing yet. When a procedure or a staged measurement works, Kelvin offers to keep it here."));
      return;
    }
    list.replaceChildren(...items.map((it) => {
      const body = h("pre", { class: "know-body", hidden: true }, it.body);
      const show = h("button", { class: "link soft", type: "button" }, "Show");
      show.addEventListener("click", () => { body.hidden = !body.hidden; show.textContent = body.hidden ? "Show" : "Hide"; });
      const when = (it.saved ? "kept " + new Date(it.saved * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "comes with Kelvin") +
        (it.files && it.files.length ? " · " + it.files.length + (it.files.length === 1 ? " file" : " files") : "");
      const on = h("input", { type: "checkbox", "aria-label": "Kelvin uses this " + it.kind });
      on.checked = it.enabled !== false;
      on.addEventListener("change", async () => {
        try { await api("/api/know/" + it.kind + "/" + it.scope + "/" + encodeURIComponent(it.name), { body: { enabled: on.checked } }); it.enabled = on.checked; }
        catch (e) { on.checked = !on.checked; msg.textContent = e.message; }
      });
      const extra = [];
      if (it.kind === "skill") {
        const get = h("button", { class: "link soft", type: "button", title: "As a .zip for Claude or another Kelvin" }, "Download");
        get.addEventListener("click", () => downloadSkill(it));
        extra.push(get);
      }
      const row = h("li", { class: "memory-note" + (it.enabled === false ? " off" : "") }, h("span", { class: "memory-kind" }, it.kind),
        h("div", { class: "memory-body" }, h("span", {}, h("b", {}, it.name), it.description ? " — " + it.description : ""),
          h("span", { class: "memory-meta" }, when), show, ...extra, body),
        h("label", { class: "know-on", title: "Kelvin sees it (off: kept, but not offered)" }, on, "On"));
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
        if (confirm(r.error + ". Replace it?")) return addSkill(file, scope, after, msg, true);
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
        "Nothing yet. When you state a lasting preference (units, how careful to be, how you like answers), Kelvin keeps it here."));
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

  async function saveSettings() {
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
    $("settings-msg").textContent = "Saving…";
    try {
      const r = await api("/api/settings", { body });
      S.settings = r.settings;
      applyModels(r.models.groups, r.default_model, r.auto_model);
      $("settings-dialog").close();
      toast("Settings saved.");
    } catch (e) {
      $("settings-msg").textContent = e.message;
    }
  }

  // ------------------------------------------------------------------ text size: the desktop window's zoom, each person's own
  const ZOOMS = [0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];
  const ZOOM = { wheel: 0, saving: null };
  function canZoom() { return !!(window.pywebview && window.pywebview.api && window.pywebview.api.set_zoom); }
  function zoomNow() { const z = S.settings && Number(S.settings.text_size); return ZOOMS.includes(z) ? z : 1; }
  function showZoom() {
    const z = zoomNow(), can = canZoom();
    $("zoom-now").textContent = Math.round(z * 100) + "%";
    $("zoom-out").disabled = !can || z <= ZOOMS[0];
    $("zoom-in").disabled = !can || z >= ZOOMS[ZOOMS.length - 1];
    $("zoom-reset").hidden = !can || z === 1;
    $("zoom-hint").hidden = !can;
    $("zoom-browser").hidden = can;   // a browser has its own zoom
  }
  async function applyZoom() {
    if (canZoom()) { try { await window.pywebview.api.set_zoom(zoomNow()); } catch (e) { /* stays as it was */ } }
    showZoom();
  }
  function stepZoom(dir) {   // +1 bigger, -1 smaller, 0 back to 100%
    if (!canZoom() || !S.settings) return;
    const i = ZOOMS.indexOf(zoomNow());
    const z = dir === 0 ? 1 : ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, i + dir))];
    if (z === zoomNow()) return;
    S.settings.text_size = z;
    applyZoom();
    clearTimeout(ZOOM.saving);   // kept with the person's settings once they stop changing it
    ZOOM.saving = setTimeout(() => api("/api/settings", { body: { text_size: String(z) } }).catch(() => {}), 600);
  }
  function zoomKeys() {   // the desktop window's own Ctrl + and Ctrl − are off (pywebview): Kelvin's do the same
    document.addEventListener("keydown", (e) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || !canZoom()) return;
      const dir = e.key === "=" || e.key === "+" ? 1 : e.key === "-" || e.key === "_" ? -1 : e.key === "0" ? 0 : null;
      if (dir === null) return;
      e.preventDefault();
      stepZoom(dir);
    });
    window.addEventListener("wheel", (e) => {
      if (!e.ctrlKey || !canZoom()) return;
      e.preventDefault();
      ZOOM.wheel += e.deltaY;
      if (Math.abs(ZOOM.wheel) < 80) return;   // a touchpad sends many small steps
      stepZoom(ZOOM.wheel < 0 ? 1 : -1);
      ZOOM.wheel = 0;
    }, { passive: false });
    window.addEventListener("pywebviewready", applyZoom);
    $("zoom-in").addEventListener("click", () => stepZoom(1));
    $("zoom-out").addEventListener("click", () => stepZoom(-1));
    $("zoom-reset").addEventListener("click", () => stepZoom(0));
  }

  // ------------------------------------------------------------------ layout bits
  function autosize() {
    const t = $("input");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 220) + "px";
    updateSend();
  }
  function closeSidebar() { $("app").classList.remove("side-open"); syncScrim(); }
  function syncScrim() {
    const narrow = window.matchMedia("(max-width: 1100px)").matches;
    $("scrim").hidden = !($("app").classList.contains("side-open") || ((S.labOpen || RUNS.open) && narrow));
  }

  const SUGGESTIONS = [
    ["Check the lab", "What is on, and is anything away from safe?"],
    ["Sweep the gate", "Sweep the gate from 0 to 5 V, record the lock-in signal, and tell me where it peaks."],
    ["Measure Rabi oscillations", "Find the NV resonance with CW ODMR, then measure Rabi oscillations to get the π time."],
    ["Plan an experiment", "Plan how to measure the field dependence of the resistance at base temperature."],
  ];

  // ------------------------------------------------------------------ start
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
    applyZoom();   // this person's text size (desktop window)
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
    $("activity-all").addEventListener("click", () => {   // the lab's instrument log: everyone's activity, by name
      PANEL.everyone = !PANEL.everyone;
      $("activity-all").setAttribute("aria-pressed", String(PANEL.everyone));
      pollState();
    });
    S.chats = boot.chats;
    S.projects = boot.projects || [];
    $("lab-name").textContent = boot.lab;
    $("sim-tag").hidden = !boot.simulated;
    S.labSub = boot.lab + (boot.simulated ? " (simulated)" : "") + " · Claude, GPT, Gemini or a local model";
    $("empty-sub").textContent = S.labSub;
    $("toggle-incognito").addEventListener("click", toggleIncognito);
    applyIncognito();
    window.addEventListener("pagehide", () => {   // closing the page also forgets an incognito chat
      if (S.incognito && S.chatId) fetch("/api/chats/" + S.chatId, { method: "DELETE", keepalive: true, headers: { "X-Kelvin-Token": token } });
    });
    $("suggestions").replaceChildren(...SUGGESTIONS.map(([title, text]) => {
      const b = h("button", { class: "suggestion", type: "button" }, h("b", {}, title), h("span", {}, text));
      b.addEventListener("click", () => sendMessage(text));
      return b;
    }));
    applyModels(boot.models.groups, boot.default_model, boot.auto_model);
    initRunsPane();
    fitLayout();
    initTerminal();
    renderChats();
    showEmpty(true);
    loadNewFolder();
    loadStartChecklist();

    $("open-help").addEventListener("click", () => openHelp());
    $("help-go").addEventListener("click", helpSearch);
    $("help-q").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); helpSearch(); } });
    $("eln-day-go").addEventListener("click", () => fetchDownload("/api/eln?day=" + encodeURIComponent($("eln-day").value || "today"),
      ($("eln-day").value || "today") + "-lab-record.zip", "day's lab notebook record"));
    $("owner-pin-set").addEventListener("click", () => ownerPinChange(false));
    $("owner-pin-remove").addEventListener("click", () => ownerPinChange(true));
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
    $("phone-on").addEventListener("change", switchPhone);
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
    $("data-choose").addEventListener("click", async () => {
      const path = await pickFolder($("data-path").value.trim());
      if (path) { $("data-path").value = path; dataPathChanged(); }
    });
    $("data-open").addEventListener("click", () => api("/api/data-folder", { body: { open: true } }).catch((e) => showDataFolder(e.message, true)));
    $("pair-start").addEventListener("click", startPairing);
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
    $("relay-on").addEventListener("change", setRelay);
    $("protect-add-gate").addEventListener("click", () => $("protect-gates").append(gateRow({ dielectric: "hBN",
      drive: (PROT.opts.drives.find((d) => d.gate) || {}).key, leak: (PROT.opts.drives.find((d) => d.gate) || {}).leak })));
    $("protect-add-limit").addEventListener("click", () => $("protect-limits").append(limitRow((PROT.opts.drives[0] || {}).key)));
    $("protect-save").addEventListener("click", () => saveProtection(false));
    $("protect-clear").addEventListener("click", () => saveProtection(true));
    $("hands-on").addEventListener("click", handsOn);
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
    $("hands-done-panel").addEventListener("click", openUnlock);
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
    $("open-queue").addEventListener("click", () => (QUEUE.open ? hideQueue() : showQueue()));
    $("make-report").addEventListener("click", makeReport);
    $("watch-add-toggle").addEventListener("click", () => { $("watch-form").hidden = !$("watch-form").hidden; });
    $("watch-form").addEventListener("submit", addWatch);
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
    $("setup-scripts-choose").addEventListener("click", () => answerScripts(false));
    $("setup-scripts-none").addEventListener("click", () => answerScripts(true));
    $("setup-wait").addEventListener("close", () => { WAIT.closed = true; });
    $("lab-add-scripts").addEventListener("click", addScripts);
    $("lab-recipes").addEventListener("click", recipesFromScripts);
    $("lab-add").addEventListener("click", addByAddress);
    $("lab-save").addEventListener("click", () => connectLab({ yaml: $("lab-yaml").value }));
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
    document.querySelector(".lab-chip").addEventListener("click", () => toggleLab(true));
    for (const b of document.querySelectorAll("#lab-seg button")) {
      b.addEventListener("click", () => {
        if (b.classList.contains("on")) return;
        if (b.dataset.use === "mine" && !$("lab-yaml").value.trim()) { $("lab-msg").textContent = "Set up your instruments first: scan, or add one by address."; return; }
        connectLab({ use: b.dataset.use });
      });
    }
    $("new-chat").addEventListener("click", () => newChat(null, true));
    $("folder-btn").addEventListener("click", (e) => { e.stopPropagation(); folderMenu(); });
    $("toggle-sidebar").addEventListener("click", () => { $("app").classList.toggle("side-open"); syncScrim(); });
    $("scrim").addEventListener("click", () => { closeSidebar(); if (S.labOpen) toggleLab(false); if (RUNS.open) toggleRuns(false); });
    $("toggle-lab").addEventListener("click", () => toggleLab());
    $("inst-filter").addEventListener("input", (e) => { PANEL.filter = e.target.value; renderInstruments(); });
    for (const b of document.querySelectorAll("#inst-seg button")) {
      b.addEventListener("click", () => {
        PANEL.activeOnly = b.dataset.v === "active";
        store("lh.activeOnly", PANEL.activeOnly ? "1" : "0");
        renderInstruments();
      });
    }
    $("close-lab").addEventListener("click", () => toggleLab(false));
    $("open-settings").addEventListener("click", openSettings);
    for (const b of document.querySelectorAll(".set-nav button")) b.addEventListener("click", () => settingsPage(b.dataset.page));
    zoomKeys();
    $("save-settings").addEventListener("click", saveSettings);
    $("save-project").addEventListener("click", saveProjectSettings);
    $("quit-app").hidden = !boot.can_quit;
    $("quit-app").addEventListener("click", async () => {
      if (!confirm("Quit Kelvin? The lab keeps its current settings.")) return;
      try { await api("/api/quit", { body: {} }); } catch (e) { /* already gone */ }
      document.body.replaceChildren(h("div", { class: "nokey" }, h("h1", {}, "Kelvin has quit"), h("p", {}, "You can close this tab.")));
    });
    for (const b of document.querySelectorAll("[data-clear]")) {
      b.addEventListener("click", () => {
        const input = document.querySelector('#settings-form input[name="' + b.dataset.clear + '"]');
        input.value = ""; input.dataset.clear = "1"; input.placeholder = "Will be removed when you save";
      });
    }
    window.addEventListener("resize", syncScrim);
    setInterval(() => { if (S.labOpen || S.live.size || S.answering.size || !$("lab-badge").hidden) pollState(); }, 1500);
    setInterval(pollState, 4000);   // often enough that an alarm shows within seconds
    pollState();
    $("input").focus();
  }
  init();
})();
