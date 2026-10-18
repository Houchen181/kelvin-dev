"use strict";
(() => {
  const token = decodeURIComponent(location.hash.slice(1));
  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  const S = { chats: [], chatId: null, groups: [], labels: {}, streaming: false, labOpen: false, settings: null, approvals: new Map() };

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
    chev: '<path d="M9 6l6 6-6 6"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    hand: '<path d="M12 22c4.4 0 8-3.6 8-8V9a2 2 0 0 0-4 0V5a2 2 0 0 0-4 0v-1a2 2 0 0 0-4 0v9l-1.6-1.6a2 2 0 0 0-2.9 2.8L8 18c1 2.4 2.4 4 4 4z"/>',
    flask: '<path d="M9 3h6M10.5 3v6.2L5.2 18.3A1.8 1.8 0 0 0 6.8 21h10.4a1.8 1.8 0 0 0 1.6-2.7L13.5 9.2V3"/><path d="M7.5 15h9"/>',
  };
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
      headers: { "X-LabHarness-Token": token, "Content-Type": "application/json" },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      let msg = "HTTP " + res.status;
      try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
      throw new Error(msg);
    }
    return res.json();
  }

  let toastTimer = null;
  function toast(text) {
    let t = document.querySelector(".toast");
    if (!t) { t = h("div", { class: "toast", role: "status" }); document.body.append(t); }
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 4000);
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
    const xs = series.flatMap((s) => s.points.map((p) => p.x));
    const ys = series.flatMap((s) => s.points.map((p) => p.y));
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
    const H = 200, M = { l: 48, r: series.length <= 4 ? 62 : 14, t: 10, b: 26 };
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
    series.forEach((s, k) => {
      const color = series.length > 1 ? "var(--s" + (k + 1) + ")" : "var(--series-1)";
      const d = s.points.map((p, i) => (i ? "L" : "M") + sx(p.x).toFixed(1) + "," + sy(p.y).toFixed(1)).join("");
      root.append(svg("path", { class: "line", d, style: "stroke:" + color }));
      const last = s.points[s.points.length - 1];
      root.append(svg("circle", { class: "end", cx: sx(last.x), cy: sy(last.y), r: 4, style: "fill:" + color }));
      if (series.length <= 4) {
        const label = svg("text", { class: "endlabel", x: sx(last.x) + 8, y: sy(last.y) + 4 });
        label.textContent = series.length > 1 ? s.name : fmt(last.y, yUnit);
        root.append(label);
      }
    });
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

  async function renderRun(runId, anchor) {
    const block = h("div", { class: "plots-block" });
    anchor.after(block);
    let data;
    try { data = await api("/api/runs/" + encodeURIComponent(runId)); } catch (e) { return; }
    const grid = h("div", { class: "plots" });
    const axes = data.axes;
    if (axes.length && data.rows.length > 1) {
      const xa = axes[axes.length - 1];
      const outer = axes.length > 1 ? axes[0] : null;
      for (const sc of data.scalars) {
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
        grid.append(lineChart({ title: sc.key, series, xKey: xa.key, xUnit: xa.unit, yUnit: sc.unit }));
      }
    }
    for (const tr of data.traces) {
      grid.append(lineChart({ title: tr.key + (data.rows.length > 1 ? " (last row)" : ""), xKey: "x", xUnit: tr.x_unit, yUnit: tr.unit,
        series: [{ name: tr.key, points: tr.x.map((x, i) => ({ x, y: tr.y[i] })) }] }));
    }
    if (!grid.children.length) return;
    const table = h("div", { class: "table-wrap", hidden: true });
    const toggle = h("button", { class: "link", type: "button" }, "Show table");
    toggle.addEventListener("click", () => {
      if (!table.children.length) table.append(runTable(data));
      table.hidden = !table.hidden;
      toggle.textContent = table.hidden ? "Show table" : "Hide table";
    });
    block.append(grid, h("div", { class: "plots-foot" }, h("span", {}, "Run " + runId + " · " + data.rows.length + " points"), toggle), table);
  }

  function runTable(data) {
    const cols = [...data.axes, ...data.scalars];
    const head = h("tr", {}, ...cols.map((c) => h("th", {}, c.key + (c.unit ? " (" + c.unit + ")" : ""))));
    const rows = data.rows.slice(0, 500).map((r) => h("tr", {}, ...cols.map((c) =>
      h("td", {}, typeof r[c.key] === "number" ? Number(r[c.key].toPrecision(6)).toString() : String(r[c.key])))));
    return h("table", {}, h("thead", {}, head), h("tbody", {}, ...rows));
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
      case "get_value": return v("Reading", "Read") + " " + code(a.instrument + "." + a.parameter);
      case "set_value": return v("Setting", "Set") + " " + code(a.instrument + "." + a.parameter) + " → " + esc(String(a.value));
      case "send_raw": return v("Sending", "Sent") + " " + code(a.command) + " to " + esc(a.instrument);
      case "undo_last": return v("Undoing", "Undid") + " the last change";
      case "safe_state": return v("Sending", "Sent") + " every drive to safe";
      case "health_check": return v("Running", "Ran") + " health checks";
      case "validate_plan": return v("Checking", "Checked") + " plan " + code(planName(a));
      case "simulate_plan": return v("Dry-running", "Dry-ran") + " plan " + code(planName(a));
      case "run_plan": return v("Running", "Ran") + " plan " + code(planName(a));
      case "list_runs": return v("Listing", "Listed") + " recent runs";
      case "get_run_data": return v("Reading", "Read") + " the data of run " + code(a.run_id);
      case "get_trace": return v("Reading", "Read") + " a trace from run " + code(a.run_id);
      case "recent_events": return v("Reading", "Read") + " the event log";
      case "wait": return v("Waiting", "Waited") + " " + esc(String(a.seconds)) + " s";
      default: return code(name);
    }
  }
  function summaryOf(content) {
    const lines = String(content || "").split("\n").filter((l) => l.trim());
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
  function makeApproval(ev) {
    const verdict = h("span", { class: "verdict" });
    const count = h("span", { class: "muted small" });
    const yes = h("button", { class: "btn primary", type: "button" }, "Approve");
    const no = h("button", { class: "btn", type: "button" }, "Decline");
    const actions = h("div", { class: "approval-actions" }, yes, no, count);
    const el = h("div", { class: "approval", role: "group", "aria-label": ev.title },
      h("div", { class: "approval-title" }, icon("hand"), h("span", {}, ev.title)),
      h("ul", {}, ...ev.items.map((i) => h("li", {}, i))), actions);
    let left = ev.seconds_left;
    const tick = () => { count.textContent = left > 0 ? "Declines itself in " + left + " s" : ""; left -= 1; };
    tick();
    const timer = setInterval(tick, 1000);
    const card = {
      el, decided: false,
      close(text, good) {
        clearInterval(timer);
        if (!card.decided) { verdict.textContent = text; verdict.className = "verdict " + (good ? "yes" : "no"); }
        actions.replaceChildren(verdict);
        el.classList.add("done");
        card.decided = true;
        S.approvals.delete(ev.id);
        refreshTitle();
      },
    };
    async function answer(approve) {
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
    document.title = (n ? "(" + n + ") Approval needed · " : "") + "Lab Harness";
  }

  // ------------------------------------------------------------------ one assistant reply
  class Reply {
    constructor(modelKey) {
      this.el = h("div", { class: "msg assistant" });
      this.who = h("span", {}, "Lab assistant");
      this.el.append(h("div", { class: "who" }, h("span", { class: "logo" }, icon("flask")), this.who));
      this.text = null; this.buf = ""; this.raf = 0; this.thinking = null; this.steps = new Map();
      this.setModel(modelKey);
    }
    setModel(key) { if (key) this.who.textContent = "Lab assistant · " + (S.labels[key] || key.split(":").pop()); }
    addText(delta) {
      if (!this.text) { this.endThinking(); this.text = h("div", { class: "md cursor" }); this.buf = ""; this.el.append(this.text); }
      this.buf += delta;
      if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; if (this.text) this.text.innerHTML = md(this.buf); });
    }
    resetText() { if (this.text) { this.text.remove(); this.text = null; this.buf = ""; } }
    endText() {
      if (this.text) { cancelAnimationFrame(this.raf); this.raf = 0; this.text.innerHTML = md(this.buf); this.text.classList.remove("cursor"); }
      this.text = null;
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
    toolEnd(id, content, isError, runId) {
      const step = this.steps.get(id);
      if (!step) return;
      step.finish(content, isError);
      step.done = true;
      if (runId) renderRun(runId, step.last || step.el);
    }
    approval(ev) {
      const card = makeApproval(ev);
      const step = this.steps.get(ev.call_id);
      if (step) { (step.last || step.el).after(card.el); step.last = card.el; } else this.el.append(card.el);
    }
    note(text) { this.endText(); this.el.append(h("div", { class: "note" }, text)); }
    error(text) { this.endText(); this.el.append(h("div", { class: "callout" }, text)); }
    finish() {
      this.endText(); this.endThinking();
      for (const step of this.steps.values()) if (!step.done && step.unknown) { step.unknown(); step.done = true; }
    }
  }

  // ------------------------------------------------------------------ thread
  const thread = $("thread");
  const scroller = $("scroller");
  const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
  const toBottom = () => { scroller.scrollTop = scroller.scrollHeight; };

  function userBubble(text) { return h("div", { class: "msg user" }, h("div", { class: "bubble" }, text)); }
  function showEmpty(show) { $("empty").hidden = !show; thread.hidden = show; }

  function renderChat(chat) {
    thread.replaceChildren();
    let reply = null;
    for (const m of chat.messages) {
      if (m.role === "user") {
        if (reply) reply.finish();
        reply = null;
        thread.append(userBubble(m.text));
      } else if (m.role === "assistant") {
        if (!reply) { reply = new Reply(m.provider + ":" + m.model); thread.append(reply.el); }
        for (const b of m.blocks) {
          if (b.type === "text") { reply.addText(b.text); reply.endText(); }
          else if (b.type === "thinking") { reply.addThinking(b.text); reply.endThinking(); }
          else if (b.type === "tool_call") reply.toolStart(b.id, b.name, b.input);
        }
      } else if (m.role === "tool" && reply) {
        for (const r of m.results) {
          const run = r.name === "run_plan" ? (String(r.content).match(/run_id='([^']+)'/) || [])[1] : null;
          reply.toolEnd(r.id, r.content, r.is_error, run);
        }
      }
    }
    if (reply) reply.finish();
    showEmpty(!chat.messages.length);
    requestAnimationFrame(toBottom);
  }

  // ------------------------------------------------------------------ chats sidebar
  function renderChats() {
    const list = $("chat-list");
    list.replaceChildren(...S.chats.map((c) => {
      const del = h("button", { class: "del", type: "button", "aria-label": "Delete conversation" }, icon("trash"));
      const item = h("div", { class: "chat-item" + (c.id === S.chatId ? " active" : ""), role: "button", tabindex: "0" },
        h("span", { class: "t" }, c.title || "New chat"), del);
      item.addEventListener("click", () => openChat(c.id));
      item.addEventListener("keydown", (e) => { if (e.key === "Enter") openChat(c.id); });
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("Delete “" + (c.title || "New chat") + "”? The lab's event log and run data are kept.")) return;
        try { await api("/api/chats/" + c.id, { method: "DELETE" }); } catch (err) { toast(err.message); return; }
        if (S.chatId === c.id) newChat();
        await refreshChats();
      });
      return item;
    }));
  }
  async function refreshChats() {
    try { S.chats = await api("/api/chats"); } catch (e) { return; }
    renderChats();
    const current = S.chats.find((c) => c.id === S.chatId);
    $("chat-title").textContent = current ? current.title : "";
  }
  async function openChat(id) {
    if (S.streaming) { toast("Wait for the current answer, or press stop."); return; }
    let chat;
    try { chat = await api("/api/chats/" + id); } catch (e) { toast(e.message); return; }
    S.chatId = id;
    renderChat(chat);
    $("chat-title").textContent = chat.title;
    if (chat.model && S.labels[chat.model]) $("model").value = chat.model;
    renderChats();
    closeSidebar();
  }
  function newChat() {
    if (S.streaming) { toast("Wait for the current answer, or press stop."); return; }
    S.chatId = null;
    thread.replaceChildren();
    showEmpty(true);
    $("chat-title").textContent = "";
    renderChats();
    closeSidebar();
    $("input").focus();
  }

  // ------------------------------------------------------------------ sending
  function setStreaming(on) {
    S.streaming = on;
    const send = $("send");
    send.classList.toggle("stop", on);
    send.setAttribute("aria-label", on ? "Stop" : "Send");
    updateSend();
  }
  function updateSend() {
    const send = $("send");
    send.disabled = !S.streaming && (!$("input").value.trim() || !$("model").value);
  }

  async function sendMessage(text) {
    text = text.trim();
    if (!text || S.streaming) return;
    const model = $("model").value;
    if (!model) { openSettings(); return; }
    store("lh.model", model);
    if (!S.chatId) {
      try { S.chatId = (await api("/api/chats", { body: { model } })).id; } catch (e) { toast(e.message); return; }
    }
    $("input").value = ""; autosize();
    showEmpty(false);
    thread.append(userBubble(text));
    const reply = new Reply(model);
    thread.append(reply.el);
    toBottom();
    setStreaming(true);
    try {
      const res = await fetch("/api/chats/" + S.chatId + "/turn", {
        method: "POST",
        headers: { "X-LabHarness-Token": token, "Content-Type": "application/json" },
        body: JSON.stringify({ text, model }),
      });
      if (!res.ok) {
        let msg = "HTTP " + res.status;
        try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
        reply.error(msg);
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
          if (line.trim()) handle(reply, JSON.parse(line));
        }
      }
    } catch (e) {
      reply.error("Lost the connection to the portal (" + e.message + "). The lab keeps its own log.");
    } finally {
      reply.finish();
      setStreaming(false);
      refreshChats();
      pollState();
    }
  }

  function handle(reply, ev) {
    const stick = nearBottom();
    switch (ev.type) {
      case "turn_start": reply.setModel(ev.model); $("chat-title").textContent = ev.title; refreshChats(); break;
      case "text": reply.addText(ev.delta); break;
      case "thinking": reply.addThinking(ev.delta); break;
      case "reset": reply.resetText(); break;
      case "tool_start": reply.toolStart(ev.id, ev.name, ev.input); break;
      case "approval": reply.approval(ev); break;
      case "approval_closed": { const card = S.approvals.get(ev.id); if (card) card.close("Closed", false); break; }
      case "tool_end": reply.toolEnd(ev.id, ev.content, ev.is_error, ev.run_id); pollState(); break;
      case "note": reply.note(ev.text); break;
      case "error": reply.error(ev.message); break;
      default: break;
    }
    if (stick) toBottom();
  }

  // ------------------------------------------------------------------ lab panel
  async function pollState() {
    let s;
    try { s = await api("/api/state"); } catch (e) { return; }
    $("agent-state").textContent = s.busy ? "· " + s.busy : "· idle";
    $("lab-status").replaceChildren(...Object.entries(s.status).map(([name, params]) =>
      h("div", { class: "inst" }, h("div", { class: "inst-name" }, name),
        h("div", { class: "chips" }, ...params.map((p) => h("span", { class: "chip" + (p.away ? " away" : "") }, p.name + " " + p.value))))));
    $("lab-events").replaceChildren(...s.events.map((e) => h("li", {}, e)));
    // requests not already shown in a conversation (e.g. after a page reload)
    const loose = s.pending.filter((p) => !S.approvals.has(p.id));
    $("lab-pending").replaceChildren(...loose.map((p) => makeApproval(p).el));
    const badge = $("lab-badge");
    badge.textContent = String(s.pending.length);
    badge.hidden = !s.pending.length;
    if (loose.length && !S.labOpen) toggleLab(true);
  }
  function toggleLab(open) {
    S.labOpen = open === undefined ? !S.labOpen : open;
    $("labpanel").hidden = !S.labOpen;
    syncScrim();
    if (S.labOpen) pollState();
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
  function applyModels(groups, preferred) {
    S.groups = groups;
    S.labels = {};
    for (const g of groups) for (const m of g.models) S.labels[m.key] = m.label;
    fillModels($("model"), groups, store("lh.model") || preferred);
    if (!$("model").value) $("input").placeholder = "Add an API key in Settings to start";
    else $("input").placeholder = "Ask the lab assistant…";
    updateSend();
  }
  function openSettings() {
    const keys = (S.settings && S.settings.keys) || {};
    for (const hint of document.querySelectorAll("[data-hint]")) {
      const src = keys[hint.dataset.hint];
      hint.textContent = src ? "Set (from " + src + ")" : "Not set";
      hint.classList.toggle("set", !!src);
    }
    for (const input of document.querySelectorAll("#settings-form input")) { input.value = ""; delete input.dataset.clear; }
    $("local-url").value = (S.settings && S.settings.local_base_url) || "";
    $("local-models").value = (S.settings && S.settings.local_models) || "";
    fillModels($("default-model"), S.groups, (S.settings && S.settings.default_model) || "", true);
    $("settings-msg").textContent = "";
    $("settings-dialog").showModal();
  }
  async function saveSettings() {
    const body = {};
    for (const input of document.querySelectorAll("#settings-form input[type=password]")) {
      if (input.dataset.clear) body[input.name] = "";
      else if (input.value.trim()) body[input.name] = input.value.trim();
    }
    body.local_base_url = $("local-url").value.trim();
    body.local_models = $("local-models").value.trim();
    body.default_model = $("default-model").value;
    $("settings-msg").textContent = "Saving…";
    try {
      const r = await api("/api/settings", { body });
      S.settings = r.settings;
      applyModels(r.models.groups, r.settings.default_model);
      $("settings-dialog").close();
      toast("Settings saved.");
    } catch (e) {
      $("settings-msg").textContent = e.message;
    }
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
    $("scrim").hidden = !($("app").classList.contains("side-open") || (S.labOpen && narrow));
  }

  const SUGGESTIONS = [
    ["Check the lab", "What is on, and is anything away from safe?"],
    ["Sweep the gate", "Sweep the gate from 0 to 5 V, record the lock-in signal, and tell me where it peaks."],
    ["Take an RF trace", "Turn on the VNA at −20 dBm and record S21 around 4 GHz."],
    ["Plan an experiment", "Plan how to measure the field dependence of the resistance at base temperature."],
  ];

  // ------------------------------------------------------------------ start
  async function init() {
    if (!token) { $("nokey").hidden = false; return; }
    let boot;
    try { boot = await api("/api/bootstrap"); } catch (e) {
      $("nokey").hidden = false;
      $("nokey").querySelector("p").textContent = "The portal did not accept this link (" + e.message + "). Open the link the server printed.";
      return;
    }
    $("app").hidden = false;
    S.settings = boot.settings;
    S.chats = boot.chats;
    $("lab-name").textContent = boot.lab;
    $("sim-tag").hidden = !boot.simulated;
    $("empty-sub").textContent = boot.lab + (boot.simulated ? " (simulated)" : "") + " · Claude, GPT, Gemini or a local model";
    $("suggestions").replaceChildren(...SUGGESTIONS.map(([title, text]) => {
      const b = h("button", { class: "suggestion", type: "button" }, h("b", {}, title), h("span", {}, text));
      b.addEventListener("click", () => sendMessage(text));
      return b;
    }));
    applyModels(boot.models.groups, boot.default_model);
    renderChats();
    showEmpty(true);

    $("composer").addEventListener("submit", (e) => {
      e.preventDefault();
      if (S.streaming) api("/api/stop", { body: {} }).catch(() => {});
      else sendMessage($("input").value);
    });
    $("input").addEventListener("input", autosize);
    $("input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!S.streaming) sendMessage($("input").value); }
    });
    $("model").addEventListener("change", () => { store("lh.model", $("model").value); updateSend(); });
    $("new-chat").addEventListener("click", newChat);
    $("toggle-sidebar").addEventListener("click", () => { $("app").classList.toggle("side-open"); syncScrim(); });
    $("scrim").addEventListener("click", () => { closeSidebar(); if (S.labOpen) toggleLab(false); });
    $("toggle-lab").addEventListener("click", () => toggleLab());
    $("close-lab").addEventListener("click", () => toggleLab(false));
    $("open-settings").addEventListener("click", openSettings);
    $("save-settings").addEventListener("click", saveSettings);
    for (const b of document.querySelectorAll("[data-clear]")) {
      b.addEventListener("click", () => {
        const input = document.querySelector('#settings-form input[name="' + b.dataset.clear + '"]');
        input.value = ""; input.dataset.clear = "1"; input.placeholder = "Will be removed when you save";
      });
    }
    $("kill").addEventListener("click", async () => {
      if (!confirm("Send every drive to its safe value? This stops the assistant and declines open requests.")) return;
      try {
        const r = await api("/api/safe_state", { body: {} });
        toast(r.results.length ? "Safe state: " + r.results.length + " change(s) made." : "Everything was already safe.");
      } catch (e) { toast("Kill switch failed: " + e.message); }
      pollState();
    });
    window.addEventListener("resize", syncScrim);
    setInterval(() => { if (S.labOpen || S.streaming || !$("lab-badge").hidden) pollState(); }, 1500);
    setInterval(pollState, 10000);
    pollState();
    $("input").focus();
  }
  init();
})();
