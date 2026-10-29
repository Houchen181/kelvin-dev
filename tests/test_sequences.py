"""Pulse sequences written out (pulses.parse): the syntax, checked as written and again at the times it plays; the
timing drawn so that a 4 ns pulse shows; the drawing and the exact edges on the card that starts a generator; a plan's
sweep checked at every value before anything plays; written sequences played on the virtual generator and on a Pulse
Streamer. Field test P5: six fixed protocols with five parameters, and a 64-character text strip in which a 4 ns pulse
was invisible."""
import re
import struct
from pathlib import Path

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, pulses
from simlab import build_virtual_lab
from labharness.clock import SimClock
from labharness.drivers import MODULES, PLAYING
from labharness.engine import RunEngine
from labharness.gate import ApprovalRequest
from labharness.homelab import build_lab

from test_pulse_drivers import FakePulseStreamer, pulsestreamer

ECHO = """
sequence: echo_written
let: { half: tau / 2 }
steps:
  - mw: t_pi / 2
  - wait: half
  - mw: t_pi
  - wait: half
  - mw: t_pi / 2
  - wait: 20 ns
  - laser: t_laser
    gate:
      - { for: t_gate, window: signal }
      - { start: t_laser - t_gate, for: t_gate, window: reference }
  - wait: t_wait
"""

XY8 = """
sequence: xy8
description: pi/2, then x and y pi pulses tau/8 apart through their own switches, pi/2, readout
let: { spacing: tau / 8 }
steps:
  - mw_x: t_pi / 2
  - repeat: 2
    steps:
      - wait: spacing / 2
      - mw_x: t_pi
      - wait: spacing
      - mw_y: t_pi
      - wait: spacing
      - mw_x: t_pi
      - wait: spacing
      - mw_y: t_pi
      - wait: spacing / 2
  - mw_x: t_pi / 2
  - wait: 20 ns
  - laser: t_laser
    gate: [{for: t_gate}, {start: t_laser - t_gate, for: t_gate}]
  - wait: t_wait
"""


@pytest.fixture(autouse=True)
def library():
    """Each test starts with no written sequences, and leaves the library as it found it."""
    before = dict(pulses.LIBRARY)
    pulses.LIBRARY.clear()
    yield pulses.LIBRARY
    pulses.LIBRARY.clear()
    pulses.LIBRARY.update(before)


def flat(spans):
    return [t for span in spans for t in span]


# ------------------------------------------------------------------ the syntax

def test_a_written_echo_is_the_protocols_echo():
    pytest.importorskip("numpy")
    prog = pulses.parse(ECHO)
    assert prog.name == "echo_written" and prog.channels == ["mw", "laser", "gate"]
    for tau in (0.0, 2e-6, 37e-6):
        mine = prog.build({"tau": tau, "t_pi": 1e-7})
        ref = pulses.build("hahn_echo", {"tau": tau, "t_pi": 1e-7})
        assert mine.period == pytest.approx(ref.period, abs=1e-15)
        for ch in ("laser", "mw", "gate"):
            assert flat(mine.channels[ch]) == pytest.approx(flat(ref.channels[ch]), abs=1e-15)
        assert [w for w, _, _ in mine.windows] == ["signal", "reference"]
        assert flat([(a, b) for _, a, b in mine.windows]) == pytest.approx(flat([(a, b) for _, a, b in ref.windows]))
    assert pulses.check(prog.build({"tau": 2e-6})) == []


def test_loops_units_and_channels_of_its_own():
    pytest.importorskip("numpy")
    seq = pulses.parse(XY8).build({"tau": 4e-6, "t_pi": 40e-9})
    x, y = seq.channels["mw_x"], seq.channels["mw_y"]
    assert len(x) == 6 and len(y) == 4                                    # 2 x (x y x y) between the pi/2 pulses
    pis = sorted(x[1:-1] + y)
    assert [round((b[0] - a[1]) * 1e9) for a, b in zip(pis, pis[1:])] == [500] * 7   # tau / 8 apart, both loops
    assert [round((b - a) * 1e9) for a, b in x] == [20, 40, 40, 40, 40, 20]
    assert seq.channels["laser"][0][0] == pytest.approx(x[-1][1] + 20e-9)   # "20 ns" read as seconds
    assert [w for w, _, _ in seq.windows] == ["signal", "reference"]        # unlabelled windows alternate
    assert seq.mw_duty() == pytest.approx((10 * 40e-9 - 2 * 20e-9) / seq.period)   # every mw... channel heats
    assert seq.order() == ["laser", "mw_x", "mw_y", "gate"]
    assert pulses.seconds_text("3us + tau") == "(3*1e-06) + tau" and pulses.seconds_text(5e-8) == "5e-08"


