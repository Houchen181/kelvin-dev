"""Per-instrument locks (fix plan item 12). One lock for the whole lab froze every chat for the length of a run or a
queue: no reading, no card lookup, no plot, no queue edit and no stop from another chat, and the refusal blamed the
cable (F1 t01 t02, F2 l02 l06, F3 #11). Changes to the lab still go one at a time, as the gate's approvals are the
lab's; readings, lookups, the queue and Stop go beside them, and a reading never sends a command to an instrument a
run is using (lablock.py)."""
import threading
import time

import pytest

from labharness.lablock import HELD, SLOW, LabLock


def elsewhere(fn):
    """fn's answer on another thread (the lock's owner reads what it likes)."""
    out = []
    t = threading.Thread(target=lambda: out.append(fn()))
    t.start()
    t.join(5)
    return out[0]


def test_a_change_holds_the_instruments_it_uses_and_readings_go_beside_it():
    lock = LabLock()
    whole, narrowed, done = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def change():   # a run that uses the lock-in
        with lock:
            seen["own"] = lock.holds("gauge")   # its own thread reads as it likes
            whole.set()
            narrowed.wait(5)
            with lock.only(["li"]):
                narrowed.clear()
                whole.set()
                done.wait(5)
    t = threading.Thread(target=change)
    t.start()
    assert whole.wait(5)
    with lock.reading("gauge", 5.0) as why:   # before it says what it uses: it holds them all, said at once
        t0 = time.monotonic()
        assert why == HELD and time.monotonic() - t0 < 0.5
    whole.clear()
    narrowed.set()
    assert whole.wait(5)
    assert lock.holds("li") and not lock.holds("gauge")
    with lock.reading("gauge", 1.0) as why:
        assert why is None                      # read now, beside the run
    with lock.reading("li", 1.0) as why:
        assert why == HELD                      # never a command between the run's own
    done.set()
    t.join(5)
    assert seen["own"] is False and not lock.holds("li") and lock.owner() == (None, None)


def test_a_change_waits_for_the_readings_under_way_and_readings_never_wait_for_a_change():
    lock = LabLock()
    reading, order = threading.Event(), []

    def read():
        with lock.reading("smu", 1.0) as why:
            assert why is None
            reading.set()
            time.sleep(0.4)
            order.append("read")
    t = threading.Thread(target=read)
    t.start()
    assert reading.wait(5)
    with lock:   # it starts once the reading of its instrument has finished
        order.append("change")
        with lock.reading("smu", 0.1) as why:   # its own thread reads
            assert why is None
    t.join(5)
    assert order == ["read", "change"]


def test_taken_again_inside_a_narrowed_change_it_holds_every_instrument_until_released():
    lock = LabLock()
    with lock:
        with lock.only(["li"]):
            assert not elsewhere(lambda: lock.holds("gauge"))
            with lock:   # code that knows nothing of only(): the whole lab again
                assert elsewhere(lambda: lock.holds("gauge"))
            assert not elsewhere(lambda: lock.holds("gauge"))
            with lock.only(["gauge"]):   # within a narrowed change it adds to what that holds
                assert elsewhere(lambda: lock.holds("gauge")) and elsewhere(lambda: lock.holds("li"))
        assert elsewhere(lambda: lock.holds("gauge"))
    assert not elsewhere(lambda: lock.holds("gauge"))
    with pytest.raises(RuntimeError):
        with lock.only(["li"]):   # only the work that has the lock says what it uses
            pass
    with pytest.raises(RuntimeError):
        lock.release()


def test_a_reading_that_hangs_holds_up_a_change_only_so_long(monkeypatch):
    from labharness import lablock
    monkeypatch.setattr(lablock, "SETTLE_S", 0.3)
    lock = LabLock()
    hung, release = threading.Event(), threading.Event()

    def read():   # an instrument that stopped answering
        with lock.reading("smu", 1.0):
            hung.set()
            release.wait(5)
    t = threading.Thread(target=read, daemon=True)
    t.start()
    assert hung.wait(5)
    with lock.reading("smu", 0.2) as why:   # another reading of it gives up, and says why
        assert why == SLOW
    t0 = time.monotonic()
    with lock:
        waited = time.monotonic() - t0
    release.set()
    t.join(5)
    assert 0.25 <= waited < 2
