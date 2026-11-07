"""A first start sets up whatever it can, and an identity Kelvin can trust (review of 0.3.45, 5.1-5.5, 5.7, 6.2).

- 5.1: one instrument that couldn't connect (a COM port another program holds) left the whole lab the set-up found
  empty, though its lab.yaml was written; Set up again then said "No instruments set up yet".
- 5.2: the first start wrote its draft over a lab.yaml the person saved meanwhile (their ±0.5 V limit became the
  maker's ±210 V) and switched the lab to it.
"""
import pytest

from labharness import builtin_registry
from labharness.adapters import set_resource_manager
from labharness.agent.approval import WebApprover
from labharness.autosetup import AutoSetup
from labharness.discovery import scan
from labharness.gate import ScriptedApprover
from labharness.homelab import build_lab, empty_lab, lab_registry
from test_hardware import Bus, Device

pytest.importorskip("mcp")
from labharness.agent.runtime import LabRuntime  # noqa: E402


class Held(Bus):
    """A bus where these addresses can't be opened once the scan has asked them (a port another program took)."""

    def __init__(self, devices, held=()):
        super().__init__(devices)
        self.held, self.scanned = set(held), False

    def open_resource(self, name):
        if self.scanned and name in self.held:
            raise OSError("VI_ERROR_RSRC_BUSY: the port is held by another program")
        return super().open_resource(name)


def three():
    return {"GPIB0::24::INSTR": Device("keithley/2400", name="k2400"),
            "GPIB0::8::INSTR": Device("srs/sr830", name="sr830"),
            "ASRL3::INSTR": Device("lakeshore/336", serial={"baud_rate": 57600, "data_bits": 7, "parity": "odd"},
                                   name="ls")}


@pytest.fixture
def bus():
    b = Held(three(), held={"ASRL3::INSTR"})
    set_resource_manager(b)
    yield b
    set_resource_manager(None)


def scanner(bus, then=None):
    def scanned():
        out, taken = [], []
        for f in scan(builtin_registry(), bus):
            name = f.suggest_name(taken) if f.card_id else None
            taken += [name] if name else []
            out.append(dict(vars(f), suggested=name))
        bus.scanned = True
        if then is not None:
            then()
        return out
    return scanned


def first_runtime(tmp_path):
    rt = LabRuntime(empty_lab(), WebApprover(timeout=5), tmp_path / "data")
    rt.home = tmp_path
    return rt


# ---------------------------------------------------------------------- 5.1

def test_a_first_start_uses_every_instrument_it_could_connect_and_names_the_one_it_couldnt(tmp_path, bus):
    rt = first_runtime(tmp_path)
    status = AutoSetup(rt, {}, tmp_path, fetch=lambda u: b"", scan=scanner(bus)).run()
    assert (tmp_path / "lab.yaml").exists() and rt.lab_info.get("kind") == "file"
    held = next(n for n, ic in rt.lab.config.instruments.items() if ic.address == "ASRL3::INSTR")
    used = sorted(n for n, i in rt.lab.instruments.items() if i.enabled)
    assert len(used) == 2 and held not in used                                  # the two it could connect: in use
    assert "held by another program" in rt.lab.unavailable[held]                # the third: set aside, saying why
    assert status["state"] == "done" and "2 instruments set up" in status["message"]
    assert "not connected: %s (" % held in status["message"] and status["not_connected"] == [held]
    assert "Not connected: %s" % held in rt.notifier.notices[-1]["text"] or any(
        "Not connected: %s" % held in n["text"] for n in rt.notifier.notices)
    again = AutoSetup(rt, {}, tmp_path, fetch=lambda u: b"", scan=scanner(bus)).run("asked")
    assert again["state"] == "done" and "instruments set up" in again["message"]   # not "No instruments set up yet"


