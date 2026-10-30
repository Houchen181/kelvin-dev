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
- Raw commands: what the card lists is checked as such (a setting with its limits, an action, a reset); a source
  function, protection, units or calibration command it doesn't list is refused; anything else unlisted on an
  instrument with drives asks a person.
- A mounted device's protection (protection.py) narrows the limits and rates and adds its gates' leakage guards;
  a source's range changes only while its level is at safe; while a person has their hands on the sample
  (lab.hands_on), nothing moves away from safe.
"""
from __future__ import annotations

import math
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from . import pulses
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
    # pictures the card shows beside its lines, each {"svg": an SVG document, "caption": its numbers in words}: a
    # pulse generator's timing (pulses.illustrate). The lines say everything the pictures do
    figures: List[Dict[str, str]] = field(default_factory=list)

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
    if a is None or b is None:   # a setting with no read-back, not set since connecting: unknown, never the same
        return False
    if spec.numeric:
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-15)
    return a == b


# Raw commands no card lists that change what the instrument is or means, refused unless its card has them as a
# parameter (checked with limits) or an action: the source function (a 2400 put into current mode drove 10 µA into a
# gate oxide), protection limits and compliance, the units its readings are in (an AMI 430 switched to kG), and
# calibration (*CAL? calibrates).
RESTRICTED = {"function": "the source function (what it applies to the sample)",
              "protection": "a protection limit or compliance",
              "units": "the units its values are in",
              "calibration": "its calibration"}
_RESTRICTED_WHY = ("%r changes %s on %s, which its card doesn't list, so Kelvin can't check it; to use it, add it to "
                   "the card as a parameter (Lab → Cards…)")
_HARMLESS = re.compile(r"^\s*\*(CLS|WAI|OPC|ESE|SRE)\b", re.I)   # IEEE 488.2 status housekeeping: never an output
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _restricted(cmd: str) -> Optional[str]:
    """Which kind of RESTRICTED command this is, from the nodes of its SCPI header; None for anything else."""
    text = cmd.strip()
    if re.match(r"^\*CAL\b", text, re.I):
        return "calibration"
    header = re.split(r"[\s?]", text, maxsplit=1)[0]
    if not re.match(r"^:?[A-Za-z][A-Za-z0-9]*(:[A-Za-z][A-Za-z0-9]*)*$", header):
        return None   # not SCPI-shaped (a driver's SET path, a vendor's mnemonic): its card's families decide
    nodes = [re.sub(r"\d+$", "", n).upper() for n in header.lstrip(":").split(":")]
    if nodes[0] in ("CAL", "CALIBRATION"):
        return "calibration"
    if any(n in ("UNIT", "UNITS") for n in nodes):
        return "units"
    if any(n in ("PROT", "PROTECTION", "ILIM", "ILIMIT", "VLIM", "VLIMIT") for n in nodes) or any(
            a in ("VOLT", "VOLTAGE", "CURR", "CURRENT", "POW", "POWER") and b in ("LIM", "LIMIT")
            for a, b in zip(nodes, nodes[1:])):
        return "protection"
    if nodes[0] in ("FUNC", "FUNCTION") or (nodes[0] in ("SOUR", "SOURCE") and len(nodes) > 1
                                             and nodes[1] in ("FUNC", "FUNCTION")):
        return "function"
    return None


def _number_in(cmd: str) -> Optional[float]:
    """The first number after a command's header (its value, as far as Kelvin can tell), or None."""
    parts = re.split(r"[\s,]+", cmd.strip(), maxsplit=1)
    m = _NUMBER.search(parts[1]) if len(parts) > 1 else None
    try:
        return float(m.group(0)) if m else None
    except ValueError:
        return None


def interlock_problem(lab: Any, lock: Any) -> Optional[str]:
    """Why an interlock's condition doesn't hold now, in words (" (now 25 K)"), or None when it holds. A condition that
    can't be read, or reads what it can't be (an open sensor), fails: an interlock never passes on a reading Kelvin
    can't vouch for."""
    try:
        ok, actual = lock.require.check(lab.read)
    except Exception as e:
        return ", but %s can't be read (%s)" % (lock.require.key, str(e).splitlines()[0][:160] if str(e) else
                                                 type(e).__name__)
    if isinstance(actual, float) and not math.isfinite(actual):
        ok = False
    if ok:
        return None
    try:
        shown = lab.resolve(lock.require.key)[1].show(actual)
    except Exception:
        shown = str(actual)
    return " (now %s)" % shown


def _unreachable(spec: ParamSpec) -> bool:
    """A set point the instrument ramps to by itself whose safe value no instrument reaches: absolute zero. After a
    stop it holds where its reading is; waiting for 0 K hung a stopped run until the wait timed out."""
    return (spec.ramp == "instrument" and bool(spec.readback) and spec.unit in ("K", "mK")
            and isinstance(spec.safe, (int, float)) and spec.safe <= 0)


