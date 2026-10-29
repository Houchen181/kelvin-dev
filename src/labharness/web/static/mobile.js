// Kelvin on a phone: pairing, then alarms, approvals, the sample (with the hands-on lock), the run in progress (with
// Stop) and the instruments.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const KEY = "kelvin.device";
  let token = null;
  try { token = localStorage.getItem(KEY); } catch (e) { /* private mode: pair again next time */ }
  let me = null, timer = 0;
  const shown = {};   // what each section shows now; it is redrawn only when that changes, so a tap is never lost
  const changed = (key, data) => { const sig = JSON.stringify(data); if (shown[key] === sig) return false; shown[key] = sig; return true; };

  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids) if (kid != null) node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return node;
  }
  async function api(path, body) {
    const res = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: Object.assign({ "Content-Type": "application/json" }, token ? { "X-Kelvin-Token": token } : {}),
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (res.status === 401 && token && data && data.signin) throw new Error(data.error);   // still paired; its person is away
    if (res.status === 401 && token) { forget(); throw new Error("This phone is no longer paired."); }
    if (!res.ok) throw new Error((data && data.error) || "HTTP " + res.status);
    return data;
  }
  function forget() {
    token = null;
    for (const k of Object.keys(shown)) delete shown[k];
    try { localStorage.removeItem(KEY); } catch (e) { /* nothing kept */ }
    clearInterval(timer);
    showPair();
  }
  function showPair() {
    $("dash").hidden = true;
    $("pair").hidden = false;
    $("sub").textContent = "not paired";
    const code = (location.hash.match(/^#(\d{6})(?:&|$)/) || [])[1];   // the app's QR adds &also=<other addresses>
    if (code) $("code").value = code;
    if (location.hash) history.replaceState(null, "", location.pathname);
  }
  async function pair(e) {
    e.preventDefault();
    $("pair-msg").textContent = "";
    try {
      const r = await api("/api/pair", { code: $("code").value.trim() });
      token = r.token;
      try { localStorage.setItem(KEY, token); } catch (err) { /* works until the page closes */ }
      start();
    } catch (err) { $("pair-msg").textContent = err.message; }
  }

  async function start() {
    $("pair").hidden = true;
    $("dash").hidden = false;
    try { me = await api("/api/me"); } catch (e) { if (!token) return; }
    if (me) {
      $("who").textContent = (me.name || "This phone") + " · " + (me.role === "watch" ? "can watch, stop and decline" : "can chat and approve");
      $("full").hidden = me.role !== "control";
      $("full").href = "/#" + token;
    }
    poll();
    clearInterval(timer);
    timer = setInterval(poll, 3000);
  }
  async function poll() {
    let s;
    try { s = await api("/api/state"); } catch (e) { $("conn").className = "dot bad"; $("sub").textContent = e.message; return; }
    $("conn").className = "dot ok";
    $("lab").textContent = s.lab;
    $("sub").textContent = (s.simulated ? "simulated · " : "") + (s.busy ? "Kelvin is " + s.busy : "Kelvin is idle");
    render(s);
  }

  function act(label, cls, run) {
    const b = h("button", { class: cls, type: "button" }, label);
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await run(); } catch (e) { alert(e.message); }
      b.disabled = false;
      poll();
    });
    return b;
  }
  function render(s) {
    const alarms = (s.alarms || []).filter((a) => !a.cleared && !a.acknowledged);
    if (changed("alarms", alarms)) $("alarms").replaceChildren(...alarms.map((a) => h("div", { class: "card alarm" },
      h("b", {}, "Alarm: " + a.text),
      a.actions && a.actions.length ? h("p", { class: "muted" }, "Kelvin " + a.actions.join("; ")) : null,
      h("div", { class: "row" }, act("Got it", "", () => api("/api/alarms/" + a.id + "/ack", {}))))));

    const pending = (s.pending || []).map((r) => Object.assign({}, r, { seconds_left: Math.ceil(r.seconds_left / 30) * 30 }));
    // a card in the language its person reads cards in (r.shown, cardlang.py); the English is the card itself
    if (changed("approvals", pending)) $("approvals").replaceChildren(...pending.map((r) => h("div", { class: "card ask" },
      h("b", {}, ((r.shown || r).where ? "[" + (r.shown || r).where + "] " : "") + (r.shown || r).title),
      h("ul", {}, ...(r.shown || r).items.map((i) => h("li", {}, i))),
      r.shown ? h("details", { class: "muted" }, h("summary", {}, "English"), h("p", {}, r.title),
        h("ul", {}, ...r.items.map((i) => h("li", {}, i)))) : null,
      h("p", { class: "muted" }, "about " + (r.seconds_left > 90 ? Math.round(r.seconds_left / 60) + " min" : r.seconds_left + " s") + " left to answer"),
      h("div", { class: "row" },
        me && me.role !== "watch" ? act("Approve", "primary", async () => {
          if (confirm("Approve: " + r.title + "?")) await api("/api/approvals/" + r.id, { approve: true });
        }) : null,
        act("Decline", "", () => api("/api/approvals/" + r.id, { approve: false }))))));

    const m = s.mounted || null, hands = s.hands_on || null;
    if (changed("sample", [m && m.device_name, m && m.protection_lines, hands])) {
      const rows = [h("h2", {}, m ? "Sample: " + m.device_name + " (" + m.project_name + ")" : "No sample mounted")];
      if (m) rows.push(...((m.protection_lines || []).length ? m.protection_lines.map((l) => h("p", { class: "muted" }, l))
        : [h("p", { class: "muted" }, "Not protected: only the lab's limits hold.")]));
      if (hands) {
        const who = String(hands.by || "").replace(/^human \((phone )?(.*)\)$/, "$2").replace(/^portal$/, "the Kelvin computer");
        rows.push(h("p", { class: "lock" }, "Hands on the sample: locked at " + String(hands.since || "").slice(11, 16) + " from " + who),
          h("p", { class: "muted" }, "Everything that reaches the sample stays at safe until a person unlocks."),
          me && me.role !== "watch" ? h("div", { class: "row" }, act("Done: unlock", "primary", async () => {
            if (confirm("Done with the sample? Check that it is wired as the next measurement needs and that nobody is touching the setup.")) await api("/api/hands", { on: false });
          })) : h("p", { class: "muted" }, "Unlock on the Kelvin computer."));
      } else {
        rows.push(h("div", { class: "row" }, act("Hands on the sample", "", async () => {   // at once: no dialog on the stop path
          await api("/api/hands", { on: true });
        })));
      }
      $("sample").className = "card" + (hands ? " locked" : "");
      $("sample").replaceChildren(...rows);
    }

    const live = s.live;
    const running = live && live.status === "running";
    if (changed("run", running ? [live.run_id] : live)) {   // the card is built once per run; progress updates in place
      if (running) {
        $("run").replaceChildren(h("h2", {}, "Running " + live.plan),
          h("div", { class: "bar" }, h("i", { id: "run-bar" })),
          h("div", { class: "row" }, h("span", { class: "grow muted", id: "run-info" }),
            act("Stop", "danger", async () => {   // at once: no dialog on the stop path
              await api("/api/runs/stop", {});
            })));
      } else {
        $("run").replaceChildren(h("h2", {}, "No run in progress"),
          ...(live ? [h("p", { class: "muted" }, "Last: " + live.plan + " (" + live.status + ", " + live.done + " of " + live.total + " points)")] : []));
      }
    }
    if (running) {
      const pct = live.total ? Math.round(100 * live.done / live.total) : 0;
      const eta = live.eta_s ? " · about " + (live.eta_s > 90 ? Math.round(live.eta_s / 60) + " min" : Math.round(live.eta_s) + " s") + " left" : "";
      $("run-bar").style.width = pct + "%";
      $("run-info").textContent = live.done + " of " + live.total + " points" + eta;
    }

    if (changed("instruments", s.status)) $("instruments").replaceChildren(...Object.entries(s.status || {}).map(([name, params]) => h("li", {},
      h("b", {}, name + " "), ...params.slice(0, 5).flatMap((p, i) => [i ? ", " : "",
        h("span", { class: p.away ? "away" : "" }, p.name + " " + p.value)]))));

    const notices = (s.notices || []).slice(-6).reverse();
    if (changed("notices", notices)) $("notices").replaceChildren(...(notices.length ? notices.map((n) => h("li", {},
      h("span", { class: "muted" }, new Date(n.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + " "), n.title))
      : [h("li", { class: "muted" }, "Nothing yet.")]));
  }

  $("pair-form").addEventListener("submit", pair);
  $("forget").addEventListener("click", async () => {
    if (!confirm("Unpair this phone? You will need a new pairing code to use it again.")) return;
    try { await api("/api/me/forget", {}); } catch (e) { /* already gone */ }
    forget();
  });
  if (token) start(); else showPair();
})();
