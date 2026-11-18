"""USB IDs (VID:PID) name an instrument that doesn't say who it is (2026-11-17): a maker's USB device driven through its
SDK, a USB-serial instrument with a protocol of its own, a USBTMC instrument that ignores *IDN?.

Kelvin's cards (identify: usb), the instrument collection's entries (usb) and a table of instrument makers' vendor IDs
(usbids.py) name it, in the scan and in connected_devices. A generic USB-serial chip's ID (an FTDI's, a CH340's) names
nothing, a card that lists one included: Kelvin says "a USB-serial adapter". For a device nothing names,
connected_devices says how to look it up. Offline: pyserial's port list, probe.usb_devices, libusb and the
collection's index are fakes here."""
import dataclasses
import sys
import types

import pytest

from labharness import builtin_registry, portholders, probe, scriptrun, survey as sv, usbids
from labharness.clock import SimClock
from labharness.collection import Collection
from labharness.discovery import CANDIDATES, by_usb, identify, scan_survey, usb_words
from labharness.gate import ScriptedApprover
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory
from test_hardware import Bus, Device

PM100D_MANUAL = "https://www.thorlabs.example/manuals/pm100d.pdf"


def entry(iid, maker, model, interfaces, usb=(), covers=(), card=False, manual=""):
    """One instrument of the collection's index.json, as kic index writes it."""
    return {"id": iid, "maker": maker, "model": model, "name": "%s %s" % (maker.split(" (")[0], model),
            "covers": list(covers), "kind": "power-meter", "interfaces": list(interfaces), "identify": None,
            "usb": list(usb), "card": {"path": "instruments/x/%s/card.yaml" % iid.replace("/", "-"),
                                       "sha256": "0" * 64, "status": "community"} if card else None,
            "manuals": [{"title": "Manual", "url": manual, "access": "public"}] if manual else []}


INDEX = {"schema": 1, "instruments": [
    entry("thorlabs/pm100", "Thorlabs", "PM100", ["usb-tmc"], usb=["1313:8078", "1313:8072"], covers=["PM100USB"]),
    entry("thorlabs/pm100d", "Thorlabs", "PM100D", ["usb-tmc"], usb=["1313:8078"], manual=PM100D_MANUAL),
    entry("thorlabs/kdc101", "Thorlabs", "KDC101", ["usb"]),
    entry("thorlabs/fw102c", "Thorlabs", "FW102C", ["usb", "rs-232"]),
    entry("thorlabs/mff101", "Thorlabs", "MFF101", ["usb"]),
    entry("thorlabs/tc300", "Thorlabs", "TC300", ["usb"], card=True),
    entry("thorlabs/bc106", "Thorlabs", "BC106", ["lan"]),                           # no USB: never a candidate
    entry("keithley/2450", "Keithley (Tektronix)", "2450", ["usb-tmc", "gpib", "lan"]),
    entry("tektronix/mso5", "Tektronix", "MSO5", ["usb-tmc", "lan"]),
    entry("acme/bridge-box", "ACME", "Bridge box", ["usb"], usb=["0403:6001"]),      # an FTDI chip's own ID
]}


@pytest.fixture
def catalog(tmp_path):
    """The instrument collection's copy on this computer, as far as naming reads it: its index."""
    c = Collection(tmp_path)
    c.index = lambda: INDEX
    return c


class Port:
    """One port as pyserial's list_ports.comports() gives it."""

    def __init__(self, device, description, vid=None, pid=None, manufacturer=None, serial_number=None):
        self.device, self.description, self.vid, self.pid = device, description, vid, pid
        self.manufacturer, self.serial_number, self.product, self.interface = manufacturer, serial_number, None, None
        self.hwid = "USB VID:PID=%04X:%04X" % (vid, pid) if vid is not None else "ACPI\\PNP0501\\1"


@pytest.fixture
def ports(monkeypatch):
    """pyserial's list of this computer's ports (the tests' Python has no pyserial): the ports the test puts in it."""
    listed = []
    serial = types.ModuleType("serial")
    tools = types.ModuleType("serial.tools")
    list_ports = types.ModuleType("serial.tools.list_ports")
    list_ports.comports = lambda include_links=False: list(listed)
    serial.tools, tools.list_ports = tools, list_ports
    for name, module in (("serial", serial), ("serial.tools", tools), ("serial.tools.list_ports", list_ports)):
        monkeypatch.setitem(sys.modules, name, module)
    return listed


