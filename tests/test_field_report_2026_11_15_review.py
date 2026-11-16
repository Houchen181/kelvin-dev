"""The independent review of the 2026-11-15 field report's lab fixes (work beside a run, Set up's saves) found ways
past them, each shown with a script; these are those scripts as tests:

- another spelling of an instrument the run uses (its raw socket beside its VXI-11 address, 127.0.0.1 for
  localhost) went beside the run and sent it commands, a write among them;
- between a run's steps it holds no instrument (its report written, the queue between items), and work beside it took
  one of the run's own; nothing made the run wait for work beside it before it held that instrument again;
- work beside a run checked what the lab's holder used once, before an approval card a person answered minutes later;
- check_script beside a run failed on a lab's own name for a setting (names: Vg: smu.V);
- a save in Set up could still go over an instrument added while Kelvin connected the person's edit;
- a merge of an added entry into a text indented otherwise put it inside another instrument;
- an instrument taken out and put back was still said to be taken out."""
import threading
import time

import pytest

from labharness.clock import SimClock
from labharness.gate import Approver, ScriptedApprover
from labharness.homelab import builtin_registry
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.lablock import LabLock
from labharness.sim.twin import World, sim_backend_factory
from test_add_instrument import SMU_ONLY, file_runtime
from test_field_report_2026_11_15_lab import BENCH_LAB, LOG, Run, portal
from test_hardware import Bus, Device
from test_script_runs import folder, script_runtime, write  # noqa: F401  (fixtures)

LAN_LAB = """lab: Bench
instruments:
  smu:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
  board:
    card: srs/sr830
    address: "TCPIP0::192.168.3.1::inst0::INSTR"
"""


@pytest.fixture
def one_board_two_ways():
    """One instrument at its VXI-11 address and its raw SCPI port, and by localhost and 127.0.0.1."""
    from labharness.adapters import set_resource_manager
    board, other = Device("srs/sr830", name="board"), Device("srs/sr830", name="other")
    bus = Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k2400"),
               "TCPIP0::192.168.3.1::inst0::INSTR": board,
               "TCPIP0::192.168.3.1::5025::SOCKET": board,
               "TCPIP0::localhost::5025::SOCKET": board,
               "TCPIP0::127.0.0.1::5025::SOCKET": board,
               "TCPIP0::192.168.3.9::5025::SOCKET": other,   # two ports of one terminal server: two instruments
               "TCPIP0::192.168.3.9::5026::SOCKET": Device("srs/sr830", name="third")})
    set_resource_manager(bus)
    yield board
    set_resource_manager(None)


# ------------------------------------------------------------------ another way into the run's instrument


def test_trying_a_card_on_another_port_of_the_runs_instrument_waits_for_the_run(tmp_path, one_board_two_ways):
    from labharness.agent.runtime import LabBusy
    rt = file_runtime(tmp_path, LAN_LAB)
    card = builtin_registry().get("srs/sr830")
    with Run(rt, names=["board"]):
        one_board_two_ways.sent.clear()
        with pytest.raises(LabBusy, match="it uses board"):
            rt.card_try(card, "TCPIP0::192.168.3.1::5025::SOCKET")
        assert one_board_two_ways.sent == []


def test_adding_another_spelling_of_the_runs_instrument_waits_and_sends_nothing(tmp_path, one_board_two_ways):
    rt = file_runtime(tmp_path, LAN_LAB.replace("192.168.3.1::inst0::INSTR", "localhost::5025::SOCKET"))
    rt.last_found = ["TCPIP0::127.0.0.1::5025::SOCKET"]
    with Run(rt, names=["board"]):
        one_board_two_ways.sent.clear()
        said = rt.add_instrument("TCPIP0::127.0.0.1::5025::SOCKET", card="srs/sr830", name="board2")
        assert said.startswith("not done: Kelvin is busy with") and "it uses board" in said, said
        assert one_board_two_ways.sent == []
    said = rt.add_instrument("TCPIP0::127.0.0.1::5025::SOCKET", card="srs/sr830", name="board2")   # the lab free
    assert said.startswith("TCPIP0::127.0.0.1::5025::SOCKET is in the lab already, as board"), said
    assert "board2" not in rt.lab.instruments


