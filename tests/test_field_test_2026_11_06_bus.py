"""The field test's afternoon, continued: one Keithley 2400 hung a whole GPIB bus for 45 minutes while Kelvin went on
asking every instrument on it, each alarm said "cable or power?", and only an interface clear brought it back; a
second Kelvin started while the first still held the instruments and crashed as it quit; every log line came twice;
two Kelvins wrote the instrument collection at once; the lab's log numbered its lines twice over; one Got it was kept
three times; an agent's request waited on a hung bus until a person stopped it."""
import json
import sys
import threading
import types

import pytest

from labharness.adapters import AdapterError, set_resource_manager
from labharness.homelab import build_lab
from labharness.instrument import InstrumentFault, InstrumentLost
from labsim_bench import labsim_bench  # noqa: F401  (the fixture)
from test_field_test_2026_11_06 import events, runtime, smu_lab
from test_hardware import Bus, Device


# ------------------------------------------------------------------ a GPIB bus that hangs, and its clear

class HangingBus(Bus):
    """A GPIB board whose instruments all stop answering at once (one of them hung the bus) until an interface clear."""

    def __init__(self, devices):
        super().__init__(devices)
        self.ifc = 0

    def hang(self):
        for d in self.devices.values():
            d.silent = True

    def open_resource(self, name):
        if name.endswith("::INTFC"):
            bus = self

            class Interface:
                def send_ifc(self):
                    bus.ifc += 1
                    for d in bus.devices.values():
                        d.silent = False

                def close(self):
                    pass
            return Interface()
        return super().open_resource(name)


GPIB_LAB = """lab: Transport
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::15::INSTR
  lockin_upper:
    card: srs/sr830
    address: GPIB0::10::INSTR
  lockin_bottom:
    card: srs/sr830
    address: GPIB0::9::INSTR
"""


@pytest.fixture
def hanging():
    from labharness.adapters import BUSES
    bus = HangingBus({"GPIB0::15::INSTR": Device("keithley/2400"), "GPIB0::10::INSTR": Device("srs/sr830"),
                      "GPIB0::9::INSTR": Device("srs/sr830")})
    set_resource_manager(bus)
    yield bus
    set_resource_manager(None)
    BUSES.hung.clear()


def test_a_hung_gpib_bus_is_one_alarm_kelvin_stops_sending_on_it_and_clearing_it_brings_it_back(tmp_path, hanging):
    from labharness.adapters import BUSES
    rt = runtime(tmp_path, GPIB_LAB)
    lab = rt.lab
    assert lab.read("lockin_upper", "X") is not None
    hanging.hang()
    for name, param in [("smu", "V"), ("lockin_upper", "X"), ("lockin_bottom", "X")] * 3:   # smu first: it hung it
        with pytest.raises(InstrumentLost):
            lab.read(name, param)
    rt.monitor.tick()
    bus_alarms = [a for a in rt.monitor.alarms if a.source == "bus"]
    assert len(bus_alarms) == 1 and bus_alarms[0].key == "GPIB0" and "GPIB0" in BUSES.hung
    assert "The first to stop was smu" in bus_alarms[0].text and bus_alarms[0].view()["bus"] == "GPIB0"
    assert not [a for a in rt.monitor.alarms if a.kind == "silent"]   # one alarm for the bus, not one each
    sent = len(hanging.devices["GPIB0::10::INSTR"].sent)
    with pytest.raises(AdapterError, match="GPIB0 isn't answering"):   # at once, and nothing sent
        lab.read("lockin_upper", "X")
    assert len(hanging.devices["GPIB0::10::INSTR"].sent) == sent
    rt._refresh()   # the Lab panel's look
    assert "isn't answering" in rt._status_held["lockin_upper"]   # the Lab panel leaves it alone, and says why
    said = rt.clear_bus("GPIB0", by="human (portal)")
    assert hanging.ifc == 1 and said.startswith("cleared GPIB0"), said
    assert "lockin_upper answers" in said and "smu left out" in said
    assert "GPIB0" not in BUSES.hung and bus_alarms[0].cleared is not None
    assert lab.instruments["smu"].unavailable.startswith("left out after GPIB0 hung")
    assert lab.read("lockin_bottom", "X") is not None
    assert events(tmp_path, "bus_cleared")[-1]["answered"] == 2


