"""Who works the lab at a time: one change at a time, and readings beside it, instrument by instrument.

Kelvin changes the lab one piece of work at a time: a run, a queue's scan, a set or a raw command, a scan for
instruments, a switch of labs. The safety gate's approvals belong to the whole lab (a plan's ranges, a queue's
standing approval, the undo list), so two changes never overlap, from any chat or client. That work holds the lab's
lock (LabLock). It holds every instrument, unless it says which ones it uses (only): a run the instruments its plan
names, a set its instrument, a wait none.

Readings take no part in that. Each instrument has a lock of its own, which a reading has while it reads, so two
readings of one instrument never interleave either. A reading leaves alone an instrument the work holds: a command
in between could upset what the work does there (a lock-in's acquisition, a sweep the instrument takes), so Kelvin
gives that instrument's latest values instead. While a scan runs, another chat reads the gauge the scan doesn't use,
looks things up and edits the queue; Stop takes no lock at all.

The field tests found one lock for everything: a run or a queue held every chat's readings, lookups and queue edits
for its whole length, and the refusal blamed the cable.
"""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

SETTLE_S = 30.0   # the longest work waits, as it starts, for the readings of its instruments under way to finish

HELD, SLOW = "held", "slow"   # why a reading doesn't go ahead (LabLock.reading)


class LabLock:
    """The lab's lock, for the one piece of work that changes the lab now, and each instrument's own, for a reading.
    It takes the place of the RLock the lab had: acquire(blocking, timeout), release and with work as they did."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stack: List[Optional[frozenset]] = []   # the owner's, one per acquisition: what it holds (None: all)
        # (the owner's thread, what it holds now): one value, so a reading on another thread sees both together
        self._now: Tuple[Optional[int], Optional[frozenset]] = (None, None)
        self._buses: Dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ the work that changes the lab

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """As an RLock's. Work that takes it holds every instrument, once the readings of them under way have
        finished, until it says which ones it uses (only). Taken again on its own thread (code that knows nothing of
        what the work said), it holds them all again until that release."""
        if not self._lock.acquire(blocking, timeout):
            return False
        me = threading.get_ident()
        was = self._stack[-1] if self._stack else frozenset()   # free: it held none
        self._stack.append(None)
        self._now = (me, None)
        if was is not None:
            self._settle(lambda name: name not in was)
        return True

    def release(self) -> None:
        me = threading.get_ident()
        if self._now[0] != me or not self._stack:
            raise RuntimeError("cannot release un-acquired lock")
        self._stack.pop()
        self._now = (me, self._stack[-1]) if self._stack else (None, None)
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()

    @contextlib.contextmanager
    def only(self, names: Iterable[str]) -> Iterator[None]:
        """The work that has the lock uses only these instruments for a while (a run: those its plan names), so the
        others can be read meanwhile. Within work that said so already, it adds to what that holds. Afterwards it
        holds what it held before, once the readings of what it takes back have finished."""
        me = threading.get_ident()
        if self._now[0] != me:
            raise RuntimeError("only the work that has the lab's lock says which instruments it uses")
        old = self._stack[-1]
        new = frozenset(names) if old is None else old | frozenset(names)
        self._stack[-1] = new
        self._now = (me, new)
        if old is not None and new != old:
            self._settle(lambda name: name in new and name not in old)
        try:
            yield
        finally:
            self._stack[-1] = old
            self._now = (me, old)
            if old is None:
                self._settle(lambda name: name not in new)

    def holds(self, name: str) -> bool:
        """Whether work on another thread holds this instrument now (its own work reads it as it likes)."""
        owner, scope = self._now
        return owner is not None and owner != threading.get_ident() and (scope is None or name in scope)

    def owner(self) -> Tuple[Optional[int], Optional[frozenset]]:
        """(the thread of the work that has the lock, the instruments it holds: None for all); (None, None): free."""
        return self._now

    # ------------------------------------------------------------------ readings

    def bus(self, name: str) -> threading.RLock:
        """An instrument's own lock: a reading has it while it reads."""
        with self._guard:
            return self._buses.setdefault(name, threading.RLock())

    @contextlib.contextmanager
    def reading(self, name: str, wait: float) -> Iterator[Optional[str]]:
        """Read this instrument now: yields None to go ahead, holding its lock to the end of the with; HELD when the
        work that changes the lab holds it; SLOW when another reading has had it for longer than `wait` seconds (it
        may not be answering). Never waits for the lab's lock."""
        if self.holds(name):
            yield HELD
            return
        bus = self.bus(name)
        if not bus.acquire(timeout=max(0.0, wait)):
            yield SLOW
            return
        try:
            yield HELD if self.holds(name) else None   # the work took it while this waited
        finally:
            bus.release()

    def _settle(self, which: Callable[[str], bool]) -> None:
        """Wait until the readings under way of these instruments have finished: SETTLE_S at most, as a reading that
        takes longer has an instrument that isn't answering (the work then waits on the connection's own lock)."""
        with self._guard:
            buses = [bus for name, bus in self._buses.items() if which(name)]
        end = time.monotonic() + SETTLE_S
        for bus in buses:
            if bus.acquire(timeout=max(0.0, end - time.monotonic())):
                bus.release()
