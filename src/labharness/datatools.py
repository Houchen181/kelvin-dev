"""Kelvin's analysis tools on kept runs (the MCP server's fit_run, plot_runs, export_run and run_python): finding a
run's columns and units, then putting fitting.py, plotting.py, export.py and sandbox.py to work on them.

None of them touches an instrument or waits for the lab. They read runs and write only their own results: a fit into
its run's run.h5 (/fits/<name>), figures into the data folder's figures/, exports into the run's folder, and
run_python's files into its scratch folder."""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import expr, rundata
from .cards import trace_x

FIT_S = 60.0               # a fit's time limit
DB_UNITS = ("dB", "dBm", "dBc", "dBV", "dBW")
DEG = ("deg", "°", "degree", "degrees")


class ToolError(ValueError):
    pass


def _np() -> Any:
    try:
        import numpy
    except ImportError:
        raise ToolError("this needs NumPy, which this Kelvin does not have")
    return numpy


def run_id_of(rt: Any, run_id: str) -> str:
    run_id = str(run_id or "").strip() or "last"
    if run_id == "last":
        ids = rt.pick_runs(last=1)
        if not ids:
            raise ToolError("no runs yet")
        return ids[-1]
    return run_id


class Source:
    """One run's data for these tools: its scalar columns as arrays (an expression of them too), its traces, units."""

    def __init__(self, rt: Any, run_id: str):
        self.run_id = run_id_of(rt, run_id)
        try:
            self.folder = rt._run_dir(self.run_id)
            self.rec = rundata.load(self.folder, with_rows=False)
            self.cols = rt._columns(self.rec)
            self.table = rundata.columns(self.folder)
        except KeyError as e:
            raise ToolError(e.args[0] if e.args else "no run %r" % run_id)
        except (OSError, ValueError) as e:
            raise ToolError("run %s can't be read: %s" % (self.run_id, e))
        self.units = {"t": "s", "i": ""}
        for c in (self.cols.get("axes") or []) + (self.cols.get("measures") or []):
            self.units[c["key"]] = c.get("unit") or ""
        self.kinds = {m["key"]: m.get("kind", "scalar") for m in self.cols.get("measures") or []}
        self.x_units = {m["key"]: m.get("x_unit", "") for m in self.cols.get("measures") or []}
        self._rows: Optional[List[Dict[str, Any]]] = None

    @property
    def axes(self) -> List[str]:
        return [a["key"] for a in self.cols.get("axes") or [] if a.get("kind") != "repeat"]

    @property
    def scalars(self) -> List[str]:
        return [m["key"] for m in self.cols.get("measures") or [] if m.get("kind", "scalar") == "scalar"]

    @property
    def traces(self) -> List[str]:
        return [k for k, kind in self.table.kinds.items() if kind == "trace"]

    def values(self, text: str) -> Tuple[Any, str]:
        """A column, or an expression of the run's columns (expr.py): its values at every point and its unit (an
        expression's is unknown: '')."""
        np = _np()
        text = str(text or "").strip()
        if text in self.table.numbers:
            return np.asarray(self.table.numbers[text], dtype=float), self.units.get(text, "")
        try:
            v = expr.evaluate(text, self.table.number, np)
        except ValueError as e:
            raise ToolError("%s: %s" % (text, e))
        v = np.asarray(v, dtype=float)
        if v.ndim == 0:
            v = np.full(self.table.n, float(v))
        if v.shape != (self.table.n,):
            raise ToolError("%s doesn't give one value per point" % text)
        return v, ""

    def mask(self, where: str) -> Any:
        np = _np()
        if not where:
            return np.ones(self.table.n, dtype=bool)
        try:
            m = np.asarray(expr.evaluate(where, self.table.number, np))
        except ValueError as e:
            raise ToolError("where: %s" % e)
        if m.dtype != bool:
            raise ToolError("where needs a condition, e.g. smu.V > 1")
        return np.broadcast_to(m, (self.table.n,)).copy()

    def rows(self) -> List[Dict[str, Any]]:
        if self._rows is None:
            self._rows = rundata.load(self.folder).rows
        return self._rows

    def trace(self, key: str, row: Optional[int]) -> Tuple[Any, Any, str, str, int]:
        """One point's trace: (x, y, x unit, y unit, the point). row None: the last point that has one."""
        np = _np()
        if key not in self.traces:
            raise ToolError("%s is not a trace of %s (traces: %s)" % (key, self.run_id, ", ".join(self.traces) or
                                                                      "none"))
        sizes = self.table.sizes[key]
        if row is None:
            have = [i for i, s in enumerate(sizes) if s]
            if not have:
                raise ToolError("%s has no %s trace yet" % (self.run_id, key))
            row = have[-1]
        if not 0 <= int(row) < len(sizes):
            raise ToolError("row must be 0…%d" % (len(sizes) - 1))
        one = rundata.trace_row(self.folder, key, int(row))
        if one is None:
            one = self.rows()[int(row)]
        ys = one.get(key)
        if not isinstance(ys, list) or not ys:
            raise ToolError("%s has no %s trace at row %d" % (self.run_id, key, row))
        xs = trace_x(one, key, len(ys))
        y = np.array([np.nan if v is None else float(v) for v in ys])
        return np.asarray(xs, dtype=float), y, self.x_units.get(key, ""), self.units.get(key, ""), int(row)