def test_a_hung_bus_that_answers_again_by_itself_is_noticed(tmp_path, hanging, monkeypatch):
    from labharness import monitor
    from labharness.adapters import BUSES
    rt = runtime(tmp_path, GPIB_LAB)
    hanging.hang()
    for name, param in [("lockin_upper", "X"), ("lockin_bottom", "X")]:
        with pytest.raises(InstrumentLost):
            rt.lab.read(name, param)
    rt.monitor.tick()
    assert "GPIB0" in BUSES.hung
    for d in hanging.devices.values():   # cleared elsewhere (NI MAX), or the culprit power-cycled
        d.silent = False
    monkeypatch.setattr(monitor, "BUS_PROBE_S", 0.0)
    rt.monitor.tick()
    assert "GPIB0" not in BUSES.hung


def test_what_an_instrument_that_does_not_answer_is_told_by_what_the_bus_said():
    lab = smu_lab()
    inst = lab.instruments["smu"]
    inst.config.address = "GPIB0::15::INSTR"
    inst.read("V")   # it answers

    class Slow:
        simulated = False

        def query(self, cmd):
            raise TimeoutError("VI_ERROR_TMO")

    class Nobody(Exception):
        error_code = -1073807265
    inst.backend = Slow()
    with pytest.raises(InstrumentLost, match=r"didn't answer :READ\? in time on GPIB \(address 15\); it answered"):
        inst._io(inst.backend.query, ":READ?")

    class Off:
        simulated = False

        def query(self, cmd):
            raise Nobody("VI_ERROR_NLISTENERS")
    inst.backend = Off()
    with pytest.raises(InstrumentLost, match="nothing listens there"):
        inst._io(inst.backend.query, "*IDN?")
    assert "cable or power" not in inst.health.lost_text


# ------------------------------------------------------------------ quitting, one Kelvin, the log, the collection

def test_a_second_kelvin_shows_the_one_that_runs(tmp_path, monkeypatch):
    from labharness import desktop
    lock = tmp_path / "app.json"
    assert desktop._one_kelvin(None, lock, "key") is True   # nothing runs: this one is the one
    shown = []
    monkeypatch.setattr(desktop, "_running_copy", lambda lock, wait=1.0: 51851)
    monkeypatch.setattr(desktop, "_show", lambda port, key, lock: shown.append(port))
    assert desktop._one_kelvin(None, lock, "key") is False and shown == [51851]


def test_kelvins_log_has_each_line_once(tmp_path):
    import logging
    from labharness import desktop
    level = desktop.log.level
    close = desktop._log_to_file(tmp_path)
    try:
        logging.basicConfig(format="%(message)s")   # as the MCP library does: nothing changes
        desktop.log.info("Kelvin running once")
        logging.getLogger("pyvisa").warning("a library's own warning")
        logging.getLogger("httpx").info("a library's chatter")
    finally:
        close()
        desktop.log.propagate = True
        desktop.log.setLevel(level)
    text = (tmp_path / "kelvin.log").read_text(encoding="utf-8")
    assert text.count("Kelvin running once") == 1 and text.count("a library's own warning") == 1
    assert "a library's chatter" not in text


