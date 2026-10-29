"""What a scan looks through, and why Kelvin says it found nothing: VISA, the local network, USB, serial, GPIB.
Network and USB lookups are replaced by fakes here, so the tests send nothing anywhere."""
import logging
import sys
import types

import pytest

from labharness import builtin_registry, survey as sv
from labharness.adapters import set_resource_manager
from labharness.discovery import scan_survey
from test_hardware import Bus, Device


def test_the_same_instrument_is_asked_once():
    s = sv.Survey()
    s.add("USB0::0x2A8D::0x0101::MY123::INSTR", "VISA")
    s.add("USB0::10893::257::MY123::INSTR", "network?")            # decimal IDs: the same device
    s.add("TCPIP0::10.0.0.7::inst0::INSTR", "VISA")
    s.add("TCPIP0::10.0.0.7::hislip0::INSTR", "network (mDNS)")      # another protocol, the same host
    s.add("TCPIP0::10.0.0.8::5025::SOCKET", "network (mDNS)")
    assert s.resources == ["USB0::0x2A8D::0x0101::MY123::INSTR", "TCPIP0::10.0.0.7::inst0::INSTR",
                           "TCPIP0::10.0.0.8::5025::SOCKET"]


def test_a_scan_looks_everywhere_and_says_what_it_saw():
    bus = Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k"),
               "TCPIP0::10.0.0.7::inst0::INSTR": Device("keysight/34465a", name="dmm")})
    keysight = {"vid": 0x2A8D, "pid": 0x0101, "maker": "Keysight", "usbtmc": True, "visible": False}
    found, s = scan_survey(builtin_registry(), bus, network=True,
                           mdns=lambda: [("TCPIP0::10.0.0.7::inst0::INSTR", "network (mDNS)")],
                           usb_list=lambda: [dict(keysight)], ports=lambda: ["COM3 (USB Serial Port)"])
    assert sorted(f.card_id for f in found) == ["keithley/2400", "keysight/34465a"]
    assert s.via["TCPIP0::10.0.0.7::inst0::INSTR"] == "VISA"            # listed once, though mDNS saw it too
    lines = s.lines()
    assert lines[0].startswith("VISA: Bus; 2 instruments")
    assert "Network: searched the local network" in lines[1] and "Serial ports: COM3 (USB Serial Port)" in lines
    assert "USB instruments: Keysight 2a8d:0101 (not openable)" in lines
    assert any("Keysight" in p and "2a8d:0101" in p for p in s.problems)   # what to install, said once
    assert sv.reason(found, s).startswith("Found Keithley 2400-series")


def test_why_nothing_was_found(monkeypatch):
    empty = Bus({})
    found, s = scan_survey(builtin_registry(), empty, network=True, mdns=lambda: [], usb_list=lambda: [],
                           ports=lambda: [])
    why = sv.reason(found, s)
    assert why.startswith("No instruments are connected to this computer: VISA lists none, nothing answered on the "
                          "local network, there are no serial ports, no USB instruments are plugged in")
    assert "Run Kelvin on the computer the instruments are plugged into" in why

    monkeypatch.setattr(sys, "platform", "win32")
    found, s = scan_survey(builtin_registry(), empty, network=False, ports=lambda: [], usb_list=lambda: [
        {"vid": 0x0957, "pid": 0x1A07, "maker": "Keysight (Agilent)", "usbtmc": True, "visible": False}])
    assert "Windows has no driver" in sv.reason(found, s) and "Keysight IO Libraries" in sv.reason(found, s)
    found, s = scan_survey(builtin_registry(), empty, network=False, ports=lambda: [], usb_list=lambda: [
        {"vid": 0x3923, "pid": 0x709B, "maker": "National Instruments", "usbtmc": False, "visible": False}])
    assert "NI-488.2" in sv.reason(found, s)

    widget = Bus({"GPIB0::5::INSTR": Device(idn="ACME,WIDGET 9000,1,1.0"), "ASRL9::INSTR": Device(silent=True)})
    found, s = scan_survey(builtin_registry(), widget, network=False, ports=lambda: [], usb_list=lambda: [])
    assert "1 instrument answered, but Kelvin has no card for it" in sv.reason(found, s)
    found, s = scan_survey(builtin_registry(), Bus({"ASRL9::INSTR": Device(silent=True)}), network=False,
                           ports=lambda: ["COM9"], usb_list=lambda: [])
    assert sv.reason(found, s).startswith("1 port or address did not answer (ASRL9::INSTR)")
    assert sv.reason([], None) == "Kelvin has not looked for instruments yet"


