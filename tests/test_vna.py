"""Network analyzers: an S21 scan is one sweep read as a trace. An HP 8719ES sends its trace as a line per point, two
numbers on each, so Kelvin reads a trace to the end of the transfer (EOI) and keeps one number per point; Kelvin's
Keysight PNA card reads it as text whatever data format another program left; a trace sent as real and imaginary
pairs becomes one value per point; a binary block where text was expected says so (and the next replies are not its
leftovers); plots keep a narrow resonance and leave out a point the instrument could not measure."""
import cmath
import math
import re
import socket
import struct
import threading
import time

import pytest

from labharness import RunEngine, SafetyGate, ScriptedApprover
from labharness.adapters import set_resource_manager
from labharness.cards import BINARY_REPLY, CardError, parse_card, plot_points, thin, trace_numbers
from labharness.virtual_lab import build_lab, lab_registry
from labharness.yamlio import load_yaml

F0, KAPPA = 4.0e9, 2.0e6   # the stand-in's resonance: a dip at 4 GHz, 2 MHz wide


def s21(f):
    """A notch: -3 dB off resonance, -23 dB at it, with the phase turning through it."""
    return 0.708 * (1 - 0.9 / (1 + 2j * (f - F0) / KAPPA))


class PNA:
    """A Keysight PNA on a TCP socket, as its SCPI help describes it: channel 1, measurement 1, formatted data (one
    number per point in log magnitude or phase), complex data as real and imaginary pairs, ASCII or REAL,64."""

    def __init__(self, data_format="REAL,64"):
        self.state = {"start": 3.99e9, "stop": 4.01e9, "points": 201, "ifbw": 1e3, "power": -20.0, "rf": 1,
                      "aver": 0, "count": 1, "param": "S11", "format": "MLOG", "data": data_format, "border": "NORM"}
        self.heard = []
        self.sweeping = False   # a sweep that never ends: *OPC? never answers
        self.connections = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def trace(self):
        st = self.state
        n = st["points"]
        fs = [st["start"] + (st["stop"] - st["start"]) * i / max(1, n - 1) for i in range(n)]
        zs = [s21(f) if st["param"] == "S21" else 0.5 + 0j for f in fs]
        return zs

    def block(self, values):
        if self.state["data"].startswith("ASC"):
            return (",".join("%+.11E" % v for v in values) + "\n").encode()
        raw = struct.pack(("<" if self.state["border"] == "SWAP" else ">") + "%dd" % len(values), *values)
        return b"#%d%d" % (len(str(len(raw))), len(raw)) + raw + b"\n"

    def answer(self, line):
        self.heard.append(line)
        st, head, arg = self.state, line.split(" ")[0].upper(), line.partition(" ")[2].strip()
        number = {"SENSE1:FREQUENCY:START": "start", "SENSE1:FREQUENCY:STOP": "stop", "SENSE1:SWEEP:POINTS": "points",
                  "SENSE1:BANDWIDTH": "ifbw", "SOURCE1:POWER1": "power", "OUTPUT": "rf", "SENSE1:AVERAGE:STATE": "aver",
                  "SENSE1:AVERAGE:COUNT": "count"}
        if head == "*IDN?":
            return b"Keysight Technologies,N5227B,MY12345678,A.17.20.05\n"
        if head == "*OPC?":
            return None if self.sweeping else b"+1\n"
        if head in ("SYSTEM:ERROR?", "SYST:ERR?"):
            return b'+0,"No error"\n'
        if head.rstrip("?") in number:
            key = number[head.rstrip("?")]
            if head.endswith("?"):
                return ("%d\n" % st[key] if key in ("points", "rf", "aver", "count") else "%+.11E\n" % st[key]).encode()
            st[key] = {"ON": 1, "OFF": 0}.get(arg.upper(), None) if arg.upper() in ("ON", "OFF") else float(arg)
            if key in ("points", "rf", "aver", "count"):
                st[key] = int(st[key])
            return None
        if head == "FORMAT:DATA":
            st["data"] = arg.upper()
        elif head == "FORMAT:BORDER":
            st["border"] = arg.upper()
        elif head == "CALCULATE1:MEASURE1:PARAMETER":
            st["param"] = arg.strip("'\"")
        elif head == "CALCULATE1:MEASURE1:FORMAT":
            st["format"] = arg.upper()[:4]
        elif head == "CALCULATE1:MEASURE1:DATA:FDATA?":
            zs = self.trace()
            if st["format"] == "PHAS":
                return self.block([math.degrees(cmath.phase(z)) for z in zs])
            return self.block([20 * math.log10(abs(z)) for z in zs])
        elif head == "CALCULATE1:DATA?" and arg.upper() == "SDATA":
            return self.block([x for z in self.trace() for x in (z.real, z.imag)])
        return None

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.talk, args=(conn,), daemon=True).start()

    def talk(self, conn):
        buf = b""
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                reply = self.answer(line.decode().strip())
                if reply is not None:
                    conn.sendall(reply)
        conn.close()


