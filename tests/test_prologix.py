"""GPIB instruments through a Prologix adapter (prologix.py): addresses, the backend, several instruments sharing one
adapter, the scan behind one, Kelvin finding, trying and adding an instrument behind one, and a lab script that opens
one itself. All with pretend adapters (fake_prologix.py) and pretend instruments; the TCP test serves its adapter on
this computer only."""
import socket
import struct
import threading

import pytest

from fake_prologix import Adapters, FakePrologix, Instrument, card_instrument, says
from labharness import builtin_registry, prologix
from labharness.adapters import (AdapterError, lost_kind, marks_message_end, open_backend, resource_name,
                                 set_resource_manager, where_and_cause)
from labharness.discovery import scan_survey
from test_hardware import Bus, Device

KEITHLEY = b"KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30   Mar 17 2006 09:29:29/A02  /K/J\n"


@pytest.fixture
def adapters(monkeypatch):
    """Pretend adapters in place of real ones, and short waits where nothing answers."""
    fakes = Adapters()
    monkeypatch.setattr(prologix, "OPEN", fakes.open)
    monkeypatch.setattr(prologix, "MARGIN_S", 0.05)
    monkeypatch.setattr(prologix, "SCAN_MS", 30)
    yield fakes
    left = sorted(prologix._adapters)
    prologix._adapters.clear()
    assert not left, "still connected: %s" % left


def test_prologix_addresses():
    at = prologix.where("prologix://192.168.1.50/12")
    assert (at.adapter, at.pad, at.sad) == ("192.168.1.50:1234", 12, None)
    assert prologix.where("prologix://COM5/12/96").sad == 0 and prologix.where("prologix://com5/12/3").sad == 3
    assert prologix.where("prologix://COM5/12/0").addr_command() == b"++addr 12 96"   # 96 to 126, as the adapter wants
    assert prologix.where("prologix:///dev/ttyUSB0/7").adapter == "/dev/ttyUSB0"
    assert resource_name("prologix://com5/24") == "prologix://COM5/24"   # one way to write each
    assert resource_name("prologix://192.168.1.50:1234/24") == resource_name("prologix://192.168.1.50/24") == \
        "prologix://192.168.1.50/24"
    assert resource_name("prologix://192.168.1.50:5000/24") == "prologix://192.168.1.50:5000/24"
    for bad in ("prologix://COM5", "prologix://COM5/31", "prologix://COM5/12/40", "prologix://host:x/3"):
        with pytest.raises(ValueError):
            prologix.where(bad)
    assert prologix.adapter_of("prologix://192.168.1.50") == prologix.adapter_of("192.168.1.50:1234") == \
        prologix.adapter_of("prologix://192.168.1.50/12/0") == "192.168.1.50:1234"
    assert prologix.adapter_of("prologix://COM5/24") == prologix.adapter_of("com5") == "COM5"
    where, cause = where_and_cause("prologix://COM5/24")
    assert where == "on GPIB (address 24) through the Prologix on COM5" and "adapter" in cause
    assert where_and_cause("prologix://192.168.1.50/7")[0] == "on GPIB (address 7) through the Prologix at 192.168.1.50"
    assert marks_message_end("prologix://COM5/24") and not marks_message_end("TCPIP0::10.0.0.5::5025::SOCKET")
    # what would be the adapter's own reaches the instrument as it is
    assert prologix.escape(b"VOLT +1.5\n") == b"VOLT \x1b+1.5\x1b\n"
    assert prologix.escape(b"++addr 7\r") == b"\x1b+\x1b+addr 7\x1b\r"


