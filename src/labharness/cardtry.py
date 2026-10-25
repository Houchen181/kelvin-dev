"""Try a card on the instrument before keeping it: ask who it is and read every reading, nothing else.

This is the step that makes a drafted card trustworthy: the person (or Kelvin, on an instrument a person connected)
sees each reading's value next to the front panel, or the error it gave, and the card is fixed until they agree.
Nothing is set: no drive, no setting, no connect commands for text instruments; reads that would start something
(MEASure?, READ?, INITiate, *TST?, *CAL?) are skipped. A library card runs its open calls, which connect it."""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .adapters import AdapterError, open_backend
from .autocard import ACTION_READ
from .cards import Card
from .instrument import Instrument
from .labconfig import InstrumentConfig
from .scpi import is_query


def _clone_without_connect(card: Card) -> Card:
    import copy
    c = copy.copy(card)
    c.on_connect = []   # a text instrument is only asked, never configured, while a card is being tried
    return c


def try_card(card: Card, address: str, lab: Any = None) -> Dict[str, Any]:
    """{identity, matches, readings: [{name, label, value, unit, error, skipped}], errors, address, reused}"""
    reuse = None
    if lab is not None:
        for name, inst in lab.instruments.items():
            if inst.config.address == address and not inst.simulated:
                if card.library or "://" in address and not address.lower().startswith("serial://"):
                    raise AdapterError("%s is connected in the lab as %s. Switch to the virtual lab (Lab, Set up) to "
                                       "try another card on it, or save this card and use it for %s." % (address, name, name))
                reuse = inst.backend   # a text instrument: ask it through the connection the lab already has
    if reuse is None and address.startswith("sim://"):
        raise AdapterError("%s is a simulated instrument; try the card on a real address" % address)
    backend = reuse or open_backend("try", _clone_without_connect(card) if not card.library else card, address)
    inst = Instrument("try", card, backend, InstrumentConfig(name="try", card=card.id, address=address))
    out: Dict[str, Any] = {"address": address, "card": card.id, "reused": reuse is not None, "readings": []}
    from .drivers import is_driver_address
    text = not card.library and not is_driver_address(address)   # a text instrument: only queries are sent

    def unsafe(command: str) -> Optional[str]:
        if not text:
            return None
        if ACTION_READ.match(command):
            return "this read may start a measurement or a test; try it with the instrument in the lab"
        if not is_query(command, bool(card.connection.get("enq"))):   # on an ENQ instrument, PR1 is a read
            return "not a query (no ?), so it might change something; check it in the manual"
        return None

    try:
        try:
            why = unsafe(card.idn_query or "")
            ident = "" if why else backend.query(card.idn_query).strip()
            if why:
                out["identity_error"] = "not asked: " + why
        except Exception as e:
            ident, out["identity_error"] = "", str(e)
        out["identity"] = ident
        out["matches"] = bool(re.search(card.idn_pattern, ident)) if card.idn_pattern and ident else None
        for spec in card.params.values():
            if spec.get_cmd is None:
                continue
            row: Dict[str, Any] = {"name": spec.name, "label": spec.label or spec.name, "unit": spec.unit,
                                   "kind": spec.kind}
            why = unsafe(spec.get_cmd.render() if "{value}" not in spec.get_cmd.text else spec.get_cmd.text)
            if spec.get_before and not why:
                why = "this reading runs an acquisition (%s); try it with the instrument in the lab" % "; ".join(
                    spec.get_before)
            if why:
                row["skipped"] = why
                out["readings"].append(row)
                continue
            t0 = time.monotonic()
            try:
                value = inst.read(spec.name)
                row["value"] = value if not isinstance(value, list) else "trace (%d points)" % len(value)
                if value is None and spec.read_when:
                    row["skipped"] = "read only while %s is on" % spec.read_when
            except Exception as e:
                row["error"] = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
            row["ms"] = int((time.monotonic() - t0) * 1000)
            out["readings"].append(row)
        try:
            out["instrument_error"] = inst.instrument_error()
        except Exception as e:
            out["instrument_error"] = "the error query failed: %s" % e
    finally:
        if reuse is None:
            try:
                backend.close()
            except Exception:
                pass
    return out


def summary(result: Dict[str, Any]) -> str:
    """The try as text, for a chat or the terminal."""
    lines = ["%s on %s%s" % (result["card"], result["address"], " (through the lab's connection)" if result.get("reused") else "")]
    if result.get("identity_error"):
        lines.append("identity: no answer (%s)" % result["identity_error"])
    else:
        match = {True: "matches the card", False: "does NOT match the card's identity pattern", None: ""}[result.get("matches")]
        lines.append("identity: %r %s" % (result.get("identity", ""), match))
    ok = bad = 0
    for r in result["readings"]:
        if r.get("error"):
            bad += 1
            lines.append("  %s: ERROR %s" % (r["name"], r["error"]))
        elif r.get("skipped"):
            lines.append("  %s: skipped (%s)" % (r["name"], r["skipped"]))
        else:
            ok += 1
            lines.append("  %s = %s%s" % (r["name"], r["value"], " " + r["unit"] if r.get("unit") else ""))
    if result.get("instrument_error"):
        lines.append("instrument error queue: %s" % result["instrument_error"])
    lines.append("%d readings answered, %d failed. Compare the values with the front panel; fix the card and try "
                 "again until they agree." % (ok, bad))
    return "\n".join(lines)


def addresses_for_agent(lab: Any, found: Optional[List[str]]) -> Dict[str, str]:
    """Where Kelvin may try a card on its own: instruments a person put in the lab, and what the last scan found."""
    out = {name: ic.address for name, ic in lab.config.instruments.items() if not ic.address.startswith("sim://")}
    for a in found or []:
        out.setdefault(a, a)
    return out
