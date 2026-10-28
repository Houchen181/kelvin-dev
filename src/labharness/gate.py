"""The safety gate: the only way the agent's commands reach instruments.

- Measure / configure: runs immediately and is logged.
- Drive toward safe: runs, ramped at the safe rate.
- Drive away from safe: limit and interlock checks → asks a human unless a
  signed envelope or an approved plan covers it, or auto mode is on and the
  change is low-risk → ramps while watching guard readings, and returns to
  safe if one trips.
- A real instrument Kelvin isn't sure about (its card is a draft, or Kelvin added it to the lab itself and nobody
  has reviewed its limits): every change away from safe asks a person and says why; no envelope or auto mode
  covers it.
- Beyond limits, or blocked commands: refused.
- A mounted device's protection (protection.py) narrows the limits and rates and adds its gates' leakage guards;
  a source's range changes only while its level is at safe; while a person has their hands on the sample
  (lab.hands_on), nothing moves away from safe.
"""
from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .cards import ParamSpec, num
from .eventlog import EventLog
from .instrument import Instrument
from .labconfig import Allowed, Envelope, Guard
from .scpi import Template, is_query, split_compound


@dataclass
class Result:
    ok: bool
    action: str            # read, set, no-change, sent, held, refused, declined, tripped, stopped, timeout, error
    target: str
    value: Any = None
    before: Any = None
    message: str = ""

    def __str__(self) -> str:
        return self.message


@dataclass
class ApprovalRequest:
    title: str
    items: List[str]
    note: str = ""          # set by the approver, e.g. "no answer within 120 s"

    def __str__(self) -> str:
        return "\n".join([self.title] + ["  - " + i for i in self.items])


class Approver:
    """Gets a yes/no from a human. The agent never sees or calls this."""

    def approve(self, request: ApprovalRequest) -> bool:
        raise NotImplementedError

    def inform(self, request: ApprovalRequest, reasons: List[str]) -> None:
        """Tell the human that auto mode approved `request` without asking (default: only the event log)."""


