"""labsim stand-in for Swabian's TimeTagger module: createTimeTagger, Countrate, Counter, CountBetweenMarkers,
Correlation. Counts come from the bench (Poisson, at the rate the detector sees when the data is asked for)."""
import time

import numpy as np

from labsim.sdkclient import SdkCallError, call, devices

__version__ = "2.17.2"


def scanTimeTagger():
    return [d["serial"] for d in devices("timetagger")]


class TimeTaggerBase:
    def __init__(self, dev):
        self._dev, self._key = dev, dev["key"]
        self._free = False

    def getSerial(self):
        return self._dev["serial"]

    def getModel(self):
        return self._dev.get("info", {}).get("model", "Time Tagger Ultra")

    def setTriggerLevel(self, ch, v):
        call(self._key, "set_level", int(ch), float(v))

    def getTriggerLevel(self, ch):
        return call(self._key, "get_level", int(ch))

    def sync(self, timeout=-1):
        return True

    def setTestSignal(self, channels, enable=True):
        pass

    def getChannelList(self, type=0):
        return list(range(1, 9))

    def setInputDelay(self, ch, delay):
        pass

    def setDeadtime(self, ch, deadtime):
        return deadtime

    def reset(self):
        pass


class TimeTagger(TimeTaggerBase):
    pass


def createTimeTagger(serial="", resolution=0):
    devs = devices("timetagger")
    pick = next((d for d in devs if not serial or d["serial"] == serial), None)
    if pick is None:
        raise RuntimeError("No Time Tagger found%s." % (" with serial %s" % serial if serial else ""))
    return TimeTagger(pick)


def createTimeTaggerVirtual():
    raise RuntimeError("virtual Time Taggers aren't modelled by labsim")


def freeTimeTagger(tagger):
    tagger._free = True
    return True


class _Measurement:
    def __init__(self, tagger):
        self._tt = tagger
        self._start = None
        self._duration = None
        self.start()

    def start(self):
        self._start, self._duration = time.monotonic(), None

    def startFor(self, capture_duration, clear=True):
        self._start, self._duration = time.monotonic(), capture_duration * 1e-12

    def stop(self):
        if self._start is not None and self._duration is None:
            self._duration = time.monotonic() - self._start

    def clear(self):
        self._start = time.monotonic()

    def isRunning(self):
        return self._start is not None and (self._duration is None or time.monotonic() < self._start + self._duration)

    def waitUntilFinished(self, timeout=-1):
        if self._duration is None:
            return False
        left = self._start + self._duration - time.monotonic()
        if timeout is not None and timeout >= 0 and left * 1000 > timeout:
            time.sleep(timeout / 1000.0)
            return False
        if left > 0:
            time.sleep(left)
        return True

    def _elapsed(self):
        if self._start is None:
            return 0.0
        t = time.monotonic() - self._start
        return min(t, self._duration) if self._duration is not None else t

    def getCaptureDuration(self):
        return int(self._elapsed() * 1e12)

    def _rates(self, channels):
        try:
            return call(self._tt._key, "rates", [int(c) for c in channels])
        except SdkCallError as e:
            raise RuntimeError(str(e))


class Countrate(_Measurement):
    def __init__(self, tagger, channels):
        self._ch = list(channels)
        super().__init__(tagger)

    def getData(self):
        t = self._elapsed()
        if t <= 0:
            return np.zeros(len(self._ch))
        rates = self._rates(self._ch)
        return np.random.default_rng().poisson(np.array(rates) * t) / t

    def getCountsTotal(self):
        t = self._elapsed()
        return np.random.default_rng().poisson(np.array(self._rates(self._ch)) * t)


class Counter(_Measurement):
    def __init__(self, tagger, channels, binwidth=1000000000, n_values=1):
        self._ch, self._bw, self._n = list(channels), int(binwidth), int(n_values)
        super().__init__(tagger)

    def getData(self, rolling=True):
        rates = np.array(self._rates(self._ch))
        filled = min(self._n, int(self._elapsed() / (self._bw * 1e-12)))
        out = np.zeros((len(self._ch), self._n), dtype=np.int32)
        if filled:
            out[:, self._n - filled:] = np.random.default_rng().poisson(rates[:, None] * self._bw * 1e-12,
                                                                         (len(self._ch), filled))
        return out

    def getIndex(self):
        return np.arange(self._n, dtype=np.int64) * self._bw

    def getDataNormalized(self, rolling=True):
        return self.getData(rolling) / (self._bw * 1e-12)


class CountBetweenMarkers(_Measurement):
    def __init__(self, tagger, click_channel, begin_channel, end_channel=0, n_values=1000):
        self._ch, self._n = int(click_channel), int(n_values)
        super().__init__(tagger)

    def getData(self):
        t = self._elapsed()
        rate = self._rates([self._ch])[0]
        bins = min(self._n, max(1, int(t * 1e3)))
        out = np.zeros(self._n, dtype=np.int32)
        out[:bins] = np.random.default_rng().poisson(rate * t / bins, bins)
        return out

    def getBinWidths(self):
        t = self._elapsed()
        bins = min(self._n, max(1, int(t * 1e3)))
        w = np.zeros(self._n, dtype=np.int64)
        w[:bins] = int(t / bins * 1e12)
        return w

    def getIndex(self):
        return np.cumsum(self.getBinWidths()) - self.getBinWidths()


class Correlation(_Measurement):
    def __init__(self, tagger, channel_1, channel_2=None, binwidth=1000, n_bins=1000):
        self._bw, self._n = int(binwidth), int(n_bins)
        super().__init__(tagger)

    def getData(self):
        try:
            return np.asarray(call(self._tt._key, "g2", float(self._bw), self._n, max(self._elapsed(), 1e-3)))
        except SdkCallError as e:
            raise RuntimeError(str(e))

    def getDataNormalized(self):
        d = self.getData().astype(float)
        edge = np.concatenate([d[: self._n // 10], d[-self._n // 10:]])
        return d / max(edge.mean(), 1e-12)

    def getIndex(self):
        return (np.arange(self._n) - self._n // 2) * self._bw
