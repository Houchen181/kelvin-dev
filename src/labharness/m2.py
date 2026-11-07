"""M Squared lasers (SolsTiS Ti:sapphire, and other ICE-BLOC controllers): JSON messages over a TCP link.

Address: m2://<laser ip>:<port>, the port set on the laser's web page (Configure > Network Settings > Remote
interface) next to this computer's IP address. The laser accepts the link only from that IP: Kelvin sends the
address it reaches the laser from; add ?client_ip=<ip> if the laser must see another one (a NAT in between).

Card commands, one ICE-BLOC operation each (from M Squared's "SolsTiS 3 TCP/IP Protocol"):
    GET get_status#wavelength               a read op (get_*, poll_*, *_status, ping); #field picks a reply field,
    GET poll_move_wave_t#current_wavelength|wavelength      and a|b takes the first field the reply has
    SET move_wave_t wavelength={value}      an op that acts: name=value pairs, comma-separated; numbers go as [n]
    SET etalon_lock operation={value}       with bool_text [on, off]
A non-zero status in the reply to an op that acts is an error, and so is a parse_fail reply. Long tasks asked with
report=finished send a final report later (<op>_f_r); Kelvin keeps the newest one per op: GET move_wave_t_f_r#report.

A link that broke (the laser closed it, restarted, or the network dropped) is dropped, said as a lost instrument
(LinkLost), and made again, start_link and all, at the next exchange: it was never remade, and only a restart of Kelvin
helped. A reply that doesn't come in time is NoAnswer; the link stays, as a late reply is told apart by its id."""
from __future__ import annotations

import json
import re
import socket
import urllib.parse
from typing import Any, Dict, Optional

from .adapters import AdapterError, LinkLost, NoAnswer
from .cards import Card
from .drivers import DriverBackend, link_failed, ran_out

READ_OPS = re.compile(r"^(get_|poll_)|_status$|^ping$|^status$")
PARSE_ERRORS = {1: "the JSON could not be parsed, or the link was refused (wrong IP address)",
                2: "'message' missing", 3: "'transmission_id' missing", 4: "no transmission id value",
                5: "'op' missing", 6: "no operation name", 7: "operation not recognised",
                8: "'parameters' missing", 9: "invalid parameter name or value"}


def _one(v: Any) -> Any:
    return v[0] if isinstance(v, list) and len(v) == 1 else v


def _flat(params: Any) -> Dict[str, Any]:
    return {str(k).lower(): _one(v) for k, v in params.items()} if isinstance(params, dict) else {}


def parameters(text: Any) -> Dict[str, Any]:
    """'wavelength=810.5,report=finished' as the protocol wants it: numbers in one-element lists, words as strings."""
    if not isinstance(text, str) or "=" not in text:
        raise AdapterError("an M Squared op takes name=value pairs, e.g. SET move_wave_t wavelength={value}")
    out: Dict[str, Any] = {}
    for pair in text.split(","):
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if not re.match(r"^[a-z_][a-z0-9_]*$", name) or not value:
            raise AdapterError("not a name=value pair: %r" % pair)
        try:
            number = float(value)
        except ValueError:
            if not re.match(r"^[A-Za-z0-9_.]+$", value):   # the protocol's strings: no spaces, no minus sign
                raise AdapterError("%s=%r is neither a number nor a plain word" % (name, value))
            out[name] = value
        else:
            out[name] = [int(number) if number.is_integer() and re.match(r"^[0-9]+$", value) else number]
    return out


