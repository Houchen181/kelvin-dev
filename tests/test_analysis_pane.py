"""The Analysis pane after the overnight user test of Kelvin 0.3.32 (2026-10-30): a symmetrized curve counts each point
once, the CSV has every curve, units follow expressions and steps, the pane gives a fit its start and held values,
i, t and repeat are columns to choose, the server draws a map a curve per outer value (for the pane, the API and
Kelvin's tool alike), a kept fit records what made it, the pane's hints don't look like values, and a chat made
elsewhere reaches the sidebar."""
import asyncio
import html
import re

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("h5py")
pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from labharness import ScriptedApprover, datatools, fitting, units  # noqa: E402
from labharness._version import __version__  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.app import STATIC, create_app  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from labharness.workbench import AnalysisError, columns, compute, keep, step, table  # noqa: E402
from simlab import build_virtual_lab  # noqa: E402
from test_web import Scripted  # noqa: E402

GATE = ("plan: gate\nsetup: {lockin.amplitude: 0.05, smu.output: on}\nsweep: {smu.V: {from: 0, to: 3, points: 61}}\n"
        "settle: 0.5\nmeasure: [lockin.X, smu.I]\nafter: {smu.V: 0}")
MAP = ("plan: map\nsetup: {lockin.amplitude: 0.05, smu.output: on, magnet.rate: 0.5}\n"
       "sweep:\n  magnet.B: {values: [0, 0.5, 1]}\n  smu.V: {from: 0, to: 3, step: 0.1}\nsettle: 0.5\n"
       "measure: [lockin.X]\nafter: {smu.V: 0, magnet.B: 0}")
LOOP = ("plan: loop\nsetup: {lockin.amplitude: 0.05, smu.output: on, magnet.rate: 0.5}\n"
        "sweep: {magnet.B: {from: -1, to: 1, step: 0.05, back: true}}\nsettle: 0.5\nmeasure: [lockin.X]\n"
        "after: {magnet.B: 0}")
REPEATED = ("plan: repeated\nsetup: {lockin.amplitude: 0.05, smu.output: on}\n"
            "sweep: {smu.V: {from: 0, to: 3, points: 11}}\nrepeat: 3\nsettle: 0.5\nmeasure: [lockin.X]\nafter: {smu.V: 0}")
VNA = ("plan: vna\nsetup: {vna.rf: on, vna.power: -20, vna.f_start: 3.99e9, vna.f_stop: 4.01e9, vna.points: 401}\n"
       "measure: [vna.S21]")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def rt(tmp_path):
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 200), tmp_path / "data")


def last(rt):
    return rt.pick_runs(last=1)[-1]


def fit_of(out, curve=0):
    f = out["curves"][curve]["fit"]
    assert not f.get("error"), f.get("error")
    return {p["name"]: p for p in f["params"]}


def portal(rt, tmp_path, store=None):
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, store or ChatStore(tmp_path / "chats"), hosts=["testserver"]))
    return client, {"X-Kelvin-Token": settings.access_key}


def _js(name):
    """One function of app.js, by its name."""
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


# ------------------------------------------------------------------ 1. symmetrize counts each point once

def test_a_symmetrized_curve_keeps_one_point_per_x_so_a_fit_s_errors_are_not_shrunk(rt):
    up = np.linspace(-1, 1, 41)
    x = np.concatenate([up, up[::-1][1:] + 0.002])   # a loop: the way back a little off the way up
    y = 2 + 0.2 * x ** 2 + 0.05 * x
    for op, odd in (("symmetrize", False), ("antisymmetrize", True)):
        sx, sy, _, _, _, _ = step(np, x, y, "T", "V", "B", "R", {"op": op})
        assert sx.min() >= 0 and len(set(np.round(np.abs(sx), 9))) == sx.size   # each |x| once, never x and -x
        assert sx.size <= 42 and (sx.min() > 0 if odd else True)
        assert np.allclose(sy, 0.05 * sx if odd else 2 + 0.2 * sx ** 2, atol=1e-3)
    rt.run(LOOP)
    run = last(rt)
    model = {"model": "R0*(1 + a*x**2)", "start": {"R0": 1e-5, "a": 0.1}}
    raw = compute(rt, {"runs": [run], "x": "magnet.B", "y": "lockin.X", "fit": model})
    sym = compute(rt, {"runs": [run], "x": "magnet.B", "y": "lockin.X", "steps": [{"op": "symmetrize"}], "fit": model})
    assert raw["curves"][0]["n"] == 81 and sym["curves"][0]["n"] == 21   # -1 → 1 → -1 T by 0.05 T: 0 … 1 T once
    a_raw, a_sym = fit_of(raw)["a"], fit_of(sym)["a"]
    assert a_sym["value"] == pytest.approx(a_raw["value"], rel=1e-2)
    assert a_sym["error"] > 0.8 * a_raw["error"]   # counting x and -x both made it 0.6 times as large


