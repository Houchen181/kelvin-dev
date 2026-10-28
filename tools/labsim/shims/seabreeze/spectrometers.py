"""seabreeze.spectrometers: list_devices() and Spectrometer, for the bench's Ocean spectrometers."""
import numpy as np

from labsim.sdkclient import SdkCallError, call, devices


class SeaBreezeError(Exception):
    pass


class SeaBreezeDevice:
    def __init__(self, d):
        self._d = d
        self.serial_number = d["serial"]
        self.model = (d.get("info") or {}).get("model", "USB4000")

    def __repr__(self):
        return "<SeaBreezeDevice %s:%s>" % (self.model, self.serial_number)


def list_devices():
    return [SeaBreezeDevice(d) for d in devices("seabreeze")]


class Spectrometer:
    def __init__(self, device):
        self._dev = device
        self._key = device._d["key"]
        self._open = True

    @classmethod
    def from_first_available(cls):
        devs = list_devices()
        if not devs:
            raise SeaBreezeError("No unopened device found.")
        return cls(devs[0])

    @classmethod
    def from_serial_number(cls, serial=None):
        if serial is None:
            return cls.from_first_available()
        for d in list_devices():
            if d.serial_number == serial:
                return cls(d)
        raise SeaBreezeError("No device attached with serial number '%s'." % serial)

    def _c(self, method, *args):
        if not self._open:
            raise SeaBreezeError("Device not opened")
        try:
            return call(self._key, method, *args)
        except SdkCallError as e:
            raise SeaBreezeError(str(e))

    @property
    def model(self):
        return self._dev.model

    @property
    def serial_number(self):
        return self._dev.serial_number

    @property
    def pixels(self):
        return len(self.wavelengths())

    @property
    def integration_time_micros_limits(self):
        return tuple(self._c("limits"))

    @property
    def max_intensity(self):
        return 65535.0

    def integration_time_micros(self, integration_time_micros):
        self._c("integration_time_micros", int(integration_time_micros))

    def wavelengths(self):
        return np.asarray(self._c("wavelengths"))

    def intensities(self, correct_dark_counts=False, correct_nonlinearity=False):
        s = np.asarray(self._c("intensities"), dtype=float)
        return s - 1500.0 if correct_dark_counts else s

    def spectrum(self, correct_dark_counts=False, correct_nonlinearity=False):
        return np.vstack([self.wavelengths(), self.intensities(correct_dark_counts, correct_nonlinearity)])

    def close(self):
        self._open = False
