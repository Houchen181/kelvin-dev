"""Oscilloscopes and spectrum analyzers: a waveform comes as an IEEE 488.2 block, of text or of binary numbers, often
in the digitizer's levels; the card turns them into volts with the instrument's own scale readings, and gives the time
of each point from the first point's time and the step between points."""
import socket
import threading

import pytest

from labharness import RunEngine, SafetyGate, ScriptedApprover
from labharness.adapters import set_resource_manager
from labharness.cards import CardError, parse_card, trace_numbers
from labharness.virtual_lab import build_lab, lab_registry
from labharness.yamlio import load_yaml

SCOPE = """card: 1
id: acme/scope-100
model: ACME SCOPE-100
vendor: ACME
kind: oscilloscope
status: lab-verified
identify: { idn: "^ACME,SCOPE-100" }
parameters:
  wave:
    label: Channel 1
    kind: measure
    type: trace
    unit: V
    get: ":WAV:DATA?"
    binary: uint8
    y: "(raw - yref) * yinc + yorig"
    axis: { origin: xorig, step: xinc }
  wave_text:
    label: Channel 1, as text
    kind: measure
    type: trace
    unit: V
    get: ":WAV:DATA:ASC?"
    axis: { origin: xorig, step: xinc }
  xorig: { label: First point's time, kind: measure, unit: s, get: ":WAV:XOR?" }
  xinc: { label: Time between points, kind: measure, unit: s, get: ":WAV:XINC?" }
  yinc: { label: Volts per level, kind: measure, unit: V, get: ":WAV:YINC?" }
  yref: { label: Level at the offset, kind: measure, get: ":WAV:YREF?" }
  yorig: { label: Offset, kind: measure, unit: V, get: ":WAV:YOR?" }
"""
LEVELS = bytes([128, 138, 148, 118, 128, 10, 255])
VOLTS = [(v - 128) * 0.01 + 0.5 for v in LEVELS]


class Scope:
    """An oscilloscope on a TCP socket, as its programming manual describes it (Rigol- and Keysight-like)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self.heard = []
        threading.Thread(target=self.serve, daemon=True).start()

    def answer(self, cmd):
        self.heard.append(cmd)
        if cmd == "*IDN?":
            return b"ACME,SCOPE-100,SN0001,1.0\n"
        values = {":WAV:XOR?": "-1.0E-06", ":WAV:XINC?": "1.0E-08", ":WAV:YINC?": "0.01", ":WAV:YREF?": "128",
                  ":WAV:YOR?": "0.5"}
        if cmd in values:
            return (values[cmd] + "\n").encode()
        if cmd == ":WAV:DATA?":   # a definite-length block: #9, nine digits of length, the bytes, the line end
            return b"#9%09d" % len(LEVELS) + LEVELS + b"\n"
        if cmd == ":WAV:DATA:ASC?":
            text = ",".join("%.4E" % v for v in VOLTS).encode()
            return b"#8%08d" % len(text) + text + b"\n"
        return b"0\n"

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            buf = b""
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    conn.sendall(self.answer(line.decode().strip()))
            conn.close()


@pytest.fixture
def scope(tmp_path):
    pyvisa = pytest.importorskip("pyvisa")
    pytest.importorskip("pyvisa_py")
    pytest.importorskip("numpy")
    rm = pyvisa.ResourceManager("@py")
    set_resource_manager(rm)
    server = Scope()
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "scope.yaml").write_text(SCOPE, encoding="utf-8")
    lab = build_lab("lab: Bench\ninstruments:\n  scope: { card: acme/scope-100, address: \"TCPIP0::127.0.0.1::%d::SOCKET\" }\n"
                    % server.port, registry=lab_registry(tmp_path))
    yield lab, server
    lab.close()
    server.sock.close()
    set_resource_manager(None)
    rm.close()


def test_a_binary_waveform_in_volts_with_its_time_axis(scope):
    lab, server = scope
    assert lab.read("scope", "wave") == pytest.approx(VOLTS)          # levels to volts with the scope's own scale
    assert lab.read("scope", "wave_text") == pytest.approx(VOLTS)     # the same, as text after its block header
    run = RunEngine(SafetyGate(lab, ScriptedApprover([]))).run("""
plan: capture
time: { every: 0.05, points: 2 }
measure: [scope.wave]
""")
    assert run.status == "done", run.message
    first = run.rows[0]
    assert first["scope.wave"] == pytest.approx(VOLTS)
    assert first["scope.wave.axis"] == pytest.approx([-1e-6, -1e-6 + 6 * 1e-8])   # origin, origin + 6 steps
    assert ":WAV:DATA?" in server.heard and ":WAV:YREF?" in server.heard


def test_trace_replies_with_block_headers():
    assert trace_numbers("#800000011 1.5,2,3") == [1.5, 2.0, 3.0]
    assert trace_numbers("#0 4 5 6") == [4.0, 5.0, 6.0]
    assert trace_numbers("-1.0E-03,2.5E-03") == [-1e-3, 2.5e-3]
    assert trace_numbers("1 2\t3") == [1.0, 2.0, 3.0]


def test_trace_settings_are_checked():
    base = load_yaml(SCOPE)
    with pytest.raises(CardError, match="'ygain' is not raw or a readable numeric parameter"):
        parse_card(dict(base, parameters=dict(base["parameters"], wave=dict(base["parameters"]["wave"],
                                                                            y="raw * ygain"))))
    with pytest.raises(CardError, match="a trace's binary numbers"):
        parse_card(dict(base, parameters=dict(base["parameters"], wave=dict(base["parameters"]["wave"],
                                                                            binary="int12"))))
    with pytest.raises(CardError, match="only traces have y"):
        parse_card(dict(base, parameters=dict(base["parameters"], xinc=dict(base["parameters"]["xinc"], y="raw"))))
    with pytest.raises(CardError, match="'nowhere' is not a readable numeric parameter"):
        parse_card(dict(base, parameters=dict(base["parameters"], wave=dict(
            base["parameters"]["wave"], axis={"origin": "nowhere", "step": "xinc"}))))
    card = parse_card(base)
    assert card.params["wave"].binary == "uint8" and card.params["wave"].axis_step == ("xorig", "xinc")
    assert parse_card(dict(base, parameters=dict(base["parameters"], wave=dict(
        base["parameters"]["wave"], y="clip(raw, 0, 255) * yinc * 1e3")))).params["wave"].y.startswith("clip")