# ------------------------------------------------------------------ 2. the CSV has every curve

def test_the_csv_has_every_curve_not_only_the_ones_drawn(rt, tmp_path, monkeypatch):
    rt.run(MAP)
    run = last(rt)
    spec = {"runs": [run], "x": "magnet.B", "y": "lockin.X"}   # a curve per gate voltage: 31 of them
    drawn = compute(rt, spec)
    assert len(drawn["curves"]) == 24 and "31 curves; the first 24 are drawn" in drawn["notes"]
    _, text = table(rt, spec)
    head = text.splitlines()[0].split(",")
    assert len(head) == 62 and head[-1].endswith("smu.V = 3 V)")
    client, h = portal(rt, tmp_path)
    r = client.post("/api/analysis/csv", json={"spec": spec}, headers=h)
    assert r.status_code == 200 and len(r.text.splitlines()[0].split(",")) == 62
    monkeypatch.setattr("labharness.workbench.MAX_TABLE", 10)   # more than a CSV takes: said, never cut short
    with pytest.raises(AnalysisError, match="31 curves"):
        table(rt, spec)


# ------------------------------------------------------------------ 3. units

def test_units_follow_expressions_scales_and_derivatives(rt):
    rt.run(GATE)
    run = last(rt)

    def unit(y, steps=(), x="smu.V"):
        out = compute(rt, {"runs": [run], "x": x, "y": y, "steps": list(steps)})
        return out["axes"]["y_unit"], out["notes"]
    assert unit("lockin.X / 1e-6") == ("µV", []) and unit("lockin.X * 1e6") == ("µV", [])
    assert unit("sqrt(lockin.X**2 + lockin.X**2)") == ("V", []) and unit("lockin.X / smu.I") == ("Ω", [])
    assert unit("deriv(lockin.X, smu.V)") == ("", [])   # V/V: a ratio, truly without a unit
    assert unit("lockin.X", [{"op": "deriv"}]) == unit("deriv(lockin.X, smu.V)")   # the step and the expression agree
    assert unit("lockin.X", [{"op": "deriv"}, {"op": "deriv"}], x="smu.V * 1e3")[0] == "V/mV²"
    assert unit("lockin.X", [{"op": "scale", "factor": 1e6}])[0] == "µV"
    assert unit("lockin.X", [{"op": "normalize"}, {"op": "scale", "factor": 100, "offset": -100}])[0] == "%"
    said, notes = unit("lockin.X / 2e-8")   # the volts over a current: the number may have a unit of its own
    assert said == "" and notes == ["the unit of y (lockin.X / 2e-8) can't be told: a scale step's unit gives it"]
    assert unit("lockin.X", [{"op": "scale", "factor": 5e7, "unit": "Ω"}]) == ("Ω", [])
    peak = fit_of(compute(rt, {"runs": [run], "y": "lockin.X / 1e-6", "fit": {"model": "lorentzian"}}))
    assert (peak["A"]["unit"], peak["x0"]["unit"], peak["fwhm"]["unit"]) == ("µV", "V", "V")
    own = fit_of(compute(rt, {"runs": [run], "y": "lockin.X / 1e-6", "fit": {
        "model": "y0 + A/(1 + ((x - x0)/w)**2)", "start": {"y0": 0, "A": 9, "x0": 1.5, "w": 0.8}}}))
    assert {k: p["unit"] for k, p in own.items()} == {"y0": "µV", "A": "µV", "x0": "V", "w": "V"}


