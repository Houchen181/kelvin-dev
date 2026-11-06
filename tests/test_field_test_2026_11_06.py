"""A field test's afternoon on one lab PC (Kelvin 0.3.43, GPIB lock-ins and Keithleys): what went wrong, kept from
going wrong again. A source's output that didn't answer was shown as on; after a timeout an instrument's answers came
one behind; a Keithley 2400 left measuring continuously was asked :READ? until the GPIB bus hung, and every alarm said
"cable or power?"; the Lab panel's state failed for minutes while an instrument was being added; setup failed when the
agent added one meanwhile; a failed switch-on read as switched off; the agent's tools failed without saying why; and a
Keithley 2450 set to its 2400 command set wasn't recognized."""
import json
import threading

import pytest

from labharness import builtin_registry
from labharness import profiles
from labharness.adapters import AdapterError, VisaBackend, set_resource_manager
from labharness.cards import UNREAD
from labharness.clock import SimClock
from labharness.gate import ScriptedApprover
from labharness.homelab import build_lab
from labharness.instrument import Health, InstrumentFault, InstrumentLost, NotHere, Unavailable
from labharness.lab import Lab, connecting_entry
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, make_twin_backend
from test_hardware import Bus, Device

ONE = """lab: Gates
instruments:
  smu:
    card: keithley/2400
    address: sim://smu
"""

TWO = ONE + """  smu2:
    card: keithley/2400
    address: sim://smu2
"""


# ------------------------------------------------------------------ a value Kelvin couldn't read is unknown

def test_a_setting_that_did_not_answer_is_unknown_never_on():
    card = builtin_registry().get("keithley/2400")
    out = card.params["output"]
    assert out.show("ERROR: smu doesn't answer on GPIB (address 15): cable or power? [VI_ERROR_TMO]") == UNREAD
    assert out.show(True) == "on" and out.show(False) == "off" and out.show("0") == "off" and out.show(1) == "on"
    assert out.show("garbage").startswith("unknown")   # never on for something that isn't on
    assert card.params["V"].show("ERROR: not read") == UNREAD


def test_settings_json_keeps_what_did_not_answer_apart(tmp_path):
    card = builtin_registry().get("keithley/2400")
    data = profiles.write_settings(tmp_path, "smu", card, {"output": "ERROR: smu doesn't answer", "V": 1.5})
    assert data["settings"]["output"]["value"] is None and data["settings"]["output"]["shown"] == UNREAD
    assert data["settings"]["output"]["error"] == "smu doesn't answer"
    assert data["settings"]["V"]["value"] == 1.5
    old = {"value": "ERROR: smu doesn't answer", "shown": "on"}   # as 0.3.43 wrote it
    assert profiles.shown_setting(old) == UNREAD and profiles.shown_setting(data["settings"]["V"]) == "1.5 V"


# ------------------------------------------------------------------ after a timeout, a reply is never taken for the next

class Late:
    """A GPIB instrument whose reply to a slow query comes after Kelvin stopped waiting; a reply that arrives after
    the device clear is read off by asking who it is."""
    IDN = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1083680,C27"

    def __init__(self):
        self.timeout, self.read_termination, self.write_termination = 3000, None, None
        self.out, self.later, self.sent, self.cleared = [], [], [], 0
        self.slow, self.hung = set(), False

    def _answer(self, cmd):
        return {"*IDN?": self.IDN, ":SOUR:VOLT?": "0.000000E+00", ":OUTP?": "1"}.get(cmd, "1")

    def write(self, cmd):
        if self.hung:
            raise TimeoutError("VI_ERROR_TMO")
        self.out += self.later   # what came too late arrives now
        self.later = []
        self.sent.append(cmd)
        if cmd.endswith("?"):
            (self.later if cmd in self.slow else self.out).append(self._answer(cmd))
            self.slow.discard(cmd)

    def read(self):
        if self.hung or not self.out:
            raise TimeoutError("VI_ERROR_TMO")
        return self.out.pop(0)

    def query(self, cmd, delay=None):
        self.write(cmd)
        return self.read()

    def clear(self):
        if self.hung:
            raise TimeoutError("VI_ERROR_TMO")
        self.cleared += 1
        self.out = []

    def close(self):
        pass


