"""Real pulse generators and photon counters for spin experiments (Swabian Pulse Streamer, SpinCore PulseBlaster,
Swabian Time Tagger), each driven through a fake of its vendor library that records what Kelvin asks of it."""
import types

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, pulses
from labharness.adapters import AdapterError
from labharness.drivers import MODULES, PLAYING, TimeTagger, discover_swabian
from labharness.virtual_lab import build_lab


# ------------------------------------------------------------------ fake vendor libraries

class FakeSequence:
    def __init__(self):
        self.digital = {}

    def setDigital(self, channel, pattern):
        assert 0 <= channel < 8
        self.digital[channel] = list(pattern)


class OutputState:
    def __init__(self, digi, a0=0.0, a1=0.0):
        self.digi = list(digi)

    @classmethod
    def ZERO(cls):
        return cls([])


class FakePulseStreamer:
    REPEAT_INFINITELY = -1
    last = None

    def __init__(self, ip):
        self.ip, self.streams, self.constants = ip, [], []
        FakePulseStreamer.last = self

    def createSequence(self):
        return FakeSequence()

    def stream(self, seq, n_runs, final):
        self.streams.append((seq, n_runs, final))

    def constant(self, state):
        self.constants.append(state)

    def getSerial(self):
        return "00:26:32:f0:3a:12"

    def getFirmwareVersion(self):
        return "1.7.2"


pulsestreamer = types.SimpleNamespace(PulseStreamer=FakePulseStreamer, OutputState=OutputState,
                                      findPulseStreamers=lambda: [types.SimpleNamespace(ip="10.0.0.9", serial="ab:cd")])


class FakeSpinAPI(types.SimpleNamespace):
    PULSE_PROGRAM, CONTINUE, STOP, BRANCH = 0, 0, 1, 6

    def __init__(self):
        super().__init__(program=[], log=[])

    def pb_count_boards(self):
        return 1

    def pb_select_board(self, n):
        self.log.append(("select", n))

    def pb_init(self):
        self.log.append("init")

    def pb_core_clock(self, mhz):
        self.log.append(("clock", mhz))

    def pb_start_programming(self, target):
        self.program = []

    def pb_inst_pbonly(self, flags, inst, data, length):
        self.program.append((flags, inst, data, length))
        return len(self.program) - 1

    def pb_stop_programming(self):
        self.log.append("programmed")

    def pb_reset(self):
        self.log.append("reset")

    def pb_start(self):
        self.log.append("start")

    def pb_close(self):
        self.log.append("close")

    def pb_get_version(self):
        return b"20250210"


class FakeGated:
    """Counts per gate window, alternating; the record can start on either window."""
    def __init__(self, tagger, clicks, begin, end, n):
        self.args = (clicks, begin, end, n)
        tagger.measurements.append(self)

    def startFor(self, ps, clear):
        self.ps = ps

    def waitUntilFinished(self, ms):
        return True

    def getData(self):
        return [list(self.tagger_data)]

    def getBinWidths(self):
        return [300_000] * len(self.tagger_data) + [0, 0]

    def getIndex(self):
        return self.tagger_index + [0, 0]


class FakeTagger:
    def __init__(self, serial):
        self.serial, self.measurements, self.levels = serial, [], {}

    def setTriggerLevel(self, ch, v):
        self.levels[ch] = v

    def sync(self):
        pass

    def getModel(self):
        return "Ultra"

    def getSerial(self):
        return "1234000ABC"


class FakeRate:
    def __init__(self, tagger, chans):
        self.chans = chans

    def startFor(self, ps, clear):
        pass

    def waitUntilFinished(self, ms):
        return True

    def getData(self):
        return [123456.0]


timetagger = types.SimpleNamespace(createTimeTagger=FakeTagger, GatedCounter=FakeGated, Countrate=FakeRate,
                                   freeTimeTagger=lambda t: None, scanTimeTagger=lambda: ["1234000ABC"])


@pytest.fixture
def vendors():
    spin = FakeSpinAPI()
    MODULES.update(pulsestreamer=pulsestreamer, pulseblaster=spin, timetagger=timetagger)
    yield spin
    MODULES.clear()
    PLAYING.clear()


def gate_for(lab, tmp_path, answers=()):
    return SafetyGate(lab, ScriptedApprover(answers), EventLog(tmp_path / "events.jsonl", lab.clock))


NV_LAB = """
lab: NV
instruments:
  pg: { card: swabian/pulse-streamer, address: "pulsestreamer://169.254.8.2",
        vars: { laser: "3", mw: "1", gate: "5", laser_delay: "4.5e-7", max_mw_duty: "0.2" } }
"""


def test_the_pulse_streamer_plays_kelvins_sequence(vendors, tmp_path):
    lab = build_lab(NV_LAB)
    ps = FakePulseStreamer.last
    assert ps.ip == "169.254.8.2" and lab.instruments["pg"].backend.query("*IDN?").startswith("Swabian Instruments,Pulse")
    gate = gate_for(lab, tmp_path, [True, True])
    for k, v in (("protocol", "rabi"), ("tau", 2e-7), ("t_pi", 1e-7)):
        assert gate.set("pg", k, v).ok
    assert ps.streams == []                                               # nothing plays until it is switched on
    assert gate.set("pg", "running", True).ok                             # a drive: asked first
    seq, n_runs, final = ps.streams[-1]
    assert n_runs == -1 and final.digi == []
    assert set(seq.digital) == {3, 1, 5}                                  # the lab's wiring
    totals = {sum(n for n, _ in p) for p in seq.digital.values()}
    assert len(totals) == 1                                               # every channel spans one period
    expected = pulses.build("rabi", {"tau": 2e-7, "t_pi": 1e-7}, {"laser": 4.5e-7})
    assert seq.digital[1] == expected.pattern("mw") and seq.digital[3] == expected.pattern("laser")
    assert PLAYING["pg"].protocol == "rabi"
    r = gate.set("pg", "tau", 3e-6)                                       # MW above this lab's 20 % duty limit
    assert not r.ok and "microwave is on" in r.message and len(ps.streams) == 1
    assert lab.read("pg", "tau") == pytest.approx(2e-7)                   # the refused value never took
    assert gate.set("pg", "running", False).ok and ps.constants[-1].digi == []   # stopped: every output low
    assert "pg" not in PLAYING