def test_the_unit_algebra():
    assert units.parse("V/T/T").text() == "V/T²" and units.parse("cm²/(V·s)").text() == "cm²/(V·s)"
    assert units.parse("uV").text() == "µV" and units.parse("kOhm").text() == "kΩ"
    assert units.parse("/16384 turn").text() == "/16384 turn" and units.parse("a.u.").text() == "a.u."
    assert units.per("V", "T") == "V/T" and units.per("A", "V") == "S" and units.per("V", "V") == ""
    assert units.times("V", "s") == "V·s" and units.inverse("ms") == "kHz" and units.inverse("T") == "1/T"
    assert units.scaled("V", 1e-3) == "kV" and units.scaled("", 100) == "%" and units.scaled("V", 2) is None
    assert units.scaled(None, 1e6) is None and units.per(None, "V") is None
    cols = {"lockin.X": "V", "smu.I": "mA", "tc.T": "K", "R": "Ω"}.__getitem__
    assert units.of("lockin.X / smu.I", cols) == "V/mA" and units.of("smu.I * R", cols) == "mA·Ω"
    assert units.of("lockin.X + tc.T", cols) is None and units.of("log10(abs(lockin.X))", cols) == ""
    assert units.of("lockin.X / 1e-3 / R", cols) == "mA" and units.of("2*pi*tc.T", cols) is None
    assert units.model_units("a*exp(-x/tau) + c", ["a", "tau", "c"], "s", "µV") == {"a": "µV", "tau": "s", "c": "µV"}
    assert units.model_units("R0*(1 + a*x**2)", ["R0", "a"], "T", "Ω") == {"R0": "Ω", "a": "1/T²"}
    assert units.model_units("a*x + b", ["a", "b"], "V", None) == {}   # y's unit unknown: nothing guessed


# ------------------------------------------------------------------ 5. i, t and repeat

def test_i_t_and_repeat_are_columns_to_choose(rt):
    rt.run(REPEATED)
    run = last(rt)
    others = {c["key"]: c["unit"] for c in columns(rt, run)["others"]}
    assert others == {"i": "", "t": "s", "repeat": ""}
    out = compute(rt, {"runs": [run], "x": "smu.V", "y": "lockin.X", "group": "repeat"})
    assert [c["label"].split(" · ")[1] for c in out["curves"]] == ["repeat = 1", "repeat = 2", "repeat = 3"]
    assert compute(rt, {"runs": [run], "x": "smu.V", "y": "lockin.X", "where": "i < 11"})["curves"][0]["n"] == 11
    assert "cols.others" in _js("anColumns")   # suggested under x, y and per


# ------------------------------------------------------------------ 6. a map grouped by the server

def _turn(tmp_path, rt, turns):
    provider = Scripted(turns)
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")
            await agent.run_turn(chat["id"], "fit the map", "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    return events


def test_a_map_is_a_curve_per_outer_value_for_the_api_kelvins_tool_and_keep(rt, tmp_path):
    rt.run(MAP)
    run = last(rt)
    out = compute(rt, {"runs": [run], "y": "lockin.X", "fit": {"model": "lorentzian"}})
    assert [c["label"].split(" · ")[1] for c in out["curves"]] == ["magnet.B = 0 T", "magnet.B = 500 mT",
                                                                    "magnet.B = 1 T"]
    assert out["group"] == "magnet.B" and all(c["n"] == 31 for c in out["curves"])
    assert len(compute(rt, {"runs": [run], "group": ""})["curves"]) == 1   # "" asks for one curve
    across = compute(rt, {"runs": [run], "x": "magnet.B"})   # x the outer axis: a curve per gate voltage
    assert across["group"] == "smu.V" and across["curves"][0]["n"] == 3
    both = compute(rt, {"runs": [run], "group": "magnet.B, smu.V"})
    assert both["curves"][1]["label"].endswith("magnet.B = 0 T, smu.V = 100 mV") and both["curves"][1]["n"] == 1
    client, h = portal(rt, tmp_path)
    r = client.post("/api/analysis", json={"spec": {"runs": [run], "y": "lockin.X"}}, headers=h)
    assert r.status_code == 200 and len(r.json()["curves"]) == 3 and r.json()["group"] == "magnet.B"
    events = _turn(tmp_path, rt, [
        [{"type": "tool_call", "id": "a1", "name": "analyze",
          "input": {"runs": [run], "y": "lockin.X", "fit": {"model": "lorentzian"}}}],
        [{"type": "text", "text": "A peak at each field."}]])
    (shown,) = [e for e in events if e["type"] == "analysis"]
    assert len(shown["result"]["curves"]) == 3 and "group" not in shown["spec"]
    (end,) = [e for e in events if e["type"] == "tool_end"]
    assert "a curve per magnet.B" in end["content"] and end["content"].count("lorentzian fit") == 3
    said = keep(rt, {"runs": [run], "y": "lockin.X", "fit": {"model": "lorentzian"}}, curve=1)
    assert "/fits/lockin.X lorentzian @ magnet.B = 500 mT" in said
    (kept,) = fitting.stored(rt._run_dir(run))
    assert kept["spec"]["group"] == "magnet.B"
    spec = _js("anSpec")
    assert 'if (!ANA.auto.group) out.group = s.group || "";' in spec   # the pane leaves it to the server
