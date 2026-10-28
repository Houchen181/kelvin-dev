"""Motion arrival: a stage or a piezo the instrument moves by itself is followed until its card's status says it is
there, and only then read back (field test: an SMC100 delay line was read mid-move, which looked like a refused value,
and every pump-probe run stopped at its first point)."""
import math

import pytest

from labharness import EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover, build_virtual_lab
from labharness.cards import CardError, parse_card
from labharness.sim.twin import Twin
from labharness.virtual_lab import builtin_registry


def delay_card(**status):
    return parse_card({
        "card": 1, "id": "test/delay-line", "model": "Delay line on an SMC100-like controller", "kind": "motion-stage",
        "status": "community",
        "parameters": {"position": dict({"kind": "measure", "unit": "mm", "set": "1PA{value}", "get": "1TP?",
                                         "limits": [0, 25], "timeout": 30}, **status)}})


class DelayLine(Twin):
    """Moves at 2.5 mm/s; 1TP? says where it is now, 1TS 28 while it moves and 33 once there, ONT? =1 on target."""

    def reset(self):
        super().reset()
        self.target = self.pos = 0.0
        self.stuck = False

    def apply(self, param, value):
        if param == "position":
            self.target = value
        else:
            super().apply(param, value)

    def evolve(self, dt):
        if not self.stuck:
            d, step = self.target - self.pos, 2.5 * dt
            self.pos = self.target if abs(d) <= step else self.pos + math.copysign(step, d)

    def measure_position(self):
        return self.pos

    def _answer(self, cmd):
        if cmd.strip().upper() == "1TS":
            return "1TS0000%s" % ("28" if self.pos != self.target else "33")
        if cmd.strip().upper() == "ONT? 1":
            return "1=%d" % (self.pos == self.target)
        return super()._answer(cmd)


def lab_with(card, tmp_path, answers=()):
    registry = builtin_registry()
    registry.put(card)
    lab = build_virtual_lab(config={"lab": "delay", "instruments": {"delay": {"card": card.id}}}, registry=registry)
    twin = DelayLine(card, lab.world, "delay")
    twin.simulated = False   # checked as a real instrument is: its read-back must show the new value
    lab.instruments["delay"].backend = twin
    return lab, SafetyGate(lab, ScriptedApprover(answers), EventLog(tmp_path / "events.jsonl", lab.clock)), twin


SCAN = "plan: delay_scan\nsweep: { delay.position: { values: [10, 12.5] } }\nmeasure: [delay.position]\n"


@pytest.mark.parametrize("status", [{"busy": {"get": "1TS", "match": "(28|1E)$"}},
                                    {"arrived": {"get": "ONT? 1", "match": "=1$"}}])
def test_a_scan_waits_for_the_stage_to_arrive_before_reading_it_back(tmp_path, status):
    lab, gate, twin = lab_with(delay_card(**status), tmp_path)
    report = RunEngine(gate, RunStore(None)).run(SCAN)
    assert report.ok, report.message
    assert [r["delay.position"] for r in report.rows] == [10, 12.5]
    r = gate.set("delay", "position", 20, wait=False)            # not waited for: no read-back check mid-move
    assert r.ok and "moving" in r.message


def test_a_stage_that_never_arrives_times_out_and_says_so(tmp_path):
    lab, gate, twin = lab_with(delay_card(busy={"get": "1TS", "match": "(28|1E)$"}), tmp_path)
    twin.stuck = True
    r = gate.set("delay", "position", 10)
    assert r.action == "timeout" and "its status says it isn't at 10 mm after 30 s" in r.message


def test_busy_and_arrived_need_a_query_and_a_pattern():
    with pytest.raises(CardError, match="busy"):
        delay_card(busy={"get": "1TS"})
    with pytest.raises(CardError, match="not a valid regex"):
        delay_card(arrived={"get": "ONT? 1", "match": "("})
