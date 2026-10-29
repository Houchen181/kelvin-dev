"""What a scan looks through, so Kelvin can say what it found, what it could not open, and why it found nothing.

    VISA      the installed one (NI-VISA, Keysight IO Libraries, R&S VISA), else Kelvin's own (pyvisa-py)
    network   instruments on the local network (LXI): a VXI-11 broadcast and HiSLIP through pyvisa-py, and the mDNS
              adverts instruments send (_vxi-11, _hislip, _scpi-raw, _lxi) through zeroconf. No driver is needed
    USB       every USB device through Kelvin's own libusb (pyusb + libusb-package). An instrument is recognised by
              its USBTMC interface or its maker's vendor ID even when no driver lets VISA open it, and the survey
              says which driver is missing
    serial    the serial ports the system has (pyserial)
    GPIB      the GPIB adapters VISA knows (the scan asks behind Prologix adapters itself: discovery.py, prologix.py)

Nothing here sends a command to an instrument: listing only. The scan afterwards asks each one who it is."""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# makers of lab instruments, by USB vendor ID: an unknown device of theirs is worth naming
USB_MAKERS = {0x0957: "Keysight (Agilent)", 0x2A8D: "Keysight", 0x05E6: "Keithley", 0x0699: "Tektronix",
              0x1313: "Thorlabs", 0x0AAD: "Rohde & Schwarz", 0x1AB1: "Rigol", 0x3923: "National Instruments",
              0x1FB9: "Lake Shore", 0x0B21: "Yokogawa"}
MDNS_TYPES = ("_vxi-11._tcp.local.", "_lxi._tcp.local.", "_hislip._tcp.local.", "_scpi-raw._tcp.local.")
_libusb_ready = False


def first_line(e: BaseException) -> str:
    return str(e).splitlines()[0][:160] if str(e) else type(e).__name__


def use_bundled_libusb() -> bool:
    """Point pyusb (and so pyvisa-py's USB support) at the libusb that comes with Kelvin. False if there is none."""
    global _libusb_ready
    if _libusb_ready:
        return True
    try:
        import libusb_package
        import usb.backend.libusb1 as libusb1
    except ImportError:
        return False
    path = libusb_package.get_library_path()
    if path is None:
        return False
    if sys.platform.startswith("win") and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(path.parent))
    # the first get_backend() loads the library and keeps it, so later calls without arguments find it too
    _libusb_ready = libusb1.get_backend(find_library=lambda name: str(path)) is not None
    return _libusb_ready


@dataclass
class Survey:
    visa: str = ""
    resources: List[str] = field(default_factory=list)      # what the scan should ask, without repeats
    via: Dict[str, str] = field(default_factory=dict)       # resource -> how it was found
    network: str = ""
    serial_ports: List[str] = field(default_factory=list)
    usb: List[Dict[str, Any]] = field(default_factory=list)  # instrument-like USB devices
    gpib_adapters: Optional[int] = None
    prologix: List[str] = field(default_factory=list)       # Prologix adapters the scan asked behind, in words
    libraries: List[str] = field(default_factory=list)      # what makers' programs saw (PICam, LightField), in words
    problems: List[str] = field(default_factory=list)       # things to fix, each with what to do

    def add(self, resource: str, how: str) -> None:
        key = _key(resource)
        if any(_key(r) == key for r in self.resources):
            return
        self.resources.append(resource)
        self.via[resource] = how

    def lines(self) -> List[str]:
        """What was looked through, in words, for the log and the Lab panel."""
        out = ["VISA: %s; %s" % (self.visa or "none", _count(sum(1 for h in self.via.values() if h == "VISA"),
                                                             "instrument", "lists no instruments"))]
        if self.network:
            nets = [r for r, h in self.via.items() if h.startswith("network")]
            out.append("Network: %s; %s" % (self.network, _count(len(nets), "instrument answered",
                                                                 "no instrument answered", "instruments answered")))
        out.append("Serial ports: " + (", ".join(self.serial_ports) if self.serial_ports else "none"))
        usb = ["%s %04x:%04x%s" % (u["maker"] or "device", u["vid"], u["pid"], "" if u["visible"] else " (not openable)")
               for u in self.usb]
        out.append("USB instruments: " + (", ".join(usb) if usb else "none"))
        if self.gpib_adapters is not None or self.prologix:
            cards = ["%d in VISA" % self.gpib_adapters] if self.gpib_adapters else []
            out.append("GPIB adapters: " + ("; ".join(cards + self.prologix) or "none"))
        return out + self.libraries + ["To fix: " + p for p in self.problems]

    def as_dict(self) -> Dict[str, Any]:
        return dict(asdict(self), lines=self.lines())


def _count(n: int, one: str, zero: str, many: Optional[str] = None) -> str:
    if n == 0:
        return zero
    return "%d %s" % (n, one if n == 1 else (many or one + "s"))


