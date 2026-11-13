"""Analysis of data already recorded (the task's setup records it before the chat): a custom fit, a quantity worked
out from a spectrum, a ratio read off a cool-down."""
from __future__ import annotations

import math

from ..checks import answer_quantity, used_tool
from ..task import Task
from .nv import GAMMA, echo_plan, lines, odmr_plan, t_pi


def _echo_setup(env) -> None:
    p = env.world.p
    env.runtime.run(echo_plan(lines(p)[0], t_pi(p), 3.2 * p["T2"], points=81, integration=5.0, name="hahn_echo"))


FIT_CODE = r'''
import numpy as np
r = run
tau, c = r.data["pg.tau"], r.data["counter.contrast"]
A = np.median(c[-10:])                  # the plateau
B = np.median(c[:3]) - A                # the amplitude
ok = (c - A) / B
m = (ok > 0.05) & (ok < 0.95)
x, y = np.log(tau[m]), np.log(-np.log(ok[m]))
n, b = np.polyfit(x, y, 1)              # ln(-ln((C-A)/B)) = n ln tau - n ln T2
print("n = %.3f, T2 = %.4g s" % (n, np.exp(-b / n)))
'''

ECHO = Task(
    id="fit-echo-stretched", category="analysis", title="Custom fit: stretched-exponential echo decay",
    prompt=("Earlier today I recorded a Hahn echo on the NV ensemble (it's the only echo run in the data). Fit its "
            "contrast against pg.tau to C(tau) = A + B * exp(-(tau/T2)^n), with the stretch exponent n free, and "
            "give me T2 and n with their uncertainties."),
    params=lambda r: {"D": 2.870e9, "E": 2.5e6, "B_bias": r.uniform(3e-3, 5e-3), "rabi_0dBm": 5e6,
                      "T2": r.uniform(25e-6, 70e-6), "echo_n": r.uniform(1.4, 2.8), "R_max": 1.5e6, "contrast": 0.3},
    initial=lambda p: {"magnet": {"B": p["B_bias"], "B_actual": p["B_bias"], "rate": 0.05}},
    setup=_echo_setup,
    checks=[
        answer_quantity("T2 within 12 %", lambda ep: ep.p["T2"], "s", [r"T_?2", r"T₂", r"coheren"], rel=0.12),
        answer_quantity("stretch exponent n within 0.35", lambda ep: ep.p["echo_n"], "",
                        [r"\bn\s*[=≈~:]", r"exponent", r"stretch", r"\bn\b"], abs_=0.35),
        used_tool("fitted with Kelvin's fit or Python tools", "fit_run", "run_python", "analyze_run", "compute"),
    ],
    oracle=[{"tool": "find_runs", "input": {"text": "hahn_echo"}},
            {"tool": "run_python", "input": {"code": FIT_CODE}}],
    oracle_answer=lambda ctx: "The stretched-exponential fit gives **T2 = %.1f ± 2 us** and **n = %.2f ± 0.1**." % (
        ctx.p["T2"] * 1e6, ctx.p["echo_n"]),
    tags=["pilot", "nv", "fit"],
)


def _odmr_setup(env) -> None:
    env.runtime.run(odmr_plan(2.55e9, 3.20e9, 1301, "odmr_wide"))


FIELD = Task(
    id="odmr-bias-field", category="analysis", title="The bias field from an ODMR spectrum",
    prompt=("I took a wide CW ODMR scan of the NV ensemble earlier today (the only ODMR run in the data). From the "
            "two resonances in it, what is the magnetic field along the NV axis? Use gamma = 28 GHz/T for the NV's "
            "gyromagnetic ratio."),
    params=lambda r: {"D": r.uniform(2.866e9, 2.874e9), "E": r.uniform(1.5e6, 4.5e6), "B_bias": r.uniform(2e-3, 9e-3),
                      "R_max": 6e5},
    initial=lambda p: {"magnet": {"B": p["B_bias"], "B_actual": p["B_bias"], "rate": 0.05}},
    setup=_odmr_setup,
    checks=[answer_quantity("field within 3 %", lambda ep: ep.p["B_bias"], "T",
                            [r"field", r"\bB\b", r"B_?NV", r"B\s*[=≈~]", r"tesla", r"axis"], rel=0.03)],
    oracle=[{"tool": "find_runs", "input": {"text": "odmr"}}],
    oracle_answer=lambda ctx: "The lines are %.1f MHz apart, so the field along the NV axis is **B = %.2f mT**." % (
        (lines(ctx.p)[1] - lines(ctx.p)[0]) / 1e6, ctx.p["B_bias"] * 1e3),
    tags=["nv"],
)


RRR_PLAN = """plan: cooldown_from_room
setup: { lockin.amplitude: 0.1, lockin.tc: 0.1 }
sweep: { tc.setpoint: { ramp: [ { to: 2 } ], every: 2 } }
measure: [lockin.X, tc.T_sample]"""


def rrr(p: dict) -> float:
    def rn(t: float) -> float:
        return (p["R_res"] + p["R_slope"] * t) * 0.5 * (1 + math.tanh((t - p["Tc"]) / p["Tc_width"]))
    return rn(295.0) / rn(10.0)


RRR = Task(
    id="rrr-from-cooldown", category="analysis", title="Residual resistance ratio from a recorded cool-down",
    prompt=("This morning I recorded the film cooling from room temperature to 2 K (it's the only cool-down run). What "
            "is its residual resistance ratio, R(295 K) / R(10 K)?"),
    params=lambda r: {"sample": "film", "Tc": r.uniform(4.0, 8.0), "R_res": r.uniform(10.0, 60.0), "T_tau": 120.0},
    initial={"tc": {"heater_range": "high", "setpoint": 300.0, "T_sample": 300.0}},
    setup=lambda env: env.runtime.run(RRR_PLAN),
    checks=[answer_quantity("RRR within 5 %", lambda ep: rrr(ep.p), "", [r"RRR", r"ratio", r"R\s*\(\s*295", r"≈", r"="],
                            rel=0.05)],
    oracle=[{"tool": "find_runs", "input": {"text": "cooldown"}}],
    oracle_answer=lambda ctx: "R(295 K)/R(10 K) = **%.2f**." % rrr(ctx.p),
    tags=["cryo"],
)


TASKS = [ECHO, FIELD, RRR]
