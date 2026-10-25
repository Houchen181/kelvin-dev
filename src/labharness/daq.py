"""DAQ cards: National Instruments multifunction devices (USB-600x, USB-62xx, PCIe/PXIe-63xx, cDAQ modules) through
NI-DAQmx. Address nidaq://Dev1 (the device's name in NI MAX), optionally ?terminal=rse|nrse|diff|pseudodiff&range=10
for the analog inputs (default: the device's own terminal configuration, +-10 V).

Paths (the card's "GET <path>" / "SET <path> <value>"):
    ai<N>          analog input N, volts (averaged over `samples` samples at `rate`)
    ao<N>          analog output N, volts. NI-DAQmx can't read an output back, so Kelvin knows it once it set it
    do<N>          digital output port0/line<N> (on/off); also set only
    di<N>          digital input port0/line<N>
    ctr<N>         edges counted on counter N for `integration` seconds, as counts/s
    samples, rate, integration    how the readings above are taken (Kelvin's own; nothing is sent)

Connecting only checks that the device is there; outputs keep what they are putting out."""
from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any, List, Optional, Tuple

from . import envs
from .adapters import AdapterError
from .cards import Card
from .drivers import DriverBackend

_CHANNEL = re.compile(r"^(ai|ao|di|do|ctr)(\d+)$")
TERMINALS = {"rse": ("RSE",), "nrse": ("NRSE",), "diff": ("DIFF", "DIFFERENTIAL"),
             "pseudodiff": ("PSEUDO_DIFF", "PSEUDODIFFERENTIAL")}


def _module(module: Any, name: str) -> Any:
    if module is not None:
        return module
    try:
        import nidaqmx   # type: ignore
        import nidaqmx.system   # type: ignore  # noqa: F401
        return nidaqmx
    except ImportError as e:
        raise AdapterError("%s needs NI's DAQmx driver (NI's installer), then nidaqmx: %s"
                           % (name, envs.missing("nidaqmx"))) from e


class NIDAQ(DriverBackend):
    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        q = dict(urllib.parse.parse_qsl(url.query))
        self.device = (url.netloc or url.path).strip("/")
        if not self.device:
            raise AdapterError("%s: a DAQ address is nidaq://Dev1 (the device name NI MAX shows)" % name)
        self.daq = _module(module, name)
        terminal = q.get("terminal", "").lower()
        if terminal and terminal not in TERMINALS:
            raise AdapterError("%s: terminal is one of %s" % (name, ", ".join(TERMINALS)))
        self.terminal = terminal
        try:
            self.range = float(q.get("range", 10))
        except ValueError:
            raise AdapterError("%s: range is the analog inputs' range in volts, e.g. range=10" % name)
        try:
            names = [d.name for d in self.daq.system.System.local().devices]
        except Exception as e:
            raise AdapterError("%s: NI-DAQmx could not list devices: %s" % (name, e)) from e
        if self.device not in names:
            raise AdapterError("%s: no NI device %r (NI-DAQmx sees: %s)" % (name, self.device, ", ".join(names) or "none"))
        self.samples, self.rate, self.integration = 1, 1000.0, 0.1

    def _term(self) -> dict:
        if not self.terminal:
            return {}
        consts = self.daq.constants.TerminalConfiguration
        for attr in TERMINALS[self.terminal]:
            if hasattr(consts, attr):
                return {"terminal_config": getattr(consts, attr)}
        raise AdapterError("%s: this NI-DAQmx has no %s terminal configuration" % (self.name, self.terminal))

    def _channel(self, path: str) -> Tuple[str, int]:
        m = _CHANNEL.match(path)
        if not m:
            raise AdapterError("%s: no %r (paths: ai0, ao0, di0, do0, ctr0, samples, rate, integration)" % (self.name, path))
        return m.group(1), int(m.group(2))

    def get(self, path: str) -> Any:
        if path in ("samples", "rate", "integration"):
            return getattr(self, path)
        kind, n = self._channel(path)
        if kind in ("ao", "do"):
            raise AdapterError("%s: NI-DAQmx can't read %s back; Kelvin knows it once it has set it" % (self.name, path))
        try:
            with self.daq.Task() as task:
                if kind == "ai":
                    task.ai_channels.add_ai_voltage_chan("%s/ai%d" % (self.device, n), min_val=-self.range,
                                                         max_val=self.range, **self._term())
                    if self.samples > 1:
                        task.timing.cfg_samp_clk_timing(self.rate, samps_per_chan=self.samples)
                        data: List[float] = list(task.read(number_of_samples_per_channel=self.samples))
                        return sum(data) / len(data)
                    return float(task.read())
                if kind == "di":
                    task.di_channels.add_di_chan("%s/port0/line%d" % (self.device, n))
                    return 1 if task.read() else 0
                task.ci_channels.add_ci_count_edges_chan("%s/ctr%d" % (self.device, n))
                task.start()
                time.sleep(self.integration)
                counts = float(task.read())
                task.stop()
                return counts / self.integration
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("%s: %s/%s: %s" % (self.name, self.device, path, str(e).splitlines()[0])) from e

    def set(self, path: str, value: Any) -> None:
        if path == "samples":
            if not 1 <= int(value) <= 1_000_000:
                raise AdapterError("%s: samples is 1 to 1000000" % self.name)
            self.samples = int(value)
            return
        if path in ("rate", "integration"):
            if float(value) <= 0:
                raise AdapterError("%s: %s must be above 0" % (self.name, path))
            setattr(self, path, float(value))
            return
        kind, n = self._channel(path)
        if kind not in ("ao", "do"):
            raise AdapterError("%s: %s is an input; it can't be set" % (self.name, path))
        try:
            with self.daq.Task() as task:
                if kind == "ao":
                    task.ao_channels.add_ao_voltage_chan("%s/ao%d" % (self.device, n), min_val=-10.0, max_val=10.0)
                    task.write(float(value), auto_start=True)
                else:
                    task.do_channels.add_do_chan("%s/port0/line%d" % (self.device, n))
                    task.write(bool(value), auto_start=True)
                task.stop()   # an output keeps its value after its task ends
        except Exception as e:
            raise AdapterError("%s: %s/%s: %s" % (self.name, self.device, path, str(e).splitlines()[0])) from e

    def identity(self) -> str:
        try:
            dev = self.daq.system.Device(self.device)
            return "National Instruments,%s,%s" % (dev.product_type, "%X" % dev.dev_serial_num
                                                   if isinstance(dev.dev_serial_num, int) else dev.dev_serial_num)
        except Exception:
            return "National Instruments,DAQ,%s" % self.device


def discover_nidaq(module: Any = None) -> List[Tuple[str, str]]:
    """(address, identity) of each NI-DAQmx device. Only asks the driver; nothing reaches the devices."""
    try:
        daq = _module(module, "scan")
        found = []
        for d in daq.system.System.local().devices:
            serial = d.dev_serial_num
            found.append(("nidaq://%s" % d.name, "National Instruments,%s,%s"
                          % (d.product_type, "%X" % serial if isinstance(serial, int) else serial)))
        return found
    except Exception:
        return []
