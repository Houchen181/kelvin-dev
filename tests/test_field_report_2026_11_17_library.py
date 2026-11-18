"""Field report 2026-11-17 (R1): acquiring readings on library cards. check_card refused a library card's reading
get: "GET acquireHistogram 1": "does not look like a function that only reads (get..., is..., query...)", though a
text card's reading takes acquires: true with wait:. So a time-correlated histogram (record for a gate, then read it)
couldn't be a card's reading, and no plan could use it. The lab's wrapper acquireHistogram(gate) records for the gate
and returns the histogram.

Now a library reading that acquires (acquires: true, wait:) may call a function that records first (acquire...,
record..., capture..., take..., run...), never one whose name sets or changes something. Its call has its card's wait
on top of the connection's timeout, a person's Stop ends Kelvin's wait for it, and nothing goes to the library beside
the recording while it goes on. A card's Try and the monitor never run it; a plan's or a person's read does."""
import threading
import time

import pytest

from labharness import EventLog, RunEngine, SafetyGate, ScriptedApprover, carddraft, cardtry, drivers, library
from labharness.cards import CardError, parse_card
from labharness.homelab import build_lab, lab_registry
from labharness.instrument import Busy, GaveUp, Stopped, patience, unstoppable
from labharness.library import LibraryInstrument
from labharness.monitor import Monitor
from labharness.yamlio import load_yaml

CARD = """
card: 1
id: acme/tagger
model: ACME time tagger (the lab's Python wrapper)
vendor: ACME
kind: counter
status: lab-verified
library:
  module: faketagger
  open: ["connect {address}"]
  close: [disconnect]
display: { headline: counts }
parameters:
  counts: { label: Counts in a gate, kind: measure, type: int, get: "GET countPhotons 0.1", acquires: true, wait: 1 }
  rate: { label: Count rate, kind: measure, unit: Hz, get: "GET getCountRate" }
  histogram:
    label: Time-correlated histogram
    kind: measure
    type: trace
    get: "GET acquireHistogram 1"
    acquires: true
    wait: 5
"""
LAB = 'lab: NV rig\ninstruments:\n  tagger: { card: acme/tagger, address: "lib://192.168.1.30?path=%s" }\n'


class Helper:
    """Kelvin's helper process for a maker's library, faked: each request it gets, with the seconds Kelvin gives it to
    answer. The lab's acquireHistogram and countPhotons answer at once here."""
    made: list = []

    def __init__(self, name, command):
        self.asked = []
        Helper.made.append(self)

    def request(self, op, timeout, **fields):
        self.asked.append((op, fields.get("name"), fields.get("args"), timeout))
        if op == "ping":
            return {"python": "3.11.9", "bits": 64}
        return {"acquireHistogram": [0, 3, 5, 2], "countPhotons": 42, "getCountRate": 1234.5}.get(fields.get("name"))

    def stop(self):
        pass

    def called(self, name):
        """(arguments, seconds given) of each call of this function."""
        return [(args, timeout) for op, fn, args, timeout in self.asked if op == "call" and fn == name]


@pytest.fixture
def helper(monkeypatch):
    """Every lib:// connection gets a faked helper (drivers.MODULES: the seam the drivers' tests use)."""
    monkeypatch.setattr(Helper, "made", [])
    monkeypatch.setitem(drivers.MODULES, "lib", Helper)
    return Helper.made


@pytest.fixture
def tagger(tmp_path, helper):
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "tagger.yaml").write_text(CARD, encoding="utf-8")
    lab = build_lab(LAB % tmp_path.as_posix(), registry=lab_registry(tmp_path))
    gate = SafetyGate(lab, ScriptedApprover([]), EventLog(tmp_path / "events.jsonl", lab.clock))
    yield lab, gate, helper[-1]
    lab.close()


def with_get(get, **more):
    data = load_yaml(CARD)
    data["parameters"]["histogram"] = dict(data["parameters"]["histogram"], get=get, **more)
    return data


# ---------------------------------------------------------------------- the card

def test_check_card_takes_a_library_reading_that_acquires():
    card, problems, _ = carddraft.check(CARD)
    assert problems == [] and card is not None
    spec = card.params["histogram"]
    assert spec.acquires and spec.wait_s == 5 and spec.get_cmd.text == "GET acquireHistogram 1"
    libraries = carddraft.GUIDE.split("controls through a library")[1].split("Example scripts that send text")[0]
    assert 'get: "GET acquireHistogram 1"' in libraries and "acquires: true, wait:" in libraries   # as drafts learn it


def test_without_acquires_check_card_says_how_to_mark_a_reading_that_acquires():
    card, problems, _ = carddraft.check(CARD.replace("    acquires: true\n", ""))
    assert card is None and len(problems) == 1                       # the field report's refusal, kept...
    assert problems[0].startswith("parameters.histogram.get: acquireHistogram does not look like a function that only "
                                  "reads (get..., is..., query...)")
    assert "acquires: true and wait:" in problems[0]                 # ...saying what makes it a reading that acquires


