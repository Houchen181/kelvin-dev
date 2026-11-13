"""The NV centre: CW ODMR, Rabi, and the whole chain to T2 (find the line, calibrate the pi pulse, Hahn echo).

The NV set-up sits in a small permanent bias field (a few mT, drawn per seed), so its two lines are well apart, as in
most NV work; finding them means a wide scan first."""
from __future__ import annotations

import math

from ..checks import a_run, answer_quantity, episode
from ..task import Person, Task

GAMMA = 28.0e9


def nv_params(r) -> dict:
    return {"D": r.uniform(2.866e9, 2.874e9), "E": r.uniform(1.5e6, 4.5e6), "B_bias": r.uniform(3e-3, 6e-3),
            "rabi_0dBm": r.uniform(3.5e6, 6.5e6), "T2": r.uniform(20e-6, 80e-6), "R_max": r.uniform(6e5, 1e6),
            "contrast": r.uniform(0.15, 0.25)}


def bias(p: dict) -> dict:
    return {"magnet": {"B": p["B_bias"], "B_actual": p["B_bias"], "rate": 0.05}}


def lines(p: dict) -> tuple:
    s = math.sqrt(p["E"] ** 2 + (GAMMA * p["B_bias"]) ** 2)
    return p["D"] - s, p["D"] + s


def t_pi(p: dict, dbm: float = 0.0) -> float:
    return 1.0 / (2.0 * p["rabi_0dBm"] * 10 ** (dbm / 20.0))


LIGHT_ON = "laser.emission: on, laser.shutter: open, laser.power: 2"
LIGHT_OFF = "laser.shutter: closed, laser.emission: off"


def odmr_plan(lo: float, hi: float, points: int, name: str = "cw_odmr") -> str:
    return """plan: {name}
setup: {{ {on}, mw.output: on, mw.power: -10 }}
sweep: {{ mw.frequency: {{ from: {lo:.6e}, to: {hi:.6e}, points: {n} }} }}
measure: [counter.rate]
after: {{ mw.output: off, {off} }}""".format(name=name, on=LIGHT_ON, off=LIGHT_OFF, lo=lo, hi=hi, n=points)


def rabi_plan(f: float, dbm: float = 0.0, to: float = 4e-7, points: int = 81) -> str:
    return """plan: rabi
setup: {{ {on}, mw.output: on, mw.power: {p}, mw.frequency: {f:.6e}, pg.protocol: rabi, pg.running: on }}
sweep: {{ pg.tau: {{ from: 0, to: {to:.3e}, points: {n} }} }}
measure: [counter.signal, counter.reference, counter.contrast]
after: {{ pg.running: off, mw.output: off, {off} }}""".format(on=LIGHT_ON, off=LIGHT_OFF, p=dbm, f=f, to=to, n=points)


def echo_plan(f: float, tpi: float, to: float, points: int = 61, integration: float = 2.0, name: str = "hahn_echo") -> str:
    return """plan: {name}
setup: {{ {on}, mw.output: on, mw.power: 0, mw.frequency: {f:.6e}, pg.protocol: hahn_echo, pg.t_pi: {tpi:.4e}, counter.integration: {i}, pg.running: on }}
sweep: {{ pg.tau: {{ from: 1.0e-6, to: {to:.4e}, points: {n} }} }}
measure: [counter.signal, counter.reference, counter.contrast]
after: {{ pg.running: off, mw.output: off, {off} }}""".format(name=name, on=LIGHT_ON, off=LIGHT_OFF, f=f, tpi=tpi, to=to,
                                                        n=points, i=integration)


def swept_mw(r) -> bool:
    return r.has("mw.frequency") and (r.has("counter.rate") or r.has("counter.contrast") or r.has("counter.signal"))


def swept_tau(protocol: str):
    return lambda r: r.has("pg.tau") and protocol in r.plan_text and (r.has("counter.contrast") or r.has("counter.signal"))


LOWER_WORDS = [r"lower", r"f\s*[_-]?\s*(?:1|-|−|minus|low)", r"m_?s\s*=\s*-1", r"first", r"resonan", r"line", r"dip",
               r"\bat\b", r"and\b"]
UPPER_WORDS = [r"upper", r"higher", r"f\s*[_+]?\s*(?:2|\+|plus|high)", r"m_?s\s*=\s*\+1", r"second", r"resonan", r"line",
               r"dip", r"\bat\b", r"and\b"]