# ------------------------------------------------------------------ fit_run


def _numbers(v: Any, what: str) -> Any:
    np = _np()
    try:
        return np.asarray([float("nan") if x is None else float(x) for x in v], dtype=float)
    except (TypeError, ValueError):
        raise ToolError("%s must be a list of numbers" % what)


def fit_run(rt: Any, run_id: str = "last", model: str = "", x: str = "", y: str = "", where: str = "",
            sigma: Any = None, start: Optional[Dict[str, Any]] = None, fix: Optional[Dict[str, Any]] = None,
            bounds: Optional[Dict[str, Any]] = None, x_range: Optional[Sequence[Any]] = None,
            row: Optional[int] = None, phase: str = "", name: str = "", keep: Optional[bool] = None,
            x_values: Optional[Sequence[Any]] = None, y_values: Optional[Sequence[Any]] = None,
            x_unit: str = "", y_unit: str = "") -> Tuple[Optional[bytes], str]:
    """The fit_run tool: (a picture of the fit and its residuals, the fit in words). keep: None keeps it unless it
    failed; True keeps even a failed fit (marked failed); False only looks."""
    from . import fitting, plotting
    np = _np()
    if not str(model or "").strip():
        raise ToolError("say which model: %s, or an expression in x such as a*exp(-x/tau) + c" %
                        ", ".join(fitting.MODELS))
    model = str(model).strip()
    key = fitting.ALIASES.get(model.lower(), model.lower())
    notes: List[str] = []
    src: Optional[Source] = None
    columns: Sequence[str] = ()
    if x_values is not None or y_values is not None:   # numbers given, not a run's (Hc2 against T from several runs)
        if x_values is None or y_values is None:
            raise ToolError("give both x_values and y_values")
        xv, yv = _numbers(x_values, "x_values"), _numbers(y_values, "y_values")
        xu, yu, xn, yn = x_unit, y_unit, "x", "y"
        sig = _numbers(sigma, "sigma") if isinstance(sigma, (list, tuple)) else sigma
        ph = None
        keep = False
    else:
        src = Source(rt, run_id)
        trace_y = (y or "").strip() in src.traces or (row is not None and not y and src.traces)
        if trace_y:
            yn = (y or "").strip() or src.traces[0]
            xv, yv, xu, yu, r = src.trace(yn, row)
            row = r   # kept with the fit: the point whose trace it was
            xn = "frequency" if xu == "Hz" else "x"
            ph = src.trace(phase, r)[1] if phase else None
            pu = src.units.get(phase, "") if phase else ""
            mask = np.ones(xv.size, dtype=bool)
            sig = sigma if sigma is None or isinstance(sigma, (int, float)) else None
            if isinstance(sigma, str) and sigma.strip():
                raise ToolError("a trace's sigma is one number (the same for every sample)")
            notes.append("data: %s, the trace at row %d" % (yn, r))
        else:
            xn = (x or "").strip() or (src.axes[-1] if src.axes else "t")
            yn = (y or "").strip() or (src.scalars[0] if src.scalars else "")
            if not yn:
                raise ToolError("%s measured no scalar column: say y (a column or an expression)" % src.run_id)
            xv, xu0 = src.values(xn)
            yv, yu0 = src.values(yn)
            xu, yu = x_unit or xu0, y_unit or yu0
            mask = src.mask(where)
            if isinstance(sigma, str) and sigma.strip():
                sig, _ = src.values(sigma)
            else:
                sig = sigma
            if phase:
                ph, pu = src.values(phase)
            else:
                ph, pu = None, ""
            columns = list(src.table.numbers)
        if x_range:
            lo, hi = (list(x_range) + [None, None])[:2]
            with np.errstate(invalid="ignore"):
                if lo is not None:
                    mask &= xv >= float(lo)
                if hi is not None:
                    mask &= xv <= float(hi)
        if not mask.any():
            raise ToolError("no points of %s are left in the window (where, x_range)" % src.run_id)
        if mask.sum() < mask.size:
            notes.append("window: %d of %d points%s%s" % (int(mask.sum()), mask.size, " where " + where if where else "",
                                                          " with x in %s" % list(x_range) if x_range else ""))
        xv, yv = xv[mask], yv[mask]
        if isinstance(sig, np.ndarray):
            sig = sig[mask]
        if ph is not None:
            ph = np.asarray(ph, dtype=float)[mask]
    if key == "resonator":   # S21: magnitude (dB or linear) and, when given, phase
        mag = 10 ** (yv / 20) if yu in DB_UNITS else yv
        if ph is not None:
            rad = np.deg2rad(ph) if pu in DEG else ph
            yv = mag * np.exp(1j * rad)
            m = fitting.MODELS["resonator"]()
        else:
            yv = mag
            m = fitting._resonator_magnitude()
            notes.append("no phase given: fitted on |S21| alone, so the cable delay and alpha are not fitted")
        if yu in DB_UNITS:
            notes.append("%s in %s fitted as the linear amplitude 10^(dB/20)" % (yn, yu))
        yu = ""
    elif key in fitting.MODELS:
        m = fitting.MODELS[key]()
        if yu in DB_UNITS and key in ("lorentzian", "gaussian", "double_lorentzian"):
            yv = 10 ** (yv / 10)   # a resonance is a Lorentzian in power, not in dB
            notes.append("%s in %s fitted as linear power 10^(dB/10): its widths are linewidths (a fit on dB values "
                         "gives a width of the dB curve, not a linewidth)" % (yn, yu))
            yu = ""
    else:
        try:
            m = fitting.expression_model(model, [c for c in columns if c not in (xn,)], xn)
        except fitting.FitError as e:
            raise ToolError(str(e))
    extra = {c: src.values(c)[0][mask] for c in m.columns} if src is not None and m.columns else None
    try:
        result = fitting.fit(m, xv, yv, sig, start, fix, bounds, extra, xu, yu, deadline=time.monotonic() + FIT_S)
    except fitting.FitError as e:
        raise ToolError(str(e))
    if key == "resonator" and result.get("failed") and _rises(np, np.abs(yv)):
        notes.append("the notch model is for a dip in S21 (a resonator beside the line); this trace rises at its "
                     "resonance, a transmission peak: fit lorentzian")
    what = "%s vs %s" % (yn, xn)
    lines = (["%s:" % src.run_id] if src else []) + fitting.fit_text(result, what) + ["note: " + n for n in notes]
    if keep is None and src is not None and result.get("failed"):
        lines.append("not kept: the fit failed (keep: true keeps it anyway, marked failed)")
    elif (keep or keep is None) and src is not None:
        lines.append(_keep_fit(rt, src, m, result, name, {
            "model": model, "x": xn, "y": yn, "where": where, "sigma": sigma if isinstance(sigma, (int, float, str))
            else None, "start": start, "fix": fix, "bounds": bounds, "x_range": list(x_range) if x_range else None,
            "row": row, "phase": phase, "notes": notes or None}))
    picture = None
    try:
        fig = plotting.fit_figure(result, fitting.curve(m, result), xn, yn,
                                  "%s%s fit" % ((src.run_id + ": ") if src else "", result["model"]))
        picture = plotting.to_png(fig)
    except Exception as e:   # the numbers stand without the picture
        lines.append("(no picture: %s)" % e)
    return picture, "\n".join(lines)


