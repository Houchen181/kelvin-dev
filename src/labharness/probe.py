"""What an instrument is, before Kelvin has a card for it: what a network address answers, what is plugged into this
computer (USB devices, serial ports and network adapters, also those no driver claims), which makers' programs and
drivers are installed, and where their files are.

Everything here only looks. On the network it reads what a port says by itself, asks *IDN? on the ports instruments
use for SCPI, and loads a web page's title; it looks only at addresses on local networks, never the internet. On a
serial port it listens first, then asks only an identity query, at the speeds and framings instruments use."""
from __future__ import annotations

import concurrent.futures
import html
import ipaddress
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

WIN = sys.platform.startswith("win")

# (port, how to ask, what usually answers there)
PORTS: List[Tuple[int, str, str]] = [
    (22, "banner", "SSH: a computer running Linux (a board such as PYNQ or Red Pitaya, or a controller's PC)"),
    (23, "banner", "Telnet: a command line"),
    (80, "http", "web page"),
    (111, "open", "VXI-11 (SCPI over the network): TCPIP0::{host}::inst0::INSTR"),
    (443, "https", "web page"),
    (502, "open", "Modbus TCP (compressors, temperature controllers, chillers, gauges): modbus://{host}?unit=1, "
                  "with a card from its register map"),
    (1234, "open", "Prologix GPIB-ETHERNET: GPIB instruments behind it, each at its own GPIB address"),
    (1998, "banner", "Toptica DLC pro command line"),
    (4001, "open", "a serial device server (Moxa NPort): an RS-232 instrument behind it, TCPIP0::{host}::4001::SOCKET "
                   "(its speed and framing are set on the server)"),
    (4880, "open", "HiSLIP (SCPI over the network): TCPIP0::{host}::hislip0::INSTR"),
    (5000, "open", "Red Pitaya's SCPI server, or Quantum Design's MultiPyVu server (qd://{host}:5000)"),
    (5024, "banner", "SCPI over Telnet"),
    (5025, "idn", "SCPI over a socket: TCPIP0::{host}::5025::SOCKET"),
    (5555, "idn", "SCPI over a socket: TCPIP0::{host}::5555::SOCKET"),
    (7020, "idn", "Oxford Instruments Mercury: TCPIP0::{host}::7020::SOCKET"),
    (7230, "banner", "attocube ANC300 (anc300://{host})"),
    (7777, "idn", "Lake Shore: TCPIP0::{host}::7777::SOCKET"),
    (8000, "http", "web service"),
    (8004, "open", "Zurich Instruments LabOne data server (zhinst://<device id>?host={host})"),
    (8080, "http", "web service"),
    (8888, "http", "a Pyro4 name server, as QICK boards run (qick://{host}:8888/<proxy name>), or Jupyter"),
    (9090, "http", "Jupyter (PYNQ boards), or attocube's JSON-RPC interface (attoDRY, IDS, AMC)"),
    (10001, "open", "a serial device server (Lantronix): an RS-232 instrument behind it, "
                    "TCPIP0::{host}::10001::SOCKET (its speed and framing are set on the server)"),
]
SOCKET_PORTS = {5025, 5555, 7020, 7777}
# what a sweep of a whole network knocks on: ports only instruments and their gateways open, not the web, SSH or
# Jupyter, which any computer may have (inspect_address looks at those on one address)
SWEEP_PORTS = (111, 502, 1234, 1998, 4001, 4880, 5024, 5025, 5555, 7020, 7230, 7777, 8004, 10001)
MAX_SWEEP = 1024     # addresses one sweep looks at, at most (a /22)
MAX_IDENTIFIED = 32  # hosts that answered whose identity a sweep asks for