def with_usb(registry, card_id, ids):
    """The registry with a copy of one of its cards that lists these USB IDs (identify: usb), as a lab's own may."""
    registry.put(dataclasses.replace(registry.get(card_id), usb_ids=list(ids)))
    return registry


# ------------------------------------------------------------------ usbids: the chips, the makers, VISA's names

def test_a_generic_chips_id_names_nothing_but_a_makers_own_id_on_one_does():
    for ids in ("0403:6001", "0403:6010", "0403:6011", "0403:6014", "0403:6015", "067b:2303", "10c4:ea60",
                "10c4:ea70", "1a86:7523", "1a86:55d4"):
        assert usbids.is_generic(ids), ids
    for written in ("0403:6001", "0403:6001".upper(), "0x0403:0x6001", "USB\\VID_0403&PID_6001\\A600XYZ",
                    (0x0403, 0x6001)):
        assert usbids.is_generic(written) and usbids.normal(written) == "0403:6001", written
    assert usbids.chip("1a86:7523") == "WCH CH340" and usbids.chip("1313:8078") == ""
    assert not any(usbids.is_generic(x) for x in (None, "", "nonsense", "0403:faf0", "1313:8078", (1, 2, 3)))
    # FTDI's vendor ID with a maker's own product ID: Thorlabs' APT motor controllers (the kernel's FTDI_THORLABS_PID)
    assert usbids.maker("0403:faf0") == "Thorlabs" and usbids.maker_words("0403:FAF0") == "USB 0403:faf0: Thorlabs"
    assert usbids.maker("0403:6001") == "" and usbids.maker_words("0403:6001") == ""
    # the tables agree: a chip maker's vendor ID is no instrument maker's, and each of the makers' own IDs names one
    assert not {int(ids[:4], 16) for ids in usbids.GENERIC} & set(usbids.VENDORS)
    assert all(usbids.normal(ids) == ids for ids in list(usbids.GENERIC) + list(usbids.OWN_PRODUCTS))
    assert all(not usbids.is_generic(ids) and vid in usbids.VENDORS for ids, vid in usbids.OWN_PRODUCTS.items())


def test_the_vendor_table_names_the_maker_and_only_its_own_instruments():
    assert usbids.vendor(0x1313) == usbids.vendor("1313") == usbids.vendor("0x1313") == "Thorlabs"
    assert usbids.vendor("1313:8078") == "Thorlabs" and usbids.vendor(0x2A8D) == "Keysight"
    assert usbids.vendor(0x0403) == "" and usbids.vendor(0xFFFF) == "" and usbids.vendor("nonsense") == ""
    assert usbids.maker_words("1313:8078") == "USB vendor 1313: Thorlabs" and usbids.maker_words("1234:5678") == ""
    assert usbids.made_by("05e6:2450", "Keithley (Tektronix)")                 # Keithley's own vendor ID
    assert not usbids.made_by("0699:0400", "Keithley (Tektronix)")             # the owner in brackets is not the maker
    assert usbids.made_by("0957:1796", "HP / Agilent") and usbids.made_by("2a8d:0101", "Keysight")
    assert usbids.made_by("3923:7166", "NI (National Instruments)")
    assert not usbids.made_by("1a72:1008", "APE Angewandte Physik und Elektronik")   # no "Physik Instrumente"
    assert usbids.made_by("0403:faf0", "Thorlabs") and not usbids.made_by("0403:6001", "Thorlabs")
    assert usbids.gpib_adapter("3923:709b") == "NI GPIB-USB-HS" and usbids.gpib_adapter("3923:7166") == ""


def test_a_visa_usb_resource_carries_its_usb_id():
    assert usbids.from_visa("USB0::0x0957::0x1796::MY123::INSTR") == "0957:1796"
    assert usbids.from_visa("USB0::2391::6038::MY123::INSTR") == "0957:1796"        # VISA's decimal IDs
    assert usbids.from_visa("USB::0957::1796::MY123::INSTR") == "0957:1796"         # hex without its 0x
    assert usbids.from_visa("usb1::0x1313::0x8078::P0012345::0::INSTR") == "1313:8078"
    for other in ("GPIB0::5::INSTR", "ASRL3::INSTR", "TCPIP0::10.0.0.7::inst0::INSTR", "", None, "USB0::x::y::z"):
        assert usbids.from_visa(other) is None, other
    assert usbids.search_text("1313:80f0") == "USB VID_1313&PID_80F0" and usbids.search_text("nonsense") == ""


# ------------------------------------------------------------------ the collection: entries by USB ID, by maker

