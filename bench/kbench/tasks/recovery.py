"""Getting from a failure to the result: a plan that doesn't validate, an interlock that needs the cryostat cold first,
an instrument that drops out in the middle of a sweep."""
from __future__ import annotations

from ..checks import a_run, answer_quantity, answer_says, episode
from ..task import Person, Task, Turn

BROKEN = """plan: quick_gate_check
setup: { lockin.amplitude: 0.05, lockin.tc: 0.1, smu.output: on }
sweep: { smu.V: { from: 0, to: 3, step: 0.05 } }
settle: 0.3
measure: [lockin.x, smu.current]
after: { smu.V: 0 }"""

FIXED = BROKEN.replace("lockin.x", "lockin.X").replace("smu.current", "smu.I")

TYPOS = Task(
    id="plan-typos", category="recovery", title="Run a person's plan that doesn't validate",
    prompt="Can you run this plan for me?\n\n```yaml\n%s\n```\n\nThen tell me where the resistance peaks." % BROKEN,
    params=lambda r: {"Vcnp": r.uniform(0.6, 2.4)},
    checks=[
        a_run("ran the sweep it meant: lock-in X and smu.I over 0-3 V",
              lambda r: (r.has("lockin.X") or r.has("lockin.R")) and r.has("smu.I") and r.covers("smu.V", 0, 3, 0.1)),
        answer_quantity("peak gate voltage within 0.1 V", lambda ep: ep.p["Vcnp"], "V",
                        [r"peak", r"neutral", r"max", r"at\b"], abs_=0.1),
        answer_says("says what it changed in the plan", r"lockin\.X", r"smu\.I", r"typo", r"fix", r"correct",
                    r"chang", r"renam", where="all", kind="process", required=False),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": BROKEN}}, {"tool": "run_plan", "input": {"plan": FIXED}}],
    oracle_answer=lambda ctx: ("Your plan named lockin.x and smu.current, which don't exist; I ran it with lockin.X and "
                               "smu.I. The resistance peaks at **Vg = %.2f V**." % ctx.p["Vcnp"]),
    tags=["transport"],
)


FIELD_MAP = """plan: field_sweep
setup: { lockin.amplitude: 0.05, lockin.tc: 0.1, smu.output: on, smu.V: {v}, magnet.rate: 0.5 }
sweep: { magnet.B: { from: 0, to: 1, step: 0.05 } }
settle: 0.5
measure: [lockin.X, tc.T_sample]
after: { magnet.B: 0, smu.V: 0 }"""


def magnet_moved_cold(ep) -> tuple:
    # stepped (axis magnet.B) or ramped continuously (axis magnet.B_actual, read as the field goes): either is the sweep
    for r in ep.runs_by(lambda r: any(r.has(k) and r.covers(k, 0, 1, 0.05) for k in ("magnet.B", "magnet.B_actual"))):
        if r.has("tc.T_sample") and r.span("tc.T_sample") and r.span("tc.T_sample")[1] < 10:
            return True, "%s swept B over 0-1 T at T <= %.2f K" % (r.id, r.span("tc.T_sample")[1])
        if not r.has("tc.T_sample"):
            return True, "%s swept B over 0-1 T (the bench saw no warm ramp)" % r.id
    return False, "no completed 0-1 T field sweep"


INTERLOCK = Task(
    id="interlock-cool-first", category="recovery", title="A field sweep the magnet's interlock refuses while warm",
    prompt=("Sweep the field from 0 to 1 T at a gate voltage of 1.5 V and record the sample's resistance with the "
            "lock-in (50 mV excitation)."),
    params=lambda r: {"mr": r.uniform(0.2, 0.5), "T_tau": 20.0},
    initial={"tc": {"heater_range": "medium", "setpoint": 15.0, "T_sample": 15.0}},
    person=Person(replies=[(r"cool|temperature|below 10|interlock", "Yes, cool it down first, then do the sweep.")]),
    checks=[
        episode("recorded the 0-1 T field sweep below 10 K", magnet_moved_cold),
        answer_says("mentions the interlock or the temperature", r"interlock", r"10\s*K", r"cool", r"temperature",
                    where="all", kind="process", required=False),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": FIELD_MAP.replace("{v}", "1.5")}},
            {"tool": "set_value", "input": {"instrument": "tc", "parameter": "setpoint", "value": 2}},
            {"tool": "wait", "input": {"seconds": 120}},
            {"tool": "run_plan", "input": {"plan": FIELD_MAP.replace("{v}", "1.5")}}],
    oracle_answer=("The magnet's interlock needs the sample below 10 K, so I cooled to 2 K first; the 0-1 T sweep is "
                   "recorded."),
    tags=["cryo", "transport"],
)


SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, lockin.tc: 0.1, smu.output: on }
sweep: { smu.V: { from: 0, to: 5, step: 0.05 } }
settle: 0.3
measure: [lockin.X]
after: { smu.V: 0 }"""


def _dropout(env) -> None:
    """The lock-in stops answering (a GPIB cable knocked loose) partway through the first sweep."""
    def drop(world) -> None:
        world.twins["lockin"].fault = "offline"
        world.note("lock-in stops answering (cable knocked loose)")
    env.world.at("lock-in drops out", lambda w: w.reads.get("lockin", 0) >= 45, drop)


def _plug_back(env) -> None:
    env.world.twins["lockin"].fault = None
    env.world.note("lock-in plugged back in")


def complete_after_fault(ep) -> tuple:
    """A sweep over all of 0-5 V finished after the dropout, or the runs after it cover 0-5 V together."""
    done = [r for r in ep.runs if r.done and (r.has("lockin.X") or r.has("lockin.R"))]
    full = [r for r in done if r.covers("smu.V", 0, 5, 0.06)]
    if full:
        return True, "complete: %s" % [r.id for r in full]
    lo = min((r.span("smu.V")[0] for r in done if r.span("smu.V")), default=None)
    hi = max((r.span("smu.V")[1] for r in done if r.span("smu.V")), default=None)
    return bool(done) and lo is not None and lo <= 0.06 and hi >= 4.94, "finished runs cover %s-%s V" % (lo, hi)


DROPOUT = Task(
    id="lockin-dropout", category="recovery", title="The lock-in drops out mid-sweep; finish once it's back",
    prompt="Sweep the gate from 0 to 5 V and record the lock-in signal (50 mV excitation). Where does it peak?",
    params=lambda r: {"Vcnp": r.uniform(3.0, 4.6)},
    setup=_dropout,
    person=Person(later=[Turn("Sorry, someone knocked the lock-in's GPIB cable loose. It's plugged back in now; "
                              "please finish the measurement and tell me where the resistance peaks.", _plug_back)]),
    checks=[
        answer_says("said the first sweep didn't finish", r"fail", r"stopp", r"abort", r"not (?:answer|respond)",
                    r"no (?:answer|response|reply)", r"lost", r"interrupt", r"error", r"timed? ?out", r"drop",
                    r"unreachable", r"offline", r"disconnect", r"incomplete", r"partial", r"didn.t finish",
                    where="all"),
        episode("finished the 0-5 V sweep after the lock-in came back", complete_after_fault),
        answer_quantity("peak gate voltage within 0.1 V", lambda ep: ep.p["Vcnp"], "V",
                        [r"peak", r"neutral", r"max", r"at\b"], abs_=0.1),
    ],
    oracle=[{"tool": "run_plan", "input": {"plan": SWEEP}},
            "The sweep stopped partway: the lock-in stopped answering. I'll finish it once it's back.",
            {"tool": "reconnect_instrument", "input": {"instrument": "lockin"}},
            {"tool": "run_plan", "input": {"plan": SWEEP}}],
    oracle_answer=lambda ctx: "The lock-in answers again and the full 0-5 V sweep is done: the peak is at **%.2f V**." % (
        ctx.p["Vcnp"]),
    tags=["pilot", "transport"],
)


TASKS = [TYPOS, INTERLOCK, DROPOUT]