DRAFT = """card: 1
id: lab/pna-draft
model: A PNA card as a model might draft it
vendor: Keysight
kind: vna
status: draft
identify: { idn: "^Keysight Technologies,N5227B" }
parameters:
  f_start: { label: Start, kind: measure, unit: Hz, get: "SENSe1:FREQuency:STARt?" }
  f_stop: { label: Stop, kind: measure, unit: Hz, get: "SENSe1:FREQuency:STOP?" }
  S21:
    label: S21 (formatted data, in whatever format the analyzer is set to)
    kind: measure
    type: trace
    unit: dB
    get: "CALCulate1:MEASure1:DATA:FDATA?"
    axis: { start: f_start, stop: f_stop }
  S21_complex:
    label: S21 from its real and imaginary parts
    kind: measure
    type: trace
    unit: dB
    get: "CALCulate1:DATA? SDATA"
    pairs: true
    y: "20*log10(hypot(re, im))"
    axis: { start: f_start, stop: f_stop }
  S21_re:
    label: The real parts alone
    kind: measure
    type: trace
    get: "CALCulate1:DATA? SDATA"
    pairs: true
"""


@pytest.fixture
def vna(tmp_path):
    pyvisa = pytest.importorskip("pyvisa")
    pytest.importorskip("pyvisa_py")
    pytest.importorskip("numpy")
    rm = pyvisa.ResourceManager("@py")
    set_resource_manager(rm)
    server, drafted = PNA(), PNA()   # left in REAL,64 by another program, as a lab's own scripts often do
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "draft.yaml").write_text(DRAFT, encoding="utf-8")
    lab = build_lab("lab: Bench\ninstruments:\n  vna: { card: keysight/pna, address: \"TCPIP0::127.0.0.1::%d::SOCKET\" }\n"
                    "  draft: { card: lab/pna-draft, address: \"TCPIP0::127.0.0.1::%d::SOCKET\" }\n"
                    % (server.port, drafted.port), registry=lab_registry(tmp_path))
    yield lab, server, drafted
    lab.close()
    for s in (server, drafted):
        s.sock.close()
    set_resource_manager(None)
    rm.close()


