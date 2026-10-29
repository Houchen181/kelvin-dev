"""Try a card on the instrument before keeping it: ask who it is, read every reading, and write one setting with the
value it already has.

This is the step that makes a drafted card trustworthy: the person (or Kelvin, on an instrument a person connected)
sees each reading's value next to the front panel, or the error it gave, and the card is fixed until they agree.
Nothing changes: no drive, no connect commands for text instruments; reads that would start something (MEASure?,
READ?, INITiate, *TST?, *CAL?) are skipped, and said to be (a skip is not a check). One setting (never a drive) is
written with the value it reads, then read back with the error queue: a set command the instrument doesn't take (a
long form it refuses, -113) shows here rather than in the first run, where it once passed for a set. A library card
runs its open calls, which connect it."""
from __future__ import annotations

import math
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
                    raise AdapterError("%s is connected in the lab as %s: save this card and use it for %s, or take %s "
                                       "out of the lab config (Lab, Set up) to try another card on it."
                                       % (address, name, name, name))
                reuse = inst.backend   # a text instrument: ask it through the connection the lab already has
    if reuse is None and address.startswith("sim://"):
        raise AdapterError("%s is a simulated instrument; try the card on a real address" % address)
    backend = reuse or open_backend("try", _clone_without_connect(card) if not card.library else card, address)
    inst = Instrument("try", card, backend, InstrumentConfig(name="try", card=card.id, address=address),
                      getattr(lab, "stops", None))   # a person's stop ends its waits too
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
        if text and not card.connection.get("write_only"):
            check = write_check(inst, card)
            if check is not None:
                out["write_check"] = check
    finally:
        if reuse is None:
            try:
                backend.close()
            except Exception:
                pass
    return out


def _same(spec: Any, a: Any, b: Any) -> bool:
    if spec.numeric and a is not None and b is not None:
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-15)
    return a == b


def write_check(inst: Instrument, card: Card) -> Optional[Dict[str, Any]]:
    """One setting written with the value it has (so nothing changes), read back, and the error queue read after:
    {name, value, ok, error}. Only a setting that reads back, never a drive, a source's range or a reading that
    starts something; None when the card has none. Errors already queued are read out first, so only the write's
    own count."""
    for spec in card.params.values():
        if spec.is_drive or spec.set_cmd is None or spec.get_cmd is None or spec.get_before or spec.acquires \
                or spec.read_when or card.range_owner(spec.name) is not None \
                or spec.type not in ("float", "int", "bool", "enum") or ACTION_READ.match(spec.get_cmd.render()):
            continue
        try:
            value = inst.read(spec.name)
        except Exception:
            continue   # its reading failed: the readings above say so
        if value is None:
            continue
        row: Dict[str, Any] = {"name": spec.name, "value": value, "unit": spec.unit}
        try:
            for _ in range(10):   # older errors (a reading's) are not the write's
                if not inst.instrument_error():
                    break
            inst.write(spec.name, value)
            back = inst.read(spec.name)
            err = inst.instrument_error()
        except Exception as e:
            row.update(ok=False, error=str(e).splitlines()[0][:300] if str(e) else type(e).__name__)
            return row
        if err:
            row.update(ok=False, error="the instrument reported %s after %s" % (err, spec.set_cmd.render(
                spec.to_wire(value))))
        elif not _same(spec, back, value):
            row.update(ok=False, error="it reads %s after Kelvin wrote %s" % (spec.show(back), spec.show(value)))
        else:
            row.update(ok=True, sent=spec.set_cmd.render(spec.to_wire(value)))
        return row
    return None


def summary(result: Dict[str, Any]) -> str:
    """The try as text, for a chat or the terminal."""
    lines = ["%s on %s%s" % (result["card"], result["address"], " (through the lab's connection)" if result.get("reused") else "")]
    if result.get("identity_error"):
        lines.append("identity: no answer (%s)" % result["identity_error"])
    else:
        match = {True: "matches the card", False: "does NOT match the card's identity pattern", None: ""}[result.get("matches")]
        lines.append("identity: %r %s" % (result.get("identity", ""), match))
    ok = bad = skipped = 0
    for r in result["readings"]:
        if r.get("error"):
            bad += 1
            lines.append("  %s: ERROR %s" % (r["name"], r["error"]))
        elif r.get("skipped"):
            skipped += 1
            lines.append("  %s: NOT TRIED (%s)" % (r["name"], r["skipped"]))
        else:
            ok += 1
            lines.append("  %s = %s%s" % (r["name"], r["value"], " " + r["unit"] if r.get("unit") else ""))
    if result.get("instrument_error"):
        lines.append("instrument error queue: %s" % result["instrument_error"])
    check = result.get("write_check")
    if check is not None:
        lines.append("set command: %s %s" % (check["name"], "takes %s (written with the value it had, read back the "
                                                           "same)" % check["sent"] if check.get("ok") else
                                             "FAILED: %s" % check["error"]))
    lines.append("%d readings answered, %d failed, %d not tried%s. Compare the values with the front panel; fix the "
                 "card and try again until they agree." % (ok, bad, skipped, "" if check is not None else
                                                           "; no setting could be written back to check a set command"))
    return "\n".join(lines)


def addresses_for_agent(lab: Any, found: Optional[List[str]]) -> Dict[str, str]:
    """Where Kelvin may try a card on its own: instruments a person put in the lab, and what the last scan found."""
    out = {name: ic.address for name, ic in lab.config.instruments.items() if not ic.address.startswith("sim://")}
    for a in found or []:
        out.setdefault(a, a)
    return out