class ConsoleApprover(Approver):
    def approve(self, request: ApprovalRequest) -> bool:
        print(request)
        try:
            return input("Approve? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False


class DenyAll(Approver):
    def approve(self, request: ApprovalRequest) -> bool:
        return False


class ScriptedApprover(Approver):
    """Answers from a list, for tests and scripted demos. Fails loudly on an unexpected ask."""

    def __init__(self, answers: Any = (), echo: bool = False):
        self.answers = list(answers)
        self.requests: List[ApprovalRequest] = []
        self.echo = echo

    def approve(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        if not self.answers:
            raise AssertionError("unexpected approval request:\n%s" % request)
        answer = bool(self.answers.pop(0))
        if self.echo:
            print("  ? %s\n    → %s" % (str(request).replace("\n", "\n    "), "approved" if answer else "declined"))
        return answer


class SimulationApprover(Approver):
    """Approves everything and records what would have been asked. Twins only."""

    def __init__(self) -> None:
        self.requests: List[ApprovalRequest] = []

    def approve(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return True


def _dur(seconds: float) -> str:
    if seconds < 90:
        return "%.0f s" % seconds
    if seconds < 5400:
        return "%.1f min" % (seconds / 60)
    return "%.1f h" % (seconds / 3600)


def _same(spec: ParamSpec, a: Any, b: Any) -> bool:
    if spec.numeric:
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-15)
    return a == b


def _unreachable(spec: ParamSpec) -> bool:
    """A set point the instrument ramps to by itself whose safe value no instrument reaches: absolute zero. After a
    stop it holds where its reading is; waiting for 0 K hung a stopped run until the wait timed out."""
    return (spec.ramp == "instrument" and bool(spec.readback) and spec.unit in ("K", "mK")
            and isinstance(spec.safe, (int, float)) and spec.safe <= 0)


OUTSIDE_S = 1800   # how long auto mode pauses after the model read text from the web, for clients without chats


class SafetyGate:
    def __init__(self, lab: Any, approver: Approver, log: Optional[EventLog] = None,
                 envelope: Optional[Envelope] = None, operator: str = "agent"):
        if isinstance(approver, SimulationApprover) and not all(i.simulated for i in lab.instruments.values()):
            raise ValueError("SimulationApprover can only be used on simulated instruments")
        self.lab = lab
        self.approver = approver
        self.log = log if log is not None else EventLog(None, lab.clock)
        self.envelope = envelope
        self.operator = operator
        self.auto = False  # auto mode: set by a person through the portal, never by the agent
        # text from outside the lab the model has read (a web page, a manual, a board's output): it could try to
        # steer the model, so auto mode asks a person instead. outside_chat: the portal's chat has read some.
        self.outside: Optional[Tuple[float, str]] = None
        self.outside_chat: Optional[str] = None
        self.in_chat = False   # a portal chat's turn: the chat's own account of web text counts, not the clock's
        self._grants: Dict[str, Allowed] = {}
        self._standing: Dict[str, Allowed] = {}   # what a person approved for a whole queue of scans, while it runs
        # the drive levels the running plan or queue item set: switching their enable on parks them at safe (_move),
        # then puts them back (_reapply), so a run measures at what was approved, not at safe. None: no plan running
        self._asked: Optional[Dict[str, Any]] = None
        self._undo: List[Tuple[str, str, Any]] = []
        self._stop_gen = 0
        self._prefixes: Dict[int, Template] = {}
        self._acting = threading.local()   # a person's own command (the portal's terminal), on this thread only

    @contextmanager
    def acting_as(self, by: str) -> Iterator[None]:
        """Commands on this thread are a person's, not the agent's: logged as theirs and kept off the agent's undo
        list. Every check still applies: limits, interlocks, blocked commands, approvals."""
        old = getattr(self._acting, "by", None)
        self._acting.by = by
        try:
            yield
        finally:
            self._acting.by = old

    def _who(self) -> str:
        return getattr(self._acting, "by", None) or self.operator

    # ------------------------------------------------------------------ agent-facing

    def get(self, instr: str, param: str) -> Result:
        key = "%s.%s" % (instr, param)
        try:
            inst = self.lab.instrument(instr)
            spec = inst.spec(param)
            value = inst.read(param)
        except KeyError as e:
            return self._done(Result(False, "refused", key, message="refused: %s" % e.args[0]), "get")
        except Exception as e:
            return self._done(Result(False, "error", key, message="%s: %s" % (key, e)), "get")
        return self._done(Result(True, "read", key, value=value, message="%s = %s" % (key, spec.show(value))), "get")

    def set(self, instr: str, param: str, value: Any, wait: bool = True) -> Result:
        return self._set(instr, param, value, wait, self._who())

    def raw(self, instr: str, command: str) -> Result:
        try:
            inst = self.lab.instrument(instr)
        except KeyError as e:
            return self._done(Result(False, "refused", instr, message="refused: %s" % e.args[0]), "raw")
        parts = split_compound(command)
        if not parts:
            return Result(False, "refused", instr, message="refused: empty command")
        results = []
        for part in parts:
            results.append(self._raw_one(inst, part))
            if not results[-1].ok:
                break
        if len(results) == 1:
            return results[0]
        last = results[-1]
        return Result(last.ok, last.action, instr, value=last.value, message="; ".join(r.message for r in results))

    def act(self, instr: str, name: str, in_plan: bool = False) -> Result:
        """One of the card's named actions (trigger, arm, fire a burst), sent as the card writes it. One that drives the
        sample (drives: true) asks a person first, like a drive change, unless it is part of a plan they approved;
        the hands-on lock stops it. Blocked commands are never sent."""
        target = "%s.%s" % (instr, name)
        try:
            inst = self.lab.instrument(instr)
        except KeyError as e:
            return self._done(Result(False, "refused", target, message="refused: %s" % e.args[0]), "action")
        action = inst.card.actions.get(name)
        if action is None:
            return self._done(Result(False, "refused", target, message="refused: %s has no action %s (it has: %s)" % (
                instr, name, ", ".join(inst.card.actions) or "none")), "action")
        for c in action.send:
            if any(b.match(c) is not None for b in inst.card.blocked):
                return self._done(Result(False, "refused", target, message="refused: %r is blocked on %s" % (c, instr)),
                                  "action")
        if action.drives:
            hands = self._hands_problem(target)
            if hands:
                return self._done(Result(False, "refused", target, message="refused: %s" % hands), "action")
            if not in_plan:
                request = ApprovalRequest("%s: %s?" % (instr, action.label), [
                    "%s sends %s, which drives the sample" % (target, " then ".join(repr(c) for c in action.send))])
                if not self.ask(request):
                    note = " (%s)" % request.note if request.note else ""
                    return self._done(Result(False, "declined", target, message="declined: %s not done%s"
                                             % (target, note)), "action")
        said = inst.act(name)
        return self._done(Result(True, "sent", target, value=said[-1] if said else None,
                                 message="%s: %s%s" % (instr, action.label, " → " + said[-1] if said else "")),
                          "action", None, instrument=instr, name=name)

    def undo(self) -> Result:
        if not self._undo:
            return Result(False, "refused", "", message="nothing to undo")
        instr, param, before = self._undo.pop()
        r = self._set(instr, param, before, True, self._who(), record_undo=False)
        r.message = "undo: " + r.message
        return r

    def describe(self, instr: str) -> str:
        inst = self.lab.instrument(instr)
        card = inst.card
        lines = ["%s: %s (%s, card %s, %s, trust %s)" % (inst.name, card.model, card.kind, card.id, card.status, inst.trust)]
        unsure = self.unsure(inst)
        if unsure:
            lines.append("  not sure yet: %s; every output change asks a person" % unsure)
        for spec in card.params.values():
            bits = [spec.name, spec.kind]
            if spec.unit:
                bits.append(spec.unit)
            lim = inst.limits(spec.name)
            if lim:
                src = inst.limits_source(spec.name)   # lab, vendor, or the mounted device's
                bits.append("[%s, %s] %s" % (num(lim[0]), num(lim[1]),
                                             {"vendor": "card", "lab": "lab config"}.get(src, src)))
            if spec.type == "enum":
                bits.append("{%s}" % ", ".join(spec.values or {}))
            if spec.is_drive:
                bits.append("safe %s" % spec.show(spec.safe))
            if spec.ramp == "gate":
                bits.append("ramp %s %s/s" % (num(inst.rate(spec.name)), spec.unit))
            g = self._guard_text(inst, spec)
            if g:
                bits.append(g)
            if spec.set_cmd is None:
                bits.append("read-only")
            if spec.get_before:
                bits.append("runs an acquisition (%s), then reads" % "; ".join(spec.get_before))
            lines.append("  " + "  ".join(bits))
        if card.actions:
            lines.append("  actions (do_action, or a plan's trigger:): " + ", ".join(
                "%s%s" % (n, " (drives the sample: asks first)" if a.drives else "") for n, a in card.actions.items()))
        return "\n".join(lines)

    def status(self) -> Dict[str, Dict[str, Any]]:
        return self.lab.snapshot()

    def to_safe(self, instr: str) -> List[Result]:
        """Bring an instrument's drives to safe in its card's order (ramped, or held where the lab says so)."""
        by = self._who()
        try:
            inst = self.lab.instrument(instr)
        except KeyError as e:
            return [self._done(Result(False, "refused", instr, message="refused: %s" % e.args[0]), "safe", by)]
        try:
            results = self._to_safe(inst, wait=True)
        except Exception as e:
            results = [Result(False, "error", instr, message="%s: instrument error: %s" % (instr, e))]
        return [self._done(r, "safe", by) for r in results]

    def unsure(self, inst: Instrument) -> Optional[str]:
        """Why Kelvin can't be sure what a person allows on this real instrument, if it can't: its card is a draft
        nobody has checked, or Kelvin added it to the lab itself and nobody has reviewed its limits. Then every change
        away from safe asks a person, saying this, and neither an envelope nor auto mode covers it."""
        if inst.simulated:
            return None
        why = []
        if inst.card.status == "draft":
            why.append("its card is a draft that nobody has checked against the instrument")
        if self.lab.config.observe_only or inst.config.added_by:
            why.append("Kelvin added it to the lab itself, and its limits are the maker's: nobody has reviewed them "
                       "for this setup")
        return "; ".join(why) or None

    def request_abort(self) -> None:
        self._stop_gen += 1
        stops = getattr(self.lab, "stops", None)
        if stops is not None:
            stops.pulse()   # every long wait ends now: a settle, a ramp's step, an instrument's sweep

    def _pause(self, seconds: float, gen: int) -> None:
        """The clock's sleep, cut short by a stop."""
        stops = getattr(self.lab, "stops", None)
        if stops is None or self._stop_gen != gen:
            self.lab.clock.sleep(0 if self._stop_gen != gen else seconds)
            return
        stops.sleep(self.lab.clock, seconds, stops.count)

    @property
    def stop_generation(self) -> int:
        return self._stop_gen

    # ------------------------------------------------------------------ auto mode

    def auto_verdict(self, inst: Instrument, spec: ParamSpec, values: List[Any]) -> Tuple[bool, str]:
        """Is driving this parameter to these values low-risk enough to skip the human? And why, in words."""
        key = "%s.%s" % (inst.name, spec.name)
        rule = inst.config.auto.get(spec.name, inst.config.auto.get("*"))
        if rule == "never":
            return False, "%s is never auto-approved in this lab" % key
        if isinstance(rule, Allowed):
            if all(rule.covers(v) for v in values):
                return True, "%s stays in its auto range %s" % (key, rule.show(spec))
            return False, "%s leaves its auto range %s" % (key, rule.show(spec))
        if not spec.numeric:
            if all(spec.distance(v) <= 1 for v in values):
                return True, "%s is one step from safe" % key
            return False, "%s goes more than one step from safe" % key
        lim = inst.limits(spec.name)
        if lim is None or spec.safe is None:
            return False, "%s has no limits to measure the step against" % key
        worst = 0.0
        for v in values:
            edge = lim[1] if v >= spec.safe else lim[0]
            span = abs(edge - spec.safe)
            worst = max(worst, abs(v - spec.safe) / span if span > 0 else (0.0 if v == spec.safe else math.inf))
        fraction = self.lab.config.auto_fraction
        if worst <= fraction + 1e-9:
            return True, "%s stays within %.0f%% of the way from safe to its limit" % (key, 100 * worst)
        return False, "%s goes %.0f%% of the way from safe to its limit (auto mode covers %.0f%%)" % (
            key, 100 * worst, 100 * fraction)

    def note_outside(self, source: str) -> None:
        """The model has read text from the web: until it has been a while, auto mode asks a person (a portal chat
        keeps its own account instead, in_chat and outside_chat)."""
        self.outside = (time.monotonic(), source)

    def outside_reason(self, chat: Optional[str] = None) -> Optional[str]:
        """Why auto mode asks a person now, because the model read text from outside the lab (the web; in a portal
        chat, also a file in the chat's folder); None when it hasn't. In a portal chat, until the person writes again
        in it; for other clients, for OUTSIDE_S. chat: what a portal chat has read ("" for nothing), for a request it
        makes outside the lab's turn, which doesn't set outside_chat."""
        read = self.outside_chat if chat is None else chat
        if read:
            return ("this chat has read text from outside the lab (%s) since the person last wrote, which could try "
                    "to steer a model, so a person approves" % read)
        if chat is None and not self.in_chat and self.outside is not None and \
                time.monotonic() - self.outside[0] < OUTSIDE_S:
            return ("Kelvin read text from the web (%s) in the last %d minutes, which could try to steer a model, "
                    "so a person approves" % (self.outside[1], OUTSIDE_S // 60))
        return None

    def review_steps(self, inst: Instrument) -> str:
        """Where a person ends Kelvin's uncertainty about an instrument (unsure), in the portal."""
        steps = []
        if inst.card.status == "draft":
            steps.append("mark its card reviewed (Lab → Cards… → Mark reviewed)")
        if self.lab.config.observe_only or inst.config.added_by:
            steps.append("check its limits and mark them reviewed (Lab → Set up… → Limits reviewed…)")
        return " and ".join(steps)

    def auto_plan(self, grants: Dict[str, Allowed], dry_run: Any) -> Tuple[bool, List[str]]:
        """Can auto mode approve a plan with these drive ranges? Needs a clean dry run and every drive low-risk."""
        if not self.auto:
            return False, []
        outside = self.outside_reason()
        if outside:
            return False, [outside]
        if dry_run is None or not getattr(dry_run, "ok", False):
            return False, ["the dry run did not finish cleanly"]
        fine, blockers = [], []
        for key, allowed in grants.items():
            inst, spec = self.lab.resolve(key)
            unsure = self.unsure(inst)
            if unsure:
                blockers.append("%s: %s, so a person approves it until they %s" % (key, unsure,
                                                                                  self.review_steps(inst)))
                continue
            values = sorted(allowed.values, key=str) if allowed.values is not None else [allowed.lo, allowed.hi]
            ok, why = self.auto_verdict(inst, spec, values)
            (fine if ok else blockers).append(why)
        if blockers:
            return False, blockers
        return True, fine + ["the dry run finished cleanly"]

    def auto_simple(self, request: ApprovalRequest, why: str, chat: Optional[str] = None) -> bool:
        """Kelvin's own yes to a request that drives nothing (trying a card, adding an instrument at an address,
        keeping a skill or recipe, installing or removing a maker's package, saving code a library loads): approved
        without asking, whatever the approval mode, and said so; only driving the sample waits for a person. False
        when the model has read text from the web, which could be steering it (then the request says so, and a person
        answers). chat: as for outside_reason."""
        outside = self.outside_reason(chat)
        if outside:
            request.items.append("Not approved by Kelvin itself: " + outside)
            return False
        self.auto_approve(request, [why], "drives nothing")
        return True

    def auto_approve(self, request: ApprovalRequest, reasons: List[str], why: str = "auto mode") -> None:
        """Approve without asking, and say so: in the event log and to the human's screen."""
        request.note = "%s: %s" % (why, "; ".join(reasons))
        self.log.write("ask", by=self._who(), request=str(request), answer="auto-approved", note=request.note)
        try:
            self.approver.inform(request, reasons)
        except Exception:
            pass

    # ------------------------------------------------------------------ used by the run engine

    def ask(self, request: ApprovalRequest) -> bool:
        answer = bool(self.approver.approve(request))
        self.log.write("ask", by=self._who(), request=str(request), answer="approved" if answer else "declined",
                       note=request.note or None)
        return answer

    @contextmanager
    def standing(self, grants: Dict[str, Allowed]) -> Iterator[None]:
        """A queue's approval: every drive range its scans and steps use, approved once before it started. Scans
        inside it run without asking again; anything outside still asks."""
        old, self._standing = self._standing, dict(grants)
        asked, self._asked = self._asked, {}
        try:
            yield
        finally:
            self._standing = old
            self._asked = asked

    @contextmanager
    def preapproved(self, grants: Dict[str, Allowed]) -> Iterator[None]:
        old, self._grants = self._grants, dict(grants)
        asked, self._asked = self._asked, {}
        try:
            yield
        finally:
            self._grants = old
            self._asked = asked

    def plan_approval(self, drive_values: Dict[str, List[Any]]) -> Tuple[List[str], Dict[str, Allowed], List[str]]:
        """For a plan's drive targets: lines to show the human, the ranges to grant, and refusals."""
        items: List[str] = []
        grants: Dict[str, Allowed] = {}
        problems: List[str] = []
        for key, values in drive_values.items():
            inst, spec = self.lab.resolve(key)
            current = self._current(inst, spec)
            if current is None:   # unknown (no read-back): every value but safe counts as driving harder
                away = [v for v in values if spec.distance(v) > 0]
            else:
                d0 = spec.distance(current)
                away = [v for v in values if not _same(spec, v, current) and spec.distance(v) >= d0]
            if not away:
                continue
            problem = self._away_problem(inst, spec)
            standing = self._standing.get(key)
            if not problem and standing is not None and all(standing.covers(v) for v in away):
                continue   # inside what a person approved for the queue this scan is part of
            if problem:
                problems.append(problem)
                continue
            unsure = self.unsure(inst)
            if not unsure and inst.trust == "envelope" and self.envelope and all(self.envelope.covers(key, v)
                                                                                  for v in away):
                continue
            allowed = Allowed.of(spec, values)
            grants[key] = allowed
            bits = ["%s: now %s, plan uses %s" % (key, spec.show(current), allowed.show(spec))]
            if spec.ramp == "gate":
                bits.append("ramp %s %s/s" % (num(inst.rate(spec.name)), spec.unit))
            g = self._guard_text(inst, spec)
            if g:
                bits.append(g)
            if unsure:
                bits.append("Kelvin isn't sure about %s: %s" % (inst.name, unsure))
            items.append("; ".join(bits))
        return items, grants, problems

    # ------------------------------------------------------------------ internals

    def _set(self, instr: str, param: str, value: Any, wait: bool, by: str, record_undo: bool = True,
             gen: Optional[int] = None) -> Result:
        """gen: the stop generation a plan or queue item started at; a stop since then ends this change's waits and
        refuses a move away from safe (by default, only a stop during the change itself counts)."""
        key = "%s.%s" % (instr, param)
        try:
            inst = self.lab.instrument(instr)
            spec = inst.spec(param)
        except KeyError as e:
            return self._done(Result(False, "refused", key, message="refused: %s" % e.args[0]), "set", by)
        if spec.set_cmd is None:
            return self._done(Result(False, "refused", key, message="refused: %s is read-only" % key), "set", by)
        try:
            target = spec.coerce(value)
        except ValueError as e:
            return self._done(Result(False, "refused", key, message="refused: %s" % e), "set", by)
        problem = self._limit_problem(inst, spec, target)
        if problem:
            return self._done(Result(False, "refused", key, value=target, message="refused: " + problem), "set", by)
        try:
            current = self._current(inst, spec)
            owner = inst.card.range_owner(spec.name)
            if owner is not None and not _same(spec, current, target):
                problem = self._range_problem(inst, owner, spec, target)
                if problem:
                    return self._done(Result(False, "refused", key, value=target, message="refused: " + problem),
                                      "set", by)
            if not spec.is_drive:
                if inst.card.status == "draft" and not inst.simulated and not _same(spec, current, target):
                    # nobody has checked this card's measure/drive tags yet: a "setting" might reach the sample
                    request = ApprovalRequest("Change a setting on %s?" % inst.name, [
                        "%s: %s → %s. Its card is a draft no one has reviewed yet, so Kelvin can't be sure this "
                        "only configures the instrument" % (key, spec.show(current), spec.show(target))])
                    if not self.ask(request):
                        note = " (%s)" % request.note if request.note else ""
                        return self._done(Result(False, "declined", key, value=target, message="declined: %s stays %s%s"
                                                 % (key, spec.show(current), note)), "set", by)
                res = self._move(inst, spec, current, target, away=False, wait=wait, how=None, gen=gen)
            elif current is None and spec.distance(target) == 0:   # unknown, to its safe value: always allowed
                res = self._move(inst, spec, current, target, away=False, wait=wait, how="to safe", gen=gen)
            elif current is None and spec.ramp == "gate" and spec.numeric:
                return self._done(Result(False, "refused", key, value=target, message=(
                    "refused: Kelvin can't read %s back and hasn't set it since connecting, so it can't ramp from "
                    "where it is; set it to its safe value (%s) first" % (key, spec.show(spec.safe)))), "set", by)
            elif _same(spec, current, target):
                res = Result(True, "no-change", key, value=target, message="%s already %s" % (key, spec.show(target)))
            elif current is not None and spec.distance(target) < spec.distance(current):
                res = self._move(inst, spec, current, target, away=False, wait=wait, how="toward safe", gen=gen)
            else:
                if gen is not None and self._stop_gen != gen:   # stopped since the plan began: nothing goes up
                    return self._done(Result(False, "stopped", key, value=current, message="%s stays %s (stop requested)"
                                             % (key, spec.show(current))), "set", by)
                problem = self._away_problem(inst, spec)
                if problem:
                    return self._done(Result(False, "refused", key, value=target, message="refused: " + problem), "set", by)
                unsure = self.unsure(inst)
                how = self._covered(inst, spec, target, envelope=not unsure)
                if how is None:
                    request = ApprovalRequest("Approve drive change on %s?" % inst.name,
                                              [self._describe_move(inst, spec, current, target)])
                    if unsure:
                        request.items.append("Kelvin isn't sure about %s: %s" % (inst.name, unsure))
                    if not self.auto:
                        ok, why = False, ""
                    elif unsure:
                        ok, why = False, "nothing on %s is automatic until a person reviews it: %s" % (
                            inst.name, self.review_steps(inst))
                    elif self.outside_reason():
                        ok, why = False, self.outside_reason()
                    else:
                        ok, why = self.auto_verdict(inst, spec, [target])
                    if ok:
                        self.auto_approve(request, [why])
                        how = "auto-approved"
                    elif self.auto:
                        request.items.append("Auto mode does not cover this: " + why)
                if how is None:
                    if not self.ask(request):
                        msg = "declined: %s stays %s" % (key, spec.show(current))
                        if request.note:
                            msg += " (%s)" % request.note
                        return self._done(Result(False, "declined", key, value=current, message=msg), "set", by)
                    how = "approved"
                res = self._move(inst, spec, current, target, away=True, wait=wait, how=how, gen=gen)
                if res.ok and spec.enables:
                    res = self._reapply(inst, spec, res, wait, by, gen)
        except Exception as e:
            res = Result(False, "error", key, message="%s: instrument error: %s" % (key, e))
            current = None
        undoable = res.ok and record_undo and res.action != "no-change" and current is not None
        if undoable and getattr(self._acting, "by", None) is None:
            self._undo.append((instr, param, current))
        if res.ok and spec.is_drive and self._asked is not None:
            self._asked[key] = target
        return self._done(res, "set", by)

    def _reapply(self, inst: Instrument, spec: ParamSpec, res: Result, wait: bool, by: str,
                 gen: Optional[int] = None) -> Result:
        """An enable has just switched on, its levels parked at safe first (_move). Put back each level the running
        plan or queue item set, through every check again (limits, interlocks, its approval): a plan that sets the
        level before the switch would otherwise measure at safe while its approval shows the level."""
        for dep_name in spec.enables:
            dep = inst.spec(dep_name)
            level = (self._asked or {}).get("%s.%s" % (inst.name, dep_name))
            if level is None or dep.distance(level) == 0:
                continue
            r = self._set(inst.name, dep_name, level, wait, by, record_undo=False, gen=gen)
            if not r.ok:
                return Result(False, r.action, res.target, value=res.value, before=res.before,
                              message="%s; but %s could not be put back: %s" % (res.message, dep_name, r.message))
            res.message += "; %s put back to %s" % (dep_name, dep.show(level))
        return res

    def _done(self, res: Result, method: str, by: Optional[str] = None, **extra: Any) -> Result:
        self.log.write("command", by=by or self._who(), method=method, target=res.target, action=res.action,
                       ok=res.ok, value=res.value if not isinstance(res.value, list) else "trace(%d)" % len(res.value),
                       before=res.before, message=res.message, **extra)
        return res

    def _current(self, inst: Instrument, spec: ParamSpec) -> Any:
        """The value now; None when unknown: no read-back and not set since Kelvin connected (Kelvin leaves an
        instrument as it finds it, so a drive is never assumed to be at its card's default)."""
        if spec.get_cmd is None and spec.name not in inst._last and (spec.is_drive or spec.default is None):
            return None
        return inst.read(spec.name)

    def _limit_problem(self, inst: Instrument, spec: ParamSpec, target: Any) -> Optional[str]:
        if not spec.numeric:
            return None
        lim = inst.limits(spec.name)
        if lim is None or lim[0] <= target <= lim[1]:
            return None
        src = inst.limits_source(spec.name)
        why = {"vendor": "the instrument's own range, from its card %s; the lab config sets none tighter" % inst.card.id,
               "lab": "a person changes them with Limits… in Lab → Set up…"}.get(
                   src, "the protection a person set for the mounted device")
        return "%s.%s %s is outside %s limits [%s, %s]%s (%s)" % (
            inst.name, spec.name, spec.show(target), inst.limits_where(spec.name),
            num(lim[0]), num(lim[1]), " " + spec.unit if spec.unit else "", why)

    def _range_problem(self, inst: Instrument, owner: ParamSpec, spec: ParamSpec, target: Any) -> Optional[str]:
        """A source's range (or its autorange) changes only while the source's level is at safe: a range change
        with a level applied can glitch the output. While a device's protection holds the range, autorange stays off."""
        level = self._current(inst, owner)
        if level is None or owner.distance(level) > 0:
            return ("%s.%s changes only while %s.%s is at %s (now %s): changing a source's range with a level "
                    "applied can glitch the output" % (inst.name, spec.name, inst.name, owner.name,
                                                       owner.show(owner.safe), owner.show(level)))
        if spec.name == owner.autorange_param and target and owner.name in inst.device_limits:
            return ("%s keeps %s's range fixed while it is mounted (sample protection), so autorange stays off"
                    % ("device " + inst.device_name if inst.device_name else "the mounted device", inst.name))
        return None

    def _hands_problem(self, what: str) -> Optional[str]:
        """While a person has their hands on the sample, nothing moves away from safe."""
        hands = getattr(self.lab, "hands_on", None)
        if not hands:
            return None
        return ("%s: %s has hands on the sample (since %s), so every drive stays at safe until a person unlocks it "
                "in the Lab panel" % (what, hands.get("by") or "someone", hands.get("since") or "now"))

    def _away_problem(self, inst: Instrument, spec: ParamSpec) -> Optional[str]:
        key = "%s.%s" % (inst.name, spec.name)
        hands = self._hands_problem(key)
        if hands:
            return hands
        for g in inst.guards(spec.name):   # nothing would watch it: a switched-off instrument is never read
            away = self.lab.away(g.instr)
            if away:
                return "%s: its guard %s is on %s, which is %s" % (key, g.key, g.instr, away)
        for lock in self.lab.config.interlocks:
            if lock.drive != key:
                continue
            away = self.lab.away(lock.require.key.split(".")[0])
            if away:
                return "interlock: %s needs %s, but %s is %s" % (key, lock.require.text,
                                                                  lock.require.key.split(".")[0], away)
            ok, actual = lock.require.check(self.lab.read)
            if not ok:
                _, cspec = self.lab.resolve(lock.require.key)
                return "interlock: %s needs %s (now %s)" % (key, lock.require.text, cspec.show(actual))
        return None

    def _covered(self, inst: Instrument, spec: ParamSpec, target: Any, envelope: bool = True) -> Optional[str]:
        """What already approves this change: a person's approval of the plan or queue, or (unless Kelvin isn't sure
        about the instrument) the project envelope."""
        key = "%s.%s" % (inst.name, spec.name)
        grant = self._grants.get(key)
        if grant is not None and grant.covers(target):
            return "plan approval"
        standing = self._standing.get(key)
        if standing is not None and standing.covers(target):
            return "queue approval"
        if envelope and inst.trust == "envelope" and self.envelope is not None and self.envelope.covers(key, target):
            return "envelope"
        return None

    def _guard_text(self, inst: Instrument, spec: ParamSpec) -> str:
        parts = []
        for g in inst.guards(spec.name):
            unit = self.lab.instruments[g.instr].spec(g.param).unit
            parts.append("|%s| > %s%s" % (g.key, num(g.max_abs), " " + unit if unit else ""))
        return "stops if " + " or ".join(parts) if parts else ""

    def _eta(self, inst: Instrument, spec: ParamSpec, current: Any, target: Any) -> Optional[float]:
        if not spec.numeric or current is None:
            return None
        if spec.ramp == "gate":
            return abs(target - current) / inst.rate(spec.name)
        if spec.ramp == "instrument" and spec.rate_param:
            rate = inst.read(spec.rate_param)
            if rate and rate > 0:
                return abs(target - current) / (rate / (60.0 if spec.rate_per == "min" else 1.0))
        return None

    def _describe_move(self, inst: Instrument, spec: ParamSpec, current: Any, target: Any) -> str:
        bits = ["%s.%s %s → %s" % (inst.name, spec.name, spec.show(current), spec.show(target))]
        eta = self._eta(inst, spec, current, target)
        if spec.ramp == "gate":
            bits.append("ramp %s %s/s (~%s)" % (num(inst.rate(spec.name)), spec.unit, _dur(eta)))
        elif eta:
            bits.append("~%s" % _dur(eta))
        g = self._guard_text(inst, spec)
        if g:
            bits.append(g)
        return "; ".join(bits)

    def _move(self, inst: Instrument, spec: ParamSpec, current: Any, target: Any,
              away: bool, wait: bool, how: Optional[str], gen: Optional[int] = None) -> Result:
        key = "%s.%s" % (inst.name, spec.name)
        clock = self.lab.clock
        notes: List[str] = []
        gen = self._stop_gen if gen is None else gen
        for dep_name in spec.enables:  # a switch that applies other drives to the sample
            dep = inst.spec(dep_name)
            dep_now = self._current(inst, dep)
            if dep_now is not None and dep.distance(dep_now) == 0:
                continue
            if away or dep_now is None:  # switching on: park the drive at safe first so nothing jumps
                inst.write(dep.name, dep.safe)
                notes.append("%s set to %s first" % (dep.name, dep.show(dep.safe)))
            else:     # switching off: bring the drive down gently first
                r = self._move(inst, dep, dep_now, dep.safe, away=False, wait=True, how=None)
                if not r.ok:
                    return r
                notes.append("%s ramped to %s first" % (dep.name, dep.show(dep.safe)))
        t0 = clock.now()
        if spec.ramp == "gate" and spec.numeric and current is not None:   # unknown: only ever straight to safe
            stop = self._ramp(inst, spec, current, target, watch=away, gen=gen)
            if stop is not None:
                return stop
        else:
            inst.write(spec.name, target)
            if spec.ramp == "instrument" and spec.readback:
                if wait:
                    stop = self._wait(inst, spec, current, target, watch=away, gen=gen)
                    if stop is not None:
                        return stop
                else:
                    eta = self._eta(inst, spec, current, target)
                    notes.append("ramping" + (", ~%s" % _dur(eta) if eta else ""))
            elif away:
                trip = self._guard_trip(inst, spec)
                if trip:
                    return self._tripped(inst, spec, target, trip)
        err = inst.instrument_error() or self._shown(inst, spec, target)
        if err:
            return Result(False, "error", key, value=target, before=current,
                          message="%s: instrument reported %s" % (key, err))
        elapsed = clock.now() - t0
        msg = "%s %s → %s" % (key, spec.show(current), spec.show(target))
        if elapsed >= 0.5:
            msg += ", %s" % _dur(elapsed)
        if how:
            msg += " (%s)" % how
        if notes:
            msg += "; " + "; ".join(notes)
        return Result(True, "set", key, value=target, before=current, message=msg)

    def _shown(self, inst: Instrument, spec: ParamSpec, target: Any) -> Optional[str]:
        """What a real instrument shows instead of the number Kelvin just set, when it reads back clearly different
        for a while: it refused or clipped the value (a range can depend on its other settings, as a generator's
        amplitude on its load setting), and its card has no error queue that would say so. None when it agrees or
        can't be read back. Not for switches and modes, whose state may take seconds to follow (a laser enabling),
        nor for an instrument that ramps itself (_wait follows it)."""
        if inst.simulated or spec.get_cmd is None or spec.ramp == "instrument" or not spec.numeric:
            return None
        lim = inst.limits(spec.name)
        close = max(0.01 * abs(target), 1e-3 * (lim[1] - lim[0]) if lim else 0.0, 1e-12)   # rounded, or measured
        now = None
        for attempt in range(3):
            if attempt:
                self.lab.clock.sleep(0.3)
            try:
                now = inst.read(spec.name)
            except Exception:
                return None
            if now is None or abs(now - target) <= close:
                return None
        return "that it is at %s, not %s: it may have refused or clipped the value" % (spec.show(now), spec.show(target))

    def _ramp(self, inst: Instrument, spec: ParamSpec, current: float, target: float,
              watch: bool, gen: int) -> Optional[Result]:
        rate = inst.rate(spec.name)
        step = spec.ramp_step or rate * 0.2
        delta = target - current
        n = max(1, int(math.ceil(abs(delta) / step - 1e-9)))
        dwell = abs(delta) / n / rate
        v = current
        for k in range(1, n + 1):
            if watch and self._stop_gen != gen:
                return Result(False, "stopped", "%s.%s" % (inst.name, spec.name), value=v, before=current,
                              message="%s.%s stopped at %s (stop requested)" % (inst.name, spec.name, spec.show(v)))
            v = target if k == n else current + delta * k / n
            if spec.type == "int":
                v = int(round(v))
            inst.write(spec.name, v)
            if watch:
                self._pause(dwell, gen)   # a stop ends the step's dwell; a ramp to safe never stops
            else:
                self.lab.clock.sleep(dwell)
            if watch:
                trip = self._guard_trip(inst, spec)
                if trip:
                    return self._tripped(inst, spec, v, trip)
        return None

    def _wait(self, inst: Instrument, spec: ParamSpec, current: Any, target: float,
              watch: bool, gen: int) -> Optional[Result]:
        """Follow a set point the instrument ramps to by itself until its reading arrives. A stop ends every such
        wait, toward safe too (a PPMS cooling down held a stopped run for ten minutes): away from safe the set point is
        held where the reading is; toward safe it goes on there by itself, and only the waiting ends."""
        clock = self.lab.clock
        key = "%s.%s" % (inst.name, spec.name)
        eta = self._eta(inst, spec, current, target)
        timeout = 1.5 * eta + 60 if eta else spec.timeout
        poll = max(0.1, min(5.0, (eta or 10.0) / 50))
        t0 = clock.now()
        while True:
            val = inst.read(spec.readback)
            if abs(val - target) <= spec.tol:
                return None
            if watch:
                trip = self._guard_trip(inst, spec)
                if trip:
                    return self._tripped(inst, spec, val, trip)
            if self._stop_gen != gen:
                if watch:
                    inst.write(spec.name, val)
                    return Result(False, "stopped", key, value=val,
                                  message="%s stopped at %s (stop requested)" % (key, spec.show(val)))
                return Result(False, "stopped", key, value=val, message="%s: stopped waiting at %s (stop requested); "
                              "it goes on to %s by itself" % (key, spec.show(val), spec.show(target)))
            if clock.now() - t0 > timeout:
                return Result(False, "timeout", key, value=val, message="%s: %s is %s after %s (target %s)" % (
                    key, spec.readback, spec.show(val), _dur(timeout), spec.show(target)))
            self._pause(poll, gen)   # a stop wakes it at once

    def _guard_trip(self, inst: Instrument, spec: ParamSpec) -> Optional[Tuple[Guard, float]]:
        for g in inst.guards(spec.name):
            val = self.lab.read(g.instr, g.param)
            if val is None:  # not readable now (its switch is off), so nothing flows to trip on
                continue
            if abs(val) > g.max_abs:
                return g, val
        return None

    def _tripped(self, inst: Instrument, spec: ParamSpec, at: Any, trip: Tuple[Guard, float]) -> Result:
        g, val = trip
        key = "%s.%s" % (inst.name, spec.name)
        gspec = self.lab.instrument(g.instr).spec(g.param)
        detail = "|%s| = %s > %s" % (g.key, gspec.show(abs(val)), gspec.show(g.max_abs))
        if g.on_trip == "hold":
            if spec.ramp == "instrument" and spec.readback:
                inst.write(spec.name, inst.read(spec.readback))
            end = "holding"
        else:
            r = self._move(inst, spec, at, spec.safe, away=False, wait=False, how=None)
            end = "back to %s" % spec.show(spec.safe) if r.ok else "could not return to safe: " + r.message
        msg = "%s tripped at %s: %s; %s" % (key, spec.show(at), detail, end)
        self.log.write("trip", by=self._who(), target=key, at=at, guard=g.key, reading=val, message=msg)
        return Result(False, "tripped", key, value=at, message=msg)

    def _to_safe(self, inst: Instrument, wait: bool) -> List[Result]:
        out = []
        for name in inst.card.safe_sequence:
            r = self._safe_one(inst, inst.spec(name), wait)
            if r is not None:
                out.append(r)
        return out

    def abort_to_safe(self, keys: List[str], by: str) -> List[Result]:
        """After a stop or a failure: these drives go to safe, each instrument's in its card's safe order, or hold
        where the lab config says hold. Nothing here waits for a set point the instrument ramps to by itself (a stop
        must not hang on a cool-down: it is set and ramps there on its own), and one whose safe value no instrument
        reaches (0 K) holds where its reading is instead. Never raises: each result says what happened."""
        wanted: Dict[str, List[str]] = {}
        for key in keys:
            instr, _, param = key.partition(".")
            wanted.setdefault(instr, []).append(param)
        out: List[Result] = []
        for instr, params in wanted.items():
            inst = self.lab.instruments.get(instr)
            if inst is None:
                continue
            if not inst.enabled:
                out.append(self._done(Result(False, "error", instr, message="%s is %s, so Kelvin could not bring %s to "
                                             "safe" % (instr, self.lab.away(instr), ", ".join(params))), "safe", by))
                continue
            seq = inst.card.safe_sequence
            for param in [p for p in seq if p in params] + [p for p in params if p not in seq]:
                try:
                    r = self._safe_one(inst, inst.spec(param), wait=False)
                except Exception as e:
                    r = Result(False, "error", "%s.%s" % (instr, param),
                               message="%s.%s: instrument error: %s" % (instr, param, e))
                if r is not None:
                    out.append(self._done(r, "safe", by))
        return out

    def _safe_one(self, inst: Instrument, spec: ParamSpec, wait: bool) -> Optional[Result]:
        """One drive to safe (None: already there). Held instead where the lab config says hold, or, when not
        waiting, where a set point's safe value can't be reached."""
        key = "%s.%s" % (inst.name, spec.name)
        if self.lab.config.safe_actions.get(key) == "hold":
            return self._hold(inst, spec, "")
        if not wait and _unreachable(spec):
            return self._hold(inst, spec, " (its safe %s can't be reached)" % spec.show(spec.safe))
        current = self._current(inst, spec)
        if current is not None and spec.distance(current) == 0:
            return None
        return self._move(inst, spec, current, spec.safe, away=False, wait=wait, how="safe state")

    def _hold(self, inst: Instrument, spec: ParamSpec, why: str) -> Optional[Result]:
        """A set point the instrument ramps to by itself stays where its reading is: the ramp stops there."""
        if spec.ramp != "instrument" or not spec.readback:
            return None
        key = "%s.%s" % (inst.name, spec.name)
        actual = inst.read(spec.readback)
        if abs(inst.read(spec.name) - actual) <= spec.tol:
            return None
        inst.write(spec.name, actual)
        return Result(True, "held", key, value=actual, message="%s held at %s%s" % (key, spec.show(actual), why))

    def _prefix(self, spec: ParamSpec) -> Template:
        t = self._prefixes.get(id(spec))
        if t is None:
            t = self._prefixes[id(spec)] = spec.family()
        return t

    def _raw_one(self, inst: Instrument, cmd: str) -> Result:
        card = inst.card
        target = "%s: %s" % (inst.name, cmd)

        def done(res: Result, **extra: Any) -> Result:
            return self._done(res, "raw", None, command=cmd, instrument=inst.name, **extra)

        try:
            for b in card.blocked:
                if b.match(cmd) is not None:
                    return done(Result(False, "refused", target,
                                       message="refused: %r is blocked on %s (firmware/calibration class)" % (cmd, inst.name)))
            if is_query(cmd, bool(card.connection.get("enq"))):
                resp = inst.query_raw(cmd).strip()
                shown = resp if len(resp) <= 80 else resp[:77] + "..."
                return done(Result(True, "read", target, value=resp, message="%s %s → %s" % (inst.name, cmd, shown)))
            for spec in card.setters:
                text = spec.set_cmd.match(cmd)
                if text is None:
                    continue
                try:
                    value = spec.from_wire(text)
                except ValueError as e:
                    return done(Result(False, "refused", target, message="refused: %s" % e))
                res = self._set(inst.name, spec.name, value, True, self._who())
                res.message = "raw %r → %s" % (cmd, res.message)
                return res
            for r in card.reset:
                if r.match(cmd) is not None:
                    prep = self._to_safe(inst, wait=True)
                    bad = [p for p in prep if not p.ok]
                    if bad:
                        return done(Result(False, bad[0].action, target, message="not sent: " + bad[0].message))
                    inst.write_raw(cmd)
                    return done(Result(True, "sent", target, message="%s: drives brought to safe, then sent %r" % (inst.name, cmd)))
            for spec in card.drives:
                if spec.set_cmd is not None and self._prefix(spec).match(cmd) is not None:
                    hands = self._hands_problem("%s.%s" % (inst.name, spec.name))
                    if hands:
                        return done(Result(False, "refused", target, message="refused: %r not sent; %s" % (cmd, hands)))
                    item = ("%s: %r touches drive %s but is not a plain %s setting, so limits can't be checked"
                            % (inst.name, cmd, spec.name, spec.name))
                    request = ApprovalRequest("Send raw command?", [item])
                    if not self.ask(request):
                        note = " (%s)" % request.note if request.note else ""
                        return done(Result(False, "declined", target, message="declined: %r not sent%s" % (cmd, note)))
                    return self._send(inst, cmd, target, done, "approved")
            if card.status == "draft" and not inst.simulated:
                hands = self._hands_problem(inst.name)
                if hands:
                    return done(Result(False, "refused", target, message="refused: %r not sent; %s" % (cmd, hands)))
                item = "%s: %r is not on the (draft) card, so it can't be classified" % (inst.name, cmd)
                if not self.ask(ApprovalRequest("Send raw command?", [item])):
                    return done(Result(False, "declined", target, message="declined: %r not sent" % cmd))
                return self._send(inst, cmd, target, done, "approved")
            return self._send(inst, cmd, target, done, None, unmatched=True)
        except Exception as e:
            return done(Result(False, "error", target, message="%s: instrument error: %s" % (inst.name, e)))

    def _send(self, inst: Instrument, cmd: str, target: str, done: Any, how: Optional[str], **extra: Any) -> Result:
        inst.write_raw(cmd)
        err = inst.instrument_error()
        if err:
            return done(Result(False, "error", target, message="%s: sent %r, instrument reported %s" % (inst.name, cmd, err)), **extra)
        return done(Result(True, "sent", target, message="%s: sent %r%s" % (inst.name, cmd, " (%s)" % how if how else "")), **extra)