def _key(resource: str) -> Tuple[str, ...]:
    """The same instrument written two ways (USB IDs in hex or decimal, an IP with another board number)."""
    r = resource.upper()
    m = re.match(r"^USB\d*::(\w+)::(\w+)::([^:]+)", r)
    if m:
        return ("USB", "%04x" % int(m.group(1), 0), "%04x" % int(m.group(2), 0), m.group(3))
    m = re.match(r"^TCPIP\d*::([^:]+)::", r)
    if m:
        return ("TCPIP", m.group(1))
    return (r,)


def _is_pyvisa(rm: Any) -> bool:
    return type(rm).__module__.startswith("pyvisa")


def _is_builtin(rm: Any) -> bool:
    lib = getattr(rm, "visalib", None)
    return lib is not None and type(lib).__module__.startswith("pyvisa_py")


def describe_visa(rm: Any) -> str:
    if not _is_pyvisa(rm):
        return getattr(rm, "description", None) or type(rm).__name__
    if _is_builtin(rm):
        return "Kelvin's built-in VISA (pyvisa-py)"
    lib = getattr(rm, "visalib", None)
    path = str(getattr(lib, "library_path", "") or "")
    maker = ""
    try:
        from pyvisa import constants
        maker = str(lib.get_attribute(rm.session, constants.VI_ATTR_RSRC_MANF_NAME)[0] or "")
    except Exception:
        pass
    return "%s (%s)" % (maker or "installed VISA", os.path.basename(path) or "system")


def _builtin_rm() -> Any:
    try:
        import pyvisa
        use_bundled_libusb()
        return pyvisa.ResourceManager("@py")
    except Exception:
        return None


