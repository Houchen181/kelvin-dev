"""What a report says, independent of the format it is written in."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from .chart import Chart, Table


@dataclass
class RunPart:
    run_id: str
    plan: str
    status: str                       # done, aborted, ...
    when: str                         # "Oct 17 2026, 22:17-22:22"
    duration: str
    points: str                       # "63 of 63"
    operator: str
    reason: str = ""
    plan_text: str = ""
    sweep: List[str] = field(default_factory=list)       # "smu.V: 0 ... 4 V, step 0.2 V"
    setup: List[str] = field(default_factory=list)       # "lockin.amplitude 50 mV"
    measured: List[str] = field(default_factory=list)
    charts: List[Chart] = field(default_factory=list)
    assessment: str = ""
    next: str = ""
    results: List[Table] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    conditions: List[Tuple[str, str]] = field(default_factory=list)   # instrument, its values at the start
    away: List[str] = field(default_factory=list)                     # drives away from safe at the start
    found: List[Tuple[str, str]] = field(default_factory=list)        # the plan's instruments: settings it left alone
    changes: List[str] = field(default_factory=list)                  # drives that ended somewhere else
    unread: List[str] = field(default_factory=list)                   # drives the end couldn't read: where, unknown
    events: List[Tuple[str, str]] = field(default_factory=list)       # time, approvals, trips, alarms


@dataclass
class Report:
    title: str
    subtitle: str
    created: str
    lab: str
    simulated: bool
    summary: List[str] = field(default_factory=list)       # the user's or Kelvin's own words, paragraphs
    runs: List[RunPart] = field(default_factory=list)
    log: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[Tuple[str, str]] = field(default_factory=list)   # project notes: kind, text
    references: List[str] = field(default_factory=list)          # the project's reading list, formatted
    folder: str = ""

    @property
    def run_lines(self) -> List[str]:
        """One line per run for the summary: name, outcome, first sentence of the assessment."""
        out = []
        for r in self.runs:
            first = r.assessment.split(". ")[0].rstrip(".") if r.assessment else r.reason or r.status
            out.append("%s (%s): %s." % (r.plan, r.status if r.status != "done" else r.points + " points", first))
        return out