def test_the_collection_finds_its_entries_by_usb_id_the_most_specific_first(catalog):
    assert [e["id"] for e in catalog.instruments_for_usb("1313:8078")] == ["thorlabs/pm100d", "thorlabs/pm100"]
    assert [e["id"] for e in catalog.instruments_for_usb("1313:8078".upper())] == ["thorlabs/pm100d", "thorlabs/pm100"]
    assert [e["id"] for e in catalog.instruments_for_usb("1313:8072")] == ["thorlabs/pm100"]
    assert catalog.instruments_for_usb("0403:6001") == []      # listed, but a generic chip's ID names nothing
    assert catalog.instruments_for_usb("1313:80f0") == [] and catalog.instruments_for_usb("nonsense") == []
    assert Collection(catalog.dir.parent).instruments_for_usb("1313:8078") == []   # no copy yet: nothing


def test_the_collection_lists_a_makers_usb_instruments_as_candidates(catalog):
    thorlabs = [e["id"] for e in catalog.instruments_from_maker("1313:80f0")]
    assert thorlabs == ["thorlabs/tc300", "thorlabs/fw102c", "thorlabs/kdc101", "thorlabs/mff101", "thorlabs/pm100",
                        "thorlabs/pm100d"]                     # a card first; BC106 talks over the network only
    assert [e["id"] for e in catalog.instruments_from_maker("0403:faf0")] == thorlabs   # Thorlabs' ID on an FTDI chip
    assert [e["id"] for e in catalog.instruments_from_maker("0699:0400")] == ["tektronix/mso5"]   # never Keithley's
    assert [e["id"] for e in catalog.instruments_from_maker("05e6:2450")] == ["keithley/2450"]
    assert catalog.instruments_from_maker("0403:6001") == [] and catalog.instruments_from_maker("1234:5678") == []


# ------------------------------------------------------------------ the scan: a device that doesn't answer

def test_a_silent_serial_instrument_is_named_as_the_collection_knows_its_usb_id(catalog):
    bus = Bus({"ASRL5::INSTR": Device(silent=True)})
    found = identify("ASRL5::INSTR", builtin_registry(), bus, usb={"ASRL5::INSTR": "1313:8078"}, catalog=catalog)
    assert found.card_id is None and found.collection == "thorlabs/pm100d" and found.model == "Thorlabs PM100D"
    assert found.kind == "power-meter" and found.manuals == [PM100D_MANUAL] and found.usb == "1313:8078"
    assert found.how == "no card yet; the instrument collection knows its USB ID 1313:8078 as thorlabs/pm100d"
    line = found.line()
    assert line.startswith("ASRL5::INSTR: Thorlabs PM100D (thorlabs/pm100d in the instrument collection, by its USB ID "
                           "1313:8078) didn't say who it is") and PM100D_MANUAL in line, line
    assert found.suggest_name() == "pm"
    plain = identify("ASRL5::INSTR", builtin_registry(), bus, usb={"ASRL5::INSTR": "1313:8078"})   # collection off
    assert plain.collection == "" and plain.how.endswith("; USB vendor 1313: Thorlabs"), plain.how


def test_a_usbtmc_instrument_that_ignores_idn_is_named_from_its_visa_name(catalog):
    for address in ("USB0::0x1313::0x8078::P0012345::INSTR", "USB0::4883::32888::P0012345::INSTR"):
        bus = Bus({address: Device(silent=True)})
        found = identify(address, builtin_registry(), bus, usb={}, catalog=catalog)
        assert found.collection == "thorlabs/pm100d" and found.usb == "1313:8078", (address, found.how)
        assert bus.devices[address].sent == []                 # it was only asked who it is, and said nothing
    keysight = Bus({"USB0::0x2A8D::0x0101::MY123::INSTR": Device(silent=True)})
    found = identify("USB0::0x2A8D::0x0101::MY123::INSTR", builtin_registry(), keysight, usb={}, catalog=catalog)
    assert found.collection == "" and "; USB vendor 2a8d: Keysight" in found.how, found.how


def test_kelvins_card_names_it_by_usb_id_before_the_collection(catalog):
    registry = with_usb(builtin_registry(), "thorlabs/pm100", ["1313:8078"])
    bus = Bus({"USB0::0x1313::0x8078::P0012345::INSTR": Device(silent=True)})
    found = identify("USB0::0x1313::0x8078::P0012345::INSTR", registry, bus, usb={}, catalog=catalog)
    assert found.card_id == "thorlabs/pm100" and found.how == "USB ID 1313:8078" and found.collection == ""