def mdns_instruments(seconds: float = 2.0, interfaces: Any = None) -> List[Tuple[str, str]]:
    """Instruments that advertise themselves on the local network (LXI does), as VISA resources."""
    try:
        from zeroconf import InterfaceChoice, ServiceBrowser, ServiceStateChange, Zeroconf
    except ImportError:
        return []
    seen: List[Tuple[str, str]] = []

    def on_change(zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        if state_change is ServiceStateChange.Added:
            seen.append((service_type, name))
    zc = Zeroconf(interfaces=interfaces or InterfaceChoice.All)
    try:
        ServiceBrowser(zc, list(MDNS_TYPES), handlers=[on_change])
        time.sleep(seconds)
        by_host: Dict[str, Tuple[int, str]] = {}
        for service_type, name in list(seen):
            info = zc.get_service_info(service_type, name, timeout=1500)
            if info is None:
                continue
            for ip in info.parsed_addresses():
                if ":" in ip:   # IPv6: VISA resource strings want IPv4 here
                    continue
                if service_type.startswith(("_vxi-11", "_lxi")):
                    rank, res = 0, "TCPIP0::%s::inst0::INSTR" % ip
                elif service_type.startswith("_hislip"):
                    port = info.port or 4880
                    rank, res = 1, "TCPIP0::%s::hislip0%s::INSTR" % (ip, "" if port == 4880 else ",%d" % port)
                else:
                    rank, res = 2, "TCPIP0::%s::%d::SOCKET" % (ip, info.port or 5025)
                if ip not in by_host or rank < by_host[ip][0]:
                    by_host[ip] = (rank, res)
        return [(res, "network (mDNS)") for _, res in sorted(by_host.values(), key=lambda x: x[1])]
    finally:
        zc.close()


def usb_devices() -> List[Dict[str, Any]]:
    """USB devices that look like instruments: a USBTMC interface, or a lab-instrument maker's vendor ID."""
    if not use_bundled_libusb():
        return []
    import usb.core
    out = []
    try:
        devices = list(usb.core.find(find_all=True))
    except Exception:
        return []
    for d in devices:
        tmc = False
        try:
            for cfg in d:
                for intf in cfg:
                    tmc = tmc or (intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 3)
        except Exception:
            pass
        maker = USB_MAKERS.get(d.idVendor, "")
        if tmc or maker:
            out.append({"vid": d.idVendor, "pid": d.idProduct, "maker": maker, "usbtmc": tmc, "visible": False})
    return out


def serial_ports() -> List[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return ["%s (%s)" % (p.device, p.description) if p.description and p.description != "n/a" else p.device
            for p in sorted(list_ports.comports(), key=lambda p: p.device)]


def _usb_advice(u: Dict[str, Any]) -> str:
    who = "%s instrument (USB %04x:%04x)" % (u["maker"] or "A USB", u["vid"], u["pid"]) if u["maker"] else \
        "a USB instrument (%04x:%04x)" % (u["vid"], u["pid"])
    who = who[0].upper() + who[1:]
    if u["vid"] == 0x3923 and not u["usbtmc"]:
        return ("A National Instruments adapter (USB %04x:%04x, probably GPIB-USB) is plugged in, but VISA has no "
                "GPIB adapter: install NI-488.2 (with NI-VISA)" % (u["vid"], u["pid"]))
    if sys.platform.startswith("win"):
        return ("%s is plugged in, but Windows has no driver that lets Kelvin open it: install the maker's IO software "
                "(Keysight IO Libraries Suite or NI-VISA), which brings the USB driver, then scan again" % who)
    if sys.platform.startswith("linux"):
        return ("%s is plugged in, but Kelvin may not open it: add a udev rule giving your user access to %04x:%04x"
                % (who, u["vid"], u["pid"]))
    return "%s is plugged in, but Kelvin could not open it: unplug it, plug it in again, and scan again" % who


def network_adverts(broadcast: bool = True, mdns_seconds: float = 2.0,
                    mdns: Optional[Callable[[], Iterable[Tuple[str, str]]]] = None
                    ) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Instruments on the local network that answer a VXI-11 broadcast (through Kelvin's built-in VISA) or announce
    themselves over mDNS (LXI instruments do), as (VISA resource, how it was found), and what went wrong. mdns
    replaces the mDNS lookup in tests."""
    found: List[Tuple[str, str]] = []
    problems: List[str] = []
    if broadcast:
        py = _builtin_rm()
        if py is not None:
            try:
                found += [(str(r), "network (VXI-11)") for r in py.list_resources("TCPIP?*::INSTR")]
            except Exception:
                pass
    try:
        found += list(mdns() if mdns is not None else mdns_instruments(mdns_seconds))
    except Exception as e:
        problems.append("Looking on the network failed (%s)" % first_line(e))
    return found, problems


def survey(rm: Any, network: bool = True, usb: bool = True, mdns_seconds: float = 2.0,
           mdns: Optional[Callable[[], Iterable[Tuple[str, str]]]] = None,
           usb_list: Optional[Callable[[], List[Dict[str, Any]]]] = None,
           ports: Optional[Callable[[], List[str]]] = None) -> Survey:
    """Look through every place an instrument can be. The callables replace the real lookups in tests."""
    s = Survey(visa=describe_visa(rm))
    try:
        for r in rm.list_resources("?*::INSTR"):
            s.add(str(r), "VISA")
    except Exception as e:
        s.problems.append("VISA could not list instruments (%s): reinstall NI-VISA or Keysight IO Libraries"
                          % first_line(e))
    real = _is_pyvisa(rm)
    if network and (real or mdns is not None):
        # an installed VISA lists only what is configured in it, so the broadcast goes out too; the built-in one
        # broadcast when it listed
        found_net, problems = network_adverts(real and not _is_builtin(rm), mdns_seconds,
                                              mdns if mdns is not None else (None if real else (lambda: [])))
        s.problems += problems
        for r, how in found_net:
            s.add(r, how)
        s.network = "searched the local network (VXI-11 broadcast and mDNS)"
    if real:
        try:
            s.gpib_adapters = len(list(rm.list_resources("GPIB?*::INTFC")))
        except Exception:
            s.gpib_adapters = 0
    s.serial_ports = (ports or serial_ports)()
    if usb:
        s.usb = (usb_list or (usb_devices if real else (lambda: [])))()
        for u in s.usb:
            u["visible"] = any(_key(r)[:3] == ("USB", "%04x" % u["vid"], "%04x" % u["pid"]) for r in s.resources)
            if not u["visible"]:
                s.problems.append(_usb_advice(u))
    return s


def reason(found: List[Any], s: Optional[Survey]) -> str:
    """Why a scan gives Kelvin nothing to connect (or what it gives), in a sentence or two, for the Lab panel."""
    known = [f for f in found if getattr(f, "card_id", None) and not getattr(f, "in_lab", None)]
    unknown = [f for f in found if getattr(f, "idn", "") and not getattr(f, "card_id", None)]
    silent = [f for f in found if not getattr(f, "idn", "") and not getattr(f, "card_id", None)
              and not getattr(f, "in_lab", None)]
    if known:
        return "Found " + ", ".join("%s (%s)" % (f.model, f.address) for f in known)
    if s is None:
        return "Kelvin has not looked for instruments yet"
    if unknown:
        named = [f.model for f in unknown if getattr(f, "collection", "") and f.model]   # the collection knows them
        return ("%s answered, but Kelvin has no card for %s yet: press Find its manual and write its card"
                % ("The " + " and the ".join(named) if len(named) == len(unknown) else
                   _count(len(unknown), "instrument", "", "instruments"), "it" if len(unknown) == 1 else "them"))
    if s.problems:
        return "No instrument Kelvin can open was found. " + s.problems[0]
    if silent:
        return ("%s did not answer (%s): check they are switched on and connected, then scan again"
                % (_count(len(silent), "port or address", "", "ports or addresses"),
                   ", ".join(f.address for f in silent[:4]) + (" ..." if len(silent) > 4 else "")))
    places = ["VISA lists none", "nothing answered on the local network" if s.network else "",
              "there are no serial ports" if not s.serial_ports else "", "no USB instruments are plugged in"]
    return ("No instruments are connected to this computer: " + ", ".join(p for p in places if p) +
            ". Run Kelvin on the computer the instruments are plugged into, or add a network instrument by its "
            "address in Lab setup")
