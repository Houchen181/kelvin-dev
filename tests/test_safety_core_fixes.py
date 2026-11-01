"""Bugs a hunt through the safety gate and the lab's core found (2026-10-31), each test made from its repro: a ramp
that never read its drive's guards, conditions on a mode or a switch that never compared, a heater switched on past an
interlock through the set point it applies, a reset sent with a held field, a wait whose reading failed, two axes of
one ANC300 on one link, a late Modbus TCP reply, Bring to safe waiting for 0 K, a twin that read the whole lab per
instrument, a "can't be read" alarm nothing could end, a Lake Shore 625's error registers, and stops that gave
nobody's reason."""
import re
import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from labharness import EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover, homelab
from labharness.cards import ParamSpec
from labharness.clock import SimClock
from labharness.conditions import parse_condition, typed
from labharness.instrument import SensorError
from labharness.lab import Lab
from labharness.labconfig import ConfigError, load_lab_config
from labharness.monitor import RESET_EVERY, Monitor, Watch
from simlab import DATA, build_virtual_lab

LAB = (DATA / "virtual_lab.yaml").read_text(encoding="utf-8")


def engine_on(lab, answers=()):
    approver = ScriptedApprover(list(answers))
    gate = SafetyGate(lab, approver, EventLog(None, lab.clock))
    engine = RunEngine(gate, RunStore(None))
    engine.analyze = False
    return engine, gate, approver


# ------------------------------------------------------------------ 1. a ramp reads its drive's guards

def test_a_ramp_away_from_safe_reads_its_drives_guards():
    """A ramp: sweep sets its set point without waiting, and only a set point's wait read its guards: with tc.setpoint
    guarded at |tc.T_sample| ≤ 20 K, a stepped sweep tripped at 20 K, while the ramp heated the sample to 50 K and
    ended "done", its dry run (auto mode's look) too. A ramp toward safe (a cool-down) isn't stopped by them, as a
    set point's wait toward safe isn't."""
    lab = build_virtual_lab(seed=1, config=LAB.replace(
        "    limits: { setpoint: [0, 300] }\n",
        "    limits: { setpoint: [0, 300] }\n    guards:\n      setpoint: [{ read: T_sample, max_abs: 20 }]\n"))
    engine, gate, _ = engine_on(lab, [True])
    report = engine.run("plan: heat\nsetup: { tc.heater_range: medium }\nsweep:\n"
                        "  tc.setpoint: { ramp: [{to: 50}], every: 30 }\nmeasure: [tc.T_sample]\n", dry_run=True)
    assert report.status == "aborted", report.message
    at = float(re.search(r"tc\.setpoint tripped at ([\d.]+) K: \|tc\.T_sample\| = [\d.]+ K > 20 K", report.message)
               .group(1))
    assert 20 <= at < 20.5 and lab.read("tc", "T_sample") < 20.5 and gate.trip_count == 1   # not 50 K
    assert lab.read("tc", "setpoint") == 0 and lab.read("tc", "heater_range") == "off"
    assert report.dry_run.status == "aborted" and "tripped at" in report.dry_run.message
    lab.instruments["tc"].backend.state.update(heater_range="medium", setpoint=30.0, T_sample=30.0)
    report = engine.run("plan: cool\nsweep:\n  tc.setpoint: { ramp: [{to: 2}], every: 30 }\nmeasure: [tc.T_sample]\n")
    assert report.status == "done", report.message


# ------------------------------------------------------------------ 2. a mode's or a switch's word in a condition

