"""Phones and other computers: pairing, what each paired device may do, and the listener on the network.

The portal itself only listens on 127.0.0.1, where the access key works. Phone access opens a second listener on the
network (off until the user switches it on). There, only paired devices' own tokens work, never the access key:
- a device is paired with a one-time 6-digit code shown on this computer (the code expires in 10 minutes, and
  repeated wrong codes lock pairing for a while);
- "watch" devices see the lab, runs and alarms, can stop a run and can decline (never approve) a request;
- "control" devices can also chat with Kelvin and approve requests;
- settings, the lab config, instruments, approval mode and pairing are for this computer only.
Traffic on a plain Wi-Fi network is not encrypted, so away from a trusted network use a VPN such as Tailscale."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import re
import secrets
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

PHONE_PORT = 8771
ROLES = ("watch", "control")
CODE_TTL = 600
MAX_FAILURES = 5          # wrong codes within FAILURE_WINDOW lock pairing
FAILURE_WINDOW = 600

# What a paired device may do. Anything not listed is for this computer only.
_WATCH = [("GET", r"/api/(state|live|me|runs|runs/[^/]+)"),
          ("POST", r"/api/(stop|runs/stop|alarms/[^/]+/ack|approvals/[^/]+|me/forget)")]
_CONTROL = _WATCH + [
    ("GET", r"/api/(bootstrap|chats|chats/[^/]+|projects|projects/[^/]+|memory|settings|lab|mode|watches|reports"
            r"|reports/[^/]+)"),
    ("POST", r"/api/(chats|chats/[^/]+/(turn|compare|choose|review)|projects|watches|reports)"),
    ("PATCH", r"/api/(chats/[^/]+|projects/[^/]+)"),
    ("DELETE", r"/api/(chats/[^/]+|projects/[^/]+/(notes|papers)/[^/]+|watches/[^/]+|memory/notes/[^/]+)"),
]
RULES = {"watch": [(m, re.compile(p + "$")) for m, p in _WATCH],
         "control": [(m, re.compile(p + "$")) for m, p in _CONTROL]}


def allowed(role: str, method: str, path: str) -> bool:
    if role == "owner":
        return True
    return any(m == method and rx.match(path) for m, rx in RULES.get(role, []))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def addresses(port: int) -> List[Dict[str, str]]:
    """Where a phone can reach this computer: its addresses on the local network and on Tailscale."""
    found = set()
    with contextlib.suppress(OSError):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))   # picks the interface of the default route; sends nothing
            found.add(s.getsockname()[0])
        finally:
            s.close()
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    out = []
    for ip in sorted(found):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local:
            continue
        kind = "tailscale" if addr in ipaddress.ip_network("100.64.0.0/10") else "network" if addr.is_private else "public"
        out.append({"ip": ip, "kind": kind, "url": "http://%s:%d" % (ip, port)})
    return sorted(out, key=lambda a: ("tailscale", "network", "public").index(a["kind"]))


def remote_host_ok(host: str) -> bool:
    """On the network listener the Host must be an address or a VPN/LAN name, not a stranger's domain (which is
    how a web page would try to reach Kelvin through DNS rebinding)."""
    name = host.rsplit(":", 1)[0].strip("[]") if host else ""
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return name.endswith((".ts.net", ".local")) or name.lower() == socket.gethostname().lower()


@dataclass
class Pairing:
    code: str
    name: str
    role: str
    expires: float
    device: Optional[str] = None    # set when a phone used it


def _quiet_server(config: Any) -> Any:
    """A uvicorn server that leaves signal handling to the main server (both run on the same event loop)."""
    import uvicorn

    class Quiet(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield
    return Quiet(config)


class Remote:
    bind_host = "0.0.0.0"   # every interface; tests use 127.0.0.1 so no firewall asks

    def __init__(self, settings: Any):
        self.settings = settings
        self.pairings: Dict[str, Pairing] = {}
        self.failures: List[float] = []
        self.port: Optional[int] = None      # where the network listener is, while it runs
        self.error: Optional[str] = None
        self._server: Any = None
        self._task: Optional[asyncio.Future] = None
        self._seen: Dict[str, float] = {}

    # ------------------------------------------------------------------ devices

    def device_for(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        h = token_hash(token)
        for d in self.settings.devices:
            if secrets.compare_digest(d["hash"], h):
                now = time.time()
                if now - self._seen.get(d["id"], 0) > 60:   # remember when it was last used, not on every poll
                    self._seen[d["id"]] = now
                    self.settings.touch_device(d["id"], now)
                return d
        return None

    def start_pairing(self, name: str, role: str) -> Pairing:
        name = " ".join(str(name or "").split())[:40] or "Phone"
        if role not in ROLES:
            raise ValueError("role must be watch or control")
        now = time.time()
        self.pairings = {c: p for c, p in self.pairings.items() if p.expires > now and not p.device}
        code = "%06d" % secrets.randbelow(10 ** 6)
        while code in self.pairings:
            code = "%06d" % secrets.randbelow(10 ** 6)
        p = Pairing(code, name, role, now + CODE_TTL)
        self.pairings[code] = p
        return p

    def claim(self, code: str) -> Tuple[Dict[str, Any], str]:
        """A phone offers a code; returns (device, its token). PermissionError when wrong or locked."""
        now = time.time()
        self.failures = [t for t in self.failures if now - t < FAILURE_WINDOW]
        if len(self.failures) >= MAX_FAILURES:
            raise PermissionError("Too many wrong codes. Pairing is locked for a few minutes; make a new code then.")
        p = self.pairings.get(str(code or "").strip())
        if p is None or p.expires < now or p.device:
            self.failures.append(now)
            raise PermissionError("That code is wrong or has expired. Make a new one on the Kelvin computer.")
        token = secrets.token_urlsafe(32)
        device = self.settings.add_device(p.name, p.role, token_hash(token))
        p.device = device["id"]
        return device, token

    def pairing_state(self, code: str) -> Optional[Dict[str, Any]]:
        p = self.pairings.get(code)
        if p is None:
            return None
        return {"code": p.code, "name": p.name, "role": p.role, "expires_in": max(0, int(p.expires - time.time())),
                "device": p.device}

    # ------------------------------------------------------------------ the network listener

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, app: Any, port: int) -> None:
        """Listen on every interface at `port` (raises OSError if it is taken)."""
        await self.stop()
        import uvicorn
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):   # Windows: never share the port with another program
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bind_host, port))
        except OSError:
            sock.close()
            raise
        sock.listen(64)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False)
        self._server = _quiet_server(config)
        self._task = asyncio.ensure_future(self._server.serve(sockets=[sock]))
        self.error = None
        for _ in range(50):   # until it accepts connections
            if getattr(self._server, "started", False) or self._task.done():
                break
            await asyncio.sleep(0.05)
        if self._task.done():
            self.error = "the phone listener stopped at once"
            self.port = None

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            if self._task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(self._task), 5)
        self._server = None
        self._task = None
        self.port = None