OUTSIDE_S = 1800   # how long auto mode pauses after the model read text from the web, for clients without chats
ARRIVED_SETTLE = 5.0   # lab seconds a reading may take to settle once its stage's status says the move is over
WAIT_STALL_S = 600.0   # a wait for a set point ends once its reading has not got closer for this long (lab seconds)


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
        # the actions that drive the sample (instr.action) its scans start, which the queue's card showed: approved
        # with it (runtime.queue_start), and counted only while one of its items runs (standing)
        self.queue_actions: frozenset = frozenset()
        self._queued = threading.local()   # on the queue's own thread only: another chat's run never borrows them
        # the drive levels the running plan or queue item set: switching their enable on parks them at safe (_move),
        # then puts them back (_reapply), so a run measures at what was approved, not at safe. None: no plan running
        self._asked: Optional[Dict[str, Any]] = None
        # alarm(kind, key, text, actions): what the gate meets while it changes something goes up as an alarm (a
        # guard's trip, a wait for a set point that gave up). The monitor sets it (Monitor.from_gate); None on a twin
        self.alarm: Optional[Callable[..., Any]] = None
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
        if len(parts) > 1 and any(";" in s.set_cmd.text and s.set_cmd.match(command) is not None
                                  for s in inst.card.setters):
            parts = [command]   # the card's own setting is a chain (an AMI 430's TARG {value};RAMP): checked as one
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
        if card.techniques:   # hardware-timed, buffered measurements a plan runs (techniques.py)
            from .techniques import describe as techniques_of
            lines += techniques_of(card)
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

    def _draft(self, inst: Instrument) -> bool:
        """A real instrument on a card nobody has reviewed: even its safe values are unchecked."""
        return inst.card.status == "draft" and not inst.simulated

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
        queued, self._queued.on = getattr(self._queued, "on", False), True
        try:
            yield
        finally:
            self._standing = old
            self._asked = asked
            self._queued.on = queued

    def action_covered(self, key: str) -> bool:
        """An action that drives the sample (instr.action), started by a scan of the queue running now whose card
        showed it: the queue's approval covers it, so the scan doesn't ask again in the night."""
        return getattr(self._queued, "on", False) and key in self.queue_actions

    @contextmanager
    def preapproved(self, grants: Dict[str, Allowed]) -> Iterator[None]:
        old, self._grants = self._grants, dict(grants)
        asked, self._asked = self._asked, {}
        try:
            yield
        finally:
            self._grants = old
            self._asked = asked

    def plan_approval(self, drive_values: Dict[str, List[Any]], forecast: Any = None, asking: bool = False,
                      whole_path: bool = False, later: Iterable[str] = (), noun: str = "plan"
                      ) -> Tuple[List[str], Dict[str, Allowed], List[str]]:
        """For a plan's drive targets: lines to show the human, the ranges to grant, and refusals. forecast: the run
        walked through (forecast.py), for a card that shows every drive it changes, toward safe or inside the envelope
        too, with the rate an instrument moves each at; and that covers a draft card's settings in the plan, which
        would otherwise ask at every step. The lines are empty when nothing needs a person, unless asking: the run
        asks for something else anyway (an action that drives the sample), so its card shows them all.
        whole_path: for a queue, whose items run one after another for hours, and between which a drive may go
        anywhere (an item switches an output off, the next switches it on; an abort sends it to safe): every value
        away from safe counts, not only those beyond where the drive is now, so no item asks again in the night.
        later: drives whose interlock a queue item before theirs works on (a cool-down before a field sweep): their
        interlock is checked when they move, not now, and the line says so. noun: what uses the values, as the
        lines say it (the plan; the script, for a lab's script)."""
        lines: List[Tuple[bool, str]] = []   # (a person approves it, the line)
        grants: Dict[str, Allowed] = {}
        problems: List[str] = []
        later = set(later)

        def shown(inst: Instrument, spec: ParamSpec, current: Any, values: List[Any], why: str = "") -> List[str]:
            key = "%s.%s" % (inst.name, spec.name)
            uses = Allowed.of(spec, values).show(spec)
            bits = ["%s: now %s, %s uses %s%s" % (key, spec.show(current), noun, uses, ", " + why if why else "")]
            if spec.ramp == "gate":
                bits.append("ramp %s %s/s" % (num(inst.rate(spec.name)), spec.unit))
            detail = forecast.detail(key) if forecast is not None else ""
            return bits + ([detail] if detail else [])

        for key, values in drive_values.items():
            inst, spec = self.lab.resolve(key)
            current = forecast.start(key) if forecast is not None else self._current(inst, spec)
            if current is None or whole_path:   # unknown (no read-back), or a queue: every value but safe drives harder
                away = [v for v in values if spec.distance(v) > 0]
            else:
                d0 = spec.distance(current)
                away = [v for v in values if not _same(spec, v, current) and spec.distance(v) >= d0]
            toward = not away and self._draft(inst)
            if toward:   # on a draft card even a change toward safe asks: the plan's one approval covers it (a queue's
                # every value: in the night an item may find the drive changed, at the front panel)
                away = [v for v in values if whole_path or current is None or not _same(spec, v, current)]
            if not away:
                if forecast is not None and key in forecast.changes:   # toward safe: it runs, so it is shown
                    lines.append((False, "; ".join(shown(inst, spec, current, values, "toward its safe value"))))
                continue
            problem = None if toward else self._away_problem(inst, spec, key in later)   # toward safe: no interlock
            standing = self._standing.get(key)
            if not problem and standing is not None and all(standing.covers(v) for v in away):
                continue   # inside what a person approved for the queue this scan is part of
            if problem:
                problems.append(problem)
                continue
            unsure = self.unsure(inst)
            if not unsure and inst.trust == "envelope" and self.envelope and all(self.envelope.covers(key, v)
                                                                                  for v in away):
                if forecast is not None:
                    lines.append((False, "; ".join(shown(inst, spec, current, values, "inside the project envelope"))))
                continue
            grants[key] = Allowed.of(spec, values)
            bits = shown(inst, spec, current, values)
            g = self._guard_text(inst, spec)
            if g:
                bits.append(g)
            if forecast is not None:
                bits += self._interlock_text(key)
            if unsure:
                bits.append("Kelvin isn't sure about %s: %s" % (inst.name, unsure))
            if key in later:
                for lock in self.lab.config.interlocks:
                    broken = interlock_problem(self.lab, lock) if lock.drive == key else None
                    if broken:   # it reads, but doesn't hold yet (one that can't be read was refused above)
                        bits.append("its interlock needs %s, which doesn't hold now%s: an item before it works on "
                                    "that, and it is checked again before %s moves (if it still doesn't hold, the "
                                    "queue pauses there)" % (lock.require.text, broken, key))
            lines.append((True, "; ".join(bits)))
        for key, change in (forecast.settings(every=whole_path) if forecast is not None else {}).items():
            inst, spec = self.lab.resolve(key)
            if not self._draft(inst):
                continue   # a setting on a reviewed card only configures: it never asks
            standing = self._standing.get(key)
            if standing is not None and all(standing.covers(v) for v in change.values):
                continue
            # on a draft card each setting asks (nobody has checked which are drives): this one approval covers them
            grants[key] = Allowed.of(spec, change.values)
            lines.append((True, "; ".join(shown(inst, spec, change.start, change.values) + [
                "Kelvin isn't sure about %s: %s" % (inst.name, self.unsure(inst))])))
        for key, change in (forecast.drives() if forecast is not None else {}).items():
            if key not in drive_values:   # a drive in a part of the run these values don't list: shown all the same
                inst, spec = self.lab.resolve(key)
                lines.append((False, "; ".join(shown(inst, spec, change.start, change.values))))
        # what the person approves first (a phone's notice shows the first line), then what else the run changes
        items = [text for asks, text in lines if asks] + [text for asks, text in lines if not asks]
        if forecast is not None:
            forecast.asked = [text for asks, text in lines if asks]
        return items if asking or any(asks for asks, _ in lines) else [], grants, problems

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
            problem = self._breaks_interlock(inst, spec, target)
            if problem:
                return self._done(Result(False, "refused", key, value=target, message="refused: " + problem), "set", by)
            current = self._current(inst, spec)
            owner = inst.card.range_owner(spec.name)
            if owner is not None and not _same(spec, current, target):
                problem = self._range_problem(inst, owner, spec, target)
                if problem:
                    return self._done(Result(False, "refused", key, value=target, message="refused: " + problem),
                                      "set", by)
            if not spec.is_drive:
                how = None
                if inst.card.status == "draft" and not inst.simulated and not _same(spec, current, target):
                    # nobody has checked this card's measure/drive tags yet: a "setting" might reach the sample. The
                    # plan or queue a person approved covers the settings its card showed (plan_approval)
                    how = self._covered(inst, spec, target, envelope=False)
                    if how is None:
                        request = ApprovalRequest("Change a setting on %s?" % inst.name, [
                            "%s: %s → %s. Its card is a draft no one has reviewed yet, so Kelvin can't be sure this "
                            "only configures the instrument" % (key, spec.show(current), spec.show(target))])
                        if not self.ask(request):
                            note = " (%s)" % request.note if request.note else ""
                            return self._done(Result(False, "declined", key, value=target, message=(
                                "declined: %s stays %s%s" % (key, spec.show(current), note))), "set", by)
                res = self._move(inst, spec, current, target, away=False, wait=wait, how=how, gen=gen)
            elif current is None and spec.distance(target) == 0:   # unknown, to its safe value: always allowed
                res = self._move(inst, spec, current, target, away=False, wait=wait, how="to safe", gen=gen)
            elif current is None and spec.ramp == "gate" and spec.numeric:
                return self._done(Result(False, "refused", key, value=target, message=(
                    "refused: Kelvin can't read %s back and hasn't set it since connecting, so it can't ramp from "
                    "where it is; set it to its safe value (%s) first" % (key, spec.show(spec.safe)))), "set", by)
            elif _same(spec, current, target):
                res = Result(True, "no-change", key, value=target, message="%s already %s" % (key, spec.show(target)))
            elif current is not None and spec.distance(target) < spec.distance(current):
                how = "toward safe"
                if self._draft(inst) and self._covered(inst, spec, target, envelope=False) is None:
                    # nobody has checked this card's safe value against the instrument: "toward safe" is a guess too
                    request = ApprovalRequest("Approve drive change on %s?" % inst.name, [
                        self._describe_move(inst, spec, current, target),
                        "Kelvin isn't sure about %s: %s, so even a change toward its safe value (%s) asks" % (
                            inst.name, self.unsure(inst), spec.show(spec.safe))])
                    if not self.ask(request):
                        note = " (%s)" % request.note if request.note else ""
                        return self._done(Result(False, "declined", key, value=current, message="declined: %s stays %s%s"
                                                 % (key, spec.show(current), note)), "set", by)
                    how = "toward safe, approved"
                res = self._move(inst, spec, current, target, away=False, wait=wait, how=how, gen=gen)
            else:
                if gen is not None and self._stop_gen != gen:   # stopped since the plan began: nothing goes up
                    return self._done(Result(False, "stopped", key, value=current, message="%s stays %s (stop requested)"
                                             % (key, spec.show(current))), "set", by)
                problem = self._away_problem(inst, spec) or self._fixed_range_problem(inst, spec, target)
                if problem:
                    return self._done(Result(False, "refused", key, value=target, message="refused: " + problem), "set", by)
                unsure = self.unsure(inst)
                how = self._covered(inst, spec, target, envelope=not unsure)
                if how is None:
                    request = ApprovalRequest("Approve drive change on %s?" % inst.name,
                                              [self._describe_move(inst, spec, current, target)])
                    pulses.illustrate(request, inst, {spec.name: target})   # a generator's timing, if it starts one
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

    def fixed_range(self, inst: Instrument, spec: ParamSpec) -> Optional[float]:
        """A source's present range while it is fixed (its autorange off, or it has none), which its level can't pass;
        None while it ranges itself, or for a drive with no range on its card."""
        if not spec.range_param or not spec.numeric:
            return None
        if spec.autorange_param:
            auto = inst.read(spec.autorange_param)
            if auto is None or auto:
                return None
        rng = inst.read(spec.range_param)
        return float(rng) if isinstance(rng, (int, float)) and not isinstance(rng, bool) and rng > 0 else None

    def _fixed_range_problem(self, inst: Instrument, spec: ParamSpec, target: Any,
                             rng: Optional[float] = None) -> Optional[str]:
        """A level beyond the source's fixed range: the instrument would clip or refuse it partway through a sweep
        (a 2400 held on its 21 V range under a ±60 V limit stopped at -21 V). rng: the range to check against."""
        if not isinstance(target, (int, float)):
            return None
        if rng is None:
            try:
                rng = self.fixed_range(inst, spec)
            except Exception:   # its range can't be read now: the change's own checks say what the instrument does
                return None
        if rng is None or abs(target) <= rng * (1 + 1e-9):
            return None
        bigger = next((r for r in spec.range_steps if r >= abs(target) * (1 - 1e-9)), None)
        return "%s.%s %s is beyond its fixed range (%s)%s" % (
            inst.name, spec.name, spec.show(target), spec.show(rng),
            ": set %s.%s to %s first, while %s.%s is at %s" % (inst.name, spec.range_param, spec.show(bigger),
                                                                inst.name, spec.name, spec.show(spec.safe))
            if bigger is not None else "")

    def _hands_problem(self, what: str) -> Optional[str]:
        """While a person has their hands on the sample, nothing moves away from safe."""
        hands = getattr(self.lab, "hands_on", None)
        if not hands:
            return None
        return ("%s: %s has hands on the sample (since %s), so every drive stays at safe until a person unlocks it "
                "in the Lab panel" % (what, hands.get("by") or "someone", hands.get("since") or "now"))

    def _away_problem(self, inst: Instrument, spec: ParamSpec, later: bool = False) -> Optional[str]:
        """Why this drive can't move away from safe now (hands on the sample, a guard or interlock that can't be
        watched or doesn't hold), or None. later: a queue's start, for a drive whose interlock an item before it works
        on: a condition that doesn't hold yet is left for the check when it moves (one whose instrument is off isn't)."""
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
            broken = interlock_problem(self.lab, lock)
            if broken and not (later and broken.startswith(" (now")):   # one that can't be read is never left
                return "interlock: %s needs %s%s" % (key, lock.require.text, broken)
        return None

    def _breaks_interlock(self, inst: Instrument, spec: ParamSpec, target: Any) -> Optional[str]:
        """Setting the condition of an interlock (a switch, a set point it names) is checked against it too: refused
        when the new value fails it while its drive is away from safe. A reading that drifts out of it is the
        monitor's to catch (Monitor._check_interlocks)."""
        key = "%s.%s" % (inst.name, spec.name)
        for lock in self.lab.config.interlocks:
            if lock.require.key != key:
                continue
            ok, _ = lock.require.check(lambda i, p: target)
            if ok:
                continue
            try:
                dinst, dspec = self.lab.resolve(lock.drive)
                now = self._current(dinst, dspec) if dinst.enabled else None
            except Exception:
                now = None
            if now is not None and dspec.distance(now) == 0:
                continue   # its drive is at safe: nothing the interlock protects
            return ("interlock: %s (now %s) needs %s; setting %s to %s would break it, so bring %s to safe first"
                    % (lock.drive, dspec.show(now) if now is not None else "unknown", lock.require.text, key,
                       spec.show(target), lock.drive))
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

    def _interlock_text(self, key: str) -> List[str]:
        """The interlocks on a drive, with their condition as it reads now, for its card (a heating card once didn't
        say the field was up, nor that an interlock tied the two)."""
        out = []
        for lock in self.lab.config.interlocks:
            if lock.drive != key:
                continue
            try:
                actual = lock.require.check(self.lab.read)[1]
                now = self.lab.resolve(lock.require.key)[1].show(actual)
            except Exception:
                now = "?"
            out.append("interlock: needs %s (now %s)" % (lock.require.text, now))
        return out

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
        """A change on its card: from and to, how fast (the gate's ramp, or the rate the instrument moves at, read
        from it) and for how long, and for an output going on, what it applies and how the instrument is set."""
        from .forecast import applies, mode_words, rate_words, read_now
        bits = ["%s.%s %s → %s" % (inst.name, spec.name, spec.show(current), spec.show(target))]
        eta = self._eta(inst, spec, current, target)
        if spec.ramp == "gate":
            bits.append("ramp %s %s/s (~%s)" % (num(inst.rate(spec.name)), spec.unit, _dur(eta)))
        elif spec.ramp == "instrument" and spec.numeric:
            rate, words = rate_words(self, inst, spec)
            seconds = abs(target - current) / rate if rate and current is not None else None
            bits.append(words + (", ~%s" % _dur(seconds) if seconds and seconds >= 1 else ""))
        elif eta:
            bits.append("~%s" % _dur(eta))
        if spec.enables and spec.distance(target) > 0:   # an output going on: it once went on into current mode
            now = lambda key: read_now(self, key)   # noqa: E731
            bits.append(applies(inst, spec, now, set(self._asked or {})))
            mode = mode_words(inst, now)
            if mode:
                bits.append(mode)
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
        stale, standing = self._drain(inst)   # older errors are not this change's
        if stale:
            notes.append(("it shows a standing error from before: %s" if standing else
                          "older errors in its queue, cleared first: %s") % "; ".join(sorted(set(stale))[:3]))
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
        moving = spec.busy_cmd is not None or spec.arrived_cmd is not None   # it says when it is there
        if spec.ramp == "gate" and spec.numeric and current is not None:   # unknown: only ever straight to safe
            stop = self._ramp(inst, spec, current, target, watch=away, gen=gen)
            if stop is not None:
                return stop
        else:
            inst.write(spec.name, target)
            if wait and (moving or (spec.ramp == "instrument" and spec.readback)):
                err = self._new_error(inst, stale, standing)   # a refused move (a stage not homed) says so now,
                if err:                                          # not after the whole wait for it
                    return Result(False, "error", key, value=target, before=current,
                                  message="%s: instrument reported %s" % (key, err))
            if moving and wait:   # its read-back means something only once it is there
                stop = self._arrive(inst, spec, current, target, gen)
                if stop is not None:
                    return stop
            elif moving:
                notes.append("moving")
            if spec.ramp == "instrument" and spec.readback:
                if wait:
                    stop = self._wait(inst, spec, current, target, watch=away, gen=gen, arrived=moving)
                    if stop is not None:
                        return stop
                else:
                    eta = self._eta(inst, spec, current, target)
                    notes.append("ramping" + (", ~%s" % _dur(eta) if eta else ""))
            elif away:
                trip = self._guard_trip(inst, spec)
                if trip:
                    return self._tripped(inst, spec, target, trip)
        err = self._new_error(inst, stale, standing) or (self._shown(inst, spec, target) if wait or not moving
                                                          else None)
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

    def _drain(self, inst: Instrument) -> Tuple[List[str], bool]:
        """The errors already in an instrument's queue before a command, read out of the way and logged, so they are
        never blamed on it (field tests: stale errors were reported as a later safe move's, and queues overflowed)."""
        if inst.card.error_query is None:
            return [], False
        try:
            stale, standing = inst.drain_errors()
        except Exception:
            return [], False   # the queue doesn't answer: the command's own check says so
        if stale:
            self.log.write("stale_errors", by=self._who(), instrument=inst.name, errors=stale, standing=standing or None)
        return stale, standing

    def _new_error(self, inst: Instrument, stale: List[str], standing: bool) -> Optional[str]:
        """The command's own error: what the queue holds now, unless it is the standing status it showed before."""
        err = inst.instrument_error()
        return None if err is not None and standing and err == stale[-1] else err

    def _shown(self, inst: Instrument, spec: ParamSpec, target: Any) -> Optional[str]:
        """What a real instrument shows instead of the number Kelvin just set, when it reads back clearly different
        for a while: it refused or clipped the value (a range can depend on its other settings, as a generator's
        amplitude on its load setting), and its card has no error queue that would say so. None when it agrees or
        can't be read back. Not for switches and modes, whose state may take seconds to follow (a laser enabling),
        nor for an instrument that ramps itself (_wait follows it)."""
        if inst.card.status == "draft" and not inst.simulated and spec.get_cmd is not None and not spec.numeric \
                and spec.type in ("bool", "enum") and spec.ramp != "instrument":
            # a draft card's command may be one the instrument doesn't take (a long form it answers -113 to): its
            # switch or mode is read back too, given a moment to follow, before the change counts as made
            now = None
            for attempt in range(4):
                if attempt:
                    self.lab.clock.sleep(0.5)
                try:
                    now = inst.read(spec.name)
                except Exception:
                    return None
                if now is None or now == target:
                    return None
            return "that it is %s, not %s: the draft card's command may not be one it takes" % (spec.show(now),
                                                                                              spec.show(target))
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
              watch: bool, gen: int, arrived: bool = False) -> Optional[Result]:
        """Follow a set point the instrument ramps to by itself until its reading arrives. A stop ends every such
        wait, toward safe too (a PPMS cooling down held a stopped run for ten minutes): away from safe the set point is
        held where the reading is; toward safe it goes on there by itself, and only the waiting ends. arrived: its
        status already says the move is over, so the reading only settles: a stage that stopped short isn't waited for
        until the move's whole timeout. A reading that stops getting closer for WAIT_STALL_S ends the wait too (a PPMS
        that stalled on its way held a run, its current source on, for hours): monitor.Approach tells. A wait that
        gives up goes up as an alarm (it stays until a person says Got it), and says the switch the set point goes
        through when that may be why (a heater range too low). A move away from safe whose switch is off (a heater
        range at off) isn't waited for at all: nothing brings the reading there (it was waited on for ten minutes,
        then called stuck)."""
        from .monitor import Approach, rate_per_s, switch_words
        clock = self.lab.clock
        key = "%s.%s" % (inst.name, spec.name)
        eta = self._eta(inst, spec, current, target)
        timeout = ARRIVED_SETTLE if arrived else 1.5 * eta + 60 if eta else spec.timeout
        poll = max(0.1, min(5.0, (eta or 10.0) / 50))
        t0 = clock.now()
        way = None if arrived else Approach(target, spec.tol, t0, rate_per_s(inst, spec), WAIT_STALL_S)
        off = self._switched_off(inst, spec) if watch and not arrived else None
        if off:
            val = inst.read(spec.readback)
            if abs(val - target) > spec.tol:
                return self._gave_up(inst, spec, val, target, watch, "%s: %s is %s, so nothing brings %s (%s) to %s, "
                                     "and Kelvin didn't wait for it" % (key, off[0], off[1], spec.readback,
                                                                       spec.show(val), spec.show(target)),
                                     " (switch %s on first, or set both in one plan's setup)" % off[0], alarm=False)
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
            if way is not None and way.update(val, clock.now()) >= WAIT_STALL_S:
                switch = switch_words(inst, spec)
                return self._gave_up(inst, spec, val, target, watch, "%s: %s has not got closer to %s for %s (it reads "
                                     "%s)%s, so Kelvin stopped waiting" % (
                                         key, spec.readback, spec.show(target), _dur(clock.now() - way.since),
                                         spec.show(val) + ("; %s" % switch if switch else ""),
                                         "" if switch else ": the instrument may be stuck"))
            if clock.now() - t0 > timeout:
                if arrived:
                    return Result(False, "error", key, value=val, message=(
                        "%s: its status says the move is over, but %s is %s, not %s: it stopped short or refused the "
                        "move" % (key, spec.readback, spec.show(val), spec.show(target))))
                return self._gave_up(inst, spec, val, target, watch, "%s: %s is %s after %s (target %s)" % (
                    key, spec.readback, spec.show(val), _dur(timeout), spec.show(target)))
            self._pause(poll, gen)   # a stop wakes it at once

    def _switched_off(self, inst: Instrument, spec: ParamSpec) -> Optional[Tuple[str, str]]:
        """("tc.heater_range", "off") when the switch a set point reaches the sample through (its applies_when) is at
        safe: nothing drives its reading there. None otherwise, or when it can't be read."""
        switch = inst.card.params.get(spec.applies_when) if spec.applies_when else None
        try:
            now = inst.read(switch.name) if switch is not None else None
        except Exception:
            return None
        if now is None or switch.distance(now) > 0:
            return None
        return "%s.%s" % (inst.name, switch.name), switch.show(now)

    def _gave_up(self, inst: Instrument, spec: ParamSpec, val: Any, target: Any, away: bool, text: str,
                 after: str = "", alarm: bool = True) -> Result:
        """A wait for a set point that ends before its reading got there. A move away from safe is set back to where
        its reading got to, as a stop leaves it (its guards are watched only while Kelvin waits, so it must not go on
        by itself unwatched), and the result says so: the set point a person approved was once put back silently.
        alarm: it goes up as one (gate.alarm), before the set point changes, so the watchdog's alarm for it becomes
        this one."""
        key = "%s.%s" % (inst.name, spec.name)
        if away:
            text += "; %s set back to %s, where %s is, not %s" % (key, spec.show(val), spec.readback, spec.show(target))
        text += after
        if alarm:
            self._raise_alarm("stall", key, text, target=target)
        if away:
            inst.write(spec.name, val)
        return Result(False, "timeout", key, value=val, message=text)

    def _raise_alarm(self, kind: str, key: str, text: str, actions: Optional[List[str]] = None, **more: Any) -> None:
        """An alarm for the monitor (gate.alarm), if it is listening. Never in the gate's way."""
        if self.alarm is None:
            return
        try:
            self.alarm(kind, key, text, actions, **more)
        except Exception:
            pass

    def _arrive(self, inst: Instrument, spec: ParamSpec, current: Any, target: Any, gen: int) -> Optional[Result]:
        """Follow a move the instrument makes by itself (a stage, a piezo) until its card's status says it is there:
        a delay line read back mid-move looked like a refused value, and every run stopped at its first point. A stop
        ends the wait."""
        clock = self.lab.clock
        key = "%s.%s" % (inst.name, spec.name)
        eta = self._eta(inst, spec, current, target)
        timeout = 1.5 * eta + 60 if eta else spec.timeout
        poll = max(0.05, min(1.0, (eta or 5.0) / 50))
        t0 = clock.now()
        while True:
            if self._arrived(inst, spec):
                return None
            if self._stop_gen != gen:
                return Result(False, "stopped", key, value=target, before=current, message=(
                    "%s: stopped waiting for it to reach %s (stop requested)" % (key, spec.show(target))))
            if clock.now() - t0 > timeout:
                return Result(False, "timeout", key, value=target, before=current, message=(
                    "%s: its status says it isn't at %s after %s" % (key, spec.show(target), _dur(timeout))))
            self._pause(poll, gen)

    def _arrived(self, inst: Instrument, spec: ParamSpec) -> bool:
        if spec.busy_cmd is not None and re.search(spec.busy_re, inst.query_raw(spec.busy_cmd.render()).strip()):
            return False
        if spec.arrived_cmd is not None and not re.search(spec.arrived_re,
                                                          inst.query_raw(spec.arrived_cmd.render()).strip()):
            return False
        return True

    def _guard_trip(self, inst: Instrument, spec: ParamSpec) -> Optional[Tuple[Guard, Any]]:
        """The first guard reading past its limit. One that can't be read, or isn't a number (NaN), trips too: a ramp
        nobody can watch doesn't go on (a NaN leakage once let a sweep run unguarded)."""
        for g in inst.guards(spec.name):
            try:
                val = self.lab.read(g.instr, g.param)
            except Exception as e:
                return g, e
            if val is None:  # not readable now (its switch is off), so nothing flows to trip on
                continue
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not math.isfinite(val) \
                    or abs(val) > g.max_abs:
                return g, val
        return None

    def _tripped(self, inst: Instrument, spec: ParamSpec, at: Any, trip: Tuple[Guard, Any]) -> Result:
        g, val = trip
        key = "%s.%s" % (inst.name, spec.name)
        gspec = self.lab.instruments[g.instr].spec(g.param)
        if isinstance(val, Exception):
            detail = "%s can't be read (%s)" % (g.key, str(val).splitlines()[0][:160] if str(val) else type(val).__name__)
        elif not isinstance(val, (int, float)) or isinstance(val, bool) or not math.isfinite(val):
            detail = "%s reads %r, not a number" % (g.key, val)
        else:
            detail = "|%s| = %s > %s" % (g.key, gspec.show(abs(val)), gspec.show(g.max_abs))
        if g.on_trip == "hold":
            if spec.ramp == "instrument" and spec.readback:
                inst.write(spec.name, inst.read(spec.readback))
            end = "holding"
        else:
            end = self._cut(inst, spec, at)[1]
        msg = "%s tripped at %s: %s; %s" % (key, spec.show(at), detail, end)
        self.log.write("trip", by=self._who(), target=key, at=at, guard=g.key,
                       reading=val if not isinstance(val, Exception) else str(val), message=msg)
        # an alarm that stays until a person says Got it: a gate's leakage trip in the night went into the record and
        # nowhere else, and the morning's account said no alarm was raised
        self._raise_alarm("trip", key, msg)
        return Result(False, "tripped", key, value=at, message=msg)

    def _switch_of(self, inst: Instrument, spec: ParamSpec) -> Optional[ParamSpec]:
        """The switch that applies this drive to the sample (its applies_when, or an output whose enables name it)."""
        names = ([spec.applies_when] if spec.applies_when else []) + [p.name for p in inst.card.drives
                                                                      if spec.name in p.enables]
        for name in names:
            s = inst.card.params.get(name)
            if s is not None and s.is_drive and s.type in ("bool", "enum") and s.set_cmd is not None:
                return s
        return None

    def _cut(self, inst: Instrument, spec: ParamSpec, at: Any) -> Tuple[List[Result], str]:
        """A drive back to safe, and the switch that applies it to the sample off: a leakage trip that only set V = 0
        did nothing on a source someone had put in current mode. The moves' results, and what they did in words."""
        results: List[Result] = []
        words: List[str] = []
        if at is None or spec.distance(at) > 0:
            r = self._move(inst, spec, at, spec.safe, away=False, wait=False, how=None)
            results.append(r)
            words.append("back to %s" % spec.show(spec.safe) if r.ok else "could not return to safe: " + r.message)
        switch = self._switch_of(inst, spec)
        if switch is not None:
            try:
                now = self._current(inst, switch)
                if now is None or switch.distance(now) > 0:
                    r = self._move(inst, switch, now, switch.safe, away=False, wait=False, how=None)
                    results.append(r)
                    words.append("%s %s" % (switch.name, switch.show(switch.safe)) if r.ok else
                                 "%s could not be switched %s: %s" % (switch.name, switch.show(switch.safe), r.message))
            except Exception as e:
                words.append("%s could not be switched %s: %s" % (switch.name, switch.show(switch.safe), e))
        return results, "; ".join(words) or "already at %s" % spec.show(spec.safe)

    def cut(self, instr: str, param: str, by: str) -> List[Result]:
        """A watch's action (monitor.py): the drive to safe and the switch that applies it off, without asking (only
        ever toward safe); a drive the lab config holds is held instead."""
        key = "%s.%s" % (instr, param)
        try:
            inst = self.lab.instrument(instr)
            spec = inst.spec(param)
        except KeyError as e:
            return [self._done(Result(False, "refused", key, message="refused: %s" % e.args[0]), "safe", by)]
        try:
            if self.lab.config.safe_actions.get(key) == "hold":
                held = self._hold(inst, spec, " (the lab config holds it)")
                results = [held] if held is not None else []
            else:
                results = self._cut(inst, spec, self._current(inst, spec))[0]
        except Exception as e:
            results = [Result(False, "error", key, message="%s: instrument error: %s" % (key, e))]
        return [self._done(r, "safe", by) for r in results]

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

        def refused(why: str) -> Result:
            return done(Result(False, "refused", target, message="refused: " + why))

        def asked(item: str) -> Result:
            request = ApprovalRequest("Send raw command?", [item])
            if not self.ask(request):
                note = " (%s)" % request.note if request.note else ""
                return done(Result(False, "declined", target, message="declined: %r not sent%s" % (cmd, note)))
            return self._send(inst, cmd, target, done, "approved")

        try:
            for b in card.blocked:
                if b.match(cmd) is not None:
                    return refused("%r is blocked on %s (firmware/calibration class)" % (cmd, inst.name))
            if _restricted(cmd) == "calibration":   # *CAL? is a query that calibrates
                return refused("%r is blocked on %s (firmware/calibration class)" % (cmd, inst.name))
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
                    return refused(str(e))
                res = self._set(inst.name, spec.name, value, True, self._who())
                res.message = "raw %r → %s" % (cmd, res.message)
                return res
            for name, action in card.actions.items():   # the card's own named command: as do_action sends it
                if len(action.send) == 1 and action.send[0].strip().lower() == cmd.strip().lower():
                    return self.act(inst.name, name)
            for r in card.reset:
                if r.match(cmd) is not None:
                    prep = self._to_safe(inst, wait=True)
                    bad = [p for p in prep if not p.ok]
                    if bad:
                        return done(Result(False, bad[0].action, target, message="not sent: " + bad[0].message))
                    inst.write_raw(cmd)
                    return done(Result(True, "sent", target, message="%s: drives brought to safe, then sent %r" % (inst.name, cmd)))
            restricted = _restricted(cmd)
            for spec in card.drives:
                if spec.set_cmd is not None and self._prefix(spec).match(cmd) is not None:
                    key = "%s.%s" % (inst.name, spec.name)
                    hands = self._hands_problem(key)
                    if hands:
                        return refused("%r not sent; %s" % (cmd, hands))
                    if restricted:
                        return refused(_RESTRICTED_WHY % (cmd, RESTRICTED[restricted], inst.name))
                    return self._raw_drive_family(inst, spec, cmd, refused, asked)
            if restricted:
                return refused(_RESTRICTED_WHY % (cmd, RESTRICTED[restricted], inst.name))
            if card.status == "draft" and not inst.simulated:
                hands = self._hands_problem(inst.name)
                if hands:
                    return refused("%r not sent; %s" % (cmd, hands))
                return asked("%s: %r is not on the (draft) card, so it can't be classified" % (inst.name, cmd))
            if card.drives and not _HARMLESS.match(cmd):   # it could reach the sample: a person decides
                hands = self._hands_problem(inst.name)
                if hands:
                    return refused("%r not sent; %s" % (cmd, hands))
                return asked("%s: %r is not on its card, so Kelvin can't tell what it does; %s has drives that reach "
                             "the sample (%s)" % (inst.name, cmd, inst.name, ", ".join(s.name for s in card.drives)))
            return self._send(inst, cmd, target, done, None, unmatched=True)
        except Exception as e:
            return done(Result(False, "error", target, message="%s: instrument error: %s" % (inst.name, e)))

    def _raw_drive_family(self, inst: Instrument, spec: ParamSpec, cmd: str, refused: Any, asked: Any) -> Result:
        """A raw command in a drive's family that isn't its plain setting (a sweep's stop value, a mode): Kelvin can't
        be sure what it applies. Refused on a drive with an interlock (the interlock can't be checked against it), or
        when the number it carries is beyond the drive's limits; otherwise a person sees that number against the
        limits, and decides."""
        key = "%s.%s" % (inst.name, spec.name)
        locks = [lk.require.text for lk in self.lab.config.interlocks if lk.drive == key]
        if locks:
            return refused("%r touches drive %s, which an interlock guards (%s): only its plain setting can be checked "
                           "against it, so it isn't sent" % (cmd, key, "; ".join(locks)))
        problem = self._away_problem(inst, spec)
        if problem:
            return refused("%r not sent; %s" % (cmd, problem))
        bits = ["%s: %r touches drive %s but is not a plain %s setting" % (inst.name, cmd, spec.name, spec.name)]
        lim = inst.limits(spec.name) if spec.numeric else None
        number = _number_in(cmd)
        unit = " " + spec.unit if spec.unit else ""
        if lim is not None and number is not None:
            if not lim[0] <= number <= lim[1]:
                return refused("%r carries %s%s, beyond %s's limits [%s, %s]%s" % (
                    cmd, num(number), unit, key, num(lim[0]), num(lim[1]), unit))
            bits.append("it carries %s%s, within %s's limits [%s, %s]%s (now %s)" % (
                num(number), unit, key, num(lim[0]), num(lim[1]), unit, spec.show(self._current(inst, spec))))
        else:
            bits.append("Kelvin finds no value in it to check against %s's limits" % key)
        g = self._guard_text(inst, spec)
        if g:
            bits.append("guards are not watched for it (%s)" % g)
        return asked("; ".join(bits))

    def _send(self, inst: Instrument, cmd: str, target: str, done: Any, how: Optional[str], **extra: Any) -> Result:
        stale, standing = self._drain(inst)
        inst.write_raw(cmd)
        err = self._new_error(inst, stale, standing)
        if err:
            return done(Result(False, "error", target, message="%s: sent %r, instrument reported %s" % (inst.name, cmd, err)), **extra)
        return done(Result(True, "sent", target, message="%s: sent %r%s" % (inst.name, cmd, " (%s)" % how if how else "")), **extra)