def test_collection_files_are_kept_even_while_another_program_holds_them_a_moment(tmp_path, monkeypatch):
    import os
    from labharness import collection
    calls, real = [], os.replace

    def busy(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real(a, b)
    monkeypatch.setattr(collection.os, "replace", busy)
    monkeypatch.setattr(collection.time, "sleep", lambda s: None)
    collection._write(tmp_path / "index.json", b"{}")
    assert (tmp_path / "index.json").read_bytes() == b"{}" and len(calls) == 3


def test_one_kelvin_at_a_time_updates_the_collection(tmp_path):
    from labharness.collection import Collection
    from labharness.rundata import _hold, _let_go
    c = Collection(tmp_path)
    c.dir.mkdir(parents=True, exist_ok=True)
    held = _hold(c.dir / "sync.lock")   # another Kelvin updating it

    def no(url):
        raise AssertionError("fetched while another Kelvin updates it")
    try:
        c.sync(fetch=no)
    finally:
        _let_go(held)


def test_the_lab_log_numbers_its_own_lines(tmp_path):
    from labharness.eventlog import EventLog
    shared = tmp_path / "instrument-log.jsonl"
    nobody = EventLog(tmp_path / "lab" / "events.jsonl", shared=shared)
    ada = EventLog(tmp_path / "ada" / "events.jsonl", shared=shared, user="ada")
    for _ in range(3):
        ada.write("set")
    nobody.write("lab_connected")
    ada.write("set")
    lines = [json.loads(line) for line in shared.read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in lines] == [1, 2, 3, 4, 5]
    assert [e["own_seq"] for e in lines] == [1, 2, 3, 1, 4]


def test_got_it_three_times_is_kept_once(tmp_path):
    rt = runtime(tmp_path)
    a = rt.monitor._new("error", "smu", "smu reports an error")
    for _ in range(3):
        assert rt.acknowledge_alarm(a.id)
    assert len(events(tmp_path, "alarm_ack")) == 1


def test_a_tool_that_reads_instruments_waits_a_minute_at_most():
    pytest.importorskip("mcp")
    from labharness.agent.server import at_most
    late = threading.Event()
    said = at_most("smu.I", lambda: late.wait(5) and "late", seconds=0.2)
    late.set()
    assert said.startswith("still waiting for smu.I after")
    assert at_most("smu.V", lambda: "1 V", seconds=1) == "1 V"
    with pytest.raises(ZeroDivisionError):
        at_most("x", lambda: 1 / 0, seconds=1)


def test_others_that_may_hold_an_instrument_are_named(monkeypatch):
    from labharness import others

    class P:
        def __init__(self, pid, name, cmd=()):
            self.pid, self.info = pid, {"pid": pid, "name": name, "cmdline": list(cmd)}
    procs = [P(15668, "Kelvin.exe"), P(15669, "Kelvin.exe", ["Kelvin.exe", "--bridge", "x.py"]),
             P(4000, "MATLAB.exe"), P(1, "System")]

    class Me:
        pid = 2620

        def children(self, recursive=False):
            return []

        def ppid(self):
            return 0
    fake = types.SimpleNamespace(Process=lambda pid=None: Me(), process_iter=lambda attrs=None: iter(procs))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(others, "_last", (0.0, []))
    assert others.holders() == ["another Kelvin (process 15668)", "MATLAB"]
    assert others.holders_text() == "; running now: another Kelvin (process 15668), MATLAB"


# ------------------------------------------------------------------ on labsim: the field test's two Keithleys

BENCH = """name: Field test GPIB (labsim)
gpib_boards: 1
instruments:
  smu:
    model: keithley_2400
    address: GPIB0::15::INSTR
    continuous: true
  k30:
    model: keithley_2450_scpi2400
    address: GPIB0::30::INSTR
"""

ON_BENCH = """lab: Field test
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::15::INSTR
  k30:
    card: keithley/2400
    address: GPIB0::30::INSTR
"""


def test_on_labsim_a_2400_left_measuring_continuously_and_a_2450_in_2400_mode(labsim_bench, tmp_path):
    (tmp_path / "bench.yaml").write_text(BENCH, encoding="utf-8")
    labsim_bench(str(tmp_path / "bench.yaml"))
    lab = build_lab(ON_BENCH, partial=True)
    try:
        k30 = lab.health_check(["k30"])[0]
        assert k30.ok and "answers as a Keithley 2450" in str(k30), k30
        smu = lab.instruments["smu"]
        smu.write("output", True)
        with pytest.raises(InstrumentFault, match="measure continuously"):
            smu.read("I")
        assert "measure continuously" in str(lab.health_check(["smu"])[0])
        smu.act("stop_continuous")
        assert isinstance(smu.read("I"), float) and isinstance(smu.read("V_meas"), float)
        smu.write("output", False)
    finally:
        lab.close()


# ------------------------------------------------------------------ Kelvin's own looks, slow

class Counting:
    """A connection that counts what is asked of it, around a twin."""
    def __init__(self, inner):
        self.inner, self.asked = inner, []
        self.simulated = getattr(inner, "simulated", False)

    def query(self, cmd):
        self.asked.append(cmd)
        return self.inner.query(cmd)

    def write(self, cmd):
        self.inner.write(cmd)

    def close(self):
        pass


def test_the_lab_panel_reads_an_instrument_every_30_s_and_its_settings_every_2_minutes(tmp_path):
    import time as _time
    from labharness.agent import runtime as rtmod
    rt = runtime(tmp_path)
    smu = rt.lab.instruments["smu"]
    smu.backend = Counting(smu.backend)
    rt._looked.clear()   # (the runtime looked as it started)
    rt._settings_at.clear()
    rt._status_at = 0.0
    rt.ui_state()
    first = list(smu.backend.asked)
    assert first and any(c.upper().startswith(":SOUR:VOLT:RANG") or "RANG" in c.upper() for c in first)   # settings too
    for _ in range(3):   # the page asks every 4 s: nothing more is sent within 30 s
        rt._status_at = 0.0
        rt.ui_state()
    assert smu.backend.asked == first
    smu.write("V", 0.5)   # Kelvin changed it: the next look reads it at once
    rt._status_at = 0.0
    rt.ui_state()
    assert len(smu.backend.asked) > len(first) and rt._status["smu"]["V"] == 0.5
    first = list(smu.backend.asked)
    rt._looked["smu"] = _time.time() - rtmod.LOOK_S - 1   # 30 s later: its readings and outputs, not its settings
    rt._status_at = 0.0
    rt.ui_state()
    again = smu.backend.asked[len(first):]
    assert again and not any("RANG" in c.upper() or "NPLC" in c.upper() for c in again)
    assert "V_range" in rt._status["smu"]   # a setting not read now shows as it was read
    smu.backend.asked.clear()
    rt.ui_state(fresh=["smu"])   # a row just opened: read now, settings too
    assert any("RANG" in c.upper() for c in smu.backend.asked)


def test_nothing_is_read_in_the_background_on_a_bus_a_run_uses(tmp_path, monkeypatch):
    rt = runtime(tmp_path, GPIB_LAB.replace("GPIB0::15::INSTR", "sim://smu").replace("GPIB0::10::INSTR", "sim://lu")
                 .replace("GPIB0::9::INSTR", "sim://lb"))
    for name, addr in (("smu", "GPIB0::15::INSTR"), ("lockin_upper", "GPIB0::10::INSTR"),
                       ("lockin_bottom", "GPIB0::9::INSTR")):
        inst = rt.lab.instruments[name]
        inst.config.address = addr
        inst.backend = Counting(inst.backend)
        inst.backend.simulated = False
    monkeypatch.setattr(rt.lock, "owner", lambda: (1, frozenset({"smu"})))   # a run uses the 2400
    rt._looked.clear()
    rt._status_at = 0.0
    rt.ui_state()
    assert not rt.lab.instruments["lockin_upper"].backend.asked   # same bus: left to the run
    assert "measuring on GPIB0" in rt._status_held["lockin_upper"]


def test_error_queues_wait_while_a_plan_runs(tmp_path):
    rt = runtime(tmp_path)
    smu = rt.lab.instruments["smu"]
    smu.backend = Counting(smu.backend)
    smu.backend.simulated = False
    rt.gate._asked = {"smu.V": 1.0}   # a plan runs
    try:
        rt.monitor._check_errors(10 ** 6)
    finally:
        rt.gate._asked = None
    assert not smu.backend.asked
    rt.monitor._check_errors(2 * 10 ** 6)
    assert smu.backend.asked   # after it, once a minute as before


def test_a_retry_of_a_silent_instrument_waits_a_second_at_most():
    from labharness.adapters import VisaBackend
    from labharness import builtin_registry

    class Res:
        timeout, read_termination, write_termination = 5000, None, None

        def close(self):
            pass

    class RM:
        def open_resource(self, name):
            return Res()
    smu = VisaBackend("smu", builtin_registry().get("keithley/2400"), "GPIB0::15::INSTR", rm=RM())
    with smu.brief(1000):
        assert smu.res.timeout == 1000
    assert smu.res.timeout == 5000