def _rises(np: Any, y: Any) -> bool:
    """Whether a trace's largest excursion from its ends' level is upward (a transmission peak, not a notch)."""
    y = y[np.isfinite(y)]
    if y.size < 5:
        return False
    edge = max(2, y.size // 10)
    base = float(np.median(np.concatenate([y[:edge], y[-edge:]])))
    return float(y.max()) - base > base - float(y.min())


def _keep_fit(rt: Any, src: Source, model: Any, result: Dict[str, Any], name: str, spec: Dict[str, Any]) -> str:
    """The fit into the run's run.h5, where a run that has finished keeps it, with who kept it (the agent, for the
    person signed in) and when; then the run's scan report is written again, so it shows the fit (kept fits are the
    run's other fits: the accepted one, analyze_run's, is its result)."""
    from . import fitting
    folder = src.folder
    if not (folder / rundata.RUN_FILE).exists() or not rundata.have_h5():
        return "not kept: this run has no run.h5 yet (it is still being measured, or this Kelvin has no h5py)"
    if rundata.being_recorded(folder):
        return "not kept: the run is still being measured; fit it again once it has finished to keep the fit"
    default = "%s %s" % (spec.get("y") or "", result["model"] if result["model"] != "expression" else "model")
    account = getattr(rt, "user", None)
    try:
        kept = fitting.store(folder, name or default.strip(), result, spec, by="agent", account=account)
    except Exception as e:
        return "not kept: %s" % e
    rt.gate.log.write("fit_kept", by="agent", account=account, run=src.run_id, fit=kept, model=result["model"])
    return "kept in %s as /fits/%s (with its covariance and residuals); fit_run again with the same name replaces " \
           "it%s" % (folder / rundata.RUN_FILE, kept, _report_again(rt, src.run_id))


_REPORTS = threading.Lock()   # a run's scan report, written again by one fit at a time (two may be kept at once)


def _report_again(rt: Any, run_id: str) -> str:
    """The run's scan report written again with its kept fits, where Kelvin keeps scan reports up to date. Never
    fails the fit: it is kept whatever happens to the report."""
    if "scan" not in getattr(rt, "report_kinds", ()) or getattr(rt, "auto_report", "off") == "off":
        return ""
    try:
        with _REPORTS:
            made = rt.write_scan_report(run_id)
    except Exception as e:
        return "; its scan report was not written again: %s" % (str(e).splitlines()[0][:160] if str(e) else e)
    return "; its scan report shows it now (%s)" % ", ".join(made) if made else ""

# ------------------------------------------------------------------ plot_runs


def _slug(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text).strip("_")[:40] or "figure"


def _grouped(src: Source, key: str, mask: Any) -> List[Tuple[str, Any]]:
    """One curve per value of a column (an outer axis): (label, mask)."""
    g, unit = src.values(key)
    values = [v for v in dict.fromkeys(g[mask].tolist()) if v == v]
    from .cards import si
    return [("%s = %s" % (key, si(v, unit)), mask & (g == v)) for v in values]


def plot_runs(rt: Any, curves: Optional[List[Dict[str, Any]]] = None, title: str = "", x_label: str = "",
              y_label: str = "", x_unit: str = "", y_unit: str = "", x_log: bool = False, y_log: bool = False,
              x_range: Optional[Sequence[Any]] = None, y_range: Optional[Sequence[Any]] = None,
              heat_map: Optional[Dict[str, Any]] = None, formats: Optional[Sequence[str]] = None,
              name: str = "") -> Tuple[Optional[bytes], str]:
    """The plot_runs tool: (the figure as PNG, its caption and the files it was saved as)."""
    from . import plotting
    from .fitting import kept_curve, stored
    from .reports.chart import FIT
    np = _np()
    curves = list(curves or [])
    if not curves and not heat_map:
        curves = [{"run": "last"}]
    drawn: List[plotting.Curve] = []
    notes: List[str] = []
    xs_seen: List[str] = []
    ys_seen: List[str] = []
    units_y: List[str] = []
    units_x: List[str] = []
    fixed = {"x": False, "y": False}   # a column shown in the unit asked for (µV), not rescaled to another prefix
    for n, spec in enumerate(curves):
        if not isinstance(spec, dict):
            raise ToolError("each curve is an object such as {run: 'last', x: 'smu.V', y: 'lockin.X / smu.I'}")
        src = Source(rt, str(spec.get("run") or "last"))
        y = str(spec.get("y") or "").strip()
        row = spec.get("row")
        unit_given = str(spec.get("unit") or y_unit or "")
        if (y and y in src.traces) or (row is not None and not y and src.traces):
            key = y or src.traces[0]
            xv, yv, xu, yu, r = src.trace(key, None if row is None else int(row))
            groups = [("%s%s" % (spec.get("label") or key, " (row %d)" % r if len(src.table.sizes[key]) > 1 else ""),
                       np.ones(xv.size, dtype=bool))]
            xn, yn, ev = ("frequency" if xu == "Hz" else "x"), key, None
        else:
            xn = str(spec.get("x") or "").strip() or (src.axes[-1] if src.axes else "t")
            yn = y or (src.scalars[0] if src.scalars else "")
            if not yn:
                raise ToolError("%s measured no scalar column: say y" % src.run_id)
            xv, xu = src.values(xn)
            yv, yu = src.values(yn)
            ev = src.values(str(spec["errors"]))[0] if spec.get("errors") else None
            mask = src.mask(str(spec.get("where") or ""))
            group = str(spec.get("group") or "").strip()
            if group == "outer":
                group = src.axes[0] if len(src.axes) > 1 else ""
            several = len({str(c.get("run") or "last") for c in curves}) > 1
            label = str(spec.get("label") or ("%s %s" % (src.run_id, yn) if several else yn))
            groups = _grouped(src, group, mask) if group else [(label, mask)]
        factor = 1.0
        fixed["y"] = fixed["y"] or bool(yu and unit_given)
        fixed["x"] = fixed["x"] or bool(xu and x_unit)
        if yu and unit_given and unit_given != yu:
            try:
                factor = plotting.convert(yu, unit_given)
            except ValueError as e:
                raise ToolError("%s: %s" % (yn, e))
        shown_unit = unit_given or yu
        xfactor = 1.0
        if xu and x_unit and x_unit != xu:
            try:
                xfactor = plotting.convert(xu, x_unit)
            except ValueError as e:
                raise ToolError("%s: %s" % (xn, e))
        style = str(spec.get("style") or "line")
        if style not in ("line", "points", "both"):
            raise ToolError("style is line, points or both")
        for label, m in groups:
            drawn.append(plotting.Curve(xv[m] * xfactor, yv[m] * factor, label, "", style,
                                        ev[m] * factor if ev is not None else None))
        if spec.get("fit"):
            fits = {f["name"]: f for f in stored(src.folder, with_data=True)}
            f = fits.get(str(spec["fit"]))
            if f is None:
                raise ToolError("%s keeps no fit %r (it keeps: %s)" % (src.run_id, spec["fit"], ", ".join(fits) or
                                                                       "none"))
            fx, fy = kept_curve(f)
            drawn.append(plotting.Curve(np.asarray(fx) * xfactor, np.asarray(fy) * factor, "%s fit%s" % (
                f.get("model"), " (failed)" if f.get("failed") else ""), FIT, "fit"))
            if f.get("failed"):
                notes.append("the kept fit %s failed (%s): it is no result" % (f["name"], f["failed"]))
        xs_seen.append(xn)
        ys_seen.append(yn)
        units_y.append(shown_unit)
        units_x.append(x_unit or xu)
    real = [c for c in drawn if c.style != "fit"]
    for c, color in zip(real, plotting.colors_for(len(real))):
        c.color = color
    if len(set(u for u in units_y if u)) > 1:
        notes.append("the curves are in different units (%s) on one axis" % ", ".join(sorted(set(u for u in units_y
                                                                                                if u))))
    heat = None
    if heat_map:
        heat, hx, hy, hux, huy = _heat(rt, heat_map)
        xs_seen.append(hx)
        ys_seen.append(hy)
        units_x.append(hux)
        units_y.append(huy)
    lim = lambda r: tuple((list(r) + [None, None])[:2]) if r else None   # noqa: E731
    panel = plotting.Panel(drawn, x_label or " / ".join(dict.fromkeys(xs_seen)), x_unit or (units_x[0] if units_x
                                                                                             else ""),
                           y_label or " / ".join(dict.fromkeys(ys_seen)), units_y[0] if units_y else "",
                           bool(x_log), bool(y_log), lim(x_range), lim(y_range), heat, x_fixed=fixed["x"],
                           y_fixed=fixed["y"])
    for c in drawn:
        if (x_log and np.any(np.asarray(c.xs) <= 0)) or (y_log and np.any(np.asarray(c.ys) <= 0)):
            notes.append("values at or below 0 are left out on a log axis")
            break
    fig = plotting.Figure([panel], title or (heat_map and "map") or "", notes=notes)
    formats = [f.lower().lstrip(".") for f in (formats or ["png"])]
    bad = [f for f in formats if f not in plotting.FORMATS]
    if bad:
        raise ToolError("formats are png, svg and pdf, not %s" % ", ".join(bad))
    folder = Path(rt.data_dir) / "figures"
    folder.mkdir(parents=True, exist_ok=True)
    base = "%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"),
                      _slug(name or title or (ys_seen[0] if ys_seen else "figure")))
    saved = []
    png = plotting.to_png(fig)
    for f in formats:
        path = folder / ("%s.%s" % (base, f))
        path.write_bytes(png if f == "png" else plotting.RENDER[f](fig))
        saved.append(str(path))
    rt.gate.log.write("figure", by="agent", files=[Path(s).name for s in saved])
    return png, plotting.caption(fig) + "\nsaved: " + "; ".join(saved)


