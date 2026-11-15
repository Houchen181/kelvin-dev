"""A field report (2026-11-15) from a lab bringing up an NV pulsed-measurement rig while a 289-point attoDRY pressure
log (about 43 hours, the attoDRY alone) ran in the queue:

- item 2: check_script, which only reads a script and says what its run would ask, was refused ("Kelvin is busy with
  the running plan attodry maintenance pressure log"), and add_instrument for the QICK board, on a network of its own
  and never touched by the log, was refused twice ("a plan or the queue is running"), for two days;
- item 6: the QICK board was gone from the lab after a reload, and lab_status said nothing of it.

Work that drives nothing on instruments the running work doesn't use now goes beside it (runtime._beside): adding an
instrument, connecting one again. check_script never waits for the lab: beside a run it reads the latest values Kelvin
has seen (_CheckGate), never the instruments."""
import re
import threading
import time

import pytest

from labharness.gate import ScriptedApprover
from test_add_instrument import SMU_ONLY, file_runtime
from test_hardware import Bus, Device
from test_script_runs import SWEEP, folder, lab, script_runtime, write  # noqa: F401  (fixtures)

LOG = {"run_id": "20261113-194813", "plan": "attodry_maintenance_pressure_log", "total": 289, "done": 41,
       "eta_s": 150000.0, "status": "running"}


class Run:
    """A run on these instruments (None: every one), holding the lab on a thread of its own until it ends, as the
    queue's 43-hour log did."""

    def __init__(self, rt, names=None):
        self.rt, self.names = rt, names
        self.started, self.end = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self):
        assert self.rt.lock.acquire(timeout=5, names=self.names)
        self.rt.live = dict(LOG)
        self.started.set()
        try:
            self.end.wait(30)
        finally:
            self.rt.live = dict(LOG, status="done")
            self.rt.lock.release()

    def __enter__(self):
        self.thread.start()
        assert self.started.wait(5)
        return self

    def __exit__(self, *exc):
        self.end.set()
        self.thread.join(5)


def sent_to(lab, names):
    """Every command sent to these instruments from now on."""
    sent = []
    for name in names:
        backend = lab.instruments[name].backend
        for verb in ("query", "write"):
            original = getattr(backend, verb)

            def record(cmd, *a, _send=original, _name=name, **k):
                sent.append((_name, cmd))
                return _send(cmd, *a, **k)
            setattr(backend, verb, record)
    return sent


# ------------------------------------------------------------------ item 2: check_script beside a run


def test_check_script_answers_beside_a_run_and_sends_nothing(lab, folder, tmp_path):
    write(folder, "sweep.py", SWEEP)
    rt = script_runtime(lab, folder, tmp_path)
    alone = rt.check_script("sweep.py")   # the lab free: as before
    assert "Run script sweep.py?" not in alone and "run_script asks a person once:" in alone, alone
    sent = sent_to(rt.lab, ["smu", "lockin", "laser", "lockin2"])
    with Run(rt, names=["lockin"]):
        t0 = time.monotonic()
        said = rt.check_script("sweep.py")
        took = time.monotonic() - t0
    assert took < 3.0, took
    assert not said.startswith("not done"), said
    assert "Kelvin is busy with" not in said.split("\n")[0]
    assert "run_script asks a person once:" in said and "smu.V: now" in said, said
    assert "the values above are the latest it has seen" in said and "attodry_maintenance_pressure_log" in said, said
    assert sent == []   # nothing asked of any instrument: not the one the run uses, nor the others


def test_check_script_beside_a_run_on_every_instrument_still_answers(lab, folder, tmp_path):
    write(folder, "sweep.py", SWEEP)
    rt = script_runtime(lab, folder, tmp_path)
    sent = sent_to(rt.lab, ["smu", "lockin"])
    with Run(rt, names=None):
        said = rt.check_script("sweep.py")
    assert "run_script asks a person once:" in said and "latest it has seen" in said, said
    assert sent == []


# ------------------------------------------------------------------ item 2: adding an instrument beside a run


@pytest.fixture
def board_bus():
    """The lab's source-meter on GPIB, and a lock-in on a network address of its own (the QICK board's place)."""
    from labharness.adapters import set_resource_manager
    bus = Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k2400"),
               "GPIB0::8::INSTR": Device("srs/sr830", name="sr830"),
               "TCPIP0::192.168.3.1::5025::SOCKET": Device("srs/sr830", name="board")})
    set_resource_manager(bus)
    yield bus
    set_resource_manager(None)


def test_an_instrument_the_run_never_touches_is_added_beside_it(tmp_path, board_bus):
    rt = file_runtime(tmp_path, SMU_ONLY)
    rt.gate.approver = ScriptedApprover([])   # nobody is asked: an address the scan found connects and reads
    rt.last_found = ["TCPIP0::192.168.3.1::5025::SOCKET"]
    with Run(rt, names=["smu"]) as run:
        said = rt.add_instrument("TCPIP0::192.168.3.1::5025::SOCKET", card="srs/sr830", name="board")
        assert said.startswith("added board ("), said
        assert rt.lock.owner()[0] == run.thread.ident   # the run went on holding the lab throughout
        assert "board" in rt.lab.instruments and rt.lab.instruments["board"].enabled
    text = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    assert text.startswith(SMU_ONLY) and "  board:\n    card: srs/sr830" in text, text