def test_a_generic_chips_id_never_names_it_a_card_that_lists_it_included(catalog):
    registry = with_usb(builtin_registry(), "thorlabs/pm100", ["0403:6001"])
    bus = Bus({"ASRL5::INSTR": Device(silent=True)})
    found = identify("ASRL5::INSTR", registry, bus, usb={"ASRL5::INSTR": "0403:6001"}, catalog=catalog)
    assert found.card_id is None and found.collection == "" and found.usb == "0403:6001"
    assert found.how.startswith("no answer (it may be write-only, use other settings, or be switched off); a USB-serial "
                                "adapter (FTDI FT232R, USB 0403:6001), whose ID is the chip's own and names no "
                                "instrument"), found.how
    assert "inspect_serial" in found.how and found.how in found.line()


def test_a_device_named_only_by_its_maker_gets_the_makers_instruments_as_candidates(catalog):
    bus = Bus({"ASRL6::INSTR": Device(silent=True)})
    found = identify("ASRL6::INSTR", builtin_registry(), bus, usb={"ASRL6::INSTR": "1313:80f0"}, catalog=catalog)
    assert found.card_id is None and found.collection == ""
    assert found.how.startswith("no answer (it may be write-only, use other settings, or be switched off); USB vendor "
                                "1313: Thorlabs; it may be one of the instrument collection's Thorlabs instruments "
                                "that talk over USB: thorlabs/tc300 (Thorlabs TC300), thorlabs/fw102c"), found.how
    assert found.how.endswith(" and 1 more (find_card Thorlabs)") and "bc106" not in found.how
    assert found.how.count("thorlabs/") == CANDIDATES          # five said by name, the sixth counted
    tek = Bus({"ASRL7::INSTR": Device(silent=True)})
    said = identify("ASRL7::INSTR", builtin_registry(), tek, usb={"ASRL7::INSTR": "0699:0400"}, catalog=catalog).how
    assert "USB vendor 0699: Tektronix" in said and "tektronix/mso5" in said and "keithley" not in said, said
    unknown = identify("ASRL6::INSTR", builtin_registry(), bus, usb={"ASRL6::INSTR": "1234:5678"}, catalog=catalog)
    assert unknown.how == "no answer (it may be write-only, use other settings, or be switched off); USB 1234:5678"


class Quiet:
    """Serial ports nothing answers on; each one opened is kept, with the framing it was set to."""

    def __init__(self):
        self.opened = []

    def open_resource(self, name):
        def silent(*args, **kwargs):
            raise TimeoutError("VI_ERROR_TMO")
        port = types.SimpleNamespace(name=name, read=silent, query=silent, write=silent, close=lambda: None)
        self.opened.append(port)
        return port


def test_a_ch340_board_takes_neither_framing_nor_name_from_a_card_for_the_chip(catalog):
    """A CH340 resets as its port opens, so it is asked once (discovery._ask_board), at a card's framing for its USB
    ID: a card for the chip's own ID (a Lake Shore 336's, 57600 7O1, here) is no card for whatever sits behind it."""
    registry = with_usb(builtin_registry(), "lakeshore/336", ["1a86:7523"])
    quiet = Quiet()
    found = identify("ASRL4::INSTR", registry, quiet, usb={"ASRL4::INSTR": "1a86:7523"}, catalog=catalog)
    assert len(quiet.opened) == 1 and quiet.opened[0].baud_rate == 9600   # a sketch's usual 9600 8N1, opened once
    assert found.card_id is None and found.collection == "" and found.usb == "1a86:7523"
    assert found.how == ("an Arduino-type board or a USB-serial adapter (WCH CH340, USB 1a86:7523: the chip's own ID, "
                         "which names no instrument): it printed nothing as it started and didn't answer *IDN?; give "
                         "Kelvin its commands to make a card"), found.how


def test_a_scan_names_what_it_can_by_usb_id(catalog, ports):
    """The whole scan, with pyserial's ports: an FTDI cable, a Thorlabs device on a COM port of its own, and a
    USBTMC power meter that ignores *IDN?."""
    ports.append(Port("COM5", "USB Serial Port (COM5)", 0x0403, 0x6001, "FTDI"))
    ports.append(Port("COM6", "Thorlabs TSP01 (COM6)", 0x1313, 0x80F0, "Thorlabs"))
    bus = Bus({"ASRL5::INSTR": Device(silent=True), "ASRL6::INSTR": Device(silent=True),
               "USB0::0x1313::0x8078::P0012345::INSTR": Device(silent=True)})
    found, looked = scan_survey(builtin_registry(), bus, network=False, catalog=catalog, usb_list=lambda: [],
                                ports=lambda: [])
    by = {f.address: f for f in found}
    assert "a USB-serial adapter (FTDI FT232R, USB 0403:6001)" in by["ASRL5::INSTR"].line()
    assert "USB vendor 1313: Thorlabs; it may be one of" in by["ASRL6::INSTR"].line()
    assert by["USB0::0x1313::0x8078::P0012345::INSTR"].collection == "thorlabs/pm100d"
    assert all(f.card_id is None for f in found)