@pytest.mark.parametrize("text, said", [
    ("sequence: rabi\nsteps: [{mw: 1 us}]", "a protocol's name already"),
    ("sequence: Bad Name\nsteps: [{mw: 1 us}]", "a name of lower-case letters"),
    ("sequence: x\nsteps: [{mw: tua}]", "tua is not one of the generator's times"),
    ("sequence: x\nsteps: [{repeat: 0, steps: [{mw: 1 us}]}]", "a whole number from 1 to"),
    ("sequence: x\nsteps: [{wait: 1 us, mw: 2 us}]", "a wait is on its own in its step"),
    ("sequence: x\nsteps: [{gate: {for: 1 us, window: sig}}]", "only the gate has windows"),
    ("sequence: x\nsteps: [{laser: {for: 1 us, window: signal}}]", "only the gate has windows"),
    ("sequence: x\nlet: {pi: 1 us}\nsteps: [{mw: 1 us}]", "let.pi: a name of its own"),
    ("sequence: x\nlet: {ns: 1}\nsteps: [{mw: 1 us}]", "a unit such as ns"),
    ("sequence: x\nsteps: [{mw: 1 us}, {wait: 2 us}]\nloop: 3", "unknown key 'loop'"),
    ("sequence: x\nsteps: [{wait: 1 us}]", "nothing is ever on"),
    ("sequence: x\nsteps: [{gate: [{for: 1 us, window: signal}, {start: 2 us, for: 1 us}]}]",
     "say signal or reference on every window"),
    ("sequence: x\nsteps: [{mw: 1 us +}]", "not an expression"),
    ("sequence: x\nsteps: [{steps: [{mw: 1 us}]}]", "how many times"),
    ("sequence: x\nsteps: [{wait: 1 us}, {repeat: 2, steps: [{repeat: 2, steps: [{repeat: 2, steps: [{repeat: 2, "
     "steps: [{repeat: 2, steps: [{repeat: 2, steps: [{repeat: 2, steps: [{mw: 1 us}]}]}]}]}]}]}]}]",
     "nested more than"),
])
def test_a_sequence_is_checked_as_it_is_written(text, said):
    with pytest.raises(pulses.SequenceError) as e:
        pulses.parse(text)
    assert any(said in p for p in e.value.errors), e.value.errors


@pytest.mark.parametrize("text, said", [
    ("sequence: x\nsteps: [{mw: tau - 1 us}]", "less than nothing"),
    ("sequence: x\nsteps: [{mw: 1 us}, {mw: {start: -5 ns, for: 1 us}}]", "starts 5 ns before its step"),
    ("sequence: x\nsteps: [{mw: [{for: 1 us}, {start: 500 ns, for: 1 us}]}]", "two mw pulses overlap"),
    ("sequence: x\nperiod: 1 us\nsteps: [{laser: 3 us}]", "shorter than the sequence"),
    ("sequence: x\nsteps: [{wait: tau - 1 ms}, {mw: 1 us}]", "less than nothing"),
])
def test_and_again_at_the_times_it_plays(text, said):
    pytest.importorskip("numpy")
    prog = pulses.parse(text)
    with pytest.raises(ValueError, match=re.escape(said)):
        prog.build({"tau": 100e-9})


def test_what_a_generator_would_refuse():
    pytest.importorskip("numpy")
    hot = pulses.parse("sequence: hot\nsteps: [{mw_x: 3 us, laser: 1 us}, {mw_y: 3 us}, {wait: 1 us}]").build({})
    assert hot.mw_duty() == pytest.approx(6 / 7)
    assert any("microwave is on 86%" in p for p in pulses.check(hot))
    many = pulses.parse("sequence: many\nsteps: [{repeat: 3000, steps: [{mw: 10 ns}, {wait: 1 us}]}]").build({})
    assert any("more than the generator holds (4096)" in p for p in pulses.check(many, {"max_segments": 4096}))
    assert pulses.check(many) == []                                         # a generator without that limit
    windows = pulses.parse("sequence: w\nsteps: [{gate: [{for: 1 us}, {start: 2 us, for: 1 us}, "
                           "{start: 4 us, for: 1 us}]}]").build({})
    assert any("the gate has 3 windows" in p for p in pulses.check(windows))
    tiny = pulses.parse("sequence: tiny\nsteps: [{mw: 0.5 ns}, {laser: 1 us}]").build({})
    assert any("shorter than the generator's" in p for p in pulses.check(tiny))
    assert pulses.check(pulses.parse("sequence: long\nsteps: [{laser: 1 us}, {wait: 0.2 s}]").build({}))


