"""labsim stand-in for Andor's pyAndorSDK3: AndorSDK3().GetCamera(i), features as attributes, acquire()."""
import numpy as np

from labsim.sdkclient import SdkCallError, call, devices

__version__ = "1.1.0"


class ATCoreException(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class CameraException(ATCoreException):
    pass


class Acquisition:
    def __init__(self, image):
        self.image = image

    def show(self):   # pragma: no cover - plotting isn't modelled
        pass


class Camera:
    def __init__(self, key):
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_open", True)

    def _call(self, method, *args):
        if not self._open:
            raise CameraException("camera is closed")
        try:
            return call(self._key, method, *args)
        except SdkCallError as e:
            raise ATCoreException(str(e), e.code)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._call("get", name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self._call("set", name, value)

    def acquire(self, timeout=10000, frame_count=1):
        return Acquisition(np.asarray(self._call("acquire", int(timeout))))

    def close(self):
        object.__setattr__(self, "_open", False)


class AndorSDK3:
    def __init__(self):
        self._devices = devices("andor3")

    @property
    def DeviceCount(self):
        return len(self._devices)

    @property
    def SoftwareVersion(self):
        return "3.15.30092.0"

    def GetCamera(self, index):
        if not 0 <= int(index) < len(self._devices):
            raise ATCoreException("ATCore error 21 (AT_ERR_INVALIDHANDLE): no camera %s" % index, 21)
        return Camera(self._devices[int(index)]["key"])
