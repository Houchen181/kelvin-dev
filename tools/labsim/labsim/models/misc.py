"""Instruments that don't speak SCPI: a Pfeiffer TPG 362 gauge controller (ENQ protocol), a home-built Arduino
hot plate, an Oxford Instruments Mercury iTC (its own READ:/SET: language)."""
from __future__ import annotations

import math
import re
import time

from ..instrument import Chunk, CommandError, Instrument, command
from .common import Wired

ACK, NAK, ENQ = b"\x06", b"\x15", b"\x05"


class PfeifferTPG362(Wired, Instrument):
    """Mnemonic + CR LF; the gauge acknowledges (ACK or NAK) and answers only to ENQ. COM,n makes it stream
    readings by itself."""
    model = "pfeiffer_tpg362"
    idn = "TPG362,PTG28290,{serial},010100,010100"
    default_serial = "44990000"
    has_488 = False
    has_syst_err = False
    drops_unread = False
    terminators = (b"\r\n", b"\r", b"\n")
    reply_end = b"\r\n"
    framing = (9600, 8, "none", 1.0)
    usb_ids = "0403:6015"

    def reset(self) -> None:
        self.pending = None
        self.unit = 0
        self.stream_every = None
        self.last_stream = 0.0

    def feed(self, data: bytes, end: bool, bus: str, now: float) -> None:
        if ENQ in data and self.reachable():
            data = data.replace(ENQ, b"")
            if self.pending is not None:
                self.out.append(Chunk(self.pending.encode() + b"\r\n", now + 0.02, True))
                self.traffic.append((time.strftime("%H:%M:%S"), ">", self.pending))
        if data:
            super().feed(data, end, bus, now)

    def _reading(self, g: int) -> str:
        p = self.world.vacuum.gauge(g)
        p *= {0: 1.0, 1: 0.750062, 2: 100.0}[self.unit]
        status = 0 if p > 1e-9 else 1
        return "%d,%.4E" % (status, p * (1 + self.noise(0.005)))

    def handle(self, text: str):
        mnem, _, arg = text.strip().upper().partition(",")
        answers = {"PR1": lambda: self._reading(1), "PR2": lambda: self._reading(2),
                   "PRX": lambda: self._reading(1) + "," + self._reading(2),
                   "AYT": lambda: self.identity(), "UNI": lambda: str(self.unit),
                   "TID": lambda: "PKR,TPR", "ERR": lambda: "0000", "COM": lambda: "1"}
        if mnem not in answers:
            self.pending = None
            return NAK
        if mnem == "UNI" and arg:
            self.unit = int(arg) if arg.isdigit() and int(arg) < 3 else self.unit
        if mnem == "COM":
            self.stream_every = {0: 0.1, 1: 1.0, 2: 60.0}.get(int(arg or 1), 1.0)
        self.pending = answers[mnem]()
        return ACK

    def tick(self, dt: float, now: float) -> None:
        if self.stream_every and now - self.last_stream >= self.stream_every and len(self.out) < 50:
            self.last_stream = now
            self.out.append(Chunk(self._reading(1).encode() + b"\r\n", now, True))


class ArduinoHeater(Wired, Instrument):
    """A student's Arduino sketch: ID?, T?, SET x, SET?, HEAT ON/OFF, answering OK, a number, or ERR (also to
    *IDN?). Opening its port resets the board (the DTR line): the heater goes off and the sketch takes ~2 s to start,
    then prints READY."""
    model = "arduino_heater"
    idn = "HEATER-ARDUINO v2"
    default_serial = "85735313233351F0D1E1"
    has_488 = False
    has_syst_err = False
    drops_unread = False
    terminators = (b"\n",)
    reply_end = b"\r\n"
    framing = (9600, 8, "none", 1.0)
    usb_ids = "2341:0043"

    def reset(self) -> None:
        self.setpoint, self.heat, self.boot_until = 25.0, False, 0.0
        self.world.hotplate.duty = 0.0

    def on_open(self, bus: str, now: float) -> None:
        if bus == "asrl":
            self.reset()
            self.inbuf.clear()
            self.out.clear()
            self.boot_until = now + 1.8
            self.out.append(Chunk(b"READY\r\n", self.boot_until, False))
            self.world.note("%s reset by its port being opened (heater off)" % self.name)

    def status(self):
        return {"T_C": round(self.world.hotplate.T, 2), "setpoint_C": self.setpoint, "heating": self.heat}

    def feed(self, data: bytes, end: bool, bus: str, now: float) -> None:
        if now < self.boot_until:
            return    # still in the bootloader: what arrives is lost
        super().feed(data, end, bus, now)

    def tick(self, dt: float, now: float) -> None:
        hp = self.world.hotplate
        hp.duty = 1.0 if self.heat and hp.T < self.setpoint - 0.2 else (0.0 if not self.heat or hp.T > self.setpoint + 0.2
                                                                       else hp.duty)

    def handle(self, text: str):
        t = text.strip()
        u = t.upper()
        if u == "ID?":
            return self.identity()
        if u == "T?":
            return "%.2f" % (self.world.hotplate.T + self.noise(0.03))
        if u == "SET?":
            return "%.2f" % self.setpoint
        m = re.fullmatch(r"SET\s+([-+]?\d+(?:\.\d*)?)", u)
        if m:
            v = float(m.group(1))
            if not 0 <= v <= float(self.opt("max_C", 60)):
                return "ERR"
            self.setpoint = v
            return "OK"
        if u in ("HEAT ON", "HEAT OFF"):
            self.heat = u.endswith("ON")
            return "OK"
        if u == "HEAT?":
            return "ON" if self.heat else "OFF"
        return "ERR"