# ------------------------------------------------------------------ the timing drawn, and exactly

def rects(svg, color):
    return [(float(x), float(w)) for x, w in re.findall(r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)" '
                                                       r'height="[\d.]+" fill="#%s"/>' % color, svg)]


def test_a_4_ns_pulse_shows_in_the_drawing():
    pytest.importorskip("numpy")
    seq = pulses.build("rabi", {"tau": 4e-9, "t_wait": 1e-3})              # 4 ns in a 1 ms period
    svg = pulses.svg(seq)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>") and "<title>Timing of rabi" in svg
    mw = rects(svg, pulses.MW_COLORS[0])
    assert len(mw) == 1 and mw[0][1] >= 5                                  # at least a few pixels wide
    assert "not to scale" in svg and ">4 ns<" not in svg and ">1 ms<" in svg   # each interval's length under it
    assert pulses.lines(seq)[1] == "mw: 0 → 4 ns"                          # and exactly, in words
    cw = pulses.svg(pulses.build("cw", {"t_laser": 2e-6}))
    assert "to scale" in cw and "not to scale" not in cw
    data = pulses.png(seq)
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", data[16:24]) == (pulses.WIDTH, 192)
    assert "→" in pulses.caption(seq) and "<script" not in svg


def test_the_drawing_is_an_svg_a_browser_reads():
    """The cards show it as <img src="data:image/svg+xml,...">: it must be well-formed XML, with everything inside
    its own size, or the browser shows nothing."""
    pytest.importorskip("numpy")
    import xml.etree.ElementTree as ET
    from urllib.parse import quote, unquote
    ns = "{http://www.w3.org/2000/svg}"
    for seq in (pulses.build("rabi", {"tau": 4e-9, "t_wait": 1e-3}), pulses.parse(XY8).build({"tau": 4e-6,
                                                                                             "t_pi": 40e-9})):
        svg = pulses.svg(seq)
        root = ET.fromstring(svg)
        assert root.tag == ns + "svg" and int(root.get("width")) == pulses.WIDTH
        assert root.find(ns + "title").text.startswith("Timing of")
        rects = root.findall(ns + "rect")[1:]                             # the pulses (the first is the paper)
        assert len(rects) == sum(len(v) for v in seq.channels.values())
        assert all(float(r.get("width")) > 0 and float(r.get("x")) + float(r.get("width")) <= pulses.WIDTH
                   for r in rects)
        assert all(0 < float(t.get("y")) < int(root.get("height")) for t in root.findall(ns + "text"))
        url = "data:image/svg+xml;charset=utf-8," + quote(svg, safe="-_.!~*'()")   # as encodeURIComponent makes it
        assert unquote(url.split(",", 1)[1]) == svg


def test_the_edges_are_exact():
    pytest.importorskip("numpy")
    seq = pulses.parse(ECHO).build({"tau": 2e-6, "t_pi": 1e-7})
    assert pulses.lines(seq) == ["laser: 2.22 µs → 5.22 µs", "mw: 0 → 50 ns, 1.05 µs → 1.15 µs, 2.15 µs → 2.2 µs",
                                 "gate: signal 2.22 µs → 2.52 µs, reference 4.92 µs → 5.22 µs"]
    many = pulses.parse(XY8).build({"tau": 4e-6, "t_pi": 40e-9})
    assert pulses.lines(many, most=2)[1].endswith("; and 4 more")
    assert pulses.exact(1.0505e-6) == "1.0505 µs" and pulses.exact(4.2e-7) == "420 ns" and pulses.exact(0) == "0"
    text = pulses.describe(many)
    assert text.startswith("xy8:") and "mw_y: 810 ns → 850 ns" in text


# ------------------------------------------------------------------ on the card that starts a generator

@pytest.fixture
def nv():
    lab = build_virtual_lab(clock=SimClock(), seed=3)
    approver = ScriptedApprover([True] * 10)
    gate = SafetyGate(lab, approver)
    return lab, gate, approver


def test_the_card_that_starts_a_generator_shows_its_timing(nv):
    lab, gate, approver = nv
    assert gate.set("pg", "protocol", "rabi").ok and gate.set("pg", "tau", 1e-7).ok
    assert approver.requests == []                                         # settings: nothing asks
    assert gate.set("pg", "running", True).ok
    card = approver.requests[0]
    assert card.items[0].startswith("pg.running off → on")
    assert "pg plays rabi in a loop: period 4.12 µs, microwave on 2.4% of the time, laser 73%" in card.items
    assert "pg mw: 0 → 100 ns" in card.items and "pg gate: signal 120 ns → 420 ns, reference 2.82 µs → 3.12 µs" \
        in card.items
    assert len(card.figures) == 1 and card.figures[0]["svg"].startswith("<svg")
    assert card.figures[0]["caption"].startswith("Timing of rabi: laser: 120 ns → 3.12 µs")