class OneRM:
    def __init__(self, res):
        self.res = res

    def open_resource(self, name):
        return self.res


def backend(res):
    card = builtin_registry().get("keithley/2400")
    return VisaBackend("smu", card, "GPIB0::15::INSTR", rm=OneRM(res))


def test_a_reply_that_comes_too_late_is_never_taken_for_the_next_command():
    res = Late()
    smu = backend(res)
    res.slow.add(":SOUR:VOLT?")
    with pytest.raises(TimeoutError):
        smu.query(":SOUR:VOLT?")
    assert res.cleared == 1 and smu.owed   # cleared at once; checked again before the next command
    assert smu.query(":OUTP?") == "1"     # not the late "0.000000E+00"
    assert res.sent[-2:] == ["*IDN?", ":OUTP?"] and not smu.owed


def test_while_the_bus_hangs_nothing_more_is_sent_and_after_it_the_instrument_is_checked_first():
    res = Late()
    smu = backend(res)
    res.hung = True
    with pytest.raises(TimeoutError):
        smu.query(":OUTP?")
    sent = list(res.sent)
    with pytest.raises(TimeoutError):   # the clear before it doesn't go through: the command isn't sent
        smu.query(":OUTP?")
    assert res.sent == sent
    res.hung = False
    assert smu.query(":OUTP?") == "1" and res.sent[-2:] == ["*IDN?", ":OUTP?"]


def test_one_that_answers_as_someone_else_after_a_timeout_is_out_of_step():
    res = Late()
    res.IDN = "SOMETHING ELSE"
    smu = backend(res)
    res.slow.add(":SOUR:VOLT?")
    with pytest.raises(TimeoutError):
        smu.query(":SOUR:VOLT?")
    with pytest.raises(AdapterError, match="out of step"):
        smu.query(":OUTP?")


# ------------------------------------------------------------------ a 2400 measuring continuously, and its error queue

class Continuous:
    """A 2400's twin left measuring continuously by another program (arm count infinite) until it is stopped."""
    simulated = False

    def __init__(self, twin):
        self.twin, self.arm, self.reads, self.sent = twin, "+2.147484E+09", 0, []

    def write(self, cmd):
        self.sent.append(cmd)
        up = cmd.upper()
        if up.startswith(":ARM:COUN"):
            self.arm = cmd.split()[-1]
        elif not up.startswith((":ABOR", ":TRIG", ":ARM", ":FORM", "*CLS")):
            self.twin.write(cmd)

    def query(self, cmd):
        self.sent.append(cmd)
        if cmd.upper().startswith(":ARM:COUN"):
            return self.arm
        if cmd.upper().startswith(":READ"):
            self.reads += 1
        return self.twin.query(cmd)

    def close(self):
        pass


def smu_lab(text=ONE):
    return build_lab(text)


def test_a_2400_left_measuring_continuously_is_never_asked_read_and_kelvin_says_why():
    lab = smu_lab()
    inst = lab.instruments["smu"]
    fake = inst.backend = Continuous(inst.backend)
    inst.write("output", True)
    with pytest.raises(InstrumentFault, match="measure continuously.*Stop continuous mode"):
        inst.read("I")
    assert fake.reads == 0 and inst.health.fails == 0   # :READ? never sent; not "cable or power?"
    snap = lab.snapshot(["smu"])["smu"]
    assert str(snap["I"]).startswith("ERROR") and snap["V"] is not None   # its other settings still read
    inst.act("stop_continuous")
    assert inst.read("I") is not None and fake.reads == 1
    inst.read("V_meas")
    assert fake.reads == 2   # each caller its own :READ?: another's may be from before a change
    lab.snapshot(["smu"])
    assert fake.reads == 3   # one look at the instrument: V and I from one :READ?
    checks = len([c for c in fake.sent if c.upper().startswith((":ARM", ":TRIG", ":FORM"))])
    inst.write("V", 0.5)   # a setting that can't change what the checks ask: they are not asked again
    inst.read("I")
    assert len([c for c in fake.sent if c.upper().startswith((":ARM", ":TRIG", ":FORM"))]) == checks