# USB vendor IDs of instrument makers, and of the chips instruments are built around
USB_VENDORS = {
    0x0403: "FTDI USB-serial chip (in many instruments)", 0x10C4: "Silicon Labs USB-serial chip",
    0x067B: "Prolific USB-serial chip", 0x1A86: "WCH USB-serial chip", 0x0483: "STMicroelectronics",
    0x04D8: "Microchip", 0x04B4: "Cypress", 0x2341: "Arduino", 0x1209: "open hardware (pid.codes)",
    0x0957: "Keysight (Agilent)", 0x2A8D: "Keysight", 0x05E6: "Keithley", 0x0699: "Tektronix", 0x1313: "Thorlabs",
    0x0AAD: "Rohde & Schwarz", 0x1AB1: "Rigol", 0x3923: "National Instruments", 0x1FB9: "Lake Shore",
    0x0B21: "Yokogawa", 0xF4EC: "Siglent", 0x2184: "GW Instek", 0x0CE9: "Pico Technology", 0x2457: "Ocean Insight",
    0x151F: "Opal Kelly (Swabian Time Tagger)", 0x0456: "Analog Devices", 0x2676: "Basler", 0x1E10: "FLIR",
    0x1409: "IDS Imaging", 0x199E: "The Imaging Source", 0x0525: "Linux USB gadget", 0x1D6B: "Linux USB gadget",
}
USB_PRODUCTS = {
    (0x0403, 0xFAF0): "Thorlabs APT / Kinesis motion controller",
    (0x0525, 0xA4A2): "a board running Linux, as a USB network (PYNQ and RFSoC boards, BeagleBone): it is at an "
                      "address on the network adapter it makes",
}
HUBS = ("usbhub", "usbhub3", "usbccgp_hub")
# drivers Windows gives keyboards, mice, Bluetooth, webcams and sound: such a device is listed briefly
ORDINARY = {"usbccgp", "hidusb", "kbdhid", "mouhid", "bthusb", "usbvideo", "usbaudio", "usbaudio2", "wudfrd",
            "winusb", "usbstor", "uaspstor"}
ORDINARY_NAMES = ("keyboard", "mouse", "receiver", "bluetooth", "camera", "webcam", "fingerprint", "touchpad",
                  "headset", "audio", "card reader", "mass storage", "flash", "hub")
MAKER_WORDS = (
    "visa", "ni-", "national instruments", "labview", "keysight", "agilent", "tektronix", "rohde", "zurich",
    "labone", "attocube", "daisy", "thorlabs", "kinesis", "princeton", "teledyne", "lightfield", "picam", "andor",
    "solis", "oxford instruments", "lake shore", "lakeshore", "quantum design", "multivu", "swabian", "time tagger",
    "spincore", "ocean", "newport", "coherent", "toptica", "basler", "pylon", "hamamatsu", "picoquant", "pico technology",
    "picoscope", "stanford research", "keithley", "rigol", "siglent", "anritsu", "bluefors", "physik instrumente",
    "smaract", "standa", "zaber", "pfeiffer", "edwards", "leybold", "cryomech", "montana instruments", "janis",
    "ftdi", "silicon labs", "cp210", "prolific", "opal kelly", "spectrum instrumentation", "alazar", "gage",
    "xilinx", "amd vivado", "pynq", "python", "anaconda", "matlab", "qcodes", "hightech", "lumencor", "nkt",
    "santec", "exfo", "yokogawa", "ametek", "signal recovery", "femto", "menlo", "highfinesse", "wavemeter", "bristol",
    "photon", "spectro", "cryo", "instrument",
)
FILE_KINDS = {".py": 0, ".dll": 1, ".h": 2, ".pdf": 3, ".m": 4, ".ipynb": 4, ".vi": 5, ".lib": 6, ".so": 1,
              ".dylib": 1, ".whl": 0, ".zip": 6, ".exe": 7}


# ------------------------------------------------------------------ a network address

def host_of(address: str) -> Tuple[str, Optional[int]]:
    """The host (and port, if one is named) in what a person or a scan gives: 192.168.1.20, 192.168.1.20:5025,
    TCPIP0::192.168.1.20::5025::SOCKET, http://host/..., qick://host:8888/name."""
    a = address.strip()
    if "::" in a:
        parts = a.split("::")
        port = next((int(p) for p in parts[2:3] if p.isdigit()), None)
        return parts[1] if len(parts) > 1 else "", port
    if "://" in a:
        u = urllib.parse.urlparse(a)
        try:
            return u.hostname or "", u.port
        except ValueError:
            return u.hostname or "", None
    m = re.match(r"^\[?([^\]]+?)\]?(?::(\d+))?$", a) if a.count(":") <= 1 or a.startswith("[") else None
    if m:
        return m.group(1), int(m.group(2)) if m.group(2) else None
    return a, None


def local_ip(host: str) -> str:
    """The address of a host on a local network; raises ValueError for one on the internet."""
    try:
        ip = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError, UnicodeError) as e:
        raise ValueError("%s is not an address Kelvin can find (%s)" % (host, e))
    if ipaddress.ip_address(ip).is_global:
        raise ValueError("%s (%s) is on the internet: Kelvin looks only at addresses on local networks, where "
                         "instruments are" % (host, ip))
    return ip


def _clean(data: bytes, limit: int = 160) -> str:
    text = data.decode("utf-8", "replace")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _read(sock: socket.socket, seconds: float, limit: int = 65536, until: Optional[bytes] = None) -> bytes:
    data, end = b"", time.monotonic() + seconds
    while len(data) < limit:
        left = end - time.monotonic()
        if left <= 0:
            break
        sock.settimeout(left)
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, OSError):
            break
        if not chunk:
            break
        data += chunk
        if until is not None and until in data:
            break
    return data