class M2Laser(DriverBackend):
    quiet_close = True   # closing the link sends the laser nothing

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        if not url.hostname or not url.port:
            raise AdapterError("%s: an M Squared laser's address is m2://<laser ip>:<port>, with the port set on the "
                               "laser's Network page (Remote interface)" % name)
        q = dict(urllib.parse.parse_qsl(url.query))
        self.host, self.port = url.hostname, url.port
        self.client_ip = q.get("client_ip")
        self.timeout = float(card.connection.get("timeout_ms", 5000)) / 1000.0
        self.connect = getattr(module, "create_connection", None) or socket.create_connection   # tests pass a fake
        self.sock: Any = None
        self.buf = ""
        self.tx = 0
        self.reports: Dict[str, Dict[str, Any]] = {}
        self._connect()

    def _connect(self) -> None:
        """The link, and the laser's yes to it (start_link, from this computer's address)."""
        try:
            self.sock = self.connect((self.host, self.port), self.timeout)
        except OSError as e:
            raise link_failed(self.name, "could not reach %s:%d. Check the laser is on and its Remote interface is "
                                         "enabled on that port" % (self.host, self.port), e) from e
        self.buf = ""
        client = self.client_ip or self.sock.getsockname()[0]
        try:
            reply = self._call("start_link", {"ip_address": client})
        except (LinkLost, NoAnswer):
            self._drop()
            raise
        except AdapterError as e:
            self._drop()
            raise AdapterError("%s: the laser refused the link from %s (%s). On the laser's Network page, the Remote "
                               "interface address must be %s." % (self.name, client, e, client)) from e
        if str(reply.get("status", "")).lower() != "ok":
            self._drop()
            raise AdapterError("%s: the laser refused the link from %s. On the laser's Network page, the Remote "
                               "interface address must be %s." % (self.name, client, client))

    def _drop(self) -> None:
        """The link let go of: the next exchange makes a new one."""
        sock, self.sock = self.sock, None
        self.buf = ""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _receive(self) -> Dict[str, Any]:
        dec = json.JSONDecoder()
        while True:
            text = self.buf.lstrip()
            if text.startswith("{"):
                try:
                    obj, end = dec.raw_decode(text)
                except ValueError:
                    obj, end = None, 0   # not complete yet
                if end:
                    self.buf = text[end:]
                    if isinstance(obj, dict) and isinstance(obj.get("message"), dict):
                        return obj["message"]
                    continue
            elif text:
                self.buf = text[text.find("{"):] if "{" in text else ""   # skip anything before a message
                continue
            try:
                chunk = self.sock.recv(4096)
            except OSError as e:   # socket.timeout too: the link stays (a late reply is told apart by its id)
                if not ran_out(e):
                    self._drop()
                raise link_failed(self.name, "no reply from the laser", e) from e
            if not chunk:
                self._drop()
                raise LinkLost("%s: the laser closed the link" % self.name)
            self.buf += chunk.decode("ascii", "replace")

    def _call(self, op: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.sock is None:   # dropped after a failure: made again (the failed op is never sent again)
            self._connect()
        self.tx = self.tx % 16383 + 1
        msg: Dict[str, Any] = {"transmission_id": [self.tx], "op": op}
        if params:
            msg["parameters"] = params
        try:
            self.sock.sendall(json.dumps({"message": msg}).encode("ascii"))
        except OSError as e:
            self._drop()
            raise link_failed(self.name, "lost the link to the laser", e) from e
        while True:
            m = self._receive()
            got = str(m.get("op", ""))
            p = m.get("parameters") or {}
            if got == "parse_fail":
                code = _one(p.get("protocol_error"))
                raise AdapterError("%s: the laser rejected %s (protocol error %s: %s)"
                                   % (self.name, op, code, PARSE_ERRORS.get(code, "unknown")))
            if got.endswith("_f_r"):   # a long task finished (or failed): keep it, wait for our own reply
                self.reports[got.lower()] = _flat(p)
                continue
            if _one(m.get("transmission_id")) not in (self.tx, None):
                continue   # a late reply to an earlier request
            return _flat(p)

    def set(self, path: str, value: Any) -> None:
        if READ_OPS.search(path) or path.endswith("_f_r"):
            raise AdapterError("%s: %s only reads; use GET" % (self.name, path))
        reply = self._call(path, parameters(value))
        status = reply.get("status")
        if status not in (None, 0, "ok"):
            raise AdapterError("%s: %s failed on the laser (status %s)" % (self.name, path, status))

    def get(self, path: str) -> Any:
        op, _, field = path.partition("#")
        if op.endswith("_f_r"):
            if op not in self.reports:
                raise AdapterError("%s: no %s report has arrived yet" % (self.name, op))
            reply = self.reports[op]
        elif READ_OPS.search(op):
            reply = self._call(op)
        else:
            raise AdapterError("%s: %s acts on the laser; Kelvin reads only with get_*, poll_* and *_status ops" % (self.name, op))
        if not field:
            return json.dumps(reply)
        for name in field.split("|"):
            if name in reply:
                value = reply[name]
                return value if isinstance(value, (int, float, str)) else json.dumps(value)
        raise AdapterError("%s: %s's reply has no %s (it has: %s)" % (self.name, op, field, ", ".join(reply)))

    def identity(self) -> str:
        return "M Squared,%s,%s" % (self.card.model, self.host)

    def close(self) -> None:
        self._drop()