@pytest.mark.parametrize("get", ["GET recordTrace 1", "GET captureFrame", "GET takeSpectrum 0.5",
                                 "GET runMeasurement 2", "GET getHistogram 1", "GET sub.acquireHistogram 1"])
def test_functions_that_record_then_answer_are_readings_that_acquire(get):
    assert parse_card(with_get(get), "t").params["histogram"].acquires


@pytest.mark.parametrize("get", ["GET setGate 1", "GET applyGate 1", "GET toggleShutter", "GET resetHistogram",
                                 "GET calibrateDelays", "GET uploadSequence", "GET downloadData", "GET deleteHistogram",
                                 "GET moveStage 2", "GET goHome", "GET homeAxis", "GET runCalibration",
                                 "GET acquireAndReset 1"])
def test_a_function_that_sets_or_changes_something_is_refused_even_with_acquires(get):
    with pytest.raises(CardError, match=r"sets or changes something \(\w+(\.\.\.)?\), and no reading calls such a "
                                        r"function, not even one that acquires"):
        parse_card(with_get(get), "t")


def test_a_reading_that_acquires_still_names_what_it_does():
    with pytest.raises(CardError, match="doMeasurement does not look like a function that reads or acquires"):
        parse_card(with_get("GET doMeasurement"), "t")
    with pytest.raises(CardError, match="with a library, a reading calls one function"):   # no text card's arm first
        parse_card(with_get(["SET armHistogram", "GET readHistogram"]), "t")


# ---------------------------------------------------------------------- the library's call

def test_the_library_gives_an_acquiring_call_its_wait_on_top_of_the_timeout():
    card = parse_card(load_yaml(CARD), "t")
    backend = LibraryInstrument("tagger", card, "lib://192.168.1.30?path=C:/lab/tagger", module=Helper)
    assert backend.query("GET acquireHistogram 1") == "0,3,5,2"     # numbers, as a trace reads them (not "[0, 3...]")
    assert backend.query("GET countPhotons 0.1") == "42" and backend.query("GET getCountRate") == "1234.5"
    helper = backend.bridge
    assert helper.called("acquireHistogram") == [(["1"], 15.0)]     # the connection's 10 s and the card's wait of 5 s
    assert helper.called("countPhotons") == [(["0.1"], 11.0)]
    assert helper.called("getCountRate") == [([], 10.0)]             # a plain reading: the timeout alone
    assert helper.called("connect") == [(["192.168.1.30"], 10.0)]


def test_trying_the_card_never_runs_an_acquisition(helper):
    result = cardtry.try_card(parse_card(load_yaml(CARD), "t"), "lib://192.168.1.30?path=C:/lab/tagger")
    rows = {r["name"]: r for r in result["readings"]}
    assert rows["rate"]["value"] == pytest.approx(1234.5)
    assert rows["histogram"]["skipped"] == ("this reading runs an acquisition (GET acquireHistogram 1); try it with "
                                            "the instrument in the lab")
    assert "runs an acquisition (GET countPhotons 0.1)" in rows["counts"]["skipped"]
    said = cardtry.summary(result)
    assert "histogram: NOT TRIED" in said and "counts: NOT TRIED" in said and "2 not tried" in said
    tried = helper[-1]
    assert tried.called("getCountRate") and not tried.called("acquireHistogram") and not tried.called("countPhotons")


def test_the_monitor_never_runs_an_acquisition(tagger):
    lab, gate, helper = tagger
    monitor = Monitor(lab, gate, lambda *a: None)
    gate._asked = {}                                  # a plan runs: the monitor asks each instrument whether it answers
    lab.instruments["tagger"].health.answered_at = None
    looked = len(helper.called("getCountRate"))
    monitor.tick()
    assert len(helper.called("getCountRate")) == looked + 1   # its plain reading, not its headline, which acquires
    assert sorted(lab.snapshot()["tagger"]) == ["rate"]       # nor does a look at the lab, or a run's record of it
    assert not helper.called("acquireHistogram") and not helper.called("countPhotons")


def test_a_plans_and_a_persons_read_run_the_acquisition(tagger):
    lab, gate, helper = tagger
    assert lab.read("tagger", "histogram") == [0.0, 3.0, 5.0, 2.0]     # Instrument.read: a trace
    got = gate.get("tagger", "histogram")                                # a person's get_value
    assert got.ok and got.value == [0.0, 3.0, 5.0, 2.0]
    assert gate.get("tagger", "counts").value == 42
    asked = len(helper.called("acquireHistogram"))
    run = RunEngine(gate).run("plan: histograms\ntime: { every: 0.05, points: 2 }\nmeasure: [tagger.histogram]\n")
    assert run.status == "done", run.message
    calls = helper.called("acquireHistogram")
    assert asked == 2 and len(calls) == asked + 2                      # the read, the get, and the plan's two points
    assert all(c == (["1"], 15.0) for c in calls)                      # each with the card's wait on top


