"""An instrument = its card + a backend (a twin now, VISA/serial/... in Phase 1).

These methods talk to hardware with no safety checks. Only the safety gate
and the run engine call them; the agent never gets an Instrument.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .cards import Card, ParamSpec
from .labconfig import Guard, InstrumentConfig


OFF_TEXT = "%s is switched off in the Lab panel: Kelvin sends it nothing until a person switches it on"


class InstrumentOff(RuntimeError):
    """A command for an instrument a person switched off (Lab panel)."""


class OffBackend:
    """Stands in for a switched-off instrument's connection: it sends nothing, and refuses every command."""
    simulated = False

    def __init__(self, name: str):
        self.name = name

    def write(self, command: str) -> None:
        raise InstrumentOff(OFF_TEXT % self.name)

    def query(self, command: str) -> str:
        raise InstrumentOff(OFF_TEXT % self.name)

    def close(self) -> None:
        pass


class Instrument:
    def __init__(self, name: str, card: Card, backend: Any, config: InstrumentConfig):
        self.name = name
        self.card = card
        self.backend = backend
        self.config = config
        self._last: Dict[str, Any] = {}   # what Kelvin set, for parameters with no read-back
        # the mounted device's protection (protection.py): it only ever narrows the lab's limits and rates
        self.device_name: Optional[str] = None
        self.device_limits: Dict[str, Tuple[float, float]] = {}
        self.device_rates: Dict[str, float] = {}
        self.device_guards: Dict[str, List[Guard]] = {}

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.backend, "simulated", False))

    @property
    def enabled(self) -> bool:
        """False when a person switched it off in the Lab panel: then nothing is sent to it."""
        return not isinstance(self.backend, OffBackend)

    @property
    def trust(self) -> str:
        return self.config.trust

    def spec(self, param: str) -> ParamSpec:
        return self.card.param(param)

    def read(self, param: str) -> Any:
        """The parameter's value; None if the card says to read it only while a switch is on and it is off."""
        spec = self.spec(param)
        if spec.read_when is not None and not self.read(spec.read_when):
            return None
        if spec.get_cmd is None:
            if param in self._last:
                return self._last[param]
            if spec.default is not None:
                return spec.default
            raise ValueError("%s.%s cannot be read" % (self.name, param))
        if spec.type == "trace":
            return self._trace(spec)
        return spec.from_wire(self.backend.query(spec.get_cmd.render()))

    def _trace(self, spec: ParamSpec) -> List[float]:
        """A trace: as text, or as a block of binary numbers (binary: on the card) where the connection can read one;
        then in its units through the card's y expression of raw and the instrument's own scale readings."""
        command = spec.get_cmd.render()
        if spec.binary and hasattr(self.backend, "query_binary"):
            values = self.backend.query_binary(command, spec.binary)
        else:
            values = spec.from_wire(self.backend.query(command))
        if not spec.y or not values:
            return values
        import numpy as np
        from . import expr
        raw, known = np.asarray(values, dtype=float), {}

        def column(name: str) -> Any:
            if name == "raw":
                return raw
            if name not in self.card.params:
                raise KeyError("%s has no parameter %r" % (self.card.id, name))
            if name not in known:
                known[name] = np.float64(float(self.read(name)))
            return known[name]
        return [float(v) for v in np.broadcast_to(expr.evaluate(spec.y, column, np), raw.shape)]

    def write(self, param: str, value: Any) -> None:
        spec = self.spec(param)
        self.backend.write(spec.set_cmd.render(spec.to_wire(value)))
        self._last[param] = value

    def query_raw(self, command: str) -> str:
        return self.backend.query(command)

    def write_raw(self, command: str) -> None:
        self.backend.write(command)

    def instrument_error(self) -> Optional[str]:
        """The first queued instrument error, or None."""
        if self.card.error_query is None:
            return None
        resp = self.backend.query(self.card.error_query.render()).strip()
        return None if re.match(r"^\+?0+\s*(,|$)", resp) else resp   # "0,No error", 0 (SRS LERR?), 0000 (Pfeiffer ERR)

    def limits(self, param: str) -> Optional[Tuple[float, float]]:
        """The lab's limits (or the card's), narrowed to the mounted device's: the tighter side always holds."""
        base = self.config.limits.get(param) or self.spec(param).limits
        dev = self.device_limits.get(param)
        if dev is None:
            return base
        if base is None:
            return dev
        return max(base[0], dev[0]), min(base[1], dev[1])

    def limits_source(self, param: str) -> str:
        dev = self.device_limits.get(param)
        base = self.config.limits.get(param) or self.spec(param).limits
        if dev is not None and (base is None or dev[0] > base[0] or dev[1] < base[1]):
            return "device %s" % self.device_name if self.device_name else "device"
        return "lab" if param in self.config.limits else "vendor"

    def rate(self, param: str) -> Optional[float]:
        base = self.config.rates.get(param) or self.spec(param).max_rate
        dev = self.device_rates.get(param)
        return min(base, dev) if base and dev else (dev or base)

    def guards(self, param: str) -> List[Guard]:
        return self.config.guards.get(param, []) + self.device_guards.get(param, [])

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if close:
            close()