class Silent:
    """A 2400 that answers its error queue but never :READ?."""
    simulated = False

    def __init__(self, twin):
        self.twin, self.errors = twin, ['830,"Invalid with INFinite ARM:COUNT"']

    def write(self, cmd):
        self.twin.write(cmd)

    def query(self, cmd):
        if cmd.upper().startswith(":READ"):
            raise TimeoutError("VI_ERROR_TMO")
        return self.twin.query(cmd)

    def ask_after_timeout(self, cmd):
        return self.errors.pop(0) if self.errors else '0,"No error"'

    def close(self):
        pass


def test_a_query_it_did_not_answer_says_what_its_error_queue_says_not_cable_or_power():
    lab = smu_lab()
    inst = lab.instruments["smu"]
    inst.write("output", True)
    inst.backend = Silent(inst.backend)
    with pytest.raises(InstrumentFault, match=r"didn't answer :READ\?: it reports 830"):
        inst.read("I")
    assert inst.health.fails == 0
    inst.backend.ask_after_timeout = lambda cmd: None   # its error queue doesn't answer either: lost
    with pytest.raises(InstrumentLost, match=r"didn't answer :READ\? in time; it answered"):
        inst.read("I")


def test_kelvin_leaves_an_instrument_that_stopped_answering_alone_for_a_while():
    h = Health()
    assert h.resting() is None
    h.failed(False, "smu doesn't answer")
    until = h.resting()
    assert until is not None and 4 < until - h.lost_at <= 5
    for _ in range(10):
        h.failed(False, "smu doesn't answer")
    assert h.resting() - h.lost_at == 60   # a minute at most
    h.answered()
    assert h.resting() is None


# ------------------------------------------------------------------ an instrument joins the lab once it is connected

def test_an_instrument_joins_the_lab_only_once_connected_and_with_its_new_entry():
    registry = builtin_registry()
    cfg = load_lab_config(ONE, registry)
    world = World(SimClock())
    seen, box = [], {}

    def factory(name, card, address):
        if "lab" in box:
            lab = box["lab"]
            seen.append((name in lab.config.instruments, name in lab.instruments, connecting_entry(name) is not None))
        return make_twin_backend(card, world, name)
    lab = box["lab"] = Lab(cfg, registry, factory, SimClock())
    ic = load_lab_config(TWO, registry).instruments["smu2"]
    before_cfg, before_inst = lab.config.instruments, lab.instruments
    lab.add(ic, registry.get(ic.card))
    assert seen[-1] == (False, False, True)   # nobody sees it while it connects; the factory sees its entry
    assert "smu2" in lab.instruments and "smu2" in lab.config.instruments
    assert "smu2" not in before_cfg and "smu2" not in before_inst   # new dictionaries: no one's loop changes size
    lab.remove("smu2")
    assert "smu2" not in lab.instruments and "smu2" not in lab.config.instruments


def test_a_switch_on_that_cannot_connect_is_on_and_not_connected_never_switched_off():
    registry = builtin_registry()
    cfg = load_lab_config(ONE, registry)

    def factory(name, card, address):
        raise AdapterError("smu: cannot open GPIB0::10::INSTR (VI_ERROR_TMO)")
    lab = Lab(cfg, registry, factory, SimClock(), switched_off=lambda _: ["smu"])
    with pytest.raises(AdapterError):
        lab.switch("smu", True)
    assert "cannot open" in lab.instruments["smu"].unavailable
    assert lab.health_check(["smu"])[0].problems[0].startswith("not connected (smu: cannot open")
    with pytest.raises(NotHere) as e:
        lab.instrument("smu")
    assert str(e.value).startswith("smu is not connected")   # its words, without a KeyError's quotes


def test_instruments_close_within_their_time_even_one_that_hangs():
    lab = smu_lab(TWO)
    done = threading.Event()

    class Hangs:
        def close(self):
            done.wait(5)
    lab.instruments["smu2"].backend = Hangs()
    assert lab.close(within=0.3) == ["smu2"]
    done.set()