def test_an_interlock_or_a_plans_condition_on_a_mode_compares_its_word():
    """"off", "on", "true" and "false" in a condition became Python's True and False, which a mode's reading never
    equals (a heater range reads "off"): "tc.heater_range != off" always held, an interlock that never failed, and
    "== off" never did. Once the lab is known the word is the mode's own; one the mode never gives is an error."""
    text = LAB.replace('    require: "tc.T_sample < 10"\n', '    require: "tc.heater_range == off"\n')
    lab = build_virtual_lab(seed=1, config=text)
    assert lab.config.interlocks[0].require.value == "off"
    engine, gate, _ = engine_on(lab, [True])
    assert gate.set("magnet", "B", 0.5).ok                                   # the heater is off: it holds
    r = gate.set("tc", "heater_range", "low")
    assert r.action == "refused" and "magnet.B (now 500 mT) needs tc.heater_range == off" in r.message
    report = engine.run("plan: p\npreconditions: ['tc.heater_range != off']\ntime: {every: 1, points: 3}\n"
                        "measure: [tc.T_sample]\n")
    assert report.status == "refused" and "precondition tc.heater_range != off not met (now off)" in report.message
    report = engine.run("plan: q\ntime: {every: 1, points: 3}\nmeasure: [tc.T_sample]\nwhile: tc.heater_range == off\n")
    assert report.status == "done" and len(report.rows) == 3                  # it held all along
    with pytest.raises(ConfigError, match=r"interlocks\[0\]: tc.heater_range != of: heater_range must be one of "):
        build_virtual_lab(seed=1, config=text.replace("== off", "!= of"))
    shutter = parse_condition("laser.shutter == closed")                      # a switch's own words
    typed(shutter, ParamSpec("shutter", "drive", type="bool", words=("open", "closed")))
    assert shutter.value is False


def test_a_wake_up_until_a_mode_reads_its_word(tmp_path):
    pytest.importorskip("mcp")
    from test_wakeups import setup
    _, store, _, agent, _ = setup(tmp_path, [])
    chat = store.create("scripted:m1")
    assert not agent.waker.add(chat["id"], {"until": "tc.heater_range == off", "note": "x"})[1]
    assert agent.waker._check(agent.waker.pending[chat["id"]]).startswith("tc.heater_range == off is reached")
    assert agent.waker.add(chat["id"], {"until": "tc.heater_range == of", "note": "x"})[1]


# ------------------------------------------------------------------ 3. a switch that applies a set point

def test_switching_a_heater_on_is_checked_against_the_interlock_its_set_point_breaks():
    """The interlock check covered a set point that drives the interlock's reading, not the switch it goes through:
    tc.setpoint left at 15 K after a warm-up, the heater off, the field up at 1 T; switching the heater on passed, its
    card silent, and the sample heated to 15 K under "magnet.B requires tc.T_sample < 10". A plan that switches it on
    is refused before it runs, unless it sets the set point itself."""
    lab = build_virtual_lab(seed=1)
    lab.instruments["tc"].backend.state.update(heater_range="off", setpoint=15.0, T_sample=1.6)
    lab.instruments["magnet"].backend.state.update(B=1.0, B_actual=1.0)
    engine, gate, approver = engine_on(lab, [True, True])
    r = gate.set("tc", "heater_range", "medium")
    assert r.message == ("refused: interlock: magnet.B (now 1 T) needs tc.T_sample < 10; switching tc.heater_range to "
                         "medium would take tc.T_sample to 15 K (tc.setpoint), which breaks it, so bring magnet.B to "
                         "safe first, or set tc.setpoint where it holds")
    assert not approver.requests
    lab.clock.sleep(120)
    assert lab.read("tc", "heater_range") == "off" and lab.read("tc", "T_sample") < 2
    report = engine.run("plan: a\nsetup: { tc.heater_range: medium }\ntime: {every: 10, points: 2}\n"
                        "measure: [tc.T_sample]\n")
    assert report.status == "refused" and ("while tc.T_sample is at 15 K (the plan's setup sets tc.heater_range to "
                                           "medium, with tc.setpoint at 15 K)") in report.message
    report = engine.run("plan: b\nsetup: { tc.heater_range: medium, tc.setpoint: 5 }\ntime: {every: 10, points: 2}\n"
                        "measure: [tc.T_sample]\n")
    assert report.status == "done" and max(r["tc.T_sample"] for r in report.rows) < 10


# ------------------------------------------------------------------ 4. a reset, every drive at safe first

