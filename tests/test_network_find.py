"""Finding instruments on the lab's network whose address nobody knows: those that announce themselves, and every
address of this computer's networks with a port open that instruments or their gateways use, each asked who it is.
A link-local network, too wide to sweep, through this computer's neighbour table and GigE Vision's discovery instead.
Only looking: one connection per port, *IDN? only where instruments speak SCPI, the lab's own instruments left alone.
Nothing here touches a real network: a port on this computer, a pretend network, a pretend cable and neighbour table
(no broadcast is sent, and this computer's own neighbour table is never read)."""
import ipaddress
import json
import socket
import threading

import pytest

from labharness import probe
from simlab import build_virtual_lab

NET = "192.168.77.0/24"
OWN = "192.168.77.10"
SR830 = b"Stanford_Research_Systems,SR830,s/n86813,ver1.07\n"
LINK = "169.254.0.0/16"
OWN_LINK = "169.254.10.20"   # this computer's Ethernet 2: a cable straight to the instruments, nobody's DHCP on it


def ack(maker, model, serial, ip, mac, name="", info=""):
    """A GigE Vision camera's DISCOVERY_ACK as the standard lays it out: a header, then its bootstrap registers."""
    body = bytearray(0xF8)
    body[0:4] = b"\x00\x02\x00\x00"                      # GigE Vision 2.0
    body[0x0A:0x10] = bytes(int(x, 16) for x in mac.split(":"))
    body[0x24:0x28] = socket.inet_aton(ip)
    body[0x34:0x38] = socket.inet_aton("255.255.0.0")
    for at, text in ((0x48, maker), (0x68, model), (0x88, "1.0.3"), (0xA8, info), (0xD8, serial), (0xE8, name)):
        body[at:at + len(text)] = text.encode()
    return b"\x00\x00\x00\x03\x00\xf8\x00\x01" + bytes(body)   # status success, DISCOVERY_ACK, 248 bytes, id 1


PYLON = ack("Princeton Instruments", "PyLoN-IR 1024-1.7", "X045418024", "169.254.12.34", "00:50:c2:12:34:56")
AWAY = ack("Acme Vision", "AV-500", "AV123", "192.168.1.50", "00:0f:31:00:00:01", name="left")   # another network's
# Windows' arp -a -N 169.254.10.20: the lab's lock-in at .6.6, an SR830 at .7.7, something quiet at .9.9, the PyLoN-IR
ARP = """
Interface: 169.254.10.20 --- 0x7
  Internet Address      Physical Address      Type
  169.254.6.6           00-11-22-33-44-66     dynamic
  169.254.7.7           00-11-22-33-44-55     dynamic
  169.254.9.9           00-aa-bb-cc-dd-ee     dynamic
  169.254.12.34         00-50-c2-12-34-56     dynamic
  169.254.255.255       ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
  239.255.255.250       01-00-5e-7f-ff-fa     static
  255.255.255.255       ff-ff-ff-ff-ff-ff     static
"""


class Cable:
    """A UDP socket on a pretend cable: what Kelvin sends, and the answers that come back (then nothing more)."""

    def __init__(self, answers):
        self.answers, self.sent, self.bound, self.options, self.closed = list(answers), [], None, [], False

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def bind(self, address):
        self.bound = address

    def sendto(self, data, address):
        self.sent.append((data, address))

    def settimeout(self, seconds):
        pass

    def recvfrom(self, n):
        if not self.answers:
            raise socket.timeout()
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.closed = True


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
    assert ("169.254.0.0/16 (Direct cable) is a link-local network, too wide to sweep: looked there through this "
            "computer's neighbour table, GigE Vision's discovery broadcast and mDNS instead") in said
    assert "140.247.0.0/22 (Building) is a public network" in said and "Tailscale" not in said

    assert probe.sweep_networks("192.168.5.*", []) == ([ipaddress.ip_network("192.168.5.0/24")], [])
    assert probe.sweep_networks("192.168.5.7", [])[0] == [ipaddress.ip_network("192.168.5.0/24")]
    assert probe.sweep_networks("10.1.4.0/22", [])[0] == [ipaddress.ip_network("10.1.4.0/22")]
    for bad, why in (("10.0.0.0/16", "at most"), ("8.8.8.0/24", "not a local network"),
                     ("127.0.0.0/24", "not a local network"), ("the lab", "not a network"),
                     ("169.254.0.0/16", "or name none: this computer's link-local networks are looked at")):
        with pytest.raises(ValueError, match=why):
            probe.sweep_networks(bad, [])
    with pytest.raises(ValueError) as wide:
        probe.sweep_networks("10.0.0.0/16", [])
    assert "link-local" not in str(wide.value)