# ---------------------------------------------------------------------- the maker's library in Kelvin's helper

VENDOR = '''
"""The lab's wrapper around its time tagger: acquireHistogram(gate) records for the gate, then returns the histogram.
Each call is written to calls.txt beside it."""
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _note(text):
    with open(os.path.join(HERE, "calls.txt"), "a") as f:
        f.write(text + "\\n")


def getCountRate():
    _note("getCountRate")
    return 1234.5


def acquireHistogram(gate):
    _note("acquireHistogram %s began" % gate)
    time.sleep(gate)
    _note("acquireHistogram %s ended" % gate)
    return [0, 3, 5, 2]
'''

SLOW_CARD = """
card: 1
id: acme/tagger
model: ACME time tagger (the lab's Python wrapper)
vendor: ACME
kind: counter
status: lab-verified
connection: { timeout_ms: 800 }
library: { module: faketagger }
parameters:
  rate: { label: Count rate, kind: measure, unit: Hz, get: "GET getCountRate" }
  histogram: { label: Histogram, kind: measure, type: trace, get: "GET acquireHistogram 2", acquires: true, wait: 10 }
  long: { label: Long histogram, kind: measure, type: trace, get: "GET acquireHistogram 3", acquires: true, wait: 10 }
"""


@pytest.fixture
def slow(tmp_path):
    """The lab's tagger through Kelvin's own helper process (data/bridge/kelvin_bridge.py), started with this Python."""
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "faketagger.py").write_text(VENDOR, encoding="utf-8")
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "tagger.yaml").write_text(SLOW_CARD, encoding="utf-8")
    lab = build_lab(LAB % vendor.as_posix(), registry=lab_registry(tmp_path))
    calls = vendor / "calls.txt"
    yield lab, lambda: calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    lab.close()


def until(ok, seconds=15.0):
    end = time.monotonic() + seconds
    while not ok():
        assert time.monotonic() < end, "never happened"
        time.sleep(0.02)


def test_a_recording_longer_than_the_connections_timeout_is_waited_for(slow):
    lab, calls = slow
    began = time.monotonic()
    assert lab.read("tagger", "histogram") == [0.0, 3.0, 5.0, 2.0]     # 2 s of recording, where the timeout is 0.8 s
    assert time.monotonic() - began >= 1.9 and not lab.instruments["tagger"].backend.gone
    assert calls() == ["acquireHistogram 2 began", "acquireHistogram 2 ended"]


def test_a_stop_ends_kelvins_wait_and_nothing_goes_beside_the_recording(slow):
    lab, calls = slow
    out = {}

    def read(name):
        try:
            out[name] = lab.read("tagger", name)
        except Exception as e:
            out[name] = e
    first = threading.Thread(target=read, args=("long",))
    first.start()
    until(lambda: calls() == ["acquireHistogram 3 began"])
    stopped = time.monotonic()
    lab.stops.pulse()                                                    # a person's Stop
    first.join(10)
    assert isinstance(out["long"], Stopped) and time.monotonic() - stopped < 1.5
    second = threading.Thread(target=read, args=("rate",))              # waits behind the recording, sending nothing
    second.start()
    time.sleep(0.5)
    lab.stops.pulse()                                                    # a Stop ends that wait too
    second.join(10)
    assert isinstance(out["rate"], Stopped) and "nothing was sent" in str(out["rate"])
    assert calls() == ["acquireHistogram 3 began"]
    assert lab.read("tagger", "rate") == pytest.approx(1234.5)          # once the recording is done
    assert calls() == ["acquireHistogram 3 began", "acquireHistogram 3 ended", "getCountRate"]
    assert not lab.instruments["tagger"].backend.gone                   # its helper was never ended


def test_a_look_waits_for_a_recording_no_longer_than_its_own(slow, monkeypatch):
    """A watch a person put on a reading that acquires is the monitor's look at a recording: it gives up on its own
    call past LOOK_CALL_S (the call goes on), and waits behind it no longer than its patience."""
    lab, calls = slow
    monkeypatch.setattr(library, "LOOK_CALL_S", 0.5)
    began = time.monotonic()
    with patience(5.0), unstoppable(), pytest.raises(GaveUp):          # the monitor's look at the lab
        lab.read("tagger", "long")
    assert time.monotonic() - began < 2.5
    with patience(0.5), unstoppable(), pytest.raises(Busy, match="still in acquireHistogram; nothing was sent"):
        lab.read("tagger", "rate")
    assert lab.read("tagger", "rate") == pytest.approx(1234.5)          # once the recording is done
    assert calls() == ["acquireHistogram 3 began", "acquireHistogram 3 ended", "getCountRate"]
