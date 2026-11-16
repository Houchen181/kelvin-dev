"""Every way a script spells or reaches a lab instrument is the lab instrument (review of 0.3.45, section 8).

A USB instrument's default interface written out (::0::INSTR) and TCPIP:: without its board number read as "not in
this lab", so a person's yes let the script open its own session to a lab instrument, past the safety gate. A raw
socket to a LAN instrument's other port (5025 beside its VXI-11 address) went through without asking anyone."""
import types

import pytest

from labharness import scriptrun
from labharness.clock import SimClock
from labharness.homelab import builtin_registry
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory

LAB = """
lab: Address lab
instruments:
  smu:
    card: virtual/source-meter
    address: GPIB0::24::INSTR
  dmm:
    card: virtual/source-meter
    address: USB0::0x0957::0x1796::MY123::INSTR
  vna:
    card: virtual/lock-in
    address: TCPIP0::10.0.0.5::hislip0::INSTR
  laser:
    card: virtual/laser
    address: TCPIP0::10.0.0.9::4001::SOCKET
"""


@pytest.fixture
def lab():
    registry = builtin_registry()
    clock = SimClock()
    world = World(clock, 0)
    return Lab(load_lab_config(LAB, registry), registry, sim_backend_factory(world), clock, world)


@pytest.mark.parametrize("lab_address, script_address", [
    ("USB0::0x0957::0x1796::MY123::INSTR", "USB0::0x0957::0x1796::MY123::0::INSTR"),   # its default interface
    ("USB0::0x0957::0x1796::MY123::INSTR", "USB::2391::6038::MY123::0::INSTR"),
    ("TCPIP0::10.0.0.5::hislip0::INSTR", "TCPIP::10.0.0.5::hislip0::INSTR"),            # board 0 left out
    ("TCPIP0::10.0.0.5::hislip0::INSTR", "tcpip::10.0.0.5::HISLIP0::instr"),
    ("TCPIP0::10.0.0.5::inst0::INSTR", "TCPIP::10.0.0.5::INSTR"),
    ("GPIB0::24::INSTR", "GPIB::24"),
])
def test_two_spellings_of_one_address_are_one(lab_address, script_address):
    assert scriptrun.canonical(lab_address) == scriptrun.canonical(script_address)


@pytest.mark.parametrize("a, b", [
    ("GPIB0::24::INSTR", "GPIB0::24::0::INSTR"),                                        # a secondary address
    ("USB0::0x0957::0x1796::MY123::INSTR", "USB0::0x0957::0x1796::MY123::1::INSTR"),    # another interface
    ("TCPIP0::10.0.0.9::4001::SOCKET", "TCPIP0::10.0.0.9::4002::SOCKET"),
])
def test_other_ways_in_are_other_resources(a, b):
    assert scriptrun.canonical(a) != scriptrun.canonical(b)


def test_the_lab_instrument_is_found_however_it_is_spelled(lab):
    assert scriptrun.lab_name(lab, "USB0::0x0957::0x1796::MY123::0::INSTR") == "dmm"
    assert scriptrun.lab_name(lab, "TCPIP::10.0.0.5::hislip0::INSTR") == "vna"


@pytest.mark.parametrize("address, name", [
    ("TCPIP0::10.0.0.5::5025::SOCKET", "vna"),            # the analyzer's raw port beside its HiSLIP
    ("TCPIP0::10.0.0.5::inst0::INSTR", "vna"),            # its VXI-11
    ("GPIB0::24::0::INSTR", "smu"),                       # a secondary address of the source-meter
    ("USB0::0x0957::0x1796::MY123::1::INSTR", "dmm"),     # another USB interface of it
    ("TCPIP0::10.0.0.9::4002::SOCKET", None),             # another port of the laser's terminal server: another one
    ("TCPIP0::10.0.0.77::5025::SOCKET", None),            # not this lab's
    ("GPIB0::25::INSTR", None),
    ("TCPIP0::10.0.0.5::hislip0::INSTR", None),           # the lab's own way in: lab_name's, not this
])
def test_another_way_into_a_lab_instrument_is_known(lab, address, name):
    assert scriptrun.same_device(lab, address) == name


