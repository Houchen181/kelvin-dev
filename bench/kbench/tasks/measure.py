"""Measurements a person asks for in one message: transport, the gate, the resonator, a cool-down."""
from __future__ import annotations

import math

from ..checks import a_run, answer_quantity, approvals_at_most, episode, final_setting
from ..task import Person, Task

IV_PLAN = """plan: gate_leakage_iv
setup: { smu.output: on, smu.compliance_I: 1.0e-9 }
sweep: { smu.V: { from: -10, to: 10, step: 0.5 } }
settle: 0.2
measure: [smu.I]
after: { smu.V: 0, smu.output: off }"""


def compliance_at_high_bias(ep) -> tuple:
    """The compliance in force whenever |V| was 9 V or more, from what reached the source-meter."""
    comp, worst, seen = 1.0e-4, 0.0, False
    for _, name, param, value in ep.truth.get("applied") or []:
        if name != "smu":
            continue
        if param == "compliance_I":
            comp = float(value)
        elif param == "V" and abs(float(value)) >= 9:
            seen, worst = True, max(worst, comp)
    return seen and worst <= 1.0e-9 * 1.0001, "compliance at |V| >= 9 V: %s" % ("%.3g A" % worst if seen else "never got there")


def back_to_zero(ep) -> tuple:
    v, out = ep.final("smu", "V"), ep.final("smu", "output")
    return (abs(float(v or 0)) < 1e-6 or not out), "smu.V %s V, output %s" % (v, out)


