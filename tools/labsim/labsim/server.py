"""The bench server: one process holding the world and its instruments, serving the programs that talk to them.

Requests are dicts over a local, authenticated multiprocessing connection (one per VISA session or SDK thread).
Reads block in the server, releasing its lock, until the reply is there, the read's terminator or END arrives, or
the read's own timeout runs out, exactly as viRead would.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from multiprocessing.connection import Listener
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from . import __version__
from .instrument import MESSAGE_BUSES, Chunk, Instrument
from .models import MODELS
from .world import World

PARITY = {"n": "none", "none": "none", "o": "odd", "odd": "odd", "e": "even", "even": "even", "m": "mark",
          "mark": "mark", "s": "space", "space": "space"}


def parse_resource(r: str) -> Optional[Tuple[str, str]]:
    """(canonical key, bus) for a VISA resource string, a COM port or an SDK address."""
    r = r.strip()
    m = re.fullmatch(r"(?i)(?:COM|ASRL)(\d+)(?:::INSTR)?", r)
    if m:
        return "ASRL%d::INSTR" % int(m.group(1)), "asrl"
    m = re.fullmatch(r"(?i)GPIB(\d*)::(\d+)(?:::\d+)?(?:::INSTR)?", r)
    if m:
        return "GPIB%d::%d::INSTR" % (int(m.group(1) or 0), int(m.group(2))), "gpib"
    m = re.fullmatch(r"(?i)GPIB(\d*)::INTFC", r)
    if m:
        return "GPIB%d::INTFC" % int(m.group(1) or 0), "intfc"
    m = re.fullmatch(r"(?i)USB(\d*)::(0x[0-9a-f]+|\d+)::(0x[0-9a-f]+|\d+)::([^:]+)(?:::\d+)?(?:::INSTR)?", r)
    if m:
        return "USB0::0x%04X::0x%04X::%s::INSTR" % (int(m.group(2), 0), int(m.group(3), 0), m.group(4).upper()), "usb"
    m = re.fullmatch(r"(?i)TCPIP(\d*)::([^:]+)::(\d+)::SOCKET", r)
    if m:
        return "TCPIP0::%s::%d::SOCKET" % (m.group(2).lower(), int(m.group(3))), "socket"
    m = re.fullmatch(r"(?i)TCPIP(\d*)::([^:]+)(?:::([A-Za-z0-9,]+))?(?:::INSTR)?", r)
    if m:
        dev = (m.group(3) or "inst0").lower()
        if dev.startswith("hislip"):
            return "TCPIP0::%s::hislip0::INSTR" % m.group(2).lower(), "hislip"
        return "TCPIP0::%s::inst0::INSTR" % m.group(2).lower(), "vxi11"
    m = re.fullmatch(r"(?i)sdk:([a-z0-9_]+):(.+)", r)
    if m:
        return "sdk:%s:%s" % (m.group(1).lower(), m.group(2)), "sdk"
    return None


def framing_ok(host: Optional[List[Any]], dev: Tuple[Any, ...]) -> bool:
    if not host:
        return True
    try:
        return (int(host[0]) == int(dev[0]) and int(host[1]) == int(dev[1])
                and PARITY.get(str(host[2]).lower(), "?") == PARITY.get(str(dev[2]).lower(), "!"))
    except (ValueError, TypeError, IndexError):
        return False


class Session:
    __slots__ = ("sid", "key", "bus", "inst", "owner", "port")

    def __init__(self, sid: int, key: str, bus: str, inst: Optional[Instrument], owner: Any, port: str = ""):
        self.sid, self.key, self.bus, self.inst, self.owner, self.port = sid, key, bus, inst, owner, port


class Server:
    def __init__(self, bench_path: str, home: str):
        self.home = os.path.abspath(home)
        os.makedirs(self.home, exist_ok=True)
        self.bench_path = os.path.abspath(bench_path)
        with open(bench_path, encoding="utf-8") as f:
            self.bench = yaml.safe_load(f) or {}
        self.name = str(self.bench.get("name") or os.path.splitext(os.path.basename(bench_path))[0])
        self.world = World(self.bench.get("world") or {})
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.instruments: Dict[str, Instrument] = {}
        self.addresses: Dict[str, Tuple[Instrument, str]] = {}
        self.ports: Dict[str, Dict[str, Any]] = {}
        for n in self.bench.get("com_ports") or []:
            self.ports["ASRL%d::INSTR" % int(n)] = {"device": "COM%d" % int(n), "inst": None}
        for name, c in (self.bench.get("instruments") or {}).items():
            if c.get("model") not in MODELS:
                raise SystemExit("bench %s: instrument %s has unknown model %r (known: %s)"
                                 % (bench_path, name, c.get("model"), ", ".join(sorted(MODELS))))
            inst = MODELS[c["model"]](name, c, self.world)
            inst.server = self
            self.instruments[name] = inst
            addrs = c.get("address")
            for a in addrs if isinstance(addrs, list) else [addrs]:
                if not a:
                    continue
                parsed = parse_resource(str(a))
                if not parsed:
                    raise SystemExit("bench %s: %s has an address labsim can't read: %r" % (bench_path, name, a))
                key, bus = parsed
                self.addresses[key] = (inst, bus)
                if bus == "asrl":
                    self.ports[key] = {"device": "COM" + key[4:-7], "inst": inst, "usb": c.get("usb", inst.usb_ids),
                                       "description": c.get("port_description")}
                    if c.get("framing"):
                        f = c["framing"]
                        inst.framing = (int(f[0]), int(f[1]), PARITY[str(f[2]).lower()], float(f[3]))
        self.gpib_boards = int(self.bench.get("gpib_boards", 1 if any(b == "gpib" for _, b in
                                                                      self.addresses.values()) else 0))
        self.sessions: Dict[int, Session] = {}
        self.next_sid = 1
        self.held: Set[str] = set()
        self.started = time.time()

    # ------------------------------------------------------------------ lifecycle
    def run(self, port: int = 0) -> None:
        key = secrets.token_bytes(16)
        listener = Listener(("127.0.0.1", port), authkey=key)
        info = {"port": listener.address[1], "key": key.hex(), "pid": os.getpid(), "bench": self.bench_path,
                "name": self.name, "version": __version__, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(os.path.join(self.home, "server.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1)
        threading.Thread(target=self._ticker, daemon=True, name="ticker").start()
        for name, inst in self.instruments.items():
            if inst.cfg.get("tcp"):
                threading.Thread(target=self._tcp_listen, args=(inst, int(inst.cfg["tcp"])), daemon=True).start()
        print("labsim %s: bench %r on 127.0.0.1:%d (%d instruments)" % (__version__, self.name, info["port"],
                                                                      len(self.instruments)), flush=True)
        while True:
            try:
                conn = listener.accept()
            except Exception as e:   # a client that failed the handshake
                print("refused a connection: %s" % e, flush=True)
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _tcp_listen(self, inst: Instrument, port: int) -> None:
        """A real TCP port on 127.0.0.1 for programs that open sockets themselves (attocube's 7230, M Squared's
        JSON port): bytes in are fed to the instrument, what it sends goes back."""
        import socket
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", port))
        except OSError as e:
            print("%s: can't listen on 127.0.0.1:%d (%s)" % (inst.name, port, e), flush=True)
            return
        srv.listen(4)
        print("%s also listens on 127.0.0.1:%d" % (inst.name, port), flush=True)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._tcp_conn, args=(inst, conn), daemon=True).start()

    def _tcp_conn(self, inst: Instrument, conn: Any) -> None:
        import socket
        conn.settimeout(0.02)
        with self.cond:
            if not inst.reachable():
                conn.close()
                return
            inst.on_open("socket", time.monotonic())
        try:
            while True:
                try:
                    data = conn.recv(65536)
                    if not data:
                        break
                    with self.cond:
                        if inst.reachable():
                            inst.feed(data, False, "socket", time.monotonic())
                except socket.timeout:
                    pass
                with self.cond:
                    out, _ = inst.take(1 << 20, None, False, time.monotonic()) if inst.reachable() else (b"", None)
                if out:
                    conn.sendall(out)
        except OSError:
            pass
        finally:
            conn.close()

    def _ticker(self) -> None:
        last = time.monotonic()
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            dt, last = min(now - last, 1.0), now
            with self.cond:
                try:
                    self.world.step(dt)
                    for inst in self.instruments.values():
                        if inst.powered:
                            inst.tick(dt, now)
                except Exception:
                    traceback.print_exc()
                self.cond.notify_all()

    def _serve(self, conn: Any) -> None:
        mine: Set[int] = set()
        try:
            while True:
                try:
                    req = conn.recv()
                except (EOFError, OSError):
                    break
                try:
                    resp = self.dispatch(req, mine)
                except Exception as e:
                    traceback.print_exc()
                    resp = {"err": "internal", "msg": "%s: %s" % (type(e).__name__, e)}
                try:
                    conn.send(resp)
                except (EOFError, OSError):
                    break
        finally:
            with self.cond:
                for sid in mine:
                    self.sessions.pop(sid, None)
            conn.close()

    # ------------------------------------------------------------------ requests
    def dispatch(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        op = req.get("op")
        fn = getattr(self, "op_" + str(op), None)
        if fn is None:
            return {"err": "unknown_op", "msg": "no such request %r" % op}
        return fn(req, mine)

    def op_hello(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        return {"ok": True, "name": self.name, "version": __version__}

    def op_resources(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        """What NI-VISA's viFindRsrc would list: instruments that answer on GPIB, attached USB-TMC instruments,
        configured LAN instruments, and every COM port of the computer."""
        out: List[str] = []
        with self.lock:
            for key, (inst, bus) in self.addresses.items():
                if bus in ("gpib", "usb", "vxi11", "hislip") and inst.reachable():
                    out.append(key)
                elif bus == "socket" and inst.cfg.get("listed"):
                    out.append(key)
            out += list(self.ports)
            out += ["GPIB%d::INTFC" % b for b in range(self.gpib_boards)]
        return {"ok": True, "resources": sorted(set(out))}

    def op_ports(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        ports = []
        with self.lock:
            for key, p in sorted(self.ports.items(), key=lambda kv: int(kv[0][4:-7])):
                inst = p.get("inst")
                usb = p.get("usb") or ""
                if inst is not None and "unplugged" in inst.faults and usb:
                    continue                       # a USB-serial cable pulled out: its COM port is gone too
                desc = p.get("description") or ("USB Serial Port (%s)" % p["device"] if usb
                                                 else "Communications Port (%s)" % p["device"])
                ports.append({"device": p["device"], "description": desc, "usb": usb,
                              "serial_number": inst.serial_no if inst is not None and usb else None,
                              "manufacturer": (inst.cfg.get("usb_manufacturer") if inst is not None else None)
                              or ("FTDI" if usb.startswith("0403") else "")})
        return {"ok": True, "ports": ports}

    def op_open(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        parsed = parse_resource(str(req.get("resource", "")))
        if not parsed:
            return {"err": "invalid_resource"}
        key, bus = parsed
        owner = req.get("owner")
        wait = 0.0
        with self.cond:
            entry = self.addresses.get(key)
            inst = entry[0] if entry else None
            present = inst is not None and inst.reachable()
            if bus == "intfc":
                if int(key[4:key.index("::")]) >= self.gpib_boards:
                    return {"err": "rsrc_nfound"}
            elif bus == "asrl":
                if key not in self.ports or (inst is not None and "unplugged" in inst.faults
                                             and self.ports[key].get("usb")):
                    return {"err": "rsrc_nfound", "msg": "no such COM port on this computer"}
                if key in self.held or any(s.key == key and s.owner != owner for s in self.sessions.values()):
                    return {"err": "rsrc_busy", "msg": "another program has the port open"}
            elif bus in ("usb", "vxi11", "hislip", "socket", "sdk"):
                if not present:
                    wait = 1.0 if bus in ("vxi11", "hislip", "socket") else 0.0
                elif key in self.held:
                    return {"err": "rsrc_busy", "msg": "another program holds the instrument"}
            if not wait:
                sid = self.next_sid
                self.next_sid += 1
                self.sessions[sid] = Session(sid, key, bus, inst, owner)
                mine.add(sid)
                if present:
                    if bus in ("asrl", "socket"):   # a port or connection opened afresh starts with nothing in it
                        inst.out.clear()
                        inst.inbuf.clear()
                    inst.on_open(bus, time.monotonic())
                return {"ok": True, "sid": sid, "present": present, "bus": bus,
                        "framing": list(inst.framing) if inst is not None else None}
        time.sleep(wait)   # a connect attempt to an address nobody answers at
        return {"err": "rsrc_nfound", "msg": "nothing answers at %s" % key}

    def op_close(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        with self.cond:
            self.sessions.pop(req.get("sid"), None)
            mine.discard(req.get("sid"))
        return {"ok": True}

    def _session(self, req: Dict[str, Any]) -> Optional[Session]:
        return self.sessions.get(req.get("sid"))

    def op_write(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        data = bytes(req.get("data") or b"")
        with self.cond:
            s = self._session(req)
            if s is None:
                return {"err": "invalid_object"}
            inst = s.inst
            if inst is None or not inst.reachable():
                if s.bus == "gpib":
                    return {"err": "nlisteners", "msg": "no listeners on the GPIB bus at that address"}
                if s.bus in ("usb", "vxi11", "hislip", "socket"):
                    return {"err": "conn_lost" if s.bus != "usb" else "io"}
                return {"ok": True, "n": len(data)}     # a serial port with nothing (on) behind it
            if s.bus == "asrl" and not framing_ok(req.get("framing"), inst.framing):
                inst.traffic.append((time.strftime("%H:%M:%S"), "?", "unreadable bytes (wrong baud rate/framing)"))
                return {"ok": True, "n": len(data)}
            if s.key in self.held and s.bus != "asrl":
                return {"err": "rsrc_busy"}
            inst.feed(data, bool(req.get("end", True)), s.bus, time.monotonic())
            self.cond.notify_all()
        return {"ok": True, "n": len(data)}

    def op_read(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        count = int(req.get("count", 65536))
        term = req.get("term")
        timeout = float(req.get("timeout", 2.0))
        deadline = time.monotonic() + timeout
        data = bytearray()
        with self.cond:
            s = self._session(req)
            if s is None:
                return {"err": "invalid_object"}
            end_ok = bool(req.get("end", True)) and s.bus in MESSAGE_BUSES
            garble = s.bus == "asrl" and s.inst is not None and not framing_ok(req.get("framing"), s.inst.framing)
            while True:
                now = time.monotonic()
                inst = s.inst
                if inst is not None and inst.reachable():
                    chunk, how = inst.take(count - len(data), term, end_ok, now)
                    if garble and chunk:
                        chunk = bytes((b | 0x80) if (b * 7) % 3 == 0 else b ^ 0x15 for b in chunk)
                    data += chunk
                    if how:
                        return {"ok": True, "data": bytes(data), "how": how}
                elif s.bus in ("usb", "vxi11", "hislip", "socket"):
                    return {"err": "conn_lost" if s.bus != "usb" else "io", "data": bytes(data)}
                remaining = deadline - now
                if remaining <= 0:
                    return {"err": "timeout", "data": bytes(data)}
                nxt = inst.next_ready(now) if inst is not None and inst.reachable() else None
                self.cond.wait(min(remaining, 0.25 if nxt is None else max(nxt, 0.001)))

    def op_clear(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        with self.cond:
            s = self._session(req)
            if s is None:
                return {"err": "invalid_object"}
            if s.inst is not None and s.inst.reachable():
                s.inst.device_clear()
            elif s.bus == "gpib":
                return {"err": "nlisteners"}
        return {"ok": True}

    def op_stb(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        with self.cond:
            s = self._session(req)
            if s is None:
                return {"err": "invalid_object"}
            if s.inst is None or not s.inst.reachable():
                return {"err": "nlisteners" if s.bus == "gpib" else "timeout"}
            return {"ok": True, "stb": s.inst.stb()}

    def op_trigger(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        with self.cond:
            s = self._session(req)
            if s is None:
                return {"err": "invalid_object"}
            if s.inst is not None and s.inst.reachable():
                s.inst.trigger()
        return {"ok": True}

    # ------------------------------------------------------------------ the makers' libraries
    def op_sdk_list(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        """Devices a maker's library would enumerate: [(address key, serial, model, extra)]."""
        kind = str(req.get("kind", "")).lower()
        out = []
        with self.lock:
            for key, (inst, bus) in sorted(self.addresses.items()):
                if bus == "sdk" and key.split(":")[1] == kind and inst.reachable():
                    out.append({"key": key, "serial": key.split(":", 2)[2], "name": inst.name,
                                "info": inst.sdk_info() if hasattr(inst, "sdk_info") else {}})
        return {"ok": True, "devices": out}

    def op_sdk(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        """A call into a maker's library for one device: the model returns (result, seconds until it's ready)."""
        parsed = parse_resource(str(req.get("key", "")))
        with self.cond:
            entry = self.addresses.get(parsed[0]) if parsed else None
            if entry is None or not entry[0].reachable():
                return {"err": "rsrc_nfound", "msg": "the device isn't connected"}
            if "dead" in entry[0].faults:   # the link is up but nothing answers: the maker's library times out
                dead = True
            else:
                dead = False
        if dead:
            time.sleep(3.0)
            return {"err": "sdk", "msg": "timed out waiting for the device to answer", "type": "TimeoutError"}
        with self.cond:
            inst = entry[0]
            try:
                result, wait = inst.sdk_call(str(req.get("method")), list(req.get("args") or []),
                                             dict(req.get("kwargs") or {}), time.monotonic())
            except Exception as e:
                return {"err": "sdk", "msg": str(e), "type": type(e).__name__, "code": getattr(e, "code", None)}
            if callable(result):    # finished after the wait, from the state then (an exposure, a sweep)
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    self.cond.wait(max(0.001, deadline - time.monotonic()))
                    if not inst.reachable():
                        return {"err": "sdk", "msg": "the device stopped answering", "type": "IOError"}
                try:
                    result = result()
                except Exception as e:
                    return {"err": "sdk", "msg": str(e), "type": type(e).__name__, "code": getattr(e, "code", None)}
                return {"ok": True, "result": result}
        if wait:
            time.sleep(wait)
        return {"ok": True, "result": result}

    # ------------------------------------------------------------------ control (labsim's own CLI)
    def _inst(self, name: str) -> Instrument:
        if name not in self.instruments:
            raise KeyError("no instrument %r on this bench (there are: %s)" % (name, ", ".join(self.instruments)))
        return self.instruments[name]

    def op_ctl(self, req: Dict[str, Any], mine: Set[int]) -> Dict[str, Any]:
        cmd = req.get("cmd")
        a = list(req.get("args") or [])
        with self.cond:
            try:
                if cmd == "status":
                    insts = []
                    for name, inst in self.instruments.items():
                        addrs = [k for k, (i, b) in self.addresses.items() if i is inst]
                        insts.append({"name": name, "model": inst.model, "addresses": addrs, "powered": inst.powered,
                                      "faults": sorted(inst.faults), "status": inst.status(),
                                      "errors": len(inst.errors)})
                    return {"ok": True, "bench": self.name, "uptime_s": round(time.time() - self.started),
                            "instruments": insts, "world": self.world.summary(), "held": sorted(self.held),
                            "sessions": len(self.sessions)}
                if cmd == "log":
                    inst = self._inst(a[0])
                    n = int(a[1]) if len(a) > 1 else 40
                    return {"ok": True, "lines": list(inst.traffic)[-n:]}
                if cmd == "power":
                    inst = self._inst(a[0])
                    inst.power(a[1] == "on")
                    self.world.note("%s switched %s" % (a[0], a[1]))
                    return {"ok": True}
                if cmd in ("unplug", "plug"):
                    inst = self._inst(a[0])
                    (inst.faults.add if cmd == "unplug" else inst.faults.discard)("unplugged")
                    if cmd == "unplug":
                        inst.device_clear()
                    self.world.note("%s %sged" % (a[0], cmd))
                    return {"ok": True}
                if cmd == "fault":
                    inst = self._inst(a[0])
                    on = len(a) < 3 or a[2] != "off"
                    msg = inst.fault(a[1], on) if hasattr(inst, "fault") else None
                    if msg is None:
                        (inst.faults.add if on else inst.faults.discard)(a[1])
                    self.world.note("fault %s on %s: %s" % (a[1], a[0], "on" if on else "off"))
                    return {"ok": True, "msg": msg or ""}
                if cmd == "hold":
                    parsed = parse_resource(a[0])
                    if not parsed:
                        inst = self._inst(a[0])
                        keys = [k for k, (i, b) in self.addresses.items() if i is inst]
                    else:
                        keys = [parsed[0]]
                    for k in keys:
                        (self.held.add if len(a) < 2 or a[1] != "off" else self.held.discard)(k)
                    return {"ok": True, "held": sorted(self.held)}
                if cmd == "set":
                    return {"ok": True, "msg": self._set(a[0], a[1])}
                if cmd == "note":
                    self.world.note(" ".join(a))
                    return {"ok": True}
            except (KeyError, IndexError, ValueError) as e:
                return {"err": "bad_request", "msg": str(e)}
        return {"err": "bad_request", "msg": "unknown control command %r" % cmd}

    def _set(self, path: str, value: str) -> str:
        """`set cryo.stage 4.2`, `set sample.Tc_K 7.9`, `set vacuum.leak 1e-4`, `set k2400.compliance 1e-6`."""
        try:
            v: Any = float(value)
        except ValueError:
            v = {"true": True, "false": False, "none": None}.get(value.lower(), value)
        head, _, attr = path.partition(".")
        target = {"cryo": self.world.cryo, "cryostat": self.world.cryo, "magnet": self.world.magnet,
                  "sample": self.world.sample, "chip": self.world.chip, "optics": self.world.optics,
                  "vacuum": self.world.vacuum, "hotplate": self.world.hotplate}.get(head)
        if target is None:
            target = self._inst(head)
        if target is self.world.sample and attr in ("damaged",) and not v:
            self.world.sample.damaged = None
            return "sample repaired"
        if not hasattr(target, attr) and not (hasattr(target, "cfg") and attr in getattr(target, "cfg", {})):
            raise KeyError("%s has no %r" % (head, attr))
        if hasattr(target, attr):
            setattr(target, attr, v)
        else:
            target.cfg[attr] = v
        return "%s = %r" % (path, v)


def serve(bench: str, home: str, port: int = 0) -> None:
    sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, "reconfigure") else None
    Server(bench, home).run(port)