# ------------------------------------------------------------------ the agent's runtime

def runtime(tmp_path, text=ONE, answers=()):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    (tmp_path / "lab.yaml").write_text(text, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml")
    rt = LabRuntime(lab, ScriptedApprover(list(answers)), tmp_path / "data")
    rt.home, rt.cards_dir, rt.scan_network = tmp_path, tmp_path / "cards", False
    return rt


def events(tmp_path, kind):
    lines = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("type") == kind]


def test_the_lab_panel_works_while_the_lab_config_lists_an_instrument_not_in_the_lab(tmp_path):
    rt = runtime(tmp_path)
    ghost = load_lab_config(TWO, builtin_registry()).instruments["smu2"]
    rt.lab.config.instruments = dict(rt.lab.config.instruments, smu2=ghost)
    assert rt.approval_mode()["mode"] == "ask"
    state = rt.ui_state()
    assert state["lab"] == "Gates" and "smu" in state["status"]

    def broken():
        raise RuntimeError("queue view broke")
    rt.queue_view = broken   # one part that fails can't fail the panel: the rest is there
    state = rt.ui_state()
    assert state["queue"] is not None and state["alarms"] == [] and state["lab"] == "Gates"


def test_a_failed_switch_on_is_in_the_record_with_why(tmp_path):
    rt = runtime(tmp_path)
    rt.switch_instrument("smu", False, "human")

    def factory(name, card, address):
        raise AdapterError("smu: cannot open GPIB0::10::INSTR (VI_ERROR_TMO)")
    rt.lab._factory = factory
    rt.lab._parked.clear()   # its connection, set aside when switched off, is gone: connecting it again fails
    with pytest.raises(AdapterError):
        rt.switch_instrument("smu", True, "human (approved Kelvin's request)")
    failed = events(tmp_path, "switch_on_failed")
    assert failed and "cannot open GPIB0::10::INSTR" in failed[-1]["error"]
    assert rt.lab.away("smu").startswith("not connected (")


def test_an_instrument_that_is_not_connected_is_still_described(tmp_path):
    rt = runtime(tmp_path)
    rt.lab.instruments["smu"].backend = Unavailable("smu", "no camera X045418024 (PICam sees: none)")
    said = rt.describe("smu")
    assert "not connected (no camera X045418024" in said and "compliance_I" in said


def test_kelvins_refusals_reach_the_agent_in_its_words():
    pytest.importorskip("mcp")
    from labharness.agent.server import anticipated
    assert anticipated(NotHere("camera is not connected (why)")) == "camera is not connected (why)"
    assert anticipated(KeyError("no instrument 'x'")) == "no instrument 'x'"
    assert anticipated(AdapterError("smu: cannot open")) == "smu: cannot open"
    assert anticipated(ValueError("V needs a number")) == "V needs a number"
    assert anticipated(TypeError("bug")) is None and anticipated(RuntimeError("dict changed size")) is None


def test_try_card_without_an_instrument_asks_which(tmp_path):
    rt = runtime(tmp_path)
    text = (builtin_registry().get("keithley/2400").source and
            open(builtin_registry().get("keithley/2400").source, encoding="utf-8").read())
    said = rt.try_card(text)
    assert said.startswith("Which instrument? Give it as instrument") and "scan" in said


def test_stop_continuous_mode_asks_only_while_the_output_is_on(tmp_path):
    rt = runtime(tmp_path, answers=[False])
    inst = rt.lab.instruments["smu"]
    inst.backend = Continuous(inst.backend)
    said = rt.do_action("smu", "stop_continuous")   # output off: nothing reaches the sample, no question
    assert "Stop continuous mode" in said and not rt.approver.requests
    inst.write("output", True)
    said = rt.do_action("smu", "stop_continuous")
    assert said.startswith("declined") and len(rt.approver.requests) == 1


# ------------------------------------------------------------------ a Keithley 2450 set to its 2400 command set

