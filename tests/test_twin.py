"""Generic twins, for cards with no twin class of their own: they carry out the ramps a card leaves to the instrument
and answer the card's health checks as a healthy instrument would, so dry runs work for real-instrument cards."""
import re

import pytest

from labharness import builtin_registry
from labharness.cards import CardRegistry, parse_card
from labharness.clock import SimClock
from labharness.engine import RunEngine
from labharness.eventlog import EventLog
from labharness.gate import SafetyGate, SimulationApprover
from labharness.sim.twin import World, make_twin_backend
from simlab import build_virtual_lab

RAMPS = {
    "card": 1, "id": "test/ramps", "model": "Ramps", "kind": "magnet-supply", "status": "community",
    "parameters": {
        "B_set": {"kind": "drive", "unit": "T", "set": "FIELd:TARGet {value}", "get": "FIELd:TARGet?",
                  "limits": [-9, 9], "safe": 0, "ramp": "instrument", "readback": "B", "tol": 0.001,
                  "rate_param": "B_rate", "rate_per": "min"},
        "B_rate": {"kind": "measure", "unit": "T/min", "set": "FIELd:RATE {value}", "get": "FIELd:RATE?",
                   "default": 0.6},
        "B": {"kind": "measure", "unit": "T", "get": "FIELd:MEASure?"},
        "I_set": {"kind": "drive", "unit": "A", "set": "CURRent:TARGet {value}", "get": "CURRent:TARGet?",
                  "limits": [-50, 50], "safe": 0, "ramp": "instrument", "readback": "I", "max_rate": 2},
        "I": {"kind": "measure", "unit": "A", "get": "CURRent:MEASure?"},
        "T_set": {"kind": "drive", "unit": "K", "set": "TEMPerature:TARGet {value}", "get": "TEMPerature:TARGet?",
                  "limits": [1.5, 300], "safe": 1.8, "ramp": "instrument", "readback": "T"},
        "T": {"kind": "measure", "unit": "K", "get": "TEMPerature:MEASure?"},
    },
}


def twin_of(data):
    clock = SimClock()
    twin = make_twin_backend(parse_card(data), World(clock), "twin")
    return twin, clock, lambda cmd: float(twin.query(cmd))


def sim_lab(instruments, registry=None):
    return build_virtual_lab(clock=SimClock(), config={"lab": "test", "instruments": instruments},
                             registry=registry or builtin_registry())


def dry_run(lab, plan):
    return RunEngine(SafetyGate(lab, SimulationApprover(), EventLog(None, lab.clock), operator="test")).simulate(plan)


def test_a_reading_follows_its_setpoint_at_the_cards_ramp_rate():
    twin, clock, read = twin_of(RAMPS)
    assert read("TEMPerature:MEASure?") == 1.8     # at rest: a reading with no default starts at its setpoint
    twin.write("FIELd:TARGet 1")                   # at its rate parameter, 0.6 T/min
    assert read("FIELd:MEASure?") == 0
    clock.sleep(30)
    assert read("FIELd:MEASure?") == pytest.approx(0.3)
    clock.sleep(100)
    assert read("FIELd:MEASure?") == 1             # there, and it stays
    twin.write("CURRent:TARGet -5")                # at the card's max_rate, 2 A/s
    clock.sleep(1)
    assert read("CURRent:MEASure?") == pytest.approx(-2)
    clock.sleep(10)
    assert read("CURRent:MEASure?") == -5
    twin.write("TEMPerature:TARGet 4.2")           # no rate: there at once
    assert read("TEMPerature:MEASure?") == 4.2
    twin.write("FIELd:RATE 0")                     # a rate of 0 (one never set, like a PI stage's velocity) is none
    twin.write("FIELd:TARGet -1")
    assert read("FIELd:MEASure?") == -1
    assert not twin.errors


def test_a_reading_follows_only_while_its_switch_applies_the_setpoint():
    params = dict(RAMPS["parameters"], T_set=dict(RAMPS["parameters"]["T_set"], applies_when="control"),
                  control={"kind": "drive", "type": "bool", "set": "CONTrol {value}", "get": "CONTrol?", "safe": False})
    twin, clock, read = twin_of(dict(RAMPS, parameters=params))
    twin.write("TEMPerature:TARGet 10")
    clock.sleep(60)
    assert read("TEMPerature:MEASure?") == 1.8     # the control is off: the sample stays where it is
    twin.write("CONTrol 1")
    assert read("TEMPerature:MEASure?") == 10


