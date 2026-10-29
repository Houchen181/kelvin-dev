"""Finding instruments on the lab's network whose address nobody knows: those that announce themselves, and every
address of this computer's networks with a port open that instruments or their gateways use, each asked who it is.
Only looking: one connection per port, *IDN? only where instruments speak SCPI, the lab's own instruments left alone.
Nothing here touches a real network: a port on this computer, or a pretend network."""
import ipaddress
import socket
import threading

import pytest

from labharness import probe
from simlab import build_virtual_lab

NET = "192.168.77.0/24"
OWN = "192.168.77.10"
SR830 = b"Stanford_Research_Systems,SR830,s/n86813,ver1.07\n"


def test_a_sweep_knocks_once_on_each_port_and_sends_nothing():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    heard = []

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.settimeout(1)
            try:
                heard.append(conn.recv(100))
            except OSError:
                heard.append(None)
            conn.close()
    threading.Thread(target=serve, daemon=True).start()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    gone = closed.getsockname()[1]
    closed.close()
    try:
        found = probe.sweep([ipaddress.ip_network("127.0.0.0/30")], ports=[port, gone], seconds=1)
        assert found == {"127.0.0.1": [port]}
        assert probe.sweep([ipaddress.ip_network("127.0.0.0/30")], ports=[port], skip={"127.0.0.1"}) == {}
    finally:
        server.close()
    assert heard and all(not h for h in heard)   # connected and closed: nothing was sent


def test_which_networks_a_sweep_looks_at():
    adapters = [
        {"name": "Ethernet", "address": "192.168.1.10", "network": "192.168.1.0/24", "link_local": False},
        {"name": "Campus", "address": "10.20.30.40", "network": "10.20.0.0/16", "link_local": False},
        {"name": "Direct cable", "address": "169.254.3.4", "network": "169.254.0.0/16", "link_local": True},
        {"name": "Tailscale", "address": "100.101.102.103", "network": "100.64.0.0/10", "link_local": False},
        {"name": "Building", "address": "140.247.1.2", "network": "140.247.0.0/22", "link_local": False},
        {"name": "Point to point", "address": "192.168.9.1", "network": "192.168.9.1/32", "link_local": False},
    ]
    nets, notes = probe.sweep_networks("", adapters)
    assert nets == [ipaddress.ip_network("192.168.1.0/24"), ipaddress.ip_network("10.20.30.0/24")]
    said = " ".join(notes)
    assert "10.20.0.0/16 (Campus) has 65536 addresses: looked only at this computer's part of it, 10.20.30.0/24" in said
    assert "169.254.0.0/16 (Direct cable) is a link-local network" in said
    assert "140.247.0.0/22 (Building) is a public network" in said and "Tailscale" not in said

    assert probe.sweep_networks("192.168.5.*", []) == ([ipaddress.ip_network("192.168.5.0/24")], [])
    assert probe.sweep_networks("192.168.5.7", [])[0] == [ipaddress.ip_network("192.168.5.0/24")]
    assert probe.sweep_networks("10.1.4.0/22", [])[0] == [ipaddress.ip_network("10.1.4.0/22")]
    for bad, why in (("10.0.0.0/16", "at most"), ("8.8.8.0/24", "not a local network"),
                     ("127.0.0.0/24", "not a local network"), ("the lab", "not a network")):
        with pytest.raises(ValueError, match=why):
            probe.sweep_networks(bad, [])


class Pretend:
    """A connection to an address on the pretend network: what an instrument there answers."""

    def __init__(self, ip, port):
        self.ip, self.port, self.sent, self.pending = ip, port, [], b""

    def settimeout(self, seconds):
        pass

    def sendall(self, data):
        self.sent.append(data)
        if self.port == 5025 and data.startswith(b"*IDN?"):
            self.pending = SR830

    def recv(self, n):
        if not self.pending:
            raise socket.timeout()
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def close(self):
        pass