def test_a_card_says_when_the_generator_will_refuse_what_it_shows(nv):
    lab, gate, approver = nv
    assert gate.set("pg", "protocol", "rabi").ok and gate.set("pg", "tau", 9e-3).ok
    r = gate.set("pg", "running", True)
    assert any(i.startswith("pg will refuse it: the microwave is on 100%") for i in approver.requests[0].items)
    assert not r.ok and lab.read("pg", "running") is False                 # and it did


def test_other_cards_have_no_pictures(nv):
    lab, gate, approver = nv
    assert gate.set("smu", "output", True).ok
    assert approver.requests[0].figures == []


# ------------------------------------------------------------------ plans: every value before anything plays

RABI = """
plan: rabi
setup: { laser.emission: on, laser.shutter: open, laser.power: 2, mw.output: on, mw.power: 0, mw.frequency: 2.868e9,
         pg.protocol: rabi, pg.running: on }
sweep: { pg.tau: {from: 0, to: %s, points: 41} }
measure: [counter.contrast]
after: { pg.running: off }
"""


def test_a_sweep_that_would_heat_is_refused_before_anything_plays():
    lab = build_virtual_lab(clock=SimClock(), seed=3)
    approver = ScriptedApprover([])                                        # nobody is even asked
    r = RunEngine(SafetyGate(lab, approver)).run(RABI % "2.0e-5")
    assert r.status == "refused", r.message
    assert "pg at tau = 4.5 µs: the microwave is on 53%" in r.message      # the first value that would
    assert approver.requests == [] and lab.read("pg", "running") is False and lab.read("laser", "emission") is False


def test_the_runs_card_draws_the_sequence_at_both_ends_of_its_sweep():
    lab = build_virtual_lab(clock=SimClock(), seed=3)
    approver = ScriptedApprover([True])
    r = RunEngine(SafetyGate(lab, approver)).run(RABI % "4.0e-7")
    assert r.status == "done", r.message
    card = approver.requests[0]
    assert [f["caption"].split(":")[0] for f in card.figures] == ["Timing of rabi at tau = 0",
                                                                  "Timing of rabi at tau = 400 ns"]
    assert "pg plays rabi in a loop: period 4.02 µs, microwave on 0.0% of the time, laser 75% at tau = 0" in card.items
    assert "pg: tau swept; every value checked before the run (period 4.02 µs to 4.42 µs, microwave on up to 9.0% " \
           "of the time)" in card.items


def test_a_written_sequence_plays_on_the_virtual_generator(library):
    pytest.importorskip("numpy")
    pulses.register(pulses.parse(XY8))
    lab = build_virtual_lab(clock=SimClock(), seed=3)
    approver = ScriptedApprover([True])
    r = RunEngine(SafetyGate(lab, approver)).run("""
plan: xy8_decay
setup: { laser.emission: on, laser.shutter: open, laser.power: 2, pg.t_pi: 4.0e-8, pg.sequence: xy8, pg.running: on }
sweep: { pg.tau: {values: [1.0e-6, 4.0e-6]} }
measure: [counter.signal, pg.period, pg.mw_duty]
after: { pg.running: off }
""")
    assert r.status == "done", r.message
    want = [pulses.program("xy8").build({"tau": tau, "t_pi": 4e-8}) for tau in (1e-6, 4e-6)]
    assert [row["pg.period"] for row in r.rows] == pytest.approx([s.period for s in want])
    assert [row["pg.mw_duty"] for row in r.rows] == pytest.approx([s.mw_duty() for s in want])
    assert "Timing of xy8 at tau = 1 µs" in approver.requests[0].figures[0]["caption"]
    gate = SafetyGate(lab, ScriptedApprover([]))
    assert not gate.set("pg", "sequence", "xy16").ok                       # not saved: the generator says so
    assert lab.read("pg", "sequence") == "xy8"


# ------------------------------------------------------------------ a real generator plays it as written

@pytest.fixture
def streamer():
    MODULES.update(pulsestreamer=pulsestreamer)
    yield
    MODULES.clear()
    PLAYING.clear()


