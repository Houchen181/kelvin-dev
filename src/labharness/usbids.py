"""What a device's USB ID (VID:PID) says of it, for an instrument that doesn't say who it is.

Kelvin names an instrument from its identity reply (*IDN?, or a card's own query). One that doesn't answer (a maker's
USB device driven through its SDK, a USB-serial instrument with a protocol of its own, a USBTMC instrument that ignores
*IDN?) is named by its USB ID instead: Kelvin's card for it (identify: usb), the instrument collection's entry that
lists it (collection.py), else at least its maker, from its vendor ID (VENDORS). discovery.by_usb puts these together.

The IDs of the generic USB-serial chips and adapters (GENERIC) name nothing: any maker's product can sit behind an FTDI
or a CH340 that keeps the chip's own IDs, so Kelvin says "a USB-serial adapter" and asks the instrument behind it. A
maker's own product ID under a chip maker's vendor ID names that maker's product as any other ID does (OWN_PRODUCTS:
Thorlabs' APT motor controllers are 0403:faf0, FTDI's vendor ID with Thorlabs' product ID).

Every ID here has its source beside it: the Linux usb.ids list (linux-usb.org, version 2024.03.18), the Linux kernel's
USB-serial drivers (drivers/usb/serial), which bind these chips to a serial port, or the chip maker's own data sheet or
Linux driver."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# The generic USB-serial bridge chips, and USB-RS-232 adapters sold under their own IDs, by the IDs they have unless the
# maker of a product gives it its own. Never a name for an instrument: it says who it is on its serial port, or nothing
# does. The instrument collection's kic.py refuses these in a card or an instrument.yaml (its own copy of this list).
GENERIC: Dict[str, str] = {
    # FTDI: the kernel's ftdi_sio_ids.h (FTDI_8U232AM_PID, FTDI_8U232AM_ALT_PID, FTDI_8U2232C_PID, FTDI_4232H_PID,
    # FTDI_232H_PID, FTDI_FTX_PID, the FT2233HP to FT4232HA series, FTDI_SIO_PID, FTDI_232RL_PID)
    "0403:6001": "FTDI FT232R", "0403:6006": "FTDI FT232", "0403:6010": "FTDI FT2232", "0403:6011": "FTDI FT4232H",
    "0403:6014": "FTDI FT232H", "0403:6015": "FTDI FT-X", "0403:6040": "FTDI FT2233HP", "0403:6041": "FTDI FT4233HP",
    "0403:6042": "FTDI FT2232HP", "0403:6043": "FTDI FT4232HP", "0403:6044": "FTDI FT233HP",
    "0403:6045": "FTDI FT232HP", "0403:6048": "FTDI FT4232HA", "0403:8372": "FTDI FT8U100AX",
    "0403:fbfa": "FTDI FT232RL",
    # Prolific: the kernel's pl2303.h (PL2303_PRODUCT_ID, and _TB, _GC, _GB, _GT, _GL, _GE, _GS)
    "067b:2303": "Prolific PL2303", "067b:2304": "Prolific PL2303TB", "067b:23a3": "Prolific PL2303GC",
    "067b:23b3": "Prolific PL2303GB", "067b:23c3": "Prolific PL2303GT", "067b:23d3": "Prolific PL2303GL",
    "067b:23e3": "Prolific PL2303GE", "067b:23f3": "Prolific PL2303GS",
    # Silicon Labs: the kernel's cp210x.c ("Silicon Labs factory default" and "Windows Update") and usb.ids
    "10c4:ea60": "Silicon Labs CP210x", "10c4:ea61": "Silicon Labs CP210x", "10c4:ea63": "Silicon Labs CP210x",
    "10c4:ea70": "Silicon Labs CP2105", "10c4:ea71": "Silicon Labs CP2108", "10c4:ea7a": "Silicon Labs CP2105",
    "10c4:ea7b": "Silicon Labs CP2108",
    # WCH (QinHeng): the kernel's ch341.c and usb.ids for the CH340 and CH341 (4348 is WCH's older vendor ID); WCH's
    # own Linux driver, ch343ser_linux, for the CH342, CH343, CH344 and CH9101 to CH9104
    "1a86:7523": "WCH CH340", "1a86:7522": "WCH CH340", "1a86:5523": "WCH CH341", "4348:5523": "WCH CH341",
    "1a86:55d2": "WCH CH342", "1a86:55d3": "WCH CH343", "1a86:55d5": "WCH CH344", "1a86:55d8": "WCH CH9101",
    "1a86:55d4": "WCH CH9102", "1a86:55d7": "WCH CH9103", "1a86:55df": "WCH CH9104",
    # Microchip: usb.ids for the MCP2221, the MCP2200 data sheet (DS22228, its default VID and PID) for the MCP2200
    "04d8:00dd": "Microchip MCP2221", "04d8:00df": "Microchip MCP2200",
    # adapters: ATEN's UC-232A (the kernel's pl2303.h, ATEN_PRODUCT_ID; usb.ids) and Moxa's UPort 1100 series (the
    # kernel's ti_usb_3410_5052.c, MXU1_*_PRODUCT_ID)
    "0557:2008": "ATEN UC-232A", "110a:1110": "Moxa UPort 1110", "110a:1130": "Moxa UPort 1130",
    "110a:1131": "Moxa UPort 1131", "110a:1150": "Moxa UPort 1150", "110a:1151": "Moxa UPort 1151",
}

# Makers of lab instruments by USB vendor ID: (the maker, as Kelvin says it; the names its instruments go by in the
# instrument collection's maker field, for the candidates a device of theirs may be). Each from usb.ids, except Lake
# Shore's: the kernel's cp210x.c binds its temperature controllers, gaussmeters and magnet supplies at 1fb9.
VENDORS: Dict[int, Tuple[str, Tuple[str, ...]]] = {
    0x0957: ("Keysight (Agilent)", ("keysight", "agilent", "hp")),     # usb.ids: Agilent Technologies, Inc.
    0x2A8D: ("Keysight", ("keysight", "agilent", "hp")),               # usb.ids: Keysight Technologies, Inc.
    0x0699: ("Tektronix", ("tektronix",)),                             # usb.ids: Tektronix, Inc.
    0x05E6: ("Keithley", ("keithley",)),                               # usb.ids: Keithley Instruments
    0x0AAD: ("Rohde & Schwarz", ("rohde & schwarz", "hameg")),         # usb.ids: Rohde & Schwarz GmbH & Co. KG
    0x3923: ("National Instruments", ("ni", "national instruments")),  # usb.ids: National Instruments Corp.
    0x1AB1: ("Rigol", ("rigol",)),                                     # usb.ids: Rigol Technologies
    0xF4EC: ("Siglent", ("siglent",)),                                 # usb.ids: Atten Electronics / Siglent
    0xF4ED: ("Siglent", ("siglent",)),                                 # usb.ids: Shenzhen Siglent Co., Ltd.
    0x0B21: ("Yokogawa", ("yokogawa",)),                               # usb.ids: Yokogawa Electric Corp.
    0x05FF: ("Teledyne LeCroy", ("lecroy",)),                          # usb.ids: LeCroy Corp.
    0x2184: ("GW Instek", ("gw instek",)),                             # usb.ids: GW Instek
    0x0CE9: ("Pico Technology", ("pico technology",)),                 # usb.ids: Pico Technology
    0x0F7E: ("Fluke", ("fluke",)),                                     # usb.ids: Fluke Corp.
    0x2817: ("Signal Hound", ("signal hound",)),                       # usb.ids: Signal Hound, Inc.
    0x20CE: ("Mini-Circuits", ("mini-circuits", "minicircuits")),      # usb.ids: Minicircuits
    0x09DB: ("Measurement Computing", ("measurement computing",)),     # usb.ids: Measurement Computing Corp.
    0x0CD5: ("LabJack", ("labjack",)),                                 # usb.ids: LabJack Corporation
    0x1E7B: ("Zurich Instruments", ("zurich instruments",)),           # usb.ids: Zurich Instruments (HF2, UHF, MFLI)
    0x1313: ("Thorlabs", ("thorlabs",)),                               # usb.ids: ThorLabs
    0x104D: ("Newport", ("newport",)),                                 # usb.ids: Newport Corporation; NEWPORT_VID
    0x1FB9: ("Lake Shore", ("lake shore", "lakeshore")),               # the kernel's cp210x.c: Lake Shore Model 336 ...
    0x1A72: ("Physik Instrumente (PI)", ("physik instrumente",)),      # usb.ids: Physik Instrumente (kernel: PI_VID)
    0x2939: ("Zaber", ("zaber",)),                                     # usb.ids: Zaber Technologies Inc.
    0x0D4D: ("Coherent", ("coherent",)),                               # usb.ids: Coherent, Inc.
    0x2457: ("Ocean Insight (Ocean Optics)", ("ocean insight", "ocean optics")),   # usb.ids: Ocean Optics Inc.
    0x136E: ("Andor", ("andor",)),                                     # usb.ids: Andor Technology Ltd.
    0x0661: ("Hamamatsu", ("hamamatsu",)),                             # usb.ids: Hamamatsu Photonics K.K.
    0x0E0D: ("PicoQuant", ("picoquant",)),                             # usb.ids: PicoQuant GmbH
    0x2676: ("Basler", ("basler",)),                                   # usb.ids: Basler AG
    0x1AB2: ("Allied Vision", ("allied vision",)),                     # usb.ids: Allied Vision
    0x1409: ("IDS Imaging", ("ids imaging", "ids")),                   # usb.ids: IDS Imaging Development Systems GmbH
    0x199E: ("The Imaging Source", ("imaging source",)),               # usb.ids: The Imaging Source Europe GmbH
    0x20F7: ("XIMEA", ("ximea",)),                                     # usb.ids: XIMEA
    0x1E10: ("FLIR (Point Grey)", ("flir", "point grey")),             # usb.ids: Point Grey Research, Inc.
}

# A maker's own product IDs under a chip maker's vendor ID, with the vendor ID of the maker they name: the kernel's
# ftdi_sio_ids.h (FTDI_THORLABS_PID, "ThorLabs USB motor drivers"; PI_C865_PID and PI_C857_PID, Physik Instrumente's)
OWN_PRODUCTS: Dict[str, int] = {"0403:faf0": 0x1313, "0403:e0a0": 0x1A72, "0403:e0a1": 0x1A72}

# GPIB-USB adapters of instrument makers (usb.ids): no instrument themselves, the instruments on their bus are asked
# through them. Their maker's vendor ID would otherwise offer that maker's instruments as what they may be.
GPIB_ADAPTERS: Dict[str, str] = {
    "0957:0107": "Keysight 82357A", "0957:0007": "Keysight 82357A", "0957:0718": "Keysight 82357B",
    "0957:0518": "Keysight 82357B", "3923:702a": "NI GPIB-USB-B", "3923:702b": "NI GPIB-USB-B",
    "3923:709b": "NI GPIB-USB-HS",
}

_ID = re.compile(r"^(?:0x)?([0-9a-f]{4}):(?:0x)?([0-9a-f]{4})$", re.I)
_WINDOWS = re.compile(r"VID_([0-9a-f]{4})&PID_([0-9a-f]{4})", re.I)   # USB\VID_1313&PID_8078\P0012345
_VISA = re.compile(r"^USB\d*::(\w+)::(\w+)::", re.I)


def normal(vid_pid: Any) -> Optional[str]:
    """A USB ID as Kelvin keeps it, "vvvv:pppp" in lowercase hex, however it came: 0403:6001, 0x0403:0x6001,
    Windows' VID_0403&PID_6001, or (vid, pid) as numbers; None for anything else."""
    if isinstance(vid_pid, (tuple, list)):
        ok = len(vid_pid) == 2 and all(isinstance(x, int) and 0 <= x <= 0xFFFF for x in vid_pid)
        return "%04x:%04x" % tuple(vid_pid) if ok else None
    text = str(vid_pid or "").strip()
    m = _ID.match(text) or _WINDOWS.search(text)
    return ("%s:%s" % m.groups()).lower() if m else None


