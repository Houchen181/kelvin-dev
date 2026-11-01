"""A lab's LabVIEW files read in a process of their own (viworker.py), as every survey reads them (Set up's "From
your LabVIEW…", keeping its scans, survey_labview): a file that hangs the reader or takes it down is said and the rest
are read; a survey past its time limit keeps what it read; the reader has none of Kelvin's keys; and the app starts
it the way it starts its other helpers (Kelvin.exe --bridge)."""
import json
import subprocess
import sys
import time
from pathlib import Path

from labharness import labview, vigraph, viworker

ROOT = Path(__file__).resolve().parent.parent
AIM = Path(__file__).parent / "fixtures" / "labview" / "aim_tti_cpx400sp" / "Public"
PYTHON = getattr(sys, "_base_executable", "") or sys.executable   # as viworker starts one (a venv's starts another)

# a stand-in for the reader, to see what the survey does with one that hangs, stops, or reads Kelvin's keys
FAKE = r'''
import json, os, sys, time
out = os.fdopen(os.dup(1), "wb")
job = json.loads(sys.stdin.buffer.readline())
def say(msg):
    out.write((json.dumps(msg) + "\n").encode())
    out.flush()
say({"ready": True})
for i, (kind, path) in enumerate(job["files"]):
    name = os.path.basename(path)
    if name.startswith("slow"):
        time.sleep(60)
    if name.startswith("gone"):
        os._exit(3)
    keys = sorted(k for k in os.environ if k.endswith("_API_KEY"))
    say({"i": i, "s": [{"summary": 1, "name": name, "path": path, "labview": "", "read": "no", "linkage": [],
                        "controls": [], "visa": [], "scpi": [], "diagram": [], "unread": ["keys: %s" % keys]}],
         "done": True})
'''


def fake_reader(tmp_path, monkeypatch, **limits):
    script = tmp_path / "fake_reader.py"
    script.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(viworker, "command", lambda: [PYTHON if Path(PYTHON).is_file() else sys.executable, "-I",
                                                      str(script)])
    for name, value in limits.items():
        monkeypatch.setattr(viworker, name, value)


def said(results):
    return [[s["unread"][0] for s in summaries] for summaries in results]


def test_the_survey_reads_its_vis_in_a_process_of_its_own(tmp_path, monkeypatch):
    started = []
    real = viworker.command
    monkeypatch.setattr(viworker, "command", lambda: started.append(1) or real())
    monkeypatch.setattr(labview, "VISACONF", tmp_path / "no NI-VISA here" / "visaconf.ini")
    found = labview.survey(AIM)
    assert started == [1]
    assert {s["name"]: s for s in found.vis}["set_v.vi"] == vigraph.summary(AIM / "set_v.vi", link=labview.link)


def test_a_file_that_hangs_or_takes_the_reader_down_is_said_and_the_rest_are_read(tmp_path, monkeypatch):
    fake_reader(tmp_path, monkeypatch, FILE_SECONDS=1.0)
    files = [("vi", str(tmp_path / name)) for name in ("a.vi", "slow.vi", "b.vi", "gone.vi", "c.vi")]
    t0 = time.monotonic()
    results, notes = viworker.read_all(files, tmp_path)
    assert time.monotonic() - t0 < 20 and notes == []
    assert said(results) == [["keys: []"],
                             ["not read: it took too long to read (more than 1 s): Kelvin stopped reading it"],
                             ["keys: []"], ["not read: Kelvin's reader stopped on it"], ["keys: []"]]


def test_a_survey_past_its_time_limit_keeps_what_it_read(tmp_path, monkeypatch):
    fake_reader(tmp_path, monkeypatch, FILE_SECONDS=30.0, SURVEY_SECONDS=1.5)
    files = [("vi", str(tmp_path / name)) for name in ("a.vi", "slow.vi", "b.vi")]
    results, notes = viworker.read_all(files, tmp_path)
    assert said(results) == [["keys: []"], ["not read: the survey's time ran out before it was read"],
                             ["not read: the survey's time ran out before it was read"]]
    assert notes == ["the survey stopped at its time limit (1.5 s): 2 files not read"]


def test_the_reader_has_none_of_kelvins_keys(tmp_path, monkeypatch):
    fake_reader(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-key")
    results, _ = viworker.read_all([("vi", str(tmp_path / "a.vi"))], tmp_path)
    assert said(results) == [["keys: []"]]


def test_the_app_starts_the_reader_with_its_bridge_and_brings_it(tmp_path, monkeypatch):
    """Kelvin.exe runs the reader's script with --bridge (packaging/kelvin_app.py), as its other helpers; the build
    (kelvin.spec) and the package (pyproject.toml) bring the script."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert viworker.command() == [sys.executable, "--bridge", str(viworker.RUNNER)]
    monkeypatch.undo()
    assert viworker.RUNNER.is_file() and viworker.self_test()   # what the release build's self-test can ask
    job = {"path": [str(ROOT / "src")] + sys.path, "memory_mb": viworker.MEMORY_MB, "folder": str(AIM), "own": [],
           "files": [["vi", str(AIM / "set_v.vi")]]}
    out = subprocess.run([PYTHON if Path(PYTHON).is_file() else sys.executable, "-I",
                          str(ROOT / "packaging" / "kelvin_app.py"), "--bridge", str(viworker.RUNNER)],
                         input=(json.dumps(job) + "\n").encode(), stdout=subprocess.PIPE, timeout=60)
    lines = [json.loads(line) for line in out.stdout.splitlines()]
    assert lines[0] == {"ready": True} and lines[1]["done"] and lines[1]["s"][0]["read"] == "full"
    spec = (ROOT / "packaging" / "kelvin.spec").read_text(encoding="utf-8")
    assert '"data" / "labview" / "kelvin_labview.py"), "labharness/data/labview")' in spec
    assert '"data/labview/*.py"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