ODMR = Task(
    id="nv-odmr", category="measure", title="NV resonances with CW ODMR in a small bias field",
    prompt=("Find the NV ensemble's two spin resonances with CW ODMR and tell me both frequencies. There's a small "
            "permanent bias field on it (a few mT), so they won't be at 2.87 GHz."),
    params=nv_params, initial=bias,
    checks=[
        a_run("recorded the counts against the microwave frequency", swept_mw),
        answer_quantity("lower resonance within 0.5 MHz", lambda ep: lines(ep.p)[0], "Hz", LOWER_WORDS, abs_=0.5e6),
        answer_quantity("upper resonance within 0.5 MHz", lambda ep: lines(ep.p)[1], "Hz", UPPER_WORDS, abs_=0.5e6),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": odmr_plan(2.65e9, 3.10e9, 901, "odmr_wide")}},
            lambda ctx: {"tool": "run_plan", "input": {"plan": odmr_plan(lines(ctx.p)[0] - 5e6, lines(ctx.p)[0] + 5e6, 101, "odmr_lower")}},
            lambda ctx: {"tool": "run_plan", "input": {"plan": odmr_plan(lines(ctx.p)[1] - 5e6, lines(ctx.p)[1] + 5e6, 101, "odmr_upper")}}],
    oracle_answer=lambda ctx: "The two NV resonances are at **%.4f GHz** (lower) and **%.4f GHz** (upper)." % (
        lines(ctx.p)[0] / 1e9, lines(ctx.p)[1] / 1e9),
    tags=["nv"],
)


RABI = Task(
    id="nv-rabi", category="measure", title="Rabi oscillations and the pi time on the lower NV line",
    prompt=("Measure Rabi oscillations on the NV ensemble's lower spin resonance with the microwave at 0 dBm, and "
            "tell me the pi-pulse length. There's a small bias field on the NV (a few mT), so find the resonance "
            "first."),
    params=nv_params, initial=bias,
    checks=[
        a_run("recorded a Rabi sweep of the pulse length", swept_tau("rabi")),
        answer_quantity("pi time within 12 %", lambda ep: t_pi(ep.p), "s", [r"pi", r"π", r"t_?pi", r"t_?π", r"pulse"],
                        rel=0.12),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": odmr_plan(2.65e9, 3.10e9, 901, "odmr_wide")}},
            lambda ctx: {"tool": "run_plan", "input": {"plan": rabi_plan(lines(ctx.p)[0])}}],
    oracle_answer=lambda ctx: "At 0 dBm on the lower line (%.4f GHz) the pi pulse is **%.0f ns**." % (
        lines(ctx.p)[0] / 1e9, t_pi(ctx.p) * 1e9),
    tags=["nv"],
)


CHAIN = Task(
    id="nv-t2-chain", category="measure", title="NV chain: ODMR, then Rabi, then T2 with a Hahn echo",
    prompt=("Please characterise the NV ensemble: find its lower spin resonance with CW ODMR, calibrate the pi pulse "
            "with a Rabi measurement at 0 dBm, then measure its coherence time T2 with a Hahn echo. Report the "
            "resonance frequency, the pi time and T2. (There's a small permanent bias field on the NV, a few mT.)"),
    params=nv_params, initial=bias,
    checks=[
        a_run("recorded CW ODMR", swept_mw),
        a_run("recorded a Rabi sweep", swept_tau("rabi")),
        a_run("recorded a Hahn echo out past T2", lambda r: swept_tau("hahn_echo")(r)),
        answer_quantity("resonance within 0.5 MHz", lambda ep: lines(ep.p)[0], "Hz", LOWER_WORDS, abs_=0.5e6),
        answer_quantity("pi time within 15 %", lambda ep: t_pi(ep.p), "s", [r"pi", r"π", r"t_?pi", r"pulse"], rel=0.15),
        answer_quantity("T2 within 25 %", lambda ep: ep.p["T2"], "s", [r"T_?2(?!\*)", r"T₂", r"coheren", r"echo"],
                        rel=0.25),
        episode("echo used the measured pi time (within 15 %)", lambda ep: _echo_pi(ep), kind="process",
                required=False),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": odmr_plan(2.65e9, 3.10e9, 901, "odmr_wide")}},
            lambda ctx: {"tool": "run_plan", "input": {"plan": odmr_plan(lines(ctx.p)[0] - 5e6, lines(ctx.p)[0] + 5e6, 101, "odmr_lower")}},
            lambda ctx: {"tool": "run_plan", "input": {"plan": rabi_plan(lines(ctx.p)[0])}},
            lambda ctx: {"tool": "run_plan", "input": {"plan": echo_plan(lines(ctx.p)[0], t_pi(ctx.p), 3 * ctx.p["T2"])}}],
    oracle_answer=lambda ctx: ("Lower resonance **%.4f GHz**; pi pulse **%.0f ns** at 0 dBm; Hahn-echo **T2 = %.1f us**."
                               % (lines(ctx.p)[0] / 1e9, t_pi(ctx.p) * 1e9, ctx.p["T2"] * 1e6)),
    max_minutes=35, tags=["pilot", "nv", "long"],
)


def _echo_pi(ep) -> tuple:
    import re
    true = t_pi(ep.p)
    for r in ep.runs_by(swept_tau("hahn_echo")):
        m = re.search(r"pg\.t_pi:\s*([0-9.eE+-]+)", r.plan_text)
        if m:
            v = float(m.group(1))
            return abs(v - true) <= 0.15 * true, "echo t_pi %.3g s, true %.3g s" % (v, true)
    return False, "no echo with its t_pi in the plan"


TASKS = [ODMR, RABI, CHAIN]
