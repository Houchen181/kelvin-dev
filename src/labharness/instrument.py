"""An instrument = its card + a backend (a twin now, VISA/serial/... in Phase 1).

These methods talk to hardware with no safety checks. Only the safety gate
and the run engine call them; the agent never gets an Instrument.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .cards import Card, ParamSpec
from .labconfig import Guard, InstrumentConfig


class Instrument:
    def __init__(self, name: str, card: Card, backend: Any, config: InstrumentConfig):
        self.name = name
        self.card = card
        self.backend = backend
        self.config = config
        self._last: Dict[str, Any] = {}
        for command in card.on_connect:   # sent on connecting: a parameter with no read-back is known from the start
            for spec in card.params.values():
                text = spec.set_cmd.match(command) if spec.get_cmd is None and spec.set_cmd is not None else None
                if text is not None:
                    try:
                        self._last[spec.name] = spec.from_wire(text)
                    except (ValueError, TypeError):
                        pass

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.backend, "simulated", False))

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
        return spec.from_wire(self.backend.query(spec.get_cmd.render()))

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
        return None if re.match(r"^\+?0\s*(,|$)", resp) else resp   # "0,No error", or a bare 0 (SRS LERR?)

    def limits(self, param: str) -> Optional[Tuple[float, float]]:
        return self.config.limits.get(param) or self.spec(param).limits

    def limits_source(self, param: str) -> str:
        return "lab" if param in self.config.limits else "vendor"

    def rate(self, param: str) -> Optional[float]:
        return self.config.rates.get(param) or self.spec(param).max_rate

    def guards(self, param: str) -> List[Guard]:
        return self.config.guards.get(param, [])

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if close:
            close()