def test_another_port_of_a_lab_instrument_is_in_the_lab_already(tmp_path, one_board_two_ways):
    rt = file_runtime(tmp_path, LAN_LAB)
    rt.last_found = ["TCPIP0::192.168.3.1::5025::SOCKET"]
    said = rt.add_instrument("TCPIP0::192.168.3.1::5025::SOCKET", card="srs/sr830", name="board2")
    assert said.startswith("TCPIP0::192.168.3.1::5025::SOCKET is in the lab already, as board (at "), said
    assert "the same instrument another way in" in said and "board2" not in rt.lab.instruments


# ------------------------------------------------------------------ between a run's steps, and after a card


def test_between_its_steps_a_runs_own_instrument_never_goes_beside_it(tmp_path, one_board_two_ways):
    rt = file_runtime(tmp_path, LAN_LAB)
    in_report, done = threading.Event(), threading.Event()

    def run():   # a run: every instrument, then its plan's, then its report (files only), then every one again
        assert rt.lock.acquire(timeout=5)
        rt.live = dict(LOG)
        try:
            with rt.lock.only(["board"]):
                pass
            with rt.lock.only([]):
                in_report.set()
                done.wait(10)
        finally:
            rt.live = dict(LOG, status="done")
            rt.lock.release()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert in_report.wait(5)
    try:
        one_board_two_ways.sent.clear()
        said = rt.reconnect_instrument("board")
        assert said.startswith("not done: Kelvin is busy with") and "it uses board" in said, said
        assert one_board_two_ways.sent == []
    finally:
        done.set()
        t.join(5)


def test_beside_work_that_never_said_what_it_uses_is_refused(tmp_path, one_board_two_ways):
    rt = file_runtime(tmp_path, LAN_LAB)
    with Run(rt, names=[]):   # holding none, and never said which it uses
        said = rt.reconnect_instrument("board")
    assert "it hasn't said which instruments it uses" in said, said


class AnswersLater(Approver):
    """A person who answers the card once the run that had the lab has ended and another, on board2, has begun."""

    def __init__(self, rt, first):
        self.rt, self.first, self.second, self.asked = rt, first, None, []

    def approve(self, request):
        self.asked.append(request.title)
        self.first.__exit__(None, None, None)
        self.second = Run(self.rt, names=["board2"]).__enter__()
        return True

    def inform(self, request, reasons):
        pass


def test_an_add_beside_a_run_is_checked_again_after_its_card(tmp_path, one_board_two_ways):
    lab = LAN_LAB + """  board2:
    card: srs/sr830
    address: "TCPIP0::192.168.3.9::5025::SOCKET"
"""
    rt = file_runtime(tmp_path, lab)
    rt.last_found = []   # an address the scan didn't find, and web text read: a person is asked
    first = Run(rt, names=["board"]).__enter__()
    person = AnswersLater(rt, first)
    rt.gate.approver = person
    rt.gate.note_outside("a web page")
    try:
        said = rt.add_instrument("TCPIP0::192.168.3.9::5026::SOCKET", card="srs/sr830", name="board3")
    finally:
        if person.second is not None:
            person.second.__exit__(None, None, None)
    assert person.asked, said   # the card was shown, beside the first run
    assert said.startswith("not added: Kelvin is busy with") and "it uses board2" in said, said
    assert "board3" not in rt.lab.instruments


def test_a_run_holding_its_instruments_again_waits_for_work_beside_on_them():
    lock = LabLock()
    stops = [0]
    lock.stops = lambda: stops[0]
    narrowed, back = threading.Event(), {}

    def run():
        with lock:
            with lock.only(["gauge"]):
                narrowed.set()
                time.sleep(0.3)
            back["at"] = time.monotonic()   # holding every instrument again
    beside_until = {}

    def beside():
        with lock.beside(["board"]):
            time.sleep(1.0)
            beside_until["at"] = time.monotonic()
    t = threading.Thread(target=run)
    t.start()
    assert narrowed.wait(5)
    b = threading.Thread(target=beside)
    b.start()
    t.join(10)
    b.join(10)
    assert back["at"] >= beside_until["at"] - 0.05   # it held board again only once the work beside had finished

    narrowed.clear()
    t = threading.Thread(target=run)
    t.start()
    assert narrowed.wait(5)
    b = threading.Thread(target=lambda: lock.beside(["board"]).__enter__())   # one that never ends
    b.start()
    b.join(5)
    t0 = time.monotonic()
    time.sleep(0.4)
    stops[0] += 1   # a person's Stop ends the wait
    t.join(10)
    assert not t.is_alive() and back["at"] - t0 < 3.0