def test_an_s21_scan_with_kelvins_pna_card(vna):
    lab, server, _ = vna
    run = RunEngine(SafetyGate(lab, ScriptedApprover([]))).run("""
plan: s21
setup: { vna.f_start: 3.99e9, vna.f_stop: 4.01e9, vna.points: 401, vna.ifbw: 1000 }
measure: [vna.S21, vna.S21_phase]
""")
    assert run.status == "done", run.message
    row = run.rows[0]
    ys, phase = row["vna.S21"], row["vna.S21_phase"]
    assert len(ys) == len(phase) == 401 and row["vna.S21.axis"] == pytest.approx([3.99e9, 4.01e9])
    dip = min(range(401), key=ys.__getitem__)
    assert dip == 200 and ys[dip] == pytest.approx(20 * math.log10(abs(s21(F0))), abs=1e-6)   # the dip at 4 GHz
    assert ys[0] == pytest.approx(20 * math.log10(abs(s21(3.99e9))), abs=1e-6)
    assert phase[0] == pytest.approx(math.degrees(cmath.phase(s21(3.99e9))), abs=1e-6)
    # the reading asked for text, made measurement 1 S21, took one sweep and waited for it, then read
    heard = server.heard
    read = heard.index("CALCulate1:MEASure1:DATA:FDATA?")
    for before in ("FORMat:DATA ASCii,0", "CALCulate1:MEASure1:PARameter 'S21'", "CALCulate1:MEASure1:FORMat "
                   "MLOGarithmic", "SENSe1:SWEep:MODE SINGle", "*OPC?"):
        assert before in heard[:read], before
    assert heard.index("SENSe1:SWEep:MODE SINGle") < heard.index("*OPC?") < read


def test_a_stop_during_a_sweep_on_a_plain_socket_connects_again(vna, monkeypatch):
    from labharness import adapters
    from labharness.instrument import Stopped
    monkeypatch.setattr(adapters, "WAIT_SLICE_MS", 100)
    lab, server, _ = vna
    server.sweeping = True   # *OPC? would answer only after the card's 3600 s
    outcome = {}

    def read():
        try:
            outcome["value"] = lab.read("vna", "S21")
        except Exception as e:
            outcome["error"] = e
    t0 = time.time()
    worker = threading.Thread(target=read)
    worker.start()
    time.sleep(0.3)
    lab.stops.pulse()   # the stop button
    worker.join(3)
    assert not worker.is_alive() and time.time() - t0 < 2 and isinstance(outcome.get("error"), Stopped)
    assert server.connections == 2   # a new connection, so the +1 the old one owes is never read as an answer
    assert lab.instruments["vna"].backend.res.timeout == 3000   # its own timeout, not the sweep's hour
    server.sweeping = False
    assert lab.read("vna", "f_start") == pytest.approx(3.99e9)


def test_a_binary_block_where_text_was_expected_says_so(vna):
    lab = vna[0]
    with pytest.raises(Exception) as err:   # the drafted card reads FDATA as text; the analyzer sends REAL,64
        lab.read("draft", "S21")
    assert "came in binary, not as text" in str(err.value) and "FORMat:DATA ASCii" in str(err.value)
    assert lab.read("draft", "f_start") == pytest.approx(3.99e9)   # the block's leftovers were dropped


def test_real_and_imaginary_pairs_make_one_value_per_point(vna):
    lab, _, drafted = vna
    drafted.state.update(points=101, data="ASCII,0", param="S21")
    complex_db = lab.read("draft", "S21_complex")
    assert len(complex_db) == 101
    assert complex_db[50] == pytest.approx(20 * math.log10(abs(s21(F0))))
    assert lab.read("draft", "S21_re")[50] == pytest.approx(s21(F0).real)   # pairs without y: the first numbers