def opener(lab, answers, auto=False):
    """A run's own end of opening an address (ScriptRun._open), in a run a person approved, or auto mode (auto)."""
    asked = []

    def ask(request):
        asked.append(request)
        return answers.pop(0)
    return types.SimpleNamespace(lab=lab, rehearsal=False, allowed=set(), prep=types.SimpleNamespace(name="sweep.py"),
                                 opened={}, passed=[], _ask=ask, auto=auto, allowed_ports=set()), asked


@pytest.mark.parametrize("answers", [[True], [False]])
def test_another_way_into_a_lab_instrument_is_refused_without_asking(lab, answers):
    """Decided 2026-11-15, after a review: a person's yes to another way into a lab instrument was asked for, then
    overridden by the run's own guard. It is refused in every run, nobody asked, and its card says so up front."""
    me, asked = opener(lab, list(answers))
    for address, kind in (("TCPIP0::10.0.0.5::5025::SOCKET", "socket"), ("GPIB0::24::0::INSTR", "visa")):
        with pytest.raises(scriptrun._Refuse, match="refused: sweep.py opens %s, which reaches this lab's" % address) \
                as e:
            scriptrun.ScriptRun._open(me, address, kind)
        assert "skip the safety gate, so Kelvin refuses it as the script opens it" in str(e.value) and e.value.stop
    assert asked == [] and me.passed == []


def test_a_socket_to_no_lab_instrument_needs_no_one(lab):
    me, asked = opener(lab, [])
    assert scriptrun.ScriptRun._open(me, "TCPIP0::10.0.0.77::5025::SOCKET", "socket") == {} and asked == []
    assert scriptrun.ScriptRun._open(me, "TCPIP0::10.0.0.9::4002::SOCKET", "socket") == {} and asked == []


@pytest.mark.parametrize("address", ["(1, 2, 3)", "TCPIP0::[::1::5025::SOCKET", "TCPIP0::host::SOCKET"])
def test_a_socket_address_kelvin_cant_read_asks_a_person(lab, address):
    """An address Kelvin can't read is never taken for one that reaches no lab instrument (a review of 2026-11-15:
    ::ffff:127.0.0.1 reached the lab's lock-in, read as outside the lab); an IPv6 one, in brackets, it reads."""
    me, asked = opener(lab, [False])
    with pytest.raises(scriptrun._Refuse, match="declined: sweep.py may not open"):
        scriptrun.ScriptRun._open(me, address, "socket")
    assert len(asked) == 1


def test_an_ipv6_address_is_read_and_its_lab_instrument_known(lab):
    me, asked = opener(lab, [])
    assert scriptrun.ScriptRun._open(me, "TCPIP0::[2001:db8::7]::5025::SOCKET", "socket") == {} and asked == []
    assert scriptrun._same_host("[::1]", "::1") and scriptrun._lan("TCPIP0::[::1]::5025::SOCKET") == ("[::1]",
                                                                                                    "5025::SOCKET")


def test_in_a_run_auto_mode_approved_an_address_outside_the_lab_ends_the_run(lab):
    """Nobody's yes covers it: auto mode covers only what the gate checks, so the run ends there, as at its fence
    (2026-11-15), and nobody is asked mid-run."""
    me, asked = opener(lab, [], auto=True)
    for address, kind in (("TCPIP0::10.0.0.77::5025::SOCKET", "socket"), ("COM11", "serial")):
        with pytest.raises(scriptrun._Refuse, match="auto mode's fence refused opening %s, outside the lab" % address):
            scriptrun.ScriptRun._open(me, address, kind)
    assert asked == [] and me.passed == []


def test_a_lab_instrument_spelled_another_way_goes_through_kelvin(lab):
    me, asked = opener(lab, [])
    assert scriptrun.ScriptRun._open(me, "USB0::0x0957::0x1796::MY123::0::INSTR", "visa") == {"instrument": "dmm"}
    assert scriptrun.ScriptRun._open(me, "TCPIP::10.0.0.5::hislip0::INSTR", "visa") == {"instrument": "vna"}
    assert asked == []


def test_what_a_person_is_told_of_another_way_in(lab):
    said = scriptrun.outside(lab, "GPIB0::24::0::INSTR")
    assert said == ("GPIB0::24::0::INSTR, which reaches this lab's smu another way than its address in the lab "
                    "(GPIB0::24::INSTR): commands sent there skip the safety gate, so Kelvin refuses it as the script "
                    "opens it, and the run ends there")
    assert "not in this lab" in scriptrun.outside(lab, "GPIB0::25::INSTR")