def test_a_reset_goes_out_only_once_every_drive_is_at_safe(lab, make_gate):
    """A card's reset goes out after the instrument's drives are brought to safe, but a drive the lab config holds on
    a stop (magnet.B: hold) was held at 1 T instead, and *RST went out with the field up while the result said "drives
    brought to safe". The hold is for stops: the reset brings the field down first."""
    gate, _ = make_gate([True])
    assert gate.set("magnet", "B", 1.0).ok
    backend = lab.instruments["magnet"].backend
    sent, write = [], backend.write

    def recording(command):
        sent.append((command, lab.read("magnet", "B_actual")))
        write(command)
    backend.write = recording
    r = gate.raw("magnet", "*RST")
    assert r.ok and r.message == "magnet: drives brought to safe, then sent '*RST'"
    assert [c for c, _ in sent] == ["FIELd:TARGet 0", "*RST"] and abs(sent[-1][1]) <= 0.001   # the field was down


# ------------------------------------------------------------------ 5. a wait whose reading fails

def test_a_wait_whose_reading_fails_gives_up_and_holds_its_set_point(lab, make_gate):
    """A set point's wait whose reading failed once (a sensor flagged, a GPIB timeout) ended "instrument error" with
    the set point left at 50 K: the sample went there unwatched, nothing held it, and no alarm went up. It ends as a
    wait that gave up: the set point back to the reading's last value, and an alarm."""
    gate, _ = make_gate([True, True])
    alarms = []
    gate.alarm = lambda kind, key, text, actions=None, **more: alarms.append((kind, key))
    assert gate.set("tc", "heater_range", "high").ok
    tc = lab.instruments["tc"]
    read, reads = tc.read, []

    def flaky(param):
        if param == "T_sample":
            reads.append(param)
            if len(reads) == 5:
                raise SensorError("tc.T_sample: the instrument flags this reading (sensor open?)")
        return read(param)
    tc.read = flaky
    r = gate.set("tc", "setpoint", 50)
    assert r.action == "timeout" and r.message.startswith(
        "tc.setpoint: T_sample can't be read (tc.T_sample: the instrument flags this reading (sensor open?)), so Kelvin "
        "stopped waiting; tc.setpoint set back to ") and r.message.endswith(", T_sample's last reading, not 50 K")
    assert read("setpoint") < 10 and alarms == [("stall", "tc.setpoint")]
    lab.clock.sleep(600)
    assert read("T_sample") < 10


def test_a_stage_whose_reading_fails_once_it_arrived_is_not_moved_back(tmp_path):
    """A stage whose status says the move is over isn't set back where it was when its encoder then can't be read:
    that would move it again."""
    from test_arrival import encoder_card
    card = encoder_card(busy={"get": "1TS", "match": "(28|1E)$"}, kind="drive", safe=0)
    registry = homelab.builtin_registry()
    registry.put(card)
    lab = build_virtual_lab(config={"lab": "stage", "instruments": {"stage": {"card": card.id}}}, registry=registry)
    stage = lab.instruments["stage"]
    read = stage.read
    stage.read = lambda p: (_ for _ in ()).throw(SensorError("no encoder")) if p == "position_actual" else read(p)
    _, gate, _ = engine_on(lab, [True])
    r = gate.set("stage", "position", 10)
    assert r.action == "error" and r.message == ("stage.position: its status says the move is over, but "
                                                 "position_actual can't be read (no encoder)")
    assert read("position") == 10


# ------------------------------------------------------------------ 6. one ANC300, one lock for its axes

def test_the_axes_of_one_anc300_share_its_link_and_its_lock():
    """Two lab entries for axes of one ANC300 share its connection, but each had its own lock: a reading of axis 1 (the
    monitor's alive probe, the Lab panel) while axis 2 stepped took the step's reply ("answered ''"), a false alarm
    that the controller doesn't answer. A fake ANC300 on 127.0.0.1 answers each command in turn."""
    from labharness.positioners import ANC300
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def controller():
        conn, _ = srv.accept()
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    if line.startswith(b"stepw"):
                        time.sleep(0.8)   # until the axis stops
                    conn.sendall(b"mode = stp\r\nOK\r\n" if line.startswith(b"getm") else b"OK\r\n")
        except OSError:   # Kelvin's end closed
            return
    threading.Thread(target=controller, daemon=True).start()
    card = SimpleNamespace(idn_query="ver", vendor="attocube", model="ANC300", connection={})
    address = "anc300://127.0.0.1:%d" % srv.getsockname()[1]
    x, y = ANC300("x", card, address), ANC300("y", card, address)
    try:
        assert x.dev is y.dev and x.lock is y.lock
        out = {}
        stepping = threading.Thread(target=lambda: out.update(y=y.write("SET steps2 500")))
        stepping.start()
        time.sleep(0.2)   # axis 2 is moving: now axis 1's mode is read
        out["x"] = x.query("GET mode1")
        stepping.join()
        assert out == {"x": "stp", "y": None}
    finally:
        x.close()
        y.close()
        srv.close()