def is_generic(vid_pid: Any) -> bool:
    """Whether a USB ID is a generic USB-serial chip's or adapter's own (GENERIC): it names no instrument, as any
    maker's product can sit behind one."""
    return normal(vid_pid) in GENERIC


def chip(vid_pid: Any) -> str:
    """The generic USB-serial chip or adapter a USB ID is ("FTDI FT232R"); "" for any other ID."""
    return GENERIC.get(normal(vid_pid) or "", "")


def _vid(vid: Any) -> Optional[int]:
    """A vendor ID as a number: given as one, or in hex ("1313", "0x1313"), or as the first half of a USB ID."""
    if isinstance(vid, int) and not isinstance(vid, bool):
        return vid
    text = str(vid or "").strip().split(":")[0]
    try:
        return int(text, 16) if text else None
    except ValueError:
        return None


def vendor(vid: Any) -> str:
    """The instrument maker a USB vendor ID belongs to (VENDORS: 0x1313 is Thorlabs); "" for one the table doesn't
    have. vid: a number, hex text ("1313", "0x1313"), or a whole USB ID ("1313:8078")."""
    entry = VENDORS.get(_vid(vid) or -1)
    return entry[0] if entry else ""


def _maker_entry(vid_pid: Any) -> Optional[Tuple[str, Tuple[str, ...]]]:
    ids = normal(vid_pid)
    if not ids or ids in GENERIC:
        return None
    return VENDORS.get(OWN_PRODUCTS.get(ids, int(ids[:4], 16)))


