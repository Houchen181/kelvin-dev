"""What shims/sitecustomize.py installs in a program pointed at a bench (LABSIM_HOME set): the bench's COM ports in
pyserial, and Andor's spectrograph DLL in ctypes. The real ports and DLLs of the computer stay as they are."""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from .link import LabsimError, Link

_ports: Dict[str, Dict[str, Any]] = {}
_ports_at = 0.0
_lock = threading.Lock()


def bench_ports() -> Dict[str, Dict[str, Any]]:
    global _ports, _ports_at
    with _lock:
        if time.monotonic() - _ports_at > 2.0:
            try:
                link = Link()
                _ports = {p["device"].upper(): p for p in link.must("ports")["ports"]}
                link.close()
            except LabsimError:
                _ports = {}
            _ports_at = time.monotonic()
        return _ports


def _port_name(port: Any) -> Optional[str]:
    if not isinstance(port, str):
        return None
    name = port.upper().replace("\\\\.\\", "")
    return name if name in bench_ports() else None


def install_serial() -> None:
    try:
        import serial
        import serial.tools.list_ports as list_ports
        from serial.tools.list_ports_common import ListPortInfo
    except ImportError:
        return
    real_serial = serial.Serial
    real_for_url = serial.serial_for_url
    real_comports = list_ports.comports
    parity_names = {"N": "none", "E": "even", "O": "odd", "M": "mark", "S": "space"}

    class LabsimSerial(serial.SerialBase):
        """A COM port of the bench: the bytes go to and come from the instrument behind it."""

        def open(self) -> None:
            if self._port is None:
                raise serial.SerialException("Port must be configured before it can be used.")
            if self.is_open:
                raise serial.SerialException("Port is already open.")
            self._link = Link()
            r = self._link.call("open", resource=_port_name(self._port) or self._port, owner=os.getpid())
            if "err" in r:
                self._link.close()
                if r["err"] == "rsrc_busy":
                    raise serial.SerialException("could not open port '%s': PermissionError(13, 'Access is denied.', "
                                                 "None, 5)" % self._port)
                raise serial.SerialException("could not open port '%s': FileNotFoundError(2, 'The system cannot find "
                                             "the file specified.', None, 2)" % self._port)
            self._sid = r["sid"]
            self._pending = bytearray()
            self.is_open = True

        def _reconfigure_port(self, force_update: bool = False) -> None:
            pass

        def _framing(self) -> List[Any]:
            return [int(self._baudrate), int(self._bytesize), parity_names.get(self._parity, "none"),
                    float(self._stopbits)]

        def close(self) -> None:
            if self.is_open:
                try:
                    self._link.call("close", sid=self._sid)
                except LabsimError:
                    pass
                self._link.close()
                self.is_open = False

        def _fetch(self, count: int, timeout: float) -> bytes:
            r = self._link.call("read", sid=self._sid, count=count, term=None, end=False, timeout=timeout,
                                framing=self._framing())
            if "err" in r and r["err"] not in ("timeout",):
                raise serial.SerialException("ClearCommError failed (%s)" % r["err"])
            return bytes(r.get("data") or b"")

        @property
        def in_waiting(self) -> int:
            if not self.is_open:
                raise serial.PortNotOpenError()
            self._pending += self._fetch(65536, 0.0)
            return len(self._pending)

        def read(self, size: int = 1) -> bytes:
            if not self.is_open:
                raise serial.PortNotOpenError()
            out = bytearray(self._pending[:size])
            del self._pending[:size]
            timeout = 86400.0 if self._timeout is None else float(self._timeout)
            end = time.monotonic() + timeout
            while len(out) < size:
                left = end - time.monotonic()
                got = self._fetch(size - len(out), max(0.0, left))
                out += got
                if not got and left <= 0:
                    break
                if self._timeout == 0:
                    break
            return bytes(out)

        def write(self, data: Any) -> int:
            if not self.is_open:
                raise serial.PortNotOpenError()
            data = bytes(data)
            r = self._link.call("write", sid=self._sid, data=data, end=False, framing=self._framing())
            if "err" in r:
                raise serial.SerialTimeoutException("Write timeout")
            return len(data)

        def reset_input_buffer(self) -> None:
            self._pending.clear()
            if self.is_open:
                self._fetch(65536, 0.0)

        def reset_output_buffer(self) -> None:
            pass

        def flush(self) -> None:
            pass

        def send_break(self, duration: float = 0.25) -> None:
            pass

        def _update_break_state(self) -> None:
            pass

        def _update_rts_state(self) -> None:
            pass

        def _update_dtr_state(self) -> None:
            pass

        @property
        def cts(self) -> bool:
            return True

        @property
        def dsr(self) -> bool:
            return True

        @property
        def ri(self) -> bool:
            return False

        @property
        def cd(self) -> bool:
            return True

        def cancel_read(self) -> None:
            pass

        def cancel_write(self) -> None:
            pass

    class Serial(real_serial):
        def __new__(cls, port: Any = None, *args: Any, **kwargs: Any) -> Any:
            if _port_name(port):
                return LabsimSerial(port, *args, **kwargs)
            return real_serial.__new__(cls)

    def serial_for_url(url: Any, *args: Any, **kwargs: Any) -> Any:
        if _port_name(url):
            do_open = kwargs.pop("do_not_open", False) is False
            s = LabsimSerial(None, *args, **kwargs)
            s.port = url
            if do_open:
                s.open()
            return s
        return real_for_url(url, *args, **kwargs)

    def comports(include_links: bool = False) -> List[Any]:
        try:
            real = list(real_comports(include_links))
        except TypeError:
            real = list(real_comports())
        bench = bench_ports()
        out = [p for p in real if p.device.upper() not in bench]
        for name, p in bench.items():
            info = ListPortInfo(p["device"])
            info.description = p["description"]
            usb = p.get("usb") or ""
            if usb:
                vid, pid = (int(x, 16) for x in usb.split(":"))
                info.vid, info.pid = vid, pid
                info.serial_number = p.get("serial_number")
                info.manufacturer = p.get("manufacturer") or None
                info.location = "1-%d" % (2 + int(name[3:]) % 6)
                info.hwid = "USB VID:PID=%04X:%04X SER=%s LOCATION=%s" % (vid, pid, info.serial_number or "",
                                                                         info.location)
            else:
                info.hwid = "ACPI\\PNP0501\\%s" % name[3:]
            out.append(info)
        return sorted(out, key=lambda i: i.device)

    serial.Serial = Serial
    serial.serial_for_url = serial_for_url
    list_ports.comports = comports
    if "serial.tools.list_ports_windows" in sys.modules or os.name == "nt":
        try:
            import serial.tools.list_ports_windows as lpw
            lpw.comports = comports
        except ImportError:
            pass


def install() -> None:
    install_serial()
    try:
        from . import dlls
        dlls.install()
    except ImportError:
        pass