def _heat(rt: Any, spec: Dict[str, Any]) -> Tuple[Any, str, str, str, str]:
    from . import plotting
    from .pictures import grid_of
    src = Source(rt, str(spec.get("run") or "last"))
    if len(src.axes) < 2 and not (spec.get("x") and spec.get("y")):
        raise ToolError("%s swept one axis: a heat map needs two (give x and y)" % src.run_id)
    xn = str(spec.get("x") or src.axes[-1])
    yn = str(spec.get("y") or src.axes[0])
    zn = str(spec.get("z") or (src.scalars[0] if src.scalars else ""))
    if not zn:
        raise ToolError("say z, the value to color")
    xv, xu = src.values(xn)
    yv, yu = src.values(yn)
    zv, zu = src.values(zn)
    zu = str(spec.get("unit") or zu)
    mask = src.mask(str(spec.get("where") or ""))
    rows = [{"x": a, "y": b, "z": c} for a, b, c, m in zip(xv.tolist(), yv.tolist(), zv.tolist(), mask) if m]
    grid = grid_of(rows, "y", "x", "z")
    if grid is None:
        raise ToolError("the points of %s don't form a grid over %s and %s" % (src.run_id, xn, yn))
    outs, ins, z = grid
    return plotting.Heat(ins, outs, z, zn, zu, bool(spec.get("log"))), xn, yn, xu, yu