# ------------------------------------------------------------------ check_script and the lab's own names

NAMED_LAB = """
lab: Script lab
instruments:
  smu:
    card: virtual/source-meter
    address: GPIB0::24::INSTR
    limits: { V: [-10, 10] }
    rates: { V: 10 }
  lockin:
    card: virtual/lock-in
    address: GPIB0::8::INSTR
names: { Vg: smu.V }
"""

EXPERIMENT = '''
from kelvin import lab, record

def main():
    lab.set("Vg", 0.5)
    record(X=lab.get("lockin.X"))

main()
'''


def test_check_script_beside_a_run_takes_the_labs_own_names(folder, tmp_path):
    registry = builtin_registry()
    clock = SimClock()
    world = World(clock, 0)
    lab = Lab(load_lab_config(NAMED_LAB, registry), registry, sim_backend_factory(world), clock, world)
    write(folder, "exp.py", EXPERIMENT)
    rt = script_runtime(lab, folder, tmp_path)
    with Run(rt, names=["lockin"]):
        said = rt.check_script("exp.py")
    assert "run_script asks a person once:" in said and "smu.V" in said, said


# ------------------------------------------------------------------ Set up's saves


def test_a_save_never_goes_over_an_instrument_added_while_it_connected(tmp_path, one_board_two_ways, monkeypatch):
    pytest.importorskip("starlette")   # the portal: not in CI's core job (Python 3.9, PyYAML and pytest only)
    pytest.importorskip("mcp")
    from labharness.web import app as app_module
    runtime, client, headers = portal(tmp_path)
    with client:
        assert client.post("/api/lab", json={"yaml": BENCH_LAB}, headers=headers).json()["ok"]
        base = client.get("/api/lab", headers=headers).json()["yaml"]
        build = app_module.build_lab
        added = BENCH_LAB + '  board:\n    card: srs/sr830\n    address: "TCPIP0::192.168.3.9::5025::SOCKET"\n'

        def building(*a, **k):   # while Kelvin connects the person's edit, it adds an instrument from a chat
            (tmp_path / "lab.yaml").write_text(added, encoding="utf-8")
            return build(*a, **k)
        monkeypatch.setattr(app_module, "build_lab", building)
        r = client.post("/api/lab", json={"yaml": base.replace("[-5, 5]", "[-2, 2]"), "base": base},
                        headers=headers)
        assert r.status_code == 409 and r.json()["yaml"] == added, r.text
        assert "board added" in r.json()["error"]
        assert (tmp_path / "lab.yaml").read_text(encoding="utf-8") == added


def test_an_entry_indented_otherwise_is_never_merged_into_another():
    from labharness.discovery import merge_added
    base = "lab: Bench\ninstruments:\n    smu:\n        card: keithley/2400\n        address: \"GPIB0::24::INSTR\"\n"
    now = base + "    lockin:\n        card: srs/sr830\n        address: \"GPIB0::8::INSTR\"\n"
    mine = "lab: Bench\ninstruments:\n  smu:\n    card: keithley/2400\n    address: \"GPIB0::24::INSTR\"\n"
    assert merge_added(mine, base, now) == (None, [])


def test_an_instrument_put_back_is_no_longer_said_to_be_taken_out(tmp_path, one_board_two_ways):
    from labharness.configversions import ConfigVersions
    rt = file_runtime(tmp_path, LAN_LAB)
    rt.versions = ConfigVersions(tmp_path)
    rt.config_noticed()
    (tmp_path / "lab.yaml").write_text(SMU_ONLY, encoding="utf-8")
    rt.config_changed("human (portal)", "", "config")
    rt.lab.remove("board")
    assert "board: taken out of the lab config" in rt.lab_status()
    (tmp_path / "lab.yaml").write_text(LAN_LAB, encoding="utf-8")   # put back
    rt.config_changed("human (portal)", "", "config")
    rt.lab.config.instruments = dict(rt.lab.config.instruments, board=load_lab_config(
        LAN_LAB, builtin_registry()).instruments["board"])
    assert "taken out" not in rt.lab_status()