def test_the_pulseblaster_loops_and_stops_dark(vendors, tmp_path):
    spin = vendors
    lab = build_lab("""
lab: NV
instruments:
  pb: { card: spincore/pulseblaster, address: "pulseblaster://0?clock=500&pro=1",
        vars: { laser: "0", mw: "2", gate: "4" } }
""")
    assert ("clock", 500.0) in spin.log and lab.instruments["pb"].backend.query("*IDN?") == "SpinCore,PulseBlaster,board 0,20250210"
    gate = gate_for(lab, tmp_path, [True])
    assert gate.set("pb", "protocol", "pulsed_odmr").ok and gate.set("pb", "running", True).ok
    prog = spin.program
    assert all(flags & 0xE00000 == 0xE00000 for flags, *_ in prog)        # ESR-PRO: ordinary pulses
    assert prog[-1][1] == spin.BRANCH and prog[-1][2] == 0                # loops back to the start
    seq = PLAYING["pb"]
    assert sum(length for *_, length in prog) == pytest.approx(seq.period * 1e9)
    mw_on = [i for i, (flags, *_) in enumerate(prog) if flags & (1 << 2)]
    assert mw_on and prog[mw_on[0]][3] == pytest.approx(100.0)            # the 100 ns pi pulse
    assert gate.set("pb", "running", False).ok
    assert spin.program[0][0] & 0x1FFFFF == 0 and spin.program[-1][1] == spin.STOP   # parked all-off
    r = gate.set("pb", "t_gate", 1.5e-6)                                  # not running: only stored
    assert r.ok


def test_the_pulseblaster_refuses_edges_closer_than_it_can_switch(vendors, tmp_path):
    lab = build_lab("""
lab: NV
instruments:
  pb: { card: spincore/pulseblaster, address: "pulseblaster://0?clock=100" }
""")
    gate = gate_for(lab, tmp_path, [True])
    assert gate.set("pb", "protocol", "rabi").ok and gate.set("pb", "tau", 2e-8).ok   # 20 ns < 5 cycles at 100 MHz
    r = gate.set("pb", "running", True)
    assert not r.ok and ("resolution" in r.message or "shortest" in r.message)


def test_the_time_tagger_finds_which_window_is_the_signal(vendors):
    seq = pulses.build("rabi", {"tau": 1e-7})
    PLAYING["pg"] = seq
    (_, s0, _), (_, r0, _) = seq.windows
    within, across = int((r0 - s0) * 1e12), int((seq.period - (r0 - s0)) * 1e12)
    tagger = FakeTagger("")
    counter = TimeTagger("counter", types.SimpleNamespace(idn_query="*IDN?"), "timetagger://?click=1&gate=2",
                         module=types.SimpleNamespace(createTimeTagger=lambda s: tagger, GatedCounter=FakeGated,
                                                      Countrate=FakeRate))
    # the record starts on a reference window: ref 100, sig 90, ref 100, sig 90, ref 100
    FakeGated.tagger_data = [100, 90, 100, 90, 100]
    FakeGated.tagger_index = [0, across, across + within, 2 * across + within, 2 * across + 2 * within]
    assert counter.acquire(1.0) == (180.0, 200.0)                         # pairs (90, 100), (90, 100)
    assert tagger.measurements[-1].args == ([1], 2, -2, TimeTagger.BINS)  # the gate's falling edge closes a window
    FakeGated.tagger_data = [90, 100, 90, 100]                             # starting on a signal window
    FakeGated.tagger_index = [0, within, within + across, 2 * within + across]
    assert counter.acquire(1.0) == (180.0, 200.0)
    assert counter.count_rate(1.0) == 123456.0
    FakeGated.tagger_data, FakeGated.tagger_index = [], []
    with pytest.raises(AdapterError, match="no gate windows"):
        counter.acquire(1.0)


def test_one_acquisition_serves_signal_reference_and_contrast(vendors):
    calls = []
    counter = TimeTagger("counter", types.SimpleNamespace(idn_query="*IDN?"), "timetagger://X?click=1&gate=2",
                         module=types.SimpleNamespace(createTimeTagger=lambda s: FakeTagger(s), GatedCounter=FakeGated,
                                                      Countrate=FakeRate))
    counter.acquire = lambda seconds: calls.append(seconds) or (90.0, 100.0)
    counter.write("SET integration 2")
    assert counter.query("GET signal") == "90" and counter.query("GET reference") == "100"
    assert counter.query("GET contrast") == "0.9" and calls == [2.0]
    assert counter.query("GET signal") == "90" and calls == [2.0, 2.0]       # read again: a new acquisition


def test_a_scan_finds_swabian_devices(vendors):
    assert discover_swabian() == [("pulsestreamer://10.0.0.9", "Swabian Instruments,Pulse Streamer 8/2,ab:cd"),
                                  ("timetagger://1234000ABC", "Swabian Instruments,Time Tagger,1234000ABC")]
