"""labsim stand-in for Quantum Design's MultiPyVu: Client(host, port) for the bench's PPMS / DynaCool."""
import enum
import time

from labsim.sdkclient import SdkCallError, call, devices

__version__ = "2.2.1"


class MultiPyVuError(Exception):
    pass


class _Temperature:
    class approach_mode(enum.IntEnum):
        fast_settle = 0
        no_overshoot = 1

    class state_code_dict(dict):
        pass


class _Field:
    class approach_mode(enum.IntEnum):
        linear = 0
        no_overshoot = 1
        oscillate = 2

    class driven_mode(enum.IntEnum):
        persistent = 0
        driven = 1


class _Chamber:
    class mode(enum.IntEnum):
        seal = 0
        purge_seal = 1
        vent_seal = 2
        pump_continuous = 3
        vent_continuous = 4
        high_vacuum = 5


class _Subsystem(enum.IntFlag):
    no_subsystem = 0
    temperature = 1
    field = 2
    chamber = 4


class Client:
    temperature = _Temperature
    field = _Field
    chamber = _Chamber
    subsystem = _Subsystem

    def __init__(self, host="localhost", port=5000, socket_timeout=None):
        self.host, self.port = host, int(port)
        self._key = None

    def open(self):
        devs = devices("ppms")
        want = "%s:%d" % (self.host, self.port)
        pick = next((d for d in devs if d["serial"] in (want, "localhost:%d" % self.port, "127.0.0.1:%d" % self.port)),
                    None) or (devs[0] if len(devs) == 1 and self.host in ("localhost", "127.0.0.1") else None)
        if pick is None:
            raise MultiPyVuError("No connection could be made to the MultiPyVu server at %s (is MultiVu running, "
                                 "with the server started?)" % want)
        self._key = pick["key"]
        return self

    def close_client(self):
        self._key = None

    def close_server(self):
        self._key = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close_client()

    def _c(self, method, *args):
        if self._key is None:
            raise MultiPyVuError("The client is not connected; call open() first")
        try:
            r = call(self._key, method, *args)
        except SdkCallError as e:
            raise MultiPyVuError(str(e))
        return tuple(r) if isinstance(r, list) else r

    def get_temperature(self):
        return self._c("get_temperature")

    def set_temperature(self, set_point, rate_per_minute, approach_mode=0):
        self._c("set_temperature", float(set_point), float(rate_per_minute), str(getattr(approach_mode, "name",
                                                                                           approach_mode)))

    def get_field(self):
        return self._c("get_field")

    def set_field(self, set_point, rate_per_sec, approach_mode=0, driven_mode=None):
        self._c("set_field", float(set_point), float(rate_per_sec), str(getattr(approach_mode, "name", approach_mode)),
                str(getattr(driven_mode, "name", driven_mode or "driven")))

    def get_chamber(self):
        return self._c("get_chamber")

    def set_chamber(self, mode):
        self._c("set_chamber", str(getattr(mode, "name", mode)))

    def get_resistance(self, bridge_number):
        return self._c("get_resistance", int(bridge_number))

    def wait_for(self, delay_sec, timeout_sec=0, bitmask=0):
        time.sleep(float(delay_sec))
        end = time.monotonic() + (float(timeout_sec) or 1e9)
        while time.monotonic() < end:
            t_ok = not (int(bitmask) & 1) or self.get_temperature()[1] == "Stable"
            f_ok = not (int(bitmask) & 2) or self.get_field()[1].startswith("Holding")
            if t_ok and f_ok:
                return
            time.sleep(1.0)

    @property
    def resistivity(self):
        return self
