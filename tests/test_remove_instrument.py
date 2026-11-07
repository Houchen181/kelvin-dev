"""Kelvin takes an instrument out of the lab when the person asks (remove_instrument; asked for 2026-11-06: "at least
the agent should be able to remove it, not necessary to have a button on that"). Its entry leaves the lab config with
its own comment lines, the rest kept; the lab forgets it and lets go of its connection without sending it anything; its
watches end. While an output of it that reaches the sample is on, the person decides; never while a plan runs or while
another rule in the lab config names it."""
import json

import pytest

from labharness import builtin_registry
from labharness.adapters import set_resource_manager
from labharness.discovery import remove_entry
from labharness.gate import ScriptedApprover
from labharness.homelab import build_lab, lab_registry
from labharness.labconfig import load_lab_config
from test_hardware import Bus, Device

LAB = """lab: Transport lab
instruments:
  # the lock-in on the sample's voltage leads
  lockin:
    card: srs/sr860
    address: "GPIB0::8::INSTR"

  # the source-meter on the gate
  smu:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
names:
  Vg: smu.V
"""


@pytest.fixture
def bench():
    bus = Bus({"GPIB0::8::INSTR": Device("srs/sr860", name="sr860"), "GPIB0::24::INSTR": Device("keithley/2400")})
    set_resource_manager(bus)
    yield bus
    set_resource_manager(None)


def runtime_on(tmp_path, text, answers=()):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    (tmp_path / "lab.yaml").write_text(text, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml", registry=lab_registry(tmp_path), partial=True)
    rt = LabRuntime(lab, ScriptedApprover(list(answers)), tmp_path / "data")
    rt.home, rt.cards_dir, rt.scan_network = tmp_path, tmp_path / "cards", False
    rt.lab_info = {"kind": "file", "path": str(tmp_path / "lab.yaml"), "error": None, "has_file": True}
    return rt


def events(tmp_path, kind):
    lines = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("type") == kind]


def test_the_entry_goes_with_its_own_comments_and_the_rest_stays():
    assert remove_entry(LAB, "smu") == LAB.replace(
        '  # the source-meter on the gate\n  smu:\n    card: keithley/2400\n    address: "GPIB0::24::INSTR"\n'
        '    limits: { V: [-5, 5] }\n', "")
    assert remove_entry(LAB, "lockin") == LAB.replace(   # no blank line left under instruments:
        '  # the lock-in on the sample\'s voltage leads\n  lockin:\n    card: srs/sr860\n'
        '    address: "GPIB0::8::INSTR"\n\n', "")
    one_line = 'instruments:\n  a: { card: srs/sr830, address: "GPIB0::8::INSTR" }\n  b: { card: srs/sr830 }\n'
    assert remove_entry(one_line, "a") == "instruments:\n  b: { card: srs/sr830 }\n"
    assert remove_entry(remove_entry(one_line, "a"), "b") == "instruments:\n"   # the last one: a lab still to set up
    assert load_lab_config("lab: x\ninstruments:\n", builtin_registry()).instruments == {}
    with pytest.raises(KeyError):
        remove_entry(LAB, "Vg")   # a name, not an instrument


def test_kelvin_takes_an_instrument_out_and_lets_its_connection_go(tmp_path, bench):
    rt = runtime_on(tmp_path, LAB)   # no answers: Kelvin approves it itself, never asking anyone
    rt.add_watch("smu.I", above=1e-6)
    said = rt.remove_instrument("smu")
    assert said.startswith("removed smu (keithley/2400 at GPIB0::24::INSTR) from the lab config; its connection is "
                           "closed; its watches ended (smu.I above 1e-06)"), said
    assert "Left" not in said   # its output was off: a level or a current limit set doesn't reach the sample
    text = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    assert "smu:" not in text and "# the source-meter on the gate" not in text
    assert "# the lock-in on the sample's voltage leads" in text and "card: srs/sr860" in text
    assert "Vg: smu.V" in text   # the lab's name for it waits for it
    assert "smu" not in rt.lab.instruments and "smu" not in rt.lab.config.instruments
    assert rt.lab.read("lockin", "R") is not None   # the rest of the lab as it was
    assert not rt.monitor.watches
    saved = json.loads((tmp_path / "data" / rt.WATCHES).read_text(encoding="utf-8"))
    assert saved["labs"]["Transport lab"] == []
    removed = events(tmp_path, "instrument_removed")[-1]
    assert removed["instrument"] == "smu" and removed["connection"] == "closed"
    from labharness.agent.runtime import event_line
    assert event_line(removed).endswith(" agent: took smu (keithley/2400 at GPIB0::24::INSTR) out of the lab")
    assert "no instrument 'smu'" in rt.remove_instrument("smu")
    assert rt.remove_instrument("lockin").startswith("removed lockin")   # the last one too
    assert rt.lab.instruments == {} and load_lab_config(tmp_path / "lab.yaml", lab_registry(tmp_path)).instruments == {}