def _http(sock: socket.socket, host: str, path: str, seconds: float) -> Dict[str, str]:
    sock.sendall(("GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: Kelvin (lab assistant)\r\nAccept: text/html, */*\r\n"
                  "Connection: close\r\n\r\n" % (path, host)).encode("ascii"))
    data = _read(sock, seconds)
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        return {"raw": _clean(data)} if data else {}
    out = {"status": lines[0].split(" ", 1)[-1][:40]}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        if k.strip().lower() in ("server", "location"):
            out[k.strip().lower()] = v.strip()[:120]
    m = re.search(rb"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    if m:
        out["title"] = html.unescape(_clean(m.group(1), 120))
    out["body"] = body[:4000].decode("utf-8", "replace")
    return out


Connect = Callable[[str, int, float], socket.socket]


def _connect(ip: str, port: int, seconds: float) -> socket.socket:
    return socket.create_connection((ip, port), timeout=seconds)


def probe_port(ip: str, port: int, how: str, seconds: float = 0.8, connect: Optional[Connect] = None) -> Optional[Dict[str, Any]]:
    """What one port says, or None when it is closed or silent to a connection."""
    try:
        sock = (connect or _connect)(ip, port, seconds)
    except OSError:
        return None
    out: Dict[str, Any] = {"port": port}
    try:
        if how == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE   # an instrument's own certificate: only its page title is read
            try:
                sock = ctx.wrap_socket(sock, server_hostname=None)
            except (ssl.SSLError, OSError):
                return out
        if how in ("http", "https"):
            out.update(_http(sock, ip, "/", seconds + 1.0))
        elif how == "banner":
            said = _read(sock, seconds, 4096)
            if said:
                out["banner"] = _clean(said)
        elif how == "idn":
            said = _read(sock, 0.3, 4096)   # some greet first
            if said:
                out["banner"] = _clean(said)
            sock.sendall(b"*IDN?\n")
            reply = _read(sock, seconds + 0.7, 4096, until=b"\n")
            if reply:
                out["idn"] = _clean(reply, 200)
        elif how == "any":   # a port the person named: what it says by itself, else its answer to *IDN?
            said = _read(sock, seconds, 4096)
            if said:
                out["banner"] = _clean(said)
            else:
                sock.sendall(b"*IDN?\n")
                reply = _read(sock, seconds + 0.7, 4096, until=b"\n")
                if reply.startswith(b"HTTP/"):
                    out["web"] = True
                elif reply:
                    out["idn"] = _clean(reply, 200)
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return out


def lxi_identity(ip: str, connect: Optional[Connect] = None, seconds: float = 1.5) -> Dict[str, str]:
    """An LXI instrument's identification page (http://<ip>/lxi/identification): maker, model, serial, firmware."""
    try:
        sock = (connect or _connect)(ip, 80, seconds)
    except OSError:
        return {}
    try:
        page = _http(sock, ip, "/lxi/identification", seconds).get("body", "")
    except OSError:
        return {}
    finally:
        sock.close()
    out = {}
    for tag in ("Manufacturer", "Model", "SerialNumber", "FirmwareRevision"):
        m = re.search(r"<(?:\w+:)?%s>\s*([^<]{1,80}?)\s*</" % tag, page)
        if m:
            out[tag] = m.group(1)
    return out


def inspect(address: str, ports: Optional[List[Tuple[int, str, str]]] = None, seconds: float = 0.8,
            connect: Optional[Connect] = None) -> Dict[str, Any]:
    """Every listed port of one local address, at once: {ip, found: [{port, hint, ...}], lxi: {...}}."""
    host, named = host_of(address)
    ip = local_ip(host)
    table = list(ports if ports is not None else PORTS)
    if named is not None and named not in [p for p, _, _ in table]:
        table.append((named, "any", "the port named"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(24, len(table) or 1)) as pool:
        results = list(pool.map(lambda p: probe_port(ip, p[0], p[1], seconds, connect), table))
    found = []
    for (port, how, hint), res in zip(table, results):
        if res is not None:
            res["hint"] = hint.replace("{host}", ip)
            res["how"] = how
            found.append(res)
    lxi = lxi_identity(ip, connect) if any(f["port"] == 80 for f in found) and ports is None else {}
    return {"host": host, "ip": ip, "found": found, "lxi": lxi, "looked": [p for p, _, _ in table]}


def sweep_networks(named: str = "", adapters: Optional[List[Dict[str, Any]]] = None
                   ) -> Tuple[List[ipaddress.IPv4Network], List[str]]:
    """The networks a sweep looks at, with notes on what it leaves out: the range named (192.168.1.0/24,
    192.168.1.*, or an address for its /24), else each network this computer is on (of a wider one, only this
    computer's own /24). Raises ValueError for a range on the internet or wider than MAX_SWEEP addresses."""
    if named.strip():
        text = named.strip()
        if text.endswith(".*"):
            text = text[:-2] + ".0/24"
        elif "/" not in text:
            text += "/24"
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise ValueError("%r is not a network: give it as 192.168.1.0/24 (or 192.168.1.*)" % named)
        if net.version != 4 or net.is_global or net.is_loopback or net.is_multicast or net.is_unspecified:
            raise ValueError("%s is not a local network: Kelvin looks only on local networks, where instruments are"
                             % net)
        if net.num_addresses > MAX_SWEEP:
            raise ValueError("%s has %d addresses: Kelvin looks at %d at most (a /22), so name a smaller range"
                             % (net, net.num_addresses, MAX_SWEEP))
        return [net], []
    nets: List[ipaddress.IPv4Network] = []
    notes: List[str] = []
    for a in adapters if adapters is not None else network_adapters():
        net = ipaddress.ip_network(a["network"], strict=False)
        if net.prefixlen >= 31 or net.is_loopback:
            continue
        if a.get("link_local") or net.is_link_local:
            notes.append("%s (%s) is a link-local network, too wide to sweep: an instrument there answers its "
                         "announcements, or give its address (front panel) to inspect_address" % (net, a["name"]))
            continue
        if ipaddress.ip_address(a["address"]) in ipaddress.ip_network("100.64.0.0/10"):
            continue   # a VPN's own addresses (Tailscale): no instruments there
        if net.is_global:
            notes.append("%s (%s) is a public network: Kelvin doesn't sweep public addresses" % (net, a["name"]))
            continue
        if net.num_addresses > 256:
            own = ipaddress.ip_network("%s/24" % a["address"], strict=False)
            notes.append("%s (%s) has %d addresses: looked only at this computer's part of it, %s; name another "
                         "range to look there" % (net, a["name"], net.num_addresses, own))
            net = own
        if net not in nets:
            nets.append(net)
    return nets, notes


def sweep(networks: Iterable[ipaddress.IPv4Network], ports: Iterable[int] = SWEEP_PORTS, seconds: float = 0.35,
          connect: Optional[Connect] = None, skip: Iterable[str] = (), workers: int = 192) -> Dict[str, List[int]]:
    """Which addresses of these networks have which of these ports open: one connection to each, closed at once,
    nothing sent. {ip: [ports]}, in address order."""
    left_out = set(skip)
    hosts = [str(ip) for net in networks for ip in net.hosts() if str(ip) not in left_out]
    tasks = [(ip, port) for ip in hosts for port in ports]

    def knock(task: Tuple[str, int]) -> Optional[Tuple[str, int]]:
        try:
            sock = (connect or _connect)(task[0], task[1], seconds)
        except OSError:
            return None
        try:
            sock.close()
        except OSError:
            pass
        return task

    found: Dict[str, List[int]] = {}
    if tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
            for hit in pool.map(knock, tasks):
                if hit:
                    found.setdefault(hit[0], []).append(hit[1])
    return {ip: sorted(found[ip]) for ip in sorted(found, key=ipaddress.ip_address)}


def identify_host(ip: str, ports: Iterable[int], seconds: float = 0.8, connect: Optional[Connect] = None
                  ) -> Dict[str, Any]:
    """What one address that answered a sweep says on its open ports (*IDN? on SCPI sockets, a greeting where
    instruments greet), and its LXI identification page when it speaks VXI-11 or HiSLIP. Like inspect's result."""
    table = [p for p in PORTS if p[0] in set(ports)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(table) or 1)) as pool:
        results = list(pool.map(lambda p: probe_port(ip, p[0], p[1], seconds, connect), table))
    found = []
    for (port, how, hint), res in zip(table, results):
        if res is not None:
            res["hint"], res["how"] = hint.replace("{host}", ip), how
            found.append(res)
    lxi = lxi_identity(ip, connect) if {111, 4880} & {f["port"] for f in found} else {}
    return {"host": ip, "ip": ip, "found": found, "lxi": lxi, "looked": [p for p, _, _ in table]}


def what_answers(f: Dict[str, Any]) -> str:
    """One port's finding in words, with what it likely is."""
    port, bits = f["port"], []
    if f.get("idn"):
        bits.append("answers *IDN? with %r" % f["idn"])
    if f.get("banner"):
        bits.append("says %r" % f["banner"])
    if f.get("status"):
        page = "web page %r" % f["title"] if f.get("title") else "web server (%s)" % f["status"]
        server = f.get("server", "")
        if "tornado" in server.lower() or "/tree" in f.get("location", "") or "/login" in f.get("location", ""):
            page += ", Jupyter (a PYNQ board's password is xilinx unless changed)"
        elif server:
            page += " (%s)" % server
        bits.append(page)
    elif f.get("raw"):
        raw = f["raw"]
        kind = ("a Pyro4 server" if "PYRO" in raw else "a JSON-RPC server" if "jsonrpc" in raw.lower() else
                "something that is not a web server")
        bits.append("%s (answered a web request with %r)" % (kind, raw[:60]))
    elif f.get("web"):
        bits.append("a web server")
    if not bits:
        bits.append("open, says nothing by itself")
    return "port %d: %s; usually %s" % (port, "; ".join(bits), f["hint"])


# ------------------------------------------------------------------ a serial port

BAUDS = (9600, 19200, 57600, 115200, 38400, 4800, 2400, 1200)
FRAMINGS = (("none", 8), ("odd", 7), ("even", 7))   # 8N1, and the 7-bit framings older controllers use (Lake Shore)


def _printable(data: bytes) -> bool:
    text = data.strip(b"\r\n\x00 ")
    return len(text) >= 3 and sum(32 <= b < 127 or b in (9, 10, 13) for b in text) >= 0.9 * len(text)


def _open_serial(port: str, baud: int, parity: str, bits: int, timeout: float) -> Any:
    import serial   # pyserial, which Kelvin brings
    return serial.Serial(port, baud, bytesize=bits, parity={"none": "N", "odd": "O", "even": "E"}[parity],
                         stopbits=1, timeout=timeout)


def inspect_serial(port: str, query: str = "*IDN?", bauds: Optional[Iterable[int]] = None, listen: float = 0.6,
                   opener: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """What an instrument on a serial port says: first what it sends by itself (some stream their readings), at each
    speed; then its answer to an identity query at each speed and framing, stopping at the first clear answer.
    Returns {port, streams: [(baud, text)], answer: {baud, parity, data_bits, reply} or None, tried}."""
    opener = opener or _open_serial
    out: Dict[str, Any] = {"port": port, "streams": [], "answer": None, "tried": 0}
    speeds = list(bauds or BAUDS)
    for baud in speeds:   # listening sends nothing
        try:
            s = opener(port, baud, "none", 8, listen)
        except Exception as e:
            out["error"] = str(e).splitlines()[0] if str(e) else type(e).__name__
            return out
        try:
            said = s.read(512)
        finally:
            s.close()
        if _printable(said):
            out["streams"].append((baud, _clean(said, 160)))
            break
    for baud in speeds:
        for parity, bits in FRAMINGS:
            out["tried"] += 1
            try:
                s = opener(port, baud, parity, bits, 0.4)
            except Exception:
                continue
            echo = False
            try:
                s.reset_input_buffer()
                s.write((query + "\r\n").encode("ascii"))
                reply = s.read_until(b"\n", 256)
                if reply.strip() == query.encode("ascii").strip():   # it echoes the command, then answers
                    echo, reply = True, s.read_until(b"\n", 256)
            finally:
                s.close()
            if _printable(reply):
                out["answer"] = {"baud": baud, "parity": parity, "data_bits": bits, "reply": _clean(reply, 200),
                                 "echo": echo, "ending": "\r\n" if reply.endswith(b"\r\n") else
                                 "\r" if reply.endswith(b"\r") else "\n"}
                return out
    return out


# ------------------------------------------------------------------ what is plugged in

def _usb_ids(text: str) -> Tuple[Optional[int], Optional[int]]:
    m = re.search(r"VID_([0-9A-F]{4}).*?PID_([0-9A-F]{4})", text or "", re.I)
    return (int(m.group(1), 16), int(m.group(2), 16)) if m else (None, None)


def _windows_usb() -> List[Dict[str, Any]]:
    """Present USB devices and their interfaces from Windows' device list (SetupAPI): what the device calls itself,
    the driver Windows gave it, and a problem code when it has none (28: no driver installed)."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class DEVINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID), ("DevInst", wintypes.DWORD),
                    ("Reserved", ctypes.c_void_p)]

    class PROPKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.ULONG)]

    api = ctypes.WinDLL("setupapi", use_last_error=True)
    cfg = ctypes.WinDLL("cfgmgr32")
    api.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    api.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    api.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DEVINFO)]
    api.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    api.SetupDiGetDeviceInstanceIdW.argtypes = [ctypes.c_void_p, ctypes.POINTER(DEVINFO), wintypes.LPWSTR,
                                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
    api.SetupDiGetDeviceRegistryPropertyW.argtypes = [ctypes.c_void_p, ctypes.POINTER(DEVINFO), wintypes.DWORD,
                                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
                                                      ctypes.POINTER(wintypes.DWORD)]
    api.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    api.SetupDiGetDevicePropertyW.argtypes = [ctypes.c_void_p, ctypes.POINTER(DEVINFO), ctypes.POINTER(PROPKEY),
                                              ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    api.SetupDiGetDevicePropertyW.restype = wintypes.BOOL
    api.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    cfg.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(wintypes.ULONG),
                                          wintypes.DWORD, wintypes.ULONG]
    cfg.CM_Get_DevNode_Status.restype = wintypes.DWORD

    handle = api.SetupDiGetClassDevsW(None, "USB", None, 0x2 | 0x4)   # present devices of every class, on USB
    if not handle or handle == ctypes.c_void_p(-1).value:
        return []
    # DEVPKEY_Device_BusReportedDeviceDesc: the name the device itself reports, driver or not
    reported_key = PROPKEY(GUID(0x540B947E, 0x8B40, 0x45BC, (ctypes.c_ubyte * 8)(0xA8, 0xA2, 0x6A, 0x0B, 0x89, 0x4C,
                                                                                  0xBD, 0xA2)), 4)
    out: List[Dict[str, Any]] = []
    try:
        i = 0
        while i < 512:
            info = DEVINFO()
            info.cbSize = ctypes.sizeof(DEVINFO)
            if not api.SetupDiEnumDeviceInfo(handle, i, ctypes.byref(info)):
                break
            i += 1
            ident = ctypes.create_unicode_buffer(512)
            if not api.SetupDiGetDeviceInstanceIdW(handle, ctypes.byref(info), ident, 512, None):
                continue

            def registry(code: int) -> str:
                buf = ctypes.create_unicode_buffer(512)
                ok = api.SetupDiGetDeviceRegistryPropertyW(handle, ctypes.byref(info), code, None, buf,
                                                           ctypes.sizeof(buf), None)
                return buf.value.strip() if ok else ""
            buf, kind = ctypes.create_unicode_buffer(512), wintypes.ULONG()
            reported = buf.value.strip() if api.SetupDiGetDevicePropertyW(
                handle, ctypes.byref(info), ctypes.byref(reported_key), ctypes.byref(kind), buf, ctypes.sizeof(buf),
                None, 0) else ""
            status, problem = wintypes.ULONG(), wintypes.ULONG()
            code = cfg.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), info.DevInst, 0)
            vid, pid = _usb_ids(ident.value)
            out.append({"id": ident.value, "vid": vid, "pid": pid, "description": registry(0), "service": registry(4),
                        "class": registry(7), "maker": registry(11), "name": registry(12), "reported": reported,
                        "problem": problem.value if code == 0 and status.value & 0x400 else 0})
    finally:
        api.SetupDiDestroyDeviceInfoList(handle)
    return out


def _mac_usb() -> List[Dict[str, Any]]:
    import json
    out: List[Dict[str, Any]] = []

    def walk(items: Iterable[Any]) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            keys = {k.lower(): v for k, v in item.items()}
            vid = next((v for k, v in keys.items() if "vendor" in k and "id" in k), "")
            pid = next((v for k, v in keys.items() if "product" in k and "id" in k), "")
            name = keys.get("_name") or next((v for k, v in keys.items() if "productname" in k), "")
            maker = next((v for k, v in keys.items() if k in ("manufacturer", "usbdevicekeyvendorname")), "")
            if vid:
                to_int = lambda s: int(str(s).split()[0], 16) if str(s).strip() else None   # noqa: E731
                try:
                    out.append({"id": name, "vid": to_int(vid), "pid": to_int(pid), "description": name,
                                "reported": name, "maker": maker, "service": "", "class": "", "name": name,
                                "serial": keys.get("serial_num") or keys.get("usbdevicekeyserialnumber") or "",
                                "problem": 0})
                except ValueError:
                    pass
            walk(item.get("_items") or [])
    for kind in ("SPUSBHostDataType", "SPUSBDataType"):
        try:
            data = json.loads(subprocess.run(["system_profiler", kind, "-json"], capture_output=True, timeout=30).stdout)
        except Exception:
            continue
        walk(data.get(kind) or [])
        if out:
            break
    return out


def _linux_usb() -> List[Dict[str, Any]]:
    out = []
    for d in sorted(Path("/sys/bus/usb/devices").glob("*")):
        def read(name: str) -> str:
            try:
                return (d / name).read_text().strip()
            except OSError:
                return ""
        if not read("idVendor"):
            continue
        drivers = sorted({p.resolve().name for p in d.glob("*:*/driver")})
        out.append({"id": d.name, "vid": int(read("idVendor"), 16), "pid": int(read("idProduct") or "0", 16),
                    "description": read("product"), "reported": read("product"), "maker": read("manufacturer"),
                    "service": ",".join(drivers), "class": "", "name": "", "serial": read("serial"), "problem": 0})
    return out


def usb_devices() -> List[Dict[str, Any]]:
    """USB devices plugged in now, one entry per device (its interfaces folded in), hubs left out."""
    try:
        raw = _windows_usb() if WIN else _mac_usb() if sys.platform == "darwin" else _linux_usb()
    except Exception:
        return []
    devices: List[Dict[str, Any]] = []
    # the devices first, then their interfaces (USB\VID_..&PID_..&MI_00\...), folded into the device they belong to
    for d in sorted(raw, key=lambda d: "&MI_" in d.get("id", "").upper()):
        ident = d.get("id", "")
        if ident.upper().startswith("USB\\ROOT_HUB") or d.get("service", "").lower() in HUBS or (
                "hub" in d.get("description", "").lower() and d.get("class", "").lower() in ("usb", "")):
            continue
        parts = ident.split("\\")
        interface = "&MI_" in ident.upper()
        serial = d.get("serial") or (parts[2] if len(parts) > 2 and "&" not in parts[2] and not interface else "")
        entry = next((e for e in devices if d.get("vid") is not None and e["vid"] == d.get("vid")
                      and e["pid"] == d.get("pid") and (interface or e["serial"] == serial)), None)
        if entry is None:
            entry = {"vid": d.get("vid"), "pid": d.get("pid"), "serial": serial, "names": [], "maker": "",
                     "drivers": [], "classes": [], "problem": 0}
            devices.append(entry)
        for name in (d.get("reported"), d.get("name"), d.get("description")):
            if name and name not in entry["names"] and not re.match(r"^USB (Composite|Input) Device$", name):
                entry["names"].append(name)
        maker = d.get("maker", "")
        if maker and not maker.startswith("(") and maker.lower() not in ("microsoft", "generic") and not entry["maker"]:
            entry["maker"] = maker
        for field, value in (("drivers", d.get("service")), ("classes", d.get("class"))):
            if value and value not in entry[field]:
                entry[field].append(value)
        entry["problem"] = entry["problem"] or d.get("problem", 0)
    return devices


def serial_ports() -> List[Dict[str, Any]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [{"port": p.device, "description": p.description if p.description != "n/a" else "", "vid": p.vid,
             "pid": p.pid, "serial": p.serial_number or "", "maker": p.manufacturer or "", "product": p.product or ""}
            for p in sorted(list_ports.comports(), key=lambda p: p.device)]


def network_adapters() -> List[Dict[str, str]]:
    """Network adapters that are up, with their IPv4 address and network: an instrument on a cable of its own (or a
    board on USB networking) is on one of these networks."""
    try:
        import psutil
    except ImportError:
        return []
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in sorted(psutil.net_if_addrs().items()):
        if name in stats and not stats[name].isup:
            continue
        for a in addrs:
            if a.family != socket.AF_INET or a.address.startswith("127."):
                continue
            try:
                net = ipaddress.ip_network("%s/%s" % (a.address, a.netmask or "255.255.255.0"), strict=False)
            except ValueError:
                continue
            out.append({"name": name, "address": a.address, "network": str(net),
                        "link_local": str(net.network_address).startswith("169.254.")})
    return out


def ordinary(d: Dict[str, Any]) -> bool:
    """A keyboard, mouse, webcam, Bluetooth radio or memory stick rather than an instrument."""
    drivers = {x.lower() for x in d["drivers"]}
    named = " ".join(d["names"]).lower()
    return bool(drivers) and drivers <= ORDINARY and any(w in named for w in ORDINARY_NAMES)


def usb_line(d: Dict[str, Any], ports: List[Dict[str, Any]]) -> str:
    vid, pid = d.get("vid"), d.get("pid")
    ids = "%04x:%04x" % (vid, pid) if vid is not None and pid is not None else "?"
    names = " / ".join(d["names"][:2]) or "(no name)"
    maker = d["maker"] or USB_VENDORS.get(vid or -1, "")
    bits = ["%s (USB %s%s%s)" % (names, ids, ", " + maker if maker else "", ", serial " + d["serial"] if d["serial"]
                                  else "")]
    com = [p["port"] for p in ports if p.get("vid") == vid and p.get("pid") == pid
           and (not d["serial"] or not p.get("serial") or p["serial"] == d["serial"])]
    if com:
        bits.append("serial port " + ", ".join(com))
    if d["problem"] == 28 or (WIN and not d["drivers"]):
        bits.append("no driver installed: the maker's software or driver brings it")
    elif d["problem"]:
        bits.append("Windows reports a problem with it (code %d)" % d["problem"])
    elif d["drivers"]:
        bits.append("driver " + ", ".join(d["drivers"][:3]))
    known = USB_PRODUCTS.get((vid, pid))
    if known:
        bits.append(known)
    return "; ".join(bits)


# ------------------------------------------------------------------ makers' software and files

def windows_programs() -> List[Dict[str, str]]:
    import winreg
    out, seen = [], set()
    roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            for i in range(4000):
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(key, sub) as k:
                        def value(name: str) -> str:
                            try:
                                return str(winreg.QueryValueEx(k, name)[0]).strip()
                            except OSError:
                                return ""
                        name = value("DisplayName")
                        if not name or value("ParentKeyName") or value("SystemComponent") == "1":
                            continue
                        rec = {"name": name, "publisher": value("Publisher"), "version": value("DisplayVersion"),
                               "location": value("InstallLocation")}
                except OSError:
                    continue
                if (name, rec["version"]) not in seen:
                    seen.add((name, rec["version"]))
                    out.append(rec)
    return out


def mac_programs() -> List[Dict[str, str]]:
    out = []
    for folder in ("/Applications", "/Library/Frameworks", "/Library/Application Support", "/opt"):
        for p in sorted(Path(folder).glob("*")) if Path(folder).is_dir() else []:
            out.append({"name": p.stem if p.suffix in (".app", ".framework") else p.name, "publisher": "",
                        "version": "", "location": str(p)})
    return out


def programs(query: str = "") -> List[Dict[str, str]]:
    """Installed programs and drivers: those matching the query (in their name or maker), or, with none, the ones
    that look like instrument makers' software, drivers and Pythons."""
    try:
        found = windows_programs() if WIN else mac_programs()
    except Exception:
        return []
    words = [w for w in re.split(r"\s+", query.lower().strip()) if w]

    def hit(p: Dict[str, str]) -> bool:
        text = ("%s %s" % (p["name"], p["publisher"])).lower()
        if words:
            return all(w in text for w in words)
        return any(w in text for w in MAKER_WORDS)
    return sorted((p for p in found if hit(p)), key=lambda p: p["name"].lower())


def search_roots() -> List[Path]:
    """Where makers install their software, SDKs and examples (never a person's own folders)."""
    if WIN:
        env = os.environ
        names = [env.get("ProgramFiles"), env.get("ProgramFiles(x86)"), env.get("ProgramData"),
                 os.path.join(env.get("PUBLIC", r"C:\Users\Public"), "Documents"),
                 os.path.join(env.get("LOCALAPPDATA", ""), "Programs") if env.get("LOCALAPPDATA") else None]
    elif sys.platform == "darwin":
        names = ["/Applications", "/Library/Frameworks", "/Library/Application Support", "/opt", "/usr/local"]
    else:
        names = ["/opt", "/usr/local", "/usr/share/doc"]
    out = []
    for n in names:
        if n and Path(n).is_dir() and Path(n) not in out:
            out.append(Path(n))
    return out


def find_files(query: str, roots: Optional[List[Path]] = None, limit: int = 30, seconds: float = 20.0,
               depth: int = 7) -> List[Tuple[str, str]]:
    """Files and folders whose names hold every word of the query, in the makers' install folders: (path, what)."""
    words = [w for w in re.split(r"[\s*]+", query.lower().strip()) if w]
    if not words:
        return []
    end, hits, looked = time.monotonic() + seconds, [], 0
    stack = [(r, 0) for r in reversed(roots if roots is not None else search_roots())]
    while stack and time.monotonic() < end and looked < 400000:
        folder, level = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            looked += 1
            name = e.name.lower()
            try:
                is_dir = e.is_dir(follow_symlinks=False) and not e.is_symlink() and not (
                    hasattr(e, "is_junction") and e.is_junction())
            except OSError:
                continue
            if all(w in name for w in words):
                if is_dir:
                    hits.append((e.path, "folder"))
                else:
                    try:
                        size = e.stat().st_size
                    except OSError:
                        size = 0
                    hits.append((e.path, "%s, %s" % (Path(name).suffix or "file", _size(size))))
            if is_dir and level < depth and name not in ("windowsapps", "$recycle.bin", "winsxs", "node_modules",
                                                         "__pycache__", ".git"):
                stack.append((Path(e.path), level + 1))
    hits.sort(key=lambda h: (h[1] != "folder", FILE_KINDS.get(Path(h[0]).suffix.lower(), 8), len(h[0])))
    return hits[:limit]


def _size(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return ""