def test_adding_an_instrument_to_a_lab_not_opened_yet_sets_the_one_that_cant_connect_aside(tmp_path, bus):
    (tmp_path / "lab.yaml").write_text('lab: Bench\ninstruments:\n  tc:\n    card: lakeshore/336\n'
                                       '    address: "ASRL3::INSTR"\n', encoding="utf-8")
    rt = LabRuntime(empty_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.cards_dir, rt.scan_network = tmp_path, tmp_path / "cards", False
    rt.lab_info = {"kind": "none", "path": str(tmp_path / "lab.yaml"), "error": "could not be opened",
                   "has_file": True}
    rt.scan()
    bus.scanned = True                                                         # the port is taken from now on
    out = rt.add_instrument("GPIB0::24::INSTR")
    assert out.startswith("added "), out                                       # not "could not connect your lab"
    assert rt.lab_info.get("kind") == "file" and "held by another program" in rt.lab.unavailable["tc"]
    assert any(i.enabled and ic.address == "GPIB0::24::INSTR"
               for (n, i), ic in zip(rt.lab.instruments.items(), rt.lab.config.instruments.values()))


# ---------------------------------------------------------------------- 5.2

MINE = """lab: Alice's lab
instruments:
  gate:
    card: keithley/2400
    address: GPIB0::24::INSTR
    limits: { V: [-0.5, 0.5] }    # what her sample takes
"""


def test_a_lab_config_the_person_saved_meanwhile_is_never_replaced_nor_switched_away(tmp_path):
    b = Held({"GPIB0::24::INSTR": Device("keithley/2400", name="k2400"),
              "GPIB0::8::INSTR": Device("srs/sr830", name="sr830")})
    set_resource_manager(b)
    try:
        rt = first_runtime(tmp_path)

        def save_and_connect():   # what Set up's Save and connect does, while the set-up scans
            (tmp_path / "lab.yaml").write_text(MINE, encoding="utf-8")
            lab = build_lab(MINE, None, 0, lab_registry(tmp_path), None, True, True)
            rt.switch_lab(lab, None, {"kind": "file", "path": str(tmp_path / "lab.yaml"), "error": None,
                                      "has_file": True}, "human (portal)")
        status = AutoSetup(rt, {}, tmp_path, fetch=lambda u: b"", scan=scanner(b, then=save_and_connect)).run()
        assert (tmp_path / "lab.yaml").read_text(encoding="utf-8") == MINE        # hers, as she wrote it
        assert rt.lab.config.name == "Alice's lab" and sorted(rt.lab.instruments) == ["gate"]
        assert "you saved your own lab config meanwhile" in status["message"]
        assert not (tmp_path / "lab.tmp").exists()
    finally:
        set_resource_manager(None)


# ---------------------------------------------------------------------- 5.3: a serial port is opened once

class Counting(Bus):
    """A bus that counts each address's opens; `refuse`: addresses whose open fails (another program has the port)."""

    def __init__(self, devices, refuse=()):
        super().__init__(devices)
        self.opens, self.refuse = {}, set(refuse)

    def open_resource(self, name):
        self.opens[name] = self.opens.get(name, 0) + 1
        if name in self.refuse:
            raise OSError("VI_ERROR_RSRC_BUSY: Access is denied")
        return super().open_resource(name)


def survey_of(rm, **more):
    from labharness.discovery import scan_survey
    return scan_survey(builtin_registry(), rm, network=False, usb=False, gpib_sweep=False, prologix_ports=[], **more)


def test_a_serial_port_is_opened_once_and_asked_at_each_framing_on_it(monkeypatch):
    from labharness import discovery
    monkeypatch.setattr(discovery, "_usb_ids", lambda: {})
    rm = Counting({"ASRL3::INSTR": Device("lakeshore/336", serial={"baud_rate": 57600, "data_bits": 7,
                                                                    "parity": "odd"}, name="ls"),
                   "ASRL4::INSTR": Device(silent=True)})
    found, _ = survey_of(rm)
    by = {f.address: f for f in found}
    assert by["ASRL3::INSTR"].card_id == "lakeshore/336" and by["ASRL3::INSTR"].settings.startswith("57600 7O")
    assert rm.opens == {"ASRL3::INSTR": 1, "ASRL4::INSTR": 1}                # 13 framings, one open each (13 before)


def test_a_port_another_program_holds_is_said_as_such_and_opened_once(monkeypatch):
    from labharness import discovery, others
    monkeypatch.setattr(discovery, "_usb_ids", lambda: {})
    monkeypatch.setattr(others, "holders", lambda: ["LabVIEW"])
    rm = Counting({"ASRL5::INSTR": Device(silent=True)}, refuse={"ASRL5::INSTR"})
    found, _ = survey_of(rm)
    assert rm.opens == {"ASRL5::INSTR": 1}
    how = found[0].how
    assert "could not open it" in how and "another program may have it open; running now: LabVIEW" in how, how
    assert "switched off" not in how


def test_a_bluetooth_port_is_not_asked_and_the_scan_says_what_it_asks(monkeypatch):
    from labharness import discovery
    monkeypatch.setattr(discovery, "_usb_ids", lambda: {})
    monkeypatch.setattr(discovery, "_bluetooth_ports", lambda: {"ASRL7::INSTR"})
    rm = Counting({"ASRL7::INSTR": Device(silent=True), "GPIB0::8::INSTR": Device("srs/sr830", name="sr830")})
    said = []
    found, _ = survey_of(rm, progress=said.append)
    assert "ASRL7::INSTR" not in rm.opens and [f.how for f in found if f.address == "ASRL7::INSTR"] == [discovery.BLUETOOTH]
    assert said == ["asking GPIB0::8::INSTR who it is, 1 of 1"]


def test_the_set_up_says_which_port_it_asks_now(tmp_path):
    rt = first_runtime(tmp_path)
    seen = []

    def scanning(progress=None):
        progress("asking COM4 who it is, 2 of 9")
        seen.append(setup.status["message"])
        return []
    setup = AutoSetup(rt, {}, tmp_path, fetch=lambda u: b"", scan=scanning)
    setup.run()
    assert seen == ["Looking for connected instruments: asking COM4 who it is, 2 of 9"]


# ---------------------------------------------------------------------- 5.4: a GPIB bus hung at start

class HungBoard:
    """A GPIB board an instrument hangs: every address times out instead of saying nothing listens."""
    __module__ = "pyvisa.highlevel"

    def __init__(self, answers=None, nobody=()):
        self.asked, self.answers, self.nobody = [], dict(answers or {}), set(nobody)

    def list_resources(self, query="?*::INSTR"):
        return ("GPIB0::INTFC",) if "INTFC" in query else ()

    def open_resource(self, name):
        board = self

        class Res:
            timeout = 2000

            def query(self, cmd, delay=None):
                board.asked.append(name)
                if name in board.answers:
                    return board.answers[name]
                if name in board.nobody:
                    raise OSError("VI_ERROR_NLISTENERS: no listeners")
                raise TimeoutError("VI_ERROR_TMO")

            def clear(self):
                raise TimeoutError("VI_ERROR_TMO")

            def close(self):
                pass
        return Res()


def test_a_hung_gpib_bus_stops_the_sweep_and_says_to_clear_it():
    from labharness.discovery import scan_survey
    from labharness.survey import reason
    rm = HungBoard()
    found, looked = scan_survey(builtin_registry(), rm, network=False, usb=False, gpib_sweep=True, prologix_ports=[],
                                ports=lambda: [])
    assert found == [] and len(rm.asked) == 3                                   # 3 timeouts, not 30 and 8 queries each
    assert "GPIB0 seems hung" in looked.problems[0] and "seems hung" in reason(found, looked)


def test_a_bus_with_an_old_silent_instrument_is_not_taken_for_a_hung_one():
    from labharness.discovery import gpib_listeners
    nobody = {"GPIB0::%d::INSTR" % p for p in range(1, 31)} - {"GPIB0::5::INSTR", "GPIB0::24::INSTR"}
    rm = HungBoard(answers={"GPIB0::24::INSTR": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1,C30"}, nobody=nobody)
    hung = []
    out = gpib_listeners(rm, "GPIB0", hung=hung)
    assert hung == [] and out == [("GPIB0::5::INSTR", ""), ("GPIB0::24::INSTR",
                                                            "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1,C30")]


# ---------------------------------------------------------------------- 5.5: a silent instrument isn't asked on and on

class AnswersOnlyWhoItIs:
    """A connection whose instrument answers its identity and nothing else (each other query times out)."""
    quiet_close = True

    def __init__(self, idn):
        self.idn, self.asked = idn, []

    def query(self, cmd):
        self.asked.append(cmd)
        if cmd.strip().upper() == "*IDN?":
            return self.idn
        raise TimeoutError("VI_ERROR_TMO")

    def write(self, cmd):
        self.asked.append(cmd)

    def close(self):
        pass


def silent_lab(card, idn):
    from labharness.clock import SimClock
    from labharness.lab import Lab
    from labharness.labconfig import load_lab_config
    registry = builtin_registry()
    backend = AnswersOnlyWhoItIs(idn)
    cfg = load_lab_config({"lab": "bench", "instruments": {"x": {"card": card, "address": "GPIB0::8::INSTR"}}},
                          registry)
    return Lab(cfg, registry, lambda name, c, address: backend, SimClock()), backend


def test_the_health_check_stops_at_an_instrument_that_doesnt_answer():
    lab, backend = silent_lab("srs/sr830", "Stanford_Research_Systems,SR830,s/n12345,ver1.07")
    result = lab.health_check(["x"])[0]
    assert not result.ok and result.problems
    readings = [c for c in backend.asked if c.strip().upper() != "*IDN?"]
    assert len(readings) <= 3, backend.asked   # its 11 readings each waited their timeout (36 s) before


def test_the_set_ups_read_stops_at_a_silent_instrument_and_skips_acquisitions():
    from labharness import profiles
    lab, backend = silent_lab("keithley/2400", "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30")
    values = profiles.read_values(lab.instruments["x"])
    asked = [v for v in values.values() if not str(v).startswith("ERROR: not read")]
    assert len(asked) == 1 and len(values) >= 10                                # 1 asked; 26 timeouts (78 s) before
    assert all(str(v).startswith("ERROR: not read: x did not answer") for v in list(values.values())[1:])

    class Spec:
        def __init__(self, name, **k):
            self.name, self.type, self.acquires, self.get_cmd = name, k.get("type", "scalar"), k.get("acquires", False), "Q?"

    class Camera:
        name, _last, read_ = "cam", {}, []
        card = type("C", (), {"params": {"peak": Spec("peak", acquires=True), "frame": Spec("frame", type="image"),
                                         "temperature": Spec("temperature")}})()

        def read(self, p):
            Camera.read_.append(p)
            return 1.0
    assert profiles.read_values(Camera()) == {"temperature": 1.0} and Camera.read_ == ["temperature"]


def test_the_set_ups_read_holds_only_its_instrument_and_never_waits_out_a_run(tmp_path, monkeypatch):
    import threading
    from labharness import profiles
    from labharness.lablock import LabLock
    monkeypatch.setattr(profiles, "PREPARE_WAIT_S", 0.3)
    lab, backend = silent_lab("srs/sr830", "Stanford_Research_Systems,SR830,s/n12345,ver1.07")
    lock, said = LabLock(), []
    held, done = threading.Event(), threading.Event()

    def a_run():   # the whole lab, as a run that names every instrument
        with lock:
            held.set()
            done.wait(10)
    t = threading.Thread(target=a_run, daemon=True)
    t.start()
    assert held.wait(5)
    report = profiles.prepare(tmp_path, lab, lock, manuals=False, busy=said.append)
    assert report["x"]["settings"].startswith("not read now: other work has it") and backend.asked == []
    done.set()
    t.join(5)
    report = profiles.prepare(tmp_path, lab, lock, manuals=False, busy=said.append)
    assert "did not answer" in report["x"]["settings"] and said == ["reading the settings of x", None]
    assert profiles.unprepared(tmp_path, lab) == []                              # it has its folder now


def test_after_a_change_of_the_lab_only_the_new_instruments_are_read(tmp_path, bus):
    from labharness import profiles
    rt = first_runtime(tmp_path)
    AutoSetup(rt, {}, tmp_path, fetch=lambda u: b"", scan=scanner(bus)).run()
    assert profiles.unprepared(tmp_path, rt.lab) == [n for n in rt.lab.instruments if not rt.lab.instruments[n].enabled]
    read = []
    real = profiles.prepare

    def counting(*a, **k):
        read.append(k.get("names"))   # None: every instrument, as each Save and connect read them before
        return real(*a, **k)
    profiles.prepare = counting
    try:
        status = AutoSetup(rt, {}, tmp_path, fetch=lambda u: b"").run("lab changed")
    finally:
        profiles.prepare = real
    assert read == [[]] and status["state"] == "done"                          # nothing new: nothing read again


def test_the_draft_is_written_only_as_a_new_file(tmp_path):
    from labharness.autosetup import _write_new
    path = tmp_path / "lab.yaml"
    assert _write_new(path, "lab: draft\n") and path.read_text(encoding="utf-8") == "lab: draft\n"
    assert not _write_new(path, "lab: another\n") and path.read_text(encoding="utf-8") == "lab: draft\n"
    assert not (tmp_path / "lab.tmp").exists()