def test_an_instrument_on_the_runs_gpib_bus_waits_for_it_and_says_why(tmp_path, board_bus):
    rt = file_runtime(tmp_path, SMU_ONLY)
    rt.last_found = ["GPIB0::8::INSTR"]
    with Run(rt, names=["smu"]):
        said = rt.add_instrument("GPIB0::8::INSTR", card="srs/sr830", name="lockin")
    assert said.startswith("not done: Kelvin is busy with the running plan attodry_maintenance_pressure_log"), said
    assert "It can't go beside that work: GPIB0::8::INSTR is on GPIB0, a GPIB bus" in said, said
    assert "lockin" not in rt.lab.instruments and "lockin:" not in (tmp_path / "lab.yaml").read_text(encoding="utf-8")


def test_beside_a_run_that_holds_every_instrument_nothing_is_added(tmp_path, board_bus):
    rt = file_runtime(tmp_path, SMU_ONLY)
    rt.last_found = ["TCPIP0::192.168.3.1::5025::SOCKET"]
    with Run(rt, names=None):
        said = rt.add_instrument("TCPIP0::192.168.3.1::5025::SOCKET", card="srs/sr830", name="board")
    assert said.startswith("not done: Kelvin is busy with") and "it uses every instrument" in said, said
    assert "board" not in rt.lab.instruments


def test_with_the_lab_free_and_the_queue_between_its_items_an_instrument_is_added(tmp_path, board_bus, monkeypatch):
    rt = file_runtime(tmp_path, SMU_ONLY)
    rt.last_found = ["TCPIP0::192.168.3.1::5025::SOCKET"]
    monkeypatch.setattr(type(rt.queue), "state", property(lambda self: "running"), raising=False)
    said = rt.add_instrument("TCPIP0::192.168.3.1::5025::SOCKET", card="srs/sr830", name="board")
    assert said.startswith("added board ("), said


def test_an_instrument_the_run_doesnt_use_is_connected_again_beside_it(tmp_path, board_bus):
    rt = file_runtime(tmp_path, SMU_ONLY + """  board:
    card: srs/sr830
    address: "TCPIP0::192.168.3.1::5025::SOCKET"
""")
    assert "board" in rt.lab.instruments
    with Run(rt, names=["smu"]):
        said = rt.reconnect_instrument("board")
        assert said.startswith("board is connected"), said
        said = rt.reconnect_instrument("smu")   # the one the run uses waits for it
        assert said.startswith("not done: Kelvin is busy with") and "it uses smu" in said, said


# ------------------------------------------------------------------ item 6: an instrument left out is said


def test_lab_status_names_an_instrument_left_out_of_the_lab(tmp_path, board_bus):
    from labharness.homelab import LEFT_OUT
    rt = file_runtime(tmp_path, SMU_ONLY)
    why = LEFT_OUT % "Kelvin has no card qick/rfsoc-nv (deleted in Lab, Cards…)"
    rt.lab_info = dict(rt.lab_info, left_out={"qick": why})
    status = rt.lab_status()
    assert "qick: in the lab config (lab.yaml), but left out of the lab: Kelvin has no card qick/rfsoc-nv" in status
    assert "tell the person" in status.split("qick: ", 1)[1].split("\n", 1)[0]


# ------------------------------------------------------------------ item 6: Set up never saves over what changed


BENCH_LAB = """lab: Bench
instruments:
  smu:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
"""