class HP8719:
    """An HP 8719ES on GPIB, as its programmer's guide describes it: HP's own mnemonics (STAR, POIN, SOUPON, S21,
    LOGM), IDN?, OPC? answering 1 once the next command is done, and the formatted trace (OUTPFORM) in FORM4: 24
    characters a number, the two numbers of a point separated by a comma, a line feed after each point."""

    def __init__(self):
        self.state = {"start": 3.99e9, "stop": 4.01e9, "points": 201, "ifbw": 3000.0, "power": -10.0, "soup": 1,
                      "param": "S11", "format": "LOGM", "form": "FORM3"}   # left in binary by another program
        self.sent = []
        self.out = ""   # the output queue, sent with EOI on its last byte
        self.sweeping = False   # a sweep that never ends: OPC? never answers
        self.cleared = 0

    def handle(self, message):
        self.sent.append(message)
        st, done = self.state, False
        for cmd in [c.strip().upper() for c in message.split(";") if c.strip()]:
            whole = re.fullmatch(r"FORM[1-5]|S[12][12]", cmd)   # mnemonics that end in digits
            head, value = (cmd, None) if whole else re.match(r"([A-Z]+)(\?|ON|OFF|\s*[-+0-9.E]+)?$", cmd).groups()
            value = (value or "").strip()
            key = {"STAR": "start", "STOP": "stop", "POIN": "points", "IFBW": "ifbw", "POWE": "power",
                   "SOUP": "soup"}.get(head)
            if head == "IDN" and value == "?":
                self.out += "HEWLETT PACKARD,8719ES,0,7.74\n"
            elif head == "OPC" and value == "?":
                done = True
            elif key and value == "?":
                self.out += "%24.15E\n" % st[key] if key != "soup" else "%d\n" % st[key]
            elif key:
                st[key] = {"ON": 1, "OFF": 0}.get(value, None) if value in ("ON", "OFF") else float(value)
                if key == "points":
                    st[key] = int(st[key])
            elif head in ("S11", "S12", "S21", "S22"):
                st["param"] = head
            elif head in ("LOGM", "PHAS"):
                st["format"] = head
            elif head.startswith("FORM"):
                st["form"] = head
            elif head == "OUTPFORM":
                assert st["form"] == "FORM4"
                n = st["points"]
                for i in range(n):
                    z = s21(st["start"] + (st["stop"] - st["start"]) * i / (n - 1)) if st["param"] == "S21" else 0.5
                    v = 20 * math.log10(abs(z)) if st["format"] == "LOGM" else math.degrees(cmath.phase(z))
                    self.out += "%24.15E,%24.15E\n" % (v, 0.0)
            elif head == "OUTPERRO":
                self.out += '0,"NO ERRORS"\n'
        if done and not self.sweeping:
            self.out += "1\n"


class GpibSession:
    """A GPIB session as PyVISA gives it: a read stops at the read termination when one is set (and drops it), and
    otherwise at EOI, the end of what the instrument queued."""

    def __init__(self, dev):
        self.dev = dev
        self.timeout, self.read_termination, self.write_termination = 3000, "\n", "\n"

    def write(self, cmd):
        self.dev.handle(cmd)

    def read(self):
        if not self.dev.out:
            time.sleep(min(self.timeout, 200) / 1000.0)   # waits out its timeout, as VISA does
            raise TimeoutError("VI_ERROR_TMO")
        if self.read_termination:
            line, _, self.dev.out = self.dev.out.partition(self.read_termination)
            return line
        out, self.dev.out = self.dev.out, ""
        return out

    def query(self, cmd, delay=None):
        self.write(cmd)
        return self.read()

    def clear(self):   # a device clear: the output queue emptied, a pending OPC? forgotten
        self.dev.out = ""
        self.dev.cleared += 1

    def close(self):
        pass


class GpibBus:
    def __init__(self, devices):
        self.devices = devices

    def list_resources(self, query="?*::INSTR"):
        return tuple(self.devices)

    def open_resource(self, name):
        return GpibSession(self.devices[name])


HP_DRAFT = """card: 1
id: lab/hp8719-draft
model: An HP 8719ES card as a model might draft it, before pairs
vendor: HP
kind: vna
status: draft
identify: { idn: "8719ES", query: "IDN?" }
parameters:
  f_start: { label: Start, kind: measure, unit: Hz, get: "STAR?" }
  trace: { label: Formatted trace, kind: measure, type: trace, get: "OUTPFORM" }
"""