def test_an_instrument_behind_a_gpib_usb(adapters):
    smu = card_instrument("keithley/2400", name="k2400")
    plain = Instrument()   # takes every command, answers none
    fake = adapters.add("COM5", {24: smu, 7: plain}, version="Prologix GPIB-USB Controller version 6.107")
    backend = open_backend("smu", builtin_registry().get("keithley/2400"), "prologix://com5/24")
    raw = prologix.open_resource("prologix://COM5/7")
    try:
        assert backend.resource == "prologix://COM5/24" and backend.quiet_close
        assert backend.query("*IDN?").startswith("KEITHLEY INSTRUMENTS INC.,MODEL 2400")
        assert smu.heard[-1] == b"*IDN?\n"   # the card's line end, sent with EOI
        # set up once, sparing its EEPROM: saving off before any setting changes
        assert fake.commands[:2] == ["ver", "savecfg 0"] and fake.saved == 0
        assert {"mode 1", "auto 0", "eoi 1", "eos 3", "eot_enable 1", "eot_char 4", "addr 24"} <= set(fake.commands)
        assert fake.links == 1 and fake.open == 1   # both instruments, one connection
        raw.write("SOUR:VOLT +1.5")
        raw.write("++addr 24")   # a command can't address another instrument, or reset the adapter
        raw.write("++rst")
        assert plain.heard == [b"SOUR:VOLT +1.5\n", b"++addr 24\n", b"++rst\n"]
        assert "rst" not in fake.commands and fake.commands.count("addr 24") == 1
        assert backend.query("*IDN?").startswith("KEITHLEY")   # and the SMU is the one asked again
        assert fake.commands[-2:] == ["addr 24", "read eoi"]
        raw.timeout = 100
        with pytest.raises(TimeoutError) as e:   # it says nothing: a timeout, as on VISA
            raw.query("*IDN?")
        assert lost_kind(e.value) == "silent" and "GPIB 7 behind the Prologix on COM5 did not answer" in str(e.value)
        backend.drain()   # a device clear
        assert smu.heard[-1] == b"<device clear>"
    finally:
        raw.close()
        assert fake.open == 1
        backend.close()
    assert fake.open == 0 and fake.overlapped == 0