def test_the_survey_names_a_usb_devices_maker_from_the_vendor_table(monkeypatch):
    class UsbDevice:   # what pyusb finds: its IDs, and its configurations' interfaces
        def __init__(self, vid, pid, usbtmc=False):
            self.idVendor, self.idProduct = vid, pid
            self.configs = [[types.SimpleNamespace(bInterfaceClass=0xFE if usbtmc else 0xFF,
                                                   bInterfaceSubClass=3 if usbtmc else 0)]]

        def __iter__(self):
            return iter(self.configs)
    plugged = [UsbDevice(0x1313, 0x80F0), UsbDevice(0x0E0D, 0x0003), UsbDevice(0x0403, 0x6001),
               UsbDevice(0x1234, 0x5678, usbtmc=True), UsbDevice(0x1234, 0x0001)]
    core = types.ModuleType("usb.core")
    core.find = lambda find_all=True: list(plugged)
    usb = types.ModuleType("usb")
    usb.core = core
    monkeypatch.setitem(sys.modules, "usb", usb)
    monkeypatch.setitem(sys.modules, "usb.core", core)
    monkeypatch.setattr(sv, "use_bundled_libusb", lambda: True)
    devices = sv.usb_devices()
    assert [(d["vid"], d["maker"], d["usbtmc"]) for d in devices] == [
        (0x1313, "Thorlabs", False), (0x0E0D, "PicoQuant", False), (0x1234, "", True)]   # no FTDI cable, no mouse
    assert "USB instruments: Thorlabs 1313:80f0 (not openable), PicoQuant 0e0d:0003 (not openable), device " \
           "1234:5678 (not openable)" in sv.Survey(usb=devices).lines()


# ------------------------------------------------------------------ connected_devices and a script's ports

LAB = """
lab: USB lab
instruments:
  pm:
    card: virtual/lock-in
    address: USB0::0x1313::0x8078::P0099999::INSTR
  laser:
    card: virtual/laser
    address: serial://COM7
    limits: { power: [0, 50] }
"""


def device(vid, pid, names, maker="", drivers=("usbser",), serial=""):
    """One USB device as probe.usb_devices lists it."""
    return {"vid": vid, "pid": pid, "serial": serial, "names": list(names), "maker": maker, "drivers": list(drivers),
            "classes": [], "problem": 0}


def serial_port(port, vid, pid, serial=""):
    return {"port": port, "description": "USB Serial Port (%s)" % port, "vid": vid, "pid": pid, "serial": serial,
            "maker": "", "product": "", "interface": ""}