# ------------------------------------------------------------------ export_run


def export_run(rt: Any, run_id: str = "last", format: str = "csv") -> str:   # noqa: A002 - the tool's own word
    from . import export
    rid = run_id_of(rt, run_id)
    try:
        folder = rt._run_dir(rid)
    except KeyError as e:
        raise ToolError(e.args[0] if e.args else "no run %r" % run_id)
    try:
        paths, notes = export.export(folder, format)
    except export.ExportError as e:
        raise ToolError(str(e))
    except ImportError:
        raise ToolError("%s needs NumPy, which this Kelvin does not have (csv and xlsx don't)" % format)
    rt.gate.log.write("export", by="agent", run=rid, files=[p.name for p in paths])
    live = " (a snapshot: the run is still being measured)" if rundata.being_recorded(folder) else ""
    return "\n".join(["%s exported%s:" % (rid, live)] + ["  %s (%s)" % (p, _size(p.stat().st_size)) for p in paths] +
                     ["left out: " + n for n in notes] +
                     ["The person can also download it from the run's tab in the Runs pane (CSV, Excel, NumPy)."])


def _size(n: int) -> str:
    for unit in ("bytes", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "bytes" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d" % n


# ------------------------------------------------------------------ run_python


def run_python(rt: Any, code: str, runs: Optional[Sequence[str]] = None, timeout_s: float = 60.0
               ) -> Tuple[List[bytes], str]:
    """The run_python tool: (the pictures it made, what it printed and saved)."""
    from . import sandbox
    names = [str(r) for r in (runs if runs is not None else ["last"]) if str(r).strip()]
    folders: Dict[str, Path] = {}
    for name in names:
        rid = run_id_of(rt, name)
        try:
            folders[rid] = rt._run_dir(rid)
        except KeyError as e:
            raise ToolError(e.args[0] if e.args else "no run %r" % name)
    gen = rt.gate.stop_generation
    call = getattr(getattr(rt, "_client", None), "call", None)

    def stopped() -> bool:   # a person's Stop, or the MCP client cancelling the call
        return rt.gate.stop_generation != gen or bool(call is not None and call.cancelled)
    out = sandbox.run(code, folders, Path(rt.data_dir) / "python", timeout_s=timeout_s, stopped=stopped)
    rt.gate.log.write("run_python", by="agent", runs=list(folders), seconds=round(out.seconds, 2),
                      status=out.status, folder=str(out.folder), code_chars=len(code or ""))
    return out.images, out.text()
