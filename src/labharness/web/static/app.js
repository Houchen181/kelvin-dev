"use strict";
(() => {
  const token = decodeURIComponent(location.hash.slice(1));
  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  const S = { compare: [], lastReply: null, chats: [], projects: [], incognito: false, chatId: null, groups: [], labels: {}, streaming: false, labOpen: false, settings: null, approvals: new Map() };

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
    incognito: '<path d="M3 11h18"/><path d="M6 11l1.6-5.2h8.8L18 11"/><circle cx="7.5" cy="16" r="2.6"/><circle cx="16.5" cy="16" r="2.6"/><path d="M10.1 16h3.8"/>',
    memory: '<path d="M7 3h10a1 1 0 0 1 1 1v17l-6-4-6 4V4a1 1 0 0 1 1-1z"/>',
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    dots: '<circle cx="5" cy="12" r="1.1"/><circle cx="12" cy="12" r="1.1"/><circle cx="19" cy="12" r="1.1"/>',
    folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z"/>',
    pencil: '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    hand: '<path d="M12 22c4.4 0 8-3.6 8-8V9a2 2 0 0 0-4 0V5a2 2 0 0 0-4 0v-1a2 2 0 0 0-4 0v9l-1.6-1.6a2 2 0 0 0-2.9 2.8L8 18c1 2.4 2.4 4 4 4z"/>',
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
      grid.append(lineChart({ title: tr.key + (data.rows.length > 1 ? " (latest)" : ""), xKey: tr.x_unit === "Hz" ? "frequency" : "x",
        xUnit: tr.x_unit, yUnit: tr.unit, fit: fitFor(data.analysis, tr.key, true),
        series: [{ name: tr.key, points: tr.x.map((x, i) => ({ x, y: tr.y[i] })) }] }));
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
      if (next) selectRun(next); else $("run-body").querySelector(".pane-empty").hidden = false;
    }
    $("runs-dot").hidden = ![...RUNS.tabs.values()].some((t) => t.status === "running");
  }
  function toggleRuns(open) {
    RUNS.open = open === undefined ? !RUNS.open : open;
    $("runpane").hidden = !RUNS.open;
    $("toggle-runs").setAttribute("aria-pressed", String(RUNS.open));
    $("toggle-runs").classList.toggle("on", RUNS.open);
    syncScrim();
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
    v.stop.addEventListener("click", async () => {
      if (!confirm("Stop this run? The drives it touched go back to safe (or its on_abort steps run).")) return;
      try { await api("/api/runs/stop", { body: {} }); } catch (e) { toast(e.message); }
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
    v.plan = h("details", { class: "rv-plan", hidden: true }, h("summary", {}, "Plan"), h("pre", {}));
    v.foot = h("div", { class: "plots-foot" }, h("span", {}, tab.runId), v.tableBtn);
    tab.body.replaceChildren(
      h("div", { class: "rv-head" }, v.state, h("code", { class: "rv-plan-name" }, tab.plan), h("div", { class: "spacer" }), v.stop),
      h("div", { class: "rv-progress" }, v.count), v.barBox, v.analysis, v.charts, v.foot, v.table, v.plan);
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
    else v.count.textContent = "Not finished (" + done + " points recorded)";
    if (tab.final && !v.finalDrawn) {  // a finished run is drawn once
      v.finalDrawn = true;
      clearTimeout(tab.timer); tab.timer = 0;
      v.analysis.replaceChildren(...(tab.final.analysis ? [analysisBlock(tab.final.analysis)] : []));
      v.charts.replaceChildren(runCharts(tab.final));
      v.plan.hidden = !tab.final.plan_text;
      v.plan.querySelector("pre").textContent = tab.final.plan_text || "";
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

  function setPaneWidth(w) {
    const max = Math.max(360, window.innerWidth - 420);
    const width = Math.round(Math.min(max, Math.max(340, w)));
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
    window.addEventListener("resize", () => { if (RUNS.open) setPaneWidth($("runpane").getBoundingClientRect().width); });
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

  // ------------------------------------------------------------------ reports
  function reportNames(content) {
    return [...String(content || "").matchAll(/^report: ([\w.-]+\.(?:pdf|pptx))/gm)].map((m) => m[1]);
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
    $("report-dialog").showModal();
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
      case "get_trace": return v("Reading", "Read") + " a trace from run " + code(a.run_id);
      case "recent_events": return v("Reading", "Read") + " the event log";
      case "wait": return v("Waiting", "Waited") + " " + esc(String(a.seconds)) + " s";
      case "save_note": return v("Saving", "Saved") + " a note to the project";  // older chats
      case "search_papers": return v("Searching", "Searched") + " the literature for " + code(a.query || "");
      case "read_paper": return v("Reading", "Read") + " " + code(a.paper || "") + (a.start ? " from character " + esc(String(a.start)) : "");
      case "save_paper": return v("Adding", "Added") + " " + code(a.paper || "") + " to the reading list";
      case "make_report": return v("Writing", "Wrote") + " a report" + (a.title ? ": " + esc(String(a.title)) : "");
      case "remember": return v("Remembering", "Remembered") + (a.scope === "global" ? " for every chat" : " for this project") +
        (a.text ? ": " + esc(String(a.text)) : "");
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
  // auto mode approved this without asking: show what ran and why, like an answered request
  function autoApproval(ev) {
    return h("div", { class: "approval done auto", role: "group", "aria-label": ev.title },
      h("div", { class: "approval-title" }, icon("check"), h("span", {}, ev.title)),
      h("ul", {}, ...ev.items.map((i) => h("li", {}, i))),
      h("div", { class: "approval-actions" }, h("span", { class: "verdict auto" }, "Auto-approved"),
        h("span", { class: "muted small" }, (ev.reasons || []).join("; "))));
  }

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
    answerReview(r) { this.endText(); this.el.append(reviewCard(r)); }
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

  function replayMessage(reply, m) {
    if (m.role === "assistant") {
      for (const b of m.blocks) {
        if (b.type === "text") { reply.addText(b.text); reply.endText(); }
        else if (b.type === "thinking") { reply.addThinking(b.text); reply.endThinking(); }
        else if (b.type === "tool_call") reply.toolStart(b.id, b.name, b.input);
      }
      if (m.review) reply.answerReview(m.review);
    } else if (m.role === "tool") {
      for (const r of m.results) {
        if (r.review) reply.review(Object.assign({ call_id: r.id }, r.review));
        const run = r.name === "run_plan" ? (String(r.content).match(/run_id='([^']+)'/) || [])[1] : null;
        reply.toolEnd(r.id, r.content, r.is_error, run);
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
        thread.append(userBubble(m.text));
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
    const item = h("div", { class: "chat-item" + (c.id === S.chatId ? " active" : ""), role: "button", tabindex: "0", draggable: "true" },
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
        rowButton("plus", "New chat in " + p.name, () => newChat(p.id)),
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
    const top = Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 8);
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
      { label: "New chat in this project", icon: "plus", onClick: () => newChat(p.id) },
      { label: "Rename", icon: "pencil", onClick: () => { SIDE.renaming = p.id; renderChats(); } },
      { label: "Project settings", icon: "sliders", onClick: () => openProjectSettings(p) },
      { sep: true },
      { label: "Delete project", icon: "trash", danger: true, onClick: () => deleteProject(p, chats) },
    ]);
  }

  // project memory: the user's instructions and the notes Kelvin saved, seen only by this project's chats
  let editingProject = null;
  async function openProjectSettings(p) {
    try { Object.assign(p, await api("/api/projects/" + p.id)); } catch (e) { toast(e.message); return; }  // notes may be new
    editingProject = p;
    $("project-dialog-name").textContent = p.name;
    $("project-instructions").value = p.instructions || "";
    const rules = p.rules || { providers: null, literature: true };
    for (const c of document.querySelectorAll("#project-providers input")) c.checked = !rules.providers || rules.providers.includes(c.value);
    $("project-literature").checked = rules.literature !== false;
    $("rules-summary").textContent = rulesText(rules);
    $("project-msg").textContent = "";
    renderNotes(p);
    renderPapers(p);
    $("project-dialog").showModal();
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
      Object.assign(p, await api("/api/projects/" + p.id, { method: "PATCH", body: { instructions: $("project-instructions").value, rules } }));
      applyRules();
      $("project-dialog").close();
      toast("Saved. Conversations in " + p.name + " use it from their next message.");
    } catch (e) {
      $("project-msg").textContent = e.message;
    }
  }

  // ------------------------------------------------------------------ who may see a project
  const PROVIDER_NAMES = { anthropic: "Claude", openai: "GPT", gemini: "Gemini", local: "local models" };
  function rulesText(rules) {
    if (!rules) return "";
    const parts = [];
    if (rules.providers) parts.push("only " + rules.providers.map((p) => PROVIDER_NAMES[p] || p).join(" and "));
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
    try { Object.assign(p, await api("/api/projects/" + p.id, { method: "PATCH", body: { name } })); } catch (e) { toast(e.message); }
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
  function applyIncognito() {
    document.body.classList.toggle("incognito", S.incognito);
    const b = $("toggle-incognito");
    b.setAttribute("aria-pressed", String(S.incognito));
    b.classList.toggle("on", S.incognito);
    b.hidden = !!S.chatId && !S.incognito;  // offered on a new chat, shown while in one
    $("greeting").textContent = S.incognito ? "Incognito chat" : "What are we measuring today?";
    $("empty-sub").textContent = S.incognito
      ? "Kelvin uses no memory here, keeps nothing from this chat, and forgets it when you leave. Instrument commands and runs are still logged."
      : S.labSub || "";
    $("input").placeholder = S.incognito ? "Ask anything; nothing here is remembered" : "Ask Kelvin…";
  }
  function toggleIncognito() {
    if (S.streaming) { toast("Wait for the current answer, or press stop."); return; }
    if (S.incognito) { newChat(); return; }   // leaving: forget it and start a normal chat
    if (S.chatId) newChat();
    S.incognito = true;
    SIDE.newProject = null;
    applyIncognito();
    setTitle("", null);
    renderChats();
    $("input").focus();
  }

  async function openChat(id) {
    if (S.streaming) { toast("Wait for the current answer, or press stop."); return; }
    let chat;
    try { chat = await api("/api/chats/" + id); } catch (e) { toast(e.message); return; }
    leaveIncognito();
    S.chatId = id;
    SIDE.newProject = null;
    S.chatRules = chat.rules || null;
    renderChat(chat);
    setTitle(chat.title, chat.project);
    if (chat.model && S.labels[chat.model]) $("model").value = chat.model;
    applyRules();
    renderChats();
    closeSidebar();
  }
  function newChat(projectId) {
    if (S.streaming) { toast("Wait for the current answer, or press stop."); return; }
    leaveIncognito();
    S.chatId = null;
    S.chatRules = null;
    SIDE.newProject = projectId || null;
    applyRules();
    thread.replaceChildren();
    showEmpty(true);
    setTitle("", SIDE.newProject);
    renderChats();
    closeSidebar();
    applyIncognito();
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
    const extra = S.compare.filter((k) => k !== model);
    if (extra.length) return sendCompare(text, [model, ...extra]);
    store("lh.model", model);
    if (!S.chatId) {
      const rules = currentRules();
      try { S.chatId = (await api("/api/chats", { body: { model, project: SIDE.newProject, incognito: S.incognito } })).id; } catch (e) { toast(e.message); return; }
      S.chatRules = rules;
      applyIncognito();
    }
    $("input").value = ""; autosize();
    $("input").placeholder = "Ask Kelvin…";
    showEmpty(false);
    thread.append(userBubble(text));
    const reply = new Reply(model);
    thread.append(reply.el);
    toBottom();
    setStreaming(true);
    try {
      const res = await fetch("/api/chats/" + S.chatId + "/turn", {
        method: "POST",
        headers: { "X-Kelvin-Token": token, "Content-Type": "application/json" },
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
      offerReview(reply);
    }
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
      if (S.labels[r.model]) { $("model").value = r.model; store("lh.model", r.model); }
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
      try { S.chatId = (await api("/api/chats", { body: { model: models[0], project: SIDE.newProject, incognito: S.incognito } })).id; } catch (e) { toast(e.message); return; }
      S.chatRules = rules;
      applyIncognito();
    }
    $("input").value = ""; autosize();
    $("input").placeholder = "Ask Kelvin…";
    showEmpty(false);
    for (const el of document.querySelectorAll(".msg-actions")) el.remove();
    thread.append(userBubble(text));
    const view = new CompareView(models);
    thread.append(view.el);
    toBottom();
    setStreaming(true);
    try {
      const res = await fetch("/api/chats/" + S.chatId + "/compare", {
        method: "POST",
        headers: { "X-Kelvin-Token": token, "Content-Type": "application/json" },
        body: JSON.stringify({ text, models }),
      });
      if (!res.ok) {
        let msg = "HTTP " + res.status;
        try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
        view.lanes[0].reply.error(msg);
        return;
      }
      await readEvents(res, (ev) => {
        const stick = nearBottom();
        if (ev.lane !== undefined) {
          const lane = view.lanes[ev.lane];
          if (lane && ev.type === "lane_end") lane.reply.finish();
          else if (lane) handle(lane.reply, ev);
        } else if (ev.type === "compare_start") { setTitle(ev.title, currentProject()); refreshChats(); }
        else if (ev.type === "memory") view.el.before(recallBox(ev.notes));
        else if (ev.type === "compare_end") { view.setChosen(ev.chosen, true); S.lastReply = view.lanes[ev.chosen].reply; }
        else if (ev.type === "error") view.lanes[0].reply.error(ev.message);
        if (stick) toBottom();
      });
    } catch (e) {
      view.lanes[0].reply.error("Lost the connection to the portal (" + e.message + ").");
    } finally {
      for (const l of view.lanes) l.reply.finish();
      setStreaming(false);
      refreshChats();
      pollState();
      offerReview(S.lastReply);
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
  function reviewCard(r) {
    const [tone, word] = VERDICTS[r.verdict] || ["muted", "No verdict"];
    const who = S.labels[r.model] || r.label || r.model;
    return h("div", { class: "review " + tone },
      h("div", { class: "review-head" }, h("span", { class: "verdict-chip" }, word),
        h("span", { class: "review-who" }, "Second opinion" + (r.kind === "plan" ? " on the plan" : "") + " · " + who)),
      h("div", { class: "md", html: md(r.text || "") }));
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
    reply.el.append(wrap);
  }
  async function askReview(reply, wrap, model) {
    const pending = h("div", { class: "review pending" }, h("span", { class: "spinner" }),
      h("span", {}, "Asking " + (S.labels[model] || model) + " for a second opinion…"));
    wrap.before(pending);
    wrap.hidden = true;
    try {
      const r = await api("/api/chats/" + S.chatId + "/review", { body: { model } });
      pending.replaceWith(reviewCard(r));
      $("input").placeholder = "Reply — Kelvin will see the second opinion";
    } catch (e) {
      pending.replaceWith(h("div", { class: "callout" }, e.message));
      wrap.hidden = false;
    }
  }

  function handle(reply, ev) {
    const stick = nearBottom();
    switch (ev.type) {
      case "turn_start": reply.setModel(ev.model); setTitle(ev.title, currentProject()); refreshChats(); break;
      case "text": reply.addText(ev.delta); break;
      case "thinking": reply.addThinking(ev.delta); break;
      case "reset": reply.resetText(); break;
      case "tool_start": reply.toolStart(ev.id, ev.name, ev.input); break;
      case "approval": reply.approval(ev); break;
      case "approval_closed": {
        const card = S.approvals.get(ev.id);
        if (card) card.close(card.choice === undefined ? "Closed" : card.choice ? "Approved" : "Declined", !!card.choice);
        break;
      }
      case "tool_end": reply.toolEnd(ev.id, ev.content, ev.is_error, ev.run_id); pollState(); break;
      case "run_progress": reply.runProgress(ev); break;
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
  const OPEN = new Set(), SHOW_SETTINGS = new Set();
  const PANEL = { filter: "", activeOnly: store("lh.activeOnly") === "1", instruments: [] };

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
    const stateText = inst.state === "error" ? "not responding" : inst.state === "active" ? inst.active + " active" : "safe";
    const chev = icon("chev"); chev.classList.add("chev");
    const count = inst.state === "active" ? h("span", { class: "inst-count" }, String(inst.active))
      : inst.state === "error" ? h("span", { class: "inst-count err" }, "!") : h("span", { class: "inst-count none" });
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
    const item = h("div", { class: "inst " + inst.state + (open ? " open" : "") }, row);
    const on = inst.drives.filter((d) => d.away);
    if (inst.state === "error") item.append(h("div", { class: "inst-sub" }, "Not responding"));
    else if (on.length && !open) item.append(h("div", { class: "inst-sub" }, on.map((d) => d.label + " " + d.value).join(" · ")));
    if (!open) return item;
    const card = item;
    const body = h("div", { class: "inst-body" });
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
    const shown = all.filter((i) => (!PANEL.activeOnly || i.state !== "safe") &&
      (!q || (i.name + " " + i.model + " " + i.kind).toLowerCase().includes(q)));
    const active = all.filter((i) => i.state === "active").length;
    const errors = all.filter((i) => i.state === "error").length;
    $("inst-summary").textContent = all.length + " · " + active + " active" + (errors ? " · " + errors + " not responding" : "");
    $("seg-active").textContent = "Active" + (active + errors ? " (" + (active + errors) + ")" : "");
    for (const b of document.querySelectorAll("#inst-seg button")) {
      const on = (b.dataset.v === "active") === PANEL.activeOnly;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    }
    $("lab-status").replaceChildren(...(shown.length ? shown.map(instrumentRow)
      : [h("div", { class: "inst-empty" }, PANEL.activeOnly && !q ? "Nothing is active." : "No instrument matches.")]));
  }

  async function pollState() {
    let s;
    try { s = await api("/api/state"); } catch (e) { return; }
    $("agent-state").textContent = s.busy ? "Kelvin is " + s.busy : "Kelvin is idle";
    $("agent-state").classList.toggle("busy", !!s.busy);
    PANEL.instruments = s.instruments;
    renderInstruments();
    $("lab-events").replaceChildren(...s.events.map((e) => {
      const m = e.match(/^(\d\d:\d\d:\d\d)\s+(.*)$/);
      return m ? h("li", {}, h("time", {}, m[1]), h("span", {}, m[2])) : h("li", {}, e);
    }));
    // requests not already shown in a conversation (e.g. after a page reload)
    const loose = s.pending.filter((p) => !S.approvals.has(p.id));
    $("lab-pending").replaceChildren(...loose.map((p) => makeApproval(p).el));
    const badge = $("lab-badge");
    badge.textContent = String(s.pending.length);
    badge.hidden = !s.pending.length;
    if (loose.length && !S.labOpen) toggleLab(true);
    showMode(s.approval_mode, s.approval_rule);
    renderAlerts(s.alarms);
    renderWatches(s.watches || [], s.status);
    notifySystem(s.notices);
    $("lab-name").textContent = s.lab;
    $("sim-tag").hidden = !s.simulated;
    const problem = s.lab_info && s.lab_info.error;
    $("lab-problem").hidden = !problem;
    if (problem) $("lab-problem").textContent = problem + " Kelvin is using the virtual lab meanwhile.";
    if (s.live && s.live.status === "running") noticeRun(s.live.run_id, s.live.plan);  // e.g. a run an external agent started
  }
  // ------------------------------------------------------------------ alarms, watches, notifications
  function renderAlerts(alarms) {
    const live = (alarms || []).filter((a) => !a.cleared && !a.acknowledged);
    const box = $("alerts");
    box.hidden = !live.length;
    box.replaceChildren(...live.map((a) => {
      const ok = h("button", { class: "btn small", type: "button" }, "Got it");
      ok.addEventListener("click", async () => { try { await api("/api/alarms/" + a.id + "/ack", { body: {} }); } catch (e) { /* gone */ } pollState(); });
      return h("div", { class: "alert" }, icon("warn"), h("div", { class: "alert-body" }, h("b", {}, a.text),
        a.actions.length ? h("span", {}, "Kelvin " + a.actions.join("; ")) : null), ok);
    }));
    document.title = (live.length ? "(!) " : "") + document.title.replace(/^\(!\) /, "");
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
    $("watch-summary").textContent = watches.length ? watches.filter((w) => w.alarm).length + " alarm · " + watches.length + " watched" : "";
    $("watch-list").replaceChildren(...(watches.length ? watches.map((w) => {
      const remove = w.source === "lab" ? h("span", { class: "tag", title: "From the lab config; the lab owner changes it there" }, "lab")
        : rowButton("x", "Stop watching", async () => { try { await api("/api/watches/" + w.id, { method: "DELETE" }); } catch (e) { toast(e.message); } pollState(); });
      return h("li", { class: "watch" + (w.alarm ? " alarm" : "") }, h("span", { class: "dot" + (w.alarm ? " bad" : " on") }),
        h("span", { class: "watch-rule" }, w.rule, w.then.length ? h("span", { class: "watch-then" }, " → " + w.then.join(", ")) : null),
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
  const LAB = { cards: [], found: [] };

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
  const CARDS = { editing: null };
  async function openCards() {
    fillModels($("card-model"), S.groups, (S.settings && S.settings.default_model) || $("model").value);
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
      return h("li", { class: "memory-note" + (c.problems.length ? " broken" : "") },
        h("span", { class: "memory-kind" + (reviewed ? " ok" : "") }, c.problems.length ? "broken" : reviewed ? "reviewed" : "draft"),
        h("div", { class: "memory-body" }, h("span", {}, c.model),
          h("span", { class: "memory-meta" }, (c.id || c.file) + (c.problems.length ? " · " + c.problems[0] : ""))),
        h("span", { class: "card-actions" }, edit, c.problems.length ? null : flip),
        rowButton("trash", "Delete this card", async () => {
          if (!confirm("Delete the card " + (c.id || c.file) + "?")) return;
          try { await api("/api/cards/delete", { body: { id: c.id || c.file.replace(/\.yaml$/, "") } }); } catch (e) { $("cards-msg").textContent = e.message; }
          loadCards();
        }));
    }) : [h("li", { class: "memory-empty" }, "No cards of your own yet.")]));
  }
  function showCardEditor(text, editing, problems, warnings, notes) {
    CARDS.editing = editing || null;
    $("card-edit").hidden = text == null;
    $("card-check").hidden = $("card-save").hidden = text == null;
    if (text == null) return;
    $("card-yaml").value = text;
    $("card-editing").textContent = editing ? "editing " + editing : "new card";
    showCardProblems(problems || [], warnings || []);
    $("card-notes").textContent = notes || "";
    $("card-yaml").focus();
  }
  function showCardProblems(problems, warnings) {
    $("card-problems").replaceChildren(
      ...problems.map((p) => h("li", { class: "bad" }, icon("warn"), h("span", {}, p))),
      ...warnings.map((w) => h("li", { class: "bad" }, icon("warn"), h("span", { class: "muted" }, w))),
      ...(problems.length ? [] : [h("li", { class: "good" }, icon("check"), h("span", {}, "The card is valid."))]));
  }
  async function draftCard() {
    const manual = $("card-manual").value.trim();
    if (manual.length < 200) { $("cards-msg").textContent = "Paste the manual's command reference first."; return; }
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
      const r = await api("/api/cards", { body: { yaml: $("card-yaml").value, replace: CARDS.editing } });
      $("cards-msg").textContent = "Saved " + r.id + " (" + r.status + "). Add it to the lab under Instruments.";
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

  async function openLabSetup() {
    let r;
    try { r = await api("/api/lab"); } catch (e) { toast(e.message); return; }
    LAB.cards = r.cards;
    showLabState(r.info, r.name);
    $("lab-card").replaceWith(Object.assign(cardSelect("", "Card…"), { id: "lab-card" }));
    $("lab-yaml").value = r.yaml || "";
    $("lab-found").hidden = $("lab-add-found").hidden = true;
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
    $("lab-msg").textContent = r.found.length ? "" : "Nothing found. Is VISA installed, and are the instruments on and connected?";
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
      return h("label", { class: "found" }, check, name, model, h("code", { class: "found-addr", title: f.settings || "" }, f.address));
    }));
  }
  async function addToConfig(items) {
    if (!items.length) { $("lab-msg").textContent = "Nothing chosen."; return; }
    try {
      const r = await api("/api/instruments/draft", { body: { items, base: $("lab-yaml").value } });
      $("lab-yaml").value = r.yaml;
      $("lab-msg").textContent = "Added " + items.map((i) => i.name).join(", ") + ". Review the limits, then save and connect.";
    } catch (e) { $("lab-msg").textContent = e.message; }
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
        : "Connected to " + r.name + ". Every instrument answered as expected.";
      pollState();
    } catch (e) {
      $("lab-msg").textContent = e.message;
    } finally { save.disabled = false; }
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
    if ($("rules-chip")) applyRules();
    if (!$("model").value) $("input").placeholder = "Add an API key in Settings to start";
    else $("input").placeholder = "Ask Kelvin…";
    updateSend();
  }
  function openSettings() {
    const keys = (S.settings && S.settings.keys) || {};
    for (const hint of document.querySelectorAll("[data-hint]")) {
      const src = keys[hint.dataset.hint];
      hint.textContent = src ? "Set (from " + src + ")" : "Not set";
      hint.classList.toggle("set", !!src);
    }
    for (const input of document.querySelectorAll("#settings-form input:not([type=checkbox])")) { input.value = ""; delete input.dataset.clear; }
    $("local-url").value = (S.settings && S.settings.local_base_url) || "";
    $("local-models").value = (S.settings && S.settings.local_models) || "";
    fillModels($("default-model"), S.groups, (S.settings && S.settings.default_model) || "", true);
    fillModels($("reviewer"), S.groups, (S.settings && S.settings.reviewer) || "", true);
    $("reviewer").options[0].textContent = "Off";
    $("global-instructions").value = (S.settings && S.settings.instructions) || "";
    $("learn").checked = !S.settings || S.settings.learn !== false;
    $("literature").checked = !S.settings || S.settings.literature !== false;
    $("pair-box").hidden = true;
    loadPhone();
    const kinds = (S.settings && S.settings.notify_kinds) || ["alarm", "run"];
    for (const c of document.querySelectorAll("#notify-kinds input")) c.checked = kinds.includes(c.value);
    $("allow-notify").hidden = !("Notification" in window) || Notification.permission !== "default";
    $("settings-msg").textContent = "";
    renderGlobalNotes(null);
    $("settings-dialog").showModal();
    api("/api/memory").then((m) => renderGlobalNotes(m.notes)).catch(() => renderGlobalNotes([]));
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
  async function saveSettings() {
    const body = {};
    for (const input of document.querySelectorAll("#settings-form input[type=password]")) {
      if (input.dataset.clear) body[input.name] = "";
      else if (input.value.trim()) body[input.name] = input.value.trim();
    }
    body.local_base_url = $("local-url").value.trim();
    body.local_models = $("local-models").value.trim();
    body.default_model = $("default-model").value;
    body.reviewer = $("reviewer").value;
    body.instructions = $("global-instructions").value;
    body.learn = $("learn").checked ? "on" : "off";
    body.literature = $("literature").checked ? "on" : "off";
    body.notify_kinds = [...document.querySelectorAll("#notify-kinds input")].filter((c) => c.checked).map((c) => c.value).join(",") || "none";
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
    $("scrim").hidden = !($("app").classList.contains("side-open") || ((S.labOpen || RUNS.open) && narrow));
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
      $("nokey").querySelector("p").textContent = "Kelvin did not accept this link (" + e.message + "). Open the link Kelvin printed when it started.";
      return;
    }
    $("app").hidden = false;
    for (const slot of document.querySelectorAll("[data-mark]")) slot.innerHTML = MARK;
    S.settings = boot.settings;
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
    applyModels(boot.models.groups, boot.default_model);
    initRunsPane();
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
    $("model").addEventListener("change", () => {
      store("lh.model", $("model").value); updateSend();
      S.compare = S.compare.filter((k) => k !== $("model").value); compareLabel();
    });
    $("compare-btn").addEventListener("click", toggleCompareMenu);
    $("phone-on").addEventListener("change", switchPhone);
    $("pair-start").addEventListener("click", startPairing);
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
    $("card-file").addEventListener("change", async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      if (f.size > 2e6) { $("cards-msg").textContent = "That file is large; paste only the command reference."; return; }
      let text = await f.text();
      if (/\.html?$/i.test(f.name)) text = new DOMParser().parseFromString(text, "text/html").body.innerText;
      $("card-manual").value = text;
      if (!$("card-name").value) $("card-name").value = f.name.replace(/\.[^.]+$/, "");
    });
    $("open-report").addEventListener("click", openReport);
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
    $("lab-add").addEventListener("click", addByAddress);
    $("lab-save").addEventListener("click", () => connectLab({ yaml: $("lab-yaml").value }));
    for (const b of document.querySelectorAll("#lab-seg button")) {
      b.addEventListener("click", () => {
        if (b.classList.contains("on")) return;
        if (b.dataset.use === "mine" && !$("lab-yaml").value.trim()) { $("lab-msg").textContent = "Set up your instruments first: scan, or add one by address."; return; }
        connectLab({ use: b.dataset.use });
      });
    }
    $("new-chat").addEventListener("click", () => newChat());
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
    setInterval(() => { if (S.labOpen || S.streaming || !$("lab-badge").hidden) pollState(); }, 1500);
    setInterval(pollState, 4000);   // often enough that an alarm shows within seconds
    pollState();
    $("input").focus();
  }
  init();
})();
