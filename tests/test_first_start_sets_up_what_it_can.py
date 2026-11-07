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


def test_the_draft_is_written_only_as_a_new_file(tmp_path):
    from labharness.autosetup import _write_new
    path = tmp_path / "lab.yaml"
    assert _write_new(path, "lab: draft\n") and path.read_text(encoding="utf-8") == "lab: draft\n"
    assert not _write_new(path, "lab: another\n") and path.read_text(encoding="utf-8") == "lab: draft\n"
    assert not (tmp_path / "lab.tmp").exists()
