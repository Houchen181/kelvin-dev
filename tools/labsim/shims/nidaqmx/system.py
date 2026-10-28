"""nidaqmx.system: the devices NI MAX would show."""
from labsim.sdkclient import devices

from .errors import DaqError


class Device:
    def __init__(self, name):
        self.name = name
        d = next((x for x in devices("nidaq") if x["serial"].lower() == str(name).lower()), None)
        if d is None:
            raise DaqError("Device identifier is invalid.\nDevice Specified: %s" % name, -200220)
        self._info = d.get("info") or {}

    @property
    def product_type(self):
        return self._info.get("product_type", "USB-6363")

    @property
    def dev_serial_num(self):
        return int(self._info.get("serial", 0x1F2E3D4C))

    def reset_device(self):
        pass


class _Devices(list):
    @property
    def device_names(self):
        return [d.name for d in self]


class System:
    @staticmethod
    def local():
        return System()

    @property
    def devices(self):
        return _Devices(Device(d["serial"]) for d in devices("nidaq"))

    @property
    def driver_version(self):
        return (23, 8, 0)