def test_instruments_behind_one_adapter_take_turns(adapters):
    def counting(name):
        n = [0]

        def answer(data):
            if data.strip() == b"N?":
                n[0] += 1
                return b"%s %d\n" % (name, n[0])
        return Instrument(answer)
    fake = adapters.add("192.168.1.50:1234", {5: counting(b"A"), 7: counting(b"B")})
    a = prologix.open_resource("prologix://192.168.1.50/5")
    b = prologix.open_resource("prologix://192.168.1.50:1234/7")
    got, errors = {"A": [], "B": []}, []

    def ask(res, key):
        try:
            for _ in range(40):
                got[key].append(res.query("N?"))
        except Exception as e:
            errors.append(e)
    try:
        threads = [threading.Thread(target=ask, args=(a, "A")), threading.Thread(target=ask, args=(b, "B"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors, errors
        assert got["A"] == ["A %d" % i for i in range(1, 41)] and got["B"] == ["B %d" % i for i in range(1, 41)]
        assert fake.links == 1 and fake.overlapped == 0
    finally:
        a.close()
        b.close()
    assert fake.open == 0


def test_slow_replies_traces_blocks_and_instruments_without_eoi(adapters, monkeypatch):
    monkeypatch.setattr(prologix, "MAX_TMO_MS", 100)   # the adapter's wait, short: so a slow reply needs asking again
    slow = Instrument(lambda data: b"1\n" if data.strip() == b"*OPC?" else None, delay=0.35)
    old = Instrument(lambda data: b"+1.234E-03\r\n" if data.strip() == b"READ?" else None, eoi=False)
    lines = Instrument(lambda data: b"1,2\n3,4\n5,6\n" if data.strip() == b"TRACE?" else None)
    points = struct.pack("<4h", 1, 4, 1028, -1)   # 1028 is 04 04: the adapter's END byte, inside the block
    block = Instrument(lambda data: b"#18" + points + b"\n" if data.strip() == b"CURV?" else None)
    fake = adapters.add("COM5", {3: slow, 4: old, 5: lines, 6: block})
    res = {n: prologix.open_resource("prologix://COM5/%d" % n) for n in (3, 4, 5, 6)}
    try:
        res[3].timeout = 2000
        assert res[3].query("*OPC?") == "1"   # asked again (++read) until it answered
        assert fake.commands.count("read eoi") >= 3 and fake.overlapped == 0   # never before the adapter gave up
        res[4].read_termination, res[4].timeout = "\r\n", 300
        assert res[4].query("READ?") == "+1.234E-03"   # it sent no EOI: what came was the reply
        assert res[4].query("READ?") == "+1.234E-03" and fake.commands[-1] == "read 10"   # now read to its line end
        assert res[5].query("TRACE?") == "1,2" and res[5].read() == "3,4"   # line by line, as VISA reads
        res[5].read_termination = ""
        assert res[5].query("TRACE?") == "1,2\n3,4\n5,6\n"   # whole, to its end
        pytest.importorskip("pyvisa")
        assert res[6].query_binary_values("CURV?", datatype="h") == [1, 4, 1028, -1]
        assert fake.overlapped == 0
    finally:
        for r in res.values():
            r.close()


def test_a_backend_waits_on_a_slow_instrument_and_a_stop_ends_the_wait(adapters, monkeypatch):
    from labharness.instrument import Stopped
    monkeypatch.setattr(prologix, "MAX_TMO_MS", 100)
    monkeypatch.setattr("labharness.adapters.WAIT_SLICE_MS", 200)   # the wait is read a slice at a time
    vna = Instrument(lambda data: b"1\n" if data.strip() == b"*OPC?" else None, delay=0.6)
    fake = adapters.add("COM5", {16: vna})
    backend = open_backend("vna", builtin_registry().get("keithley/2400"), "prologix://COM5/16")
    try:
        assert backend.query_waiting("*OPC?", 5, lambda: False) == "1"   # a second at a time, as on VISA
        with pytest.raises(Stopped):
            backend.query_waiting("*OPC?", 5, lambda: True)
        assert vna.heard[-1] == b"<device clear>"   # the answer it still owed is dropped
    finally:
        backend.close()
    assert fake.overlapped == 0


def test_a_lost_adapter_is_connected_again_and_a_wrong_one_refused(adapters):
    fake = adapters.add("COM5", {24: says(KEITHLEY)})
    res = prologix.open_resource("prologix://COM5/24")
    try:
        assert res.query("*IDN?").startswith("KEITHLEY")
        fake.refuse = True   # unplugged
        with pytest.raises(ConnectionError) as e:
            res.query("*IDN?")
        assert lost_kind(e.value) == "gone" and "lost the connection to the Prologix on COM5" in str(e.value)
        with pytest.raises(ConnectionError) as e:   # still unplugged: it can't be opened, and says so
            res.query("*IDN?")
        assert lost_kind(e.value) == "gone" and "could not be opened" in str(e.value)
        fake.refuse = False   # plugged in again
        assert res.query("*IDN?").startswith("KEITHLEY") and fake.links == 2
    finally:
        res.close()
    with pytest.raises(AdapterError, match="does not take a connection"):
        prologix.open_resource("prologix://192.168.1.99/5")   # nothing at that address
    adapters.add("COM7", version="ERROR")   # another device on an FTDI port
    with pytest.raises(AdapterError, match=r"did not answer \+\+ver as a Prologix adapter does"):
        prologix.open_resource("prologix://COM7/5")
    assert prologix.version_of("COM7") == "" and adapters.fakes["COM7"].commands == ["ver", "ver"]


def test_a_scan_asks_every_gpib_address_behind_a_gpib_usb(adapters):
    k2400, lockin = card_instrument("keithley/2400", name="k2400"), card_instrument("srs/sr830", name="sr830")
    widget = says(b"ACME,WIDGET 9000,1,1.0\n")
    old = Instrument(lambda data: b"+1.000E+00\n")   # an older instrument: its reading, whatever it is asked
    fake = adapters.add("COM5", {24: k2400, 8: lockin, 12: widget, 3: old},
                        version="Prologix GPIB-USB Controller version 6.107")
    other = adapters.add("COM6", version="ERROR")   # an FTDI port with something else on it
    bus = Bus({"ASRL5::INSTR": Device(silent=True), "GPIB0::9::INSTR": Device("srs/sr830", name="card"),
               "ASRL6::INSTR": Device("lakeshore/336", serial={"baud_rate": 57600, "data_bits": 7, "parity": "odd"},
                                      name="ls")})
    found, looked = scan_survey(builtin_registry(), bus, network=False, prologix_ports=["COM5", "COM6"])
    by = {f.address: f for f in found}
    assert by["prologix://COM5/24"].card_id == "keithley/2400" and by["prologix://COM5/24"].how == "identity reply"
    assert by["prologix://COM5/8"].card_id == "srs/sr830" and by["prologix://COM5/8"].suggest_name() == "lockin"
    assert by["prologix://COM5/12"].card_id is None and "no card matches" in by["prologix://COM5/12"].line()
    assert by["prologix://COM5/3"].idn == "" and "something other than who it is" in by["prologix://COM5/3"].line()
    assert len([a for a in by if a.startswith("prologix://")]) == 4   # nothing at the other 26 addresses
    assert "ASRL5::INSTR" not in by and bus.devices["ASRL5::INSTR"].sent == []   # its port is the adapter's
    assert by["ASRL6::INSTR"].card_id == "lakeshore/336" and other.commands == ["ver"]   # asked as a serial port
    assert by["GPIB0::9::INSTR"].card_id == "srs/sr830"
    for inst in (k2400, lockin, widget, old):
        assert inst.heard == [b"*IDN?\n"]   # who it is, and nothing else
    assert fake.saved == 0 and fake.overlapped == 0 and fake.open == 0
    lines = [line for line in looked.lines() if line.startswith("GPIB adapters")]
    assert lines == ["GPIB adapters: Prologix GPIB-USB Controller version 6.107 on COM5 (GPIB 3, GPIB 8, GPIB 12, "
                     "GPIB 24 answered)"]

    empty = adapters.add("COM8", version="Prologix GPIB-USB Controller version 6.107")
    found, looked = scan_survey(builtin_registry(), Bus({}), network=False, prologix_ports=["COM8"])
    assert not [f for f in found if f.address.startswith("prologix://")] and empty.open == 0
    said = [p for p in looked.problems if "behind the Prologix on COM8" in p]
    assert said and said[0].startswith("Nothing on the GPIB bus behind the Prologix on COM8 answered *IDN?")
    assert "prologix://COM8/<its GPIB address>" in said[0]


LAB = """lab: Bench
instruments:
  lockin:
    card: srs/sr830
    address: "prologix://192.168.1.50/8"
"""


def test_kelvin_finds_tries_and_adds_an_instrument_behind_a_gpib_ethernet(tmp_path, adapters):
    pytest.importorskip("mcp")
    from test_add_instrument import file_runtime
    lockin, smu = card_instrument("srs/sr830", name="sr830"), card_instrument("keithley/2400", name="k2400")
    fake = adapters.add("192.168.1.50:1234", {8: lockin, 24: smu})
    set_resource_manager(Bus({}))   # no instrument on VISA
    rt = None
    try:
        rt = file_runtime(tmp_path, LAB)
        assert rt.lab.read("lockin", "frequency") > 0   # the lab's lock-in, behind the adapter
        found = rt.scan()   # the adapter the lab is on: every other address behind it
        assert "prologix://192.168.1.50/24: Keithley 2400" in found and "card keithley/2400" in found, found
        assert "prologix://192.168.1.50/8: already in this lab" in found
        assert fake.links == 1   # asked through the lab's own connection
        tried = rt.try_card("keithley/2400", "prologix://192.168.1.50/24")
        assert "identity: 'KEITHLEY INSTRUMENTS" in tried and "matches the card" in tried and "ERROR" not in tried, tried
        out = rt.add_instrument("prologix://192.168.1.50/24")
        assert out.startswith("added smu (") and "health: ok" in out, out
        text = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
        assert 'address: "prologix://192.168.1.50/24"' in text and text.count("added_by: kelvin") == 1
        rt.lab.instruments["smu"].write("V", 0.5)
        assert rt.lab.read("smu", "V") == pytest.approx(0.5)
        assert any(h.startswith(b":SOUR") and b"0.5" in h and b"?" not in h for h in smu.heard)
        assert "in the lab already, as smu" in rt.add_instrument("prologix://192.168.1.50:1234/24")
        listed = rt.inspect_address("prologix://192.168.1.50")   # an adapter at a known address
        assert "Prologix GPIB-ETHERNET Controller version 01.06.06.00 at 192.168.1.50" in listed
        assert "prologix://192.168.1.50/24: already in this lab (in this lab as smu)" in listed
        assert fake.links == 1 and fake.saved == 0 and fake.overlapped == 0
    finally:
        if rt is not None:
            rt.lab.close()
        set_resource_manager(None)
    assert fake.open == 0


def test_find_on_network_and_inspect_address_look_behind_a_gpib_ethernet(tmp_path, adapters, monkeypatch):
    """A pretend network, 192.168.77.0/24, with a GPIB-ETHERNET at .60: the sweep finds its port 1234 open, and the
    instruments behind it are asked who they are."""
    pytest.importorskip("mcp")
    from labharness import probe, survey
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover

    class Knock:   # a connection to its port 1234, closed at once: nothing is sent on it
        def settimeout(self, seconds):
            pass

        def close(self):
            pass

    def connect(ip, port, seconds):
        if (ip, port) != ("192.168.77.60", prologix.PORT):
            raise ConnectionRefusedError()
        return Knock()
    monkeypatch.setattr(probe, "_connect", connect)
    monkeypatch.setattr(probe, "network_adapters", lambda: [
        {"name": "Ethernet", "address": "192.168.77.10", "network": "192.168.77.0/24", "link_local": False}])
    monkeypatch.setattr(survey, "network_adverts", lambda *a, **k: ([], []))
    smu, widget = says(KEITHLEY), says(b"ACME,WIDGET 9000,1,1.0\n")
    old = Instrument(lambda data: b"+1.000E+00\n")
    fake = adapters.add("192.168.77.60:1234", {24: smu, 9: widget, 5: old})
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    said = rt.find_on_network()
    there = next(line for line in said.splitlines() if line.startswith("- 192.168.77.60:"))
    assert "port 1234: open" in there and "Prologix GPIB-ETHERNET" in there
    assert "prologix://192.168.77.60/24 answers 'KEITHLEY INSTRUMENTS" in there
    assert "→ card keithley/2400" in there and "add_instrument prologix://192.168.77.60/24" in there
    assert "draft_card prologix://192.168.77.60/9" in there
    assert "prologix://192.168.77.60/5" not in there   # it answered, but not who it is: inspect_address lists it
    assert "prologix://192.168.77.60/24" in rt.last_found
    assert rt.found_info[resource_name("prologix://192.168.77.60/24")]["card_id"] == "keithley/2400"
    assert all(inst.heard == [b"*IDN?\n"] for inst in (smu, widget, old)) and fake.open == 0

    inspected = rt.inspect_address("192.168.77.60")   # its address, from its front panel
    assert "port 1234: open" in inspected and "Prologix GPIB-ETHERNET Controller version 01.06.06.00" in inspected
    assert "- prologix://192.168.77.60/24: Keithley 2400" in inspected
    assert fake.overlapped == 0 and fake.saved == 0 and fake.open == 0


SCRIPT_LAB = """
lab: Prologix scripts
instruments:
  smu:
    card: virtual/source-meter
    address: prologix://COM5/24
    limits: { V: [-10, 10] }
  lockin:
    card: virtual/lock-in
    address: prologix://127.0.0.1/8
"""


def test_a_lab_script_that_opens_the_adapter_itself_is_named_and_asked_about(tmp_path, monkeypatch):
    """run_script: a script that opens the adapter itself, to speak its ++ commands, reaches the instruments behind it
    past the gate. The run's approval says whose adapter it is, and a socket to a GPIB-ETHERNET is asked about (one
    to anything else that is no lab instrument's is let through)."""
    from labharness import scriptrun
    from labharness.clock import SimClock
    from labharness.eventlog import EventLog, RunStore
    from labharness.gate import SafetyGate, ScriptedApprover
    from labharness.lab import Lab
    from labharness.labconfig import load_lab_config
    from labharness.sim.twin import World, sim_backend_factory
    from test_script_runs import FAKE_PYVISA, FAKE_SERIAL, _python, output, write
    fakes = tmp_path / "fakes"
    for name, text in (("pyvisa", FAKE_PYVISA), ("serial", FAKE_SERIAL)):
        (fakes / name).mkdir(parents=True)
        (fakes / name / "__init__.py").write_text(text)
    monkeypatch.setenv("PYTHONPATH", str(fakes))   # a pyvisa and a pyserial that refuse every real instrument
    folder = tmp_path / "scripts"
    folder.mkdir()
    registry = builtin_registry()
    clock = SimClock()
    world = World(clock, 0)
    lab = Lab(load_lab_config(SCRIPT_LAB, registry), registry, sim_backend_factory(world), clock, world)

    write(folder, "by_hand.py", """
        import serial, socket
        gpib = serial.Serial("COM5", 115200, timeout=1)
        gpib.write(b"++addr 24\\n")
        net = socket.create_connection(("127.0.0.1", 1234), timeout=1)
    """)
    gate = SafetyGate(lab, ScriptedApprover([]), EventLog(None, clock))
    request, _, _ = scriptrun.approval(gate, scriptrun.prepare(lab, "by_hand.py", [folder], choose_python=_python))
    said = str(request)
    assert ("It also opens serial://COM5, the Prologix adapter this lab's smu is behind: commands sent through it skip "
            "the safety gate") in said, said
    assert "It also opens TCPIP0::127.0.0.1::1234::SOCKET, the Prologix adapter this lab's lockin is behind" in said

    write(folder, "worked_out.py", """
        import socket
        host = ".".join(["127", "0", "0", "1"])   # worked out as it runs: the reading can't name it
        net = socket.create_connection((host, 1234), timeout=1)
        print("connected past Kelvin")
    """)
    gate = SafetyGate(lab, ScriptedApprover([True, False]), EventLog(None, clock))
    prep = scriptrun.prepare(lab, "worked_out.py", [folder], choose_python=_python)
    request, grants, problems = scriptrun.approval(gate, prep)
    assert gate.ask(request) and not problems
    report = scriptrun.ScriptRun(gate, prep, RunStore(tmp_path / "runs"), grants, set()).execute()
    asked = str(gate.approver.requests[-1])
    assert "Let worked_out.py open TCPIP0::127.0.0.1::1234::SOCKET?" in asked
    assert "the Prologix adapter this lab's lockin is behind" in asked
    assert report.status == "aborted", report.message
    assert "declined: worked_out.py may not open TCPIP0::127.0.0.1::1234::SOCKET" in report.message
    assert "connected past Kelvin" not in output(report)


def test_a_gpib_ethernet_over_tcp():
    """Kelvin's own TCP connection (prologix._Socket) to a pretend adapter served on 127.0.0.1."""
    fake = FakePrologix({24: says(KEITHLEY)})
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()

    def serve():
        conn, _ = server.accept()
        conn.settimeout(0.005)
        with conn:
            while not stop.is_set():
                try:
                    data = conn.recv(4096)
                    if not data:
                        return
                    fake.feed(data)
                except socket.timeout:
                    pass
                out = fake.take(0)
                if out:
                    conn.sendall(out)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        res = prologix.open_resource("prologix://127.0.0.1:%d/24" % server.getsockname()[1])
        try:
            assert res.query("*IDN?") == KEITHLEY.decode("ascii").strip()
            assert fake.commands[:2] == ["ver", "savecfg 0"] and fake.commands[-3:] == ["addr 24", "read_tmo_ms 3000",
                                                                                       "read eoi"]
        finally:
            res.close()
    finally:
        stop.set()
        server.close()
        thread.join(2)
    assert not prologix._adapters


def test_a_gpib_usb_through_pyserial(monkeypatch):
    """Kelvin's own serial connection (prologix._Serial), with pyserial's port standing in for the adapter's."""
    serial = pytest.importorskip("serial")
    fake = FakePrologix({24: says(KEITHLEY)}, version="Prologix GPIB-USB Controller version 6.107")
    opened = []

    class Port:
        def __init__(self, port, baudrate, timeout=None, write_timeout=None):
            opened.append(port)
            self.timeout, self.buf, self.closed = timeout, b"", False

        def write(self, data):
            fake.feed(data)
            return len(data)

        def flush(self):
            pass

        def read(self, n=1):
            if not self.buf:
                self.buf = fake.take(self.timeout or 0)
            out, self.buf = self.buf[:n], self.buf[n:]
            return out

        @property
        def in_waiting(self):
            self.buf += fake.take(0)
            return len(self.buf)

        def reset_input_buffer(self):
            fake.take(0, drop=True)
            self.buf = b""

        def close(self):
            self.closed = True
    monkeypatch.setattr(serial, "Serial", Port)
    res = prologix.open_resource("prologix://COM5/24")
    try:
        assert res.query("*IDN?") == KEITHLEY.decode("ascii").strip() and opened == ["COM5"]
    finally:
        res.close()
    assert not prologix._adapters