def portal(tmp_path):
    pytest.importorskip("starlette")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.homelab import empty_lab
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted, client_for
    runtime = LabRuntime(empty_lab(), WebApprover(timeout=5), tmp_path / "data")
    parts = (runtime, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats"))
    client, headers = client_for(parts, {"scripted": Scripted([])})
    return runtime, client, headers


def test_set_up_opened_before_kelvin_added_an_instrument_keeps_it(tmp_path, board_bus):
    runtime, client, headers = portal(tmp_path)
    with client:
        assert client.post("/api/lab", json={"yaml": BENCH_LAB}, headers=headers).json()["ok"]
        base = client.get("/api/lab", headers=headers).json()["yaml"]   # the person opens Lab, Set up…
        runtime.last_found = ["GPIB0::8::INSTR"]
        said = runtime.add_instrument("GPIB0::8::INSTR", card="srs/sr830", name="lockin")   # meanwhile, from a chat
        assert said.startswith("added lockin ("), said
        mine = base.replace("V: [-5, 5]", "V: [-2, 2]")   # their edit, of the file as they opened it
        r = client.post("/api/lab", json={"yaml": mine, "base": base}, headers=headers)
        assert r.status_code == 200, r.text
        r = r.json()
        assert r["ok"] and r["kept"] == ["lockin"], r
        text = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
        assert "V: [-2, 2]" in text and "  lockin:\n    card: srs/sr830" in text, text
        assert "lockin" in runtime.lab.instruments and r["yaml"] == text


def test_set_up_saved_after_another_change_is_refused_and_shows_the_file(tmp_path, board_bus):
    runtime, client, headers = portal(tmp_path)
    with client:
        assert client.post("/api/lab", json={"yaml": BENCH_LAB}, headers=headers).json()["ok"]
        base = client.get("/api/lab", headers=headers).json()["yaml"]
        elsewhere = base.replace("V: [-5, 5]", "V: [-3, 3]")   # another window's save meanwhile
        (tmp_path / "lab.yaml").write_text(elsewhere, encoding="utf-8")
        r = client.post("/api/lab", json={"yaml": base.replace("lab: Bench", "lab: Bench 2"), "base": base},
                        headers=headers)
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["changed"] and body["yaml"] == elsewhere
        assert "changed since you opened it (an instrument's entry or the lab's own settings changed)" in body["error"]
        assert (tmp_path / "lab.yaml").read_text(encoding="utf-8") == elsewhere   # never saved over
        old = client.post("/api/lab", json={"yaml": elsewhere.replace("V: [-3, 3]", "V: [-1, 1]")}, headers=headers)
        assert old.json()["ok"]   # a save that says nothing of what it was made from (an older page) is as before


def test_merge_added_takes_only_instruments_added_meanwhile():
    from labharness.discovery import merge_added
    lockin = '  lockin:   # added by Kelvin\n    card: srs/sr830\n    address: "GPIB0::8::INSTR"\n    added_by: kelvin\n'
    now = BENCH_LAB + lockin
    mine = BENCH_LAB.replace("lab: Bench", "lab: Bench B")
    merged, added = merge_added(mine, BENCH_LAB, now)
    assert added == ["lockin"] and merged == mine + lockin
    assert merge_added(mine + lockin.replace("lockin", "other"), BENCH_LAB, now) == (None, [])   # the same address
    assert merge_added(mine, BENCH_LAB, now.replace("lab: Bench", "lab: Other")) == (None, [])   # more changed
    assert merge_added(mine, BENCH_LAB, BENCH_LAB.replace("[-5, 5]", "[-4, 4]")) == (None, [])


def test_trying_a_card_on_an_address_the_run_never_touches_goes_beside_it(tmp_path, board_bus):
    from labharness import builtin_registry
    from labharness.agent.runtime import LabBusy
    rt = file_runtime(tmp_path, SMU_ONLY)
    card = builtin_registry().get("srs/sr830")
    with Run(rt, names=["smu"]):
        result = rt.card_try(card, "TCPIP0::192.168.3.1::5025::SOCKET")   # Set up's Try, a person's
        assert result["identity"].startswith("Stanford_Research_Systems"), result
        with pytest.raises(LabBusy, match="GPIB0::8::INSTR is on GPIB0, a GPIB bus"):
            rt.card_try(card, "GPIB0::8::INSTR")   # on the run's own bus: it waits for the run


def test_lab_status_says_when_and_by_whom_an_instrument_was_taken_out(tmp_path, board_bus):
    from labharness.configversions import ConfigVersions
    rt = file_runtime(tmp_path, SMU_ONLY + """  qick:
    card: srs/sr830
    address: "TCPIP0::192.168.3.1::5025::SOCKET"
""")
    rt.versions = ConfigVersions(tmp_path)
    rt.config_noticed()   # the lab config as Kelvin found it
    (tmp_path / "lab.yaml").write_text(SMU_ONLY, encoding="utf-8")   # a save of an editor opened before qick was added
    rt.config_changed("human (portal)", "tightened the smu", "config")
    rt.lab.remove("qick")
    status = rt.lab_status()
    line = next(l for l in status.split("\n") if l.startswith("qick: "))
    assert "taken out of the lab config on 20" in line and "by human (portal) (why: tightened the smu)" in line, line


def test_validate_plan_answers_beside_a_run_and_sends_nothing(tmp_path):
    pytest.importorskip("mcp")
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    plan = "plan: iv\nsweep: { smu.V: {from: 0, to: 1, step: 0.5} }\nmeasure: [smu.I, lockin.X]\nafter: { smu.V: 0 }\n"
    alone = rt.validate(plan)
    assert alone.startswith("ok: iv"), alone
    sent = sent_to(rt.lab, list(rt.lab.instruments))
    with Run(rt, names=["tc"]):
        said = rt.validate(plan)
    assert said.startswith("ok: iv") and "the latest values it has seen" in said, said

    def asked(text):   # what the card would ask, a guard's reading of the moment (noise) left out
        return [re.sub(r"\(now [^)]*\)", "", line) for line in text.split("\n")
                if line.startswith(("needs approval", "no approval"))]
    assert asked(said) == asked(alone) and asked(said), said
    assert sent == []