# ------------------------------------------------------------------ 7. a Modbus TCP reply that came late

@pytest.mark.parametrize("pause", [0.4, 0.05])
def test_a_late_modbus_tcp_reply_is_never_taken_for_the_next(pause):
    """One reply that came after its time stayed in the connection, and every exchange after it read the one before's
    reply ("a reply to another request") until Kelvin restarted. A fake device on 127.0.0.1 answers its first request
    late; the next request goes after that reply came, or before it."""
    from labharness.adapters import AdapterError
    from labharness.modbus import ModbusInstrument
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def device():
        conn, _ = srv.accept()
        late = True
        try:
            while True:
                request = conn.recv(260)
                if not request:
                    return
                tid, _, _, unit = struct.unpack(">HHHB", request[:7])
                if late:
                    time.sleep(0.6)   # its timeout is 0.3 s
                    late = False
                pdu = bytes([3, 2]) + struct.pack(">H", 1234)
                conn.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
        except OSError:   # Kelvin's end closed
            return
    threading.Thread(target=device, daemon=True).start()
    card = SimpleNamespace(connection={"timeout_ms": 300}, idn_query="*IDN?", vendor="X", model="Y")
    m = ModbusInstrument("comp", card, "modbus://127.0.0.1:%d?unit=1" % srv.getsockname()[1])
    try:
        with pytest.raises(AdapterError, match="no reply"):
            m.query("READ HR 0 uint16")
        replies = []
        for _ in range(4):
            time.sleep(pause)
            replies.append(m.query("READ HR 0 uint16"))
        assert replies == ["1234"] * 4
    finally:
        m.close()
        srv.close()


# ------------------------------------------------------------------ 8. to safe, a set point at 0 K

def test_bring_to_safe_holds_a_set_point_whose_safe_value_is_0_k(lab, make_gate):
    """to_safe (the agent's tool, and a reset's first step) waited for tc.setpoint to reach 0 K: 13 minutes, then
    "timeout" and a latched "may be stuck" alarm. As after a stop, a set point no instrument reaches is held where its
    reading is, and nothing waits."""
    gate, _ = make_gate([])
    alarms = []
    gate.alarm = lambda kind, key, text, actions=None, **more: alarms.append(kind)
    lab.instruments["tc"].backend.state.update(heater_range="medium", setpoint=20.0, T_sample=12.0)
    t0 = lab.clock.now()
    results = gate.to_safe("tc")
    assert [(r.action, r.ok) for r in results] == [("set", True), ("held", True)]
    assert results[1].message.startswith("tc.setpoint held at 12") and results[1].message.endswith(
        "(its safe 0 K can't be reached)")
    assert lab.clock.now() - t0 < 1 and not alarms and lab.read("tc", "heater_range") == "off"


# ------------------------------------------------------------------ 9. a twin of the lab

def test_a_twin_reads_each_instrument_once(lab):
    """make_twin (every dry run: simulate_plan, auto mode's look, a queue's rehearsal) read the whole lab once per real
    instrument: 530 queries for the test lab's 53, and one that doesn't answer timed out once per instrument."""
    count = [0]

    class Real:
        """A real instrument's backend as the lab sees it: no state of its own (a VISA backend has none)."""
        simulated = False

        def __init__(self, inner):
            self._inner = inner

        def write(self, command):
            return self._inner.write(command)

        def query(self, command):
            count[0] += 1
            return self._inner.query(command)

        def close(self):
            pass
    for inst in lab.instruments.values():
        inst.backend = Real(inst.backend)
    lab.snapshot()
    once, count[0] = count[0], 0
    lab.make_twin()
    assert once > 40 and count[0] == once