@pytest.fixture
def hp(tmp_path):
    analyzer, drafted = HP8719(), HP8719()
    set_resource_manager(GpibBus({"GPIB0::16::INSTR": analyzer, "GPIB0::17::INSTR": drafted}))
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "draft.yaml").write_text(HP_DRAFT, encoding="utf-8")
    lab = build_lab("lab: Bench\ninstruments:\n  vna: { card: hp/8720, address: \"GPIB0::16::INSTR\" }\n"
                    "  draft: { card: lab/hp8719-draft, address: \"GPIB0::17::INSTR\" }\n",
                    registry=lab_registry(tmp_path))
    yield lab, analyzer, drafted
    lab.close()
    set_resource_manager(None)


def test_an_s21_scan_on_an_hp_8719es(hp):
    lab, analyzer, _ = hp
    run = RunEngine(SafetyGate(lab, ScriptedApprover([]))).run("""
plan: s21
setup: { vna.f_start: 3.99e9, vna.f_stop: 4.01e9, vna.points: 401, vna.ifbw: 1000 }
measure: [vna.S21, vna.S21_phase]
""")
    assert run.status == "done", run.message
    row = run.rows[0]
    ys, phase = row["vna.S21"], row["vna.S21_phase"]
    assert len(ys) == len(phase) == 401 and row["vna.S21.axis"] == pytest.approx([3.99e9, 4.01e9])
    dip = min(range(401), key=ys.__getitem__)
    assert dip == 200 and ys[dip] == pytest.approx(20 * math.log10(abs(s21(F0))), abs=1e-9)
    assert phase[0] == pytest.approx(math.degrees(cmath.phase(s21(3.99e9))), abs=1e-9)
    sent = analyzer.sent
    read = sent.index("OUTPFORM")
    assert sent[read - 6:read] == ["FORM4", "LINFREQ", "S21", "LOGM", "AVERREST", "OPC?;SING"]
    assert analyzer.out == "" and lab.read("vna", "f_start") == pytest.approx(3.99e9)   # nothing left over


def test_a_trace_of_many_lines_arrives_whole_on_gpib(hp):
    lab, _, drafted = hp
    drafted.state.update(form="FORM4", param="S21")
    trace = lab.read("draft", "trace")   # read to EOI, not to the first line end
    assert len(trace) == 2 * 201 and trace[1] == 0.0
    assert lab.read("draft", "f_start") == pytest.approx(3.99e9)   # not the trace's second line


def test_a_stop_ends_a_sweep_the_analyzer_is_still_taking(hp, monkeypatch):
    from labharness import adapters
    from labharness.instrument import Stopped
    monkeypatch.setattr(adapters, "WAIT_SLICE_MS", 100)
    lab, analyzer, _ = hp
    analyzer.sweeping = True   # OPC?;SING would answer only after the card's 1200 s
    outcome = {}

    def read():
        try:
            outcome["value"] = lab.read("vna", "S21")
        except Exception as e:
            outcome["error"] = e
    t0 = time.time()
    worker = threading.Thread(target=read)
    worker.start()
    time.sleep(0.3)
    lab.stops.pulse()   # the stop button
    worker.join(3)
    assert not worker.is_alive() and time.time() - t0 < 2
    assert isinstance(outcome.get("error"), Stopped) and analyzer.cleared   # cleared: no answer still owed
    analyzer.sweeping = False
    assert lab.read("vna", "f_start") == pytest.approx(3.99e9)


def test_a_raw_rf_switch_in_hp_mnemonics_still_asks_first(hp):
    lab, analyzer, _ = hp
    analyzer.state["soup"] = 0
    result = SafetyGate(lab, ScriptedApprover([False])).raw("vna", "SOUPON")
    assert not result.ok and "SOUPON" not in analyzer.sent and analyzer.state["soup"] == 0