def test_gige_cameras_answer_one_discovery_broadcast_sent_from_the_adapters_own_address():
    cable = Cable([(PYLON, ("169.254.12.34", 3956)),
                   (probe.GVCP_DISCOVERY, ("169.254.1.1", 3956)),   # another computer asking: not an answer
                   ConnectionResetError(),                          # Windows: an ICMP for something sent earlier
                   (PYLON, ("169.254.12.34", 3956)),                # its broadcast answer too: still one camera
                   (AWAY, ("192.168.1.50", 3956))])
    cameras = probe.gige_cameras(OWN_LINK, seconds=5, udp=lambda: cable)
    assert probe.GVCP_DISCOVERY == bytes.fromhex("4211000200000001") and probe.GVCP_PORT == 3956
    assert cable.sent == [(probe.GVCP_DISCOVERY, ("255.255.255.255", 3956))]   # one datagram: who is there
    assert cable.bound == (OWN_LINK, 0) and (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in cable.options
    assert cable.closed
    assert [(c["maker"], c["model"], c["serial"], c["ip"], c["mac"], c["name"], c["version"]) for c in cameras] == [
        ("Princeton Instruments", "PyLoN-IR 1024-1.7", "X045418024", "169.254.12.34", "00:50:c2:12:34:56", "", "1.0.3"),
        ("Acme Vision", "AV-500", "AV123", "192.168.1.50", "00:0f:31:00:00:01", "left", "1.0.3")]
    assert cameras[0]["mask"] == "255.255.0.0"
    assert probe.gige_answer(b"\x00\x00\x00\x03\x00\xf8\x00\x01") is None   # too short to say anything
    assert probe.gige_answer(PYLON[:4].replace(b"\x00\x03", b"\x00\x05") + PYLON[4:]) is None   # another command's


def test_the_neighbour_table_on_one_adapter_in_any_language(monkeypatch):
    adapter = {"name": "Ethernet 2", "address": OWN_LINK, "network": LINK, "link_local": True}
    asked = []

    def windows(command):
        asked.append(command)
        return ARP
    monkeypatch.setattr(probe, "WIN", True)
    assert probe.neighbours(adapter, windows) == {
        "169.254.6.6": "00:11:22:33:44:66", "169.254.7.7": "00:11:22:33:44:55", "169.254.9.9": "00:aa:bb:cc:dd:ee",
        "169.254.12.34": "00:50:c2:12:34:56"}
    assert asked == [["arp", "-a", "-N", OWN_LINK]]   # that adapter's entries only: Windows picks them by its address
    german = ("Schnittstelle: 169.254.10.20 --- 0x7\n  Internetadresse  Physische Adresse  Typ\n"
              "  169.254.9.9      00-aa-bb-cc-dd-ee  dynamisch\n  169.254.255.255  ff-ff-ff-ff-ff-ff  statisch\n")
    assert probe.neighbour_table(german, LINK, OWN_LINK) == {"169.254.9.9": "00:aa:bb:cc:dd:ee"}

    monkeypatch.setattr(probe, "WIN", False)
    linux = ("169.254.7.7 lladdr 00:11:22:33:44:55 REACHABLE\n169.254.8.8  FAILED\n169.254.1.1  INCOMPLETE\n"
             "169.254.10.20 lladdr 00:00:5e:00:01:01 PERMANENT\n192.168.1.1 lladdr 00:aa:00:aa:00:aa STALE\n")
    asked.clear()
    eth = {"name": "eth1", "address": OWN_LINK, "network": LINK, "link_local": True}
    assert probe.neighbours(eth, lambda command: asked.append(command) or linux) == {"169.254.7.7": "00:11:22:33:44:55"}
    assert asked == [["ip", "-4", "neigh", "show", "dev", "eth1"]]

    mac = ("? (169.254.7.7) at 0:11:22:33:44:55 on en5 [ethernet]\n"
           "? (169.254.1.1) at (incomplete) on en5 [ethernet]\n"
           "? (169.254.12.34) at 0:50:c2:12:34:56 on en6 [ethernet]\n"
           "? (192.168.1.1) at 0:aa:0:aa:0:aa on en0 ifscope [ethernet]\n")

    def macos(command):   # no ip command: arp's whole table, this adapter's lines
        asked.append(command)
        if command[0] == "ip":
            raise FileNotFoundError(command[0])
        return mac
    asked.clear()
    en5 = {"name": "en5", "address": OWN_LINK, "network": LINK, "link_local": True}
    assert probe.neighbours(en5, macos) == {"169.254.7.7": "00:11:22:33:44:55"}
    assert asked[-1] == ["arp", "-an"]


def test_a_link_local_network_is_looked_at_without_sweeping_it(monkeypatch):
    adapters = [{"name": "Wi-Fi", "address": OWN, "network": NET, "link_local": False},
                {"name": "Ethernet 2", "address": OWN_LINK, "network": LINK, "link_local": True}]
    cable = Cable([(PYLON, ("169.254.12.34", 3956)), (AWAY, ("192.168.1.50", 3956))])
    monkeypatch.setattr(probe, "WIN", True)
    seen = probe.look_on_link_local(adapters, skip={"169.254.6.6"}, udp=lambda: cable, run=lambda command: ARP)
    assert [a["name"] for a in seen["looked"]] == ["Ethernet 2"] and cable.bound == (OWN_LINK, 0)
    assert list(seen["hosts"]) == ["169.254.12.34", "169.254.7.7", "169.254.9.9"]   # the lab's own .6.6 left out
    assert seen["hosts"]["169.254.9.9"] == "in this computer's neighbour table on Ethernet 2 (MAC 00:aa:bb:cc:dd:ee)"
    assert [(c["model"], c["adapter"], c["network"]) for c in seen["cameras"]] == [
        ("PyLoN-IR 1024-1.7", "Ethernet 2", LINK), ("AV-500", "Ethernet 2", LINK)]
    assert "192.168.1.50" not in seen["hosts"] and not seen["problems"]   # on another network: not looked at

    def no_socket():
        raise OSError("the requested address is not valid in its context")

    def no_arp(command):
        raise FileNotFoundError(command[0])
    seen = probe.look_on_link_local(adapters, udp=no_socket, run=no_arp)
    assert seen["hosts"] == {} and seen["problems"] == [
        "GigE Vision's discovery could not be sent on Ethernet 2 (the requested address is not valid in its context)",
        "This computer's neighbour table for Ethernet 2 could not be read (arp)"]
    assert probe.look_on_link_local(adapters[:1], udp=no_socket, run=no_arp)["looked"] == []   # no link-local here


def test_what_a_gige_camera_is_and_what_kelvin_reaches_it_through():
    pylon = dict(probe.gige_answer(PYLON, "169.254.12.34"), adapter="Ethernet 2", network=LINK)
    said = probe.gige_line(pylon)
    assert said.startswith("GigE Vision camera Princeton Instruments PyLoN-IR 1024-1.7, serial X045418024 "
                           "(MAC 00:50:c2:12:34:56) → a Teledyne Princeton Instruments camera: PICam finds it itself, "
                           "so scan_instruments lists it (picam://<serial>), not a network search")
    # a GigE engine that names itself rather than the camera's maker: the camera is told by its model
    engine = probe.gige_answer(ack("Pleora Technologies Inc.", "ProEM-HS 1024BX3", "P123", "169.254.1.2",
                                   "00:11:1c:00:00:02"))
    assert "Teledyne Princeton Instruments camera: PICam" in probe.gige_line(engine)
    bare = probe.gige_line(probe.gige_answer(ack("Pleora Technologies Inc.", "iPORT NTx-Mini", "P124", "169.254.1.3",
                                                 "00:11:1c:00:00:03")))   # an engine that names neither
    assert "a Teledyne Princeton Instruments camera, if it is one" in bare and "another camera: genicam://P124" in bare
    away = dict(probe.gige_answer(AWAY, "192.168.1.50"), adapter="Ethernet 2", network=LINK)
    said = probe.gige_line(away)
    assert said.startswith("GigE Vision camera Acme Vision AV-500, serial AV123, named 'left' (MAC 00:0f:31:00:00:01) "
                           "at 192.168.1.50, on Ethernet 2's cable but not on its network 169.254.0.0/16: give the "
                           "camera an address there")
    assert said.endswith("→ genicam://AV123 (card generic/genicam-camera), through Harvesters and the GenTL producer "
                         "(.cti) its maker's SDK installs")
    basler = probe.gige_line(probe.gige_answer(ack("Basler", "blaze-101", "40012345", "169.254.5.5",
                                                   "00:30:53:00:00:01")))   # a time-of-flight camera of Basler's
    assert "pylon://40012345, through Basler's pypylon" in basler and "Princeton" not in basler


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
        if self.port == 9090 and data.startswith(b"{"):   # an attoDRY's eNSPIRE controller says what it is
            ask = json.loads(data)
            assert ask["method"] == "com.attocube.cryostat.interface.system.getDeviceType" and ask["api"] == 2
            self.pending = (json.dumps({"jsonrpc": "2.0", "id": ask["id"], "result": [0, "attoDRY2100XL"]})
                            + "\n").encode()

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
    .31, a chiller's Modbus at .40, an attoDRY's eNSPIRE controller at .45, a Moxa serial server at .55, and the
    lab's own lock-in at .12."""
    pytest.importorskip("mcp")
    from labharness import discovery, survey
    open_ports = {("192.168.77.20", 5025), ("192.168.77.31", 4880), ("192.168.77.40", 502), ("192.168.77.45", 9090),
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
    monkeypatch.setattr(probe, "_udp", lambda: pytest.fail("a GigE Vision broadcast with no link-local network"))
    monkeypatch.setattr(probe, "_run_text", lambda command: pytest.fail("this computer's neighbour table was read"))
    return touched


@pytest.fixture
def cable(network, monkeypatch):
    """The pretend network, and this computer's Ethernet 2 (169.254.10.20, link-local) on a cable to the lab's lock-in
    (169.254.6.6), an SR830 that fell back to 169.254.7.7, something at .9.9 that answers no port instruments use, and
    a PyLoN-IR (GigE, at .12.34); another camera there, whose own address (192.168.1.50) is on another network."""
    swept = probe._connect

    def connect(ip, port, seconds):
        if not ip.startswith("192.168.77."):
            network.append((ip, port))
            if (ip, port) != ("169.254.7.7", 5025):
                raise ConnectionRefusedError()
            return Pretend(ip, port)
        return swept(ip, port, seconds)
    udp = Cable([(PYLON, ("169.254.12.34", 3956)), (AWAY, ("192.168.1.50", 3956))])
    asked = []
    monkeypatch.setattr(probe, "_connect", connect)
    monkeypatch.setattr(probe, "_udp", lambda: udp)
    monkeypatch.setattr(probe, "_run_text", lambda command: asked.append(command) or ARP)
    monkeypatch.setattr(probe, "WIN", True)
    monkeypatch.setattr(probe, "network_adapters", lambda: [
        {"name": "Ethernet", "address": OWN, "network": NET, "link_local": False},
        {"name": "Ethernet 2", "address": OWN_LINK, "network": LINK, "link_local": True}])
    return udp, asked


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
    assert lines[0].endswith("and for instruments that announce themselves: 5 addresses answered")
    srs = next(ln for ln in lines if ln.startswith("- 192.168.77.20:"))
    assert "answers 'Stanford_Research_Systems,SR830,s/n86813,ver1.07' → card srs/sr830" in srs
    assert "add_instrument TCPIP0::192.168.77.20::5025::SOCKET" in srs
    gen = next(ln for ln in lines if ln.startswith("- 192.168.77.31:"))
    assert "TCPIP0::192.168.77.31::hislip0::INSTR answers 'Keysight Technologies,33622A" in gen
    assert "draft_card TCPIP0::192.168.77.31::hislip0::INSTR" in gen
    assert "Modbus TCP" in next(ln for ln in lines if ln.startswith("- 192.168.77.40:"))
    assert "serial device server (Moxa NPort)" in next(ln for ln in lines if ln.startswith("- 192.168.77.55:"))
    cryo = next(ln for ln in lines if ln.startswith("- 192.168.77.45:"))
    assert "jsonrpc://192.168.77.45:9090 answers 'attocube,attoDRY2100XL' → card attocube/attodry-enspire" in cryo
    assert rt.found_info["jsonrpc://192.168.77.45:9090"]["card_id"] == "attocube/attodry-enspire"
    assert "In the lab already, left alone: lockin (192.168.77.12)" in lines and "smu" not in said

    assert not [t for t in network if t[0] in ("192.168.77.12", OWN)]   # the lab's own and this computer: untouched
    assert {("TCPIP0::192.168.77.31::hislip0::INSTR", "visa")} == {t for t in network if t[1] == "visa"}
    for address in ("TCPIP0::192.168.77.20::5025::SOCKET", "TCPIP0::192.168.77.31::hislip0::INSTR",
                    "modbus://192.168.77.40?unit=1", "TCPIP0::192.168.77.55::4001::SOCKET"):
        assert address in rt.last_found                  # try_card and add_instrument may use them without asking
    assert rt.found_info[resource_name("TCPIP0::192.168.77.20::5025::SOCKET")]["card_id"] == "srs/sr830"
    assert "looked for instruments on the network (%s): found TCPIP0::192.168.77.20::5025::SOCKET" % NET \
        in rt.events(5, "network")


def test_kelvin_finds_what_is_on_a_link_local_cable_without_sweeping_it(cable, network, tmp_path):
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    udp, asked = cable
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.lab.config.instruments["lockin"].address = "TCPIP0::169.254.6.6::5025::SOCKET"
    said = rt.find_on_network()
    lines = said.splitlines()
    assert lines[0].startswith("Looked on %s (ports 111 502 1234" % NET)
    assert lines[0].endswith(", the link-local 169.254.0.0/16 (Ethernet 2) without sweeping and for instruments that "
                             "announce themselves: 9 addresses answered")
    assert udp.sent == [(probe.GVCP_DISCOVERY, ("255.255.255.255", 3956))] and udp.bound == (OWN_LINK, 0)
    assert asked == [["arp", "-a", "-N", OWN_LINK]]

    camera = next(ln for ln in lines if ln.startswith("- 169.254.12.34:"))
    assert camera == ("- 169.254.12.34: GigE Vision camera Princeton Instruments PyLoN-IR 1024-1.7, serial X045418024 "
                      "(MAC 00:50:c2:12:34:56) → a Teledyne Princeton Instruments camera: PICam finds it itself, so "
                      "scan_instruments lists it (picam://<serial>), not a network search")
    srs = next(ln for ln in lines if ln.startswith("- 169.254.7.7:"))
    assert "answers 'Stanford_Research_Systems,SR830,s/n86813,ver1.07' → card srs/sr830" in srs
    assert "add_instrument TCPIP0::169.254.7.7::5025::SOCKET" in srs
    assert "TCPIP0::169.254.7.7::5025::SOCKET" in rt.last_found
    assert "- 169.254.9.9: in this computer's neighbour table on Ethernet 2 (MAC 00:aa:bb:cc:dd:ee); answers none of " \
           "the ports instruments use" in lines
    away = next(ln for ln in lines if ln.startswith("- GigE Vision camera Acme Vision AV-500"))
    assert "at 192.168.1.50, on Ethernet 2's cable but not on its network 169.254.0.0/16" in away
    assert "In the lab already, left alone: lockin (169.254.6.6)" in lines
    assert any(ln.startswith("169.254.0.0/16 (Ethernet 2) is a link-local network, too wide to sweep") for ln in lines)

    looked = {ip for ip, port in network if port != "visa" and not ip.startswith("192.168.77.")}
    assert looked == {"169.254.7.7", "169.254.9.9", "169.254.12.34"}   # every port of each; nothing else out there
    assert {port for ip, port in network if ip == "169.254.9.9"} == {p for p, _, _ in probe.PORTS}
    assert not [address for address in rt.last_found if "picam" in address or "192.168.1.50" in address]
    assert "169.254.0.0/16" in rt.events(5, "network")

    udp.sent.clear()
    asked.clear()
    rt.find_on_network("192.168.77.16/28")   # a range named: that range only
    assert not udp.sent and not asked


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