def test_dry_runs_wait_out_the_ramps_an_instrument_makes():
    lab = sim_lab({"cryo": {"card": "qd/multivu", "limits": {"T_set": [1.8, 350], "B_set": [-9, 9]}},
                   "lockin": {"card": "srs/sr830"}})
    report = dry_run(lab, {"plan": "hall", "sweep": {
        "cryo.T_set": {"values": [2], "stabilize": {"read": "cryo.T", "within": 0.02, "for": 60}},
        "cryo.B_set": {"from": -1, "to": 1, "step": 0.5}}, "measure": ["lockin.X", "cryo.B", "cryo.T"],
        "after": {"cryo.B_set": 0}})
    assert report.ok, report.message                # MultiVu's card gives no rate: the field is there at once
    assert [row["cryo.B"] for row in report.rows] == [-1, -0.5, 0, 0.5, 1]
    assert all(row["cryo.T"] == 2 for row in report.rows)

    lab = sim_lab({"scan": {"card": "pi/gcs-piezo", "vars": {"axis": "1"}, "limits": {"position": [0, 100]}}})
    report = dry_run(lab, {"plan": "line", "setup": {"scan.velocity": 10},
                           "sweep": {"scan.position": {"values": [0, 50, 100]}}, "measure": ["scan.position_actual"]})
    assert report.ok, report.message
    assert [row["scan.position_actual"] for row in report.rows] == [0, 50, 100]
    assert report.duration_s == pytest.approx(10, abs=0.5)    # two 50 µm moves at 10 µm/s


def test_every_real_card_passes_its_health_check_on_a_twin():
    reg = builtin_registry()
    cards = [card for card in reg if not card.twin]
    lab = sim_lab({"i%d" % i: {"card": card.id} for i, card in enumerate(cards)}, reg)
    bad = [str(r) for r in lab.health_check() if not r.ok]
    assert not bad, bad
    name = {inst.card.id: n for n, inst in lab.instruments.items()}
    assert lab.read(name["ami/430"], "field_units") == "T"       # set up in tesla, as its health check requires
    assert lab.read(name["lightcon/pharos"], "errors") == "[]"   # no errors, written the way the laser writes them
    report = dry_run(sim_lab({"magnet": {"card": "ami/430", "limits": {"B_target": [-1, 1]}},
                              "mw": {"card": "rs/smb100a", "limits": {"power": [-145, 15]}}}),
                     {"plan": "fmr", "setup": {"mw.frequency": 1.0e10, "mw.power": 10, "mw.output": "on"},
                      "sweep": {"magnet.B_target": {"from": 0, "to": 0.1, "step": 0.05}}, "measure": ["magnet.B"],
                      "after": {"mw.output": "off", "magnet.B_target": 0}})
    assert report.ok, report.message


def test_a_twin_fails_the_health_checks_an_instrument_would_fail():
    lab = sim_lab({"mw": {"card": "rs/smb100a"}, "magnet": {"card": "ami/430"}})
    mw = lab.instruments["mw"].backend
    assert mw.query(":SYSTem:ERRor?") == '0,"No error"'    # the card's :SYSTem:ERRor[:NEXT]?, as it is sent
    mw.errors += ['-222,"Data out of range"', '-221,"Settings conflict"']
    assert mw.query(":SYSTem:ERRor?") == '-222,"Data out of range"'   # the queued error, not a made-up 0
    lab.instruments["magnet"].backend.load_state({"field_units": "kG"})     # a dry run copies a 430 left in kG
    results = {r.instr: r for r in lab.health_check()}
    assert not results["mw"].ok and "-221" in str(results["mw"])
    assert not results["magnet"].ok and "FIELD:UNITS? returned '0'" in str(results["magnet"])


def test_a_twin_answers_a_health_query_its_card_has_no_command_for():
    reg = CardRegistry()
    reg.add(parse_card({"card": 1, "id": "test/meter", "model": "Meter", "kind": "multimeter", "status": "draft",
                        "identify": {"idn": "^ACME,METER-?[0-9]+"},       # and no example reply
                        "parameters": {"V": {"kind": "measure", "unit": "V", "get": "MEASure:VOLTage?"}},
                        "health": [{"query": "SYSTem:VERSion?", "expect": "^1999\\.0$"}]}))
    lab = sim_lab({"meter": {"card": "test/meter"}}, reg)
    (result,) = lab.health_check()
    assert result.ok, result
    twin = lab.instruments["meter"].backend
    assert re.search("^ACME,METER-?[0-9]+", twin.query("*IDN?")) and twin.query("SYSTem:VERSion?") == "1999.0"
    with pytest.raises(TimeoutError):               # anything else it still doesn't know
        twin.query("SYSTem:BEEPer?")