def k2450_bus(lang):
    dev = Device("keithley/2400", idn="KEITHLEY INSTRUMENTS,MODEL 2400,04453531,1.6.7c")
    plain = dev.twin.query
    dev.twin.query = lambda cmd: lang if cmd.strip().upper() == "*LANG?" else plain(cmd)
    return Bus({"GPIB0::30::INSTR": dev})


@pytest.mark.parametrize("lang,ok", [("SCPI2400", True), ("SCPI", False)])
def test_a_2450_set_to_its_2400_command_set_is_recognized_and_said(lang, ok):
    set_resource_manager(k2450_bus(lang))
    try:
        lab = build_lab("lab: Gates\ninstruments:\n  k30:\n    card: keithley/2400\n    address: GPIB0::30::INSTR\n")
        result = lab.health_check(["k30"])[0]
    finally:
        set_resource_manager(None)
    assert result.ok is ok, result
    assert "answers as a Keithley 2450" in str(result)
    if not ok:
        assert "*LANG? answered 'SCPI'" in str(result)
    assert not builtin_registry().get("keithley/2400").variant("KEITHLEY INSTRUMENTS INC.,MODEL 2400,1083680,C27")


def test_reconnecting_a_camera_whose_helper_kelvin_ended_makes_a_fresh_one():
    import types
    registry = builtin_registry()
    cfg = load_lab_config(ONE, registry)
    world = World(SimClock())
    made = []

    def factory(name, card, address):
        made.append(name)
        return make_twin_backend(card, world, name)
    lab = Lab(cfg, registry, factory, SimClock())
    inst = lab.instruments["smu"]
    closed = []
    inst.backend = types.SimpleNamespace(helper=types.SimpleNamespace(gone="a frame didn't come in 90 s"),
                                         close=lambda: closed.append(1))
    said = lab.reconnect("smu")
    assert "connected again (its connection had ended: a frame didn't come in 90 s)" in said
    assert closed == [1] and made == ["smu", "smu"] and inst.read("V") is not None
    assert lab.reconnect("smu") == ""   # connected: nothing to do


# ------------------------------------------------------------------ what a review found

def test_a_2400_another_program_left_taking_many_readings_still_sweeps_its_source(tmp_path):
    rt = runtime(tmp_path, answers=[True])
    inst = rt.lab.instruments["smu"]
    inst.backend = Continuous(inst.backend)   # its arm count infinite: I and V_meas can't be read
    result = rt.lab.health_check(["smu"])[0]
    assert result.ok and "I" in result.not_now and "not read now" in str(result)
    said = rt.run("""plan: gate_only
sweep: { smu.V: {from: 0, to: 0.2, step: 0.1} }
measure: [smu.V]
after: { smu.V: 0 }""")
    assert "preflight failed" not in said, said
    said = rt.run("""plan: needs_current
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 0.2, step: 0.1} }
measure: [smu.I]
after: { smu.V: 0, smu.output: off }""")
    assert "preflight failed" in said and "measure continuously" in said, said


class Socket:
    """A plain socket instrument with a start-up command: reconnecting after a timeout sends it again, once."""
    def __init__(self, opened):
        self.timeout, self.read_termination, self.write_termination = 3000, None, None
        self.opened, self.out = opened, []
        opened.append(self)

    def write(self, cmd):
        if cmd.endswith("?"):
            self.out.append("ACME,W9,1,1.0" if cmd == "*IDN?" else "1")

    def read(self):
        if not self.out:
            raise TimeoutError("VI_ERROR_TMO")
        return self.out.pop(0)

    def query(self, cmd, delay=None):
        if len(self.opened) == 1 and cmd != "*IDN?":   # the first connection's first answer comes too late
            raise TimeoutError("VI_ERROR_TMO")
        self.write(cmd)
        return self.read()

    def close(self):
        pass


