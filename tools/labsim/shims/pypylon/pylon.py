"""labsim stand-in for pypylon.pylon: TlFactory, DeviceInfo, InstantCamera with GenICam nodes, GrabResult."""
import os

import numpy as np

from labsim.sdkclient import SdkCallError, call, devices

from . import genicam

GrabStrategy_OneByOne, GrabStrategy_LatestImageOnly, GrabStrategy_LatestImages, GrabStrategy_UpcomingImage = 0, 1, 2, 3
TimeoutHandling_Return, TimeoutHandling_ThrowException = 0, 1
PixelType_Mono8, PixelType_Mono12 = 17301505, 17825797
_NODES = ("ExposureTime", "Gain", "Width", "Height", "PixelFormat", "OffsetX", "OffsetY", "AcquisitionFrameRate")


def _sdk(key, method, *args):
    try:
        return call(key, method, *args)
    except SdkCallError as e:
        genicam._raise(e.kind, str(e))


class DeviceInfo:
    def __init__(self, d=None):
        d = d or {}
        self._d = d
        self.key = d.get("key")

    def GetSerialNumber(self):
        return self._d.get("serial", "")

    def GetModelName(self):
        return self._d.get("info", {}).get("model", "")

    def GetFriendlyName(self):
        return self._d.get("info", {}).get("friendly", "")

    def GetFullName(self):
        return "%s#%s" % (self.GetModelName(), self.GetSerialNumber())

    def GetDeviceClass(self):
        return self._d.get("info", {}).get("class", "BaslerUsb")

    def GetVendorName(self):
        return "Basler"

    def GetUserDefinedName(self):
        return ""

    def SetSerialNumber(self, s):
        self._d["serial"] = s

    def __repr__(self):
        return "<DeviceInfo %s %s>" % (self.GetModelName(), self.GetSerialNumber())


class _Device:
    def __init__(self, info):
        self.info = info


class TlFactory:
    _instance = None

    @staticmethod
    def GetInstance():
        if TlFactory._instance is None:
            TlFactory._instance = TlFactory()
        return TlFactory._instance

    def EnumerateDevices(self, filter=None):
        found = [DeviceInfo(d) for d in devices("pylon")]
        if filter:
            want = [f.GetSerialNumber() for f in filter if f.GetSerialNumber()]
            if want:
                found = [d for d in found if d.GetSerialNumber() in want]
        return tuple(found)

    def CreateDevice(self, info):
        if isinstance(info, str):
            info = next((d for d in self.EnumerateDevices() if d.GetSerialNumber() == info), None)
        if info is None or not any(d.GetSerialNumber() == info.GetSerialNumber() for d in self.EnumerateDevices()):
            raise genicam.RuntimeException("No device is available or no device contains the provided device info "
                                           "properties.")
        return _Device(next(d for d in self.EnumerateDevices() if d.GetSerialNumber() == info.GetSerialNumber()))

    def CreateFirstDevice(self, info=None):
        found = self.EnumerateDevices([info] if info is not None else None)
        if not found:
            raise genicam.RuntimeException("No device is available or no device contains the provided device info "
                                           "properties.")
        return _Device(found[0])


class _Node:
    def __init__(self, cam, name):
        self._cam, self._name = cam, name

    def GetValue(self):
        self._cam._need_open()
        return _sdk(self._cam._key, "get", self._name)

    def SetValue(self, v):
        self._cam._need_open()
        _sdk(self._cam._key, "set", self._name, v)

    def _limits(self):
        return _sdk(self._cam._key, "limits", self._name)

    def GetMin(self):
        return self._limits()[0]

    def GetMax(self):
        return self._limits()[1]

    def GetInc(self):
        return 1

    def IsReadable(self):
        return self._cam.IsOpen()

    def IsWritable(self):
        return self._cam.IsOpen()

    def ToString(self):
        return str(self.GetValue())

    Value = property(GetValue, SetValue)
    Min = property(GetMin)
    Max = property(GetMax)


class GrabResult:
    def __init__(self, ok, array=None, error=""):
        self._ok, self._array, self._error = ok, array, error

    def GrabSucceeded(self):
        return self._ok

    @property
    def Array(self):
        return self._array

    def GetArray(self):
        return self._array

    @property
    def Width(self):
        return int(self._array.shape[1]) if self._array is not None else 0

    @property
    def Height(self):
        return int(self._array.shape[0]) if self._array is not None else 0

    def GetErrorDescription(self):
        return self._error

    ErrorDescription = property(GetErrorDescription)

    def GetErrorCode(self):
        return 0 if self._ok else 0xE1000014

    def GetNumberOfSkippedImages(self):
        return 0

    def Release(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.Release()


class InstantCamera:
    def __init__(self, device=None):
        self._device = device
        self._open = False
        self._grabbing = 0
        self._owner = "%d.%d" % (os.getpid(), id(self))

    @property
    def _key(self):
        if self._device is None:
            raise genicam.RuntimeException("The camera is not attached to a device.")
        return self._device.info.key

    def Attach(self, device):
        self._device = device

    def IsPylonDeviceAttached(self):
        return self._device is not None

    def DetachDevice(self):
        self._device = None

    def DestroyDevice(self):
        self.Close()
        self._device = None

    def GetDeviceInfo(self):
        return self._device.info

    def Open(self):
        _sdk(self._key, "open", self._owner)
        self._open = True

    def Close(self):
        if self._open:
            try:
                call(self._key, "close", self._owner)
            except SdkCallError:
                pass
        self._open = False

    def IsOpen(self):
        return self._open

    def _need_open(self):
        if not self._open:
            raise genicam.AccessException("Node is not readable/writable: the camera is not open.")

    def __getattr__(self, name):
        if name in _NODES:
            return _Node(self, name)
        raise AttributeError(name)

    def GrabOne(self, timeout, strategy=None):
        if not self._open:
            self.Open()
        r = _sdk(self._key, "grab", int(timeout))
        if not r.get("ok"):
            raise genicam.TimeoutException("Grab timed out. The acquisition did not complete within %d ms." % timeout)
        return GrabResult(True, np.asarray(r["array"]))

    def StartGrabbing(self, strategy=GrabStrategy_OneByOne, *a):
        if not self._open:
            self.Open()
        self._grabbing = -1

    def StartGrabbingMax(self, n, strategy=GrabStrategy_OneByOne):
        self.StartGrabbing()
        self._grabbing = int(n)

    def IsGrabbing(self):
        return self._grabbing != 0

    def StopGrabbing(self):
        self._grabbing = 0

    def RetrieveResult(self, timeout, handling=TimeoutHandling_ThrowException):
        if not self._grabbing:
            raise genicam.RuntimeException("The grab engine thread is not running.")
        try:
            res = self.GrabOne(timeout)
        except genicam.TimeoutException:
            if handling == TimeoutHandling_Return:
                return GrabResult(False, None, "timeout")
            raise
        if self._grabbing > 0:
            self._grabbing -= 1
        return res

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.Close()


class ImageFormatConverter:
    OutputPixelFormat = PixelType_Mono8

    def Convert(self, result):
        return result


def FeaturePersistence():   # pragma: no cover - not modelled
    raise genicam.RuntimeException("FeaturePersistence is not available on a labsim bench")
