"""labsim stand-in for NI's nidaqmx: system.System.local().devices, Task with AI/AO/DI/DO/counter channels."""
import re

from labsim.sdkclient import SdkCallError, call, devices

from . import constants, system  # noqa: F401
from .errors import DaqError

__version__ = "1.0.2"


def _dev(name):
    for d in devices("nidaq"):
        if d["serial"].lower() == name.lower():
            return d
    raise DaqError("Device identifier is invalid.\nDevice Specified: %s" % name, -200220)


class _Chans:
    def __init__(self, task, kind):
        self._task, self._kind = task, kind

    def _add(self, physical, **kw):
        for part in str(physical).split(","):
            m = re.fullmatch(r"\s*([^/]+)/(ai|ao|ctr|port\d+/line)(\d+)(?::(\d+))?\s*", part)
            if not m:
                raise DaqError("Physical channel name specified is invalid: %s" % part, -201003)
            dev = _dev(m.group(1))
            lo = int(m.group(3))
            hi = int(m.group(4)) if m.group(4) else lo
            for n in range(lo, hi + 1):
                self._task._chans.append((self._kind, dev["key"], n, kw))

    def add_ai_voltage_chan(self, physical_channel, name_to_assign_to_channel="", terminal_config=None,
                            min_val=-5.0, max_val=5.0, units=None, custom_scale_name=""):
        self._add(physical_channel, lo=min_val, hi=max_val)

    def add_ao_voltage_chan(self, physical_channel, name_to_assign_to_channel="", min_val=-10.0, max_val=10.0,
                            units=None, custom_scale_name=""):
        self._add(physical_channel, lo=min_val, hi=max_val)

    def add_di_chan(self, lines, name_to_assign_to_lines="", line_grouping=None):
        self._add(lines)

    def add_do_chan(self, lines, name_to_assign_to_lines="", line_grouping=None):
        self._add(lines)

    def add_ci_count_edges_chan(self, counter, name_to_assign_to_channel="", edge=None, initial_count=0,
                                count_direction=None):
        self._add(counter)


class _Timing:
    def __init__(self, task):
        self._task = task

    def cfg_samp_clk_timing(self, rate, source="", active_edge=None, sample_mode=None, samps_per_chan=1000):
        self._task._rate, self._task._samples = float(rate), int(samps_per_chan)


class Task:
    def __init__(self, new_task_name=""):
        self._chans = []
        self._rate, self._samples = 1000.0, 1
        self._started = None
        self.ai_channels = _Chans(self, "ai")
        self.ao_channels = _Chans(self, "ao")
        self.di_channels = _Chans(self, "di")
        self.do_channels = _Chans(self, "do")
        self.ci_channels = _Chans(self, "ci")
        self.timing = _Timing(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def start(self):
        import time
        self._started = time.monotonic()

    def stop(self):
        pass

    def close(self):
        self._chans = []

    def _c(self, key, method, *args):
        try:
            return call(key, method, *args)
        except SdkCallError as e:
            raise DaqError(str(e), e.code if isinstance(e.code, int) else -200000)

    def read(self, number_of_samples_per_channel=None, timeout=10.0):
        if not self._chans:
            raise DaqError("Task contains no channels", -200478)
        kind = self._chans[0][0]
        n = 1 if number_of_samples_per_channel in (None, -1) else int(number_of_samples_per_channel)
        if kind == "ai":
            key = self._chans[0][1]
            data = self._c(key, "read_ai", [c[2] for c in self._chans], n, self._rate)
            for row, c in zip(data, self._chans):
                lo, hi = c[3].get("lo", -10), c[3].get("hi", 10)
                row[:] = [min(hi, max(lo, v)) for v in row]
            if number_of_samples_per_channel is None:
                return data[0][0] if len(data) == 1 else [row[0] for row in data]
            return data[0] if len(data) == 1 else data
        if kind == "ci":
            import time
            elapsed = time.monotonic() - (self._started or time.monotonic())
            return int(self._c(self._chans[0][1], "count", max(elapsed, 1e-3)))
        if kind == "di":
            return False if number_of_samples_per_channel is None else [False] * n
        raise DaqError("Read operation is not supported for output channels", -200429)

    def write(self, data, auto_start=False, timeout=10.0):
        kind = self._chans[0][0] if self._chans else None
        if kind == "ao":
            values = data if isinstance(data, (list, tuple)) else [data]
            for c, v in zip(self._chans, values):
                v = v[-1] if isinstance(v, (list, tuple)) else v
                if not c[3].get("lo", -10) <= float(v) <= c[3].get("hi", 10):
                    raise DaqError("Requested value is not a supported value for this property.", -200077)
                self._c(c[1], "write_ao", [c[2]], [float(v)])
            return len(values)
        if kind == "do":
            return 1
        raise DaqError("Task contains no output channels", -200478)
