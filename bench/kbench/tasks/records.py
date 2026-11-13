"""Questions about earlier runs: the setup records a day's work (gate sweeps at three fields with different lock-in
settings, a leakage check the gate's guard stopped, an ODMR scan), and the person asks about it."""
from __future__ import annotations

from ..checks import answer_quantity, answer_says, no_lab_changes
from ..task import Task

DAY = [
    ("gate_sweep_0T", 0.0, 0.1, 0.05),
    ("gate_sweep_0p5T", 0.5, 0.1, 0.05),
    ("gate_sweep_1T", 1.0, 0.3, 0.02),
]
SWEEP = """plan: {name}
setup: {{ lockin.amplitude: {amp}, lockin.tc: {tc}, smu.output: on, magnet.rate: 0.5, magnet.B: {b} }}
sweep: {{ smu.V: {{ from: 0, to: 4, step: 0.05 }} }}
settle: 0.3
measure: [lockin.X, smu.I]
after: {{ smu.V: 0 }}"""
LEAK = """plan: leakage_check
setup: { smu.output: on }
sweep: { smu.V: { from: 0, to: 14, step: 0.05 } }
settle: 0.2
measure: [smu.I]
after: { smu.V: 0 }"""
ODMR = """plan: odmr_check
setup: { laser.emission: on, laser.shutter: open, laser.power: 2, mw.output: on, mw.power: -10 }
sweep: { mw.frequency: { from: 2.80e9, to: 2.94e9, points: 141 } }
measure: [counter.rate]
after: { mw.output: off, laser.shutter: closed, laser.emission: off }"""


def day_params(r) -> dict:
    return {"Vcnp": r.uniform(1.0, 3.0), "R_peak": r.uniform(6e3, 12e3), "R_bg": r.uniform(0.5e3, 1.5e3),
            "mr": r.uniform(0.15, 0.6), "V_bd": r.uniform(10.5, 13.0)}


def record_day(env) -> None:
    rt = env.runtime
    for name, b, tc, amp in DAY:
        rt.run(SWEEP.format(name=name, b=b, tc=tc, amp=amp))
    rt.run(ODMR)
    rt.run(LEAK)   # the gate's guard stops it where the leakage passes 1 nA
    rt.run("plan: back_to_zero\nsetup: { magnet.B: 0, smu.V: 0 }\nmeasure: [smu.I]")


def peak_at(p: dict, b: float) -> float:
    return (p["R_bg"] + p["R_peak"] * (1 + p["mr"] * b * b)) / (1 + p["T_base"] / p["T_scale"])


PEAK = Task(
    id="records-peak-field", category="records", title="Which of today's gate sweeps peaked highest",
    prompt=("Earlier today I took gate sweeps of the sample at a few magnetic fields (lock-in, through the 10 MOhm "
            "series resistor). Which field gave the highest peak resistance, and what was that resistance? Don't "
            "measure anything new, just look at the data."),
    params=day_params, setup=record_day,
    checks=[
        answer_says("names 1 T as the field", r"\b1(\.0+)?\s*T\b", r"\b1000\s*mT\b"),
        answer_quantity("peak resistance at 1 T within 5 %", lambda ep: peak_at(ep.p, 1.0), "Ohm",
                        [r"resist", r"peak", r"R\b", r"max", r"highest"], rel=0.05),
        no_lab_changes("measured nothing new"),
    ],
    oracle=[{"tool": "find_runs", "input": {"text": "gate_sweep"}}],
    oracle_answer=lambda ctx: "The 1 T sweep peaked highest: **%.3g kOhm** at its charge-neutrality peak." % (
        peak_at(ctx.p, 1.0) / 1e3),
    tags=["records", "pilot"],
)


STOPPED = Task(
    id="records-stopped-run", category="records", title="Which run stopped early, why, and where",
    prompt=("One of today's runs stopped early. Which one was it, why did it stop, and at what gate voltage? Please "
            "don't run anything, just check the records."),
    params=day_params, setup=record_day,
    checks=[
        answer_says("names the leakage check", r"leakage[_ ]check"),
        answer_says("says the leakage or the guard stopped it", r"leak", r"guard", r"1\s*nA", r"current"),
        answer_quantity("the gate voltage where it stopped, within 0.15 V", lambda ep: ep.p["V_bd"] + 1e-9 / ep.p["bd_slope"],
                        "V", [r"stop", r"at\b", r"gate", r"V_?g", r"reached", r"trip"], abs_=0.15),
        no_lab_changes("ran nothing"),
    ],
    oracle=[{"tool": "find_runs", "input": {"status": "aborted"}}],
    oracle_answer=lambda ctx: ("The **leakage_check** run stopped early: the gate's guard tripped when the leakage "
                               "passed 1 nA, at **%.2f V**." % (ctx.p["V_bd"] + 0.005)),
    tags=["records"],
)


SETTINGS = Task(
    id="records-settings", category="records", title="The lock-in settings of an earlier run",
    prompt=("What lock-in time constant and excitation amplitude did I use for today's 1 T gate sweep? Just look it "
            "up."),
    params=day_params, setup=record_day,
    checks=[
        answer_quantity("time constant 0.3 s", 0.3, "s", [r"time\s*constant", r"\btc\b", r"τ", r"TC"], rel=0.01),
        answer_quantity("excitation 20 mV", 0.02, "V", [r"amplitude", r"excitation", r"drive", r"output"], rel=0.01),
        no_lab_changes("ran nothing"),
    ],
    oracle=[{"tool": "find_runs", "input": {"text": "gate_sweep_1T"}}],
    oracle_answer="The 1 T sweep used a **time constant of 0.3 s** and an **excitation amplitude of 20 mV**.",
    tags=["records"],
)


TASKS = [PEAK, STOPPED, SETTINGS]