def test_the_pulse_streamer_plays_a_written_sequence_on_its_wired_outputs(streamer, library, tmp_path):
    pytest.importorskip("numpy")
    pulses.register(pulses.parse(XY8))
    lab = build_lab("""
lab: NV
instruments:
  pg: { card: swabian/pulse-streamer, address: "pulsestreamer://169.254.8.2",
        vars: { laser: "0", mw: "1", gate: "2", mw_x: "3", mw_y: "4" } }
""")
    gate = SafetyGate(lab, ScriptedApprover([True] * 3), EventLog(tmp_path / "events.jsonl", lab.clock))
    ps = FakePulseStreamer.last
    assert gate.set("pg", "sequence", "xy8").ok and gate.set("pg", "running", True).ok
    seq, _, _ = ps.streams[-1]
    want = pulses.program("xy8").build({"t_pi": 1e-7, "tau": 1e-7})
    assert set(seq.digital) == {0, 2, 3, 4}                                # laser, gate, mw_x and mw_y; mw unused
    assert seq.digital[4] == want.pattern("mw_y")
    assert lab.read("pg", "mw_duty") == pytest.approx(want.mw_duty())
    assert gate.set("pg", "sequence", "none").ok                           # back to its protocol, still playing
    assert PLAYING["pg"].protocol == "pulsed_odmr"
    r = gate.set("pg", "sequence", "xy16")
    assert not r.ok and "no sequence 'xy16'" in r.message


def test_a_channel_no_output_is_wired_to_is_refused(streamer, library, tmp_path):
    pytest.importorskip("numpy")
    pulses.register(pulses.parse(XY8))
    lab = build_lab("""
lab: NV
instruments:
  pg: { card: swabian/pulse-streamer, address: "pulsestreamer://169.254.8.2" }
""")
    gate = SafetyGate(lab, ScriptedApprover([True]), EventLog(tmp_path / "events.jsonl", lab.clock))
    assert gate.set("pg", "sequence", "xy8").ok
    r = gate.set("pg", "running", True)
    assert not r.ok and "uses mw_x, mw_y, which no output is wired to" in r.message
    assert FakePulseStreamer.last.streams == [] and lab.read("pg", "running") is False


# ------------------------------------------------------------------ Kelvin's tools, and where the cards are shown

def test_kelvin_writes_shows_and_saves_a_sequence(tmp_path, library):
    pytest.importorskip("mcp")
    from simlab import demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=1), tmp_path, demo_envelope(lab))
    text, png = rt.pulse_view("", {"tau": 4e-6}, "pg", XY8)
    assert text.startswith("xy8:") and "checks: ok" in text and png[:4] == b"\x89PNG"
    bad, picture = rt.pulse_view("", {}, "pg", "sequence: x\nsteps: [{mw: tua}]")
    assert bad.startswith("the sequence is not right yet") and "tua" in bad and picture is None
    assert "unknown protocol" in rt.pulse_sequence("spin_lock", {}, "")
    said = rt.save_sequence(XY8)
    assert said.startswith("saved xy8 (channels mw_x, mw_y, laser, gate)") and (tmp_path / "sequences" / "xy8.yaml").exists()
    assert "the sequence is not right yet" in rt.save_sequence("sequence: x\nsteps: [{mw: tua}]")
    assert rt.pulse_sequence("", {}, "pg", "xy8").startswith("xy8:")      # by its name
    pulses.LIBRARY.clear()
    LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=1), tmp_path, None)   # Kelvin starts again
    assert "xy8" in pulses.LIBRARY
    assert rt.gate.set("pg", "sequence", "xy8").ok
    rt.gate.approver = ScriptedApprover([True])
    assert rt.gate.set("pg", "running", True).ok
    assert rt.save_sequence(XY8.replace("t_pi / 2", "t_pi / 4")).startswith("not saved: pg is playing xy8 now")


def test_every_approval_surface_draws_the_figures(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    approver = WebApprover(timeout=0.2)
    shown = []
    approver.subscribe(shown.append)
    request = ApprovalRequest("Approve drive change on pg?", ["pg.running off → on"],
                              figures=[{"svg": "<svg/>", "caption": "Timing of rabi"}])
    import threading
    t = threading.Thread(target=approver.approve, args=(request,))
    t.start()
    pending = approver.wait_for_pending(2)
    assert pending["figures"] == request.figures and approver.pending_list()[0]["figures"] == request.figures
    assert shown[0]["figures"] == request.figures
    t.join()
    src = Path(__file__).resolve().parents[1] / "src" / "labharness"
    for page in ("web/static/app.js", "web/static/mobile.js", "agent/ui.html"):
        text = (src / page).read_text(encoding="utf-8")
        assert "data:image/svg+xml;charset=utf-8," in text and "figures" in text, page   # as an image: nothing runs