def test_an_output_on_is_the_persons_call(tmp_path, bench):
    rt = runtime_on(tmp_path, LAB, answers=[False, True])
    smu = rt.lab.instruments["smu"]
    smu.write("V", 1.0)
    smu.write("output", True)
    before = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    assert rt.remove_instrument("smu").startswith("declined: the person did not want smu removed")
    asked = rt.approver.requests[-1]
    assert asked.title == "Remove smu from the lab while it is on?"
    assert "Left on: smu.V 1 V, smu.output on" in str(asked)
    assert "Kelvin can't bring it to safe once it is out" in str(asked)
    assert (tmp_path / "lab.yaml").read_text(encoding="utf-8") == before and "smu" in rt.lab.instruments
    dev = bench.devices["GPIB0::24::INSTR"]
    n = len(dev.sent)
    said = rt.remove_instrument("smu")
    assert said.startswith("removed smu") and "Left on: smu.V 1 V, smu.output on" in said
    assert [c for c in dev.sent[n:] if not c.rstrip().endswith("?")] == []   # it only read it: nothing set, nothing off


def test_one_that_doesnt_answer_goes_with_a_word_to_check_it(tmp_path, bench):
    rt = runtime_on(tmp_path, LAB)
    bench.devices["GPIB0::24::INSTR"].silent = True   # unplugged since Kelvin connected it
    said = rt.remove_instrument("smu")   # no answers: Kelvin approves it itself
    assert said.startswith("removed smu") and "smu didn't answer, so Kelvin can't tell whether its outputs are on: " \
                                              "check it at the instrument." in said, said


def test_a_second_entry_of_the_same_instrument_goes_without_asking(tmp_path, bench):
    rt = runtime_on(tmp_path, LAB.replace("names:", '  smu_again:\n    card: keithley/2400\n'
                                                   '    address: "GPIB0::24::INSTR"\nnames:'))
    rt.lab.instruments["smu"].write("output", True)
    said = rt.remove_instrument("smu_again")   # no answers: nobody is asked
    assert said.startswith("removed smu_again") and "The same instrument stays in the lab as smu." in said
    assert rt.lab.read("smu", "output") is True


def test_never_while_a_plan_runs_or_another_rule_names_it(tmp_path, bench):
    rt = runtime_on(tmp_path, LAB + 'interlocks:\n  - drive: smu.V\n    require: "lockin.R < 1"\n')
    before = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    said = rt.remove_instrument("lockin")
    assert said.startswith("not removed: other rules in the lab config name lockin (interlocks[0]: lockin.R is not "
                           "a readable parameter). They are the person's"), said
    assert "interlocks[0]: 'smu.V' is not a drive" in rt.remove_instrument("smu")
    assert (tmp_path / "lab.yaml").read_text(encoding="utf-8") == before
    assert set(rt.lab.instruments) == {"lockin", "smu"}
    rt.live = {"status": "running"}
    assert rt.remove_instrument("lockin") == ("not removed: a plan or the queue is running; remove it when it has "
                                              "finished")
    rt.live = None
    rt.lab_info = {"kind": "builtin"}
    assert rt.remove_instrument("lockin").startswith("not removed: this lab has no lab config file")


def test_a_makers_driver_is_left_connected_and_a_visa_session_closed():
    class Driver:   # a maker's driver: closing it could send the instrument something
        closed = 0

        def close(self):
            self.closed += 1

    class Session(Driver):   # a VISA session: closing it sends nothing
        quiet_close = True
    lab = build_lab("lab: x\ninstruments:\n  a: {card: srs/sr830}\n  b: {card: srs/sr830}\n  c: {card: srs/sr830}\n"
                    "  d: {card: srs/sr830}\n")
    driver, parked, session = Driver(), Driver(), Session()
    lab.instruments["a"].backend, lab.instruments["b"].backend, lab.instruments["c"].backend = driver, parked, session
    lab.switch("b", False)   # switched off: its driver set aside, unclosed
    assert lab.take_out("a") == "set aside" and lab.take_out("b") == "set aside" and lab.take_out("c") == "closed"
    assert lab.take_out("d") == ""   # simulated: nothing of its own to let go
    lab.close()
    assert (driver.closed, parked.closed, session.closed) == (0, 0, 1)
    assert set(lab.instruments) == set(lab.config.instruments) == set()