def test_connected_devices_says_what_each_usb_id_names(catalog, tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    registry = builtin_registry()
    clock = SimClock()
    world = World(clock, 0)
    lab = Lab(load_lab_config(LAB, registry), registry, sim_backend_factory(world), clock, world)
    monkeypatch.setattr(probe, "usb_devices", lambda: [
        device(0x1313, 0x8078, ["PM100D"], "Thorlabs", ["usbtmc"], serial="P0012345"),
        device(0x1313, 0x8078, ["PM100D"], "Thorlabs", ["usbtmc"], serial="P0099999"),   # the lab's pm
        device(0x0403, 0x6001, ["USB Serial Port"], "FTDI", ["FTDIBUS"], serial="A600XYZ"),
        device(0x067B, 0x2303, ["Prolific USB-to-Serial Comm Port"], "Prolific"),       # the lab's laser, on COM7
        device(0x1313, 0x80F0, ["TSP01"], "Thorlabs", ["WinUSB"]),
        device(0x0403, 0xFAF0, ["APT DC Motor Controller", "Kinesis KDC101"], "Thorlabs", ["FTDIBUS"]),
        device(0x3923, 0x709B, ["GPIB-USB-HS"], "National Instruments", ["NiViPxiK"]),
        device(0x1234, 0x5678, ["Widget"], "", ["WinUSB"]),
        device(0x046D, 0xC52B, ["USB Receiver"], "Logitech", ["HidUsb"])])            # a mouse's: listed briefly
    monkeypatch.setattr(probe, "serial_ports", lambda: [serial_port("COM5", 0x0403, 0x6001, "A600XYZ"),
                                                        serial_port("COM7", 0x067B, 0x2303)])
    monkeypatch.setattr(probe, "network_adapters", lambda: [])
    monkeypatch.setattr(portholders, "holders_of", lambda names, timeout=portholders.LOOK_S, likely=True: {})
    monkeypatch.setattr(portholders, "waiting", lambda: [])
    monkeypatch.setattr(portholders, "visa_users", lambda timeout=portholders.LOOK_S, names=None: [])
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    rt.collection = catalog
    lines = rt.connected_devices().splitlines()

    def line(start):
        [hit] = [x for x in lines if x.startswith("- " + start)]
        return hit
    assert line("PM100D (USB 1313:8078, Thorlabs, serial P0012345)").endswith(
        "; the instrument collection knows its USB ID 1313:8078 as thorlabs/pm100d (Thorlabs PM100D), with no card "
        "yet; its manual: %s" % PM100D_MANUAL)
    assert line("PM100D (USB 1313:8078, Thorlabs, serial P0099999)").endswith("; driver usbtmc")   # the lab's own
    assert "; a USB-serial adapter (FTDI FT232R, USB 0403:6001), whose ID is the chip's own" in line("USB Serial Port")
    assert line("Prolific USB-to-Serial").endswith("; driver usbser")   # on COM7, the lab's laser: nothing more
    tsp = line("TSP01")
    assert "; USB vendor 1313: Thorlabs; it may be one of the instrument collection's Thorlabs instruments" in tsp
    assert tsp.endswith('; nothing Kelvin has names the product: web_search "USB VID_1313&PID_80F0" says what it is')
    kinesis = line("APT DC Motor Controller")
    assert "; USB 0403:faf0: Thorlabs; it may be one of the instrument collection's Thorlabs instruments that talk " \
           "over USB: thorlabs/kdc101 (Thorlabs KDC101), thorlabs/tc300" in kinesis, kinesis   # the one it names first
    assert "; a GPIB-USB adapter (NI GPIB-USB-HS, USB 3923:709b): no instrument itself" in line("GPIB-USB-HS")
    assert line("Widget").endswith('; nothing Kelvin has names the product: web_search "USB VID_1234&PID_5678" says '
                                   'what it is')
    assert "- also: USB Receiver" in lines
    rt.collection = None                    # the collection switched off: the maker, and the search
    lines = rt.connected_devices().splitlines()
    assert line("TSP01").endswith('; USB vendor 1313: Thorlabs; nothing Kelvin has names the product: web_search '
                                  '"USB VID_1313&PID_80F0" says what it is')


def test_usb_words_for_a_card_and_a_gpib_adapter():
    registry = with_usb(builtin_registry(), "thorlabs/pm100", ["1313:8078"])
    assert usb_words(by_usb("1313:8078", registry)) == "Kelvin's card for its USB ID 1313:8078 is thorlabs/pm100 (%s)" \
        % registry.get("thorlabs/pm100").model
    assert usb_words(by_usb("0957:0718", registry)).startswith("a GPIB-USB adapter (Keysight 82357B, USB 0957:0718)")
    assert usb_words(by_usb("nonsense", registry)) == "" and by_usb(None, registry).ids == ""


def test_a_scripts_port_on_a_generic_chip_takes_no_card_from_its_usb_id(ports):
    """scriptrun.port_device names the device behind a port a script opens: a card for its USB ID makes it that card's
    instrument (a laser's, a light source), never for a generic chip's ID."""
    registry = with_usb(builtin_registry(), "coherent/obis", ["0403:6001"])
    lab = types.SimpleNamespace(registry=registry)
    ports.append(Port("COM9", "USB Serial Port (COM9)", 0x0403, 0x6001, "FTDI"))
    device = scriptrun.port_device(lab, "serial://COM9")
    assert device["card"] is None and device["light"] == "" and device["words"].startswith("USB Serial Port (COM9)")
    registry.put(dataclasses.replace(registry.get("coherent/obis"), usb_ids=["0403:faf0"]))   # a maker's own ID
    ports.append(Port("COM10", "APT Device (COM10)", 0x0403, 0xFAF0, "Thorlabs"))
    assert scriptrun.port_device(lab, "serial://COM10")["card"].id == "coherent/obis"
