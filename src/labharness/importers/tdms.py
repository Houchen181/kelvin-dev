"""NI's TDMS files (.tdms: what LabVIEW and NI's programs save measurements in) as a run, read with npTDMS (an
open-source reader, BSD licence), which Kelvin installs on this computer itself (envs.py: the
drivers environment) and never ships. A TDMS file holds groups of channels, each a column of values with its
properties (a unit, a waveform's start and step). How one is read:
- the group with the most values: its channels of one length side by side (the other groups, and channels of
  another length, are left out and said);
- x: a channel named as time or as the sweep's axis (Time, t, X), else a waveform's own time (its start and step:
  wf_start_offset, wf_increment), else the first channel, as a table's first column is;
- each channel's unit from its unit_string; dates and times as seconds from the first; text channels left out (said).
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..imports import DataError, MAX_ROWS, Table, key_of, table_from_columns

MARK = b"TDSm"            # a TDMS file's first bytes
X_NAMES = ("time", "t", "time_s", "elapsed_time", "timestamp", "x")


def missing() -> str:
    from .. import envs
    return envs.missing("npTDMS")


def _column(channel: Any, np: Any) -> Tuple[Optional[List[float]], str, str]:
    """A channel's values as numbers (None: text, which is left out), its unit, and how it was read."""
    data = channel[:]
    a = np.asarray(data)
    unit = str(channel.properties.get("unit_string") or "").strip()
    if a.dtype.kind == "M":   # dates and times: seconds from the first
        s = a.astype("datetime64[ns]").astype("int64")
        good = s[s != np.iinfo("int64").min]
        first = good[0] if good.size else 0
        return [float(v - first) / 1e9 for v in s], "s", "as seconds from its first time"
    if a.dtype.kind in "biuf":
        return [float(v) for v in a.astype(float)], unit, ""
    if a.dtype.kind == "c":
        return [float(v) for v in np.abs(a)], unit, "as its magnitude (it is complex)"
    return None, unit, ""


def _waveform(channel: Any) -> Optional[Tuple[float, float]]:
    """A waveform channel's start and step (seconds), or None."""
    p = channel.properties
    step = p.get("wf_increment")
    try:
        step = float(step) if step is not None else None
        start = float(p.get("wf_start_offset") or 0.0)
    except (TypeError, ValueError):
        return None
    return (start, step) if step else None


def _numbers(channel: Any) -> bool:
    """Whether a channel holds numbers, dates or times (not text), as its metadata says."""
    kind = getattr(channel.dtype, "kind", "")
    return bool(kind) and kind in "biufcM"


def read_tdms(p: Path) -> Table:
    """A .tdms file as a Table. DataError: npTDMS isn't on this computer (saying how Kelvin installs it), or the
    file isn't TDMS, or holds no channel of numbers, or more than MAX_ROWS of them: told from its metadata, before
    any value is read (then only the chosen group's are)."""
    try:
        from nptdms import TdmsFile
    except ImportError:
        raise DataError("%s is an NI TDMS file, which Kelvin reads with npTDMS: %s" % (p.name, missing()))
    import numpy as np
    try:
        with TdmsFile.open(str(p)) as f:   # its metadata now; a channel's values only when asked for
            return _table(p, [(g.name, list(g.channels())) for g in f.groups()], np)
    except DataError:
        raise
    except Exception as e:   # npTDMS's own errors for a file that isn't TDMS, or is damaged
        raise DataError("%s could not be read as a TDMS file (%s)" % (
            p.name, str(e).splitlines()[0][:120] if str(e) else type(e).__name__))


def _table(p: Path, groups: List[Tuple[str, List[Any]]], np: Any) -> Table:
    best: Optional[Tuple[int, str, List[Any], int]] = None
    texts: List[str] = []
    for name, channels in groups:   # by the metadata alone: how many values each channel has, and of what
        numeric = []
        for c in channels:
            if not _numbers(c):
                texts.append("%s/%s" % (name, c.name))
            elif len(c):
                numeric.append(c)
        if not numeric:
            continue
        length = Counter(len(c) for c in numeric).most_common(1)[0][0]
        same = [c for c in numeric if len(c) == length]
        size = length * len(same)
        if best is None or size > best[0]:
            best = (size, name, same, len(numeric) - len(same))
    if best is None:
        raise DataError("%s holds no channel of numbers%s" % (
            p.name, " (its text channels: %s)" % ", ".join(texts[:10]) if texts else ""))
    _, group, chosen, other_length = best
    if len(chosen[0]) > MAX_ROWS:
        raise DataError("%s's channels have more than %d values" % (p.name, MAX_ROWS))
    columns: List[Tuple[Any, List[float], str, str]] = []
    for c in chosen:   # only now are values read: the chosen group's, MAX_ROWS at most each
        try:
            values, unit, how = _column(c, np)
        except Exception as e:
            texts.append("%s/%s (%s)" % (group, c.name, type(e).__name__))
            continue
        if values is None:
            texts.append("%s/%s" % (group, c.name))
        elif values:
            columns.append((c, values, unit, how))
    if not columns:
        raise DataError("%s holds no channel of numbers%s" % (
            p.name, " (its text channels: %s)" % ", ".join(texts[:10]) if texts else ""))
    length = len(columns[0][1])
    named: List[Tuple[str, List[float]]] = []
    notes: List[str] = []
    keys = [key_of(c.name, "channel") for c, _, _, _ in columns]
    prefer = [k for k in keys if k in X_NAMES]
    waves = [_waveform(c) for c, _, _, _ in columns]
    if not prefer and all(w is not None for w in waves) and len({w[1] for w in waves if w}) == 1:
        start, step = waves[0]   # waveforms: their own time is x
        named.append(("time (s)", [start + step * i for i in range(length)]))
        prefer = ["time"]
        notes.append("%s: x is the waveforms' time (from %g s, every %g s)" % (p.name, start, step))
    elif len(columns) == 1 and not prefer:
        named.append(("point", [float(i) for i in range(length)]))
        notes.append("%s: one channel, on its points' numbers" % p.name)
    for (c, values, unit, how), key in zip(columns, keys):
        named.append(("%s (%s)" % (c.name, unit) if unit else str(c.name), values))
        if how:
            notes.append("%s: %s, %s" % (p.name, key, how))
    table = table_from_columns(key_of(p.stem, "data"), p.name, named, prefer=prefer)
    table.notes = ["%s: TDMS group %s, %d channel%s" % (p.name, group, len(columns), "" if len(columns) == 1 else "s")]
    table.notes += notes
    left = [g for g, chans in groups if g != group and chans]
    if left:
        table.notes.append("%s: other groups left out: %s" % (p.name, ", ".join(left[:10])))
    if other_length:
        table.notes.append("%s: %d channel%s of another length left out" % (p.name, other_length,
                                                                            "" if other_length == 1 else "s"))
    if texts:
        table.notes.append("%s: text channels left out: %s" % (p.name, ", ".join(texts[:10])))
    return table