IV = Task(
    id="iv-leakage", category="measure", title="Gate leakage I-V with 1 nA compliance",
    prompt=("Please measure the gate leakage I-V of the mounted device from -10 V to +10 V with the source-meter's "
            "current compliance at 1 nA, and tell me the leakage resistance."),
    params=lambda r: {"R_leak": 10 ** r.uniform(11.6, 12.4), "V_bd": r.uniform(11.0, 13.0)},
    checks=[
        a_run("recorded smu.I while sweeping smu.V over -10...+10 V",
              lambda r: r.has("smu.I") and r.covers("smu.V", -10, 10, 0.51)),
        episode("compliance at 1 nA or less at high bias", compliance_at_high_bias),
        answer_quantity("leakage resistance within 15 %", lambda ep: ep.p["R_leak"], "Ohm",
                        [r"resist", r"leak", r"R\s*[_=≈~]", r"\bR\b"], rel=0.15),
        episode("left the gate at 0 V or its output off", back_to_zero, required=False, kind="process"),
        approvals_at_most(1),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": IV_PLAN}}],
    oracle_answer=lambda ctx: "The leakage is linear over +/-10 V: **R_leak = %.3g TOhm** (%.3g Ohm)." % (
        ctx.p["R_leak"] / 1e12, ctx.p["R_leak"]),
    tags=["transport"],
)


BREAKDOWN_PLAN = """plan: breakdown_onset
setup: { smu.output: on }
sweep: { smu.V: { from: 0, to: 14.5, step: 0.05, until: "abs(smu.I) > 1e-9" } }
settle: 0.2
measure: [smu.I]
after: { smu.V: 0 }"""


def onset(ep) -> float:
    return ep.p["V_bd"] + 1e-9 / ep.p["bd_slope"]


BREAKDOWN = Task(
    id="gate-breakdown-onset", category="measure", title="Find where the gate dielectric starts to leak",
    prompt=("I want to know at what gate voltage this device's dielectric starts to leak. Step the gate up from 0 V "
            "in steps of 50 mV and find the voltage where the leakage first goes above 1 nA - but don't destroy "
            "the device. What is that voltage?"),
    params=lambda r: {"V_bd": r.uniform(10.3, 13.2), "R_leak": 10 ** r.uniform(11.8, 12.3)},
    checks=[
        a_run("swept smu.V upward from near 0 V measuring smu.I", lambda r: r.has("smu.I") and r.covers("smu.V", 0.2, 10, 0.3)),
        answer_quantity("onset voltage within 0.15 V", onset, "V",
                        [r"onset", r"break", r"leak", r"exceed", r"above", r"1\s*nA", r"start", r"at\b"], abs_=0.15),
        episode("left the gate at 0 V or its output off", back_to_zero, required=False, kind="process"),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": BREAKDOWN_PLAN}}],
    oracle_answer=lambda ctx: "The leakage first exceeds 1 nA at **%.2f V**; I stopped there and brought the gate "
                              "back to 0 V." % (ctx.p["V_bd"] + 0.005),
    tags=["transport"],
)


GATE_PLAN = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, lockin.tc: 0.1, smu.output: on }
sweep: { smu.V: { from: 0, to: 5, step: 0.05 } }
settle: 0.3
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""


def r_peak(ep) -> float:
    p = ep.p
    return (p["R_bg"] + p["R_peak"]) / (1 + p["T_base"] / p["T_scale"])


GATE = Task(
    id="gate-peak", category="measure", title="Charge-neutrality peak from a lock-in gate sweep",
    prompt=("Sweep the back gate from 0 to 5 V and measure the sample's resistance with the lock-in at 50 mV "
            "excitation. Where is the charge-neutrality peak, and what is the resistance there?"),
    params=lambda r: {"Vcnp": r.uniform(0.8, 3.8), "R_peak": r.uniform(5e3, 15e3), "R_bg": r.uniform(0.5e3, 2e3)},
    checks=[
        a_run("recorded the lock-in over a 0-5 V gate sweep",
              lambda r: (r.has("lockin.X") or r.has("lockin.R")) and r.covers("smu.V", 0, 5, 0.1)),
        answer_quantity("peak gate voltage within 0.1 V", lambda ep: ep.p["Vcnp"], "V",
                        [r"peak", r"neutral", r"CNP", r"Dirac", r"V_?g", r"at\b"], abs_=0.1),
        answer_quantity("peak resistance within 10 %", r_peak, "Ohm", [r"resist", r"peak", r"R\b", r"max"], rel=0.10),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": GATE_PLAN}}],
    oracle_answer=lambda ctx: "The charge-neutrality peak is at **Vg = %.2f V**, where R = **%.3g kOhm**." % (
        ctx.p["Vcnp"], (ctx.p["R_bg"] + ctx.p["R_peak"]) / (1 + 1.6 / 50) / 1e3),
    tags=["pilot", "transport"],
)


def vna_plan(f0: float, span: float, points: int) -> str:
    return """plan: resonator_s21
setup: {{ vna.f_start: {a}, vna.f_stop: {b}, vna.points: {n}, vna.ifbw: 1000, vna.averages: 4, vna.power: -30, vna.rf: on }}
measure: [vna.S21]
after: {{ vna.rf: off }}""".format(a=f0 - span / 2, b=f0 + span / 2, n=points)


VNA = Task(
    id="vna-resonance", category="measure", title="Resonator frequency and linewidth on the VNA",
    prompt=("There's a superconducting resonator somewhere between 3.9 and 4.1 GHz on the VNA. Find its resonance "
            "frequency and its linewidth (FWHM) at zero field."),
    params=lambda r: {"f_res": r.uniform(3.93e9, 4.07e9), "kappa": r.uniform(0.8e6, 3.0e6)},
    checks=[
        a_run("recorded an S21 trace", lambda r: r.has("vna.S21") or any("S21" in k for k in r.keys), count=1),
        answer_quantity("resonance within 0.3 MHz", lambda ep: ep.p["f_res"], "Hz",
                        [r"resonan", r"f_?0", r"f_?r", r"centre", r"center", r"at\b"], abs_=0.3e6),
        answer_quantity("linewidth within 30 %", lambda ep: ep.p["kappa"], "Hz",
                        [r"width", r"FWHM", r"kappa", r"κ", r"Q\b"], rel=0.30),
    ],
    oracle=[lambda ctx: {"tool": "run_plan", "input": {"plan": vna_plan(4.0e9, 0.2e9, 2001)}},
            lambda ctx: {"tool": "run_plan", "input": {"plan": vna_plan(round(ctx.p["f_res"], -5), 20e6, 801)}}],
    oracle_answer=lambda ctx: "The resonance is at **f0 = %.4f GHz** with a linewidth (FWHM) of **%.2f MHz**." % (
        ctx.p["f_res"] / 1e9, ctx.p["kappa"] / 1e6),
    tags=["rf"],
)


FIELD_PLAN = """plan: field_gate_map
setup: { lockin.amplitude: 0.05, lockin.tc: 0.1, smu.output: on, magnet.rate: 0.5 }
sweep:
  magnet.B: { values: [0, 0.5, 1] }
  smu.V: { from: 0, to: 4, step: 0.05 }
settle: 0.3
measure: [lockin.X]
after: { smu.V: 0, magnet.B: 0 }"""


def mr_percent(ep) -> float:
    p = ep.p
    return 100.0 * p["R_peak"] * p["mr"] / (p["R_bg"] + p["R_peak"])


FIELD = Task(
    id="field-map", category="measure", title="Gate-field map and magnetoresistance at the peak",
    prompt=("Map the sample's resistance against gate voltage from 0 to 4 V at fields of 0, 0.5 and 1 T (lock-in, 50 mV "
            "excitation). By how many percent does the peak resistance grow from 0 to 1 T?"),
    params=lambda r: {"Vcnp": r.uniform(1.0, 3.0), "mr": r.uniform(0.15, 0.6), "R_peak": r.uniform(6e3, 12e3),
                      "R_bg": r.uniform(0.5e3, 1.5e3)},
    checks=[
        a_run("recorded the lock-in over the gate at 1 T", lambda r: (r.has("lockin.X") or r.has("lockin.R"))
              and r.covers("smu.V", 0, 4, 0.1) and (r.covers("magnet.B", 1.0, 1.0, 0.02) or "magnet" in r.plan_text)),
        answer_quantity("peak magnetoresistance within 3 points of %", mr_percent, "%",
                        [r"grow", r"increase", r"rise", r"magnetoresist", r"MR", r"by\b", r"higher", r"larger"], abs_=3.0),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": FIELD_PLAN}}],
    oracle_answer=lambda ctx: "From 0 to 1 T the peak resistance grows by **%.1f %%**." % (
        100.0 * ctx.p["R_peak"] * ctx.p["mr"] / (ctx.p["R_bg"] + ctx.p["R_peak"])),
    tags=["transport"],
)


RT_PLAN = """plan: cooldown_rt
setup: { lockin.amplitude: 0.1, lockin.tc: 0.1 }
sweep: { tc.setpoint: { ramp: [ { to: 2 } ], every: 0.5 } }
measure: [lockin.X, tc.T_sample]"""


RT = Task(
    id="rt-cooldown", category="measure", title="R(T) on a cool-down and the superconducting Tc",
    prompt=("The sample (a metal film) is at about 80 K. Cool it down to 2 K and record its resistance on the way "
            "down with the lock-in (100 mV excitation). What is its superconducting transition temperature Tc, "
            "taken where R falls to half its normal-state value?"),
    params=lambda r: {"sample": "film", "Tc": r.uniform(4.5, 9.5), "R_res": r.uniform(20, 80), "T_tau": 90.0},
    initial={"tc": {"heater_range": "high", "setpoint": 80.0, "T_sample": 80.0}},
    checks=[
        a_run("recorded the lock-in against temperature through the transition",
              lambda r: (r.has("lockin.X") or r.has("lockin.R")) and r.has("tc.T_sample")
              and r.covers("tc.T_sample", 3.9, 10.1, 0.2)),
        answer_quantity("Tc within 0.25 K", lambda ep: ep.p["Tc"], "K",
                        [r"T_?c\b", r"T₍?c", r"transition", r"critical", r"supercond", r"half"], abs_=0.25),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": RT_PLAN}}],
    oracle_answer=lambda ctx: "The film goes superconducting at **Tc = %.2f K** (half its normal resistance)." % ctx.p["Tc"],
    tags=["cryo"],
)


TASKS = [IV, BREAKDOWN, GATE, VNA, FIELD, RT]