# ------------------------------------------------------------------ 10. a "can't be read" alarm with no watch left

def test_a_cant_be_read_alarm_clears_once_no_watch_reads_its_instrument(lab, make_gate):
    """A watch whose reading can't be parsed raises "<instr> can't be read". Removing the watch cleared only the
    watch's own alarm, and Got it doesn't end one that isn't latched: it stayed on until a restart."""
    gate, _ = make_gate([])
    monitor = Monitor(lab, gate, lambda *a: None)
    w = monitor.add(Watch("lockin.X", above=1.0, every=1.0))
    inst = lab.instruments["lockin"]
    read = inst.read
    inst.read = lambda p: (_ for _ in ()).throw(ValueError("X needs a number, got 'OVLD'")) if p == "X" else read(p)
    for _ in range(4):
        lab.clock.sleep(2)
        monitor.tick()
    [alarm] = monitor.active()
    assert alarm.text == "lockin can't be read (X needs a number, got 'OVLD')"
    monitor.remove(w.id)
    assert not monitor.active() and alarm.cleared_text == ("lockin is no longer read: no watch is on it (its watch "
                                                           "was removed)")


# ------------------------------------------------------------------ 11. a Lake Shore 625's error registers

def test_a_lake_shore_625s_error_registers_are_read_whole():
    """The 625's ERST? answers three registers, hardware, operational and persistent switch heater (its manual, 5-37:
    nnn,nnn,nnn): a quench is 000,032,000. Only the first was read, as SCPI's error code, so a quench or a heater
    fault read as no error."""
    reg = homelab.builtin_registry()
    replies = iter(["000,032,000", "000,000,001", "000,000,000"])

    class Supply:
        simulated = False

        def query(self, command):
            return next(replies)

        def write(self, command):
            pass

        def close(self):
            pass
    cfg = load_lab_config({"lab": "x", "instruments": {"magnet": {"card": "lakeshore/625",
                                                                  "address": "ASRL5::INSTR"}}}, reg)
    inst = Lab(cfg, reg, lambda n, c, a: Supply(), SimClock()).instruments["magnet"]
    assert inst.instrument_error() == "000,032,000"
    assert inst.instrument_error() == "000,000,001"
    assert inst.instrument_error() is None


# ------------------------------------------------------------------ 12. a stop that isn't a person's says whose

def test_a_watch_or_a_restart_that_stops_a_run_says_so(lab, make_engine):
    """A lab watch's stop (stop_run: a lab watch with actions has it) and an output found off under a run (a restart)
    stopped the run as a person's Stop does, saying "stop requested" while the alarm was theirs."""
    engine, gate, _ = make_engine([])
    monitor = Monitor(lab, gate, lambda *a: None)
    monitor.add(Watch("tc.T_sample", above=20, for_s=10, every=1, then=[("laser", "emission", False)], stop_run=True,
                      source="lab"))
    tc, points = lab.instruments["tc"], []
    read = tc.read

    def progress(event):
        if event.get("event") == "point":
            points.append(event)
            if len(points) == 2:
                tc.read = lambda param: 25.0 if param == "T_sample" else read(param)
            if len(points) >= 2:
                monitor.tick()
    engine.progress = progress
    report = engine.run("plan: p\ntime: {every: 15, points: 6}\nmeasure: [tc.T_sample]\n")
    assert report.status == "aborted" and ("aborted after 3/6 points: stopped by a watch (tc.T_sample = 25 K is above "
                                           "20 K for 10s)") in report.message
    tc.read = read
    smu = lab.instruments["smu"].backend
    smu.simulated = False   # as a real instrument: the monitor checks it
    gate.approver.answers.append(True)
    with gate.preapproved({}):
        assert gate.set("smu", "output", True).ok                    # the run switched it on
        smu.state["output"] = False                                  # a power cut: back with its power-on settings
        for _ in range(3):
            lab.clock.sleep(RESET_EVERY)
            monitor.tick()
    assert gate.stop_words() == ("stopped by smu.output, found off under the running plan (smu restarted, or someone "
                                 "switched it off)")