def test_network_instruments_from_their_mdns_adverts(monkeypatch):
    adverts = {   # (type, name): (addresses, port)
        ("_vxi-11._tcp.local.", "DMM._vxi-11._tcp.local."): (["10.0.0.5"], 111),
        ("_scpi-raw._tcp.local.", "DMM._scpi-raw._tcp.local."): (["10.0.0.5"], 5025),   # same host: VXI-11 wins
        ("_hislip._tcp.local.", "SMU._hislip._tcp.local."): (["10.0.0.6", "fe80::1"], 4880),
        ("_scpi-raw._tcp.local.", "PSU._scpi-raw._tcp.local."): (["10.0.0.8"], 5025),
        ("_hislip._tcp.local.", "VNA._hislip._tcp.local."): (["10.0.0.9"], 4881),
    }

    class Info:
        def __init__(self, addresses, port):
            self.addresses, self.port = addresses, port

        def parsed_addresses(self):
            return list(self.addresses)

    class Zeroconf:
        closed = False

        def __init__(self, interfaces=None):
            pass

        def get_service_info(self, service_type, name, timeout=0):
            return Info(*adverts[(service_type, name)])

        def close(self):
            Zeroconf.closed = True

    class StateChange:
        Added = object()

    def browser(zc, service_types, handlers):
        for (t, n) in adverts:
            if t in service_types:
                for handler in handlers:
                    handler(zeroconf=zc, service_type=t, name=n, state_change=StateChange.Added)

    fake = types.SimpleNamespace(Zeroconf=Zeroconf, ServiceBrowser=browser, ServiceStateChange=StateChange,
                                 InterfaceChoice=types.SimpleNamespace(All="all"))
    monkeypatch.setitem(sys.modules, "zeroconf", fake)
    assert sv.mdns_instruments(seconds=0) == [
        ("TCPIP0::10.0.0.5::inst0::INSTR", "network (mDNS)"),
        ("TCPIP0::10.0.0.6::hislip0::INSTR", "network (mDNS)"),
        ("TCPIP0::10.0.0.8::5025::SOCKET", "network (mDNS)"),
        ("TCPIP0::10.0.0.9::hislip0,4881::INSTR", "network (mDNS)"),
    ]
    assert Zeroconf.closed


def test_the_start_up_scan_says_why_and_logs_it(tmp_path, caplog):
    pytest.importorskip("mcp")
    from labharness.autosetup import AutoSetup
    from test_autosetup import FakeSettings, runtime_for
    set_resource_manager(Bus({}))
    try:
        rt = runtime_for(tmp_path)
        rt.scan_network = False
        with caplog.at_level(logging.INFO, logger="kelvin.setup"):
            status = AutoSetup(rt, FakeSettings(), tmp_path).run()
        assert status["message"].startswith("No instruments are connected to this computer")
        assert any(line.startswith("VISA: Bus") for line in status["lines"])
        assert rt.last_survey["reason"] == status["message"] and rt.lab_info["kind"] == "none"
        logged = caplog.text
        assert "Instrument scan (start)" in logged and "VISA: Bus" in logged and "Instrument setup: No instruments" in logged

        off = AutoSetup(rt, FakeSettings(auto_setup="off"), tmp_path, scan=lambda: pytest.fail("switched off")).run()
        assert "switched off in Settings" in off["message"]
    finally:
        set_resource_manager(None)


def test_the_portal_shows_why(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from test_autosetup import runtime_for
    from test_web import Scripted
    from labharness.web.app import STATIC, create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from starlette.testclient import TestClient
    set_resource_manager(Bus({}))
    try:
        rt = runtime_for(tmp_path)
        settings = Settings(tmp_path / "settings.json")
        settings.update({"scan_network": "off"})
        app = create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"],
                         providers={"scripted": Scripted([])})
        headers = {"X-Kelvin-Token": settings.access_key}
        with TestClient(app) as client:
            assert rt.scan_network is False                                   # the Settings switch
            r = client.post("/api/instruments/scan", json={}, headers=headers).json()
            assert r["found"] == [] and r["survey"]["reason"].startswith("No instruments are connected")
            state = client.get("/api/state", headers=headers).json()
            assert state["simulated"] and state["survey"]["lines"][0].startswith("VISA: Bus")
            shown = client.post("/api/settings", json={"scan_network": "on"}, headers=headers).json()["settings"]
            assert shown["scan_network"] and rt.scan_network is True
    finally:
        set_resource_manager(None)
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="lab-why"' in page and 'id="scan-network"' in page and 'id="lab-survey-why"' in page
