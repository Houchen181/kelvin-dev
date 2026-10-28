"""labsim stand-in for zhinst.core (LabOne's Python API): ziDAQServer and ziDiscovery for the bench's devices."""
import numpy as np

from labsim.sdkclient import SdkCallError, call, devices

__version__ = "24.10.64599"


class CoreError(RuntimeError):
    pass


def _devs():
    return {d["serial"].lower(): d for d in devices("zhinst")}


class ziDiscovery:
    def findAll(self):
        return list(_devs())

    def find(self, dev):
        d = str(dev).lower()
        if d not in _devs():
            raise CoreError("Device %s not found." % dev)
        return d

    def get(self, dev):
        d = _devs().get(str(dev).lower())
        if d is None:
            raise CoreError("Device %s not found." % dev)
        info = dict(d.get("info") or {})
        info.setdefault("deviceid", d["serial"].upper())
        return info


class ziDAQServer:
    def __init__(self, host="localhost", port=8004, api_level=6):
        if not _devs():
            raise CoreError("ZIAPIServerException: Unable to connect socket (%s:%d): the data server isn't running"
                            % (host, port))
        self.host, self.port, self.api_level = host, port, api_level
        self.connected = set()

    def _split(self, path):
        p = str(path).strip()
        if not p.startswith("/"):
            raise CoreError("ZIAPIException: invalid path %s" % path)
        parts = p.split("/")
        dev = parts[1].lower()
        d = _devs().get(dev)
        if d is None:
            raise CoreError("ZIAPIException with status code: 16387. Path not found: %s" % path)
        return d["key"], "/" + "/".join(parts[2:]).lower()

    def _call(self, path, method, *args):
        key, node = self._split(path)
        try:
            return call(key, method, node, *args)
        except SdkCallError as e:
            raise CoreError(str(e))

    def connectDevice(self, dev, interface="1GbE", params=None):
        if str(dev).lower() not in _devs():
            raise CoreError("ZIAPIException: Device %s not found" % dev)
        self.connected.add(str(dev).lower())

    def disconnectDevice(self, dev):
        self.connected.discard(str(dev).lower())

    def set(self, path, value=None):
        if isinstance(path, (list, tuple)):
            for p, v in path:
                self._call(p, "set", v)
            return
        self._call(path, "set", value)

    setDouble = setInt = setString = set

    def getDouble(self, path):
        return float(self._call(path, "get"))

    def getInt(self, path):
        return int(self._call(path, "get"))

    def getString(self, path):
        return str(self._call(path, "get"))

    def getSample(self, path):
        s = self._call(path, "sample")
        return {k: np.asarray(v) for k, v in s.items()}

    def get(self, path, flat=False, **kw):
        value = self._call(path, "get")
        return {path.lower(): {"value": np.asarray([value])}} if flat else value

    def sync(self):
        pass

    def listNodes(self, path, **kw):
        return []
