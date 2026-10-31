"""What the agent can do, as short plain-text answers (feature #9: concise).

All instrument access goes through the safety gate. Changes to the lab go one at a time under the lab's lock, and
each instrument has a lock of its own for a reading (lablock.py): a reading never waits for a run, and never sends a
command to an instrument the run is using (its latest values instead). The approval page reads cached state and
never waits on the lock.
"""
from __future__ import annotations

import contextlib
import contextvars
import csv
import io
import json
import math
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import yaml

from .. import frames, pulses
from ..cards import ParamSpec, num, plot_points, thin, trace_x
from ..engine import RunEngine, RunReport
from ..eventlog import EventLog, RunFolderError, RunFolders, RunStore
from ..gate import ApprovalRequest, Approver, SafetyGate, _dur
from ..lab import Lab
from ..lablock import HELD, LabLock
from ..labconfig import Allowed, ConfigError, Envelope, load_lab_config
from .. import carddraft
from .. import protection
from .. import rundata
from ..literature import Literature
from ..monitor import Monitor, Watch
from ..notify import Notifier
from ..plans import Plan, PlanError, load_plan
from ..scanqueue import Item, ScanQueue, load_named, load_step, parse_item, save_named, saved_queues
from ..reports import Collector, pick_runs, write_pdf, write_pptx
from ..reports import kinds as report_kinds
from ..reports.document import write_document_pdf, write_document_pptx
from ..yamlio import load_yaml

if TYPE_CHECKING:
    from ..catalog import Catalog

MAX_WAIT_S = 3600
IDLE_OFF_S = 3600.0   # light left on this long with nothing happening goes to safe (None or 0: never)
LIGHT = ("laser", "laser-driver", "light-source", "led", "lamp")   # card kinds whose output is light on the sample
BACKGROUND_S = 5.0    # how often Kelvin looks for that, and (once, after a start) for what a restart left
BUSY_WAIT_S = 30.0   # how long a request waits for the lab while another holds it, before it answers "busy"
READ_WAIT_S = 10.0   # how long a reading waits for another reading of the same instrument, before it gives the latest
PANEL_WAIT_S = 0.2   # the same, for the Lab panel's look every few seconds: it shows the latest values meanwhile
MAX_CONFIG_CHARS = 30000   # of the lab config shown at once; an instrument's own entry is always shown whole
SETUP_STEPS = ("connecting", "scanning", "drafting", "reading")   # the instrument setup at work (autosetup.py)
# a rehearsal is never a run (field tests: run_plan with dry_run: true went on to run for real)
REHEARSAL = ("REHEARSAL ONLY, on the simulated lab: nothing was sent to the instruments. A real run is its own call "
             "(%s without dry_run), made when the person asks for it.")
SCRIPTS_NEXT = ("Next, with the person (nothing above was sent to any instrument): add each instrument that is not in "
                "the lab yet with add_instrument at its address, with the likely card and the connection settings "
                "above (Kelvin checks who answers there first); one with no card fitting: draft_card, saying which "
                "commands the scripts use, as those are the ones this lab relies on. The limits above come from the "
                "values the scripts use: propose them and set them only once the person agrees. Then offer to keep "
                "each measurement script as a recipe (save_recipe; it shows in the chat): its loops become sweeps "
                "(an outer loop, the outer axis), its readings the measure list, its waits the settle times, its "
                "setup and its end the setup and after steps; say what it can't carry over (the script's own "
                "processing, plotting). Never run the scripts.")


def _light(card: Any) -> bool:
    """A light source on the sample (a laser, a laser driver, an LED or lamp source)."""
    return card.kind in LIGHT or "laser" in card.kind


def _write_json(path: Path, data: Any) -> None:
    """Written whole, then put in place: a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def default_project_info(project_id: Optional[str]) -> Dict[str, Any]:
    """Without the portal there are no projects: everything is the General workspace."""
    return {"id": project_id, "name": "General" if not project_id else project_id, "devices": [], "notes": [],
            "references": [], "summary": "", "private": False, "unshared": False}


def _limits_text(limits: Dict[str, Tuple[float, float]]) -> str:
    return ", ".join("%s [%s, %s]" % (p, num(lo), num(hi)) for p, (lo, hi) in sorted(limits.items())) or "none"


def tail_events(path: Path, n: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = min(size, 256 * 1024)
        f.seek(size - block)
        lines = f.read().decode("utf-8", "replace").splitlines()
    if block < size:
        lines = lines[1:]  # the first line may be cut
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def event_line(e: Dict[str, Any]) -> str:
    t = str(e.get("wall", ""))[11:19]
    kind = e.get("type")
    if kind in ("command", "trip"):
        text = e.get("message", "")
    elif kind == "ask":
        lines = str(e.get("request", "")).splitlines()
        what = lines[1].strip(" -") if len(lines) > 1 else (lines[0] if lines else "")
        note = str(e.get("note") or "")
        said = note if note.startswith(str(e.get("answer"))) else "%s%s" % (e.get("answer"), " (%s)" % note if note
                                                                           else "")
        return "%s %s (asked by %s): %s" % (t, said, e.get("by") or "?", what)
    elif kind == "answer":
        from .approval import who_text
        text = "%s %s: %s" % (who_text({"account": e.get("account"), "channel": e.get("channel"),
                                        "device": e.get("device"), "vouched": e.get("vouched")}),
                              e.get("answer"), e.get("request"))
        return "%s %s" % (t, text)
    elif kind == "config_version":   # the lab's config history (configversions.py)
        text = "%s #%s%s: %s" % (e.get("title"), e.get("version"), " (rollback to #%s)" % e["rollback"]
                                 if e.get("rollback") else "", "; ".join(e.get("changes") or []) or "changed")
    elif kind == "lab_roles":
        text = "lab roles: %s" % (", ".join("%s %s" % kv for kv in sorted((e.get("roles") or {}).items()))
                                  or "none (everyone equal)")
    elif kind == "run_start":
        text = "run %s started, %s points" % (e.get("run"), e.get("points"))
    elif kind == "run_end":
        text = "run %s %s, %s points" % (e.get("run"), e.get("status"), e.get("points"))
    elif kind == "run_refused":
        text = "plan %s refused: %s" % (e.get("plan"), e.get("reason"))
    elif kind == "run_declined":
        text = "plan %s declined" % e.get("plan")
    elif kind == "preflight":
        text = "preflight ok" if e.get("ok") else "preflight FAILED: " + "; ".join(
            r for r in e.get("results", []) if "FAIL" in r)
    elif kind == "safe_state":  # older logs
        text = "SAFE STATE: " + ("; ".join(e.get("results") or []) or "all drives already safe")
    elif kind == "stop":
        text = "stopped the current ramp or plan" + (
            "; declined %d open request(s)" % e["declined"] if e.get("declined") else "") + (
            " (asked: %s)" % e["note"] if e.get("note") else "")
    elif kind == "incident":
        text = "after a restart: %s" % "; ".join(
            "%s %s" % (k, ", ".join(map(str, v)) if isinstance(v, list) else v) for k, v in e.items()
            if k in ("outputs", "runs", "queue", "alarms", "safe"))
    elif kind == "idle_off":
        text = "left on for %s with nothing happening, put to safe: %s" % (e.get("idle"),
                                                                          "; ".join(e.get("steps") or []))
    elif kind == "idle_on":
        text = "still on after %s with nothing happening (left as they are): %s" % (
            e.get("idle"), ", ".join(e.get("still") or []))
    elif kind == "call_cancelled":
        text = "the client cancelled %s%s%s" % (e.get("tool"), "; its work stopped" if e.get("stopped") else "",
                                                "; declined what it asked for" if e.get("declined") else "")
    elif kind == "alarm":
        text = "ALARM: %s%s" % (e.get("text"), " -> " + "; ".join(e["actions"]) if e.get("actions") else "")
    elif kind == "alarm_cleared":
        text = "cleared: %s" % e.get("text")
    elif kind == "watch":
        text = "%s watch %s: %s" % (e.get("change"), e.get("watch"), e.get("rule"))
    elif kind == "review":
        text = "second opinion on %s: %s" % (e.get("plan"), e.get("verdict"))
    elif kind == "card":
        text = "card %s saved (%s)" % (e.get("card"), e.get("status"))
    elif kind == "report":
        text = "report %s (%s run%s)" % (e.get("name"), e.get("runs"), "" if e.get("runs") == 1 else "s")
    elif kind == "chain":
        text = "measurement chain of %s %s" % (e.get("device") or "the setup", "removed" if e.get("removed")
                                               else "recorded")
    elif kind == "lab_memory":
        from ..labmemory import who
        tied = "; " + ", ".join(e["instruments"]) if e.get("instruments") else ""
        return "%s %s %s a note in the lab's memory (%s%s): %s" % (
            t, who(e.get("by"), e.get("account")), e.get("change"), e.get("kind") or "?", tied, e.get("text") or "")
    elif kind == "lab_changed":
        text = "now controlling %s (%s)" % (e.get("lab"), ", ".join(e.get("instruments") or []))
    elif kind == "instrument_switched":
        text = "%s switched %s%s" % (e.get("instrument"), "on" if e.get("on") else "off",
                                     "" if e.get("on") else ": Kelvin sends it nothing")
    elif kind in ("signed_in", "signed_out"):
        text = kind.replace("_", " ")
    elif kind == "computer_task":
        text = "operating the computer: %s" % e.get("task")
    elif kind == "computer":
        text = "screen: %s" % e.get("action")
    elif kind == "computer_task_end":
        text = "done operating the computer (%s steps)" % e.get("steps")
    elif kind == "approval_mode":
        text = "approvals: " + ("auto mode on (low-risk changes run without asking)" if e.get("mode") == "auto"
                                else "ask before every drive increase")
    elif kind == "network_search":
        text = "looked for instruments on the network%s: %s" % (
            " (%s)" % ", ".join(e["networks"]) if e.get("networks") else "",
            "found " + ", ".join(e["found"]) if e.get("found") else
            "%d address(es) answered" % len(e["answered"]) if e.get("answered") else "nothing answered")
    else:
        text = str(kind)
    by = e.get("by")
    return "%s %s%s" % (t, by + ": " if by and kind != "command" else "", text)


def trace_summary(spec: ParamSpec, values: List[float]) -> str:
    if not values:
        return "empty trace"
    lo = min(range(len(values)), key=values.__getitem__)
    hi = max(range(len(values)), key=values.__getitem__)
    u = " " + spec.unit if spec.unit else ""
    return "%d points; min %s%s at #%d; max %s%s at #%d" % (len(values), num(values[lo]), u, lo, num(values[hi]), u, hi)


def _runs_a_program(address: str) -> bool:
    """A lib:// address that names a Python (python=), a folder of files anywhere (path=), or neither, runs a
    program a person chose: only a person writes one (Lab setup); Kelvin uses it once it is in the lab. One that
    names an environment Kelvin installed with a person's approval (env=, envs.py), or the folder a maker's installer
    put its library in (path= in Program Files, /Applications or /opt), Kelvin may try and add itself."""
    import urllib.parse
    if not address.strip().lower().startswith("lib://"):
        return False
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(address.strip()).query))
    if q.get("python"):
        return True
    if q.get("path"):
        return not _installed_by_a_maker(q["path"])
    return not q.get("env")


def writable_folder(path: Any) -> Path:
    """A folder a person chose for Kelvin's files (the data folder, a chat's folder): its full path, made if it is
    missing, checked that Kelvin can write there."""
    import os
    text = str(path or "").strip()
    new = Path(text).expanduser()
    if not text or not new.is_absolute():
        raise ValueError("give the folder's full path, such as D:\\Data\\Kelvin or /Users/me/Data/Kelvin")
    new = Path(os.path.abspath(new))
    try:
        new.mkdir(parents=True, exist_ok=True)
        probe = new / ".kelvin-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise ValueError("Kelvin cannot write to %s: %s" % (new, e.strerror or e))
    return new


def _installed_by_a_maker(folder: str) -> bool:
    """A folder a maker's installer wrote: in Program Files (only an administrator writes there), /Applications or
    /opt."""
    import os
    roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    roots += ["/Applications", "/opt", "/Library/Frameworks"]
    try:
        path = Path(folder).resolve()
    except (OSError, ValueError):
        return False
    for r in roots:
        if r and Path(r).is_dir():
            root = Path(r).resolve()
            if path == root or root in path.parents:
                return path != root
    return False


class ClientCall:
    """A tool call an MCP client made, while Kelvin works on it (agent/server.py). The client may cancel it or go away;
    then Kelvin does nothing more for it (LabRuntime.cancel_call)."""
    CANCELLED = "the client, which cancelled the call"   # who declined what it asked for

    def __init__(self, tool: str):
        self.tool = tool
        self.thread: Optional[int] = None   # the worker thread doing it, once it has started
        self.waiting = False                # waiting for the lab, which another call or a run has
        self.cancelled = False


# whose a piece of work is: the project and chat it is filed under, and the marks that keep it unshared (an
# incognito chat, a chat that keeps unshared work's marks, the rules it ran under, a confidential or unpublished
# project)
WHOSE = ("project", "chat", "incognito", "unshared", "rules", "confidential", "unpublished")


class Viewer:
    """Who reads the lab's kept runs and records through Kelvin's tools, and so what of them it may see. Unshared
    work (a run of an unpublished, confidential or model-limited project, or one an incognito chat, or a chat that
    keeps such marks, started) is seen only from inside it: by the chat that started it, and by its project's chats,
    when their model is one its rules allow (allows). Everyone sees the rest. An agent over MCP is no chat, so it
    sees shared work only; so does a person's report or record made outside every chat."""

    def __init__(self, chat: Optional[str] = None, project: Optional[str] = None,
                 allows: Optional[Callable[[Dict[str, Any]], bool]] = None, whose: Optional[Dict[str, Any]] = None):
        self.chat = chat          # the chat asking (a second opinion or a Compare lane asks as its chat)
        self.project = project    # that chat's project
        self.allows = allows      # {rules: [rule sets], confidential} -> may its model see that work; None: never
        self.whose = whose        # the asking chat's own marks (WHOSE), kept with what it adds to the lab's records


# the viewer of this thread's work (LabRuntime.viewing), passed on to the threads and tasks it starts with their
# context; None: Kelvin itself and the person at the Kelvin computer, who see everything
_VIEWER: "contextvars.ContextVar[Optional[Viewer]]" = contextvars.ContextVar("kelvin_viewer", default=None)


def unshared_runs(n: int) -> str:
    return "%d run%s of unshared work (another project's or chat's)" % (n, "" if n == 1 else "s")


def not_shown(n: int) -> str:
    return "%s %s not shown here" % (unshared_runs(n), "is" if n == 1 else "are")


class _ReadingGate:
    """The safety gate, for checking a queue item beside the work that changes the lab (another chat adds it while
    a scan runs): what the check reads of an instrument (a source's fixed range) is read as any reading is, never
    from an instrument that work uses. A range it can't read now is left unknown, as the check does when an
    instrument doesn't answer: the queue's start checks it again, and the gate checks each change as it comes."""

    def __init__(self, rt: "LabRuntime"):
        self._rt = rt

    def __getattr__(self, name: str) -> Any:
        return getattr(self._rt.gate, name)

    def fixed_range(self, inst: Any, spec: ParamSpec) -> Optional[float]:
        with self._rt._reading(inst.name) as why:
            if why is not None:
                raise RuntimeError(why)
            return self._rt.gate.fixed_range(inst, spec)