def test_a_socket_instrument_reconnected_after_a_timeout_sends_its_start_up_once_and_goes_on():
    from labharness.cards import parse_card
    card = parse_card({"card": 1, "id": "acme/w9", "model": "ACME W9", "kind": "multimeter", "status": "draft",
                       "identify": {"idn": "ACME,W9"}, "on_connect": ["CHDR OFF"],
                       "parameters": {"v": {"kind": "measure", "unit": "V", "get": "V?"}}}, "test")
    opened = []

    class RM:
        def open_resource(self, name):
            return Socket(opened)
    smu = VisaBackend("w9", card, "TCPIP0::10.0.0.5::5025::SOCKET", rm=RM())
    with pytest.raises(TimeoutError):
        smu.query("V?")
    assert smu.query("V?") == "1" and len(opened) == 2 and not smu.owed


def test_instruments_switched_off_at_their_mains_never_make_a_working_bus_hung():
    lab = smu_lab(TWO)
    for name in ("smu", "smu2"):
        inst = lab.instruments[name]
        inst.config.address = "GPIB0::%d::INSTR" % (24 if name == "smu" else 25)
        inst.health.failed(True, "%s doesn't answer: nothing listens there" % name)   # VI_ERROR_NLISTENERS
        inst.backend.simulated = False
    assert lab.hung_buses() == []
    for name in ("smu", "smu2"):
        lab.instruments[name].health.failed(False, "%s doesn't answer" % name)   # timeouts: the bus may be hung
    assert [b for b, _ in lab.hung_buses()] == ["GPIB0"]


def test_the_first_to_stop_answering_is_the_one_whose_exchange_began_first():
    import time as _time
    lab = smu_lab()
    inst = lab.instruments["smu"]

    class Slow:
        simulated = False

        def query(self, cmd):
            _time.sleep(0.3)
            raise TimeoutError("VI_ERROR_TMO")
    inst.backend = Slow()
    began = _time.time()
    with pytest.raises(InstrumentLost):
        inst._io(inst.backend.query, ":READ?")
    assert inst.health.first_failed_at - began < 0.1   # when it began, not when it gave up


def test_quitting_leaves_a_driver_whose_closing_would_send_something_as_it_is():
    lab = smu_lab(TWO)
    closed = []

    class Quiet:
        quiet_close = True

        def close(self):
            closed.append("quiet")

    class Pulses:   # a pulse generator: closing it stops its outputs
        def close(self):
            closed.append("pulses")
    lab.instruments["smu"].backend, lab.instruments["smu2"].backend = Quiet(), Pulses()
    assert lab.close(within=1.0, quitting=True) == [] and closed == ["quiet"]


def test_a_bus_clear_asks_a_person_when_the_bus_is_not_plainly_kelvins(tmp_path, monkeypatch):
    from labharness import others
    monkeypatch.setattr(others, "holders", lambda: [])
    rt = runtime(tmp_path, ONE.replace("sim://smu", "sim://smu") + """  k2:
    card: keithley/2400
    address: sim://k2
""", answers=[False])
    for name, addr in (("smu", "GPIB0::15::INSTR"), ("k2", "GPIB0::24::INSTR")):
        inst = rt.lab.instruments[name]
        inst.config.address, inst.backend.simulated = addr, False
    said = rt.clear_bus("GPIB0")   # it answers: Kelvin hasn't found it hung, so a person decides
    assert said.startswith("declined") and "hasn't found it hung" in str(rt.approver.requests[-1])
    rt.switch_instrument("smu", False, "human")
    rt.switch_instrument("k2", False, "human")
    assert rt.clear_bus("GPIB0").startswith("not cleared: every instrument of this lab on GPIB0 is disconnected")


def test_each_viewer_has_its_own_last_lab_panel(tmp_path):
    from labharness.agent.runtime import Viewer
    rt = runtime(tmp_path)
    rt.ui_state()
    with rt.viewing(Viewer(chat="c1", project="p1")):
        rt.ui_state()
    assert set(rt._ui_last) == {None, ("c1", "p1")}


def test_a_kelvin_that_is_quitting_is_not_brought_forward(tmp_path):
    from labharness import desktop
    lock = tmp_path / "app.json"
    lock.write_text(json.dumps({"port": 1, "pid": 2, "closing": True}), encoding="utf-8")
    assert desktop._running_copy(lock) is None