def test_kelvins_pna_card_never_touches_the_calibration():
    import re
    from labharness import builtin_registry
    card = builtin_registry().get("keysight/pna")
    assert card.kind == "vna" and re.search(card.idn_pattern, "Keysight Technologies,N5227B,MY12345678,A.17.20.05")
    assert not re.search(card.idn_pattern, "Keysight Technologies,E5071C,MY1,B.13.12")   # an ENA speaks otherwise
    blocked = lambda cmd: any(b.match(cmd) is not None for b in card.blocked)   # noqa: E731
    assert blocked("SENS1:CORR:COLL:METH SOLT2") and blocked("SENS2:CORR:CSET:DEL 'mycal'") and blocked("*SAV 1")
    assert blocked("SYST:SHUT") and blocked("MMEM:DEL 'cal.csa'")
    assert not blocked("SENS1:CORR:STAT ON") and not blocked("SOUR1:POW1 -20")   # the power goes through the gate


def test_pairs_are_checked():
    base = load_yaml(DRAFT)
    params = base["parameters"]
    with pytest.raises(CardError, match="'raw' is not re or im"):
        parse_card(dict(base, parameters=dict(params, S21_complex=dict(params["S21_complex"], y="raw * 2"))))
    with pytest.raises(CardError, match="two numbers per point"):
        parse_card(dict(base, parameters=dict(params, f_start=dict(params["f_start"], pairs=True))))
    with pytest.raises(CardError, match="'re' is not raw"):
        parse_card(dict(base, parameters=dict(params, S21=dict(params["S21"], y="re * 2"))))


def test_trace_replies_that_are_not_numbers():
    raw = struct.pack(">3d", -40.0, -41.5, -42.25)
    with pytest.raises(ValueError) as err:
        trace_numbers("#224" + raw.decode("latin-1"))
    assert str(err.value) == BINARY_REPLY
    with pytest.raises(ValueError, match="not a list of numbers: 'ERROR"):
        trace_numbers("ERROR: no measurement")
    assert trace_numbers("1.5,nan,-3")[0] == 1.5


def test_plots_keep_a_narrow_resonance():
    ys = [-3.0] * 100001
    ys[51234] = -23.0                     # one point deep: stepping every 250th point would miss it
    keep = thin(ys, 400)
    assert len(keep) <= 400 and keep[0] == 0 and keep[-1] == 100000 and 51234 in keep
    assert thin([1.0, 2.0, 3.0], 400) == [0, 1, 2]
    xs, plotted = plot_points([0, 1, 2, 3], [1.0, float("nan"), None, float("-inf")], 400)
    assert xs == [0, 1, 2, 3] and plotted == [1.0, None, None, None]   # not a number: left out of the line


def test_report_charts_keep_the_resonance_and_the_gaps():
    from labharness.reports.chart import MAX_POINTS, _thin
    points = [(float(i), -3.0) for i in range(20000)]
    points[12345] = (12345.0, -23.0)
    points[500] = None
    out = _thin(points)
    assert len(out) <= MAX_POINTS + 2 and (12345.0, -23.0) in out and None in out


def test_the_run_view_and_live_view_carry_no_nan(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    from labharness import build_virtual_lab, demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(runtime, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"],
                                   providers={}))
    backend = lab.instruments["vna"].backend
    query = backend.query

    def with_a_gap(cmd, *a, **k):   # the analyzer could not measure two points
        text = query(cmd, *a, **k)
        if cmd.startswith("CALCulate:DATA?"):
            parts = text.split(",")
            parts[3], parts[4] = "nan", "inf"
            return ",".join(parts)
        return text
    backend.query = with_a_gap
    out = runtime.run("plan: s21\nsetup: {vna.points: 101, vna.rf: on}\nmeasure: [vna.S21]\n")
    run_id = out.split("run_id='")[1].split("'")[0]
    headers = {"X-Kelvin-Token": settings.access_key}
    data = client.get("/api/runs/" + run_id, headers=headers)
    assert data.status_code == 200
    ys = data.json()["traces"][0]["y"]
    assert len(ys) == 101 and ys[3] is None and ys[4] is None and isinstance(ys[5], float)
    live = client.get("/api/live", headers=headers)
    assert live.status_code == 200 and live.json()["live"]["trace"]["y"][4] is None