class LabRuntime:
    def __init__(self, lab: Lab, approver: Approver, data_dir: Any, envelope: Optional[Envelope] = None):
        self.lab = lab
        self.approver = approver
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        frames.FOLDER = self.data_dir / "frames"   # camera frames read outside a run
        pulses.use_folder(self.data_dir / "sequences")   # the lab's written pulse sequences
        self.events_path = self.data_dir / "events.jsonl"
        self.user: Optional[str] = None             # the person signed in to the portal (accounts.py), if any
        self.shared_log: Optional[Path] = None      # the lab's instrument log, everyone's events; set by the app
        self.operating: Optional[str] = None        # Claude Code bridge: the task Claude is doing on the screen now
        self.gate = SafetyGate(lab, approver, self._new_log(lab.clock), envelope, operator="agent")
        self.engine = RunEngine(self.gate, self._store())
        self.engine.progress = self._on_progress
        self.lock = LabLock()   # one change at a time; each instrument's own lock for a reading (lablock.py)
        # the latest value of every reading Kelvin has seen: instrument -> parameter -> (value, when, how it came)
        self._readings: Dict[str, Dict[str, Tuple[Any, float, str]]] = {}
        self._readings_lock = threading.Lock()
        self._status_held: Dict[str, str] = {}   # the instruments the last look at the lab didn't read, and why
        self._attribution: Dict[str, Any] = {}   # see attribution
        self._attributed = threading.local()     # a thread's own (attributed)
        self._client = threading.local()   # the MCP client's call this thread works for (ClientCall), if any
        self._viewers: Dict[str, Viewer] = {}   # the portal chats' tool calls going on, by their tokens (viewer_of)
        self._viewers_lock = threading.Lock()
        self._holder: Optional["ClientCall"] = None   # the client's call that has the lab now, if one has
        self._script_cards: Optional[Tuple[Any, Any]] = None   # (the lab's cards folder as it was, the registry)
        self.busy: Optional[str] = None
        self._status: Dict[str, Dict[str, Any]] = {}
        self._status_at = 0.0
        self.live: Optional[Dict[str, Any]] = None   # the run in progress (or the last one), for live displays
        self._catalog: Optional["Catalog"] = None    # every run in the data folder (catalog.py); see .catalog
        self._tidy_lock = threading.Lock()
        self._live_lock = threading.Lock()
        self.lab_info: Dict[str, Any] = {"kind": "file" if lab.instruments else "none",   # open_home_lab's, in the app
                                         "path": None, "error": None, "has_file": False}
        self.notifier = Notifier()
        self.literature = Literature()
        self.cards_dir: Optional[Path] = None   # the user's own cards (~/Kelvin/cards); set by the app
        self.home: Optional[Path] = None        # ~/Kelvin, where each instrument has its folder; set by the app
        self.collection: Any = None             # the instrument collection (collection.py) while it is switched on
        # the lab's scripts folder a person named (Set up), the only one run_script runs from, and the Python a person
        # set for those scripts ("" = the best one found); the app keeps both in Settings, for everyone on this computer
        self.scripts_folder: Callable[[], Optional[str]] = lambda: None
        self.scripts_python: Callable[[], str] = lambda: ""
        self.setup_status: Dict[str, Any] = {"state": "idle"}   # setting up connected instruments (autosetup.py)
        self.data_problem: Optional[str] = None   # why the chosen data folder is not in use, if it is not
        # the data folder a lab keeps its data in when nobody chose one (labfolders.py), given (lab, info); None
        # leaves the folder as it is. The app sets it where no person or account has a folder of their own
        self.lab_folder: Optional[Callable[[Lab, Dict[str, Any]], Optional[Path]]] = None
        self.scan_network = True                    # scans also look on the local network (a Settings switch)
        self.last_survey: Optional[Dict[str, Any]] = None   # what the last scan looked through, and why (survey.py)
        self.last_found: List[str] = []                     # the addresses it found: where Kelvin may try a card
        self.found_info: Dict[str, Dict[str, Any]] = {}     # what it learnt of each, by VISA resource: identity, card
        # the app's model writing a card from the maker's manual (autocard.make_card); None outside the Kelvin app
        self.card_writer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
        self.manuals_found: Dict[str, Dict[str, Any]] = {}  # by VISA resource: the manual found while writing a card
        self.fetch_url: Optional[Callable[[str], bytes]] = None   # read_web_page and save_manual (profiles.http_get)
        # the app's part when Kelvin changes the lab itself ("added" an instrument, "switched" one on): keep it in
        # Settings and give a new instrument its folder, settings and manual
        self.lab_changed: Callable[[str], None] = lambda what: None
        self.switched_off_for: Optional[Callable[[str], Iterable[str]]] = None   # who a person switched off, per lab
        self.queue = ScanQueue(self.data_dir / "queue.json")   # scans lined up to run one after another
        self.auto_report = "both"   # after each run (in its folder) and each queue (in reports/): both, pdf, pptx, off
        self.mounted: Dict[str, Any] = self._load_mounted()   # the sample in the setup now (project, device, since)
        # a person's hands on the sample (who, since): every drive held at safe; the app keeps it across restarts
        self.hands: Optional[Dict[str, Any]] = None
        self.owner: Any = None   # the lab owner's PIN when one is set (accounts.OwnerPin); set by the app
        # Kelvin's own folder, where the lab's memory is kept for everyone here (labmemory.py); set by the app and the
        # MCP server. None: the data folder (a script, a test)
        self.lab_memory_dir: Optional[Path] = None
        self._lab_told: Dict[Any, set] = {}   # the lab's notes each chat (agent session) saw at an instrument's use
        self.protection_missing: List[str] = []   # the mounted device's protection names what this lab lacks
        # the mounted device's caps that are not in the instruments yet: written when Kelvin is next idle
        self.caps_waiting = False
        self._apply_protection(recheck=True)
        self.running_meta: Dict[str, Any] = {}      # the running plan's project and marks (_attribute), while it runs
        self.live_meta: Dict[str, Any] = {}         # and the marks of the run the live view shows, after it ends too
        # the watches unshared work added, with whose work it is: their notes are that work's own
        self.unshared_watches: Dict[str, Dict[str, Any]] = {}
        # what Kelvin knows about a project (name, devices, notes, references, summary, rules, marks); the portal
        # sets this
        self.project_info: Callable[[Optional[str]], Dict[str, Any]] = default_project_info
        self.project_ids: Callable[[], List[Optional[str]]] = lambda: [None]   # None is the General workspace
        self.project_chat_runs: Callable[[Optional[str]], List[str]] = lambda pid: []   # runs its chats started
        self.report_kinds = {"scan", "device", "project"}   # which reports Kelvin keeps up to date by itself
        self.report_delay = 20.0     # seconds after a run before its device and master reports are rewritten
        self._dirty_runs: set = set()
        self._report_timer: Optional[threading.Timer] = None
        self._report_lock = threading.Lock()
        self._queue_thread: Optional[threading.Thread] = None
        self.interrupted: List[str] = []   # the runs a crash or power cut ended, found when Kelvin started (tidy_runs)
        self.monitor = self._new_monitor()
        self.alarms_before = self._restore_alarms()   # the alarms still on when Kelvin last stopped
        self.restored: Dict[str, List[str]] = {"back": [], "dropped": []}
        self._restore_watches()   # the person's and the agent's watches outlast a restart
        # what the last stop left behind (drives away from safe, runs and a queue it cut short...), checked once the
        # instruments are connected (_incident_check) and shown until a person dismisses it
        self.incident: Optional[Dict[str, Any]] = None
        self._incident_due = True
        self._acted = False   # Kelvin drove something since it started: what is on now may be its own doing
        self.idle_off_s: Optional[float] = IDLE_OFF_S
        self.idle_off_all = False   # other outputs left idle go to safe too (Settings); else people are told
        self.last_active = time.monotonic()   # the last command, run or queue item: outputs left on idle go to safe
        self._watching: Optional[threading.Thread] = None
        self._watching_stop = threading.Event()
        subscribe = getattr(approver, "subscribe", None)
        if subscribe:  # a person is needed: tell them, wherever they are
            subscribe(lambda r: None if r.get("auto") else self.tell(
                "approval", "Approval needed: " + r["title"], (r.get("items") or [""])[0], bool(r.get("unshared"))))
        if hasattr(approver, "origin"):   # a card of unshared work goes to phones through the hub without its names
            approver.origin = self._unshared_request
        if hasattr(approver, "where"):   # each card says where it acts: SIMULATOR or REAL HARDWARE
            approver.where = self.where
        if hasattr(approver, "notes"):   # and when an instrument it names is out of calibration (profiles.py)
            approver.notes = self.card_notes
        if hasattr(approver, "lessons"):   # and the lab's lessons on the instruments it names (labmemory.py)
            approver.lessons = self.lab_lessons_for
        if hasattr(approver, "escalate"):   # nobody answers at the computer: people away from it are asked
            approver.escalate = self._escalate
        self.notifier.alive = lambda: self.monitor.watching()   # the heartbeat: Kelvin is there, watching the lab
        vetoes = getattr(approver, "vetoes", None)
        if vetoes is not None:   # a cancelled call asks nobody: no one is left to act on a yes
            vetoes.append(lambda: ClientCall.CANCELLED if getattr(getattr(self._client, "call", None), "cancelled",
                                                                  False) else None)
        with self.lock:
            self._refresh()

    @property
    def attribution(self) -> Dict[str, Any]:
        """What the work of this thread is filed under (its runs' project, folder, incognito): the chat whose call
        changes the lab (the portal sets it for that call), unless this thread has its own (attributed), as a queue
        item another chat adds meanwhile has, and the queue's scans."""
        mine = getattr(self._attributed, "value", None)
        return self._attribution if mine is None else mine

    @attribution.setter
    def attribution(self, value: Dict[str, Any]) -> None:
        self._attribution = value

    @contextlib.contextmanager
    def attributed(self, attribution: Dict[str, Any]) -> Iterator[None]:
        """This thread's work is filed under this ({}: no chat's), never under the chat whose call changes the lab
        meanwhile."""
        old = getattr(self._attributed, "value", None)
        self._attributed.value = dict(attribution)
        try:
            yield
        finally:
            self._attributed.value = old

    # ------------------------------------------------------------------ who may see what: unshared work (Viewer)

    @contextlib.contextmanager
    def viewing(self, viewer: Optional[Viewer]) -> Iterator[None]:
        """The runs and records read in this thread, and in the threads and tasks it starts with its context, are
        those this viewer may see (None: everything, as for Kelvin itself and the person at the computer)."""
        token = _VIEWER.set(viewer)
        try:
            yield
        finally:
            _VIEWER.reset(token)

    @property
    def viewer(self) -> Optional[Viewer]:
        return _VIEWER.get()

    def register_viewer(self, viewer: Viewer) -> str:
        """A token for a portal chat's tool call, which names its viewer in the call's meta (agent/server.py):
        only this process knows it, so no other client can claim to be that chat."""
        import secrets
        token = secrets.token_hex(16)
        with self._viewers_lock:
            self._viewers[token] = viewer
        return token

    def forget_viewer(self, token: str) -> None:
        with self._viewers_lock:
            self._viewers.pop(token, None)

    def viewer_of(self, token: Any) -> Viewer:
        """The viewer a token names; any other caller is an agent over MCP: no chat, so it sees shared work only."""
        with self._viewers_lock:
            found = self._viewers.get(str(token or ""))
        return found if found is not None else Viewer()

    def _info(self, pid: Optional[str], cache: Optional[Dict[Any, Any]] = None) -> Dict[str, Any]:
        """project_info, kept in cache for one look through many runs."""
        if cache is None:
            return self.project_info(pid)
        if pid not in cache:
            cache[pid] = self.project_info(pid)
        return cache[pid]

    def unshared_work(self, marks: Any, cache: Optional[Dict[Any, Any]] = None) -> bool:
        """Do these marks (a run's meta, a queue item's, a record's whose) belong to unshared work: an incognito
        chat's, a chat's that keeps unshared work's marks or rules, or a project's that isn't shared (unpublished,
        confidential or model-limited)?"""
        if not isinstance(marks, dict):
            return False
        if any(marks.get(k) for k in ("incognito", "unshared", "unpublished", "confidential", "rules")):
            return True
        pid = marks.get("project")
        return bool(pid) and bool(self._info(pid, cache).get("unshared"))

    def may_see(self, marks: Any, cache: Optional[Dict[Any, Any]] = None) -> bool:
        """May the viewer of this thread (viewing) see the work these marks belong to? Unshared work only from inside:
        the chat that started it, or a chat of its project, with a model its rules allow."""
        viewer = _VIEWER.get()
        if viewer is None or not self.unshared_work(marks, cache):
            return True
        pid = marks.get("project")
        inside = (bool(marks.get("chat")) and marks.get("chat") == viewer.chat) or \
            (bool(pid) and pid == viewer.project and not marks.get("incognito"))
        if not inside or viewer.allows is None:
            return False
        info = self._info(pid, cache) if pid else {}
        return bool(viewer.allows({"rules": [marks.get("rules"), info.get("rules")],
                                   "confidential": bool(marks.get("confidential") or info.get("confidential"))}))

    def _whose_now(self) -> Optional[Dict[str, Any]]:
        """The marks of the unshared work this thread does (its own attribution, as a queue item's; else the chat
        whose tool call it is), which what it writes in the lab's record keeps, so that other chats and phones
        through the Kelvin hub never see it; None for shared work. An attribution may say itself whether it is
        unshared work (private), as the lab's record asks at every event."""
        mine = getattr(getattr(self, "_attributed", None), "value", None)
        if mine is None:
            viewer = _VIEWER.get()
            mine = viewer.whose if viewer is not None else None
        if not mine:
            return None
        private = mine.get("private")
        if not (self.unshared_work(mine) if private is None else private):
            return None
        return {k: mine[k] for k in WHOSE if mine.get(k)}

    def _unshared_request(self, request: Any) -> bool:
        """Is this approval card about unshared work: asked by its chat or its queue item, or the queue's card while
        an item waiting in it is unshared work? Phones through the Kelvin hub then get it without its names."""
        if self._whose_now() is not None or getattr(request, "unshared", False):
            return True
        return str(getattr(request, "title", "")).startswith("Run the queue") and any(
            self.unshared_work(i.whose()) for i in list(self.queue.items) if i.status == "waiting")

    def tell(self, kind: str, title: str, text: str = "", unshared: bool = False) -> Any:
        """A notice for people (notify.py). unshared: it is about unshared work, so phones through the Kelvin hub get
        it without its names."""
        notice = self.notifier(kind, title, text)
        if unshared and isinstance(notice, dict):
            notice["unshared"] = True
        return notice

    def _run_marks(self, d: Path) -> Dict[str, Any]:
        """Whose a run is: its meta (the running plan's, while it runs)."""
        live = self.live or {}
        if live.get("run_id") == d.name and live.get("status") == "running" and self.running_meta:
            return self.running_meta
        try:
            return rundata.meta(d)
        except (KeyError, OSError, ValueError):
            return {}

    def _hidden_runs(self, cache: Optional[Dict[Any, Any]] = None) -> Dict[str, Tuple[str, str, str]]:
        """The runs the viewer of this thread may not see (none without one): run id -> (plan, started, finished)."""
        if _VIEWER.get() is None:
            return {}
        out: Dict[str, Tuple[str, str, str]] = {}
        try:
            for run_id, plan, started, finished, marks in self._fresh_catalog().marks():
                if not self.may_see(marks, cache):
                    out[run_id] = (plan or "", started or "", finished or "9999")
        except Exception:   # the catalog can't be read now: the runs' own folders still refuse (_run_dir)
            pass
        live = self.live or {}
        if live.get("run_id") and self.live_meta and not self.may_see(self.live_meta, cache):
            try:
                started = datetime.strptime(str(live["run_id"])[:15], "%Y%m%d-%H%M%S").isoformat()
            except ValueError:
                started = ""
            out.setdefault(str(live["run_id"]), (str(live.get("plan") or ""), started, "9999"))
        return out

    def seen_events(self, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The lab record's events that the viewer of this thread may see (all of them, without one): not what
        unshared work it may not see wrote (whose), nor its runs' starts and ends, their approvals and their plans'
        commands while they ran."""
        events = list(events)
        if _VIEWER.get() is None:
            return events
        cache: Dict[Any, Any] = {}
        hidden = self._hidden_runs(cache)
        spans = []
        for plan, t0, t1 in hidden.values():
            try:
                early = (datetime.fromisoformat(t0) - timedelta(minutes=15)).isoformat()
            except ValueError:
                early = t0
            spans.append((plan, early, t0, t1))

        def theirs(e: Dict[str, Any]) -> bool:
            kind, wall = e.get("type"), str(e.get("wall", ""))
            for plan, early, t0, t1 in spans:
                if kind in ("command", "trip") and t0 <= wall <= t1 and (
                        kind == "trip" or str(e.get("by") or "") == "plan:" + plan):
                    return True
                if kind in ("ask", "answer", "preflight") and early <= wall <= t1 and (
                        kind == "preflight" or "Run plan %s:" % plan in str(e.get("request") or "")):
                    return True
            return False
        return [e for e in events if not (e.get("whose") and not self.may_see(e["whose"], cache))
                and e.get("run") not in hidden and not (spans and theirs(e))]

    def _on_progress(self, event: Dict[str, Any]) -> None:
        """Keep a light copy of the running plan's data (scalars only, latest trace) for live displays."""
        self.last_active = time.monotonic()
        with self._live_lock:
            if event["event"] == "start":
                self.live = {"run_id": event["run_id"], "plan": event["plan"], "total": event["total"], "done": 0,
                             "eta_s": None, "status": "running", "phase": "setup", "axes": event["axes"],
                             "measures": event["measures"], "rows": [], "trace": None}
                self.live_meta = dict(self.running_meta)   # whose it is, kept after it ends
            elif self.live is None or self.live["run_id"] != event["run_id"]:
                return
            elif event["event"] == "point":
                row = event["row"]
                axes = {a["key"] for a in self.live["axes"]}
                keep = {"i", "t"} | axes | {m["key"] for m in self.live["measures"] if m["kind"] == "scalar"}
                self._keep_point({k: v for k, v in row.items() if k in keep and k not in ("i", "t")}, axes)
                self.live["rows"].append({k: None if isinstance(v, float) and not math.isfinite(v) else v
                                          for k, v in row.items() if k in keep})   # JSON has no NaN
                for m in self.live["measures"]:
                    if m["kind"] == "trace" and isinstance(row.get(m["key"]), list):
                        ys = row[m["key"]]
                        xs, ys = plot_points(trace_x(row, m["key"], len(ys)), ys, 400)
                        self.live["trace"] = {"key": m["key"], "unit": m["unit"], "x_unit": m.get("x_unit", ""),
                                              "x": xs, "y": ys}
                self.live["done"], self.live["eta_s"], self.live["phase"] = event["done"], event["eta_s"], "measuring"
            elif event["event"] == "phase":  # after the last point: final values, or the stop/abort steps
                self.live["phase"], self.live["eta_s"] = event["phase"], event.get("eta_s")
            elif event["event"] == "end":
                self.live["status"], self.live["eta_s"], self.live["phase"] = event["status"], 0, "end"

    def live_view(self, since: int = 0) -> Optional[Dict[str, Any]]:
        """The live run with the rows recorded since `since` (thread-safe copy)."""
        with self._live_lock:
            if self.live is None:
                return None
            view = {k: v for k, v in self.live.items() if k != "rows"}
            view["rows"] = list(self.live["rows"][since:])
            view["since"] = since
            return view

    # ------------------------------------------------------------------ plumbing

    def _refresh(self, wait: Optional[float] = None) -> None:
        """Look at every instrument, for lab_status and the Lab panel: each read now, with its own lock and never the
        lab's, except one that the work changing the lab uses (a run's) or that another reading has had for longer
        than `wait` (READ_WAIT_S): that one shows the latest values Kelvin has, and why (_status_held)."""
        try:
            status: Dict[str, Dict[str, Any]] = {}
            held: Dict[str, str] = {}
            for name, inst in list(self.lab.instruments.items()):
                if not inst.enabled:
                    continue
                with self._reading(name, wait) as why:
                    if why is None:
                        status.update(self.lab.snapshot([name]))
                        self._keep(name, status.get(name) or {}, "read")
                if why is not None:
                    held[name] = why
                    status[name] = {p: v for p, (v, _, _) in self._kept(name).items()}
            self._status, self._status_held, self._status_at = status, held, time.time()
        except Exception:
            pass

    @contextlib.contextmanager
    def _reading(self, name: str, wait: Optional[float] = None) -> Iterator[Optional[str]]:
        """Read this instrument now, beside the work that changes the lab: yields None to go ahead (holding the
        instrument's own lock), or why not, in words: that work uses it (a command in between could upset what it
        does there: a lock-in's acquisition, a sweep the instrument takes), or another reading has waited `wait`
        seconds (READ_WAIT_S) for it to answer."""
        wait = READ_WAIT_S if wait is None else wait
        with self.lock.reading(name, wait) as why:
            if why is None:
                yield None
            elif why == HELD:
                yield "it is in use: %s" % self.held_by()
            elif wait >= 1:
                yield "another reading of it has waited %s for its answer (is it on and connected?)" % _dur(wait)
            else:
                yield "another reading of it is under way"

    def _keep(self, name: str, values: Dict[str, Any], how: str) -> None:
        """These values of an instrument, as its latest (a reading that failed is none: the one before stays)."""
        now = time.time()
        with self._readings_lock:
            kept = self._readings.setdefault(name, {})
            for param, v in values.items():
                if v is None or isinstance(v, (int, float, bool)) or (isinstance(v, str) and not v.startswith("ERROR")):
                    kept[param] = (v, now, how)

    def _keep_point(self, row: Dict[str, Any], axes: Iterable[str]) -> None:
        """A run's point: what it measured and what it set, as the latest values of those instruments, so a reading
        from elsewhere has them while the run uses the instruments."""
        now = time.time()
        with self._readings_lock:
            for key, v in row.items():
                name, _, param = key.partition(".")
                inst = self.lab.instruments.get(name)
                if inst is not None and param in inst.card.params and isinstance(v, (int, float, str, bool)):
                    self._readings.setdefault(name, {})[param] = (
                        v, now, "set by the run" if key in axes else "measured by the run")

    def _kept(self, name: str) -> Dict[str, Tuple[Any, float, str]]:
        with self._readings_lock:
            return dict(self._readings.get(name) or {})

    def _not_read(self, instrument: str, parameter: str, why: str) -> str:
        """A reading Kelvin doesn't take now (why): the latest value it has, and how old it is."""
        key = "%s.%s" % (instrument, parameter)
        got = self._kept(instrument).get(parameter)
        if got is None:
            return "%s: not read now (%s); Kelvin has no earlier value of it. Ask again when that is done." % (key, why)
        value, when, how = got
        spec = self._spec(key)
        return "%s = %s, %s %s ago (not read now: %s)" % (key, spec.show(value) if spec else value, how,
                                                          _dur(time.time() - when), why)

    def reading_now(self, key: str, fresh_s: float = 120.0) -> Tuple[Any, Optional[str]]:
        """One reading for a check in the background (a chat waiting for a condition): (its value, None), read now;
        while the work changing the lab uses the instrument, the run's own latest measurement of it if it has one from
        the last fresh_s seconds, never a command; else (None, why not)."""
        instr, _, param = key.partition(".")
        with self._reading(instr, 1.0) as why:
            if why is None:
                return self.lab.read(instr, param), None
        got = self._kept(instr).get(param)
        if got is not None and got[2] == "measured by the run" and time.time() - got[1] <= fresh_s:
            return got[0], None
        return None, why

    def held_by(self) -> str:
        """What has the lab now, in words: the running plan and how far it is, an approval card waiting for a person,
        the queue, or what Kelvin is doing."""
        live = self.live or {}
        restricted = _VIEWER.get() is not None   # a chat's tool: unshared work it may not see goes unnamed
        if live.get("status") == "running":
            eta = live.get("eta_s")
            seen = not restricted or self.may_see(self.live_meta)
            return "%s (point %d of %d%s)%s" % (
                "the running plan %s" % live.get("plan") if seen else "a running scan of unshared work",
                live.get("done") or 0, live.get("total") or 0, ", ~%s left" % _dur(eta) if eta else "",
                " in the queue" if self.queue.state in ("running", "pausing") else "")
        pending = getattr(self.approver, "pending_list", lambda: [])()
        if pending:
            return "an approval card waiting for a person (%s)" % (
                "unshared work" if restricted and pending[0].get("unshared") else pending[0].get("title"))
        if self.queue.state in ("running", "pausing"):
            running = next((i for i in self.queue.items if i.status == "running"), None)
            hide = restricted and running is not None and not self.may_see(running.whose())
            return "the queue (%s)" % ("an item of unshared work" if hide else self.busy or "between its items")
        return self.busy or "an earlier request"

    def busy_for(self, seconds: float) -> bool:
        """Whether the lab stays held for longer than this: by a run expected to go on longer, the queue, or an
        approval card waiting for a person (a request that needs the lab says so at once rather than wait)."""
        live = self.live or {}
        return ((live.get("status") == "running" and (live.get("eta_s") or 0) > seconds)
                or self.queue.state in ("running", "pausing")
                or bool(getattr(self.approver, "pending_list", lambda: [])()))

    def _busy_answer(self, waited: Optional[float] = None) -> str:
        """Why a change can't have the lab now. Never a guess at a cause: what holds it is named (a run, the queue, an
        approval card), or anything else with how long this waited for it."""
        what = self.held_by()
        if waited is not None:
            what += " (this waited %s for it; a stop ends it as soon as an instrument it waits on answers)" % _dur(waited)
        return ("not done: Kelvin is busy with %s. It changes the lab one thing at a time, from any chat or client; "
                "readings (the latest values of the instruments that work uses), lookups and the queue work "
                "meanwhile. Ask again when it is done; the person's Stop ends it." % what)

    def _involved(self, names: Iterable[str]) -> List[str]:
        """These instruments, and those their changes read as they go: their guards' readings, and the other side of
        their interlocks."""
        out = set(names)
        for name in list(out):
            inst = self.lab.instruments.get(name)
            if inst is not None:
                for guards in list(inst.config.guards.values()) + list(inst.device_guards.values()):
                    out.update(g.instr for g in guards)
        for lock in self.lab.config.interlocks:
            pair = {lock.drive.split(".")[0], lock.require.key.split(".")[0]}
            if pair & out:
                out |= pair
        return sorted(out)

    def _uses(self, text: str, names: Iterable[str] = ()) -> List[str]:
        """The instruments a plan or a queue step uses: those it names (requires), and every instrument of this lab
        its text mentions as instrument.parameter (steps, sweeps, conditions), so none it touches is left out."""
        out = set(names)
        for name in self.lab.instruments:
            if re.search(r"(?<![\w.])%s\s*\." % re.escape(name), text):
                out.add(name)
        return sorted(out)

    def _do(self, what: str, fn: Callable[[], str], stoppable: bool = False,
            uses: Optional[Iterable[str]] = None) -> str:
        """fn as the one change to the lab now (the lab's lock: the gate's approvals are the lab's, so changes never
        overlap). uses: the instruments it works on (and _involved), which readings then leave alone while the others
        stay readable; None: all of them. stoppable: a call that changes the lab (a drive, a run, the queue) or waits
        on it; one still waiting for the lab when a person presses Stop, or when the client that made it cancels it,
        is not run (a Stop ends what is lined up behind the stopped work too, not only what was showing)."""
        gate, stops = self.gate, self.gate.stop_generation
        call = getattr(self._client, "call", None)   # the client's call this thread works for (agent/server.py)
        if call is not None:
            call.waiting = True
        try:
            if not self.lock.acquire(timeout=2.0):
                if self.busy_for(BUSY_WAIT_S):   # a run, the queue or an approval card may take hours: answer now
                    return self._busy_answer()
                t0 = time.monotonic() - 2.0
                if not self.lock.acquire(timeout=BUSY_WAIT_S):   # never a chat stuck behind an instrument that hangs
                    return self._busy_answer(time.monotonic() - t0)
        finally:
            if call is not None:
                call.waiting = False
        try:
            if call is not None and call.cancelled:
                return "not run: the client cancelled this call while it waited for the lab"
            if stoppable and gate.stop_generation != stops:
                return ("not run: a person pressed Stop while this waited for the lab. Ask them before trying it "
                        "again.")
            self._acted = self._acted or stoppable
            held, self._holder = self._holder, call or self._holder
            self.busy, self.last_active = what, time.monotonic()
            try:
                if uses is None:
                    return fn()
                with self.lock.only(self._involved(uses)):
                    return fn()
            finally:
                self.busy, self._holder, self.last_active = None, held, time.monotonic()
                self._caps_if_waiting()
                self._refresh()
        finally:
            self.lock.release()

    def _spec(self, key: str) -> Optional[ParamSpec]:
        try:
            return self.lab.resolve(key)[1]
        except KeyError:
            return None

    def _plan(self, text: str) -> Plan:
        """Plans arrive as YAML/JSON text only, never as a file path."""
        try:
            data = load_yaml(text)
        except yaml.YAMLError as e:
            raise PlanError(["not valid YAML: %s" % str(e).splitlines()[0]])
        if not isinstance(data, dict):
            raise PlanError(["a plan must be a YAML mapping"])
        return load_plan(data)

    # ------------------------------------------------------------------ agent operations

    def lab_status(self) -> str:
        """Every instrument's values, read now beside whatever else Kelvin does: one that the work changing the lab
        uses (a run) isn't asked, and shows its latest values with how old each is."""
        def run() -> str:
            self.last_active = time.monotonic()
            self._refresh()
            sim = all(i.simulated for i in self.lab.instruments.values())
            lines = ["%s%s" % (self.lab.config.name, " (simulated)" if sim else "")]
            owner, scope = self.lock.owner()
            if owner is not None and owner != threading.get_ident():
                lines.append("busy now: %s, using %s; Kelvin doesn't ask those meanwhile (their latest values below)"
                             % (self.held_by(), "every instrument" if scope is None else ", ".join(sorted(scope))
                                or "no instrument"))
            if self.gate.auto:
                lines.append("approvals: auto mode (a person turned it on): low-risk drive changes and runs go ahead "
                             "without asking; the rest still wait for a human")
            lines += ["ALARM: %s%s" % (a.text, " (%s)" % "; ".join(a.actions) if a.actions else "")
                      for a in self.monitor.active()]
            lines += ["AFTER KELVIN RESTARTED (the person sees this in the portal until they dismiss it): " + line
                      for line in self.incident_lines()]
            if self.hands:
                lines.append("HANDS ON THE SAMPLE: %s is handling it (since %s); every drive stays at safe until a "
                             "person unlocks it in the Lab panel" % (self.hands.get("by"), self.hands.get("since")))
            lines += self._protection_lines()
            unsure = [n for n, i in self.lab.instruments.items() if i.enabled and self.gate.unsure(i)]
            if unsure:
                lines.append("not reviewed by a person yet (Kelvin added it or drafted its card): %s. Every output "
                             "change on %s asks the person, auto mode included" % (", ".join(unsure),
                                                                                  "it" if len(unsure) == 1 else "them"))
            lines += self._status_lines()
            lines += self._programs_here()
            lines.append("data folder: %s (runs/<run id>/run.h5, catalog.sqlite, reports/, events.jsonl)"
                         % self.data_dir)
            lines.append("* = drive away from its safe value")
            return "\n".join(lines)
        return run()

    def _programs_here(self) -> List[str]:
        """Makers' programs on this computer that Kelvin drives as instruments, not yet in the lab."""
        from .. import lightfield
        try:
            folder = lightfield.root() if sys.platform == "win32" else None
        except Exception:
            folder = None
        if folder is None or any(ic.address.startswith("lightfield://")
                                 for ic in self.lab.config.instruments.values()):
            return []
        return ["LightField is installed here (%s): Kelvin drives its camera and spectrograph as an instrument, not "
                "through its window: add_instrument with address lightfield:// (or lightfield://<experiment>) and "
                "card princeton/lightfield" % folder]

    def _protection_lines(self) -> List[str]:
        m = self.mounted or {}
        prot = m.get("protection") or {}
        if not prot:
            return []
        name = m.get("device_name") if self.may_see({"project": m.get("project")}) else \
            "(a sample of another project's unshared work)"   # its limits hold for every chat; its name is its own
        out = ["mounted device %s is protected (a person set this; it can't be changed or passed): %s" % (
            name, "; ".join(protection.summary(prot, self.lab)))]
        if self.protection_missing:
            out.append("this lab lacks %s, named in %s's protection" % (", ".join(self.protection_missing), name))
        return out

    def _status_lines(self) -> List[str]:
        lines = []
        for name in self.lab.switched_off:
            lines.append("%s: switched off in the Lab panel; Kelvin sends it nothing (ask_to_switch_on asks the person)"
                         % name)
        for name, why in self.lab.unavailable.items():
            if why.startswith("set aside"):   # its lab.yaml entry has a problem: the person's to fix, not a reconnect
                lines.append("%s: %s; tell the person" % (name, why))
                continue
            lines.append("%s: not connected (%s); Kelvin sends it nothing until it connects: fix that "
                         "(install_for_instrument for a missing package), then reconnect_instrument" % (name, why))
        for name, values in self._status.items():
            if name not in self.lab.instruments or not self.lab.instruments[name].enabled:
                continue
            card = self.lab.instruments[name].card
            held = self._status_held.get(name)   # not read now: its latest values, each with how old it is
            kept = self._kept(name) if held else {}
            parts = []
            for pname, v in values.items():
                spec = card.params[pname]
                if isinstance(v, str) and v.startswith("ERROR"):
                    parts.append("%s ERROR" % pname)
                    continue
                parts.append("%s %s%s%s" % (pname, spec.show(v), "*" if spec.is_drive and spec.distance(v) > 0 else "",
                                            " (%s %s ago)" % (kept[pname][2], _dur(time.time() - kept[pname][1]))
                                            if pname in kept else ""))
            if held:
                lines.append("%s (not read now: %s; its latest values): %s" % (name, held, ", ".join(parts) or
                                                                               "none yet"))
            else:
                lines.append("%s: %s" % (name, ", ".join(parts)))
        return lines

    def describe(self, instrument: str) -> str:
        """Its card and limits: nothing is sent to it, so it never waits for the lab."""
        return self.gate.describe(instrument)

    def get(self, instrument: str, parameter: str) -> str:
        """get_value: read now, beside whatever else Kelvin does; while the work changing the lab uses the instrument
        (a run), its latest value instead, as a reading then could upset what the work does there."""
        self.last_active = time.monotonic()
        inst = self.lab.instruments.get(instrument)
        if self._spec("%s.%s" % (instrument, parameter)) is None or inst is None or not inst.enabled:
            return self.gate.get(instrument, parameter).message   # refused, saying why; nothing is sent
        with self._reading(instrument) as why:
            if why is None:
                r = self.gate.get(instrument, parameter)
                if r.ok:
                    self._keep(instrument, {parameter: r.value}, "read")
                if r.ok and isinstance(r.value, list):
                    return "%s.%s: %s" % (instrument, parameter, trace_summary(self._spec(r.target), r.value))
                return r.message
        return self._not_read(instrument, parameter, why)

    def set(self, instrument: str, parameter: str, value: Any) -> str:
        return self._do("setting %s.%s" % (instrument, parameter),
                        lambda: self.gate.set(instrument, parameter, value).message, stoppable=True,
                        uses=[instrument])

    def raw(self, instrument: str, command: str) -> str:
        return self._do("raw command to " + instrument, lambda: self.gate.raw(instrument, command).message,
                        stoppable=True, uses=[instrument])

    def undo(self) -> str:
        return self._do("undo", lambda: self.gate.undo().message, stoppable=True)

    # ------------------------------------------------------------------ where the data go (people only)

    DATA = ("runs", "reports", "events.jsonl")   # what a data folder holds; chats and settings stay with Kelvin

    @property
    def runs(self) -> RunFolders:
        """Every folder this person's runs are in: the data folder's runs/ and the chats' own folders."""
        return RunFolders(self.data_dir)

    def _store(self) -> RunStore:
        return RunStore(self.data_dir / "runs", RunFolders(self.data_dir))

    def data_summary(self) -> Dict[str, Any]:
        runs = self.data_dir / "runs"
        return {"path": str(self.data_dir), "runs": sum(1 for d in runs.iterdir() if d.is_dir()) if runs.exists() else 0,
                "reports": sum(1 for f in (self.data_dir / "reports").glob("*") if f.is_file())
                if (self.data_dir / "reports").exists() else 0,
                "elsewhere": sum(1 for d in self.runs.iterdir() if d.parent != runs)}   # in chats' own folders

    def set_data_dir(self, path: Any, move: bool = False, by: str = "human") -> Dict[str, Any]:
        """Keep runs, the lab record (events.jsonl) and reports in another folder: a person's choice, from Settings.
        Refused while Kelvin is busy. With move, what is saved now goes along, but nothing there is overwritten:
        a run or report whose name is taken, or a record the new folder already has, stays where it was."""
        import shutil
        if not Path(str(path or "").strip()).expanduser().is_absolute():
            raise ValueError("give the folder's full path, such as D:\\Data\\Kelvin or /Users/me/Data/Kelvin")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Kelvin is busy (%s); try again when it is idle" % (self.busy or "working"))
        try:
            if self.live and self.live.get("status") == "running":
                raise RuntimeError("a plan is running; change the data folder after it finishes")
            new = writable_folder(path)
            old = self.data_dir
            if new.resolve() == old.resolve():
                return dict(self.data_summary(), moved=0, kept=[])
            moved, kept = 0, []
            if move:
                for sub in ("runs", "reports", "sequences"):   # the lab's written pulse sequences go along too
                    src = old / sub
                    if not src.is_dir():
                        continue
                    (new / sub).mkdir(exist_ok=True)
                    for item in sorted(src.iterdir()):
                        target = new / sub / item.name
                        if target.exists():
                            kept.append("%s/%s" % (sub, item.name))
                            continue
                        shutil.move(str(item), str(target))
                        moved += 1
                if (old / "events.jsonl").exists():
                    if (new / "events.jsonl").exists():
                        kept.append("events.jsonl")
                    else:
                        shutil.move(str(old / "events.jsonl"), str(new / "events.jsonl"))
                        moved += 1
                for f in RunFolders(old).chat_folders():   # the chats' folders keep their runs; Kelvin still finds them
                    RunFolders(new).remember(f)
                (old / RunFolders.INDEX).unlink(missing_ok=True)
            if not move or kept:
                self.gate.log.write("data_folder", by=by, moved_to=str(new),
                                    note="what stays here was not moved" if move else "earlier data stay here")
            self.data_dir = new
            frames.FOLDER = new / "frames"
            pulses.use_folder(new / "sequences")
            self.queue.move_to(new / "queue.json")
            self._save_watches()
            self._save_alarms()
            self.events_path = new / "events.jsonl"
            self.gate.log = self._new_log()
            self.engine.store = self._store()
            self.gate.log.write("data_folder", by=by, previous=str(old), moved=moved, kept=len(kept))
            self.tidy_later()
            return dict(self.data_summary(), moved=moved, kept=kept, previous=str(old))
        finally:
            self.lock.release()

    def _new_log(self, clock: Any = None) -> EventLog:
        return EventLog(self.events_path, clock or self.lab.clock, shared=self.shared_log, user=self.user,
                        whose=self._whose_now)

    def use_user(self, user: Optional[str], data_dir: Any, problem: Optional[str] = None) -> None:
        """Work for another person (None: nobody is signed in). Their data folder holds their runs, reports, record
        (events.jsonl), queue and mounted sample; the instruments, cards and the lab's instrument log are shared.
        Refused while a plan or the queue runs. Open approvals are declined and the approval mode goes back to
        asking: one person's yes is not another's."""
        new = Path(data_dir)
        new.mkdir(parents=True, exist_ok=True)
        held = self.lock.acquire(timeout=1.0)
        if not held and not self.setting_up:
            raise RuntimeError("Kelvin is busy (%s); try again when it is idle" % (self.busy or "working"))
        # held by the instrument setup (connecting, scanning, reading settings: minutes, at start): nobody's work is
        # running, so the person comes in at once; their sample's caps are written to the instruments once Kelvin is
        # idle, as after any switch (recheck below)
        try:
            if self.live and self.live.get("status") == "running":
                raise RuntimeError("A plan is running; wait for it to finish or stop it first.")
            if self.queue.state in ("running", "pausing"):
                raise RuntimeError("The queue is running; wait for it to finish or stop it first.")
            self.flush_reports()   # the last person's device and master reports, while their projects are known
            decline_all = getattr(self.approver, "decline_all", None)
            if decline_all:
                decline_all("signed out")
            if self.gate.auto:
                self.gate.auto = False
                self.gate.log.write("approval_mode", by="sign-in", mode="ask")
            if self.user:
                self.gate.log.write("signed_out", user=self.user)
            self.user = user
            self.data_dir = new
            frames.FOLDER = new / "frames"
            pulses.use_folder(new / "sequences")
            self.events_path = new / "events.jsonl"
            self.gate.log = self._new_log()
            self.engine.store = self._store()
            self.queue = ScanQueue(new / "queue.json")
            self._restore_watches()   # theirs come back, and the ones watched now are kept for them too
            self._save_watches()
            self._save_alarms()
            self.mounted = self._load_mounted()
            self._apply_protection(recheck=True)
            self.attribution = {}
            self.data_problem = problem
            with self._live_lock:
                self.live = None
            if user:
                self.gate.log.write("signed_in", user=user, data=str(new))
            self.tidy_later()
        finally:
            if held:
                self.lock.release()

    @property
    def setting_up(self) -> bool:
        """The instrument setup is at work (autosetup.py: connecting, scanning, writing cards, reading settings)."""
        return self.setup_status.get("state") in SETUP_STEPS

    # ------------------------------------------------------------------ a person's terminal (people only)

    def terminal(self, instrument: str, op: str, arg: str = "", value: Any = None,
                 by: str = "human (terminal)") -> Dict[str, Any]:
        """A command a person typed in the portal's terminal. It goes through the same gate as everything Kelvin
        does (limits, interlocks, blocked commands, approvals) and is logged as theirs. op: raw (a command for the
        instrument, e.g. SCPI), get, set, describe or manual (search its manual). raw and set are refused, not queued,
        while Kelvin is busy, so a typed command never lands in the middle of a run; get reads as get_value does,
        beside it (the latest value of an instrument the run uses)."""
        if instrument not in self.lab.instruments:
            return {"ok": False, "action": "refused", "message": "no instrument %r (have: %s)"
                    % (instrument, ", ".join(self.lab.instruments))}
        if op == "manual":
            return {"ok": True, "action": "read", "message": self.search_manual(instrument, arg)}
        if op == "describe":
            return {"ok": True, "action": "read", "message": self.gate.describe(instrument)}
        if op not in ("raw", "get", "set"):
            return {"ok": False, "action": "refused", "message": "unknown terminal operation %r" % op}
        if not arg.strip():
            return {"ok": False, "action": "refused", "message": "nothing to send"}
        if op == "get":   # a reading, as get_value: beside whatever else Kelvin does, never inside a run's work
            self.last_active = time.monotonic()
            with self._reading(instrument) as why:
                if why is None:
                    with self.gate.acting_as(by):
                        r = self.gate.get(instrument, arg.strip())
            if why is not None:
                if self._spec("%s.%s" % (instrument, arg.strip())) is None:
                    return {"ok": False, "action": "refused", "message": "%s has no parameter %r" % (instrument, arg)}
                return {"ok": False, "action": "busy", "message": self._not_read(instrument, arg.strip(), why)}
            shown = r.value if r.value is None or isinstance(r.value, (str, int, float, bool)) else str(r.value)[:200]
            return {"ok": r.ok, "action": r.action, "message": r.message, "value": shown}
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "action": "busy",
                    "message": "Kelvin is busy (%s); wait for it, or stop it first" % self.held_by()}
        try:
            self.busy, self._acted, self.last_active = "terminal: %s %s" % (instrument, arg), True, time.monotonic()
            with self.gate.acting_as(by), self.lock.only(self._involved([instrument])):
                if op == "raw":
                    r = self.gate.raw(instrument, arg.strip())
                else:
                    r = self.gate.set(instrument, arg.strip(), value)
            shown = r.value if r.value is None or isinstance(r.value, (str, int, float, bool)) else str(r.value)[:200]
            return {"ok": r.ok, "action": r.action, "message": r.message, "value": shown}
        finally:
            self.busy = None
            self._refresh()
            self.lock.release()

    def where(self, request: ApprovalRequest) -> Optional[str]:
        """Where a request acts, for its card: SIMULATOR when every instrument it names is a simulated one, else
        REAL HARDWARE (a request asked on simulated instruments once looked just like one on real ones). None for a
        request that names no instrument (installing a package, saving a file)."""
        text = " ".join([request.title] + [str(i) for i in request.items])
        named = [inst for name, inst in self.lab.instruments.items()
                 if re.search(r"(?<![\w.])%s(?![\w])" % re.escape(name), text)]
        if not named:
            return None
        return "SIMULATOR" if all(inst.simulated for inst in named) else "REAL HARDWARE"

    def card_notes(self, request: ApprovalRequest) -> List[str]:
        """What a card adds about the real instruments it names: a calibration out of date, or on record for another
        serial number (profiles.card_lines). Never asks an instrument anything."""
        if self.home is None:
            return []
        from .. import profiles
        text = " ".join([request.title] + [str(i) for i in request.items])
        named = [name for name, inst in self.lab.instruments.items() if not inst.simulated
                 and re.search(r"(?<![\w.])%s(?![\w])" % re.escape(name), text)]
        return profiles.card_lines(self.home, named)

    def answer(self, request_id: str, approve: bool, by: str = "human", channel: str = "portal",
               device: Optional[str] = None, approver: Any = None, pin: Any = None) -> bool:
        """A person's answer to a request, kept with who they are: their account (the person signed in), where they
        answered (portal, phone, approval page) and the device, so the record and the reports never take a person's
        yes for the agent's. False when the request is no longer open. With roles, a trainee's yes needs an operator
        or the owner to confirm it (approver: {name, password}, or the owner's pin); PermissionError says why not.
        A no never does."""
        who: Dict[str, Any] = {"account": self.user, "channel": channel, "device": device}
        title = getattr(self.approver, "title_of", lambda _: None)(request_id)
        # a request no longer open asks nothing (no password tried for nothing); one that can't tell, asks
        if approve and (title is not None or not hasattr(self.approver, "title_of")):
            vouched = self.vouched(approver, pin)
            if vouched:
                who["vouched"] = vouched
        try:
            ok = self.approver.answer(request_id, approve, by, who)
        except TypeError:   # an approver that takes no account of who
            ok = self.approver.answer(request_id, approve)
        if ok:
            self.gate.log.write("answer", by=by, request=title, answer="approved" if approve else "declined",
                                account=self.user, channel=channel, device=device, vouched=who.get("vouched"))
        return ok

    def stop(self, by: str = "human", note: str = "") -> int:
        """A person's stop: decline open requests and stop the current ramp or plan. Drives stay where they are
        (a stopped plan still runs its own on_abort steps). The queue stops after the scan it ends, and a call still
        waiting for the lab is not run (_do). Takes no lock, so it never waits. Returns how many requests were
        declined."""
        decline_all = getattr(self.approver, "decline_all", None)
        declined = decline_all("stop button") if decline_all else 0
        with self.queue.lock:   # a stop also stops the queue: nothing else starts after the stopped scan
            if self.queue.state in ("running", "pausing"):
                self.queue.state, self.queue.reason = "pausing", "stopped by %s" % by
                self.queue.save()
        self.gate.request_abort()
        self.gate.log.write("stop", by=by, account=self.user, declined=declined, note=note or None)
        return declined

    def stop_work(self, reason: str = "", by: str = "agent") -> str:
        """stop_run: the Stop button's stop (stop), when the person asks the agent for it in a chat, whichever chat or
        client started the work; it never waits for the lab. Says what it stopped."""
        live = self.live or {}
        run = live.get("status") == "running"
        what = self.held_by() if run or self.lock.owner()[0] is not None else None
        queue = self.queue.state in ("running", "pausing")
        declined = self.stop(by, note=" ".join(str(reason or "").split())[:200])
        if what is None and not queue and not declined:
            return "nothing to stop: no run, ramp, wait or queue was going"
        lines = []
        if run:
            lines.append("stopped %s: it ends now, and the drives it touched go to safe (or hold where the lab config "
                         "says hold)" % what)
        elif what:
            lines.append("stopped %s: it ends now (a ramp stops where it is)" % what)
        if queue:
            lines.append("the queue stops after the scan it is on; queue_start starts it again")
        if declined:
            lines.append("%d open approval request%s declined" % (declined, "" if declined == 1 else "s"))
        return "; ".join(lines)

    @contextlib.contextmanager
    def client_call(self, call: ClientCall) -> Iterator[None]:
        """This thread works for an MCP client's call (agent/server.py), so a cancel reaches what it does."""
        call.thread = threading.get_ident()
        self._client.call = call
        try:
            yield
        finally:
            self._client.call = None

    def cancel_call(self, call: ClientCall) -> None:
        """The client cancelled a call, or went away: Kelvin does nothing more for it. What it asked a person for is
        declined (and anything it would ask), the ramp, run or wait it has the lab for stops as at a person's Stop,
        and a call still waiting for the lab is never run. Another call's work goes on."""
        call.cancelled = True
        decline = getattr(self.approver, "decline_from", None)
        declined = decline(call.thread, ClientCall.CANCELLED) if decline and call.thread is not None else 0
        working = self._holder is call
        if working:
            self.gate.request_abort()
        if declined or working:
            self.gate.log.write("call_cancelled", by="MCP client", tool=call.tool, declined=declined,
                                stopped=working)

    def health(self, instruments: Optional[List[str]] = None) -> str:
        """health_check, instrument by instrument, beside whatever else Kelvin does: one that the work changing the
        lab uses (a run, which checked it as it started) is not checked now, and says so."""
        self.last_active = time.monotonic()
        names = list(instruments) if instruments else [n for n, i in self.lab.instruments.items() if i.enabled]
        lines: List[str] = []
        for name in names:
            inst = self.lab.instruments.get(name)
            if inst is None or not inst.enabled:   # says why, sending nothing
                lines += [str(r) for r in self.engine.preflight([name], fresh=True)]
                continue
            with self._reading(name) as why:
                if why is None:   # asked for: always checks again
                    lines += [str(r) for r in self.engine.preflight([name], fresh=True)]
            if why is not None:
                lines.append("%s not checked now: %s" % (name, why))
        if not instruments:
            lines += ["%s switched off (not checked)" % n for n in self.lab.switched_off]
            lines += ["%s not connected: %s" % kv for kv in self.lab.unavailable.items()]
        if self.home is not None:   # a calibration out of date or due soon: said, never a failure (profiles.py)
            from .. import profiles
            lines += profiles.calibration_notes(self.home, [n for n in names if n in self.lab.instruments
                                                            and not self.lab.instruments[n].simulated])
        return "\n".join(lines)

    def wait(self, seconds: float) -> str:
        seconds = max(0.0, min(float(seconds), MAX_WAIT_S))
        def run() -> str:
            stops, t0 = self.lab.stops.count, self.lab.clock.now()
            if not self.lab.stops.sleep(self.lab.clock, seconds, stops):   # a person's stop ends the wait
                return "stopped after %s of %s: a person pressed stop" % (_dur(self.lab.clock.now() - t0),
                                                                          _dur(seconds))
            return "waited %s" % _dur(seconds)
        return self._do("waiting", run, stoppable=True, uses=[])   # a wait uses no instrument: all can be read

    def validate(self, plan_text: str) -> str:
        def run() -> str:
            try:
                plan = self._plan(plan_text)
            except PlanError as e:
                return "invalid plan: %s" % e
            errors = self.engine.validate(plan)
            if errors:
                return "invalid plan: " + "; ".join(errors[:5])
            from ..forecast import Forecast
            forecast = Forecast(self.engine).plan(plan)   # as the run's own card: its setup actions, a draft's settings
            items, _, problems = self.gate.plan_approval(self.engine.drive_values(plan), forecast=forecast)
            asked = forecast.asked + forecast.action_lines()
            lines = ["ok: %s, %s%s, instruments %s" % (plan.name, self.engine.shape(plan), forecast.duration(),
                                                      ", ".join(plan.requires))]
            lines += ["will be refused: " + p for p in problems]
            lines += ["needs approval: " + i for i in asked] or ["no approval needed (measurement only or inside the envelope)"]
            if asked:
                lines += ["also on its card: " + i for i in items[len(forecast.asked):] + forecast.closing()]
            return "\n".join(lines)
        return self._do("validating plan", run)

    def simulate(self, plan_text: str) -> str:
        def run() -> str:
            try:
                plan = self._plan(plan_text)
            except PlanError as e:
                return "invalid plan: %s" % e
            report = self.engine.simulate(plan)
            lines = [report.message]
            lines += ["would ask: " + w.replace("\n", " | ") for w in report.would_ask]
            lines += self._preview(plan, report.rows)
            return "\n".join(lines)
        return self._do("dry run", run)

    def run(self, plan_text: str, dry_run: bool = False) -> str:
        """run_plan. dry_run: only a rehearsal on the simulated lab, never followed by the real run."""
        if dry_run:
            return REHEARSAL % "run_plan" + "\n" + self.simulate(plan_text)
        return self._do("running plan", lambda: self._run_once(plan_text)[0], stoppable=True)

    def _run_once(self, plan_text: str, dry_run: bool = False) -> Tuple[str, Optional[RunReport]]:
        """One plan, run (the caller holds the lock): run_plan's answer, and the run's report (None: not run)."""
        try:
            plan = self._plan(plan_text)
        except PlanError as e:
            return "invalid plan: %s" % e, None
        meta = self._attribute(plan)
        apart = meta.pop("_apart", "")
        try:
            with self._device_protection(meta) as held, \
                    self.lock.only(self._involved(self._uses(plan_text, plan.requires))):   # the rest stay readable
                report = self.engine.run(plan, dry_run=dry_run, meta=meta, folder=self.attribution.get("folder"))
        except RunFolderError as e:   # nowhere to keep it: said in words, not a bare tool error (l09, l10)
            return "not run: %s" % e, None
        with self.lock.only([]):   # writing its report uses no instrument: all of them can be read meanwhile
            made = self._auto_report(report.run_id)
        if report.run_id:
            summary = (report.analysis or {}).get("assessment") or report.message
            self.tell("run", ("Run finished: " if report.status == "done" else "Run stopped: ") + plan.name,
                      summary[:300], self.unshared_work(meta))
        lines = list(held)
        if apart and report.run_id:
            lines.append("filed under %s without the mounted sample: %s" % (meta.get("project_name") or "General",
                                                                           apart))
        if report.dry_run is not None and report.status in ("done", "aborted"):
            lines.append(report.dry_run.message)
        lines.append(report.message)
        if report.analysis:
            lines += self._analysis_lines(report.analysis)
        else:
            lines += self._preview(plan, report.rows)
        lines += ["warning: " + w for w in report.warnings]
        if report.path:
            lines.append("saved in %s: run.h5 (every point, trace, setting and fit)%s" % (report.path, (
                " and %s (made automatically; use make_report only for another selection or wording)"
                % " and ".join(made)) if made else ""))
        if report.run_id and report.rows:
            lines.append("rows: get_run_data(run_id=%r)" % report.run_id)
        return "\n".join(lines), report

    # ------------------------------------------------------------------ recipes (recipes.py)

    def run_recipe(self, text: str, values: Optional[Dict[str, Any]] = None) -> str:
        """A recipe's stages in order, each run through the gate like run_plan, then checked on its data; values
        kept for later stages; retried with changed values when the recipe says so. Ends at a stage that does not
        pass, or a run that stops."""
        from .. import recipes
        try:
            recipe = recipes.parse(text)
            vals = recipes.start_values(recipe, values)
        except recipes.RecipeError as e:
            return "recipe not run: %s" % e

        def run() -> str:
            lines = ["recipe %s (%d stages), values: %s" % (recipe.name, len(recipe.stages), recipes.shown(vals))]
            for k, stage in enumerate(recipe.stages, 1):
                for attempt in range(stage.retries + 1):
                    try:
                        if attempt:
                            vals.update({name: recipes.calc(e, vals) for name, e in stage.retry.items()})
                        plan_text = recipes.render(stage.plan, vals)
                    except recipes.RecipeError as e:
                        lines.append("stopped at stage %s: %s" % (stage.name, e))
                        return "\n".join(lines)
                    self.busy = "recipe %s, stage %d of %d: %s%s" % (recipe.name, k, len(recipe.stages), stage.name,
                                                                     " (try %d)" % (attempt + 1) if attempt else "")
                    answer, report = self._run_once(plan_text)
                    if report is None or not report.run_id or report.status != "done":
                        why = answer.splitlines()[0] if answer else "not run"
                        lines.append("stopped at stage %s: %s" % (stage.name, why))
                        return "\n".join(lines)
                    try:
                        passed = bool(self.value_of(report.run_id, recipes.render(stage.passes, vals))) \
                            if stage.passes else True
                        if passed:
                            vals.update({name: self.value_of(report.run_id, recipes.render(e, vals))
                                         for name, e in stage.keep.items()})
                    except (ValueError, KeyError) as e:
                        lines.append("stopped at stage %s: run %s could not be checked: %s" % (
                            stage.name, report.run_id, e.args[0] if e.args else e))
                        return "\n".join(lines)
                    if passed:
                        lines.append("stage %s: run %s passed%s%s" % (
                            stage.name, report.run_id, " (%s)" % stage.passes if stage.passes else "",
                            "; kept " + recipes.shown({n: vals[n] for n in stage.keep}) if stage.keep else ""))
                        break
                    lines.append("stage %s: run %s did not pass %s (try %d of %d)" % (
                        stage.name, report.run_id, stage.passes, attempt + 1, stage.retries + 1))
                else:
                    lines.append("stopped: stage %s did not pass after %d %s; values: %s" % (
                        stage.name, stage.retries + 1, "try" if not stage.retries else "tries", recipes.shown(vals)))
                    return "\n".join(lines)
            lines.append("done; values: %s" % recipes.shown(vals))
            return "\n".join(lines)
        return self._do("running recipe %s" % recipe.name, run, stoppable=True)

    def check_recipe(self, text: str) -> Tuple[Optional[Any], str]:
        """(the recipe, "") when it is well formed and every instrument parameter its plans use is in this lab; else
        (None, what is wrong)."""
        from .. import recipes
        try:
            recipe = recipes.parse(text)
        except recipes.RecipeError as e:
            return None, str(e)
        known = dict(recipe.values)
        for s in recipe.stages:
            try:
                plan = self._plan(recipes.render(s.plan, dict(known, **{k: 1.0 for k in s.keep})))
            except (PlanError, recipes.RecipeError) as e:
                return None, "stage %s: %s" % (s.name, e)
            keys = [st.key for st in plan.setup] + [a.key for a in plan.axes if a.kind == "instrument"] + \
                ["%s.%s" % m for m in plan.measure]
            for key in keys:
                if self._spec(key) is None:
                    return None, "stage %s: this lab has no %s" % (s.name, key)
            known.update({k: 1.0 for k in s.keep})
        return recipe, ""

    def value_of(self, run_id: str, expression: str) -> float:
        """One number from a run's columns (a recipe's checks and kept values). ValueError: not one number."""
        import numpy as np
        from .. import expr
        table = rundata.columns(self._run_dir(run_id))
        if not table.n:
            raise ValueError("run %s has no data" % run_id)
        value = np.asarray(expr.evaluate(expression, lambda key: table.number(key), np), dtype=float)
        if value.ndim:
            raise ValueError("%s is not one number (it gives %d values)" % (expression, value.size))
        return float(value)

    @staticmethod
    def _analysis_lines(a: Dict[str, Any]) -> List[str]:
        lines = ["assessment: " + a["assessment"], "next: " + a["next"]]
        for s in a["series"][:6]:
            label = s["y"] + (" (" + s["group"] + ")" if s.get("group") else "")
            if s["results"]:
                lines.append("%s [%s]: %s" % (label, s["kind"], "; ".join("%s %s" % (r["name"], r["value"]) for r in s["results"][:5])))
        return lines

    def analyze(self, run_id: str, column: Optional[str] = None, model: Optional[str] = None,
                accept: bool = False, by: str = "agent") -> str:
        """Re-analyse a recorded run, optionally one column and/or with a chosen fit model. accept: keep these fits
        as the run's (in run.h5), and its scan report shows them."""
        from ..analysis import MODELS, analyze_run
        from ..chain import derived_readings
        if model and model not in MODELS and model not in ("linear", "quadratic"):
            return "unknown model %r (choose: linear, quadratic, %s)" % (model, ", ".join(MODELS))
        try:
            rec = rundata.load(self._run_dir(run_id))
            rows, meta = [r for r in rec.rows if not r.get("stabilizing")], rec.meta   # as the run analysed them
            cols = self._columns(rec)
        except (KeyError, OSError, ValueError, PlanError) as e:
            return "cannot analyse %s: %s" % (run_id, e)
        if column and column not in [m["key"] for m in cols["measures"]]:
            return "unknown column %r (have: %s)" % (column, ", ".join(m["key"] for m in cols["measures"]))
        derive = derived_readings(meta.get("chain"), meta.get("setup"), rec.snapshots.get("start"), cols["measures"])
        a = analyze_run(rows, cols["axes"], cols["measures"], meta, force=model, only=column, derive=derive)
        lines = ["%s: %d rows" % (run_id, len(rows))] + self._analysis_lines(a)
        if accept:
            lines.append(self._accept_fit(run_id, rec.analysis or {}, a, model, by))
        return "\n".join(lines)

    def _accept_fit(self, run_id: str, kept: Dict[str, Any], new: Dict[str, Any], model: Optional[str],
                    by: str) -> str:
        """Keep a re-analysis's fits as the run's own: each of its series replaces the kept one for the same column
        (and group), marked accepted (by whom, for which person signed in, when, which model), in run.h5; then the
        run's scan report is written again, so it shows the fit that was accepted rather than the automatic one."""
        stamp = {"by": by, "account": self.user, "at": datetime.now().isoformat(timespec="seconds"),
                 "model": model or "chosen automatically"}
        fresh = [dict(s, accepted=stamp) for s in new.get("series") or [] if s.get("fit") or s.get("results")]
        if not fresh:
            return "nothing kept: this analysis fitted nothing to keep"
        done = {(s.get("y"), s.get("group")) for s in fresh}
        merged = dict(kept, series=[s for s in kept.get("series") or [] if (s.get("y"), s.get("group")) not in done]
                      + fresh)
        if not kept.get("series"):
            merged.update(assessment=new.get("assessment", ""), next=new.get("next", ""))
        d = self._run_dir(run_id)
        try:
            rundata.set_analysis(d, merged)
        except (OSError, ValueError) as e:
            return "not kept: %s" % e
        self.gate.log.write("fit_accepted", by=by, run=run_id, model=stamp["model"],
                            columns=sorted({str(s.get("y")) for s in fresh}))
        made = []
        if "scan" in self.report_kinds and self.auto_report != "off":
            try:
                made = self.write_scan_report(run_id)
            except Exception as e:   # the fit is kept whatever happens to the report
                return "kept as the run's fit; its report was not written again: %s" % e
        return "kept as the run's fit (in run.h5)%s" % (
            "; its scan report shows it now (%s)" % ", ".join(made) if made else "")

    def _preview(self, plan: Plan, rows: List[Dict[str, Any]]) -> List[str]:
        if not rows:
            return []
        axes = [a["key"] for a in self.engine.columns(plan)["axes"]]   # a ramp's is the reading it follows
        out = []
        for instr, param in plan.measure:
            key = "%s.%s" % (instr, param)
            spec = self._spec(key)
            vals = [(r[key], r) for r in rows if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
            if not vals:
                if rows and isinstance(rows[0].get(key), list):
                    out.append("%s: %d traces of %d points (get_trace)" % (key, len(rows), len(rows[0][key])))
                continue
            lo = min(vals, key=lambda x: x[0])
            hi = max(vals, key=lambda x: x[0])
            where = lambda r: ", ".join("%s=%s" % (a, num(r[a])) for a in axes if a in r) or "row %d" % r["i"]
            out.append("%s: min %s at %s; max %s at %s" % (key, spec.show(lo[0]), where(lo[1]), spec.show(hi[0]), where(hi[1])))
        return out

    # ------------------------------------------------------------------ data

    def _run_dir(self, run_id: str) -> Path:
        """A run's folder; KeyError for none, and for a run of unshared work the viewer of this thread may not see
        (may_see): every tool that reads a run by its id comes here."""
        d = self.runs.find(run_id)
        if d is None:
            raise KeyError("no run %r (see list_runs)" % run_id)
        if _VIEWER.get() is not None and not self.may_see(self._run_marks(d)):
            raise KeyError("run %s is unshared work (another project's or chat's): it isn't shown here" % run_id)
        return d.resolve()

    def _rows(self, run_id: str) -> List[Dict[str, Any]]:
        try:
            return rundata.load(self._run_dir(run_id)).rows
        except KeyError:
            return []

    def _columns(self, rec: rundata.Recorded) -> Dict[str, List[Dict[str, str]]]:
        """A run's axes and readings with units: as it recorded them, else from its plan and this lab."""
        cols = rec.columns or {}
        if cols.get("axes") or cols.get("measures"):
            if all(c.get("unit") is not None for c in cols.get("axes", []) + cols.get("measures", [])):
                return cols
        return self.engine.columns(self._plan(rec.plan_text))

    def runs_index(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Most recent runs first: run_id, plan, status, points, planned_points, reason, and unshared for a run of
        unshared work; only the runs the viewer of this thread may see."""
        return self._runs_seen(limit)[0]

    def _runs_seen(self, limit: int) -> Tuple[List[Dict[str, Any]], int]:
        """runs_index, and how many runs of unshared work it left out on the way."""
        dirs = sorted(self.runs.iterdir(), key=lambda d: d.name, reverse=True)
        out: List[Dict[str, Any]] = []
        hidden = 0
        cache: Dict[Any, Any] = {}
        live = self.live_view(10 ** 9)
        for d in dirs:
            if len(out) >= limit:
                break
            entry: Dict[str, Any] = {"run_id": d.name, "plan": d.name.split("-", 2)[-1], "status": "unfinished"}
            began = rundata.started_at(d.name)
            if began is not None:
                entry["started_at"] = began   # the name says this computer's clock; a phone may be elsewhere
            meta: Dict[str, Any] = {}
            if live and live["run_id"] == d.name and live["status"] == "running":
                entry.update(status="running", points=live["done"], planned_points=live["total"])
                meta = self.running_meta
            else:
                try:
                    meta = rundata.meta(d)
                    entry.update(status=meta.get("status") or "unfinished", points=meta.get("points"),
                                 planned_points=meta.get("planned_points"), reason=meta.get("reason"))
                except (KeyError, OSError, ValueError):
                    pass
            if not self.may_see(meta, cache):
                hidden += 1
                continue
            if self.unshared_work(meta, cache):   # the portal marks it (the Analysis list)
                entry["unshared"] = True
            out.append(entry)
        return out, hidden

    def list_runs(self, limit: int = 10) -> str:
        lines = []
        runs, hidden = self._runs_seen(limit)
        for r in runs:
            if r["status"] in ("unfinished", "running"):
                lines.append("%s: %s" % (r["run_id"], r["status"]))
            else:
                lines.append("%s: %s, %s/%s points%s" % (r["run_id"], r["status"], r["points"], r["planned_points"],
                                                         " (%s)" % r["reason"] if r.get("reason") else ""))
        if hidden:
            lines.append("(%s)" % not_shown(hidden))
        return "\n".join(lines) or "no runs yet"

    # ------------------------------------------------------------------ every run: kept as run.h5, in the catalog

    @property
    def catalog(self) -> "Catalog":
        """The catalog of the runs in the data folder in use (it follows a change of folder or person)."""
        from ..catalog import FILE, Catalog   # SQLite, only when a catalog is used
        path = self.data_dir / FILE
        if self._catalog is None or self._catalog.path != path:
            self._catalog = Catalog(path)
        return self._catalog

    def _running_id(self) -> Optional[str]:
        live = self.live_view(10 ** 9)
        return live["run_id"] if live and live["status"] == "running" else None

    def tidy_runs(self) -> Dict[str, int]:
        """Keep every run in the data folder as run.h5 and in the catalog: runs a crash or a power cut ended
        (status interrupted, with every point up to then) and runs from before run.h5 (their files move to legacy/
        in the run folder). Kelvin does this when it starts and when the data folder or the person changes, in the
        background. A run being recorded, by this Kelvin or another, is left alone."""
        with self._tidy_lock:
            root = self.runs
            done = {"interrupted": 0, "upgraded": 0, "failed": 0}
            for d in root.iterdir():
                try:
                    if (d / rundata.RECORD).exists():
                        if rundata.being_recorded(d) or (rundata.closed(d) and not rundata.have_h5()):
                            continue
                        crashed = not rundata.closed(d)
                        rundata.finish(d)
                        done["interrupted"] += crashed
                        if crashed:
                            self.interrupted.append(d.name)
                    elif rundata.have_h5() and (d / "data.jsonl").exists() and not (d / rundata.RUN_FILE).exists():
                        if rundata.upgrade(d, self._legacy_columns(d)):
                            done["upgraded"] += 1
                except Exception as e:   # that run stays as it is; the next start tries again
                    done["failed"] += 1
                    self.gate.log.write("run_not_kept", by="kelvin", run=d.name, error=str(e)[:300])
            running = self._running_id()
            self.catalog.sync(root, skip=[running] if running else ())
            if done["interrupted"] or done["upgraded"]:
                self.gate.log.write("runs_kept", by="kelvin", **done)
            return done

    def tidy_later(self) -> None:
        """tidy_runs in the background."""
        def tidy() -> None:
            try:
                self.tidy_runs()
            except Exception:
                pass
        threading.Thread(target=tidy, name="kelvin-tidy-runs", daemon=True).start()

    def _legacy_columns(self, folder: Path) -> Optional[Dict[str, Any]]:
        """An older run's axes and readings with units, from its plan and this lab (None if they don't fit)."""
        try:
            return self.engine.columns(self._plan((folder / "plan.yaml").read_text(encoding="utf-8")))
        except Exception:
            return None

    def _fresh_catalog(self) -> "Catalog":
        """The catalog, after a look at the run folders if the last was over 30 s ago (cheap: unchanged runs are
        not read again)."""
        cat = self.catalog
        cat.ready()
        if time.monotonic() - cat.synced > 30 or not cat.synced:
            running = self._running_id()
            cat.sync(self.runs, skip=[running] if running else ())
        return cat

    def find_runs(self, text: str = "", device: str = "", project: str = "", plan: str = "", status: str = "",
                  since: str = "", until: str = "", quantity: str = "", limit: int = 20) -> str:
        cat = self._fresh_catalog()
        hidden = self._hidden_runs()   # unshared work the viewer of this thread may not see
        rows = cat.find(text, device, project, plan, status, since, until, quantity, limit, skip=hidden)
        running = self._running_id()
        total = cat.count() - len(set(hidden) - {running})   # the running run isn't in the catalog yet
        lines = ["%d run%s, newest first (of %d in the data folder):" % (len(rows), "" if len(rows) == 1 else "s",
                                                                          total) if rows else
                 "no run matches (%d in the data folder)" % total]
        for r in rows:
            lines += self._run_lines(r)
        if running and running not in hidden:
            lines.append("running now: %s (in the catalog once it ends)" % running)
        if hidden:
            lines.append("(%s)" % not_shown(len(hidden)))
        return "\n".join(lines)

    @staticmethod
    def _run_lines(r: Dict[str, Any]) -> List[str]:
        card = r.get("card") or {}
        g = lambda v: "%g" % v if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)  # noqa: E731
        about = [(r.get("started") or "")[:16].replace("T", " ")]
        about += ["device " + r["device_name"]] if r.get("device_name") else []
        about += ["project " + r["project_name"]] if r.get("project_name") else []
        about += ["simulated"] if r.get("simulated") else []
        lines = ["%s: %s, %s, %s points (%s)" % (r["run_id"], r.get("plan"), r.get("status"),
                                                 card.get("points", r.get("points")), ", ".join(a for a in about if a))]
        swept = ["%s %s→%s %s" % (a["key"], g(a.get("from", "?")), g(a.get("to", "?")), a.get("unit", ""))
                 for a in card.get("swept") or []]
        measured = ["%s [%s%s]" % (m["key"], m.get("unit") or "-", ", trace" if m.get("kind") == "trace" else
                                   ", image" if m.get("kind") == "image" else "") for m in card.get("measured") or []]
        if swept or measured:
            lines.append("  swept %s; measured %s" % (", ".join(swept) or "nothing (a time series)",
                                                     ", ".join(measured) or "-"))
        held = card.get("held") or {}
        if held:
            lines.append("  held: " + ", ".join("%s %s" % kv for kv in list(held.items())[:6]))
        if r.get("assessment"):
            lines.append("  assessment: " + str(r["assessment"])[:240])
        if r.get("reason") and r.get("status") != "done":
            lines.append("  stopped: " + str(r["reason"])[:200])
        return lines

    def query_runs(self, sql: str) -> str:
        cat = self._fresh_catalog()
        hidden = self._hidden_runs()   # unshared work the viewer of this thread may not see: never in the answer
        try:
            names, rows, more = cat.query(sql, skip=hidden)
        except ValueError as e:   # catalog.QueryError
            return "query not run: %s" % e
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(names)
        for row in rows:
            w.writerow(["" if v is None else "%.6g" % v if isinstance(v, float) else v for v in row])
        text = out.getvalue()
        if not rows:
            text += "(no rows)"
        elif more:
            text += "(the first %d rows only: narrow the query or use LIMIT/OFFSET)" % len(rows)
        if hidden:
            text = text.rstrip("\n") + "\n(%s: the query read the catalog without them)" % not_shown(len(hidden))
        return text.rstrip("\n")

    def describe_run(self, run_id: str) -> str:
        """A run's card (what it swept and measured, with units and ranges, the conditions held, the fits, the
        assessment and issues), the plan as it ran and its files."""
        try:
            d = self._run_dir(run_id)
            card = rundata.card(d)
            plan_text = rundata.load(d, with_rows=False).plan_text
        except (KeyError, OSError, ValueError) as e:
            return "cannot read %s: %s" % (run_id, e.args[0] if e.args else e)
        lines = [yaml.safe_dump(card, sort_keys=False, allow_unicode=True, width=110).rstrip()]
        if plan_text.strip():
            lines += ["plan as run:", "  " + plan_text.strip().replace("\n", "\n  ")]
        lines.append("files: %s, in %s" % (", ".join(f["name"] for f in self.run_files(run_id)) or "none yet", d))
        lines.append("numbers: get_run_data (columns, where), compute (expressions), get_trace (one trace)")
        return "\n".join(lines)

    def import_data(self, paths: Any, name: str = "") -> str:
        """Files Kelvin didn't measure, as a run (imports.py): another program's tables (CSV, TSV, text, Excel),
        makers' files (BioLogic .mpt/.mpr, Gamry .dta, Arbin's exports, LightField .spe), NumPy arrays and TIFF
        images (camera frames); several with the same x values side by side in one run, to compare or subtract.
        The run goes with this chat's runs, gets the automatic fits, and Kelvin's run tools read it."""
        from .. import imports
        given = paths if isinstance(paths, (list, tuple)) else str(paths or "").split(";")
        files = [str(p).strip().strip('"') for p in given if str(p).strip()]
        if not files:
            return "name the files to import: their full paths (';' between several)"
        if len(files) > 20:
            return "at most 20 files at once"
        try:
            table = imports.merge([imports.read(p) for p in files])
        except imports.DataError as e:
            return "not imported: %s" % e
        except OSError as e:
            return "not imported: %s" % (e.strerror or e)
        return self._do("importing data", lambda: self._imported(table, files, name, self.gate.operator)["text"])

    def import_attached(self, path: Any, shown: str, attribution: Dict[str, Any], by: str = "human") -> Dict[str, Any]:
        """A data file a person attached in a chat, as a run filed under that chat (its folder and project): {text:
        what Kelvin says of it, run: its id, points, frames: whether they are camera frames}. It is made beside
        whatever works the lab, as an import touches no instrument: a file attached while a run holds the lab still
        becomes data. imports.DataError: not imported, saying why. shown: the file's name as the person attached it."""
        from .. import imports
        table = imports.read(path)
        with self.attributed(attribution):
            return self._imported(table, [shown], "", by)

    def _imported(self, table: Any, files: List[str], name: str, by: str) -> Dict[str, Any]:
        """An imported table kept as a run: its x (and a 2-D scan's slow axis), its columns, and camera frames as a
        run keeps a camera's. {text: what Kelvin says of it, run: its id, points, frames}."""
        from types import SimpleNamespace
        from ..analysis import analyze_run
        # a run's own i (its points' numbers) and t (its clock): an x named I (a current) or T (a temperature) would
        # take their place and come back as whole numbers, so it keeps its numbers under another name
        if table.x_key in ("i", "t"):
            table.x_key = "x_" + table.x_key
        if table.outer and table.outer[0] in ("i", "t"):
            table.outer = ("x_" + table.outer[0],) + tuple(table.outer[1:])
        plan_name = re.sub(r"[^\w.-]+", "_", (name or "").strip() or "imported_" + table.name)[:48]
        rec = self.engine.store.create(plan_name, {"plan": plan_name, "imported": files}, "import",
                                       self.attribution.get("folder"))
        whose = self._attribute(SimpleNamespace(device=None))
        whose.pop("_apart", None)
        rec.meta.update(whose)
        rec.meta.update(lab="imported", simulated=False, imported=files)
        axes = [{"key": table.x_key, "unit": table.x_unit, "kind": "imported"}]
        if table.outer:   # a 2-D scan: its slow axis first, as a plan's outer sweep
            axes.insert(0, {"key": table.outer[0], "unit": table.outer[1], "kind": "imported"})
        measures = [{"key": k, "unit": u, "kind": "scalar"} for k, (u, _) in table.columns.items()]
        framed = table.frames[0] if table.frames else None
        if table.frames:
            if rec.path is None:
                raise RunFolderError("camera frames need a data folder to be kept in")
            measures.insert(0, {"key": framed, "unit": table.frames[1], "kind": "image"})
        rec.set_columns({"axes": axes, "measures": measures})
        rows = []
        for i, x in enumerate(table.x):
            row: Dict[str, Any] = {"i": i, "t": 0.0, table.x_key: x}
            if table.outer:
                row[table.outer[0]] = table.outer[2][i]
            if table.frames:   # each frame a file in the run's folder, as a camera's are, packed into run.h5 at the end
                row[framed] = frames.keep(frames.save(table.frames[2][i], framed), rec.path, framed, i)
            row.update({k: values[i] for k, (_, values) in table.columns.items()})
            rec.add(row)
            rows.append(row)
        meta = dict(status="done", reason=None, points=len(rows), planned_points=len(rows), duration_s=0.0,
                    warnings=list(table.notes))
        analysis = None
        try:
            if table.fits:   # a maker's electrochemistry, a cycler's record or frames: not the sweeps they are for
                analysis = analyze_run(rows, axes, measures, meta)
                rec.set_analysis(analysis)
        except Exception:
            analysis = None   # the fits are a convenience; the numbers are kept
        rec.finish(**meta)
        self.gate.log.write("run_imported", by=by, run=rec.run_id, files=len(files))
        try:
            self.catalog.add(rec.path)
        except Exception:
            pass   # the catalog finds it at its next look
        names = ", ".join(Path(f).name for f in files)
        if framed:
            lines = ["imported %s as run %s: %d frame%s of a camera's kind (column %s)%s" % (
                names, rec.run_id, len(rows), "" if len(rows) == 1 else "s", framed,
                ", and each one's %s" % ", ".join(table.columns) if table.columns else "")]
        elif table.outer:
            lines = ["imported %s as run %s: %d points, a 2-D scan of %s (%s) over %s (%s), columns %s" % (
                names, rec.run_id, len(rows), table.x_key, table.x_unit or "no unit", table.outer[0],
                table.outer[1] or "no unit", ", ".join(table.columns))]
        else:
            lines = ["imported %s as run %s: %d points of %s (%s), columns %s" % (
                names, rec.run_id, len(rows), table.x_key, table.x_unit or "no unit", ", ".join(table.columns))]
        lines += ["saved in %s" % rec.path] + ["note: " + n for n in table.notes]
        if analysis:
            lines += self._analysis_lines(analysis)
        elif not table.fits:
            lines.append("fits: none made by themselves (Kelvin's automatic fits are made for its own sweeps); "
                         "fit_run fits the model you choose")
        if framed:
            lines.append("pictures: look_at with run_id %s shows its %s (column %s: the last frame; frame k, from 0: "
                         "that frame); run_python reads them all from run.h5 (/frames/%s: frames x rows x columns)%s"
                         % (rec.run_id, "frames' means and maxima" if table.columns else "frame", framed, framed,
                            "; numbers: get_run_data" if table.columns else ""))
        else:
            first = next((k for k, (unit, _) in table.columns.items() if unit), next(iter(table.columns)))
            lines.append("numbers: get_run_data, compute (e.g. %s / max(%s)), look_at; describe_run for its card"
                         % (first, first))
        return {"text": "\n".join(lines), "run": rec.run_id, "points": len(rows), "frames": bool(framed)}

    def stop_run(self, by: str = "human") -> bool:
        """Stop the plan that is running (its on_abort steps then run); a queue it is part of stops after it.
        False if none is running."""
        live = self.live_view(10 ** 9)
        if not live or live["status"] != "running":
            return False
        self.gate.request_abort()
        self.gate.log.write("stop", by=by, account=self.user, run=live["run_id"])
        return True

    # ------------------------------------------------------------------ the scan queue

    # ------------------------------------------------------------------ the sample in the setup

    def _load_mounted(self) -> Dict[str, Any]:
        try:
            data = json.loads((self.data_dir / "mounted.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("device_name") else {}
        except (OSError, ValueError):
            return {}

    def mount(self, project: Optional[str], device: Dict[str, Any], by: str = "human") -> Dict[str, Any]:
        """Record which sample is in the setup now. Every run from here on belongs to it (and to its project), and
        its protection holds (protection.py): its limits, its gates' leakage guards, and the caps written into the
        instruments ("caps" in the answer says what was written)."""
        info = self.project_info(project)
        self.mounted = {"project": project or None, "project_name": info.get("name") or "General",
                        "device": device.get("id"), "device_name": device.get("name"),
                        "since": datetime.now().isoformat(timespec="seconds"), "by": by,
                        "protection": device.get("protection") or {}}
        (self.data_dir / "mounted.json").write_text(json.dumps(self.mounted, indent=1), encoding="utf-8")
        self.gate.log.write("mount", by=by, device=self.mounted["device_name"], project=self.mounted["project_name"],
                            protection=protection.summary(self.mounted["protection"]) or None)
        self._apply_protection()
        caps = self._write_caps(by)
        return dict(self.mounted, caps=caps)

    def protect(self, device: Dict[str, Any], by: str = "human") -> List[str]:
        """A person changed a device's protection: if it is the mounted one, it holds from now on. Returns the caps
        written into the instruments."""
        if not self.mounted or self.mounted.get("device") != device.get("id"):
            return []
        self.mounted["protection"] = device.get("protection") or {}
        (self.data_dir / "mounted.json").write_text(json.dumps(self.mounted, indent=1), encoding="utf-8")
        self.gate.log.write("protection", by=by, device=self.mounted.get("device_name"),
                            protection=protection.summary(self.mounted["protection"]) or None)
        self._apply_protection()
        return self._write_caps(by)

    def unmount(self, by: str = "human") -> None:
        if self.mounted:
            self.gate.log.write("unmount", by=by, device=self.mounted.get("device_name"),
                                project=self.mounted.get("project_name"))
        self.mounted = {}
        self._apply_protection()
        try:
            (self.data_dir / "mounted.json").unlink()
        except OSError:
            pass

    def _apply_protection(self, recheck: bool = False) -> None:
        """Hold this lab's instruments to the mounted device's protection, and to a person's hands-on lock.
        recheck: other instruments (Kelvin started, another lab, another person's sample), so the caps are checked
        and written again when Kelvin is next idle, in case an instrument was reset meanwhile."""
        prot = (self.mounted or {}).get("protection") or {}
        self.protection_missing = protection.apply(self.lab, prot, (self.mounted or {}).get("device_name") or "")
        self.lab.hands_on = self.hands
        if recheck:
            self.caps_waiting = bool(prot)

    def _write_caps(self, by: str) -> List[str]:
        """Write the mounted device's caps into the instruments. What can't be written yet (a plan holds the
        instruments, a source isn't at 0, an instrument is switched off) waits: Kelvin writes it when it is next
        idle (_caps_if_waiting)."""
        prot = (self.mounted or {}).get("protection") or {}
        self.caps_waiting = False
        if not prot:
            return []
        if not self.lock.acquire(blocking=False):
            self.caps_waiting = True
            return ["the instruments' own caps are written when Kelvin is idle (now %s)" % (self.busy or "busy")]
        try:
            with self.gate.acting_as(by):
                lines, done = protection.write_caps(self.gate, self.lab, prot)
            self.caps_waiting = not done
            return lines + protection.outside(self.lab, prot)
        except Exception as e:
            self.caps_waiting = True
            return ["the instruments' own caps were not written: %s" % e]
        finally:
            self._refresh()
            self.lock.release()

    def _caps_if_waiting(self) -> None:
        if self.caps_waiting and self.mounted:
            self._write_caps("sample protection")

    @contextlib.contextmanager
    def _device_protection(self, meta: Dict[str, Any]) -> Iterator[List[str]]:
        """A plan that names another device (device: D5, on the mounted chip) is held to that device's protection
        too, for that run: the tighter of the two holds. Yields lines saying so (none when nothing changes)."""
        self.running_meta = meta   # whose the running plan is: phones through the hub leave unshared runs out
        m = self.mounted or {}
        dev: Dict[str, Any] = {}
        if meta.get("device") and meta.get("device") != m.get("device"):
            dev = next((d for d in self.project_info(meta.get("project")).get("devices") or []
                        if d.get("id") == meta.get("device")), {})
        extra = dev.get("protection") or {}
        if not extra:
            try:
                yield []
            finally:
                self.running_meta = {}
            return
        both = protection.combine(m.get("protection") or {}, extra)
        names = [n for n in (m.get("device_name") if m.get("protection") else None, dev.get("name")) if n]
        ranges: Dict[Tuple[str, str], Any] = {}   # the sources' ranges as they were: put back after the run
        for inst, param, _, _ in protection.caps(self.lab, both):
            if inst.enabled and inst.card.range_owner(param) is not None:
                try:
                    ranges[(inst.name, param)] = inst.read(param)
                except Exception:
                    pass
        protection.apply(self.lab, both, " and ".join(names))
        try:
            lines, _ = protection.write_caps(self.gate, self.lab, both)
            yield ["held to %s's protection for this run: %s" % (dev.get("name"), "; ".join(
                protection.summary(extra, self.lab)))] + lines
        finally:
            self.running_meta = {}
            self._apply_protection()
            for (name, param), value in ranges.items():   # only while the source is at safe (the gate's rule)
                try:
                    if value is not None and self.lab.instruments[name].read(param) != value:
                        self.gate.set(name, param, value)
                except Exception:
                    pass

    def mounted_view(self) -> Optional[Dict[str, Any]]:
        """The mounted sample with its protection in words (the Lab panel and the phone)."""
        if not self.mounted:
            return None
        return dict(self.mounted, protection_lines=protection.summary(self.mounted.get("protection") or {}, self.lab),
                    protection_missing=self.protection_missing)

    def keep_hands(self, hands: Optional[Dict[str, Any]]) -> None:
        """At start: a hands-on lock that was on when Kelvin closed is on again."""
        self.hands = hands or None
        self.lab.hands_on = self.hands

    # ------------------------------------------------------------------ hands on the sample

    def hands_on(self, by: str) -> List[str]:
        """A person is about to touch the sample (connect it, rewire, flip a breakout box): stop what runs, bring
        everything that applies something to the sample (sources, excitations, light, RF) to safe with its outputs
        off, and hold every drive where it is until a person unlocks (hands_off). Temperature, field and positions
        stay as they are. Measuring still works. Waits for a stopped plan to finish its own safe-stop steps."""
        self.hands = {"by": by, "since": datetime.now().isoformat(timespec="seconds")}
        self.lab.hands_on = self.hands   # at once: nothing moves away from safe from here on, even before the stop lands
        self.stop(by)
        with self.lock:
            lines: List[str] = []
            kept: List[str] = []
            with self.gate.acting_as(by):
                for name, inst in self.lab.instruments.items():
                    if not inst.enabled:
                        continue
                    if protection.applies_to_sample(inst.card):
                        lines += [r.message for r in self.gate.to_safe(name)]
                    else:
                        kept.append(name)
            lines = lines or ["everything that reaches the sample was already at safe"]
            if kept:
                lines.append("kept as they are (they don't touch the sample): %s" % ", ".join(kept))
            self.gate.log.write("hands_on", by=by, steps=lines)
            self._refresh()
        return lines

    def owner_check(self, pin: Any) -> None:
        """PermissionError (why, in words) unless no owner's PIN is set or this is it (accounts.OwnerPin)."""
        why = self.owner.check(pin) if self.owner is not None else None
        if why:
            raise PermissionError(why)

    def hands_off(self, by: str, pin: Any = None) -> List[str]:
        """The person is done with the sample: drives may move again (each still within limits and approvals).
        The mounted device's caps are written into the instruments again, in case one was replaced or reset. With
        an owner's PIN set, only with it (PermissionError)."""
        self.owner_check(pin)
        with self.lock:
            self.hands = None
            self.lab.hands_on = None
            self.gate.log.write("hands_off", by=by)
        self.audit_event("Hands on the sample unlocked", by)
        return self._write_caps(by)

    # ------------------------------------------------------------------ governance: roles, config history, the owner

    versions: Any = None   # the lab's config history (configversions.ConfigVersions); set by the app
    roles: Any = None      # the lab's roles, on top of the owner's PIN (accounts.LabRoles); set by the app
    accounts: Any = None   # the accounts on this computer (accounts.Accounts): an operator's password for a trainee

    def role(self) -> Optional[str]:
        """The signed-in person's role in this lab: None while it has no roles (everyone as before), else owner,
        operator or trainee (a person the owner gave no role, and nobody signed in, are trainees)."""
        return self.roles.role(self.user) if self.roles is not None else None

    def not_for_trainees(self, what: str) -> None:
        """PermissionError for a trainee: limits, the lab config, the cards' limits and reviews and auto mode are an
        operator's or the owner's (accounts.LabRoles)."""
        if self.role() == "trainee":
            from ..accounts import RoleRefused, trainee_refusal
            raise RoleRefused(trainee_refusal(what))

    def vouched(self, approver: Any = None, pin: Any = None) -> Optional[Dict[str, Any]]:
        """None when the person's own yes counts (no roles, an operator or the owner). For a trainee: who confirms
        it, checked (accounts.LabRoles.vouch: an operator's or the owner's password, or the owner's PIN), or
        PermissionError saying why not."""
        if self.role() != "trainee":
            return None
        given = approver if isinstance(approver, dict) else {}
        return self.roles.vouch(self.accounts, self.owner, given.get("name"), given.get("password"), pin)

    def config_changed(self, by: str, why: str = "", kind: str = "",
                       rollback: Optional[int] = None) -> List[Dict[str, Any]]:
        """After the lab config or the lab's cards changed: each changed file kept as a version in the lab's config
        history (configversions.py), with who (by, and the account signed in with its role), when and why. A change
        that widens Kelvin's reach is said in the lab's record and goes to the owner. Returns the new versions."""
        if self.versions is None:
            return []
        try:
            made = self.versions.sync(by, self.user, self.role(), why, kind, rollback=rollback)
        except OSError as e:   # the change stands; the record says the history missed it
            self.gate.log.write("config_history", by=by, problem=str(e)[:200])
            return []
        self._kept_versions(made, by, why)
        return made

    def config_noticed(self) -> List[Dict[str, Any]]:
        """The lab config and cards as they are now, against the history: an edit made outside Kelvin (by hand) is
        kept as a version of its own, and told to the owner when it widens Kelvin's reach; the first time, the files
        as Kelvin found them. Called when Kelvin starts and when the lab config or its history is opened."""
        if self.versions is None:
            return []
        try:
            made = self.versions.notice()
        except OSError:
            return []
        self._kept_versions([v for v in made if v.get("kind") != "first"], "outside Kelvin", "")
        return made

    def _kept_versions(self, made: List[Dict[str, Any]], by: str, why: str) -> None:
        for v in made:
            self.gate.log.write("config_version", by=by, version=v["n"], file=v["subject"], title=v.get("title"),
                                kind=v.get("kind"), why=why or None, rollback=v.get("rollback"),
                                changes=[c["text"] for c in v["changes"]][:20])
        widened = [c["text"] for v in made for c in v["changes"] if c["safety"]]   # each names its card
        if widened:
            self._tell_owner(widened, by, why, [v["n"] for v in made])

    def audit_event(self, what: str, by: str, why: str = "", widens: bool = True, **fields: Any) -> None:
        """A change to who may do what that is no file's (auto mode on, Hands on the sample unlocked, the lab's
        roles, the owner's PIN): kept in the config history's record; told to the owner when it widens Kelvin's
        reach."""
        if self.versions is not None:
            try:
                self.versions.event(what, by, self.user, self.role(), why, widens, **fields)
            except OSError:
                pass
        if widens:
            self._tell_owner([what], by, why)

    def _tell_owner(self, lines: List[str], by: str, why: str = "", versions: Optional[List[int]] = None) -> None:
        """A safety change, to the lab owner wherever they set (notify.Notifier.tell_owner) and the in-app list."""
        role = self.role()
        person = "%s%s, %s" % (self.user, " (%s)" % role if role else "", by) if self.user else by
        title = "%s: %s%s" % (self.lab.config.name, lines[0], " (and %d more)" % (len(lines) - 1)
                              if len(lines) > 1 else "")
        text = "\n".join(["By %s at %s:" % (person, time.strftime("%H:%M"))] + ["- " + line for line in lines[:12]]
                         + (["Why: " + why] if why else [])
                         + ["Kelvin keeps every version: Settings, Instruments, Lab config history%s."
                            % (" (#%s)" % ", #".join(map(str, versions)) if versions else "")])
        try:
            self.notifier.tell_owner(title[:200], text[:1500])
        except Exception:   # telling never undoes the change; the history has it
            pass

    def _attribute(self, plan: Optional[Plan], project: Optional[str] = None, incognito: Optional[bool] = None,
                   unshared: Optional[bool] = None, legacy: bool = False) -> Dict[str, Any]:
        """Which project and sample a run belongs to, and whose work it is: the plan's own device:, else the mounted
        device, else the project of the chat that started it (or queued it: attribution); runs outside every project
        go to General. Unshared work stays apart: the run of an incognito chat, of a chat that keeps unshared work's
        marks or of a chat in an unshared (unpublished, confidential or model-limited) project never goes under
        another project's sample, nor does any chat's run go under the sample of another project's unshared work
        (_apart says why, for the chat). A run keeps the chat that started it, the rules and marks it ran under, and
        who made it: runs of unshared work, and anyone else's, are never shared on the Kelvin hub, and only chats
        inside that work see them (may_see). project, incognito, unshared: in place of the attribution's.
        legacy: queued before Kelvin noted these; held like older runs."""
        whose = dict(self.attribution)
        for key, value in (("project", project), ("incognito", incognito), ("unshared", unshared)):
            if value is not None:
                whose[key] = value
        m = self.mounted
        incognito = bool(whose.get("incognito"))
        chat_pid = None if incognito else whose.get("project")
        pid = m.get("project") if m else chat_pid
        apart = ""
        if m and (pid or None) != (chat_pid or None):   # the sample in the setup is another project's
            if incognito:
                apart = "an incognito chat's runs go under no project or sample"
            elif whose.get("unshared") or self.project_info(chat_pid).get("unshared"):
                apart = "this chat's work is unshared, and the mounted sample is another project's"
            elif self.project_info(pid).get("unshared"):
                apart = "the mounted sample is another project's unshared work"
            if apart:
                pid, m = chat_pid, {}
        info = self.project_info(pid)
        out = {"project": pid, "project_name": info.get("name") or "General",
               "device": m.get("device") if m else None,
               "device_name": m.get("device_name") if m else None, "user": self.user}
        if whose.get("chat"):
            out["chat"] = whose["chat"]
        if incognito:
            out["incognito"] = True
        if whose.get("unshared"):
            out["unshared"] = True
        if whose.get("rules"):   # the model rules it ran under, which stay with it
            out["rules"] = whose["rules"]
        if whose.get("confidential"):
            out["confidential"] = True
        if whose.get("unpublished"):
            out["unpublished"] = True
        if legacy:
            out["legacy"] = True
        if apart:   # said to the chat that started it, never kept with the run
            out["_apart"] = apart
        if plan is not None and plan.device:   # a lab's script (no plan) goes under the mounted device
            dev = next((d for d in info.get("devices") or []
                        if d["id"] == plan.device or d["name"].lower() == plan.device.lower()), None)
            out["device"], out["device_name"] = (dev["id"], dev["name"]) if dev else (None, plan.device)
        self._stamp_chain(out)
        return out

    def queue_show(self) -> str:
        return self._queue_text()

    def _queue_text(self) -> str:
        """The queue in lines, as the viewer of this thread may see it: an item of unshared work it may not see shows
        only its place and state."""
        if _VIEWER.get() is None:
            return self.queue.text()
        cache: Dict[Any, Any] = {}
        return self.queue.text(lambda item: not self.may_see(item.whose(), cache))

    def queue_view(self) -> Dict[str, Any]:
        return dict(self.queue.view(), saved=saved_queues(self.data_dir / "queues"))

    def _queue_problems(self, text: str) -> List[str]:
        """What stops an item from running on this lab, before it joins the queue. It is checked beside whatever
        else Kelvin does (a scan running): what the check reads of an instrument is read as any reading is."""
        kind, _ = parse_item(text)
        if kind == "plan":
            return RunEngine(_ReadingGate(self)).validate(self._plan(text))
        step, errors = load_step(text), []
        for instr, param, value in step.sets:
            try:
                inst, spec = self.lab.resolve("%s.%s" % (instr, param))
                if spec.set_cmd is None:
                    errors.append("%s.%s is read-only" % (instr, param))
                    continue
                target = spec.coerce(value)
                lim = inst.limits(param)
                if spec.numeric and lim and not lim[0] <= target <= lim[1]:
                    errors.append("%s.%s %s is outside the limits [%s, %s]" % (instr, param, spec.show(target),
                                                                            num(lim[0]), num(lim[1])))
            except (KeyError, ValueError) as e:
                errors.append(e.args[0] if e.args else str(e))
        if step.stabilize:
            spec = self._spec(step.stabilize.key)
            if spec is None or spec.get_cmd is None:
                errors.append("%s cannot be read" % step.stabilize.key)
        return errors

    def queue_add(self, text: str, position: Optional[int] = None, note: str = "", by: str = "agent") -> str:
        try:
            problems = self._queue_problems(text)
        except PlanError as e:
            return "not added: %s" % e
        if problems:
            return "not added: " + "; ".join(problems[:5])
        whose = self.attribution
        item = self.queue.add(text, position, note, by, project=whose.get("project"),
                              incognito=bool(whose.get("incognito")), unshared=bool(whose.get("unshared")),
                              folder=whose.get("folder"), chat=whose.get("chat"), rules=whose.get("rules"),
                              confidential=bool(whose.get("confidential")))
        pos = self.queue.items.index(item) + 1
        self.gate.log.write("queue_add", by=by, item=item.id, kind=item.kind, name=item.name, position=pos)
        return "added %s %s as #%d\n%s" % (item.kind, item.name, pos, self._queue_text())

    def queue_edit(self, ref: Any, text: str, by: str = "agent") -> str:
        try:
            problems = self._queue_problems(text)
            if problems:
                return "not changed: " + "; ".join(problems[:5])
            item = self.queue.edit(ref, text)
        except (PlanError, KeyError, ValueError) as e:
            return "not changed: %s" % (e.args[0] if e.args else e)
        self.gate.log.write("queue_edit", by=by, item=item.id, name=item.name)
        return "changed %s\n%s" % (item.name, self._queue_text())

    def queue_change(self, op: str, ref: Any = None, position: Optional[int] = None, by: str = "agent") -> str:
        """move, remove, again (put a finished item back in line), clear (remove finished items)."""
        try:
            if op == "move":
                item = self.queue.move(ref, int(position or 1))
            elif op == "remove":
                item = self.queue.remove(ref)
            elif op == "again":
                item = self.queue.again(ref)
            elif op == "clear":
                n = self.queue.clear_finished()
                self.gate.log.write("queue_clear", by=by, removed=n)
                return "removed %d finished items\n%s" % (n, self._queue_text())
            else:
                return "unknown queue change %r" % op
        except (KeyError, ValueError, TypeError) as e:
            return "not changed: %s" % (e.args[0] if e.args else e)
        self.gate.log.write("queue_" + op, by=by, item=item.id, name=item.name)
        return self._queue_text()

    def queue_set_on_error(self, value: str, by: str = "human") -> None:
        if value not in ("pause", "continue"):
            raise ValueError("on_error is pause or continue")
        with self.queue.lock:
            self.queue.on_error = value
            self.queue.save()
        self.gate.log.write("queue_on_error", by=by, value=value)

    def queue_save(self, name: str) -> str:
        try:
            path = save_named(self.data_dir / "queues", name, self.queue.waiting() or list(self.queue.items))
        except (ValueError, OSError) as e:
            return "not saved: %s" % e
        return "saved the queue as %s" % path.name

    def queue_load(self, name: str, by: str = "agent") -> str:
        try:
            entries = load_named(self.data_dir / "queues", name)
        except KeyError as e:
            return e.args[0]
        added, problems = 0, []
        for text, note in entries:
            out = self.queue_add(text, None, note, by)
            if out.startswith("not added"):
                problems.append(out)
            else:
                added += 1
        return "\n".join(["added %d items from %s" % (added, name)] + problems + [self._queue_text()])

    def _queue_drives(self, items: List[Item]) -> Dict[str, List[Any]]:
        from ..forecast import action_of   # an on_abort action is the card's, not a drive's value
        out: Dict[str, List[Any]] = {}
        for item in items:
            if item.kind == "plan":
                plan = self._plan(item.text)
                values = self.engine.drive_values(plan)
                for s in plan.on_abort or []:   # what it sets after a stop, which in the night nobody is there to approve
                    if action_of(self.lab, s.instr, s.param) is None and self.lab.resolve(s.key)[1].is_drive:
                        values.setdefault(s.key, []).append(s.value)
            else:
                values = {}
                for instr, param, value in load_step(item.text).sets:
                    key = "%s.%s" % (instr, param)
                    spec = self.lab.resolve(key)[1]
                    if spec.is_drive:
                        values.setdefault(key, []).append(spec.coerce(value))
            for key, vals in values.items():
                out.setdefault(key, []).extend(vals)
        return out

    def _queue_forecast(self, items: List[Item]) -> Any:
        """The waiting items walked through in order, each from where the one before leaves the lab, for the queue's
        card (forecast.py): every drive and setting, the rates read from the instruments, the duration, the end."""
        from ..forecast import Forecast
        from .. import techniques
        forecast = Forecast(self.engine, "the queue")
        for item in items:
            if item.kind == "plan":
                plan = self._plan(item.text)
                forecast.plan(plan)
                techniques.account(forecast, self.lab, plan)   # its techniques' time, which the walk doesn't count
            else:
                forecast.queue_step(load_step(item.text))
        return forecast

    def _queue_techniques(self, items: List[Item]) -> Tuple[List[str], List[str]]:
        """The techniques that drive the sample its scans run (instr.technique), and their lines for its card: the
        queue's one approval covers them, as it covers the actions its scans start."""
        from .. import techniques
        keys: List[str] = []
        lines: List[str] = []
        for n, item in enumerate(items, 1):
            if item.kind != "plan":
                continue
            plan = self._plan(item.text)
            found = techniques.driving(self.lab, plan)
            if found:
                keys += [k for k in found if k not in keys]
                lines += ["#%d %s: %s" % (n, item.name, line) for line in techniques.lines(self.gate, plan)]
        return keys, lines

    def _queue_later(self, items: List[Item]) -> List[str]:
        """The drives whose interlock condition an item before the first that moves them works on (it sets something on
        the condition's instrument: a cool-down step before a field sweep). Their interlock is checked when they move,
        not at the start: a queue that cools first could not start while the sample was still warm."""
        touched: set = set()   # instruments an item so far sets something on
        used: set = set()      # drives an item so far moves
        later = []
        for item in items:
            if item.kind == "plan":
                plan = self._plan(item.text)
                keys = {s.key for s in plan.setup + plan.after} | {a.key for a in plan.instrument_axes} \
                    | ({plan.ramp.key} if plan.ramp else set())
            else:
                keys = set(load_step(item.text).keys)
            later += [lock.drive for lock in self.lab.config.interlocks
                      if lock.drive in keys and lock.drive not in used and lock.require.instr in touched]
            touched.update(k.split(".")[0] for k in keys)
            used.update(keys)
        return later

    def _rehearse(self, items: List[Item]) -> Tuple[float, List[str]]:
        """The whole queue, in order, on a simulated copy of the lab: (lab seconds it takes, what went wrong)."""
        from ..gate import SimulationApprover
        twin = self.lab.make_twin()
        gate = SafetyGate(twin, SimulationApprover(), EventLog(None, twin.clock), envelope=self.gate.envelope,
                          operator="dry-run")
        engine = RunEngine(gate, RunStore(None))
        engine.analyze = False
        t0, problems, warnings = twin.clock.now(), [], []
        for n, item in enumerate(items, 1):
            if item.kind == "plan":
                r = engine.run(self._plan(item.text), dry_run=False)
                if r.status != "done":
                    problems.append("#%d %s: %s" % (n, item.name, r.message))
                continue
            step = load_step(item.text)
            for instr, param, value in step.sets:
                r = gate._set(instr, param, value, True, "dry-run")
                if not r.ok:
                    problems.append("#%d %s: %s" % (n, item.name, r.message))
            if step.stabilize:
                engine._stabilize(step.stabilize, gate.stop_generation, warnings)
            twin.clock.sleep(step.wait)
        return twin.clock.now() - t0, problems

    def queue_start(self, by: str = "agent", dry_run: bool = False) -> str:
        """Check every waiting item, ask a person once for every drive range, then run the queue in the background.
        Returns at once after the approval; queue_show follows it. dry_run: only a rehearsal of the whole queue in
        order on the simulated lab (how long it takes, what would fail); the queue does not start."""
        with self.queue.lock:
            if self.queue.state in ("running", "pausing"):
                return "the queue is already running\n" + self._queue_text()
        items = self.queue.waiting()
        if not items:
            return "nothing is waiting in the queue"

        def check() -> List[str]:
            problems = []
            for n, item in enumerate(items, 1):
                try:
                    problems += ["#%d %s: %s" % (n, item.name, p) for p in self._queue_problems(item.text)]
                except PlanError as e:
                    problems.append("#%d %s: %s" % (n, item.name, e))
            return problems

        def rehearse() -> str:
            problems = check()
            if problems:
                return REHEARSAL % "queue_start" + "\nnot rehearsed:\n" + "\n".join(problems[:8])
            duration, failed = self._rehearse(items)
            _, _, refusals = self.gate.plan_approval(self._queue_drives(items), whole_path=True,
                                                     later=self._queue_later(items))
            return "\n".join([REHEARSAL % "queue_start", "the %d waiting item%s would take ~%s" % (
                len(items), "" if len(items) == 1 else "s", _dur(duration))]
                + ["would fail: " + f for f in failed[:8]] + ["would be refused: " + r for r in refusals[:8]])

        if dry_run:
            return self._do("rehearsing the queue", rehearse)

        def prepare() -> str:
            problems = check()
            if problems:
                return "the queue did not start:\n" + "\n".join(problems[:8])
            forecast = self._queue_forecast(items)   # the whole queue in order: rates, duration, what it leaves on
            starting = forecast.driving_actions()     # a waveform its scans start: on this card, approved with it
            running, technique_lines = self._queue_techniques(items)   # a technique its scans run: likewise
            # over the queue's whole path: every value an item sets away from safe, wherever the drive is now, so
            # nothing asks again in the night (an item that switches an output off made the next one ask)
            lines, grants, refusals = self.gate.plan_approval(self._queue_drives(items), forecast=forecast,
                                                              asking=bool(starting or running), whole_path=True,
                                                              later=self._queue_later(items))
            if refusals:
                return "the queue did not start; these would be refused:\n" + "\n".join(refusals[:8])
            duration = forecast.seconds or None
            scans = sum(1 for i in items if i.kind == "plan")
            steps = len(items) - scans
            title = "Run the queue: %d scan%s%s%s" % (scans, "" if scans == 1 else "s", " and %d step%s" % (
                steps, "" if steps == 1 else "s") if steps else "", forecast.duration())
            if lines or starting or running:
                request = ApprovalRequest(title, lines + forecast.action_lines(starting) + technique_lines
                                          + forecast.closing() + [
                    "This approval covers every item now waiting; anything outside these ranges asks again."])
                covered = False
                if self.gate.auto:   # as a run is: rehearsed on the simulated lab, and every range low-risk
                    duration, failed = self._rehearse(items)
                    rehearsal = type("Rehearsal", (), {"ok": not failed})()
                    covered, why = self.gate.auto_plan(grants, rehearsal)
                    if starting:   # an action that drives the sample is a person's to start, as with do_action
                        covered, why = False, why + ["its scans start %s" % ", ".join(starting)]
                    if running:   # nothing watches inside a technique: a person approves it
                        covered, why = False, why + ["its scans run %s, which drive the sample on the instrument's "
                                                     "own clock" % ", ".join(running)]
                    if covered:
                        self.gate.auto_approve(request, why)
                    else:
                        request.items.append("Auto mode does not cover this: " + "; ".join(
                            why + ["the dry run: %s" % f for f in failed[:2]]))
                if not covered and not self.gate.ask(request):
                    note = " (%s)" % request.note if request.note else ""
                    self.gate.log.write("queue_declined", by=by)
                    return "declined: the queue did not start%s" % note
            self.gate.queue_actions = frozenset(starting) | frozenset(running)   # its scans do these, not asking again
            with self.queue.lock:
                self.queue.state, self.queue.reason = "running", ""
                self.queue.save()
            self.gate.log.write("queue_start", by=by, items=len(items),
                                expected_s=round(duration) if duration else None)
            self._queue_thread = threading.Thread(target=self._queue_loop, args=(grants, by), daemon=True,
                                                  name="kelvin-queue")
            self._queue_thread.start()
            return "queue started: %s\n%s" % (title[len("Run the queue: "):], self._queue_text())
        return self._do("starting the queue", prepare, stoppable=True,
                        uses=self._uses("\n".join(item.text for item in items)))   # the rest stay readable

    def queue_pause(self, by: str = "agent") -> str:
        """Finish the item that is running, then stop there. Stop (the Stop button) also ends the running scan."""
        with self.queue.lock:
            if self.queue.state != "running":
                return "the queue is not running\n" + self._queue_text()
            self.queue.state, self.queue.reason = "pausing", "paused by %s" % by
            self.queue.save()
        self.gate.log.write("queue_pause", by=by)
        return "the queue pauses after the item that is running"

    def _queue_loop(self, grants: Dict[str, Allowed], by: str) -> None:
        q = self.queue
        started = time.time()
        while True:
            with q.lock:
                item = q.next_waiting() if q.state == "running" else None
                if item is None:
                    finished = q.state == "running"
                    q.state = "idle" if finished else "paused"
                    if finished:
                        q.reason = "finished"
                    q.save()
                    break
                q.mark(item, "running")
                position = q.items.index(item) + 1
            lost, t0 = self.monitor.mark(), time.time()
            ok, message = self._queue_item(item, position, grants, by)
            if not ok:   # an instrument that stopped answering ended it: its alarm goes up now, with the pause
                self.monitor.lost_since(lost)   # and the pause says which alarm stopped it ("stop requested" doesn't)
                said = [a.text for a in list(self.monitor.alarms) if a.raised >= t0 and a.text not in message]
                message += "; alarm: " + "; ".join(said) if said else ""
            with q.lock:   # a person's stop of the scan is never an error to go on after
                if not ok and q.state == "running" and (q.on_error == "pause" or item.status == "stopped"):
                    q.state, q.reason = "pausing", "%s: %s" % (item.name, message)
                    q.save()
        done = sum(1 for i in q.items if i.status == "done")
        self.gate.log.write("queue_end", by=by, state=q.state, reason=q.reason, done=done)
        ran = [i.run_id for i in q.items if i.run_id and i.started and i.started >= started]
        if ran and self.auto_report != "off":   # one report of everything this queue measured, like a lab notebook page
            try:
                self.report(ran, title="Queue of %s" % datetime.now().strftime("%d %b %Y"), fmt=self.auto_report,
                            by="kelvin (automatic)", folder=self.reports_dir / "queues")
            except Exception as e:
                self.gate.log.write("report_failed", by="kelvin (automatic)", runs=len(ran), error=str(e)[:300])
        # the morning's account, from the record alone: done, stopped part-way with its data, not run, every alarm
        lines = q.summarize(started, list(self.monitor.alarms), self._run_points,
                            lambda item: self.unshared_work(item.whose()))
        self.tell("run", "Queue finished" if q.state == "idle" else "Queue paused", "\n".join(lines)[:1500],
                  bool((q.last or {}).get("unshared")))

    def _run_points(self, run_id: str) -> Optional[Tuple[int, int]]:
        """(points taken, points planned) of a run, from its record; None when it can't be read."""
        try:
            m = rundata.meta(self._run_dir(run_id))
            return int(m.get("points") or 0), int(m.get("planned_points") or 0)
        except Exception:
            return None

    def _queue_item(self, item: Item, position: int, grants: Dict[str, Allowed], by: str) -> Tuple[bool, str]:
        """Run one item under the queue's approval. (ok, message)"""
        q = self.queue
        stops = self.gate.stop_generation
        with self.lock:
            if self.gate.stop_generation != stops:   # stopped while it waited for the lab: it never starts
                q.mark(item, "stopped", "a person pressed Stop before it started")
                return False, "stopped before it started"
            self.busy, self._acted = "queue %d/%d: %s" % (position, len(q.items), item.name), True
            try:   # filed as the item was queued, never under a chat whose call goes on meanwhile (attributed)
                uses = self._involved(self._uses(item.text))   # the instruments it uses; the rest stay readable
                whose = item.whose()
                whose["private"] = self.unshared_work(whose)   # asked once, not at every event it writes
                with self.gate.standing(grants), self.gate.acting_as("queue (started by %s)" % by), \
                        self.attributed(whose):
                    if item.kind == "plan":
                        plan = self._plan(item.text)
                        meta = self._attribute(plan, legacy=item.legacy)
                        meta.pop("_apart", None)
                        with self._device_protection(meta), self.lock.only(uses + self._involved(plan.requires)):
                            report = self.engine.run(plan, meta=dict(meta, queue={
                                "item": item.id, "position": position, "note": item.note}), folder=item.folder)
                        status = {"done": "done", "aborted": "stopped"}.get(report.status, "failed")
                        if report.status == "aborted" and "stop" not in (report.message or "").lower():
                            status = "failed"
                        q.mark(item, status, report.message if status != "done" else "", report.run_id)
                        with self.lock.only([]):   # its report uses no instrument
                            self._auto_report(report.run_id)
                        return status == "done", report.message
                    with self.lock.only(uses):
                        return self._queue_step(item)
            except Exception as e:   # an instrument that stopped answering, a plan that no longer parses...
                q.mark(item, "failed", str(e)[:300])
                return False, str(e)
            finally:
                self.busy = None
                self._caps_if_waiting()
                self._refresh()

    def _queue_step(self, item: Item) -> Tuple[bool, str]:
        from ..engine import _Abort
        step, q = load_step(item.text), self.queue
        gen = self.gate.stop_generation
        for instr, param, value in step.sets:
            if self.gate.stop_generation != gen:
                q.mark(item, "stopped", "stopped before %s.%s was set" % (instr, param))
                return False, "stopped"
            r = self.gate._set(instr, param, value, True, "queue:" + item.name, gen=gen)   # a stop ends its waits too
            if not r.ok:   # a person's stop in the middle of it is a stop, not a failure to go on after
                stopped = self.gate.stop_generation != gen
                q.mark(item, "stopped" if stopped else "failed", r.message)
                return False, "stopped" if stopped else r.message
        warnings: List[str] = []
        try:
            if step.stabilize:
                self.engine._stabilize(step.stabilize, gen, warnings)
            stops = self.lab.stops.count
            if self.gate.stop_generation != gen or not self.lab.stops.sleep(self.lab.clock, step.wait, stops):
                raise _Abort("stop requested")   # a stop ends the wait at once, not at its next second
        except _Abort:
            q.mark(item, "stopped", "stopped while waiting")
            return False, "stopped"
        q.mark(item, "done", "; ".join(warnings))
        return True, ""

    def run_data(self, run_id: str, columns: Optional[List[str]] = None, max_rows: int = 50, where: str = "") -> str:
        """A run's points as CSV, evenly thinned to max_rows; where: only the points meeting a condition (expr.py).
        Traces show as trace[n] and are not read (get_trace reads one)."""
        try:
            table = rundata.columns(self._run_dir(run_id))
        except (KeyError, OSError, ValueError) as e:
            return e.args[0] if e.args else str(e)
        if not table.n:
            return "run %s has no data" % run_id
        cols = columns or table.order
        missing = [c for c in cols if c not in table.order]
        if missing:
            return "unknown columns %s (have: %s)" % (", ".join(missing), ", ".join(table.order))
        picked = list(range(table.n))
        if where:
            try:
                picked = self._where(table, where)
            except ValueError as e:
                return "where not applied: %s" % e
            if not picked:
                return "no points of %s meet %s (of %d)" % (run_id, where, table.n)
        step = max(1, -(-len(picked) // max(1, int(max_rows))))
        shown = picked[::step]
        if shown[-1] != picked[-1]:
            shown.append(picked[-1])
        head = "%s: %d rows%s%s" % (run_id, table.n, "; %d where %s" % (len(picked), where) if where else "",
                                    "" if step == 1 else ", every %dth shown" % step)
        return "\n".join([head, ",".join(cols)] + [",".join(table.cell(c, i) for c in cols) for i in shown])

    @staticmethod
    def _where(table: rundata.Columns, where: str) -> List[int]:
        """The points (indices) of a run meeting a condition such as "smu.V > 1 and tc.T < 5"."""
        import numpy as np
        from .. import expr
        mask = np.asarray(expr.evaluate(where, table.number, np))
        if mask.dtype != bool:
            raise expr.ExprError("where needs a condition, e.g. smu.V > 1")
        return [int(i) for i in np.flatnonzero(np.broadcast_to(mask, (table.n,)))]

    def compute(self, run_id: str, expression: str, where: str = "", max_rows: int = 50) -> str:
        """An expression over a run's columns (expr.py): one number, or a value at each point shown against the
        swept axes, with its minimum and maximum and where they are."""
        import numpy as np
        from .. import expr
        try:
            d = self._run_dir(run_id)
            table = rundata.columns(d)
            axes = [a["key"] for a in rundata.load(d, with_rows=False).columns.get("axes") or []]
        except (KeyError, OSError, ValueError) as e:
            return e.args[0] if e.args else str(e)
        if not table.n:
            return "run %s has no data" % run_id
        picked = np.arange(table.n)
        try:
            if where:
                picked = np.asarray(self._where(table, where), dtype=int)
                if not picked.size:
                    return "no points of %s meet %s (of %d)" % (run_id, where, table.n)
            value = expr.evaluate(expression, lambda key: table.number(key)[picked], np)
        except ValueError as e:
            return "not computed: %s" % e
        value = np.asarray(value, dtype=float)
        scope = "%d points%s" % (picked.size, " where " + where if where else "")
        if value.ndim == 0:
            return "%s = %.6g (over %s)" % (expression, float(value), scope)
        if value.ndim > 1:
            return "not computed: the result has %d dimensions" % value.ndim
        n = value.size
        per_point = n == picked.size
        axes = [a for a in axes if a in table.numbers]

        def at(k: int) -> str:
            if not per_point:
                return "index %d" % k
            p = int(picked[k])
            return ", ".join("%s=%s" % (a, table.cell(a, p)) for a in axes) or "point %d" % p

        lines = ["%s over %s: %d values" % (expression, scope, n)]
        if np.isfinite(value).any():
            lo, hi = int(np.nanargmin(value)), int(np.nanargmax(value))
            lines.append("min %.6g at %s; max %.6g at %s; mean %.6g" % (value[lo], at(lo), value[hi], at(hi),
                                                                          float(np.nanmean(value))))
        else:
            lines.append("no finite values")
        step = max(1, -(-n // max(1, int(max_rows))))
        shown = list(range(0, n, step))
        if shown and shown[-1] != n - 1:
            shown.append(n - 1)
        if step > 1:
            lines.append("every %dth value shown" % step)
        if per_point:
            lines.append(",".join(["i"] + axes + ["value"]))
            lines += [",".join([table.cell("i", int(picked[k]))] + [table.cell(a, int(picked[k])) for a in axes] +
                               ["%.6g" % value[k]]) for k in shown]
        else:
            lines.append("index,value")
            lines += ["%d,%.6g" % (k, value[k]) for k in shown]
        return "\n".join(lines)

    def trace(self, run_id: str, row: int, column: str, max_points: int = 200) -> str:
        try:
            one = rundata.trace_row(self._run_dir(run_id), column, int(row))   # that trace alone, from run.h5
        except KeyError as e:
            return e.args[0] if e.args and str(e.args[0]).startswith("no run") else "%s is not a trace column" % column
        except IndexError as e:
            return "row must be 0…%d" % (e.args[0] - 1)
        if one is None:   # a run not kept as run.h5 (being measured, or Kelvin's core alone)
            rows = self._rows(run_id)
            if not 0 <= row < len(rows):
                return "row must be 0…%d" % (len(rows) - 1)
            one = rows[row]
        rows = {row: one}
        values = rows[row].get(column)
        if not isinstance(values, list):
            return "%s is not a trace column" % column
        n = len(values)
        keep = thin(values, max(4, int(max_points)))   # a narrow dip or peak stays in, however few are shown
        axis = rows[row].get(column + ".axis") or rows[row].get(column + ".x")
        spec = self._spec(column)
        lines = ["%s row %d: %s%s" % (run_id, row, trace_summary(spec, values) if spec else "%d points" % n,
                                      "" if len(keep) == n else "; %d of them shown: the first, the last and each "
                                      "stretch's lowest and highest" % len(keep))]

        def y(i: int) -> str:
            return "nan" if values[i] is None else "%.6g" % values[i]
        if axis:
            xs = trace_x(rows[row], column, n)
            lines.append("x,y")
            lines += ["%.12g,%s" % (xs[i], y(i)) for i in keep]
        else:
            lines.append("index,y")
            lines += ["%d,%s" % (i, y(i)) for i in keep]
        return "\n".join(lines)

    # ------------------------------------------------------------------ pictures (look_at)

    def look(self, run_id: str = "", column: str = "", frame: str = "", camera: str = "") -> Tuple[Optional[bytes], str]:
        """look_at: (a PNG, its caption), or (None, why not). A run's plots as its report draws them (one column's
        alone; a 2-D scan with many rows as a map; a camera column as its last frame), a saved frame, or a frame
        read now from a camera."""
        from .. import pictures
        frame = str(frame or "")
        try:
            if camera:
                return self._look_camera(camera.strip())
            if frame and not run_id:
                return self._look_file(Path(frame))
            if not run_id:
                return None, ("say what to look at: run_id (a run's plots; 'last' for the latest), frame (a saved "
                              "frame's file) or camera (instrument.parameter, a frame read now)")
            if run_id == "last":
                ids = self.pick_runs(last=1)
                if not ids:
                    return None, "no runs yet"
                run_id = ids[-1]
            d = self._run_dir(run_id)
            if frame.strip().isdigit():   # a frame by its point's number (from 0): frame k of a stack
                ref, key, total = self._frame_at(d, column, int(frame))
                return self._look_array(rundata.frame(d, ref), "%s: frame %s of %s (%d frames)" % (
                    run_id, frame.strip(), key, total))
            if frame:   # one of this run's frames, by the reference its data keep
                return self._look_array(rundata.frame(d, frame), "%s, frame %s" % (run_id, frame))
            rec = rundata.load(d)
            cols = self._columns(rec)
            part = self._collector().run(run_id, [])
            head = "Run %s (%s, %s, %s points)" % (run_id, part.plan, part.status, part.points)
            measures = cols.get("measures") or []
            names = [m["key"] for m in measures]
            if column and column not in names:
                return None, "%s measured %s, not %s" % (run_id, ", ".join(names) or "nothing", column)
            images = [m for m in measures if m.get("kind") == "image" and (not column or m["key"] == column)]
            if images and (column or len(images) == len(measures)):   # a camera column: its last frame
                key = images[0]["key"]
                last = next((r for r in reversed(rec.rows) if r.get(key)), None)
                if last is None:
                    return None, "%s has no frame of %s yet" % (run_id, key)
                return self._look_array(rundata.frame(d, str(last[key])), "%s: the last frame of %s (point %d of "
                                        "%d)" % (head, key, rec.rows.index(last) + 1, len(rec.rows)))
            axes = [a for a in cols.get("axes") or [] if a.get("kind") != "repeat"]
            scalars = [m for m in measures if m.get("kind") not in ("trace", "image")]
            if len(axes) >= 2 and scalars:   # many rows of a 2-D scan read best as a map
                m = next((m for m in scalars if m["key"] == column), None if column else scalars[0])
                grid = pictures.grid_of(rec.rows, axes[0]["key"], axes[-1]["key"], m["key"]) if m else None
                if grid and len(grid[0]) > 8:
                    outs, ins, z = grid
                    args = (m["key"], outs, ins, z, axes[-1]["key"], axes[-1].get("unit", ""), axes[0]["key"],
                            axes[0].get("unit", ""), m.get("unit", ""))
                    others = [s["key"] for s in scalars if s is not m]
                    return pictures.map_png(*args), "%s. %s%s" % (head, pictures.map_caption(*args), (
                        " Also measured: %s (look_at with column)." % ", ".join(others)) if others else "")
            charts = [c for c in part.charts if not column or c.title.split(" (")[0] == column]
            if not charts:
                return None, "%s has nothing to plot%s yet" % (run_id, " of " + column if column else "")
            return pictures.charts_png(charts), "%s.\n%s" % (head, pictures.charts_caption(charts))
        except (KeyError, ValueError, OSError, RuntimeError) as e:
            return None, "nothing to show: %s" % (e.args[0] if e.args else e)
        except ImportError:
            return None, "pictures need NumPy, which this Kelvin does not have"

    def _frame_at(self, d: Path, column: str, k: int) -> Tuple[str, str, int]:
        """The reference of a run's frame k (from 0) in its camera column (the first, without one): (ref, column,
        how many points it has). ValueError: no such frame."""
        rec = rundata.load(d)
        images = [m["key"] for m in self._columns(rec).get("measures") or []
                  if m.get("kind") == "image" and (not column or m["key"] == column)]
        if not images:
            raise ValueError("%s has no camera frames%s" % (d.name, " in " + column if column else ""))
        if not 0 <= k < len(rec.rows) or not rec.rows[k].get(images[0]):
            raise ValueError("%s has no frame %d of %s (its frames are 0 to %d)" % (
                d.name, k, images[0], len(rec.rows) - 1))
        return str(rec.rows[k][images[0]]), images[0], len(rec.rows)

    def _look_camera(self, key: str) -> Tuple[Optional[bytes], str]:
        spec = self._spec(key)
        if spec is None:
            return None, "no reading %r (instrument.parameter)" % key
        if spec.type != "image":
            return None, "%s is not a camera frame; look_at shows frames, and runs as plots" % key
        instr, _, param = key.partition(".")
        with self._reading(instr) as why:   # a frame read now, beside whatever else Kelvin does
            if why is None:
                r = self.gate.get(instr, param)
        if why is not None:   # a run uses the camera: its frames are the run's
            live = self.live or {}
            mine = live.get("status") == "running" and key in [m.get("key") for m in live.get("measures") or []]
            return None, "%s not read now (%s)%s" % (key, why, "; look_at with run_id %s shows the run's last frame"
                                                     % live.get("run_id") if mine else "")
        if not r.ok:
            return None, r.message
        return self._look_file(Path(str(r.value)), "%s, read now" % key)

    def _look_file(self, path: Path, head: str = "") -> Tuple[Optional[bytes], str]:
        """A frame read outside a run: only Kelvin's own (its frames folder), never any file on the computer."""
        from .. import pictures
        p, folder = path.resolve(), frames.folder().resolve()
        if p.suffix.lower() != ".npy" or folder not in p.parents:
            return None, "look_at shows Kelvin's own frames: a run's (give run_id), or one read outside a run"
        if not p.is_file():
            return None, "no frame %s" % path
        return self._look_array(pictures.load_frame(p), head or p.name)

    @staticmethod
    def _look_array(frame: Any, head: str) -> Tuple[Optional[bytes], str]:
        from .. import pictures
        data, caption = pictures.frame_png(frame)
        return data, "%s. %s" % (head, caption)

    def events(self, n: int = 20, about: str = "") -> str:
        """The last n entries of the event log; about: only those that mention it (an instrument, a run, a word).
        Never what the viewer of this thread may not see of unshared work (seen_events)."""
        n = max(1, min(int(n), 200))
        restricted = _VIEWER.get() is not None
        if not about.strip():
            tail = tail_events(self.events_path, 5000 if restricted else n)
            seen = self.seen_events(tail)[-n:]
            lines = [event_line(e) for e in seen]
            left = len(tail) - (tail.index(seen[0]) if seen else 0) - len(seen)   # left out among them
            if left:
                lines.append("(%d entr%s of unshared work (another project's or chat's) not shown here)"
                             % (left, "y" if left == 1 else "ies"))
            return "\n".join(lines) or "no events yet"
        word = about.strip().lower()
        lines = [line for line in map(event_line, self.seen_events(tail_events(self.events_path, 5000)))
                 if word in line.lower()]
        return "\n".join(lines[-n:]) or "nothing about %r in the last 5000 events" % about.strip()

    def calculate(self, expression: str) -> str:
        """A number worked out exactly: arithmetic, functions and physical constants (expr.py), no columns."""
        import numpy as np
        from .. import expr

        def no_column(key: str) -> Any:
            raise KeyError("%r is not a known constant (%s)" % (key, " ".join(expr.CONSTANTS)))
        try:
            with np.errstate(all="ignore"):
                value = np.asarray(expr.evaluate(expression, no_column, np), dtype=float)
        except (expr.ExprError, ArithmeticError, ValueError, TypeError) as e:
            return "not calculated: %s" % e
        shown = ("%.10g" % value.item() if value.size == 1 else
                 "[%s%s]" % (", ".join("%.6g" % v for v in value.ravel()[:20]), ", …" if value.size > 20 else ""))
        return "%s = %s" % (expression.strip(), shown)

    # ------------------------------------------------------------------ provenance and measurement chains

    def run_provenance(self, run_id: str) -> str:
        """A run's plan as it ran, its conditions at the start and the end, its measurement chain, every approval it
        needed with who gave it, and the stops, trips and alarms while it ran (provenance.py). Files only."""
        from .. import provenance
        try:
            d = self._run_dir(run_id)
            return provenance.run_provenance(d, self.lab, self.gate.log.events(),
                                             [f["name"] for f in self.run_files(run_id)])
        except (KeyError, OSError, ValueError) as e:
            return "cannot read %s: %s" % (run_id, e.args[0] if e.args else e)

    def activity(self, since: str = "", until: str = "", who: str = "", kinds: str = "", limit: int = 60) -> str:
        """The lab record over a stretch of wall-clock time, each event with who did it: the lab's instrument log
        (everyone's, with who was signed in) where there is one, else this person's record."""
        from .. import provenance
        shared = Path(self.shared_log) if self.shared_log else None
        paths = [shared] if shared is not None and shared.exists() else [self.events_path]
        try:
            every = provenance.read_events(paths)
            seen = {id(e) for e in self.seen_events(every)}
            return provenance.activity(every, since, until, who, kinds, limit,
                                       hidden=(lambda e: id(e) not in seen) if len(seen) < len(every) else None)
        except ValueError as e:
            return "no activity read: %s" % e

    def _chain_device(self, device: str) -> Tuple[Optional[str], Optional[str]]:
        """(id, name) of a device named in a chat (in the mounted device's project), or of the mounted one; never one
        of another project's unshared work (may_see)."""
        m = self.mounted or {}
        if not device:
            return m.get("device"), m.get("device_name")
        wanted = device.strip().lower()
        for pid in dict.fromkeys([m.get("project"), self.attribution.get("project")] + list(self.project_ids())):
            if not self.may_see({"project": pid}):
                continue
            for d in self.project_info(pid).get("devices") or []:
                if wanted in (str(d.get("id") or "").lower(), str(d.get("name") or "").lower()):
                    return d.get("id"), d.get("name")
        return None, device.strip()

    def measurement_chain(self, device: str = "", chain: Any = None, by: str = "agent", remove: bool = False) -> str:
        """Read, record or remove a device's measurement chain (chain.py): device by name (default the mounted one;
        "setup" for the setup's own, which a device without one of its own uses). Recorded chains are stamped into
        every run of that device from then on. The mounted sample of another project's unshared work is that
        project's: its chain is kept and read from its own chats."""
        from ..chain import ChainError, Chains, lines, parse
        m = self.mounted or {}
        if not device.strip() and m and not self.may_see({"project": m.get("project")}):
            return ("the mounted sample is another project's unshared work: its measurement chain is kept and read in "
                    "that project's chats (\"setup\" names the setup's own)")
        store = Chains(self.data_dir)
        dev_id, dev_name = (None, None) if device.strip().lower() == "setup" else self._chain_device(device)
        whose = dev_name or "the setup"
        if remove:
            gone = store.remove(dev_id, dev_name)
            if gone:
                self.gate.log.write("chain", by=by, device=dev_name, removed=True)
            return ("removed the measurement chain of %s" % whose) if gone else "%s has no chain of its own" % whose
        if chain not in (None, "", {}):
            try:
                kept = parse(chain)
            except ChainError as e:
                return "not recorded: %s" % e
            unknown = [k for k in [kept.get("excitation", {}).get("source")] + list(kept.get("readings") or {})
                       if k and self._spec(k) is None]
            store.put(kept, dev_id, dev_name, by)
            self.gate.log.write("chain", by=by, device=dev_name, chain=kept)
            return "\n".join(["recorded the measurement chain of %s; every run of it from now on carries it:" % whose]
                             + ["  " + line for line in lines(kept)]
                             + (["note: %s %s not in this lab now" % (", ".join(unknown), "is" if len(unknown) == 1
                                                                       else "are")] if unknown else []))
        found = store.get(dev_id, dev_name)
        if found is None:
            return ("no measurement chain for %s yet. Ask the person how it is wired (the excitation, whether its "
                    "amplitude is peak or rms, a series resistor, preamplifier gains) and record it" % whose)
        head = "measurement chain of %s%s (recorded by %s, %s):" % (
            whose, " (the setup's: it has none of its own)" if found["of"] == "setup" and dev_name else "",
            found.get("by") or "?", found.get("at") or "?")
        return "\n".join([head] + ["  " + line for line in lines(found["chain"])])

    def _stamp_chain(self, meta: Dict[str, Any]) -> None:
        """A run carries the measurement chain of its device (or the setup's) as it was when it ran."""
        try:
            from ..chain import Chains
            found = Chains(self.data_dir).get(meta.get("device"), meta.get("device_name"))
        except Exception:
            return
        if found:
            meta["chain"], meta["chain_of"] = found["chain"], found["of"]

    # ------------------------------------------------------------------ the lab's memory (labmemory.py)

    def _lab_memory(self) -> Any:
        """The memory of the lab Kelvin controls: in Kelvin's own folder, everyone's."""
        from ..labmemory import LabMemory, path_for
        return LabMemory(path_for(self.lab_memory_dir or self.data_dir))

    def lab_memory_stamp(self) -> str:
        """Changes when the lab's memory does, or when Kelvin controls another lab: the Lab panel reloads it then."""
        return self._lab_memory().stamp()

    def lab_notes(self) -> List[Dict[str, Any]]:
        return self._lab_memory().notes()

    def named_in(self, text: str) -> List[str]:
        """The lab's instruments a text names (a plan's keys, a request's lines): pump in pump.power, as where() reads
        a request."""
        return [name for name in self.lab.instruments
                if re.search(r"(?<![\w.])%s(?![\w])" % re.escape(name), str(text or ""))]

    def _chain_line(self) -> str:
        """The measurement chain of the mounted device (or the setup's), in one line, for every chat's brief."""
        try:
            from ..chain import Chains, lines
            m = self.mounted or {}
            found = Chains(self.data_dir).get(m.get("device"), m.get("device_name"))
        except Exception:
            return ""
        if not found:
            return ""
        whose = "the setup's" if found["of"] == "setup" else "%s's" % (found.get("device_name") or "the device")
        text = "Measurement chain (%s, recorded by %s, %s): %s" % (whose, found.get("by") or "?",
                                                                  str(found.get("at") or "?")[:10],
                                                                  "; ".join(lines(found["chain"])))
        return text if len(text) <= 500 else text[:497] + "…"

    def lab_memory_brief(self, keep: str = "remember_shared") -> str:
        """The lab's memory as every chat (and every agent's instructions) has it: what to do with it, its notes
        within the brief's budget, the measurement chain on record, and how many notes shared_memory has besides.
        keep: how this client keeps a note ("": it keeps none). "" when the lab's memory is empty and no chain is on
        record."""
        from .. import labmemory as lm
        notes, chain = self.lab_notes(), self._chain_line()
        if not notes and not chain:
            return ""
        shown, left = lm.brief(notes, self.lab.instruments)
        out = [lm.head(keep)] + shown + ([chain] if chain else [])
        if left:
            out.append("%d more note%s: shared_memory looks them up, by instrument or by words." % (
                left, "" if left == 1 else "s"))
        return "\n".join(out)

    def shared_memory(self, query: str = "", instrument: str = "") -> str:
        """shared_memory: the lab's memory, each note with who wrote it and when: all of it, the notes on an instrument,
        or those that bear on the words; with the measurement chains on record."""
        from .. import labmemory as lm
        from ..chain import Chains, lines
        notes = self.lab_notes()
        lab = "this lab (%s)" % self.lab.config.name
        query, instrument = str(query or "").strip(), str(instrument or "").strip()
        if instrument:
            found = lm.for_instruments(notes, [instrument])
            missing = "" if any(i.lower() == instrument.lower() for i in self.lab.instruments) else \
                " (%s is not in %s now)" % (instrument, lab)
            head = "the lab's memory on %s%s: %d note%s" % (instrument, missing, len(found),
                                                             "" if len(found) == 1 else "s")
        elif query:
            found = lm.find(notes, query)
            head = "the lab's memory, notes that bear on %r: %d of %d" % (query, len(found), len(notes))
        else:
            found = lm.ordered(notes, self.lab.instruments)
            head = "the lab's memory, %s: %d note%s, shared by every chat and agent here" % (
                lab, len(found), "" if len(found) == 1 else "s")
        out = [head] + [lm.line(n) for n in found]
        if not notes:
            out.append("Nothing is kept yet. When a person says how the lab is wired, where a reference comes from, "
                       "what excites the sample, or a lesson learned, keep it.")
        if not instrument and not query:
            try:
                kept = Chains(self.data_dir).every()
            except Exception:
                kept = {}
            if kept:
                out.append("Measurement chains on record (measurement_chain reads or records one):")
                for entry in kept.values():
                    whose = entry.get("device_name") or "the setup"
                    out.append("- %s (recorded by %s, %s): %s" % (whose, entry.get("by") or "?",
                                                                 str(entry.get("at") or "?")[:10],
                                                                 "; ".join(lines(entry["chain"]))))
        return "\n".join(out)

    def lab_note_request(self, kind: Any, text: Any, instruments: Any = None,
                         replaces: str = "") -> ApprovalRequest:
        """What a person sees when they are asked to let Kelvin keep a note (it read text from the web): the note, and
        the one it corrects. LabMemoryError or KeyError says what is wrong with it."""
        from .. import labmemory as lm
        kind, text, tied = lm.kind_of(kind), lm.text_of(text), lm.ties(instruments)
        items = ["%s%s: %s" % (kind, " [%s]" % ", ".join(tied) if tied else "", text)]
        if replaces:
            items.append("It corrects: %s" % self._lab_memory().get(str(replaces))["text"])
        return ApprovalRequest("Keep this in the lab's memory, which every chat and everyone here sees?", items)

    def keep_lab_note(self, kind: Any, text: Any, instruments: Any = None, replaces: str = "", by: str = "agent",
                      account: Optional[str] = None) -> Tuple[str, bool]:
        """Keep a note in the lab's memory, or correct one (replaces: its id), and say so in the lab's record. (text,
        failed)."""
        from .. import labmemory as lm
        try:
            note, what = self._lab_memory().add(kind, text, instruments, by=by, account=account,
                                                replaces=str(replaces) if replaces else None)
        except KeyError as e:
            return "not kept: %s" % (e.args[0] if e.args else e), True
        except (lm.LabMemoryError, OSError) as e:
            return "not kept: %s" % e, True
        if what != "known":
            self.gate.log.write("lab_memory", by=by, account=account, change="added" if what == "saved" else "changed",
                                note=note["id"], kind=note["kind"], text=note["text"],
                                instruments=note.get("instruments") or None)
        verb = {"saved": "kept in", "known": "already in", "updated": "corrected in"}[what]
        out = ["%s the lab's memory, which every chat and everyone here sees:" % verb, lm.line(note)]
        here = {i.lower() for i in self.lab.instruments}
        away = [i for i in note.get("instruments") or [] if i.lower() not in here]
        if away:
            out.append("note: %s %s not in this lab now" % (", ".join(away), "is" if len(away) == 1 else "are"))
        if note["kind"] == "excitation":
            out.append("The excitation's form (rms or peak), a series resistor and the preamplifiers' gains also go in "
                       "the measurement chain (measurement_chain), which every run of the device carries.")
        return "\n".join(out), False

    def remember_shared(self, kind: Any, text: Any, instruments: Any = None, replaces: str = "",
                         by: str = "agent", account: Optional[str] = None) -> str:
        """remember_shared (an agent over MCP): keep a note at once; or, when the agent read text from the web in the
        last minutes (which could be steering it into every chat), once a person approves."""
        from ..labmemory import LabMemoryError
        try:
            request = self.lab_note_request(kind, text, instruments, replaces)
        except KeyError as e:
            return "not kept: %s" % (e.args[0] if e.args else e)
        except LabMemoryError as e:
            return "not kept: %s" % e
        outside = self.gate.outside_reason()
        if outside:
            request.items.append("Not kept by Kelvin itself: " + outside)
            if not self.gate.ask(request):   # declined, or nobody answered: the request's note says which
                return "not kept: " + (request.note or "the person declined")
        return self.keep_lab_note(kind, text, instruments, replaces, by, account)[0]

    def change_lab_note(self, note_id: str, by: str, account: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        """A person changes a note in the portal (kind, text, instruments): kept with what it said before, and said in
        the lab's record. KeyError, LabMemoryError."""
        note = self._lab_memory().change(note_id, by, account, **fields)
        self.gate.log.write("lab_memory", by=by, account=account, change="changed", note=note["id"],
                            kind=note["kind"], text=note["text"], instruments=note.get("instruments") or None)
        return note

    def remove_lab_note(self, note_id: str, by: str, account: Optional[str] = None) -> Dict[str, Any]:
        """A person deletes a note in the portal; the lab's record keeps what it said."""
        note = self._lab_memory().remove(note_id)
        self.gate.log.write("lab_memory", by=by, account=account, change="deleted", note=note["id"],
                            kind=note["kind"], text=note["text"], instruments=note.get("instruments") or None)
        return note

    def lab_notes_at_use(self, instruments: Iterable[str], always: bool = False) -> str:
        """The lab's notes on these instruments, for a tool's answer: always (describe_instrument), or the first time
        a chat uses one of them (set_value, send_raw, do_action, run_plan; for agents over MCP, the first time this
        server does), and again once a note's text changes. "" when there are none to show."""
        from .. import labmemory as lm
        names = [str(n) for n in dict.fromkeys(instruments) if n]
        if not names:
            return ""
        try:
            notes = lm.for_instruments(self.lab_notes(), names)
        except Exception:   # the memory never stands in the way of the instruments
            return ""
        if not always:
            told = self._lab_told.setdefault((self.attribution or {}).get("chat"), set())
            notes = [n for n in notes if (n["id"], n["text"]) not in told]
            told.update((n["id"], n["text"]) for n in notes[:lm.AT_USE])
        if not notes:
            return ""
        shown = notes[:lm.AT_USE]
        on = [name for name in names if lm.for_instruments(shown, [name])]   # only the instruments it has notes on
        out = ["", lm.AT_USE_HEAD + "%s:" % ", ".join(on)] + [lm.line(n) for n in shown]
        if len(notes) > lm.AT_USE:
            out.append("(%d more: shared_memory with the instrument)" % (len(notes) - lm.AT_USE))
        return "\n".join(out)

    def lab_lessons_for(self, request: ApprovalRequest) -> List[str]:
        """The lab's lessons on the instruments an approval card names, for the person who answers it."""
        from .. import labmemory as lm
        named = self.named_in(" ".join([request.title] + [str(i) for i in request.items]))
        if not named:
            return []
        try:
            found = lm.for_instruments(self.lab_notes(), named, kinds=("lesson",))
        except Exception:
            return []
        return ["%s (%s, %s)" % (n["text"], lm.who(n.get("by"), n.get("account")), str(n.get("at") or "")[:10])
                for n in found[:lm.ON_CARD]]

    # ------------------------------------------------------------------ for the approval page

    def _unavailable_view(self) -> List[Dict[str, Any]]:
        """The lab's instruments that could not be connected: why, in words, and the install that would fix it."""
        from .. import envs
        return [{"name": n, "why": envs.for_people(why), "install": envs.needed(why)}
                for n, why in self.lab.unavailable.items()]

    def instruments(self) -> List[Dict[str, Any]]:
        """Each instrument for the lab panel: its readings, outputs (drives) and settings, and a state."""
        out = []
        status = dict(self._status)
        for name, inst in self.lab.instruments.items():
            card = inst.card
            if inst.unavailable:   # it could not be connected: why, and the install that would fix it
                from .. import envs
                out.append({"name": name, "model": card.model, "kind": card.kind, "state": "unavailable",
                            "enabled": False, "why": envs.for_people(inst.unavailable),
                            "install": envs.needed(inst.unavailable), "active": 0, "headline": None, "readings": [],
                            "drives": [], "settings": []})
                continue
            if not inst.enabled:
                out.append({"name": name, "model": card.model, "kind": card.kind, "state": "off", "enabled": False,
                            "active": 0, "headline": None, "readings": [], "drives": [], "settings": []})
                continue
            values = status.get(name, {})
            readings, drives, settings = [], [], []
            errors = away = 0
            for pname, value in values.items():
                spec = card.params.get(pname)
                if spec is None:   # read before the lab was loaded again with another card for it
                    continue
                bad = isinstance(value, str) and value.startswith("ERROR")
                errors += bad
                item = {"name": pname, "label": spec.title, "value": "no response" if bad else spec.show(value),
                        "error": bad}
                if spec.is_drive:
                    off_safe = (not bad) and spec.distance(value) > 0
                    applied = True
                    if spec.applies_when:  # e.g. a voltage only reaches the sample while the output is on
                        switch, gspec = values.get(spec.applies_when), card.params[spec.applies_when]
                        applied = switch is not None and not isinstance(switch, str) and gspec.distance(switch) > 0
                        item["switch"] = gspec.title
                    item["away"] = off_safe and applied
                    item["armed"] = off_safe and not applied
                    item["safe"] = spec.show(spec.safe)
                    away += item["away"]
                    drives.append(item)
                elif spec.set_cmd is None:
                    readings.append(item)
                else:
                    settings.append(item)
            headline = card.headline if card.headline in values else (readings[0]["name"] if readings else None)
            readings.sort(key=lambda r: r["name"] != headline)
            out.append({"name": name, "model": card.model, "kind": card.kind, "enabled": True,
                        "state": "error" if errors else "active" if away else "safe", "active": away,
                        "headline": headline, "readings": readings, "drives": drives, "settings": settings,
                        "in_use": self._status_held.get(name)})   # not read now (a run uses it): its latest values
        return out

    def switch_instrument(self, name: str, on: bool, by: str) -> None:
        """A person switches an instrument on or off for this lab (Lab panel). Off: Kelvin sends it nothing at all
        (lab.py Lab.switch). Not while a plan or the queue runs. Raises KeyError, RuntimeError, or AdapterError when
        switching on cannot connect."""
        with self.lock:
            if name not in self.lab.instruments:
                raise KeyError("no instrument %r in %s" % (name, self.lab.config.name))
            live = self.live or {}
            if live.get("status") == "running" or self.queue.state in ("running", "pausing"):
                raise RuntimeError("A plan or the queue is running; stop it first, then switch %s %s."
                                   % (name, "on" if on else "off"))
            inst = self.lab.instruments[name]
            if inst.enabled == on and not inst.unavailable:
                return
            self.lab.switch(name, on)
            self.gate.log.write("instrument_switched", by=by, instrument=name, on=on)
            if on:
                self._caps_if_waiting()
            self._refresh()

    def ask_to_switch_on(self, name: str, why: str) -> str:
        """Kelvin needs an instrument a person switched off: it asks them, and switches it on if they say yes.
        Switching off stays a person's: it says the instrument is not part of the experiment."""
        from ..adapters import AdapterError

        def run() -> str:
            inst = self.lab.instruments.get(name)
            if inst is None:
                return "no instrument %r (have: %s)" % (name, ", ".join(self.lab.instruments))
            if inst.enabled:
                return "%s is on" % name
            if inst.unavailable:
                return ("not asked: nobody switched %s off; it is not connected (%s). Fix that, then "
                        "reconnect_instrument" % (name, inst.unavailable))
            live = self.live or {}
            if live.get("status") == "running" or self.queue.state in ("running", "pausing"):
                return "not asked: a plan or the queue is running; ask when it has finished"
            request = ApprovalRequest("Switch %s on?" % name, [
                "Kelvin needs it: %s" % (why.strip() or "(no reason given)"),
                "You switched it off in the Lab panel. On, Kelvin connects to it again and reads it; output changes "
                "still ask you as usual."])
            if not self.gate.ask(request):
                note = " (%s)" % request.note if request.note else ""
                return "declined: %s stays off%s" % (name, note)
            try:
                self.switch_instrument(name, True, "human (approved Kelvin's request)")
            except (AdapterError, OSError, ValueError, RuntimeError) as e:
                return "%s could not be connected: %s" % (name, str(e).splitlines()[0] if str(e) else e)
            self.lab_changed("switched")
            return "%s is on again (the person approved)" % name
        return self._do("asking to switch %s on" % name, run)

    # ------------------------------------------------------------------ instruments on this computer

    def _remember_scan(self, found: List[Any], looked: Any) -> None:
        from ..adapters import resource_name
        from ..survey import reason
        self.last_survey = dict(looked.as_dict(), reason=reason(found, looked), at=time.time())
        self.last_found = [f.address for f in found]
        self.found_info = {resource_name(f.address): dict(vars(f)) for f in found}

    def scan_found(self) -> List[Dict[str, Any]]:
        """Connected instruments as data, each with a suggested name. Raises AdapterError without VISA.
        What the scan looked through, and why it found what it did, goes to last_survey."""
        from ..discovery import scan_survey
        in_lab = {ic.address: name for name, ic in self.lab.config.instruments.items()
                  if not ic.address.startswith("sim://")}
        with self.lock:
            self.busy = "scanning for instruments"
            try:
                found, looked = scan_survey(self.lab.registry, in_lab=in_lab, network=self.scan_network,
                                            catalog=self.collection)
            finally:
                self.busy = None
        self._remember_scan(found, looked)
        taken = [n for n in self.lab.config.instruments if not self.lab.config.instruments[n].address.startswith("sim://")]
        out = []
        for f in found:
            name = f.suggest_name(taken) if f.card_id and not f.in_lab else None
            if name:
                taken.append(name)
            out.append(dict(vars(f), suggested=name))
        return out

    # ------------------------------------------------------------------ instrument folders and manuals

    def _real(self, instrument: str) -> Any:
        inst = self.lab.instruments.get(instrument)
        if inst is None:
            raise KeyError("no instrument %r (have: %s)" % (instrument, ", ".join(self.lab.instruments)))
        return inst

    def instrument_info(self, instrument: str) -> str:
        """An instrument's folder: who it is, how Kelvin talks to it, its settings at setup and its manual."""
        from .. import profiles
        try:
            inst = self._real(instrument)
        except KeyError as e:
            return e.args[0]
        if inst.simulated:
            return ("%s is simulated (%s): it has no folder or manual; describe_instrument lists its parameters"
                    % (instrument, inst.card.model))
        if self.home is None:
            return "instrument folders are not kept in this Kelvin"
        p = profiles.info(self.home, instrument)
        if not p.get("card"):
            return ("%s has no folder yet; Kelvin makes it when it connects (a person can press Set up again in Lab "
                    "setup)" % instrument)
        lines = ["%s: %s (%s), card %s" % (instrument, p.get("model"), p.get("kind"), p.get("card"))]
        parsed = p.get("parsed") or {}
        if p.get("identity"):
            lines.append("identity: %s%s" % (p["identity"], " (%s)" % ", ".join("%s %s" % kv for kv in parsed.items())
                                             if parsed else ""))
        conn = ", ".join("%s %r" % kv for kv in (p.get("connection") or {}).items())
        lines.append("address: %s%s%s" % (p.get("address"), "; connection: " + conn if conn else "",
                                          "; answered with " + p["answered_with"] if p.get("answered_with") else ""))
        lines.append("folder: %s" % p["folder"])
        cal = profiles.calibration_view(self.home, instrument)   # as a person recorded it (profiles.py)
        lines.append("calibration: %s%s" % (cal["state"]["words"], "; " + cal["state"]["serial"]
                                             if cal["state"].get("serial") else ""))
        m = p.get("manual") or {}
        if p.get("has_manual"):
            lines.append("manual: %s, %s pages%s; search it with search_manual" % (
                m.get("title") or p.get("card_manual") or "manual", m.get("pages", "?"),
                " (from %s)" % m["url"] if m.get("url") else " (put there by hand)"))
        else:
            lines.append("manual: none yet (%s). A person can fetch it in Lab setup or put the PDF in the folder as "
                         "manual.pdf" % (p.get("card_manual") or "the card names no manual"))
        st = profiles.settings_of(self.home, instrument)
        if st.get("settings"):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.get("read_at") or 0))
            vals = ", ".join("%s = %s" % (k, v.get("shown", v.get("value"))) for k, v in st["settings"].items())
            lines.append("settings read at %s (get_value for the current value): %s" % (when, vals))
        return "\n".join(lines)

    def lab_config(self, instrument: str = "") -> str:
        """The lab config file Kelvin loaded (lab.yaml) as the lab wrote it, or one instrument's entry; and where the
        file now differs from what Kelvin uses."""
        from ..discovery import instrument_text
        path = Path(self.lab.config.source or ".")
        if path.suffix.lower() not in (".yaml", ".yml") or not path.is_file():
            return ("%s was not loaded from a lab config file; describe_instrument shows each instrument's limits and "
                    "where they come from" % self.lab.config.name)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return "could not read the lab config %s: %s" % (path, e)
        if instrument:
            try:
                text = instrument_text(text, instrument)
            except KeyError:
                return "the lab config %s has no instrument %r (it has: %s)" % (
                    path, instrument, ", ".join(self.lab.config.instruments) or "none")
        cut = len(text) > MAX_CONFIG_CHARS
        lines = ["lab config %s%s, as written:" % (path, ", %s's entry" % instrument if instrument else ""),
                 "```yaml", text[:MAX_CONFIG_CHARS].rstrip(), "```"]
        if cut:
            lines.append("(the rest of the file is not shown: name an instrument for its whole entry)")
        lines += self._config_drift(path)
        lines.append("A parameter with no limits: here keeps its card's range (the instrument's own); limits: here only "
                     "narrow it. describe_instrument shows the limits in use and where each comes from.")
        return "\n".join(lines)

    def _config_drift(self, path: Path) -> List[str]:
        """Where the lab config file now differs from the one Kelvin loaded: its instruments and their limits."""
        try:
            now = load_lab_config(path, self.lab.registry)
        except ConfigError as e:
            return ["The file has errors now (%s), so Kelvin keeps using what it loaded before; a person fixes them in "
                    "Lab → Set up…" % "; ".join(e.errors[:3])]
        except Exception as e:
            return ["Kelvin can't read the file as a lab config now (%s), so it keeps using what it loaded before" % e]
        loaded, out = self.lab.config.instruments, []
        for name in sorted(set(loaded) | set(now.instruments)):
            was, new = loaded.get(name), now.instruments.get(name)
            if was is None:
                out.append("- %s is in the file but not in the lab Kelvin loaded" % name)
            elif new is None:
                out.append("- %s is in the lab Kelvin loaded but no longer in the file" % name)
            elif dict(was.limits) != dict(new.limits):
                out.append("- %s's limits: %s in the file, %s in use" % (name, _limits_text(new.limits),
                                                                        _limits_text(was.limits)))
        if out:
            out.insert(0, "The file differs from what Kelvin uses until the lab is loaded again (Save and connect, "
                          "in Lab → Set up…):")
        return out

    def search_manual(self, instrument: str, query: str, max_passages: int = 4) -> str:
        from .. import profiles
        try:
            inst = self._real(instrument)
        except KeyError as e:
            return e.args[0]
        if inst.simulated or self.home is None:
            return "%s is simulated: it has no manual; describe_instrument lists its parameters" % instrument
        try:
            hits = profiles.search_manual(self.home, instrument, query, max(1, min(int(max_passages), 8)))
        except FileNotFoundError:
            return ("no manual for %s yet: a person can fetch it in Lab setup, or put the maker's PDF in %s as "
                    "manual.pdf" % (instrument, profiles.folder(self.home, instrument)))
        except (ValueError, RuntimeError) as e:
            return str(e)
        if not hits:
            return "nothing in the %s manual matches %r; try other words (the manual's own terms)" % (instrument, query)
        return "\n\n".join("[%s manual, p. %d]\n%s" % (instrument, h["page"], h["text"]) for h in hits)

    # ------------------------------------------------------------------ pulse sequences (spin experiments)

    def pulse_sequence(self, protocol: str, times: Dict[str, Any], generator: str = "", sequence: str = "") -> str:
        """The timing a pulse generator would play, checked: period, duty cycles, every edge and any problem.
        Missing times come from the generator's current settings (if named), else from pulses.DEFAULTS. sequence:
        a written sequence (its text, or the name it was saved under) in place of the protocol."""
        text, _ = self.pulse_view(protocol, times, generator, sequence)
        return text

    def pulse_view(self, protocol: str, times: Dict[str, Any], generator: str = "",
                   sequence: str = "") -> Tuple[str, Optional[bytes]]:
        """pulse_sequence's text, and the timing drawn (PNG; None when there is nothing to draw)."""
        current: Dict[str, Any] = {}
        inst = self.lab.instruments.get(generator) if generator else None
        if generator and inst is None:
            return "no instrument %r (have: %s)" % (generator, ", ".join(self.lab.instruments)), None
        why = None
        if inst is not None:
            with self._reading(generator) as why:   # beside whatever else Kelvin does; a run's generator isn't asked
                kept = self._kept(generator) if why else {}
                for k in ("t_pi", "tau", "t_laser", "t_gate", "t_wait"):
                    if k not in inst.card.params:
                        continue
                    if why:
                        if k in kept:
                            current[k] = kept[k][0]
                        continue
                    try:
                        current[k] = inst.read(k)
                    except Exception:
                        pass
        given = {k: v for k, v in (times or {}).items() if v is not None}
        written = str(sequence or "").strip()
        try:
            if written and ("\n" in written or ":" in written):   # its text: checked as written
                seq = pulses.parse(written).build(dict(current, **given), pulses.delays_of(inst))
            elif written:
                seq = pulses.program(written.lower()).build(dict(current, **given), pulses.delays_of(inst))
            else:
                seq = pulses.build(protocol, dict(current, **given), pulses.delays_of(inst) if inst else None)
        except pulses.SequenceError as e:
            return "the sequence is not right yet:\n" + "\n".join("- " + p for p in e.errors), None
        except (KeyError, ValueError, TypeError) as e:
            return str(e.args[0] if e.args else e), None
        problems = pulses.check(seq, pulses.limits_of(inst) if inst is not None else None)
        if inst is not None and written:
            wired = getattr(inst.backend, "wiring", None)
            unwired = sorted(ch for ch, spans in seq.channels.items() if spans and isinstance(wired, dict)
                             and ch not in wired)
            if unwired:
                problems.append("%s has no output wired to %s (its card's wiring vars)" % (generator,
                                                                                       ", ".join(unwired)))
        text = pulses.describe(seq) + ("\nproblems: " + "; ".join(problems) if problems else "\nchecks: ok") + (
            "\n%s's times are its latest Kelvin has, not read now: %s" % (generator, why) if why else "")
        try:
            picture = pulses.png(seq)
        except Exception:   # the words say it all; a picture is a convenience
            picture = None
        return text, picture

    def save_sequence(self, text: str, by: str = "agent") -> str:
        """Keep a written sequence in the lab's sequences folder, checked as written, so a generator plays it by name
        (its `sequence` setting). Drives nothing: playing it is the generator's running, which a person approves
        with the timing drawn on the card. A sequence a generator is playing now is not changed under it."""
        try:
            prog = pulses.parse(text)
        except pulses.SequenceError as e:
            return "not saved; the sequence is not right yet:\n" + "\n".join("- " + p for p in e.errors)
        for name, inst in self.lab.instruments.items():
            if not pulses.is_generator(inst) or "sequence" not in inst.card.params or prog.name not in pulses.LIBRARY:
                continue
            with self._reading(name) as why:   # a generator a run uses isn't asked: its latest values (unknown: playing)
                kept = self._kept(name) if why else {}
                try:
                    running = kept.get("running", (True,))[0] if why else inst.read("running")
                    current = kept.get("sequence", (prog.name,))[0] if why else inst.read("sequence")
                except Exception:
                    running, current = False, None
            if running and str(current).strip().lower() == prog.name:
                return ("not saved: %s is playing %s now%s; stop it first, or save this under another name"
                        % (name, prog.name, " (as far as Kelvin can tell: %s)" % why if why else ""))
        folder = self.data_dir / "sequences"
        replaced = (folder / (prog.name + ".yaml")).exists()
        try:
            pulses.save(folder, text)
        except (OSError, pulses.SequenceError) as e:
            return "not saved: %s" % e
        self.gate.log.write("sequence_saved", by=by, name=prog.name, channels=prog.channels, replaced=replaced)
        try:
            seq = prog.build({})
            at = "at the default times: " + pulses.summary(seq)
            problems = pulses.check(seq)
        except ValueError as e:
            at, problems = "at the default times it can't be built: %s" % e, []
        return ("saved %s%s (channels %s); a generator plays it with its sequence set to %s, and running on (a person "
                "approves that, with its timing drawn on the card)\n%s%s" % (
                    prog.name, ", replacing the one saved before" if replaced else "", ", ".join(prog.channels),
                    prog.name, at, "\nat those times: " + "; ".join(problems) if problems else ""))

    # ------------------------------------------------------------------ cards for new instruments

    def find_card(self, query: str = "") -> str:
        """The cards Kelvin has (its own, the instrument collection's, the lab's) for a maker and model, a kind, an
        identity reply or a driver address, and the instruments the collection knows that have no card yet, with
        their manuals: what to look up before writing a card. Only reads Kelvin's files."""
        from ..collection import manuals
        from ..discovery import find_cards
        from ..homelab import builtin_ids, lab_registry
        registry = lab_registry(self.home, self.collection) if self.home is not None else self.lab.registry
        found = find_cards(query, registry, self.collection)
        mine = str(self.cards_dir).replace("\\", "/") if self.cards_dir is not None else None

        def origin(card: Any) -> str:
            source = str(card.source).replace("\\", "/")
            if mine and source.startswith(mine):
                return "this lab's version of Kelvin's card" if card.id in builtin_ids() else "this lab's card"
            return "the instrument collection's" if "/collection/" in source else "Kelvin's"
        lines = []
        if found["cards"]:
            lines.append("Cards Kelvin has%s:" % (" for %r" % query if query else ""))
            lines += ["- %s: %s (%s; %s, %s)%s" % (c.id, c.model, c.kind, origin(c), c.status,
                                                    "; " + c.notes[0][:160] if c.notes and query else "")
                      for c in found["cards"]]
            lines.append("Use one as it is: add_instrument with card=<id> (try_card first on an instrument it may not "
                         "fit). Write a card only for an instrument none of these fits.")
        for e in found["entries"]:
            if not lines or not lines[-1].startswith("No card yet"):
                lines.append("No card yet, but the instrument collection knows:")
            links = manuals(e)
            lines.append("- %s: %s (%s)%s" % (e.get("id"), e.get("name") or e.get("model"), e.get("kind") or "?",
                                              "; manual " + links[0] if links else ""))
        if found["entries"]:
            lines.append("draft_card writes its card from the manual the collection links (or draft_card with manual).")
        if not lines:
            lines.append("Kelvin has no card for %r%s; draft_card finds the maker's manual and writes one." % (
                query, "" if self.collection is not None and self.collection.index() else
                " (and the instrument collection is %s)" % ("switched off" if self.collection is None else
                                                             "not fetched yet")))
        return "\n".join(lines)

    def check_card(self, text: str) -> str:
        card, problems, warnings = carddraft.check(text)
        if card is None:
            return "not valid:\n" + "\n".join("- " + p for p in problems)
        drives = [p.name for p in card.params.values() if p.is_drive]
        lines = ["valid: %s (%s), %d parameters; drives: %s" % (card.id, card.model, len(card.params),
                                                              ", ".join(drives) or "none")]
        return "\n".join(lines + ["warning: " + w for w in warnings])

    def read_examples(self, path: str) -> str:
        """A maker's files, read for a card: never in Kelvin's own folder (the lab config, the settings, the accounts,
        the cards, everyone's chats), nor a file that may hold sign-ins, keys or tokens (carddraft.read_sources)."""
        try:
            return carddraft.read_sources(path, own=self._kelvins())
        except (OSError, ValueError) as e:
            return "not read: %s" % e

    def _kelvins(self) -> List[Path]:
        """Kelvin's own folder (~/Kelvin: the lab config, the settings, the accounts, the cards, everyone's chats),
        which the tools that read a folder a person or the model names never reach."""
        return [self.home] if self.home is not None else []

    def script_cards(self) -> Any:
        """Every card Kelvin can use here, to match the lab's scripts against: Kelvin's own and the lab's. Kept
        while the lab's cards folder stays as it was, as reading every card takes a second. Only read from."""
        from ..homelab import lab_registry
        if self.home is None:
            return self.lab.registry
        folder = Path(self.home) / "cards"
        try:
            files = sorted(folder.iterdir()) if folder.is_dir() else []
            stamp = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
        except OSError:
            stamp = None
        if stamp is None or self._script_cards is None or self._script_cards[0] != stamp:
            self._script_cards = (stamp, lab_registry(self.home))
        return self._script_cards[1]

    def survey_scripts(self, path: str) -> str:
        """The lab's own scripts, read for how the lab works now (scripts.py): each instrument's address, settings,
        commands and the values used, the card that fits, and each script's outline. Nothing in them is run."""
        from .. import scripts as lab_scripts
        from ..adapters import resource_name
        try:
            found = lab_scripts.survey(path, self.script_cards(), own=self._kelvins())
        except (OSError, ValueError) as e:
            return "not read: %s" % e
        in_lab = {resource_name(ic.address): name for name, ic in self.lab.config.instruments.items()}
        here = ["%s (%s)" % (i.address, in_lab[resource_name(i.address)]) for i in found.instruments
                if i.address and resource_name(i.address) in in_lab]
        return found.text() + ("\n\nAlready in this lab: " + ", ".join(here) if here else "") + "\n\n" + SCRIPTS_NEXT

    def scripts_found(self, folder: str) -> Dict[str, Any]:
        """The lab's scripts read for the Set up panel (a person chose the folder): each instrument with its likely
        card, the limits from the values the scripts use, and who answers at its address now, asked as a scan asks
        (*IDN? only; a serial port isn't tried, as its framing takes long to find). An identity reply decides the
        card. Raises OSError for a folder that isn't there."""
        from .. import scripts as lab_scripts
        from ..adapters import resource_name
        from ..discovery import SHORT_NAMES, identify
        registry = self.script_cards()
        found = lab_scripts.survey(folder, registry)
        view = found.view()
        in_lab = {resource_name(ic.address): name for name, ic in self.lab.config.instruments.items()}
        taken = [n for n, ic in self.lab.config.instruments.items() if not ic.address.startswith("sim://")]
        with self.lock:
            self.busy = "asking the scripts' instruments who they are"
            try:
                for item, inst in zip(view["instruments"], found.instruments):
                    item["in_lab"] = in_lab.get(resource_name(inst.address)) if inst.address else None
                    item["idn"] = ""
                    if item["in_lab"] in self.lab.instruments:   # its card here: the limits the scripts' values give
                        card = self.lab.instruments[item["in_lab"]].card
                        item["card"] = {"id": card.id, "model": card.model, "kind": card.kind, "sure": True,
                                        "why": "in this lab as %s" % item["in_lab"],
                                        "mapped": lab_scripts.mapping(card, inst.commands),
                                        "limits": lab_scripts.limits(card, inst.commands)}
                    if not inst.address or item["in_lab"]:
                        continue
                    if inst.address.startswith("serial://"):
                        item["asked"] = "not asked: a serial port (Scan tries each card's framing on it)"
                        continue
                    queries = ["*IDN?"]   # and the likely card's own identity query
                    likely = registry.get(item["card"]["id"]) if item["card"] else None
                    if likely is not None and likely.idn_query not in queries:
                        queries.append(likely.idn_query)
                    try:
                        answer = identify(inst.address, registry, timeout_ms=800, queries=queries)
                    except Exception as e:   # no VISA on this computer
                        why = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
                        item["asked"] = "not asked: %s" % why
                        continue
                    item["idn"] = answer.idn
                    if not answer.idn:
                        item["asked"] = "no answer at its address now"
                    if answer.card_id and (not item["card"] or item["card"]["id"] != answer.card_id):
                        card = registry.get(answer.card_id)
                        item["card"] = {"id": card.id, "model": card.model, "kind": card.kind, "sure": True,
                                        "why": "it answered %s" % answer.idn[:60],
                                        "mapped": lab_scripts.mapping(card, inst.commands),
                                        "limits": lab_scripts.limits(card, inst.commands)}
                    elif answer.card_id:
                        item["card"]["why"] += "; it answered %s" % answer.idn[:60]
                        item["card"]["sure"] = True
            finally:
                self.busy = None
        for item in view["instruments"]:   # a name for each, from the scripts or the kind of instrument
            if item["in_lab"]:
                continue
            kind = (item["card"] or {}).get("kind", "")
            base = item["name"] or SHORT_NAMES.get(kind, re.sub(r"[^a-z0-9]+", "", kind.lower()) or "instr")
            name, i = base, 2
            while name in taken:
                name, i = "%s%d" % (base, i), i + 1
            taken.append(name)
            item["name"] = name
        return view

    # ------------------------------------------------------------------ the lab's own scripts, run (scriptrun.py)

    def _prepare_script(self, path: str, params: Optional[Dict[str, Any]] = None, args: Optional[List[Any]] = None,
                        drives: Optional[Dict[str, Any]] = None) -> Any:
        """A script from the lab's scripts folder, read and ready (nothing is run, no instrument is asked).
        scriptrun.ScriptError: why it can't run."""
        from .. import scriptrun
        folder = self.scripts_folder()
        return scriptrun.prepare(self.lab, path, [folder] if folder else [], params=params, args=args, drives=drives,
                                 python=self.scripts_python() or "")

    def check_script(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        """What running one of the lab's scripts would do, without running it: the Python that runs it, the settings
        it takes, and the one approval a person would be asked (instruments, drive ranges, what Kelvin can't
        check)."""
        from .. import scriptrun
        try:
            prep = self._prepare_script(path, params)
        except scriptrun.ScriptError as e:
            return "can't run it: %s" % e

        def run() -> str:
            request, _, problems = scriptrun.approval(self.gate, prep)
            settings = scriptrun.parameters(prep.original)
            lines = ["%s in %s" % (prep.name, prep.path.parent)]
            if settings:
                lines.append("settings it takes (params): " + "; ".join(p.text() for p in settings))
            lines += ["will be refused now: " + p for p in problems]
            lines.append("run_script asks a person once:")
            lines += ["  " + i for i in request.items]
            return "\n".join(lines)
        return self._do("reading script %s" % prep.name, run)

    def run_script(self, path: str, params: Optional[Dict[str, Any]] = None, args: Optional[List[Any]] = None,
                   drives: Optional[Dict[str, Any]] = None, mark: Callable[[str], str] = lambda text: text) -> str:
        """Run one of the lab's scripts as it is, in the lab's Python, with every instrument command through the
        gate (scriptrun.py): one approval by a person, then its commands; a stopped or failed run sends what it drove
        back to safe. mark: how the script's own output is marked for the model (text from outside the lab)."""
        from .. import scriptrun
        try:
            prep = self._prepare_script(path, params, args, drives)
        except scriptrun.ScriptError as e:
            return "script not run: %s" % e
        return self._do("running script %s" % prep.name, lambda: self._run_script(prep, mark))

    def _run_script(self, prep: Any, mark: Callable[[str], str]) -> str:
        from .. import scriptrun
        gate, lab = self.gate, self.lab
        names = list(prep.found.instruments)
        for name in names:
            away = lab.away(name)
            if away:
                return "script not run: %s is %s" % (name, away)
        request, grants, problems = scriptrun.approval(gate, prep)
        if problems:
            gate.log.write("run_refused", by=gate.operator, plan=prep.name, reason="; ".join(problems))
            return "script not run: refused: " + "; ".join(problems)
        bad = [r for r in self.engine.preflight(names, fresh=bool(grants)) if not r.ok] if names else []
        if bad:
            return "script not run: preflight failed: " + "; ".join(str(r) for r in bad)
        if gate.auto:
            request.items.append("Auto mode does not cover this run: a script can't be dry-run first")
        if not gate.ask(request):
            gate.log.write("run_declined", by=gate.operator, plan=prep.name)
            return "declined: script %s not run%s" % (prep.name, " (%s)" % request.note if request.note else "")
        meta = self._attribute(None)
        apart = meta.pop("_apart", "")

        def busy(text: str) -> None:
            self.busy = text
        with self._device_protection(meta) as held:
            report = scriptrun.ScriptRun(gate, prep, self.engine.store, grants,
                                         set(prep.found.unknown) | set(prep.found.libraries), meta,
                                         self.attribution.get("folder"), busy).execute()
        for name in names if report.status != "done" else []:
            self.engine._healthy.pop(name, None)   # checked again before the next run on them
        made = self._auto_report(report.run_id)
        self.tell("run", ("Run finished: " if report.status == "done" else "Run stopped: ") + prep.name,
                  report.message[:300], self.unshared_work(meta))
        lines = list(held) + [report.message]
        if apart and report.run_id:
            lines.append("filed under %s without the mounted sample: %s" % (meta.get("project_name") or "General",
                                                                           apart))
        if report.analysis:
            lines += self._analysis_lines(report.analysis)
        elif report.rows:
            cols = report.columns
            lines.append("its readings: %d rows of %s%s" % (len(report.rows), ", ".join(
                m["key"] for m in cols["measures"]), " against " + ", ".join(a["key"] for a in cols["axes"])
                if cols["axes"] else ""))
        if report.left:
            lines.append("left away from safe, as the script left them: " + ", ".join(report.left))
        lines += ["warning: " + w for w in report.warnings]
        if report.files:
            lines.append("files it wrote beside the script: %s%s" % (", ".join(report.files[:10]),
                                                                     " …" if len(report.files) > 10 else ""))
        lines.append("saved in %s: script.py (as it ran), output.txt, commands.jsonl%s%s%s" % (
            report.path, ", run.h5 (its readings)" if report.rows else "",
            ", " + ", ".join(report.figures) if report.figures else "",
            " and %s (made automatically)" % " and ".join(made) if made else ""))
        if report.run_id and report.rows:
            lines.append("rows: get_run_data(run_id=%r)" % report.run_id)
        if report.output:
            lines.append("its output (last lines):\n" + mark("\n".join(report.output)))
        return "\n".join(lines)

    def _saved_card(self, ref: str) -> Any:
        """A card Kelvin has, by its id (Kelvin's own, or one in ~/Kelvin/cards); None if there is none."""
        from ..homelab import lab_registry
        for registry in [self.lab.registry] + ([lab_registry(self.home)] if self.home is not None else []):
            try:
                return registry.get(ref)
            except KeyError:
                continue
        return None

    def try_card(self, text: str, instrument: str) -> str:
        """Kelvin's own try: on an instrument in the lab or one the last scan found; anywhere else a person says
        yes first. `text` is the card's YAML, or the id of a card Kelvin has (one draft_card wrote)."""
        from .. import cardtry
        from ..adapters import AdapterError
        ref = text.strip()
        card = self._saved_card(ref) if ref and "\n" not in ref and len(ref) < 120 else None
        if card is None:
            card, problems, _ = carddraft.check(text)
            if card is None:
                return "not valid:\n" + "\n".join("- " + p for p in problems)
        targets = cardtry.addresses_for_agent(self.lab, self.last_found)
        address = targets.get(instrument)
        if address is None and _runs_a_program(instrument):
            return ("Kelvin tries a card only on an instrument in the lab or an address the scan found: a lib:// "
                    "address with python= or path= names a program for Kelvin to run, so a person tries it in Lab, "
                    "Cards; or install the maker's library with install_for_instrument and use "
                    "lib://<address>?env=<environment>")
        if address is None and ("::" in instrument or "://" in instrument):
            request = ApprovalRequest("Try a card on %s?" % instrument, [
                "Kelvin's last scan did not find an instrument at %s. Trying the %s card asks it who it is and reads "
                "its readings; it changes nothing (one setting is written with the value it already has, to check "
                "the card's set command)." % (instrument, card.model)])
            if not self.gate.auto_simple(request, "trying a card asks the instrument who it is and reads it, and "
                                                  "changes nothing") \
                    and not self.gate.ask(request):
                note = " (%s)" % request.note if request.note else ""
                return "declined: the person did not want Kelvin to try %s%s" % (instrument, note)
            address = instrument
        if address is None:
            return ("Kelvin tries a card on an instrument in the lab or an address (the last scan found: %s)"
                    % (", ".join(sorted(targets)) or "nothing yet: scan first"))

        def run() -> str:
            try:
                result = cardtry.try_card(card, address, self.lab)
            except (AdapterError, OSError, ValueError) as e:
                return "could not try it: %s" % e
            self.gate.log.write("card_try", by="agent", card=card.id, address=address,
                                answered=sum(1 for r in result["readings"] if "value" in r))
            return cardtry.summary(result)
        return self._do("trying card %s" % card.id, run)

    def save_card(self, text: str, by: str = "agent", version: bool = False) -> str:
        """version: the card takes the id of one of Kelvin's cards (its own or the collection's) on purpose, as this
        lab's version of it; without it such a card is refused, so a draft never silently replaces a card Kelvin has
        (l07's Mercury draft, which lacked the card's set_reply, broke a run)."""
        if self.cards_dir is None:
            return "not saved: this Kelvin has no folder for your cards"
        from ..homelab import builtin_ids, kelvin_ids
        try:
            card, path, warnings = carddraft.save(self.cards_dir, text, kelvin_ids(self.collection), by,
                                                  version=version)
        except ValueError as e:
            return "not saved: %s" % e
        self.refresh_cards([card.id])
        self.gate.log.write("card", by=by, card=card.id, status=card.status, file=path.name)
        self.config_changed(by, "saved card %s" % card.id, "card")
        users = sorted(n for n, i in self.lab.instruments.items() if i.card.id == card.id)
        return "\n".join(["saved %s card %s as %s%s. try_card it on the instrument, then add_instrument%s. Until a "
                          "person marks it reviewed (Lab, Cards…), every setting and output change on it asks them."
                          % (card.status, card.id, path, ", this lab's version of Kelvin's card" if card.id in
                             builtin_ids() else "", " (or reconnect_instrument %s)" % ", ".join(users) if users else "")]
                         + ["warning: " + w for w in warnings])

    def draft_card(self, address: str, maker: str = "", model: str = "", manual: str = "", specs: str = "") -> str:
        """A card for an instrument Kelvin has none for: Kelvin finds the maker's manual online (or reads the one the
        person gave: `manual`, a file on this computer or its web address), writes the card from its command reference
        with the app's model, checks it and keeps it as a draft (autocard.py). Where the manual leaves some ranges to
        another document, Kelvin looks for it too (or reads `specs`, which the person gave). The maker and model come
        from the instrument's identity reply (the last scan), else from what the person said. Only they leave the
        computer, never a serial number. Sends the instrument nothing."""
        from ..adapters import resource_name
        if self.card_writer is None:
            return ("Kelvin writes cards from manuals in the Kelvin app. Here, write it yourself: the person gives the "
                    "manual's text, then card_guide, check_card, try_card and save_card")
        address = (address or "").strip()
        found = self.found_info.get(resource_name(address), {}) if address else {}
        idn = str(found.get("idn") or "")
        if found.get("card_id"):
            return "%s already matches card %s (%s): add_instrument" % (address, found["card_id"], found.get("model"))
        from ..discovery import cards_named, match_card
        from ..homelab import lab_registry
        registry = lab_registry(self.home, self.collection) if self.home is not None else self.lab.registry
        have = [c for c in [match_card(idn, registry) if idn else None] if c is not None]
        if not have and not idn and maker.strip() and model.strip():
            have = cards_named("%s %s" % (maker, model), registry)
        if have:   # Kelvin's own, the collection's or the lab's: a draft would only be a worse copy of it
            return ("Kelvin already has %s for it: %s. Use it (add_instrument with card=<id>; try_card first if "
                    "unsure); draft_card writes cards only for instruments Kelvin has none for" % (
                        "a card" if len(have) == 1 else "cards",
                        "; ".join("%s (%s)" % (c.id, c.model) for c in have[:4])))
        if not idn and not (maker.strip() and model.strip()):
            return ("%s did not answer who it is%s: give the maker and the model the person named"
                    % (address or "the instrument", "" if found else " (or the last scan did not find it)"))
        try:
            r = self.card_writer({"address": address, "idn": idn, "maker": maker.strip(), "model": model.strip(),
                                  "manual": (manual or "").strip(), "specs": (specs or "").strip(),
                                  "serial": found.get("serial") or {}})
        except Exception as e:   # the model or the maker's site failed in a way autocard did not catch
            r = {"error": str(e).splitlines()[0] if str(e) else type(e).__name__}
        if r.get("error"):
            hint = "" if manual or "manual" not in r["error"] else (
                " (ask the person for the manual's PDF or web address, then draft_card with manual)")
            return "no card yet: %s%s" % (r["error"], hint)
        if r.get("manual") and address:
            self.manuals_found[resource_name(address)] = r["manual"]
        self.gate.log.write("card", by="agent", card=r["card_id"], status="draft", manual=(r.get("manual") or {}).get("url"))
        lines = ["drafted card %s (%s, %s) from %s%s%s." % (
            r["card_id"], r["model"], r["kind"], (r.get("manual") or {}).get("url") or "its manual",
            " with ranges from %s" % r["specs"] if r.get("specs") else "",
            "; " + "; ".join(r["changed"]) if r.get("changed") else "")]
        if r.get("parameters"):
            lines.append("parameters: " + r["parameters"])
        if r.get("notes"):   # what the model left out or is unsure of: the person should hear it
            lines.append("the model's notes: " + " | ".join(r["notes"]))
        if r.get("ranges_in"):
            lines.append("Some outputs stay readings: their ranges are in %r, which Kelvin could not get. Ask the person "
                         "for its PDF or web address, then draft_card again with specs." % r["ranges_in"])
        lines.append("Next: try_card %s on %s and compare the readings with what the person sees; then "
                     "add_instrument." % (r["card_id"], address or "the instrument"))
        return "\n".join(lines)

    # ------------------------------------------------------------------ the web: pages and manuals

    def _get(self, url: str) -> bytes:
        from .. import profiles
        return (self.fetch_url or profiles.http_get)(url)

    def read_web_page(self, url: str, start: int = 0, max_chars: int = 12000) -> str:
        """A web page or a document on the web as text, for reading a maker's pages, a manual or documentation: from
        `start`, up to `max_chars` characters, and for a page the links on it, documents first. Nothing is kept."""
        from .. import profiles
        from ..autocard import page_links
        from ..literature import html_to_text
        url = (url or "").strip()
        if not self.literature.enabled:
            return "searching outside the lab (literature and web) is switched off in Settings"
        if not re.match(r"^https?://", url):
            return "give a web address (http:// or https://)"
        try:
            data = self._get(url)
        except Exception as e:
            return "could not read %s: %s" % (url, profiles._first_line(e))
        links: List[Tuple[str, str]] = []
        if data[:5] == b"%PDF-":
            try:
                pages = profiles.pdf_pages(data)
            except Exception as e:
                return "%s is a PDF Kelvin could not read: %s" % (url, profiles._first_line(e))
            text, kind = "\n\n".join("[page %d]\n%s" % (i + 1, p.strip()) for i, p in enumerate(pages)), \
                "a PDF of %d pages" % len(pages)
        else:
            page = data.decode("utf-8", errors="replace")
            text, kind, links = html_to_text(page), "a web page", page_links(page, url)
        try:
            start, max_chars = max(0, int(start)), max(1000, min(40000, int(max_chars)))
        except (TypeError, ValueError):
            start, max_chars = 0, 12000
        lines = ["%s: %s, %d characters%s" % (url, kind, len(text), ", from character %d" % start if start else ""),
                 text[start:start + max_chars].strip() or "(no text)"]
        if start + max_chars < len(text):
            lines.append("[more: read_web_page with start=%d]" % (start + max_chars))
        if links:
            lines.append("Links (documents first):\n" + "\n".join("- %s: %s" % (label or "(link)", u)
                                                                  for label, u in links[:40]))
        return "\n".join(lines)

    def save_manual(self, source: str, maker: str = "", model: str = "", address: str = "",
                    instrument: str = "") -> str:
        """Keep an instrument's manual: download it from its web address (or take a file on this computer), check that
        it names the model, and keep it in ~/Kelvin/manuals. For an instrument in the lab it goes into its folder
        (search_manual reads it); for one not added yet (its address), add_instrument puts it there, and draft_card
        writes the card from it (manual=<the kept file>)."""
        from .. import autocard, profiles
        from ..adapters import resource_name
        source = (source or "").strip().strip('"')
        if not source:
            return "give the manual's web address, or a file on this computer"
        if not re.match(r"^https?://", source) and Path(source).expanduser().is_dir():   # a folder: its manual
            folder = Path(source).expanduser()
            pdfs = sorted(folder.glob("*.pdf")) or sorted(folder.rglob("*.pdf"))[:40]
            if not pdfs:
                return "there is no PDF in %s: give the manual's file, or its web address" % folder
            if len(pdfs) > 1:
                return "%d PDFs in %s; name the manual's file: %s" % (len(pdfs), folder,
                                                                         ", ".join(p.name for p in pdfs[:20]))
            source = str(pdfs[0])
        if re.match(r"^https?://", source) and not self.literature.enabled:
            return "searching outside the lab (literature and web) is switched off in Settings"
        if instrument and instrument not in self.lab.instruments:
            return "no instrument %r in this lab" % instrument
        try:
            m = autocard.manual_from(source, self._get)
        except Exception as e:
            return "could not get %s: %s" % (source, profiles._first_line(e))
        if m["kind"] not in ("pdf", "html"):
            return "%s is neither a PDF nor a web page" % source
        text = "\n".join(m["pages"])
        name = re.sub(r"[^A-Za-z0-9]+", "-", "%s %s" % (maker, model)).strip("-") or \
            re.sub(r"[^A-Za-z0-9]+", "-", Path(source.split("?")[0]).stem).strip("-") or "manual"
        folder = Path(self.home) / "manuals"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name + (".pdf" if m["kind"] == "pdf" else ".html"))
        path.write_bytes(m["data"])
        what = "a PDF of %d pages" % len(m["pages"]) if m["kind"] == "pdf" else "a web page"
        lines = ["kept %s (%s) as %s" % (m["url"], what, path)]
        if model and not autocard.names_model(text, model):
            lines.append("It does not name the %s: check it is the right manual." % model)
        lines.append("It has a command reference (%d command lines)." % m["score"] if m["score"] >= 15 else
                     "It has little command syntax (%d lines): for a card, look for the programming or remote-control "
                     "manual too." % m["score"])
        url = m["url"] if re.match(r"^https?://", m["url"] or "") else ""
        if instrument:
            profiles.store_manual(self.home, instrument, self.lab.instruments[instrument].card, m["data"], url)
            lines.append("It is now %s's manual (search_manual reads it)." % instrument)
        elif address:
            self.manuals_found[resource_name(address)] = {"url": url, "data": m["data"], "kind": m["kind"]}
            lines.append("It goes into the instrument's folder when add_instrument adds %s." % address)
        if not instrument:
            lines.append("Next: draft_card %s with manual=%s, then try_card and add_instrument."
                         % (address or "<the instrument's address>", path))
        self.gate.log.write("manual_kept", by="agent", source=source, file=str(path), instrument=instrument or None,
                            address=address or None)
        return "\n".join(lines)

    # ------------------------------------------------------------------ what an instrument is (probe.py)

    def inspect_address(self, address: str) -> str:
        """What a network address answers: the ports instruments use, what each says (its *IDN? reply where it
        speaks SCPI, a web page's title, a greeting), the matching card and the address to use. Local networks only;
        an instrument already in the lab keeps its connection (only its web page is looked at). Behind a Prologix
        GPIB-ETHERNET (its port 1234 answers, or prologix://<address>): the instruments on its GPIB bus."""
        from .. import probe, prologix
        from ..discovery import identify, match_card
        if prologix.is_prologix(address):
            try:
                adapter = prologix.adapter_of(address)
            except ValueError as e:
                return str(e)
            if prologix.on_serial(adapter):   # a GPIB-USB's
                return self.behind_prologix(adapter)
        try:
            host, port = probe.host_of(address)
            ip = probe.local_ip(host)
        except ValueError as e:
            return str(e)
        if prologix.is_prologix(address) or port == prologix.PORT:
            return self.behind_prologix("%s:%d" % (ip, port or prologix.PORT))
        in_lab = {name: ic.address for name, ic in self.lab.config.instruments.items()
                  if probe.host_of(ic.address)[0] in (host, ip)}
        ports = [p for p in probe.PORTS if p[1] in ("http", "https")] if in_lab else None
        res = probe.inspect(address, ports)
        lines = ["%s%s: %s" % (host, " (%s)" % ip if ip != host else "",
                               "%d of the ports instruments use answer" % len(res["found"]) if res["found"] else
                               "nothing answers on the ports instruments use")]
        if in_lab:
            lines.append("In this lab already: %s; Kelvin left its connection alone and looked only at its web page."
                         % ", ".join("%s (%s)" % kv for kv in in_lab.items()))
        found_here = []
        for f in res["found"]:
            line = "- " + probe.what_answers(f)
            resource = None
            if f.get("idn"):
                resource = "TCPIP0::%s::%d::SOCKET" % (ip, f["port"])
                card = match_card(f["idn"], self.lab.registry)
                line += (" → card %s (%s): add_instrument %s" % (card.id, card.model, resource) if card else
                         " → no card matches it yet: draft_card %s" % resource)
            elif f["port"] in (111, 4880) and not in_lab:   # VXI-11 and HiSLIP speak through VISA
                candidate = "TCPIP0::%s::%s::INSTR" % (ip, "inst0" if f["port"] == 111 else "hislip0")
                try:
                    got = identify(candidate, self.lab.registry, timeout_ms=1500)
                except Exception:
                    got = None
                if got is not None and got.idn:
                    resource = candidate
                    line += "; answers %r there%s" % (got.idn[:100], " → card %s: add_instrument %s" % (
                        got.card_id, candidate) if got.card_id else " → no card matches it yet: draft_card %s"
                        % candidate)
            if resource:
                found_here.append(resource)
            lines.append(line)
        if res["lxi"]:
            lines.append("Its LXI identification: " + ", ".join("%s %s" % kv for kv in res["lxi"].items()))
        quiet = [str(p) for p in res["looked"] if p not in {f["port"] for f in res["found"]}]
        if quiet:
            lines.append("Closed or silent: " + ", ".join(quiet))
        if not res["found"]:
            lines.append("Check the address (the instrument's front panel or its maker's program shows it), that it "
                         "is switched on, and that this computer is on its network (connected_devices lists this "
                         "computer's networks). An instrument on a port of its own: inspect_address <address>:<port>.")
        if any(f["port"] == prologix.PORT for f in res["found"]):   # a GPIB-ETHERNET: who is on its GPIB bus
            lines.append(self.behind_prologix("%s:%d" % (ip, prologix.PORT)))
        for resource in found_here:
            if resource not in self.last_found:
                self.last_found.append(resource)   # found here: try_card and add_instrument may use it
        return "\n".join(lines)

    def find_on_network(self, network: str = "") -> str:
        """Instruments on the local network: those that announce themselves (mDNS, as LXI instruments do, and a
        VXI-11 broadcast), and every address of this computer's networks (or the local range named) with a port open
        that instruments or their gateways use. Each is asked who it is and matched to a card, and what answered is
        kept for try_card and add_instrument. Only looks: one connection per port, *IDN? only where instruments
        speak SCPI; an instrument already in the lab is left alone."""
        import concurrent.futures
        import ipaddress
        from .. import probe, prologix
        from ..adapters import AdapterError, resource_name
        from ..discovery import Found, identify, look_up, match_card, scan_prologix
        from ..survey import network_adverts
        adapters = probe.network_adapters()
        try:
            nets, notes = probe.sweep_networks(network, adapters)
        except ValueError as e:
            return str(e)
        in_lab: Dict[str, str] = {}   # the lab's instruments on the network: host -> name
        for name, ic in self.lab.config.instruments.items():
            address = ic.address.strip()
            if address.upper().startswith("TCPIP") or ("://" in address and not address.startswith(
                    ("sim://", "serial://", "lib://", "uvc://", "genicam://"))):
                host = probe.host_of(address)[0]
                if "." in host or ":" in host:   # an address or a host name, not a VISA board number
                    in_lab.setdefault(host, name)
        found: Dict[str, List[Found]] = {}   # host -> the instruments that said who they are there

        def keep(host: str, got: Found) -> None:
            found.setdefault(host, []).append(got)
            self.found_info[resource_name(got.address)] = {"address": got.address, "idn": got.idn,
                                                           "card_id": got.card_id, "model": got.model, "kind": got.kind}
            if got.address not in self.last_found:
                self.last_found.append(got.address)   # found here: try_card and add_instrument may use it

        self.busy = "looking for instruments on the network"
        try:
            adverts, problems = ([], []) if network else network_adverts()
            ports = probe.sweep(nets, skip=set(in_lab) | {a["address"] for a in adapters})
            hosts = list(ports)[:probe.MAX_IDENTIFIED]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                said = dict(zip(hosts, pool.map(lambda ip: probe.identify_host(ip, ports[ip]), hosts)))
            visa = [(r, probe.host_of(r)[0]) for r, _ in adverts if probe.host_of(r)[0] not in in_lab]
            gpib: List[str] = []   # Prologix GPIB-ETHERNETs: who is on the GPIB bus behind each
            for ip in hosts:
                for f in said[ip]["found"]:
                    if f.get("idn"):
                        card = match_card(f["idn"], self.lab.registry)
                        got = Found("TCPIP0::%s::%d::SOCKET" % (ip, f["port"]), f["idn"], card.id if card else None,
                                    card.model if card else "", card.kind if card else "", "identity reply")
                        keep(ip, got if card else look_up(got, self.collection))
                    elif f["port"] in (111, 4880) and all(host != ip for _, host in visa):
                        visa.append(("TCPIP0::%s::%s::INSTR" % (ip, "inst0" if f["port"] == 111 else "hislip0"), ip))
                    elif f["port"] == prologix.PORT:
                        gpib.append(ip)
            for resource, host in visa[:8]:   # VXI-11 and HiSLIP answer through VISA, one at a time
                if host in found:
                    continue   # it said who it is on a socket already
                try:
                    got = identify(resource, self.lab.registry, timeout_ms=1500, catalog=self.collection)
                except Exception:
                    continue
                if got.idn:
                    keep(host, got)
            listed = {resource_name(ic.address): n for n, ic in self.lab.config.instruments.items()}
            for ip in gpib:   # each GPIB address asked in turn: about 12 s an adapter, one adapter at a time
                self.busy = "asking who is behind the Prologix at %s" % ip
                try:
                    behind, _ = scan_prologix("%s:%d" % (ip, prologix.PORT), self.lab.registry, listed,
                                              catalog=self.collection)
                except (AdapterError, OSError):
                    continue   # no Prologix after all (it didn't answer ++ver), or another program has it
                for got in behind:
                    if got.idn and not got.in_lab:
                        keep(ip, got)
        finally:
            self.busy = None

        def order(host: str) -> Tuple[int, int, str]:
            try:
                return 0, int(ipaddress.ip_address(host)), ""
            except ValueError:
                return 1, 0, host

        everyone = sorted(set(hosts) | set(found), key=order)
        lines = ["%s%s: %s" % (
            "Looked on %s (ports %s)" % (", ".join(map(str, nets)), " ".join(map(str, probe.SWEEP_PORTS))) if nets
            else "Swept no network", "" if network else " and for instruments that announce themselves",
            "%d address%s answered" % (len(everyone), "" if len(everyone) == 1 else "es") if everyone else
            "nothing answered")]
        for host in everyone:
            seen = said.get(host) or {"found": [], "lxi": {}}
            bits = [probe.what_answers(f) for f in seen["found"] if not f.get("idn")]
            if seen["lxi"]:
                bits.append("LXI identification: " + ", ".join("%s %s" % kv for kv in seen["lxi"].items()))
            for got in found.get(host, []):
                bits.append("%s answers %r → %s" % (got.address, got.idn[:100], (
                    "card %s (%s): add_instrument %s" % (got.card_id, got.model, got.address) if got.card_id else
                    "no card yet%s: draft_card %s" % (" (the instrument collection knows it as %s)" % got.collection
                                                      if got.collection else "", got.address))))
            for f in seen["found"]:   # a gateway: the address to try a card on
                gate = {502: "modbus://%s?unit=1", 4001: "TCPIP0::%s::4001::SOCKET",
                        10001: "TCPIP0::%s::10001::SOCKET"}.get(f["port"])
                if gate and gate % host not in self.last_found:
                    self.last_found.append(gate % host)
            lines.append("- %s: %s" % (host, "; ".join(bits) or "ports %s open" % ", ".join(map(str, ports[host]))))
        if len(ports) > len(hosts):
            lines.append("Also answered, not asked who they are: " + ", ".join(list(ports)[len(hosts):]))
        if in_lab:
            lines.append("In the lab already, left alone: " + ", ".join("%s (%s)" % (n, h) for h, n in in_lab.items()))
        lines += notes + problems
        if not everyone:
            lines.append("Check that the instrument is switched on, on one of this computer's networks "
                         "(connected_devices lists them), with its network interface on (often a front-panel "
                         "setting); or give its address, from its front panel, to inspect_address.")
        self.gate.log.write("network_search", by="agent", networks=[str(n) for n in nets] or None,
                            answered=everyone or None, found=[g.address for h in everyone for g in found.get(h, [])]
                            or None)
        return "\n".join(lines)

    def behind_prologix(self, adapter: str) -> str:
        """The instruments on the GPIB bus behind a Prologix adapter (a GPIB-ETHERNET's host[:port], a GPIB-USB's
        serial port): each GPIB address asked who it is (++addr, then *IDN?) and matched to a card, kept for try_card
        and add_instrument. An instrument already in the lab is left alone."""
        from .. import prologix
        from ..adapters import AdapterError, resource_name
        from ..discovery import scan_prologix

        def run() -> str:
            in_lab = {resource_name(ic.address): name for name, ic in self.lab.config.instruments.items()}
            try:
                found, words = scan_prologix(adapter, self.lab.registry, in_lab, catalog=self.collection)
            except (AdapterError, OSError) as e:
                return str(e).splitlines()[0] if str(e) else "the Prologix %s could not be asked" % adapter
            lines = ["%s: %s" % (words, "each GPIB address from 1 to 30 asked who it is" if found else
                                 "nothing on its GPIB bus answered *IDN? (addresses 1 to 30). Check that the instruments "
                                 "are switched on and cabled; one that doesn't know *IDN? is added by its address and "
                                 "card: add_instrument prologix://%s/<its GPIB address> with card"
                                 % prologix.shown(prologix.adapter_of(adapter)))]
            for f in found:
                lines.append("- %s%s" % (f.line(), "" if f.in_lab else ": add_instrument %s" % f.address if f.card_id
                                         else ": draft_card %s" % f.address if f.idn else ""))
                if f.in_lab:
                    continue
                self.found_info[resource_name(f.address)] = dict(vars(f))
                if f.address not in self.last_found:
                    self.last_found.append(f.address)   # found here: try_card and add_instrument may use it
            return "\n".join(lines)
        return self._do("looking behind the Prologix adapter %s" % adapter, run)

    def inspect_serial(self, port: str, query: str = "*IDN?", baud: int = 0) -> str:
        """What an instrument on a serial port is: what it sends by itself (some stream readings), then its answer
        to an identity query at the speeds and framings instruments use (or the one the manual gives). Only listens
        and asks that query; a port an instrument of the lab uses is left alone."""
        from .. import probe
        from ..adapters import resource_name
        from ..discovery import match_card
        m = re.match(r"^(?:serial://)?(COM\d+|/dev/\S+)$", port.strip(), re.I) or \
            re.match(r"^ASRL(\d+)::INSTR$", port.strip(), re.I)
        if not m:
            return "%r is not a serial port (COM5, /dev/ttyUSB0)" % port
        name = m.group(1) if not m.group(1).isdigit() else "COM" + m.group(1)
        address = "serial://" + name
        for inst, ic in self.lab.config.instruments.items():
            if resource_name(ic.address) == resource_name(address):
                return "%s is %s's, in the lab: Kelvin leaves its connection alone" % (name, inst)
        query = (query or "*IDN?").strip()
        if "?" not in query or len(query) > 40:
            return "only an identity query (with a ?, as the manual gives it) is sent to an instrument Kelvin doesn't know"
        with self.lock:   # one thing at a time on the ports
            res = probe.inspect_serial(name, query, [baud] if baud else None)
        if res.get("error"):
            return "could not open %s: %s (another program may have it open)" % (name, res["error"])
        lines = []
        for speed, text in res["streams"]:
            lines.append("%s sends by itself at %d baud, 8N1: %r (an instrument that streams its readings)"
                         % (name, speed, text))
        a = res["answer"]
        if a is None:
            lines.append("nothing answered %s at %s, 8N1, 7O1 or 7E1 (%d tries). Check its cable (a null-modem one?) "
                         "and that it is switched on; its manual gives its speed and its identity query (query=)"
                         % (query, "%d baud" % baud if baud else "1200 to 115200 baud", res["tried"]))
            return "\n".join(lines)
        settings = {"baud_rate": a["baud"], "data_bits": a["data_bits"], "parity": a["parity"], "stop_bits": 1,
                    "read_termination": a["ending"], "write_termination": "\r\n"}
        if a["echo"]:
            settings["echo"] = True
        card = match_card(a["reply"], self.lab.registry)
        framing = "%d%s1" % (a["data_bits"], a["parity"][0].upper())
        lines.append("%s answers %s at %d baud, %s%s: %r → %s" % (
            name, query, a["baud"], framing, " (it echoes each command)" if a["echo"] else "", a["reply"],
            "card %s (%s): add_instrument %s" % (card.id, card.model, address) if card else
            "no card matches it yet: draft_card %s, then add_instrument with connection %s" % (address, settings)))
        if address not in self.last_found:
            self.last_found.append(address)   # found here: try_card and add_instrument may use it
        self.found_info[resource_name(address)] = {"address": address, "idn": a["reply"], "serial": settings,
                                                   "card_id": card.id if card else None,
                                                   "model": card.model if card else "", "kind": card.kind if card else ""}
        return "\n".join(lines)

    def connected_devices(self) -> str:
        """Everything plugged into this computer: USB devices (with what each calls itself and its driver, also
        those no driver claims), serial ports, and the networks this computer is on."""
        from .. import probe
        ports, devices = probe.serial_ports(), probe.usb_devices()
        lines = ["USB devices:" if devices else "No USB devices (or this computer doesn't say)."]
        plain = [d for d in devices if probe.ordinary(d)]
        lines += ["- " + probe.usb_line(d, ports) for d in devices if d not in plain]
        if plain:
            lines.append("- also: " + ", ".join((d["names"] or ["?"])[0] for d in plain))
        usb_ports = {p["port"] for p in ports if p.get("vid") is not None}
        other = [p for p in ports if p["port"] not in usb_ports]
        if other:
            lines.append("Serial ports (not USB): " + ", ".join("%s (%s)" % (p["port"], p["description"])
                                                                if p["description"] else p["port"] for p in other))
        nets = probe.network_adapters()
        if nets:
            lines.append("Networks this computer is on: " + "; ".join(
                "%s %s (this computer %s)%s" % (n["name"], n["network"], n["address"],
                                                ", link-local: instruments that fall back to 169.254.x.x" if
                                                n["link_local"] else "") for n in nets))
        lines.append("An instrument here that scan_instruments doesn't find: its maker (installed_software, or "
                     "web_search for its USB ID), then what it needs (install_for_instrument) and its card "
                     "(draft_card). On a network: inspect_address.")
        return "\n".join(lines)

    def installed_software(self, query: str = "") -> str:
        """Programs and drivers installed on this computer: those whose name or maker holds the query, or with no
        query the ones that look like instrument makers' software, drivers and Pythons."""
        from .. import probe
        found = probe.programs(query)
        if not found:
            return ("nothing installed matches %r" % query if query else
                    "no instrument makers' software found among the installed programs")
        lines = ["%s%s%s%s" % (p["name"], " " + p["version"] if p["version"] and p["version"] not in p["name"] else "",
                               " (%s)" % p["publisher"] if p["publisher"] else "",
                               " in " + p["location"] if p["location"] else "") for p in found[:60]]
        if len(found) > 60:
            lines.append("... and %d more: name a maker or program" % (len(found) - 60))
        lines.append("find_maker_files finds a program's examples, SDK folders and libraries.")
        return "\n".join(lines)

    def find_maker_files(self, query: str) -> str:
        """Files and folders named like the query in the makers' install folders (Program Files, ProgramData, Public
        Documents; /Applications, /Library, /opt): a maker's examples, its SDK's Python package, its DLL."""
        from .. import probe
        hits = probe.find_files(query)
        if not hits:
            return "nothing named like %r in %s" % (query, ", ".join(str(r) for r in probe.search_roots()))
        lines = ["%s (%s)" % h for h in hits]
        lines.append("read_examples reads a folder of examples; install_for_instrument with files installs an SDK's "
                     "Python package or keeps a DLL in an environment.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ what an instrument needs on this computer

    def environments(self) -> str:
        """Kelvin's instrument environments on this computer (envs.py): their Python, packages and files."""
        from .. import envs
        found = envs.listing(self.home)
        if not found:
            return ("no instrument environments yet: install_for_instrument makes one when an instrument needs its "
                    "maker's Python package or files")
        lines = []
        for rec in found:
            pkgs = ", ".join("%s %s" % kv for kv in sorted(rec.get("packages", {}).items())) or "no packages"
            lines.append("%s: Python %s (%d-bit); %s%s%s" % (
                rec["name"], rec.get("python"), rec.get("bits", 64), pkgs,
                "; files: " + ", ".join(rec["files"]) if rec.get("files") else "",
                "; for " + ", ".join(rec["for"]) if rec.get("for") else ""))
        uv = envs.uv_path(self.home)
        lines.append("(uv: %s) A library card uses one with lib://<address>?env=<name>; Kelvin's built-in drivers use "
                     "%r." % (uv or "not here yet", envs.DRIVERS))
        return "\n".join(lines)

    def install_for_instrument(self, env: str, packages: Optional[List[str]] = None, python: str = "", bits: int = 64,
                               files: str = "", why: str = "", by: str = "agent") -> str:
        """Install what an instrument needs on this computer, in one of Kelvin's environments (envs.py): its maker's
        Python packages from PyPI, at the version the instrument needs, and its own files (a folder or a download).
        A person approves first, seeing what, which version and from where."""
        from .. import envs, profiles
        from ..gate import ApprovalRequest
        packages = [p.strip() for p in (packages or []) if str(p).strip()]
        try:
            envs.env_dir(env, self.home)
        except ValueError as e:
            return str(e)
        if not packages and not files:
            return "say which packages (pip names, with a version if it must match) or which files to install"
        if bits not in (32, 64):
            return "bits is 32 or 64"
        bad = [p for p in packages if not envs.PACKAGE.match(p)]
        if bad or len(packages) > 12:
            return "not package names: %s" % ", ".join(bad) if bad else "at most 12 packages at a time"
        if not self.literature.enabled and (packages or re.match(r"^https?://", files or "")):
            return "searching outside the lab (literature and web) is switched off in Settings, so Kelvin downloads nothing"
        rec = envs.info(env, self.home)
        app_version, app_bits = envs.app_python()
        if rec is not None and ((python and python != rec.get("python")) or bits != rec.get("bits", 64)):
            return ("environment %s has Python %s (%d-bit): name another environment for Python %s (%d-bit)"
                    % (env, rec.get("python"), rec.get("bits", 64), python or app_version, bits))
        if env == envs.DRIVERS and (python and python != app_version or bits != app_bits):
            return ("%s is for Kelvin's built-in drivers and has Kelvin's own Python (%s, %d-bit); a library that needs "
                    "another goes in an environment of its own" % (envs.DRIVERS, app_version, app_bits))
        items = []
        if envs.uv_path(self.home) is None:
            items.append("uv, Astral's Python installer (about 20 MB), from its GitHub release (github.com/astral-sh/uv), "
                         "checked against its published SHA-256; once, kept in %s" % (Path(self.home) / "tools"))
        if rec is None:
            items.append("a new environment %s in %s, with its own Python %s (%d-bit), which uv downloads if this "
                         "computer has none (30 to 40 MB)" % (env, envs.env_dir(env, self.home), python or app_version, bits))
        for spec in packages:
            try:
                meta = envs.pypi(spec, self._get)
            except LookupError as e:
                return "%s: check the package's name in the maker's instructions" % e
            except Exception as e:
                return "could not look up %s on PyPI: %s" % (spec, profiles._first_line(e))
            items.append("%s from PyPI (%s; latest %s)%s%s" % (spec, meta["name"], meta["version"],
                                                               ": " + meta["summary"] if meta["summary"] else "",
                                                               " — " + meta["home"] if meta["home"] else ""))
        if files and not re.match(r"^https?://", files) and not Path(files).expanduser().exists():
            return "there is no file or folder %s on this computer" % files
        if files:
            items.append("the maker's files from %s, kept in the environment (a Python package among them is "
                         "installed)" % files)
        request = ApprovalRequest("Install on this computer%s?" % (" for " + why if why else ""), items)
        # a download onto the person's computer is theirs to say yes to (F3 P6: ~71 MB came unasked); the Lab panel's
        # Install button is that yes already
        if not by.startswith("human") and not self.gate.ask(request):
            note = " (%s)" % request.note if request.note else ""
            return "declined: nothing installed%s" % note
        try:
            if envs.uv_path(self.home) is None:
                envs.fetch_uv(self.home, self._get)
            if rec is None:
                rec = envs.create(env, python, bits, self.home, why=why)
            elif why and why not in rec.get("for", []):
                rec.setdefault("for", []).append(why)
                envs._save(rec, self.home)
            if packages:
                rec = envs.install(env, packages, self.home)
            if files:
                rec = envs.info(env, self.home) or rec
                kept = self._fetch_files(env, files)
                rec = envs.info(env, self.home) or rec
        except Exception as e:
            self.gate.log.write("install_failed", by=by, env=env, packages=packages, files=files or None,
                                error=profiles._first_line(e))
            return "could not install: %s" % profiles._first_line(e)
        if env == envs.DRIVERS:
            envs.attach(env, self.home)
        self.gate.log.write("installed", by=by, env=env, packages=packages, files=files or None, why=why or None)
        got = ", ".join("%s %s" % (base, rec["packages"].get(envs.base_name(base), "?"))
                        for base in [envs.base_name(p) for p in packages])
        names = {envs.base_name(p).lower() for p in packages}
        waiting = [n for n, why in self.lab.unavailable.items()   # the instruments it was missing for
                   if (envs.needed(why) or {}).get("env") == env and names >= {envs.base_name(p).lower() for p in
                                                                             envs.needed(why)["packages"]}]
        connected = [self.reconnect_instrument(n, by) for n in waiting]
        return ("installed in environment %s (Python %s, %d-bit)%s%s. %s%s"
                % (env, rec.get("python"), rec.get("bits", 64), ": " + got if got else "",
                   "; files in %s" % kept if files else "",
                   "Kelvin's built-in drivers use it now." if env == envs.DRIVERS else
                   "A library card uses it with lib://<address>?env=%s." % env,
                   "".join(" " + c for c in connected)))

    def do_action(self, instrument: str, action: str) -> str:
        """One of an instrument's named actions (its card's actions: trigger, arm, fire a burst). One that drives the
        sample asks the person first."""
        def run() -> str:
            return self.gate.act(instrument, action).message
        return self._do("%s.%s" % (instrument, action), run, stoppable=True, uses=[instrument])

    def connect_pending(self, progress: Optional[Callable[[str, int, int], None]] = None) -> Dict[str, Any]:
        """Connect the instruments of a lab Kelvin opened before connecting them (open_home_lab connect=False), one
        at a time, holding the lab for each only: a request for one already connected doesn't wait for the rest. One
        that can't be connected is set aside with why, as at any start. progress(name, i, n) before each."""
        lab = self.lab
        names = lab.connecting
        done: List[str] = []
        for i, name in enumerate(names):
            if self.lab is not lab:   # a person switched labs meanwhile
                break
            if name not in lab.connecting:   # a person switched it off meanwhile (or it connected another way)
                continue
            if progress is not None:
                progress(name, i, len(names))
            with self.lock:
                self.busy = "connecting %s" % name
                try:
                    lab.reconnect(name)
                    done.append(name)
                except Exception:
                    pass   # set aside with why: the Lab panel says it (and offers the install a missing package needs)
                finally:
                    self.busy = None
        with self.lock:
            self._apply_protection(recheck=True)   # the mounted device's caps, written now that they can be
            self._caps_if_waiting()
            self._refresh()
        missing = {n: why for n, why in lab.unavailable.items() if n in names}
        self.gate.log.write("lab_connected", lab=lab.config.name, instruments=done, not_connected=missing or None)
        return {"connected": done, "not_connected": missing}

    def reconnect_instrument(self, name: str, by: str = "agent") -> str:
        """Connect an instrument of the lab that could not be connected, once what stopped it is fixed (a package
        installed, the instrument switched on or plugged in), with its card as it is on disk now: a card fixed since
        it connected is used from now on (lab.Lab.reconnect). Connecting changes nothing on it."""
        from ..homelab import card_now
        inst = self.lab.instruments.get(name)
        if inst is None:
            return "no instrument %r (have: %s)" % (name, ", ".join(self.lab.instruments))
        try:
            card = card_now(self.lab, name, self.home, self.collection)
        except ValueError as e:   # the card is gone, or no longer fits the lab config: the instrument keeps its own
            return "%s keeps the card it connected with: %s" % (name, e)
        if not inst.enabled and not inst.unavailable:
            news = self.lab.reconnect(name, card)
            return "%s is switched off in the Lab panel: ask_to_switch_on asks the person%s" % (
                name, " (it will use %s)" % news if news else "")
        if inst.enabled and card is None:
            return "%s is connected, with its card as it is now" % name
        with self.lock:
            try:
                news = self.lab.reconnect(name, card)
            except Exception as e:
                self.gate.log.write("reconnect_failed", by=by, instrument=name, error=inst.unavailable)
                return "%s is still not connected: %s" % (name, inst.unavailable or e)
            self.gate.log.write("instrument_connected", by=by, instrument=name, card=inst.card.id,
                                reloaded=bool(news))
            self._caps_if_waiting()
            self._refresh()
        health = self.lab.health_check([name])[0]
        return "%s is connected now%s%s" % (name, ", with " + news if news else "",
                                            ": " + "; ".join(health.problems) if health.problems else
                                            " and answers as its card expects")

    def _fetch_files(self, env: str, source: str) -> str:
        """A maker's files into an environment: a folder or file on this computer, or a web address. A Python package
        (a wheel, a source archive, a folder with its setup, as SDKs bring them) is installed; anything else (a
        DLL, a .py, a folder of them, a .zip's contents) is kept in the environment's files folder."""
        import tempfile
        import zipfile
        from .. import envs
        with tempfile.TemporaryDirectory() as tmp:
            if re.match(r"^https?://", source):
                path = Path(tmp) / (re.sub(r"[^A-Za-z0-9._-]+", "_", Path(source.split("?")[0]).name) or "download")
                path.write_bytes(self._get(source))
            else:
                path = Path(source).expanduser()
                if not path.exists():
                    raise FileNotFoundError("no file or folder %s" % source)
            if path.is_file() and path.name.lower().endswith(".zip"):
                folder = Path(tmp) / "unzipped" / path.stem   # kept under the archive's name
                with zipfile.ZipFile(path) as z:
                    z.extractall(folder)   # names with .. or a drive are made safe by zipfile
                inner = list(folder.iterdir())
                path = inner[0] if len(inner) == 1 and inner[0].is_dir() else folder
            if envs.installable(path):
                envs.install_path(env, path, self.home)
                return "%s (installed as a Python package)" % envs.site_packages(env, self.home)
            return str(envs.add_files(env, path, self.home))

    def _run_over_ssh(self, address: str, command: str, notebook: str, password: str, wait: float) -> str:
        """A shell command on a board or lab computer over SSH, once a person approves it."""
        from .. import boards, envs, probe
        try:
            user, host, port = boards.ssh_target(address)
            probe.local_ip(host)
        except (ValueError, boards.JupyterError) as e:
            return str(e)
        if notebook or not (command or "").strip():
            return "over SSH, give the shell command to run (code); notebooks run through a board's Jupyter"
        wait = max(1.0, min(float(wait or 30), 300.0))
        request = ApprovalRequest("Run a command on %s over SSH?" % host, [
            "As %s on %s:%d:\n%s" % (user, host, port, command.strip()[:3000]),
            "Signed in with %s. A command still running after %g s is stopped when Kelvin disconnects: a server is "
            "started in the background (nohup ... &)." % ("the password given" if password else
                                                         "this computer's SSH key", wait)])
        if not self.gate.ask(request):
            note = " (%s)" % request.note if request.note else ""
            return "declined: nothing was run on %s%s" % (host, note)
        try:
            done, said = boards.ssh_run(address, command, password, wait, envs.root(self.home) / "tools" / "known_hosts")
        except boards.JupyterError as e:
            return "could not run it: %s" % e
        self.gate.log.write("board_run", by="agent", board="ssh://%s@%s:%d" % (user, host, port), finished=[done])
        said = said.strip()
        return "%s%s" % ("done" if done else "still running after %g s, stopped when Kelvin disconnected" % wait,
                         ": " + said[-2000:] if said else "")

    def run_on_board(self, address: str, code: str = "", notebook: str = "", password: str = "",
                     wait: float = 30) -> str:
        """Run code on a lab board through its Jupyter server (a PYNQ board for QICK, a Red Pitaya), as a notebook
        cell runs there: the code given, or one of the board's own notebooks, in a new kernel. Kelvin signs in first,
        then a person approves the code. A cell still running after `wait` seconds (a server) is left running.
        An ssh://user@host address runs code as a shell command over SSH instead (a Red Pitaya, a Raspberry Pi)."""
        import urllib.parse
        from .. import boards, probe
        if address.strip().lower().startswith("ssh://"):
            return self._run_over_ssh(address, code, notebook, password, wait)
        try:
            url = boards.board_url(address)
            probe.local_ip(urllib.parse.urlparse(url).hostname or "")
        except (ValueError, boards.JupyterError) as e:
            return str(e)
        if bool((code or "").strip()) == bool((notebook or "").strip()):
            return "give the code to run, or the path of a notebook on the board: one of them"
        wait = max(1.0, min(float(wait or 30), 300.0))
        jupyter = boards.Jupyter(url, password)
        try:
            jupyter.login()
            cells = jupyter.notebook(notebook) if notebook else [code]
        except boards.JupyterError as e:
            return "could not run it: %s" % e
        if not cells:
            return "the notebook %s has no code" % notebook
        shown = "\n\n".join(cells)
        host = urllib.parse.urlparse(url).hostname
        request = ApprovalRequest("Run code on the board at %s?" % host, [
            "%s:\n%s" % ("Its notebook %s, cell by cell" % notebook if notebook else "This code",
                          shown[:3000] + (" ..." if len(shown) > 3000 else "")),
            "In a new Python kernel on the board's Jupyter (%s)%s. A cell still running after %g s (a server) "
            "keeps running there." % (url, ", signed in with the password given" if password else "", wait)])
        if not self.gate.ask(request):
            note = " (%s)" % request.note if request.note else ""
            return "declined: nothing was run on the board%s" % note
        try:
            results = boards.run(url, cells, seconds=wait, jupyter=jupyter)
        except boards.JupyterError as e:
            return "could not run it: %s" % e
        self.gate.log.write("board_run", by="agent", board=url, notebook=notebook or None, cells=len(cells),
                            finished=[done for done, _ in results])
        lines = []
        for i, (done, said) in enumerate(results, 1):
            said = said.strip()
            lines.append("cell %d: %s%s" % (i, "done" if done else "still running (a server, left running on the "
                                            "board)", ": " + said[-1500:] if said else ""))
        if len(results) < len(cells):
            lines.append("cells %d-%d not run" % (len(results) + 1, len(cells)))
        return "\n".join(lines)

    def library_api(self, module: str, env: str = "") -> str:
        """A maker's Python package as its programmer sees it, for writing its library card when the maker gives
        no example scripts: its classes and functions with their arguments and documentation's first line. Read in
        Kelvin's helper process, from one of Kelvin's environments; nothing in the package is called."""
        from .. import library
        from ..adapters import AdapterError
        try:
            said = library.describe(module.strip(), env.strip(), self.home)
        except (AdapterError, ValueError) as e:
            return "could not read %s: %s" % (module, e)
        return (said or "%s has no public classes or functions of its own" % module) + (
            "\nA library card names its module and object (card_guide), and calls these by name; the helper "
            "calls nothing else.")

    def remove_environment(self, env: str) -> str:
        """Remove one of Kelvin's instrument environments, after a person approves; instruments using it stop
        connecting until it is installed again."""
        from .. import envs
        from ..gate import ApprovalRequest
        try:
            rec = envs.info(env, self.home)
        except ValueError as e:
            return str(e)
        if rec is None:
            return "no environment %s" % env
        using = [n for n, i in self.lab.instruments.items() if "env=%s" % env in (i.config.address or "")
                 or (env == envs.DRIVERS and i.card.requires)]
        request = ApprovalRequest("Remove an instrument environment?", [
            "%s (Python %s, %d-bit: %s)%s" % (env, rec.get("python"), rec.get("bits", 64),
                                             ", ".join(rec.get("packages", {})) or "no packages",
                                             "; used by " + ", ".join(using) if using else "")])
        mine = not using and self.gate.auto_simple(request, "removing an environment no instrument uses drives "
                                                            "nothing; it can be installed again")
        if not mine and not self.gate.ask(request):   # one in use: Kelvin would lose that instrument, so a person says
            return "declined: %s stays" % env
        whole = envs.remove(env, self.home)
        self.gate.log.write("environment_removed", by="agent", env=env)
        return "removed environment %s%s" % (env, "" if whole else ": some of its files are in use, and go when Kelvin "
                                             "next starts")

    # ------------------------------------------------------------------ adding instruments to the lab

    def add_instrument(self, address: str, card: str = "", name: str = "", connection: Optional[Dict[str, Any]] = None,
                       card_vars: Optional[Dict[str, str]] = None, by: str = "agent") -> str:
        """Kelvin adds an instrument connected to this computer to the lab (lab.yaml), marked added_by: kelvin, and
        connects it; the others stay connected as they are. It reads and configures at once; until a person reviews
        its limits, every output change on it asks them. An address the last scan did not find is approved by Kelvin
        itself (it connects and reads); a program to run asks a person in Lab setup."""
        from ..adapters import AdapterError, resource_name
        from ..cards import CONNECTION_KEYS
        from ..discovery import Found, add_to_config, card_for
        from ..labconfig import ConfigError, load_lab_config
        from ..homelab import build_lab, lab_registry
        if self.home is None:
            return "not added: this Kelvin keeps no lab config (the Kelvin app does)"
        address = (address or "").strip()
        if not address or address.startswith("sim://"):
            return "not added: give the address of an instrument connected to this computer (scan_instruments)"
        lab_file = self.home / "lab.yaml"
        registry = lab_registry(self.home, self.collection)   # the cards the lab opens with, the collection's too
        found = self.found_info.get(resource_name(address), {})
        named = (card or "").strip()
        card_id = named if named in registry else "" if named else found.get("card_id") or ""
        matched = ""
        if not card_id:   # by the name given, its identity reply or its driver address (qd://) before any draft
            hit, matched = card_for(address, registry, str(found.get("idn") or ""), named)
            if hit is None and named:
                return "not added: %s (find_card lists the cards Kelvin has)" % matched
            if hit is None:
                return ("not added: Kelvin has no card for %s yet%s%s; find_card looks for one by maker and model, "
                        "draft_card writes one from its manual" % (address, " (it answered %r)" % found["idn"][:60]
                                                                   if found.get("idn") else "",
                                                                   "; " + matched if matched else ""))
            card_id = hit.id
        try:
            the_card = registry.get(card_id)
        except KeyError:
            return "not added: there is no card %r (draft_card or save_card makes one)" % card_id

        def run() -> str:
            live = self.live or {}
            if live.get("status") == "running" or self.queue.state in ("running", "pausing"):
                return "not added: a plan or the queue is running; add it when it has finished"
            text = lab_file.read_text(encoding="utf-8") if lab_file.exists() else ""
            on_file = self.lab_info.get("kind") == "file"
            if on_file and not text.strip():
                return "not added: your lab config (lab.yaml) is gone; open Lab setup to make it again"
            try:
                current = self.lab.config if on_file else (load_lab_config(text, registry) if text.strip() else None)
            except ConfigError as e:
                return "not added: your lab config has problems (fix them in Lab setup): " + "; ".join(e.errors[:4])
            except (ValueError, yaml.YAMLError) as e:
                return "not added: your lab config can't be read (fix it in Lab setup): %s" % str(e).splitlines()[0]
            taken = dict(current.instruments) if current is not None else {}
            for n, ic in taken.items():
                if resource_name(ic.address) == resource_name(address):
                    off = on_file and n in self.lab.switched_off
                    return "%s is in the lab already, as %s (card %s)%s" % (
                        address, n, ic.card, "; it is switched off: ask_to_switch_on" if off else "")
            try:   # names in the file too, should it have changed since Kelvin connected
                listed = list(((load_yaml(text) or {}).get("instruments") or {}) if text.strip() else [])
            except Exception:
                listed = []
            names = sorted(set(taken) | {str(n) for n in listed})
            new = (name or "").strip() or Found(address, kind=the_card.kind).suggest_name(names)
            if not new.isidentifier():
                return "not added: %r can't be a name (letters, digits and _)" % new
            if new in names:
                return "not added: the lab has a %s already (%s); choose another name" % (new, ", ".join(names))
            conn = dict(connection or {})
            if not conn and found.get("serial") and "baud_rate" not in the_card.connection:   # how the scan reached it
                conn = {k: v for k, v in found["serial"].items() if k in CONNECTION_KEYS}
            item = {"name": new, "card": the_card.id, "address": address, "added_by": "kelvin",
                    "note": str(found.get("idn") or the_card.model)[:60],
                    "connection": conn, "vars": {str(k): str(v) for k, v in (card_vars or {}).items()}}
            try:
                new_text = add_to_config(text, [item], registry)
                cfg = load_lab_config(new_text, registry)
            except ConfigError as e:
                return "not added: " + "; ".join(x for x in e.errors[:4])
            except (ValueError, StopIteration, yaml.YAMLError) as e:
                why = str(e).splitlines()[0] if str(e) else type(e).__name__
                return "not added: Kelvin couldn't fit its entry into your lab config (%s); add it in Lab setup" % why
            ic = cfg.instruments.get(new)
            if ic is None or ic.address != address or ic.card != the_card.id:
                return "not added: the lab config changed while Kelvin was adding it; try again"
            asks = []
            if resource_name(address) not in {resource_name(a) for a in self.last_found}:
                if _runs_a_program(address):
                    return ("not added: a lib:// address with python= or path= names a program for Kelvin to run, "
                            "so a person adds it in Lab setup (the card is ready for them); or install the maker's "
                            "library with install_for_instrument and use lib://<address>?env=<environment>")
                asks.append("Kelvin's last scan did not find an instrument at %s. Kelvin would connect to it as %s "
                            "(%s) and read it." % (address, new, the_card.model))
            if asks:
                request = ApprovalRequest("Add %s to the lab?" % new, asks + [
                    "Every output change on it still asks you until you review its limits."])
                simple = len(asks) == 1   # only an address the scan missed: it connects and reads
                if not (simple and self.gate.auto_simple(request, "adding an instrument connects it and reads it; "
                                                                  "every output change on it still asks")) \
                        and not self.gate.ask(request):
                    note = " (%s)" % request.note if request.note else ""
                    return "declined: the person did not want %s added%s" % (new, note)
            try:   # on the person's lab: connect just this one; from no lab yet: their lab, with it
                lab = self.lab if on_file else build_lab(new_text, None, 0, registry, self.switched_off_for)
                if on_file:
                    lab.add(ic, the_card)
            except (AdapterError, OSError, ValueError, RuntimeError) as e:
                why = str(e).splitlines()[0] if str(e) else type(e).__name__
                return "not added: Kelvin could not connect %s: %s" % (address if on_file else "your lab with it", why)

            def undo() -> None:
                if on_file:
                    lab.remove(new)
                else:
                    lab.close()

            health = lab.health_check([new])[0]
            if any("does not match card" in p for p in health.problems):   # a wrong address: another instrument
                undo()
                return "not added: %s" % "; ".join(health.problems)
            try:
                lab_file.parent.mkdir(parents=True, exist_ok=True)
                fresh = lab_file.read_text(encoding="utf-8") if lab_file.exists() else ""
                if fresh != text:   # a person changed it meanwhile (an instrument's limits in Set up): add to theirs
                    new_text = add_to_config(fresh, [item], registry)
                    load_lab_config(new_text, registry)
                tmp = lab_file.with_suffix(".tmp")
                tmp.write_text(new_text, encoding="utf-8")
                tmp.replace(lab_file)
            except OSError as e:
                undo()
                return "not added: Kelvin could not save the lab config: %s" % e
            except (ConfigError, ValueError, StopIteration, yaml.YAMLError):
                undo()
                return "not added: the lab config changed while Kelvin was adding it; try again"
            self.config_changed("Kelvin (%s)" % by, "added %s at %s" % (new, address), "added")
            if not on_file:
                info = {"kind": "file", "path": str(lab_file), "error": None, "has_file": True}
                try:
                    self.switch_lab(lab, None, info, "Kelvin (%s)" % by)
                except RuntimeError as e:
                    lab.close()
                    return "added %s to your lab config, but Kelvin could not switch to it: %s" % (new, e)
            self._apply_protection(recheck=True)
            manual = self.manuals_found.pop(resource_name(address), None)
            if manual:
                from .. import profiles
                try:
                    profiles.store_manual(self.home, new, self.lab.instruments[new].card, manual["data"], manual["url"])
                except Exception:
                    pass   # the app's setup fetches it again
            self.gate.log.write("instrument_added", by=by, instrument=new, card=the_card.id, address=address,
                                lab=self.lab.config.name)
            self.notifier("setup", "Kelvin added %s to the lab" % new,
                          "%s at %s. Readings and settings work now; Kelvin asks you before each output change on it "
                          "until you review its limits in Lab setup." % (the_card.model, address))
            self.lab_changed("added")
            lim = ", ".join("%s [%s, %s]%s" % (p.name, num(p.limits[0]), num(p.limits[1]), " " + p.unit if p.unit else "")
                            for p in self.lab.instruments[new].card.drives if p.numeric and p.limits)
            return ("added %s (%s, card %s%s%s) at %s to %s; health: %s. Its outputs keep the maker's limits%s until a "
                    "person reviews them, and every output change on it asks them (auto mode doesn't cover it)."
                    % (new, the_card.model, the_card.id, ", a draft" if the_card.status == "draft" else "",
                       ", found by " + matched if matched else "", address, self.lab.config.name,
                       "ok" if health.ok else "; ".join(health.problems), " (" + lim + ")" if lim else ""))
        return self._do("adding an instrument", run)

    def refresh_cards(self, ids: Iterable[str]) -> None:
        """After cards changed (the user's, or a new copy of the collection): the card the lab's registry has for each
        of these ids, by the same layers a lab is opened with (homelab.lab_registry). Instruments already
        connected keep theirs until the lab is opened again."""
        from ..homelab import lab_registry
        ids = list(ids)
        if not ids:
            return
        fresh = lab_registry(self.cards_dir.parent if self.cards_dir else None, self.collection)
        for card_id in ids:
            if card_id in fresh:
                self.lab.registry.put(fresh.get(card_id))
            else:
                self.lab.registry.remove(card_id)

    # ------------------------------------------------------------------ experiments (the instrument collection)

    def experiment_guide(self, experiment: str = "", query: str = "") -> str:
        """The instrument collection's experiments, or one of them with its plans' instrument names changed to this
        lab's (collection.adapt). Only reads the copy of the collection on this computer."""
        from .. import collection as coll
        c = self.collection
        if c is None:
            return ("the instrument collection is not switched on in this Kelvin (Settings, Instruments), so its "
                    "experiments are not available here")
        if not c.index():
            return "Kelvin has no copy of the instrument collection yet; it fetches one when it can reach GitHub"
        if not experiment:
            found = c.experiments(query)
            if not found:
                return "no experiment in the collection fits %r; experiment_guide() lists them all" % query
            lines = ["Experiments in Kelvin's instrument collection%s:" % (" for %r" % query if query else "")]
            for e in found[:12 if query else 60]:
                roles = "; ".join("%s: %s%s" % (r, " or ".join((v or {}).get("kind") or []),
                                                ", optional" if (v or {}).get("optional") else "")
                                  for r, v in (e.get("roles") or {}).items())
                summary = str(e.get("summary") or "").split(". ")[0].rstrip(".")
                lines.append("- %s: %s. %s. Roles: %s" % (e["id"], e.get("title"), summary, roles))
            lines.append("experiment_guide(experiment=<id>): its procedure and plans, with this lab's instrument names")
            return "\n".join(lines)
        try:
            doc = c.experiment(experiment)
        except KeyError as e:
            return e.args[0]
        lab = {n: i.card for n, i in self.lab.instruments.items() if i.enabled}   # switched off: not part of it

        def kind_of(card_id: str) -> str:
            if card_id in self.lab.registry:
                return self.lab.registry.get(card_id).kind
            return str((c.instrument(card_id) or {}).get("kind") or "")
        a = coll.adapt(doc, lab, kind_of)
        lines = ["%s (%s), from Kelvin's instrument collection at %s" % (doc.get("title"), experiment,
                                                                         str(c.state().get("commit"))[:12]),
                 " ".join(str(doc.get("summary") or "").split()), "Roles:"]
        for r in a["roles"]:
            lines.append("- %s (%s%s): %s Here: %s" % (r["role"], " or ".join(r["kinds"]),
                                                       ", optional" if r["optional"] else "", r["does"],
                                                       ", ".join(r["here"]) or "nothing in this lab"))
        for key, title in (("setup", "Setup"), ("procedure", "Procedure")):
            if doc.get(key):
                lines += [title + ":"] + ["- " + str(s) for s in doc[key]]
        if a["plans"]:
            renamed = ", ".join("%s -> %s" % kv for kv in a["names"].items() if kv[0] != kv[1])
            lines.append("Plans, written for the collection's %s%s%s:" % (
                doc.get("lab") or "lab config", "; instrument names changed to this lab's: " + renamed if renamed else "",
                "; not in this lab: %s (leave out what they do, or ask the person)" % ", ".join(a["missing"])
                if a["missing"] else ""))
            for i, p in enumerate(a["plans"], 1):
                lines.append("%d. %s" % (i, p["title"]))
                lines.append(yaml.safe_dump(p["plan"], sort_keys=False, default_flow_style=None, width=1000,
                                            allow_unicode=True).rstrip())   # one line per section, never wrapped
                lines += ["   check: " + x for x in p["problems"]]
        for key, title in (("analysis", "Analysis"), ("pitfalls", "Pitfalls"), ("safety", "Safety")):
            if doc.get(key):
                lines += [title + ":"] + ["- " + str(s) for s in doc[key]]
        refs = [r for r in doc.get("references") or [] if isinstance(r, dict)]
        if refs:
            lines += ["References:"] + ["- %s%s" % (r.get("title"), " (doi:%s)" % r["doi"] if r.get("doi") else
                                                    " (%s)" % r["url"] if r.get("url") else "") for r in refs]
        lines.append("These plans are a starting point: fit the values to this sample and the lab's limits, then run "
                     "them with run_plan as usual (the same checks and approvals apply). A temperature or field set "
                     "point in setup: or on a stepped sweep waits until it gets there and measures nothing on the "
                     "way; to measure while it ramps (R(T) on a cool-down), sweep it with ramp:.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ literature

    def search_papers(self, query: str, limit: int = 8, since_year: Optional[int] = None) -> str:
        if not self.literature.enabled:
            return "literature search is switched off in Settings"
        try:
            papers, problems = self.literature.search(query, limit, since_year)
        except ValueError as e:
            return "not searched: %s" % e
        lines = [p.line(i) for i, p in enumerate(papers, 1)] or ["no papers found for %r" % query]
        lines += ["note: " + p for p in problems]
        if papers:
            lines.append("read one with read_paper(paper=<id>); cite as [Author Year](url)")
        return "\n".join(lines)

    def read_paper(self, paper: str, start: int = 0, max_chars: int = 12000) -> str:
        if not self.literature.enabled:
            return "literature search is switched off in Settings"
        try:
            return self.literature.read(paper, start, max_chars)
        except KeyError as e:
            return "not found: %s" % (e.args[0] if e.args else e)
        except Exception as e:
            return "could not read it: %s" % (str(e).splitlines()[0][:200] if str(e) else type(e).__name__)

    # ------------------------------------------------------------------ reports

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    def pick_runs(self, runs: Optional[List[str]] = None, day: str = "", last: int = 0) -> List[str]:
        """Run ids, oldest first: the ones named, a day's, or the last N (reports.pick_runs); only those the viewer of
        this thread may see (one named that it may not see is not found)."""
        return self.picked(runs, day, last)[0]

    def picked(self, runs: Optional[List[str]] = None, day: str = "", last: int = 0) -> Tuple[List[str], int]:
        """pick_runs, and how many runs of unshared work a day's or the latest left out."""
        if _VIEWER.get() is None:
            return pick_runs(self.runs, runs, day, last), 0
        if runs:
            for r in runs:
                if self.runs.find(r) is not None:
                    self._run_dir(r)   # KeyError: unshared work this viewer may not see
            return pick_runs(self.runs, runs), 0
        every = pick_runs(self.runs, None, day) if day else pick_runs(self.runs, last=10 ** 9) if last else []
        kept: List[str] = []
        cache: Dict[Any, Any] = {}
        for r in reversed(every):   # the newest first, as the last N are
            if last and not day and len(kept) >= last:
                break
            d = self.runs.find(r)
            if d is not None and self.may_see(self._run_marks(d), cache):
                kept.append(r)
        seen = (every.index(kept[-1]) if kept else 0) if last and not day else 0
        return kept[::-1], len(every) - seen - len(kept)

    def report(self, run_ids: List[str], title: str = "", summary: str = "", fmt: str = "pdf",
               notes: Any = (), by: str = "human", references: Any = (), folder: Optional[Path] = None,
               name: str = "", log: bool = True, pages: int = 0) -> List[Dict[str, Any]]:
        """Write a report of these runs (oldest first) as PDF and/or PowerPoint into reports/ (or folder, as
        name.pdf / name.pptx). Reads only files: it never talks to an instrument, so it can run while a plan runs.
        pages: at most this many pages (slides), as asked; it leaves out detail until it fits."""
        if not run_ids:
            raise ValueError("no runs to report")
        fmts = ["pdf", "pptx"] if fmt == "both" else [fmt]
        if any(f not in ("pdf", "pptx") for f in fmts):
            raise ValueError("format must be pdf, pptx or both")

        def columns(run_id: str) -> Optional[Dict[str, List[Dict[str, str]]]]:
            try:
                return self._columns(rundata.load(self._run_dir(run_id), with_rows=False))
            except Exception:   # the run's instruments are not in this lab any more
                return None

        metas = [self._run_meta(r) for r in run_ids]
        labs = sorted({m["lab"] for m in metas if m.get("lab")})
        lab_name = ", ".join(labs) if labs else self.lab.config.name
        simulated = all(m.get("simulated", True) for m in metas) if labs else \
            all(i.simulated for i in self.lab.instruments.values())
        report = Collector(self.runs, self.lab, columns, self.gate.log.events, event_line).report(
            run_ids, title.strip()[:120], summary.strip()[:4000], notes, lab_name, simulated, references)
        target = folder or self.reports_dir / "custom"
        target.mkdir(parents=True, exist_ok=True)
        base = name or "%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"),
                                  re.sub(r"[^\w-]+", "_", report.title).strip("_")[:48] or "report")
        out = []
        for f in fmts:
            path = target / ("%s.%s" % (base, f))
            n = write_pdf(report, path, pages) if f == "pdf" else write_pptx(report, path, pages)
            try:
                shown = path.relative_to(self.reports_dir).as_posix()
            except ValueError:
                shown = path.name
            out.append({"name": shown, "path": str(path), "size": path.stat().st_size, "format": f,
                        "pages" if f == "pdf" else "slides": n})
            if log:   # a run's own automatic report is part of the run, not an event in the lab record
                self.gate.log.write("report", by=by, name=shown, runs=len(run_ids))
        return out

    def _auto_report(self, run_id: Optional[str]) -> List[str]:
        """Every finished or stopped run goes into the catalog and gets its scan report in its own folder
        (report.pdf, report.pptx), and its device and project reports are rewritten a little later, unless the
        person switched automatic reports off. A report that cannot be written never fails the run."""
        if run_id:
            try:
                self.catalog.add(self._run_dir(run_id))
            except Exception:
                pass   # the catalog catches up at its next look at the run folders
            try:   # data.csv (and each trace's CSV) in the run's folder, always, as Settings says
                from ..export import write_csv
                write_csv(self._run_dir(run_id))
            except Exception as e:
                self.gate.log.write("export_failed", by="kelvin (automatic)", run=run_id, error=str(e)[:300])
        if not run_id or self.auto_report == "off":
            return []
        out: List[str] = []
        if "scan" in self.report_kinds:
            try:
                out = self.write_scan_report(run_id)
            except Exception as e:
                self.gate.log.write("report_failed", by="kelvin (automatic)", run=run_id, error=str(e)[:300])
        self._mark_dirty(run_id)
        return out

    def _formats(self, fmt: Optional[str] = None) -> List[str]:
        fmt = fmt or (self.auto_report if self.auto_report != "off" else "both")
        return ["pdf", "pptx"] if fmt == "both" else [fmt]

    def _collector(self) -> Collector:
        def columns(run_id: str) -> Optional[Dict[str, List[Dict[str, str]]]]:
            try:
                return self._columns(rundata.load(self._run_dir(run_id), with_rows=False))
            except Exception:   # the run's instruments are not in this lab any more
                return None
        return Collector(self.runs, self.lab, columns, self.gate.log.events, event_line)

    def _write_doc(self, doc: Any, base: Path, fmt: Optional[str] = None) -> List[str]:
        base.parent.mkdir(parents=True, exist_ok=True)
        out = []
        for f in self._formats(fmt):
            path = base.with_name(base.name + "." + f)
            (write_document_pdf if f == "pdf" else write_document_pptx)(doc, path)
            out.append(path.name)
        return out

    def write_scan_report(self, run_id: str, fmt: Optional[str] = None) -> List[str]:
        """The scan report, on its fixed outline, in the run's own folder: report.pdf and/or report.pptx."""
        d = self._run_dir(run_id)
        dg = report_kinds.digest(d)
        if dg is None:
            raise KeyError("run %s has not finished" % run_id)
        part = self._collector().run(run_id, self.gate.log.events())
        files = {p.name for p in d.iterdir() if p.is_file()} | {"report." + f for f in self._formats(fmt)}
        doc = report_kinds.scan_document(part, dg, files, dg.lab or self.lab.config.name, dg.simulated)
        return self._write_doc(doc, d / "report", fmt)

    # ------------------------------------------------------------------ device and master reports

    def _mark_dirty(self, run_id: str) -> None:
        if not ({"device", "project"} & self.report_kinds):
            return
        with self._report_lock:
            self._dirty_runs.add(run_id)
            if self._report_timer is None:
                self._report_timer = threading.Timer(self.report_delay, self.flush_reports)
                self._report_timer.daemon = True
                self._report_timer.start()

    def flush_reports(self) -> Dict[str, List[str]]:
        """Rewrite now the device and master reports that new runs have made out of date."""
        with self._report_lock:
            runs, self._dirty_runs = set(self._dirty_runs), set()
            if self._report_timer is not None:
                self._report_timer.cancel()
                self._report_timer = None
        if not runs:
            return {}
        try:
            owners = self._run_owners()
            return self.refresh_reports({owners.get(r) for r in runs})
        except Exception as e:
            self.gate.log.write("report_failed", by="kelvin (automatic)", runs=len(runs), error=str(e)[:300])
            return {}

    def _digests(self) -> List[report_kinds.Digest]:
        out = []
        for d in self.runs.iterdir():
            dg = report_kinds.digest(d)
            if dg is not None:
                out.append(dg)
        return sorted(out, key=lambda dg: dg.run_id)

    def _run_owners(self, digests: Optional[List[report_kinds.Digest]] = None) -> Dict[str, Optional[str]]:
        """run id -> project id (None: General). A run records its project; older runs belong to the project of
        the chat that ran them."""
        from_chats: Dict[str, Optional[str]] = {}
        for pid in self.project_ids():
            for rid in self.project_chat_runs(pid):
                from_chats.setdefault(rid, pid)
        return {d.run_id: (d.project if d.attributed else from_chats.get(d.run_id))
                for d in (digests if digests is not None else self._digests())}

    def project_folder(self, project_id: Optional[str]) -> Path:
        """reports/<project>/ (General for work outside every project); two projects with one name get their ids."""
        name = self.project_info(project_id).get("name") if project_id else "General"
        safe = report_kinds.safe_name(name or "General")
        clash = [p for p in self.project_ids() if p != project_id and
                 report_kinds.safe_name(self.project_info(p).get("name") if p else "General") == safe]
        return self.reports_dir / (safe + ("_" + project_id if clash and project_id else ""))

    def refresh_reports(self, projects: Optional[Iterable[Optional[str]]] = None,
                        fmt: Optional[str] = None) -> Dict[str, List[str]]:
        """Rewrite each project's master report, its device reports, index.csv and today's history copy.
        Returns {project folder name: files written, relative to reports/}."""
        digests = self._digests()
        owners = self._run_owners(digests)
        targets = list(dict.fromkeys(projects)) if projects is not None else list(self.project_ids())
        events = self.gate.log.events()
        collector = self._collector()
        written: Dict[str, List[str]] = {}
        for pid in targets:
            info = self.project_info(pid)
            ds = [d for d in digests if owners.get(d.run_id) == pid]
            folder = self.project_folder(pid)
            lab = next((d.lab for d in reversed(ds) if d.lab), self.lab.config.name)
            simulated = all(d.simulated for d in ds) if ds else all(i.simulated for i in self.lab.instruments.values())
            files: List[str] = []
            device_files: Dict[str, str] = {}
            devices = [dict(d) for d in info.get("devices") or []]
            known = {d["name"].lower() for d in devices}
            for name in sorted({d.device_name for d in ds if d.device_name and d.device_name.lower() not in known}):
                devices.append({"id": None, "name": name, "material": "", "description": ""})   # named only in plans
            if "device" in self.report_kinds:
                for dev in devices:
                    mine = [d for d in ds if (dev.get("id") and d.device == dev["id"]) or
                            (d.device_name and d.device_name.lower() == dev["name"].lower())]
                    if dev.get("retired") and not mine:
                        continue
                    latest = {}
                    for plan in {d.plan for d in mine}:
                        done = [d for d in mine if d.plan == plan and d.ok]
                        if done:
                            latest[plan] = collector.run(max(done, key=lambda d: d.started).run_id, events)
                    doc = report_kinds.device_document(dev, info.get("name") or "General", mine, latest,
                                                       self._mounts(events, dev["name"]), lab, simulated)
                    base = folder / "devices" / report_kinds.safe_name(dev["name"])
                    names = self._write_doc(doc, base, fmt)
                    device_files[dev["name"]] = "devices/" + names[0]
                    files += ["devices/" + n for n in names]
            if "project" in self.report_kinds:
                latest_any = {}
                for plan in {d.plan for d in ds}:
                    done = [d for d in ds if d.plan == plan and d.ok]
                    if done:
                        last = max(done, key=lambda d: d.started)
                        latest_any[plan] = (collector.run(last.run_id, events), last)
                doc = report_kinds.project_document(dict(info, devices=devices), ds, latest_any, device_files, lab,
                                                    simulated)
                base = folder / (report_kinds.safe_name(info.get("name") if pid else "General") + "_master")
                names = self._write_doc(doc, base, fmt)
                files += names
                history = folder / "history"
                history.mkdir(exist_ok=True)
                today = datetime.now().strftime("%Y-%m-%d")
                for n in names:   # one dated copy per day, the last one of the day
                    (history / ("%s_master%s" % (today, Path(n).suffix))).write_bytes((folder / n).read_bytes())
            folder.mkdir(parents=True, exist_ok=True)
            report_kinds.write_index_csv(folder / "index.csv", ds)
            written[folder.name] = files + ["index.csv"]
        return written

    @staticmethod
    def _mounts(events: List[Dict[str, Any]], device: str) -> List[Tuple[str, str]]:
        out = []
        for e in events:
            if e.get("type") in ("mount", "unmount") and str(e.get("device", "")).lower() == device.lower():
                out.append((str(e.get("wall", ""))[:16].replace("T", " "),
                            "%s by %s" % ("mounted" if e["type"] == "mount" else "taken out", e.get("by", "?"))))
        return out

    def reports_tree(self) -> Dict[str, Any]:
        """The reports folder as the Reports browser shows it: projects (master, devices, history), queues, and
        reports made on request (custom, and any older ones at the top)."""
        root = self.reports_dir
        rel = lambda p: p.relative_to(root).as_posix()   # noqa: E731
        files = lambda d: sorted((p for p in d.iterdir() if p.suffix in (".pdf", ".pptx")),
                                 key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []   # noqa: E731
        projects = []
        ids = {self.project_folder(pid).name: pid or "general" for pid in self.project_ids()}
        for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in ("queues", "custom")) \
                if root.is_dir() else []:
            pid = ids.get(d.name)
            name = (self.project_info(pid).get("name") if pid and pid != "general" else None) or d.name.replace("_", " ")
            projects.append({"name": name, "folder": d.name, "project": pid,
                             "master": [rel(p) for p in files(d)],
                             "devices": [rel(p) for p in sorted(files(d / "devices"), key=lambda p: p.name)],
                             "history": [rel(p) for p in files(d / "history")][:20]})
        return {"projects": projects, "queues": [rel(p) for p in files(root / "queues")][:30],
                "custom": [rel(p) for p in files(root / "custom") + files(root)][:40]}

    def run_file(self, run_id: str, name: str) -> Path:
        """A file a person may open from a run's folder: its reports and its data (run.h5). KeyError otherwise."""
        if not re.fullmatch(r"[\w.-]+\.(pdf|pptx|h5)", name or "") or ".." in name:
            raise KeyError("no file %r in run %s" % (name, run_id))
        path = self._run_dir(run_id) / name
        if not path.is_file():
            raise KeyError("no file %r in run %s" % (name, run_id))
        return path

    def run_files(self, run_id: str) -> List[Dict[str, Any]]:
        d = self._run_dir(run_id)
        order = {"report.pdf": 0, "report.pptx": 1, rundata.RUN_FILE: 2}
        files = [p for p in d.iterdir() if p.is_file() and p.suffix in (".pdf", ".pptx", ".h5")]
        return [{"name": p.name, "size": p.stat().st_size} for p in sorted(files, key=lambda p: (order.get(p.name, 3), p.name))]

    def _run_meta(self, run_id: str) -> Dict[str, Any]:
        try:
            return rundata.meta(self._run_dir(run_id))
        except (KeyError, OSError, ValueError):
            return {}

    def make_report(self, runs: Optional[List[str]] = None, day: str = "", last: int = 0, title: str = "",
                    summary: str = "", fmt: str = "pdf", notes: Any = (), by: str = "agent",
                    references: Any = (), kind: str = "runs", device: str = "",
                    project: Optional[str] = None, pages: int = 0) -> str:
        """The make_report tool's answer. kind runs (default): the runs chosen, or today's, or else the latest.
        kind project: the project's master report, rewritten now; device: that sample's report; scan: a run's own."""
        if kind in ("project", "device"):
            if fmt not in ("pdf", "pptx", "both"):
                return "no report: format must be pdf, pptx or both"
            try:
                written = self.refresh_reports([project], fmt)
            except Exception as e:
                return "no report: %s" % e
            folder = self.project_folder(project).name
            files = written.get(folder, [])
            if kind == "device":
                want = report_kinds.safe_name(device).lower()
                files = [f for f in files if f.startswith("devices/") and Path(f).stem.lower() == want]
                if not files:
                    return "no report: no device %r here (devices: %s)" % (device, ", ".join(
                        d["name"] for d in self.project_info(project).get("devices") or []) or "none yet")
            else:
                files = [f for f in files if not f.startswith("devices/") and f != "index.csv"]
            lines = ["report: %s/%s" % (folder, f) for f in files]
            lines.append("rewritten in %s; it is kept up to date after every scan" % (self.reports_dir / folder))
            return "\n".join(lines)
        if kind == "scan":
            try:
                ids = self.pick_runs(runs, day, last) if (runs or day or last) else self.pick_runs(last=1)
                made = [(r, self.write_scan_report(r, fmt)) for r in ids[-5:]]
            except (KeyError, ValueError, OSError) as e:
                return "no report: %s" % (e.args[0] if e.args else e)
            return "\n".join("scan report: runs/%s/%s (open it from the run in the Runs pane)" % (r, f)
                             for r, files in made for f in files) or "no report: no runs yet"
        try:
            ids, left = self.picked(runs, day, last)
            if not (runs or day or last):
                ids, left = self.picked(day="today")
                if not ids:
                    ids, left = self.picked(last=1)
        except (KeyError, ValueError) as e:
            return "no report: %s" % (e.args[0] if e.args else e)
        if not ids:
            return "no report: no runs %s%s" % ("on %s" % day if day else "yet", " (%s)" % not_shown(left)
                                                 if left else "")
        try:
            files = self.report(ids, title, summary, fmt, notes, by, references, pages=max(0, int(pages or 0)))
        except (ValueError, OSError) as e:
            return "no report: %s" % e
        lines = ["report: %s (%d run%s, %s)" % (f["name"], len(ids), "" if len(ids) == 1 else "s",
                                                "%d pages" % f["pages"] if "pages" in f else "%d slides" % f["slides"])
                 for f in files]
        if left:   # unshared work of another project or chat is never mixed in
            lines.append("left out: %s" % unshared_runs(left))
        lines.append("saved in %s; the user can open it from the chat" % self.reports_dir)
        return "\n".join(lines)

    def reports_index(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Reports made on request or by a queue, newest first (projects' reports are in reports_tree)."""
        d = self.reports_dir
        files = [p for sub in (d / "custom", d / "queues", d) if sub.is_dir() for p in sub.iterdir()
                 if p.suffix in (".pdf", ".pptx")]
        files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        return [{"name": p.relative_to(d).as_posix(), "size": p.stat().st_size, "format": p.suffix[1:]}
                for p in files[:limit]]

    def report_path(self, name: str) -> Path:
        """A report file by its path inside reports/ (e.g. custom/x.pdf, Graphene/devices/D3.pdf); KeyError for
        anything that is not one."""
        name = name or ""
        parts = name.split("/")
        if not re.fullmatch(r"[\w.-]+\.(pdf|pptx)", parts[-1]) or len(parts) > 3 or \
                any(not re.fullmatch(r"[\w.-]+", p) or p.startswith(".") for p in parts):
            raise KeyError("no report %r" % name)
        path = self.reports_dir.joinpath(*parts)
        if not path.is_file():   # a report a lab made before it switched folders (labfolders.py): still found
            from .. import labfolders
            earlier = labfolders.all_folders(self.home) if self.home is not None else []
            path = next((p for p in (f / "reports" / Path(*parts) for f in earlier) if p.is_file()), None)
            if path is None:
                raise KeyError("no report %r" % name)
        return path

    def _follow_lab_folder(self, lab: Lab, info: Dict[str, Any], by: str) -> None:
        """Switching labs, where nobody chose a data folder: the lab's own folder takes the runs, the record, the
        reports and the queue from now on (labfolders.py). Nothing moves: the earlier folder's runs are still found
        by their ids, and its reports by their names (report_path)."""
        if self.lab_folder is None:
            return
        try:
            new = self.lab_folder(lab, info)
        except Exception:
            return
        if new is None or Path(new).resolve() == self.data_dir.resolve():
            return
        try:
            new = writable_folder(new)
        except ValueError as e:   # the lab's folder can't be written: its data stay where they are, said in Settings
            self.data_problem = str(e)
            return
        old = self.data_dir
        later = RunFolders(new)
        for f in ([old / "runs"] if (old / "runs").is_dir() else []) + RunFolders(old).chat_folders():
            later.remember(f)
        self.gate.log.write("data_folder", by=by, moved_to=str(new), note="%s keeps its data there; what is here "
                            "stays here" % lab.config.name)
        self.data_dir = new
        frames.FOLDER = new / "frames"
        pulses.use_folder(new / "sequences")
        self.events_path = new / "events.jsonl"
        self.queue = ScanQueue(new / "queue.json")   # each lab's queue in its own folder (the old one stays there)
        if self.mounted:
            (new / "mounted.json").write_text(json.dumps(self.mounted, indent=1), encoding="utf-8")
        self._save_alarms()
        self._new_log().write("data_folder", by=by, previous=str(old), lab=lab.config.name)
        self.tidy_later()

    # ------------------------------------------------------------------ watching instruments

    def _new_monitor(self) -> Monitor:
        monitor = Monitor(self.lab, self.gate, self._monitor_said)
        for w in self.lab.config.watches:
            monitor.add(Watch(w["read"], w["above"], w["below"], w["for"], w["every"], list(w["then"]),
                              w["stop_run"], "lab", w["note"]))
        return monitor

    def add_watch(self, key: str, above: Optional[float] = None, below: Optional[float] = None, for_s: float = 0.0,
                  note: str = "", source: str = "person") -> Dict[str, Any]:
        """A notify-only watch (actions on alarm are for the watches written in the lab config)."""
        spec = self._spec(key)
        if spec is None:
            raise KeyError("%r is not a parameter in this lab" % key)
        if not spec.numeric or spec.get_cmd is None:
            raise ValueError("%s is not a numeric reading, so it has no range to watch" % key)
        w = self.monitor.add(Watch(key, above, below, max(0.0, float(for_s)), 10.0, [], False, source, note[:200]))
        whose = self._whose_now()
        if whose:   # added by unshared work: its note is that work's (phones through the Kelvin hub go without it)
            self.unshared_watches[w.id] = whose
        self._save_watches()
        view = next(v for v in self.monitor.list() if v["id"] == w.id)
        self.gate.log.write("watch", by=source, change="added", watch=w.id, rule=view["rule"])
        return view

    def remove_watch(self, watch_id: str, source: str = "person") -> None:
        w = self.monitor.remove(watch_id, source)   # watches from the lab config change only there
        self.unshared_watches.pop(w.id, None)
        self._save_watches()
        self.gate.log.write("watch", by=source, change="removed", watch=w.id, rule=w.rule())

    # ------------------------------------------------------------------ watches and alarms outlast a restart

    WATCHES = "watches.json"   # the person's and the agent's watches, by lab, next to the queue
    ALARMS = "alarms.json"     # the latest alarms, on or cleared

    def _escalate(self, request: Dict[str, Any]) -> bool:
        """A request nobody answered at the computer (WebApprover.escalate): it goes to people away from it as an
        alarm (the Kelvin app on a paired phone, email, a webhook) and waits for their answer, instead of being
        declined after a few minutes and ending the night's queue. False when Kelvin can reach nobody away from the
        computer: then it is declined, as before."""
        if not self.notifier.reaches_away("alarm"):
            return False
        until = time.strftime("%H:%M", time.localtime(request.get("until") or time.time()))
        self.gate.log.write("approval_escalated", by="kelvin", request=request.get("title"), until=until)
        self.tell("alarm", "Waiting for your answer: %s" % request.get("title"),
                  "Nobody answered at the Kelvin computer. Kelvin waits until %s for an answer from your phone (the "
                  "Kelvin app) or the computer; then it declines%s. Asked: %s" % (
                      until, ", and the queue pauses there" if self.queue.state in ("running", "pausing") else "",
                      "; ".join(request.get("items") or [])[:300]), bool(request.get("unshared")))
        return True

    def _monitor_said(self, kind: str, title: str, text: str = "") -> Any:
        """The monitor raised or cleared an alarm: kept with the alarm history, then told to people. One that says
        the note of a watch unshared work added is marked so: phones through the Kelvin hub get its reading, not its
        note."""
        self._save_alarms()
        notes = [w.note for i, w in list(self.monitor.watches.items()) if i in self.unshared_watches and w.note] \
            if getattr(self, "monitor", None) is not None else []
        return self.tell(kind, title, text, any(n in text for n in notes))

    def _save_alarms(self) -> None:
        try:
            _write_json(self.data_dir / self.ALARMS, {"alarms": [a.view() for a in list(self.monitor.alarms)[:50]]})
        except OSError:
            pass

    def _restore_alarms(self) -> List[Dict[str, Any]]:
        """The alarm history as Kelvin kept it, back in the monitor. One still on when Kelvin stopped can't be known
        to be on now: it shows as ended by the stop (its watch raises it again if it still holds). Returns those."""
        from ..monitor import Alarm
        try:
            data = json.loads((self.data_dir / self.ALARMS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        kept, was_on = [], []
        for v in (data.get("alarms") if isinstance(data, dict) else None) or []:
            try:
                a = Alarm("was-" + str(v["id"]).replace("was-", ""), str(v["kind"]), str(v["key"]), str(v["text"]),
                          float(v["raised"]), [str(x) for x in v.get("actions") or []],
                          float(v["cleared"]) if v.get("cleared") is not None else None, bool(v.get("acknowledged")))
            except (KeyError, TypeError, ValueError):
                continue
            if a.cleared is None:
                was_on.append(a.view())
                a.cleared, a.acknowledged = time.time(), True
                a.text += " (on when Kelvin stopped)"
            kept.append(a)
        self.monitor.alarms[:] = kept[:50]
        return was_on

    def _saved_watches(self) -> Dict[str, Any]:
        try:
            data = json.loads((self.data_dir / self.WATCHES).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data.get("labs") if isinstance(data, dict) and isinstance(data.get("labs"), dict) else {}

    def _save_watches(self) -> None:
        """This lab's person and agent watches, kept next to the queue (the lab's own are in its config)."""
        labs = self._saved_watches()
        labs[self.lab.config.name] = [dict({"read": w.key, "above": w.above, "below": w.below, "for": w.for_s,
                                            "every": w.every, "source": w.source, "note": w.note},
                                           **({"whose": self.unshared_watches[w.id]}
                                              if w.id in self.unshared_watches else {}))
                                      for w in list(self.monitor.watches.values()) if w.source != "lab"]
        try:
            _write_json(self.data_dir / self.WATCHES, {"labs": labs})
        except OSError:
            pass

    def _restore_watches(self) -> Dict[str, List[str]]:
        """The person's and agent's watches kept for this lab, watched again: each comes back unless one like it is
        on already; one whose reading the lab no longer has is dropped, and said so once (the restart's card)."""
        back, dropped = [], []
        have = {(w.key, w.above, w.below, w.for_s) for w in self.monitor.watches.values()}
        for v in self._saved_watches().get(self.lab.config.name) or []:
            try:
                key, for_s = str(v["read"]), max(0.0, float(v.get("for") or 0))
                if (key, v.get("above"), v.get("below"), for_s) in have:
                    continue
                w = self.monitor.add(Watch(key, v.get("above"), v.get("below"), for_s, float(v.get("every") or 10),
                                           [], False, str(v.get("source") or "person"), str(v.get("note") or "")[:200]))
                if isinstance(v.get("whose"), dict):   # unshared work's watch stays its own
                    self.unshared_watches[w.id] = v["whose"]
                have.add((key, w.above, w.below, for_s))
                back.append(w.rule(self.lab.resolve(key)[1].unit))
            except (KeyError, TypeError, ValueError) as e:
                dropped.append("%s (%s)" % (v.get("read") if isinstance(v, dict) else v, e.args[0] if e.args else e))
        if dropped:
            self._save_watches()
        self.restored = {"back": back, "dropped": dropped}
        return self.restored

    # ------------------------------------------------------------------ in the background: after a restart, and idle

    def start_watching(self) -> "LabRuntime":
        """What runs beside the agent: the monitor's watches; and every few seconds the check of what the last stop
        left (once, when the instruments are connected) and of outputs left on with nothing happening."""
        self.monitor.start()
        if self._watching is None:
            self._watching_stop.clear()
            self._watching = threading.Thread(target=self._keep_watch, name="kelvin-idle", daemon=True)
            self._watching.start()
        return self

    def stop_watching(self) -> None:
        self._watching_stop.set()
        self.monitor.stop()
        if self._watching is not None:
            self._watching.join(timeout=5)
            self._watching = None

    def _keep_watch(self) -> None:
        while not self._watching_stop.wait(BACKGROUND_S):
            try:
                self._incident_check()
                self._idle_check()
            except Exception:   # never the end of watching
                pass

    def _away(self, inst: Any, specs: Iterable[ParamSpec]) -> List[str]:
        """These drives of an instrument that are away from safe now, as read: ["smu.V 1 V", ...]."""
        out = []
        for spec in specs:
            if spec.get_cmd is None and spec.name not in inst._last:
                continue
            try:
                v = inst.read(spec.name)
                if v is not None and not isinstance(v, str) and spec.distance(v) > 0:
                    out.append("%s.%s %s" % (inst.name, spec.name, spec.show(v)))
            except Exception:
                continue
        return out

    def _outputs_on(self) -> Dict[str, List[str]]:
        """What applies something to the sample now (a source, an excitation, light, RF; not temperature, field or
        positions), read from the instruments: {instrument: its drives away from safe}. An instrument with an output
        switch (a card's enables, or what a level applies_when) is on when a switch is; one without, when any drive
        is away from safe. The caller holds the lock."""
        on: Dict[str, List[str]] = {}
        for name, inst in self.lab.instruments.items():
            if not inst.enabled or not protection.applies_to_sample(inst.card):
                continue
            named = {s.applies_when for s in inst.card.drives if s.applies_when}
            switches = [s for s in inst.card.drives if s.enables or s.name in named]
            if self._away(inst, switches or inst.card.drives):
                on[name] = self._away(inst, inst.card.drives)
        return on

    def _outputs_to_safe(self, names: Iterable[str], by: str) -> List[str]:
        lines: List[str] = []
        with self.gate.acting_as(by):
            for name in names:
                lines += [r.message for r in self.gate.to_safe(name)]
        self._refresh()
        return lines

    def _incident_check(self) -> None:
        """Once after Kelvin starts, when its instruments are connected: what the last stop left behind, on a card
        until a person dismisses it. Outputs on (sources, light, RF away from safe), runs and a queue it cut short,
        alarms that were on, watches it couldn't bring back; with them, the other drives away from safe. When the
        work it cut short ran unattended (a queue), the outputs go to safe at once; otherwise the card offers it."""
        if not self._incident_due or self.lab.connecting:
            return
        if not self.lock.acquire(timeout=1.0):
            return   # Kelvin is busy: next time
        try:
            self._incident_due = False
            self.tidy_runs()   # the runs a crash ended, found now if the start's tidy hasn't yet
            outputs = {} if self._acted else self._outputs_on()
            away = []   # temperature, field, positions: as they were (a person decides; going to safe could warm up)
            for inst in self.lab.instruments.values():
                if inst.enabled and not protection.applies_to_sample(inst.card):
                    away += self._away(inst, inst.card.drives)
            queue = [i.name for i in getattr(self.queue, "cut_short", [])]
            runs = list(self.interrupted)
            unattended = bool(queue)
            for run_id in runs:
                try:
                    unattended = unattended or bool(rundata.meta(self.runs / run_id).get("queue"))
                except (KeyError, OSError, ValueError):
                    pass
            alarms = [a["text"] for a in self.alarms_before]
            if not (outputs or runs or queue or alarms or self.restored["dropped"]):
                if self.restored["back"]:
                    self.notifier("alarm", "Kelvin restarted: your watches are back",
                                  "; ".join(self.restored["back"])[:300])
                return
            incident: Dict[str, Any] = {
                "at": datetime.now().isoformat(timespec="seconds"), "outputs": sorted(outputs),
                "on": sum((outputs[n] for n in sorted(outputs)), []), "away": away, "runs": runs, "queue": queue, "alarms": alarms, "watches": dict(self.restored),
                "unattended": unattended, "safe": []}
            if unattended and outputs:   # nobody was there when it stopped: what reaches the sample goes to safe now
                incident["safe"] = self._outputs_to_safe(sorted(outputs), "kelvin (after a restart)")
            self.incident = incident
            self.gate.log.write("incident", by="kelvin", **{k: v for k, v in incident.items() if v and k != "at"})
            self.notifier("alarm", "Kelvin restarted: check the lab", "; ".join(self.incident_lines())[:300])
        finally:
            self.lock.release()

    def incident_lines(self) -> List[str]:
        """The restart's card in words (lab_status, notices)."""
        i = self.incident
        if not i:
            return []
        lines = []
        if i["outputs"]:
            lines.append("left on: %s" % ", ".join(i.get("on") or i["outputs"]))
            if i["safe"]:
                lines.append("put to safe%s: %s" % (" at once (the work it cut short ran unattended)"
                                                     if i["unattended"] else "", "; ".join(i["safe"])))
        if i["away"]:
            lines.append("as they were (temperature, field, positions): %s" % ", ".join(i["away"]))
        if i["runs"]:
            lines.append("runs it cut short (kept up to their last point): %s" % ", ".join(i["runs"]))
        if i["queue"]:
            lines.append("the queue was running %s; it waits for a person to start it again" % ", ".join(i["queue"]))
        if i["alarms"]:
            lines.append("alarms that were on: %s" % "; ".join(i["alarms"]))
        if i["watches"].get("back"):
            lines.append("watches back: %s" % "; ".join(i["watches"]["back"]))
        if i["watches"].get("dropped"):
            lines.append("watches not back (this lab has no such reading): %s" % "; ".join(i["watches"]["dropped"]))
        return lines

    def incident_safe(self, by: str = "human") -> List[str]:
        """A person's answer to the restart's card: the outputs still on go to safe (as at Hands on: temperature,
        field and positions stay)."""
        with self.lock:
            on = self._outputs_on()
            lines = self._outputs_to_safe(sorted(on), by) if on else ["nothing that reaches the sample is on"]
        if self.incident is not None:
            self.incident["safe"] = lines
        self.gate.log.write("incident_safe", by=by, steps=lines)
        return lines

    def dismiss_incident(self, by: str = "human") -> None:
        if self.incident is not None:
            self.incident = None
            self.gate.log.write("incident_dismissed", by=by)

    def _idle_check(self) -> None:
        """Outputs left on with nothing happening for idle_off_s (no command, run or queue: a turn that died, a
        usage limit, a person who left). Light on the sample (lasers, light sources) goes to safe, and other outputs
        (sources, excitations, RF) too when idle_off_all; the rest are told to people, not changed (a gate held on
        purpose). Temperature, field and positions are never touched."""
        if not self.idle_off_s or time.monotonic() - self.last_active < self.idle_off_s:
            return
        if self.queue.state in ("running", "pausing") or self.setting_up or (self.live or {}).get("status") == "running":
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            on = self._outputs_on()
            off = sorted(n for n in on if self.idle_off_all or _light(self.lab.instruments[n].card))
            told = sorted(n for n in on if n not in off)
            idle = _dur(time.monotonic() - self.last_active)
            if off:
                lines = self._outputs_to_safe(off, "kelvin (idle %s)" % idle)
                self.gate.log.write("idle_off", by="kelvin", idle=idle, was=sum((on[n] for n in off), []),
                                    steps=lines)
                self.notifier("alarm", "Left on for %s with nothing happening: put to safe" % idle,
                              "; ".join(lines)[:300])
            if told:
                was = sum((on[n] for n in told), [])
                self.gate.log.write("idle_on", by="kelvin", idle=idle, still=was)
                self.notifier("alarm", "Still on after %s with nothing happening: %s" % (idle, ", ".join(told)),
                              "%s. Kelvin leaves them as they are; Stop, Hands on the sample or Settings → Switch "
                              "off outputs left idle" % "; ".join(was)[:250])
        finally:
            self.last_active = time.monotonic()   # once per idle stretch
            self.lock.release()

    def watches_text(self) -> str:
        lines = []
        for v in self.monitor.list():
            state = "ALARM" if v["alarm"] else "ok"
            value = "" if v["value"] is None else " (now %s)" % self._spec(v["key"]).show(v["value"])
            lines.append("%s [%s, %s] %s%s%s" % (v["id"], v["source"], state, v["rule"], value,
                                                 " -> " + "; ".join(v["then"]) if v["then"] else ""))
        for a in self.monitor.active():
            lines.append("active alarm %s: %s" % (a.id, a.text))
        return "\n".join(lines) or "nothing is watched"

    def mark_reviewed(self, by: str = "human") -> List[str]:
        """A person reviewed the limits of the instruments Kelvin added (their lab config has no marks now, and
        nothing else changed): from now on they count as the person's, with nothing to reconnect. Only the portal
        calls this; no agent tool can."""
        with self.lock:
            names = self.lab.config.unreviewed()
            for ic in self.lab.config.instruments.values():
                ic.added_by = ""
            self.lab.config.observe_only = False
            self.gate.log.write("lab_reviewed", by=by, instruments=names)
            return names

    def switch_lab(self, lab: Lab, envelope: Optional[Envelope], info: Dict[str, Any], by: str = "human",
                   idle: Optional[Callable[[], bool]] = None) -> List[str]:
        """Control another lab (the person's lab config, saved or changed). Refused while a plan runs. The approval
        mode goes back to asking, and the new lab is health-checked right away.
        A lab opened before connecting (build_lab later=True) takes over the connections of the instruments it has
        unchanged (same address, card, connection and names), and the old lab's other instruments are closed before
        any of the new lab's is opened: a camera or a maker's SDK takes one connection at a time (a pylon camera was
        "exclusively opened by another client", an Andor SDK shut down under the new connection). idle: the switch
        waits for the next moment Kelvin is idle (it says so, and no queue runs), so the lab never changes under an
        answer in progress (the first-run setup's switch once raced the person's first message)."""
        held = self._until_idle(idle) if idle is not None else False
        try:
            return self._switch_lab(lab, envelope, info, by)
        finally:
            if held:
                self.lock.release()

    def _until_idle(self, idle: Callable[[], bool], wait_s: float = 6 * 3600.0) -> bool:
        """Wait for Kelvin to be idle (idle(), no run, no queue) with the lab to itself: True, holding the lock."""
        end = time.monotonic() + wait_s
        while True:
            if idle() and self.queue.state not in ("running", "pausing") and self.lock.acquire(timeout=0.5):
                if idle() and not (self.live or {}).get("status") == "running":
                    return True
                self.lock.release()
            if time.monotonic() > end:
                raise RuntimeError("Kelvin was not idle for %s; switch to your instruments in Lab setup" % _dur(wait_s))
            time.sleep(0.5)

    @staticmethod
    def _carry_over(old: Lab, new: Lab) -> List[str]:
        """The instruments a lab opened before connecting has unchanged from the lab in use: their connections move
        to it, unclosed, with what Kelvin set on them. Returns their names."""
        from ..instrument import Unavailable
        from ..lab import CONNECTING
        kept = []
        for name, inst in new.instruments.items():
            was = old.instruments.get(name)
            if inst.unavailable != CONNECTING or was is None or not was.enabled or was.simulated:
                continue
            a, b = was.config, inst.config
            if (a.address, a.card, a.connection, a.vars) != (b.address, b.card, b.connection, b.vars):
                continue
            inst.backend, was.backend = was.backend, Unavailable(name, "moved to %s" % new.config.name)
            inst._last = dict(was._last)
            kept.append(name)
        return kept

    def _switch_lab(self, lab: Lab, envelope: Optional[Envelope], info: Dict[str, Any], by: str) -> List[str]:
        with self.lock:
            if self.live and self.live.get("status") == "running":
                raise RuntimeError("a plan is running; wait for it to finish or stop it first")
            old = self.lab
            kept = self._carry_over(old, lab) if old is not lab else []
            if old is not lab:
                old.close()   # the old lab's own connections, before the new lab opens its own
            for name in lab.connecting:   # one that can't be connected is set aside, with why
                try:
                    lab.reconnect(name)
                except Exception:
                    pass
            self.lab = lab
            self._follow_lab_folder(lab, info, by)
            self.gate = SafetyGate(lab, self.approver, self._new_log(lab.clock), envelope, operator="agent")
            self.engine = RunEngine(self.gate, self._store())
            self.engine.progress = self._on_progress
            self._apply_protection(recheck=True)
            self.lab_info = dict(info)
            running = self.monitor._thread is not None
            self.monitor.stop()
            alarms = self.monitor.alarms
            self.monitor = self._new_monitor()   # watches name instruments of the old lab; start again
            self.monitor.alarms[:] = alarms      # the history stays
            self._restore_watches()              # the ones kept for this lab come back
            if running:
                self.monitor.start()
            self._status = {}
            with self._readings_lock:
                self._readings = {}   # the old lab's: an instrument of the same name may be another one
            self._refresh()
            self.gate.log.write("lab_changed", by=by, lab=lab.config.name, instruments=sorted(lab.instruments),
                                kind=info.get("kind"), kept=kept or None, set_aside=info.get("set_aside") or None)
            results = [str(r) for r in self.engine.preflight()]
        return results

    def scan(self) -> str:
        """Which instruments are connected, matched to cards. Read-only: it only asks who they are."""
        def run() -> str:
            from ..adapters import AdapterError
            from ..discovery import scan_survey
            from ..survey import reason
            in_lab = {ic.address: name for name, ic in self.lab.config.instruments.items()
                      if not ic.address.startswith("sim://")}
            try:
                found, looked = scan_survey(self.lab.registry, in_lab=in_lab, network=self.scan_network,
                                            catalog=self.collection)
            except AdapterError as e:
                return str(e)
            self._remember_scan(found, looked)
            if not found:
                return "\n".join([reason(found, looked)] + looked.lines())
            lines = [f.line() for f in found]
            lines.append("To use one the person asked for: add_instrument (one with no card: draft_card first).")
            return "\n".join(lines)
        return self._do("scanning for instruments", run)

    # ------------------------------------------------------------------ approval mode (people only)

    def approval_mode(self) -> Dict[str, Any]:
        """The current mode, and what auto mode would approve in this lab, in words for the person choosing."""
        cfg = self.lab.config
        never, ranges = [], []
        for name, ic in cfg.instruments.items():
            card = self.lab.instruments[name].card
            for param, rule in ic.auto.items():
                key = name if param == "*" else "%s.%s" % (name, param)
                if rule == "never":
                    never.append(key)
                elif isinstance(rule, Allowed):
                    ranges.append("%s %s" % (key, rule.show(card.params[param])))
        rule = ("drive changes, runs and queues go ahead without asking when every drive stays within %.0f%% of the way "
                "from its safe value to its lab limit and switches move at most one step from safe; runs and queues "
                "also need a clean dry run. Instruments Kelvin isn't sure about, code on boards, switching an "
                "instrument on still ask (what drives nothing, such as installs, "
                "skills and saved files, never waits for you in either mode)"
                % (100 * cfg.auto_fraction))
        if ranges:
            rule += ". Own auto ranges: " + ", ".join(ranges)
        if never:
            rule += ". Never automatic: " + ", ".join(never)
        return {"mode": "auto" if self.gate.auto else "ask", "fraction": cfg.auto_fraction, "rule": rule + "."}

    def set_approval_mode(self, mode: str, by: str = "human", pin: Any = None) -> Dict[str, Any]:
        """Only the portal and the approval page call this; no agent tool can. It resets to ask on every start.
        Turning auto mode on needs the owner's PIN when one is set (PermissionError); back to asking never does.
        With roles, it is not for a trainee (accounts.LabRoles), and the owner is told when it goes on."""
        if mode not in ("ask", "auto"):
            raise ValueError("mode must be ask or auto")
        if mode == "auto" and not self.gate.auto:
            self.not_for_trainees("Auto mode")
            self.owner_check(pin)
        if (mode == "auto") != self.gate.auto:
            self.gate.auto = mode == "auto"
            self.gate.log.write("approval_mode", by=by, mode=mode)
            if mode == "auto":
                self.audit_event("auto mode on", by)
        return self.approval_mode()

    def ui_state(self) -> Dict[str, Any]:
        # keep the panel live, beside whatever else Kelvin does: never the lab's lock, and an instrument the work in
        # progress uses (a run's) shows its latest values
        if time.time() - self._status_at > 2.0:
            self._refresh(PANEL_WAIT_S)
        status = {}
        for name, values in dict(self._status).items():
            inst = self.lab.instruments.get(name)
            if inst is None:   # the lab was loaded again with it renamed or taken out: its last readings go
                continue
            row = []
            for pname, v in values.items():
                spec = inst.card.params.get(pname)
                if spec is None:
                    continue
                bad = isinstance(v, str) and v.startswith("ERROR")
                row.append({"name": pname, "value": "ERROR" if bad else spec.show(v),
                            "away": (not bad) and spec.is_drive and spec.distance(v) > 0})
            status[name] = row
        pending = getattr(self.approver, "pending_list", lambda: [])()
        history = getattr(self.approver, "history_list", lambda: [])()
        return {"lab": self.lab.config.name,
                "simulated": all(i.simulated for i in self.lab.instruments.values()),
                "busy": self.busy, "pending": pending, "history": history, "status": status, "queue": self.queue_view(),
                "mounted": self.mounted_view(),
                "hands_on": self.hands,
                "approval_mode": "auto" if self.gate.auto else "ask", "approval_rule": self.approval_mode()["rule"],
                "lab_info": self.lab_info,
                "unavailable": self._unavailable_view(),
                "unreviewed": self.lab.config.unreviewed(), "setup": self.setup_status, "survey": self.last_survey,
                "drafts": sorted(n for n, i in self.lab.instruments.items()
                                 if not i.simulated and i.card.status == "draft"),
                "watches": self.monitor.list(),
                "alarms": [a.view() for a in self.monitor.alarms[:10]],
                "incident": dict(self.incident, lines=self.incident_lines()) if self.incident else None,
                "notices": list(self.notifier.notices)[-20:],
                "instruments": self.instruments(),
                "live": {k: v for k, v in (self.live_view(10 ** 9) or {}).items()
                         if k in ("run_id", "plan", "done", "total", "eta_s", "status")} or None,
                "events": [event_line(e) for e in reversed(tail_events(self.events_path, 25))]}
