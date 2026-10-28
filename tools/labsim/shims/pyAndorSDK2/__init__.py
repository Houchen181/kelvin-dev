"""labsim stand-in for Andor's pyAndorSDK2: atmcd() with SDK2's calls, each returning its error code first."""
import numpy as np

from labsim.sdkclient import SdkCallError, call, devices

__version__ = "2.104.30065"


class atmcd_errors:
    class Error_Codes:
        DRV_SUCCESS = 20002
        DRV_ACQUIRING = 20072
        DRV_IDLE = 20073
        DRV_TEMP_OFF = 20034
        DRV_TEMP_NOT_STABILIZED = 20035
        DRV_TEMP_STABILIZED = 20036
        DRV_TEMP_NOT_REACHED = 20037
        DRV_NOT_INITIALIZED = 20075
        DRV_P1INVALID = 20066
        DRV_NO_NEW_DATA = 20024


class atmcd_codes:
    class Acquisition_Mode:
        SINGLE_SCAN, ACCUMULATE, KINETICS, FAST_KINETICS, RUN_TILL_ABORT = 1, 2, 3, 4, 5

    class Read_Mode:
        FULL_VERTICAL_BINNING, MULTI_TRACK, RANDOM_TRACK, SINGLE_TRACK, IMAGE = 0, 1, 2, 3, 4

    class Trigger_Mode:
        INTERNAL, EXTERNAL = 0, 1


class atmcd:
    """One camera: the first the bench has (SetCurrentCamera picks another)."""

    def __init__(self, userPath=""):
        self._devs = devices("andor2")
        self._key = self._devs[0]["key"] if self._devs else None

    def _c(self, method, *args):
        if self._key is None:
            return 20013   # DRV_VXDNOTINSTALLED: no camera on this computer
        try:
            r = call(self._key, method, *args)
        except SdkCallError:
            return 20075
        return tuple(r) if isinstance(r, list) else r

    def GetAvailableCameras(self):
        return (20002, len(self._devs))

    def GetCameraHandle(self, index):
        return (20002, 100 + int(index)) if 0 <= int(index) < len(self._devs) else (20066, 0)

    def SetCurrentCamera(self, handle):
        i = int(handle) - 100
        if not 0 <= i < len(self._devs):
            return 20066
        self._key = self._devs[i]["key"]
        return 20002

    def Initialize(self, path=""):
        return self._c("Initialize", path)

    def ShutDown(self):
        return self._c("ShutDown")

    def GetDetector(self):
        r = self._c("GetDetector")
        return r if isinstance(r, tuple) else (r, 0, 0)

    def GetHeadModel(self):
        return self._c("GetHeadModel")

    def GetCameraSerialNumber(self):
        return self._c("GetCameraSerialNumber")

    def SetTemperature(self, t):
        return self._c("SetTemperature", int(t))

    def CoolerON(self):
        return self._c("CoolerON")

    def CoolerOFF(self):
        return self._c("CoolerOFF")

    def IsCoolerOn(self):
        return self._c("IsCoolerOn")

    def GetTemperatureF(self):
        return self._c("GetTemperatureF")

    def GetTemperature(self):
        return self._c("GetTemperature")

    def SetExposureTime(self, t):
        return self._c("SetExposureTime", float(t))

    def GetAcquisitionTimings(self):
        return self._c("GetAcquisitionTimings")

    def SetAcquisitionMode(self, m):
        return self._c("SetAcquisitionMode", int(m))

    def SetReadMode(self, m):
        return self._c("SetReadMode", int(m))

    def SetImage(self, hbin, vbin, hstart, hend, vstart, vend):
        return self._c("SetImage", hbin, vbin, hstart, hend, vstart, vend)

    def SetShutter(self, typ, mode, closing, opening):
        return self._c("SetShutter", typ, mode, closing, opening)

    def SetTriggerMode(self, m):
        return 20002

    def StartAcquisition(self):
        return self._c("StartAcquisition")

    def WaitForAcquisition(self):
        return self._c("WaitForAcquisition")

    def AbortAcquisition(self):
        return self._c("AbortAcquisition")

    def GetStatus(self):
        return self._c("GetStatus")

    def GetAcquiredData(self, size):
        r = self._c("GetAcquiredData", int(size))
        if isinstance(r, tuple):
            return (r[0], np.asarray(r[1], dtype=np.int32))
        return (r, np.zeros(0, dtype=np.int32))

    def GetMostRecentImage(self, size):
        return self.GetAcquiredData(size)