def maker(vid_pid: Any) -> str:
    """The instrument maker a whole USB ID names: a maker's own product ID under a chip maker's vendor ID (Thorlabs'
    0403:faf0), else its vendor ID's (vendor); "" for a generic chip's ID, or a vendor the table doesn't have."""
    entry = _maker_entry(vid_pid)
    return entry[0] if entry else ""


def maker_words(vid_pid: Any) -> str:
    """The maker a USB ID names, in words: "USB vendor 1313: Thorlabs", or for a maker's own product ID under a chip
    maker's vendor ID "USB 0403:faf0: Thorlabs"; "" when it names none."""
    ids = normal(vid_pid)
    who = maker(ids)
    if not who or not ids:
        return ""
    return "USB %s: %s" % (ids, who) if ids in OWN_PRODUCTS else "USB vendor %s: %s" % (ids[:4], who)


def made_by(vid_pid: Any, name: str) -> bool:
    """Whether a maker's name, as the instrument collection writes it ("Keithley (Tektronix)", "HP / Agilent"), is
    the maker a USB ID names (maker). Only the maker's own name counts, not the company named in brackets that owns
    it: a Tektronix vendor ID is no Keithley instrument's."""
    entry = _maker_entry(vid_pid)
    if entry is None:
        return False
    own = [part.strip().lower() for part in re.split(r"[/,]", str(name or "").split("(")[0]) if part.strip()]
    return any(re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(word), part) for word in entry[1] for part in own)


def gpib_adapter(vid_pid: Any) -> str:
    """The GPIB-USB adapter a USB ID is ("NI GPIB-USB-HS"); "" for any other ID."""
    return GPIB_ADAPTERS.get(normal(vid_pid) or "", "")


def from_visa(resource: Any) -> Optional[str]:
    """The USB ID in a VISA USB resource name: USB0::0x0957::0x1796::MY123::INSTR is 0957:1796 (VISA may write the
    IDs in decimal too, USB0::2391::6038::MY123::INSTR); None for any other resource."""
    m = _VISA.match(str(resource or "").strip())
    if not m:
        return None
    try:
        ids = tuple(int(text, 0) for text in m.groups())
    except ValueError:   # hex without its 0x (0957), as VISA's decimal never starts with a 0: both are hex then
        try:
            ids = tuple(int(text, 16) for text in m.groups())
        except ValueError:
            return None
    return normal(ids)


def search_text(vid_pid: Any) -> str:
    """What to search the web for to learn what a USB ID is, as Windows writes the IDs: "USB VID_1313&PID_8078"."""
    ids = normal(vid_pid)
    return "USB VID_%s&PID_%s" % (ids[:4].upper(), ids[5:].upper()) if ids else ""