class MercuryITC(Wired, Instrument):
    """READ:DEV:<uid>:TEMP:SIG:TEMP -> STAT:DEV:<uid>:TEMP:SIG:TEMP:4.2341K; SET:... -> STAT:SET:...:VALID."""
    model = "oxford_mercury_itc"
    idn = "IDN:OXFORD INSTRUMENTS:MERCURY ITC:{serial}:2.6.04.000"
    default_serial = "112233"
    has_syst_err = False
    drops_unread = False
    terminators = (b"\n", b"\r\n")
    reply_end = b"\n"
    framing = (115200, 8, "none", 1.0)

    def setup(self) -> None:
        self.sensors = {k.upper(): v for k, v in (self.opt("sensors", {"MB1.T1": "sample", "DB8.T1": "stage"})).items()}
        self.heater = str(self.opt("heater_of", "MB1.T1")).upper()

    def reset(self) -> None:
        self.loop = {uid: dict(tset=0.0, enab=False, rset=1.0, rena=False, hset=0.0, p=10.0, i=1.0, d=0.0,
                               integ=0.0, sp=0.0) for uid in self.sensors}

    def status(self):
        return {uid: {"T": round(self.world.cryo.sensor(w), 4), "tset": self.loop[uid]["tset"],
                      "enab": self.loop[uid]["enab"], "heater_pct": round(self.loop[uid]["hset"], 1)}
                for uid, w in self.sensors.items()}

    def tick(self, dt: float, now: float) -> None:
        total = 0.0
        for uid, lp in self.loop.items():
            if not lp["enab"]:
                lp["hset"] = 0.0
                continue
            if lp["rena"]:
                step = lp["rset"] / 60.0 * dt
                d = lp["tset"] - lp["sp"]
                lp["sp"] = lp["tset"] if abs(d) <= step else lp["sp"] + math.copysign(step, d)
            else:
                lp["sp"] = lp["tset"]
            e = lp["sp"] - self.world.cryo.sensor(self.sensors[uid])
            lp["integ"] = max(-20.0, min(20.0, lp["integ"] + e * dt / 60.0 * lp["i"]))
            lp["hset"] = max(0.0, min(100.0, lp["p"] * (e + lp["integ"])))
            total += (lp["hset"] / 100.0) ** 2
        self.world.cryo.heaters[self.name] = min(1.0, total) * self.world.cryo.p_max

    def handle(self, text: str):
        t = text.strip()
        if t.upper() == "*IDN?":
            return self.identity()
        parts = t.split(":")
        verb = parts[0].upper()
        if verb == "READ" and len(parts) >= 3 and parts[1].upper() == "DEV":
            uid = parts[2].upper()
            path = ":".join(p.upper() for p in parts[3:])
            if uid in self.sensors:
                lp = self.loop[uid]
                T = self.world.cryo.sensor(self.sensors[uid])
                values = {"TEMP:SIG:TEMP": "%.4fK" % (T + self.noise(3e-4)),
                          "TEMP:SIG:RES": "%.3fO" % (48.0 * math.exp(6.1 / math.sqrt(max(T, 0.05)))),
                          "TEMP:LOOP:TSET": "%.4fK" % lp["tset"], "TEMP:LOOP:ENAB": "ON" if lp["enab"] else "OFF",
                          "TEMP:LOOP:HSET": "%.3f" % lp["hset"], "TEMP:LOOP:RSET": "%.4fK/m" % lp["rset"],
                          "TEMP:LOOP:RENA": "ON" if lp["rena"] else "OFF", "TEMP:LOOP:P": "%.4f" % lp["p"],
                          "TEMP:LOOP:I": "%.4f" % lp["i"], "TEMP:LOOP:D": "%.4f" % lp["d"]}
                if path in values:
                    return "STAT:DEV:%s:%s:%s" % (uid, path, values[path])
            return "STAT:DEV:%s:%s:INVALID" % (uid, ":".join(parts[3:]))
        if verb == "SET" and len(parts) >= 4 and parts[1].upper() == "DEV":
            uid = parts[2].upper()
            key = ":".join(p.upper() for p in parts[3:-1])
            val = parts[-1]
            ok = False
            if uid in self.sensors:
                lp = self.loop[uid]
                try:
                    if key == "TEMP:LOOP:TSET":
                        v = float(val.rstrip("Kk"))
                        ok = 0.0 <= v <= 400.0
                        if ok:
                            lp["tset"] = v
                    elif key in ("TEMP:LOOP:ENAB", "TEMP:LOOP:RENA"):
                        ok = val.upper() in ("ON", "OFF")
                        if ok:
                            lp["enab" if key.endswith("ENAB") else "rena"] = val.upper() == "ON"
                    elif key in ("TEMP:LOOP:RSET", "TEMP:LOOP:P", "TEMP:LOOP:I", "TEMP:LOOP:D"):
                        lp[{"RSET": "rset", "P": "p", "I": "i", "D": "d"}[key.split(":")[-1]]] = float(val.rstrip("K/m"))
                        ok = True
                except ValueError:
                    ok = False
            return "STAT:%s:%s" % (t, "VALID" if ok else "INVALID")
        return "%s:INVALID" % t
