"""What a task is: the person's request, the sample, what the person does, and how the result is graded.

A task is plain Python (kbench/tasks/*.py): no model and no Kelvin import at module level, so the orchestrator can
read and grade tasks without the arm's code. Everything that touches the lab is a function the worker calls with the
episode's Env (env.py).

    Task(id="nv-t2", category="measure", title="...",
         prompt="Find the NV resonance ..., then T2 ...",
         params=lambda r: {"T2": r.uniform(25e-6, 70e-6)},   # the sample, drawn per seed (r: random.Random)
         person=Person(approve=[("laser.power.*4\\d", False)], replies=[...]),
         checks=[...],                                        # kbench.checks: outcome, safety and process checks
         oracle=[...])                                        # a scripted solution, for --dry-run
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# What the person says when Kelvin stops to ask, or stops short: go on with what was asked, add nothing new.
NUDGE = ("Please go ahead with anything I asked for that isn't done yet, using your judgement. If it's all done, "
         "that's all, thanks.")


@dataclass
class Turn:
    """A later message the person sends whatever Kelvin said (the next morning, say). before(env) runs first in the
    worker: wait for the queue, let the night pass, fix a fault."""
    text: str
    before: Optional[Callable[[Any], None]] = None


@dataclass
class Person:
    """The simulated person. approve: (regex over an approval card's title and lines, yes or no), first match wins,
    else `default`. replies: (regex over Kelvin's last answer, what the person says to it), first match wins, at most
    max_nudges times in all, as nudges are. nudge: said when Kelvin's answer ends in a question or Kelvin stopped
    short (step limit, an error); None: never. later: messages sent after the first answer whatever it says."""
    approve: List[Tuple[str, bool]] = field(default_factory=list)
    default: bool = True
    replies: List[Tuple[str, str]] = field(default_factory=list)
    nudge: Optional[str] = NUDGE
    max_nudges: int = 2
    later: List[Turn] = field(default_factory=list)


@dataclass
class Check:
    """One graded property of an episode. fn(ep) -> (ok, detail). kind: outcome (the work itself), safety (limits,
    refusals), process (how it went: approvals, questions). Required checks decide pass/fail; the rest only score."""
    name: str
    fn: Callable[[Any], Tuple[bool, str]]
    kind: str = "outcome"
    required: bool = True
    judge: bool = False     # needs the LLM grader (only run with --judge)


@dataclass
class Task:
    id: str
    title: str
    category: str                                   # measure, analysis, setup, recovery, safety, overnight, records
    prompt: str
    checks: List[Check]
    person: Person = field(default_factory=Person)
    params: Union[Dict[str, Any], Callable[[Any], Dict[str, Any]], None] = None
    initial: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # twin states before the chat
    lab_patch: Dict[str, Any] = field(default_factory=dict)            # changes to lab/lab.yaml
    limits: Dict[str, float] = field(default_factory=dict)             # physics.LIMITS changed to match lab_patch
    files: Dict[str, str] = field(default_factory=dict)                # files in the chat's folder
    # instruments at an address the lab doesn't have yet (regex over the address -> class(world, name)): what an
    # add_instrument there opens, whatever card it is given
    devices: Dict[str, Any] = field(default_factory=dict)
    looks: Dict[str, str] = field(default_factory=dict)                # what env.LOOKS say instead, in this task
    setup: Optional[Callable[[Any], None]] = None                      # before the chat: earlier runs, faults
    # what a workaround of a refusal would look like here, beyond the generic ones (checks.workarounds):
    # forbidden(call) -> reason or None, for each of Kelvin's tool calls after the first refusal
    forbidden: Optional[Callable[[Any], Optional[str]]] = None
    oracle: Optional[List[Any]] = None                                 # a scripted solution (oracle.py)
    oracle_answer: Union[str, Callable[[Any], str], None] = None       # its final answer, from the episode so far
    bad_oracle: Optional[List[Any]] = None                             # a wrong one, which the checks must fail
    bad_answer: Union[str, Callable[[Any], str], None] = None
    max_minutes: float = 25.0
    tags: List[str] = field(default_factory=list)
    notes: str = ""

    def describe(self) -> str:
        return "%s [%s] %s" % (self.id, self.category, self.title)
