"""Arbin cyclers' exports (MITS Pro, MITS 7 and 8, Data Watcher), as CSV or Excel: one row per point with its test
time, step and cycle, current, voltage, capacities and energies ("Test_Time(s)", "Cycle_Index", "Current(A)"... or,
from MITS 8, "Test Time (s)", "Cycle Index", "Current (A)").

They read as any table does (imports.table_from_rows); what is Arbin's own is which column is x (the test time, not
the point's number), the sheets of a workbook that hold the points ("Channel_1-005", split into _1, _2 when long),
and that a temperature's "(C)" is degrees Celsius. Arbin's own database (.res) is not read: export it as Excel or CSV
from MITS Pro or Data Watcher."""
from __future__ import annotations

from typing import Any, Iterable, List, Tuple

X = ["test_time"]
LEFT_OUT = ["data_point"]   # the point's number: the run counts its own


def is_arbin(keys: Iterable[str]) -> bool:
    keys = set(keys)
    return "test_time" in keys and bool(keys & {"cycle_index", "step_index"}) and bool(keys & {"voltage", "current"})


def unit(name: str, unit: str) -> str:
    """A temperature's (C) is degrees Celsius, not coulombs."""
    return "°C" if unit == "C" and "temp" in name.lower() else unit


def points_sheets(sheets: List[Tuple[str, List[List[Any]]]]) -> List[Tuple[str, List[List[Any]]]]:
    """The sheets that hold the points: Arbin's Channel sheets (its Global_Info and Statistics sheets are about the
    test and each cycle)."""
    return [(n, rows) for n, rows in sheets if n.lower().startswith("channel")]


def joined(sheets: List[Tuple[str, List[List[Any]]]]) -> List[List[Any]]:
    """Channel sheets one after another, each later one's header row left out when it repeats the first's."""
    out: List[List[Any]] = []
    head = None
    for _, rows in sheets:
        rows = [r for r in rows if any(c not in (None, "") for c in r)]
        if not rows:
            continue
        if head is None:
            head = rows[0]
            out += rows
        else:
            out += rows[1:] if rows[0] == head else rows
    return out
