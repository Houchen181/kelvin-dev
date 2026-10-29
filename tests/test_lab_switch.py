"""Changing labs (field tests): the new lab's instruments opened before the old lab's closed (a pylon camera was
"exclusively opened by another client", an Andor SDK shut down under the new connection); one instrument's stale
limit sent the whole lab to the virtual lab, saying so only in the log; and the first-run switch raced the first
message, so a whole turn ran on the virtual lab."""
import threading
import time

import pytest

from simlab import build_virtual_lab
from labharness.clock import RealClock
from labharness.gate import ScriptedApprover
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.homelab import builtin_registry, open_home_lab

pytest.importorskip("mcp")
from labharness.agent.runtime import LabRuntime  # noqa: E402


class Exclusive:
    """Instruments that take one connection at a time, as a camera's SDK does: a second open of an address fails."""

    def __init__(self):
        self.open = {}
        self.log = []

    def factory(self, name, card, address):
        if address in self.open:
            raise RuntimeError("%s exclusively opened by another client" % address)
        hub = self

        class Backend:
            simulated = False

            def write(self, command):
                pass

            def query(self, command):
                return "0"

            def close(self):
                hub.open.pop(address, None)
                hub.log.append("closed " + address)
        self.open[address] = Backend()
        self.log.append("opened " + address)
        return self.open[address]


def lab_of(hub, instruments, later=False):
    cfg = load_lab_config({"lab": "bench", "instruments": instruments}, builtin_registry())
    return Lab(cfg, builtin_registry(), hub.factory, RealClock(), partial=True, later=later)


def test_a_new_lab_keeps_unchanged_instruments_and_opens_the_rest_after_the_old_lab_closed(tmp_path):
    hub = Exclusive()
    old = lab_of(hub, {"cam": {"card": "virtual/lock-in", "address": "fake://cam1"},
                       "smu": {"card": "virtual/source-meter", "address": "fake://smu"}})
    rt = LabRuntime(old, ScriptedApprover([]), tmp_path / "data")
    cam_link = old.instruments["cam"].backend
    new = lab_of(hub, {"cam": {"card": "virtual/lock-in", "address": "fake://cam1"},                 # unchanged
                       "smu": {"card": "virtual/source-meter", "address": "fake://smu",
                               "connection": {"read_termination": "\r"}},                          # a setting changed
                       "cam2": {"card": "virtual/lock-in", "address": "fake://cam2"}}, later=True)  # opened as the lab is
    rt.switch_lab(new, None, {"kind": "file"}, "human (portal)")
    assert rt.lab is new and new.unavailable == {}                    # nothing "exclusively opened"
    assert new.instruments["cam"].backend is cam_link                 # the camera kept its connection
    opens = [i for i, line in enumerate(hub.log) if line == "opened fake://smu"]
    assert len(opens) == 2 and opens[0] < hub.log.index("closed fake://smu") < opens[1]   # closed, then opened again
    assert hub.log.count("opened fake://cam1") == 1 and "closed fake://cam1" not in hub.log
    [changed] = [e for e in rt.gate.log.events() if e["type"] == "lab_changed"]
    assert changed["kept"] == ["cam"]


def test_one_instruments_bad_limit_sets_it_aside_and_the_rest_of_the_lab_works(tmp_path):
    (tmp_path / "lab.yaml").write_text(
        "lab: bench\ninstruments:\n"
        "  smu: {card: virtual/source-meter, address: 'sim://smu', limits: {V: [-500, 500]}}\n"
        "  lockin: {card: virtual/lock-in, address: 'sim://lockin'}\n", encoding="utf-8")
    lab, _, info = open_home_lab(tmp_path, collection=None)
    assert info["kind"] == "file" and lab.config.name == "bench"     # not the virtual lab
    assert info["set_aside"] == {"smu": "limits.V: [-500, 500] exceeds vendor limits [-210, 210]; for a wider range, "
                                         "make this lab's version of the card virtual/source-meter with the "
                                         "instrument's own limits (Lab, Cards…, Make your version)"}
    assert lab.instruments["lockin"].enabled and not lab.instruments["smu"].enabled
    why = lab.unavailable["smu"]
    assert why.startswith("set aside: its entry in lab.yaml has a problem (limits.V: [-500, 500]")
    with pytest.raises(RuntimeError, match="set aside"):
        lab.reconnect("smu")                                           # only a fixed entry connects it
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    assert "smu: set aside" in rt.lab_status() and "refused" in rt.set("smu", "V", 1)
    lab, _, info = open_home_lab(tmp_path, collection=None, connect=False)   # the app's way: later
    assert lab.unavailable["smu"].startswith("set aside") and "smu" not in lab.connecting


def test_a_problem_outside_the_instruments_entries_leaves_no_instruments_and_says_why(tmp_path):
    (tmp_path / "lab.yaml").write_text(
        "lab: bench\ninstruments:\n  smu: {card: virtual/source-meter, address: 'sim://smu'}\n"
        "interlocks:\n  - {drive: smu.V, requires: 'nothere.T < 5'}\n", encoding="utf-8")
    lab, _, info = open_home_lab(tmp_path, collection=None)
    assert info["kind"] == "none" and info["error"] and not lab.instruments


def test_the_first_run_switch_waits_until_kelvin_is_idle(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    first = rt.lab
    answering = {"now": True}
    new = build_virtual_lab(seed=2)
    worker = threading.Thread(target=lambda: rt.switch_lab(new, None, {"kind": "file"}, "Kelvin auto-setup",
                                                           idle=lambda: not answering["now"]), daemon=True)
    worker.start()
    time.sleep(1.2)
    assert rt.lab is first and worker.is_alive()                      # not under an answer in progress
    answering["now"] = False
    worker.join(5)
    assert rt.lab is new