@pytest.fixture
def network(monkeypatch):
    """192.168.77.0/24: an SR830 on a SCPI socket at .20, a Keysight generator that announces itself (HiSLIP) at
    .31, a chiller's Modbus at .40, a Moxa serial server at .55, and the lab's own lock-in at .12."""
    pytest.importorskip("mcp")
    from labharness import discovery, survey
    open_ports = {("192.168.77.20", 5025), ("192.168.77.31", 4880), ("192.168.77.40", 502),
                  ("192.168.77.55", 4001), ("192.168.77.12", 5025), (OWN, 5025)}
    touched = []

    def connect(ip, port, seconds):
        touched.append((ip, port))
        if (ip, port) not in open_ports:
            raise ConnectionRefusedError()
        return Pretend(ip, port)

    def identify(address, registry, rm=None, timeout_ms=1000, usb=None, queries=None, catalog=None):
        touched.append((address, "visa"))
        idn = "Keysight Technologies,33622A,MY12345678,A.02.03" if "192.168.77.31" in address else ""
        return discovery.Found(address, idn, None, "", "", "identity reply")

    monkeypatch.setattr(probe, "_connect", connect)
    monkeypatch.setattr(probe, "network_adapters", lambda: [
        {"name": "Ethernet", "address": OWN, "network": NET, "link_local": False}])
    monkeypatch.setattr(survey, "network_adverts", lambda *a, **k: (
        [("TCPIP0::192.168.77.31::hislip0::INSTR", "network (mDNS)")], []))
    monkeypatch.setattr(discovery, "identify", identify)
    return touched


def test_kelvin_finds_instruments_on_the_network(network, tmp_path):
    from labharness.adapters import resource_name
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.lab.config.instruments["lockin"].address = "TCPIP0::192.168.77.12::5025::SOCKET"
    rt.lab.config.instruments["smu"].address = "GPIB0::24::INSTR"         # not on the network
    said = rt.find_on_network()
    lines = said.splitlines()
    assert lines[0].startswith("Looked on %s (ports 111 502 1234" % NET)
    assert lines[0].endswith("and for instruments that announce themselves: 4 addresses answered")
    srs = next(ln for ln in lines if ln.startswith("- 192.168.77.20:"))
    assert "answers 'Stanford_Research_Systems,SR830,s/n86813,ver1.07' → card srs/sr830" in srs
    assert "add_instrument TCPIP0::192.168.77.20::5025::SOCKET" in srs
    gen = next(ln for ln in lines if ln.startswith("- 192.168.77.31:"))
    assert "TCPIP0::192.168.77.31::hislip0::INSTR answers 'Keysight Technologies,33622A" in gen
    assert "draft_card TCPIP0::192.168.77.31::hislip0::INSTR" in gen
    assert "Modbus TCP" in next(ln for ln in lines if ln.startswith("- 192.168.77.40:"))
    assert "serial device server (Moxa NPort)" in next(ln for ln in lines if ln.startswith("- 192.168.77.55:"))
    assert "In the lab already, left alone: lockin (192.168.77.12)" in lines and "smu" not in said

    assert not [t for t in network if t[0] in ("192.168.77.12", OWN)]   # the lab's own and this computer: untouched
    assert {("TCPIP0::192.168.77.31::hislip0::INSTR", "visa")} == {t for t in network if t[1] == "visa"}
    for address in ("TCPIP0::192.168.77.20::5025::SOCKET", "TCPIP0::192.168.77.31::hislip0::INSTR",
                    "modbus://192.168.77.40?unit=1", "TCPIP0::192.168.77.55::4001::SOCKET"):
        assert address in rt.last_found                  # try_card and add_instrument may use them without asking
    assert rt.found_info[resource_name("TCPIP0::192.168.77.20::5025::SOCKET")]["card_id"] == "srs/sr830"
    assert "looked for instruments on the network (%s): found TCPIP0::192.168.77.20::5025::SOCKET" % NET \
        in rt.events(5, "network")


def test_a_range_the_person_names_and_one_kelvin_refuses(network, tmp_path):
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    assert "not a local network" in rt.find_on_network("8.8.8.0/24") and not network
    said = rt.find_on_network("192.168.77.16/28")      # .17 to .30: only the SR830 is there
    assert said.splitlines()[0] == "Looked on 192.168.77.16/28 (ports %s): 1 address answered" % " ".join(
        map(str, probe.SWEEP_PORTS))
    assert not [t for t in network if t[1] == "visa"]   # a named range: no announcements asked for


def test_working_a_number_out_and_reading_the_log_about_one_thing(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    said = rt.calculate("2*muB*0.35/h")
    assert said.startswith("2*muB*0.35/h = ") and float(said.split("= ")[1]) == pytest.approx(9.7974e9, rel=1e-4)
    assert rt.calculate("h*c/(1.55e-6*qe)").endswith("= 0.7998980544")   # 1239.84198 eV nm / 1550 nm
    assert rt.calculate("sqrt(-1)").endswith("= nan")
    assert rt.calculate("Tc + 1").startswith("not calculated: 'Tc' is not a known constant")
    assert rt.calculate("__import__('os')").startswith("not calculated")
    rt.set("smu", "V", 0.5)
    rt.gate.log.write("note", by="agent", text="unrelated")
    about = rt.events(20, "smu")
    assert about and all("smu" in line for line in about.splitlines())
    assert rt.events(20, "no such thing").startswith("nothing about 'no such thing'")
