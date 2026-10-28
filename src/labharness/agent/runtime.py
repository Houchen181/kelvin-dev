"""What the agent can do, as short plain-text answers (feature #9: concise).

All instrument access goes through the safety gate and one lock, so an
agent call and a plan never talk to an instrument at the same time. The approval page reads cached state and never waits on the lock.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import math
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import yaml

from .. import frames
from ..cards import ParamSpec, num, plot_points, thin, trace_x
from ..engine import RunEngine, RunReport
from ..eventlog import EventLog, RunFolders, RunStore
from ..gate import ApprovalRequest, Approver, SafetyGate, _dur
from ..lab import Lab
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
BUSY_WAIT_S = 30.0   # how long a request waits for the lab while another holds it, before it answers "busy"
MAX_CONFIG_CHARS = 30000   # of the lab config shown at once; an instrument's own entry is always shown whole
SETUP_STEPS = ("connecting", "scanning", "drafting", "reading")   # the instrument setup at work (autosetup.py)
SCRIPTS_NEXT = ("Next, with the person (nothing above was sent to any instrument): add each instrument that is not in "
                "the lab yet with add_instrument at its address, with the likely card and the connection settings "
                "above (Kelvin checks who answers there first); one with no card fitting: draft_card, saying which "
                "commands the scripts use, as those are the ones this lab relies on. The limits above come from the "
                "values the scripts use: propose them and set them only once the person agrees. Then offer to keep "
                "each measurement script as a recipe (save_recipe; it shows in the chat): its loops become sweeps "
                "(an outer loop, the outer axis), its readings the measure list, its waits the settle times, its "
                "setup and its end the setup and after steps; say what it can't carry over (the script's own "
                "processing, plotting). Never run the scripts.")


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
        text = "%s%s: %s" % (e.get("answer"), " (%s)" % e["note"] if e.get("note") else "",
                             lines[1].strip(" -") if len(lines) > 1 else (lines[0] if lines else ""))
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
            "; declined %d open request(s)" % e["declined"] if e.get("declined") else "")
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


class LabRuntime:
    def __init__(self, lab: Lab, approver: Approver, data_dir: Any, envelope: Optional[Envelope] = None):
        self.lab = lab
        self.approver = approver
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        frames.FOLDER = self.data_dir / "frames"   # camera frames read outside a run
        self.events_path = self.data_dir / "events.jsonl"
        self.user: Optional[str] = None             # the person signed in to the portal (accounts.py), if any
        self.shared_log: Optional[Path] = None      # the lab's instrument log, everyone's events; set by the app
        self.operating: Optional[str] = None        # Claude Code bridge: the task Claude is doing on the screen now
        self.gate = SafetyGate(lab, approver, self._new_log(lab.clock), envelope, operator="agent")
        self.engine = RunEngine(self.gate, self._store())
        self.engine.progress = self._on_progress
        self.lock = threading.RLock()
        self._client = threading.local()   # the MCP client's call this thread works for (ClientCall), if any
        self._holder: Optional["ClientCall"] = None   # the client's call that has the lab now, if one has
        self._script_cards: Optional[Tuple[Any, Any]] = None   # (the lab's cards folder as it was, the registry)
        self.busy: Optional[str] = None
        self._status: Dict[str, Dict[str, Any]] = {}
        self._status_at = 0.0
        self.live: Optional[Dict[str, Any]] = None   # the run in progress (or the last one), for live displays
        self._catalog: Optional["Catalog"] = None    # every run in the data folder (catalog.py); see .catalog
        self._tidy_lock = threading.Lock()
        self._live_lock = threading.Lock()
        self.lab_info: Dict[str, Any] = {"kind": "virtual" if all(i.simulated for i in lab.instruments.values()) else "file",
                                         "path": None, "error": None, "has_file": False}
        self.notifier = Notifier()
        self.literature = Literature()
        self.cards_dir: Optional[Path] = None   # the user's own cards (~/Kelvin/cards); set by the app
        self.home: Optional[Path] = None        # ~/Kelvin, where each instrument has its folder; set by the app
        self.collection: Any = None             # the instrument collection (collection.py) while it is switched on
        self.setup_status: Dict[str, Any] = {"state": "idle"}   # setting up connected instruments (autosetup.py)
        self.data_problem: Optional[str] = None   # why the chosen data folder is not in use, if it is not
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
        self.protection_missing: List[str] = []   # the mounted device's protection names what this lab lacks
        # the mounted device's caps that are not in the instruments yet: written when Kelvin is next idle
        self.caps_waiting = False
        self._apply_protection(recheck=True)
        self.attribution: Dict[str, Any] = {}       # the project of the chat whose turn this is (portal), and incognito
        self.running_meta: Dict[str, Any] = {}      # the running plan's project and marks (_attribute), while it runs
        # what Kelvin knows about a project (name, devices, notes, references, summary); the portal sets this
        self.project_info: Callable[[Optional[str]], Dict[str, Any]] = default_project_info
        self.project_ids: Callable[[], List[Optional[str]]] = lambda: [None]   # None is the General workspace
        self.project_chat_runs: Callable[[Optional[str]], List[str]] = lambda pid: []   # runs its chats started
        self.report_kinds = {"scan", "device", "project"}   # which reports Kelvin keeps up to date by itself
        self.report_delay = 20.0     # seconds after a run before its device and master reports are rewritten
        self._dirty_runs: set = set()
        self._report_timer: Optional[threading.Timer] = None
        self._report_lock = threading.Lock()
        self._queue_thread: Optional[threading.Thread] = None
        self.monitor = self._new_monitor()
        subscribe = getattr(approver, "subscribe", None)
        if subscribe:  # a person is needed: tell them, wherever they are
            subscribe(lambda r: None if r.get("auto") else self.notifier(
                "approval", "Approval needed: " + r["title"], (r.get("items") or [""])[0]))
        vetoes = getattr(approver, "vetoes", None)
        if vetoes is not None:   # a cancelled call asks nobody: no one is left to act on a yes
            vetoes.append(lambda: ClientCall.CANCELLED if getattr(getattr(self._client, "call", None), "cancelled",
                                                                  False) else None)
        with self.lock:
            self._refresh()

    def _on_progress(self, event: Dict[str, Any]) -> None:
        """Keep a light copy of the running plan's data (scalars only, latest trace) for live displays."""
        with self._live_lock:
            if event["event"] == "start":
                self.live = {"run_id": event["run_id"], "plan": event["plan"], "total": event["total"], "done": 0,
                             "eta_s": None, "status": "running", "phase": "setup", "axes": event["axes"],
                             "measures": event["measures"], "rows": [], "trace": None}
            elif self.live is None or self.live["run_id"] != event["run_id"]:
                return
            elif event["event"] == "point":
                row = event["row"]
                keep = {"i", "t"} | {a["key"] for a in self.live["axes"]} | \
                       {m["key"] for m in self.live["measures"] if m["kind"] == "scalar"}
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

    def _refresh(self) -> None:
        try:
            self._status = self.lab.snapshot()
            self._status_at = time.time()
        except Exception:
            pass

    def _do(self, what: str, fn: Callable[[], str], stoppable: bool = False) -> str:
        """fn with the lab to itself. stoppable: a call that changes the lab (a drive, a run, the queue) or waits on
        it; one still waiting for the lab when a person presses Stop, or when the client that made it cancels it,
        is not run (a Stop ends what is lined up behind the stopped work too, not only what was showing)."""
        gate, stops = self.gate, self.gate.stop_generation
        call = getattr(self._client, "call", None)   # the client's call this thread works for (agent/server.py)
        if call is not None:
            call.waiting = True
        try:
            if not self.lock.acquire(timeout=2.0):
                if self.queue.state in ("running", "pausing"):   # a queued scan may take hours: answer, don't wait
                    return ("Kelvin is running the queue (%s). Ask again when this item is done, or pause or stop the "
                            "queue." % (self.busy or "a scan"))
                if not self.lock.acquire(timeout=BUSY_WAIT_S):   # never a chat stuck behind an instrument that hangs
                    return ("Kelvin is still busy with %s: an instrument has not answered yet (a stop takes effect as "
                            "soon as it does). Ask again in a moment; if this keeps happening, check that "
                            "instrument's cable, power and address." % (self.busy or "an earlier request"))
        finally:
            if call is not None:
                call.waiting = False
        try:
            if call is not None and call.cancelled:
                return "not run: the client cancelled this call while it waited for the lab"
            if stoppable and gate.stop_generation != stops:
                return ("not run: a person pressed Stop while this waited for the lab. Ask them before trying it "
                        "again.")
            held, self._holder = self._holder, call or self._holder
            self.busy = what
            try:
                return fn()
            finally:
                self.busy, self._holder = None, held
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
        def run() -> str:
            self._refresh()
            sim = all(i.simulated for i in self.lab.instruments.values())
            lines = ["%s%s" % (self.lab.config.name, " (simulated)" if sim else "")]
            if self.gate.auto:
                lines.append("approvals: auto mode (a person turned it on): low-risk drive changes and runs go ahead "
                             "without asking; the rest still wait for a human")
            lines += ["ALARM: %s%s" % (a.text, " (%s)" % "; ".join(a.actions) if a.actions else "")
                      for a in self.monitor.active()]
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
        return self._do("reading status", run)

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
        out = ["mounted device %s is protected (a person set this; it can't be changed or passed): %s" % (
            m.get("device_name"), "; ".join(protection.summary(prot, self.lab)))]
        if self.protection_missing:
            out.append("this lab lacks %s, named in %s's protection" % (", ".join(self.protection_missing),
                                                                       m.get("device_name")))
        return out

    def _status_lines(self) -> List[str]:
        lines = []
        for name in self.lab.switched_off:
            lines.append("%s: switched off in the Lab panel; Kelvin sends it nothing (ask_to_switch_on asks the person)"
                         % name)
        for name, why in self.lab.unavailable.items():
            lines.append("%s: not connected (%s); Kelvin sends it nothing until it connects: fix that "
                         "(install_for_instrument for a missing package), then reconnect_instrument" % (name, why))
        for name, values in self._status.items():
            if name not in self.lab.instruments or not self.lab.instruments[name].enabled:
                continue
            card = self.lab.instruments[name].card
            parts = []
            for pname, v in values.items():
                spec = card.params[pname]
                if isinstance(v, str) and v.startswith("ERROR"):
                    parts.append("%s ERROR" % pname)
                    continue
                parts.append("%s %s%s" % (pname, spec.show(v), "*" if spec.is_drive and spec.distance(v) > 0 else ""))
            lines.append("%s: %s" % (name, ", ".join(parts)))
        return lines

    def describe(self, instrument: str) -> str:
        return self._do("describing " + instrument, lambda: self.gate.describe(instrument))

    def get(self, instrument: str, parameter: str) -> str:
        def run() -> str:
            r = self.gate.get(instrument, parameter)
            if r.ok and isinstance(r.value, list):
                return "%s.%s: %s" % (instrument, parameter, trace_summary(self._spec(r.target), r.value))
            return r.message
        return self._do("reading %s.%s" % (instrument, parameter), run)

    def set(self, instrument: str, parameter: str, value: Any) -> str:
        return self._do("setting %s.%s" % (instrument, parameter),
                        lambda: self.gate.set(instrument, parameter, value).message, stoppable=True)

    def raw(self, instrument: str, command: str) -> str:
        return self._do("raw command to " + instrument, lambda: self.gate.raw(instrument, command).message,
                        stoppable=True)

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
                for sub in ("runs", "reports"):
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
            self.queue.move_to(new / "queue.json")
            self.events_path = new / "events.jsonl"
            self.gate.log = self._new_log()
            self.engine.store = self._store()
            self.gate.log.write("data_folder", by=by, previous=str(old), moved=moved, kept=len(kept))
            self.tidy_later()
            return dict(self.data_summary(), moved=moved, kept=kept, previous=str(old))
        finally:
            self.lock.release()

    def _new_log(self, clock: Any = None) -> EventLog:
        return EventLog(self.events_path, clock or self.lab.clock, shared=self.shared_log, user=self.user)

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
            self.events_path = new / "events.jsonl"
            self.gate.log = self._new_log()
            self.engine.store = self._store()
            self.queue = ScanQueue(new / "queue.json")
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
        instrument, e.g. SCPI), get, set, describe or manual (search its manual). Refused, not queued, while Kelvin
        is busy, so a typed command never lands in the middle of a run."""
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
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "action": "busy",
                    "message": "Kelvin is busy (%s); wait for it, or stop it first" % (self.busy or "working")}
        try:
            self.busy = "terminal: %s %s" % (instrument, arg)
            with self.gate.acting_as(by):
                if op == "raw":
                    r = self.gate.raw(instrument, arg.strip())
                elif op == "get":
                    r = self.gate.get(instrument, arg.strip())
                else:
                    r = self.gate.set(instrument, arg.strip(), value)
            shown = r.value if r.value is None or isinstance(r.value, (str, int, float, bool)) else str(r.value)[:200]
            return {"ok": r.ok, "action": r.action, "message": r.message, "value": shown}
        finally:
            self.busy = None
            self._refresh()
            self.lock.release()

    def stop(self, by: str = "human") -> int:
        """A person's stop: decline open requests and stop the current ramp or plan. Drives stay where they are
        (a stopped plan still runs its own on_abort steps). The queue stops after the scan it ends, and a call still
        waiting for the lab is not run (_do). Returns how many requests were declined."""
        decline_all = getattr(self.approver, "decline_all", None)
        declined = decline_all("stop button") if decline_all else 0
        with self.queue.lock:   # a stop also stops the queue: nothing else starts after the stopped scan
            if self.queue.state in ("running", "pausing"):
                self.queue.state, self.queue.reason = "pausing", "stopped by %s" % by
                self.queue.save()
        self.gate.request_abort()
        self.gate.log.write("stop", by=by, declined=declined)
        return declined

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
        return self._do("health check", lambda: "\n".join(
            [str(r) for r in self.engine.preflight(instruments, fresh=True)]   # asked for: always checks again
            + ([] if instruments else ["%s switched off (not checked)" % n for n in self.lab.switched_off]
               + ["%s not connected: %s" % kv for kv in self.lab.unavailable.items()])))

    def wait(self, seconds: float) -> str:
        seconds = max(0.0, min(float(seconds), MAX_WAIT_S))
        def run() -> str:
            stops, t0 = self.lab.stops.count, self.lab.clock.now()
            if not self.lab.stops.sleep(self.lab.clock, seconds, stops):   # a person's stop ends the wait
                return "stopped after %s of %s: a person pressed stop" % (_dur(self.lab.clock.now() - t0),
                                                                          _dur(seconds))
            return "waited %s" % _dur(seconds)
        return self._do("waiting", run, stoppable=True)

    def validate(self, plan_text: str) -> str:
        def run() -> str:
            try:
                plan = self._plan(plan_text)
            except PlanError as e:
                return "invalid plan: %s" % e
            errors = self.engine.validate(plan)
            if errors:
                return "invalid plan: " + "; ".join(errors[:5])
            items, _, problems = self.gate.plan_approval(self.engine.drive_values(plan))
            lines = ["ok: %s, %d points, instruments %s" % (plan.name, plan.points, ", ".join(plan.requires))]
            lines += ["will be refused: " + p for p in problems]
            lines += ["needs approval: " + i for i in items] or ["no approval needed (measurement only or inside the envelope)"]
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
        return self._do("running plan", lambda: self._run_once(plan_text, dry_run)[0], stoppable=True)

    def _run_once(self, plan_text: str, dry_run: bool = False) -> Tuple[str, Optional[RunReport]]:
        """One plan, run (the caller holds the lock): run_plan's answer, and the run's report (None: not run)."""
        try:
            plan = self._plan(plan_text)
        except PlanError as e:
            return "invalid plan: %s" % e, None
        meta = self._attribute(plan)
        with self._device_protection(meta) as held:
            report = self.engine.run(plan, dry_run=dry_run, meta=meta, folder=self.attribution.get("folder"))
        made = self._auto_report(report.run_id)
        if report.run_id:
            summary = (report.analysis or {}).get("assessment") or report.message
            self.notifier("run", ("Run finished: " if report.status == "done" else "Run stopped: ") + plan.name,
                          summary[:300])
        lines = list(held)
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

    def analyze(self, run_id: str, column: Optional[str] = None, model: Optional[str] = None) -> str:
        """Re-analyse a recorded run, optionally one column and/or with a chosen fit model."""
        from ..analysis import MODELS, analyze_run
        if model and model not in MODELS and model != "linear":
            return "unknown model %r (choose: linear, %s)" % (model, ", ".join(MODELS))
        try:
            rec = rundata.load(self._run_dir(run_id))
            rows, meta = rec.rows, rec.meta
            cols = self._columns(rec)
        except (KeyError, OSError, ValueError, PlanError) as e:
            return "cannot analyse %s: %s" % (run_id, e)
        if column and column not in [m["key"] for m in cols["measures"]]:
            return "unknown column %r (have: %s)" % (column, ", ".join(m["key"] for m in cols["measures"]))
        a = analyze_run(rows, cols["axes"], cols["measures"], meta, force=model, only=column)
        return "\n".join(["%s: %d rows" % (run_id, len(rows))] + self._analysis_lines(a))

    def _preview(self, plan: Plan, rows: List[Dict[str, Any]]) -> List[str]:
        if not rows:
            return []
        axes = [a.key for a in plan.axes]
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
            where = lambda r: ", ".join("%s=%s" % (a, num(r[a])) for a in axes) or "row %d" % r["i"]
            out.append("%s: min %s at %s; max %s at %s" % (key, spec.show(lo[0]), where(lo[1]), spec.show(hi[0]), where(hi[1])))
        return out

    # ------------------------------------------------------------------ data

    def _run_dir(self, run_id: str) -> Path:
        d = self.runs.find(run_id)
        if d is None:
            raise KeyError("no run %r (see list_runs)" % run_id)
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
        """Most recent runs first: run_id, plan, status, points, planned_points, reason."""
        dirs = sorted(self.runs.iterdir(), key=lambda d: d.name, reverse=True)[:limit]
        out = []
        for d in dirs:
            entry: Dict[str, Any] = {"run_id": d.name, "plan": d.name.split("-", 2)[-1], "status": "unfinished"}
            live = self.live_view(10 ** 9)
            if live and live["run_id"] == d.name and live["status"] == "running":
                entry.update(status="running", points=live["done"], planned_points=live["total"])
            else:
                try:
                    meta = rundata.meta(d)
                    entry.update(status=meta.get("status") or "unfinished", points=meta.get("points"),
                                 planned_points=meta.get("planned_points"), reason=meta.get("reason"))
                except (KeyError, OSError, ValueError):
                    pass
            out.append(entry)
        return out

    def list_runs(self, limit: int = 10) -> str:
        lines = []
        for r in self.runs_index(limit):
            if r["status"] in ("unfinished", "running"):
                lines.append("%s: %s" % (r["run_id"], r["status"]))
            else:
                lines.append("%s: %s, %s/%s points%s" % (r["run_id"], r["status"], r["points"], r["planned_points"],
                                                         " (%s)" % r["reason"] if r.get("reason") else ""))
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
        rows = cat.find(text, device, project, plan, status, since, until, quantity, limit)
        total = cat.count()
        lines = ["%d run%s, newest first (of %d in the data folder):" % (len(rows), "" if len(rows) == 1 else "s",
                                                                          total) if rows else
                 "no run matches (%d in the data folder)" % total]
        for r in rows:
            lines += self._run_lines(r)
        running = self._running_id()
        if running:
            lines.append("running now: %s (in the catalog once it ends)" % running)
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
        try:
            names, rows, more = cat.query(sql)
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
        """Files Kelvin didn't measure, as a run (imports.py): another program's tables (CSV, TSV, text) and
        LightField's .spe spectra; several with the same x values side by side in one run, to compare or subtract.
        The run goes with this chat's runs, gets the automatic fits, and Kelvin's run tools read it."""
        from types import SimpleNamespace
        from .. import imports
        from ..analysis import analyze_run
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
        plan_name = re.sub(r"[^\w.-]+", "_", (name or "").strip() or "imported_" + table.name)[:48]

        def run() -> str:
            rec = self.engine.store.create(plan_name, {"plan": plan_name, "imported": files}, "import",
                                           self.attribution.get("folder"))
            rec.meta.update(self._attribute(SimpleNamespace(device=None)))
            rec.meta.update(lab="imported", simulated=False, imported=files)
            axes = [{"key": table.x_key, "unit": table.x_unit, "kind": "imported"}]
            measures = [{"key": k, "unit": u, "kind": "scalar"} for k, (u, _) in table.columns.items()]
            rec.set_columns({"axes": axes, "measures": measures})
            rows = []
            for i, x in enumerate(table.x):
                row = {"i": i, "t": 0.0, table.x_key: x}
                row.update({k: values[i] for k, (_, values) in table.columns.items()})
                rec.add(row)
                rows.append(row)
            meta = dict(status="done", reason=None, points=len(rows), planned_points=len(rows), duration_s=0.0,
                        warnings=list(table.notes))
            analysis = None
            try:
                analysis = analyze_run(rows, axes, measures, meta)
                rec.set_analysis(analysis)
            except Exception:
                analysis = None   # the fits are a convenience; the numbers are kept
            rec.finish(**meta)
            self.gate.log.write("run_imported", by=self.gate.operator, run=rec.run_id, files=len(files))
            try:
                self.catalog.add(rec.path)
            except Exception:
                pass   # the catalog finds it at its next look
            first = next(iter(table.columns))
            lines = ["imported %s as run %s: %d points of %s (%s), columns %s" % (
                         ", ".join(Path(f).name for f in files), rec.run_id, len(rows), table.x_key,
                         table.x_unit or "no unit", ", ".join(table.columns)),
                     "saved in %s" % rec.path] + ["note: " + n for n in table.notes]
            if analysis:
                lines += self._analysis_lines(analysis)
            lines.append("numbers: get_run_data, compute (e.g. %s / max(%s)), look_at; describe_run for its card"
                         % (first, first))
            return "\n".join(lines)
        return self._do("importing data", run)

    def stop_run(self, by: str = "human") -> bool:
        """Stop the plan that is running (its on_abort steps then run); a queue it is part of stops after it.
        False if none is running."""
        live = self.live_view(10 ** 9)
        if not live or live["status"] != "running":
            return False
        self.gate.request_abort()
        self.gate.log.write("stop", by=by, run=live["run_id"])
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

    def hands_off(self, by: str) -> List[str]:
        """The person is done with the sample: drives may move again (each still within limits and approvals).
        The mounted device's caps are written into the instruments again, in case one was replaced or reset."""
        with self.lock:
            self.hands = None
            self.lab.hands_on = None
            self.gate.log.write("hands_off", by=by)
        return self._write_caps(by)

    def _attribute(self, plan: Plan, project: Optional[str] = None, incognito: Optional[bool] = None,
                   unshared: Optional[bool] = None, legacy: bool = False) -> Dict[str, Any]:
        """Which project and sample a run belongs to: the plan's own device:, else the mounted device, else the
        project of the chat that started it (or queued it); runs outside every project go to General. A chat in an
        unshared (unpublished, confidential or model-limited) project keeps its runs there, without the mounted
        sample, which is another project's. A run says who made it, and whether an incognito chat started (or
        queued) it, or a chat that keeps unshared work's marks: such runs, and anyone else's, are never shared on
        the Kelvin hub. legacy: queued before Kelvin noted these; held like older runs."""
        m = self.mounted
        chat_pid = project if project is not None else self.attribution.get("project")
        pid = m.get("project") if m else chat_pid
        if m and chat_pid and chat_pid != pid and self.project_info(chat_pid).get("unshared"):
            pid, m = chat_pid, {}
        info = self.project_info(pid)
        out = {"project": pid, "project_name": info.get("name") or "General",
               "device": m.get("device") if m else None,
               "device_name": m.get("device_name") if m else None, "user": self.user}
        if self.attribution.get("incognito") if incognito is None else incognito:
            out["incognito"] = True
        if self.attribution.get("unshared") if unshared is None else unshared:
            out["unshared"] = True
        if legacy:
            out["legacy"] = True
        if plan.device:
            dev = next((d for d in info.get("devices") or []
                        if d["id"] == plan.device or d["name"].lower() == plan.device.lower()), None)
            out["device"], out["device_name"] = (dev["id"], dev["name"]) if dev else (None, plan.device)
        return out

    def queue_show(self) -> str:
        return self.queue.text()

    def queue_view(self) -> Dict[str, Any]:
        return dict(self.queue.view(), saved=saved_queues(self.data_dir / "queues"))

    def _queue_problems(self, text: str) -> List[str]:
        """What stops an item from running on this lab, before it joins the queue."""
        kind, _ = parse_item(text)
        if kind == "plan":
            return self.engine.validate(self._plan(text))
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
        item = self.queue.add(text, position, note, by, project=self.attribution.get("project"),
                              incognito=bool(self.attribution.get("incognito")),
                              unshared=bool(self.attribution.get("unshared")), folder=self.attribution.get("folder"))
        pos = self.queue.items.index(item) + 1
        self.gate.log.write("queue_add", by=by, item=item.id, kind=item.kind, name=item.name, position=pos)
        return "added %s %s as #%d\n%s" % (item.kind, item.name, pos, self.queue.text())

    def queue_edit(self, ref: Any, text: str, by: str = "agent") -> str:
        try:
            problems = self._queue_problems(text)
            if problems:
                return "not changed: " + "; ".join(problems[:5])
            item = self.queue.edit(ref, text)
        except (PlanError, KeyError, ValueError) as e:
            return "not changed: %s" % (e.args[0] if e.args else e)
        self.gate.log.write("queue_edit", by=by, item=item.id, name=item.name)
        return "changed %s\n%s" % (item.name, self.queue.text())

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
                return "removed %d finished items\n%s" % (n, self.queue.text())
            else:
                return "unknown queue change %r" % op
        except (KeyError, ValueError, TypeError) as e:
            return "not changed: %s" % (e.args[0] if e.args else e)
        self.gate.log.write("queue_" + op, by=by, item=item.id, name=item.name)
        return self.queue.text()

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
        return "\n".join(["added %d items from %s" % (added, name)] + problems + [self.queue.text()])

    def _queue_drives(self, items: List[Item]) -> Dict[str, List[Any]]:
        out: Dict[str, List[Any]] = {}
        for item in items:
            if item.kind == "plan":
                values = self.engine.drive_values(self._plan(item.text))
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
        With dry_run, the whole queue is first run in order on the simulated lab (for its time, or a person who
        wants to see it). Returns at once after the approval; queue_show follows it."""
        with self.queue.lock:
            if self.queue.state in ("running", "pausing"):
                return "the queue is already running\n" + self.queue.text()
        items = self.queue.waiting()
        if not items:
            return "nothing is waiting in the queue"

        def prepare() -> str:
            problems = []
            for n, item in enumerate(items, 1):
                try:
                    problems += ["#%d %s: %s" % (n, item.name, p) for p in self._queue_problems(item.text)]
                except PlanError as e:
                    problems.append("#%d %s: %s" % (n, item.name, e))
            if problems:
                return "the queue did not start:\n" + "\n".join(problems[:8])
            lines, grants, refusals = self.gate.plan_approval(self._queue_drives(items))
            if refusals:
                return "the queue did not start; these would be refused:\n" + "\n".join(refusals[:8])
            duration = None
            if dry_run:
                duration, failed = self._rehearse(items)
                if failed:
                    return "the queue did not start; its dry run failed:\n" + "\n".join(failed[:8])
            scans = sum(1 for i in items if i.kind == "plan")
            steps = len(items) - scans
            title = "Run the queue: %d scan%s%s%s" % (scans, "" if scans == 1 else "s", " and %d step%s" % (
                steps, "" if steps == 1 else "s") if steps else "", ", ~%s" % _dur(duration) if duration else "")
            if lines:
                request = ApprovalRequest(title, lines + [
                    "This approval covers every item now waiting; anything outside these ranges asks again."])
                covered = False
                if self.gate.auto:   # as a run is: rehearsed on the simulated lab, and every range low-risk
                    failed = []
                    if not dry_run:
                        duration, failed = self._rehearse(items)
                    rehearsal = type("Rehearsal", (), {"ok": not failed})()
                    covered, why = self.gate.auto_plan(grants, rehearsal)
                    if covered:
                        self.gate.auto_approve(request, why)
                    else:
                        request.items.append("Auto mode does not cover this: " + "; ".join(
                            why + ["the dry run: %s" % f for f in failed[:2]]))
                if not covered and not self.gate.ask(request):
                    note = " (%s)" % request.note if request.note else ""
                    self.gate.log.write("queue_declined", by=by)
                    return "declined: the queue did not start%s" % note
            with self.queue.lock:
                self.queue.state, self.queue.reason = "running", ""
                self.queue.save()
            self.gate.log.write("queue_start", by=by, items=len(items),
                                expected_s=round(duration) if duration else None)
            self._queue_thread = threading.Thread(target=self._queue_loop, args=(grants, by), daemon=True,
                                                  name="kelvin-queue")
            self._queue_thread.start()
            return "queue started: %s\n%s" % (title[len("Run the queue: "):], self.queue.text())
        return self._do("starting the queue", prepare, stoppable=True)

    def queue_pause(self, by: str = "agent") -> str:
        """Finish the item that is running, then stop there. Stop (the Stop button) also ends the running scan."""
        with self.queue.lock:
            if self.queue.state != "running":
                return "the queue is not running\n" + self.queue.text()
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
            ok, message = self._queue_item(item, position, grants, by)
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
        if q.state == "idle":
            self.notifier("run", "Queue finished", "%d items done" % done)
        else:
            self.notifier("run", "Queue paused", q.reason[:300])

    def _queue_item(self, item: Item, position: int, grants: Dict[str, Allowed], by: str) -> Tuple[bool, str]:
        """Run one item under the queue's approval. (ok, message)"""
        q = self.queue
        stops = self.gate.stop_generation
        with self.lock:
            if self.gate.stop_generation != stops:   # stopped while it waited for the lab: it never starts
                q.mark(item, "stopped", "a person pressed Stop before it started")
                return False, "stopped before it started"
            self.busy = "queue %d/%d: %s" % (position, len(q.items), item.name)
            try:
                with self.gate.standing(grants), self.gate.acting_as("queue (started by %s)" % by):
                    if item.kind == "plan":
                        plan = self._plan(item.text)
                        meta = self._attribute(plan, item.project, item.incognito, item.unshared, item.legacy)
                        with self._device_protection(meta):
                            report = self.engine.run(plan, meta=dict(meta, queue={
                                "item": item.id, "position": position, "note": item.note}), folder=item.folder)
                        status = {"done": "done", "aborted": "stopped"}.get(report.status, "failed")
                        if report.status == "aborted" and "stop" not in (report.message or "").lower():
                            status = "failed"
                        q.mark(item, status, report.message if status != "done" else "", report.run_id)
                        self._auto_report(report.run_id)
                        return status == "done", report.message
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
            r = self.gate._set(instr, param, value, True, "queue:" + item.name)
            if not r.ok:
                q.mark(item, "failed", r.message)
                return False, r.message
        warnings: List[str] = []
        try:
            if step.stabilize:
                self.engine._stabilize(step.stabilize, gen, warnings)
            end = self.lab.clock.now() + step.wait
            while self.lab.clock.now() < end:
                if self.gate.stop_generation != gen:
                    raise _Abort("stop requested")
                self.lab.clock.sleep(min(1.0, end - self.lab.clock.now()))
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

    def _look_camera(self, key: str) -> Tuple[Optional[bytes], str]:
        spec = self._spec(key)
        if spec is None:
            return None, "no reading %r (instrument.parameter)" % key
        if spec.type != "image":
            return None, "%s is not a camera frame; look_at shows frames, and runs as plots" % key
        instr, _, param = key.partition(".")
        r = self._do("reading %s" % key, lambda: self.gate.get(instr, param))
        if isinstance(r, str):   # busy with the queue
            return None, r
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

    def events(self, n: int = 20) -> str:
        return "\n".join(event_line(e) for e in tail_events(self.events_path, max(1, min(int(n), 200)))) or "no events yet"

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
                        "headline": headline, "readings": readings, "drives": drives, "settings": settings})
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

    def pulse_sequence(self, protocol: str, times: Dict[str, Any], generator: str = "") -> str:
        """The timing a pulse generator would play, checked: the diagram, period, duty cycles and any problem.
        Missing times come from the generator's current settings (if named), else from pulses.DEFAULTS."""
        from .. import pulses
        current: Dict[str, Any] = {}
        inst = self.lab.instruments.get(generator) if generator else None
        if generator and inst is None:
            return "no instrument %r (have: %s)" % (generator, ", ".join(self.lab.instruments))
        if inst is not None:
            for k in ("t_pi", "tau", "t_laser", "t_gate", "t_wait"):
                if k in inst.card.params:
                    try:
                        current[k] = inst.read(k)
                    except Exception:
                        pass
        given = {k: v for k, v in (times or {}).items() if v is not None}
        try:
            seq = pulses.build(protocol, dict(current, **given))
        except (ValueError, TypeError) as e:
            return str(e)
        problems = pulses.check(seq)
        return pulses.describe(seq) + ("\nproblems: " + "; ".join(problems) if problems else "\nchecks: ok")

    # ------------------------------------------------------------------ cards for new instruments

    def check_card(self, text: str) -> str:
        card, problems, warnings = carddraft.check(text)
        if card is None:
            return "not valid:\n" + "\n".join("- " + p for p in problems)
        drives = [p.name for p in card.params.values() if p.is_drive]
        lines = ["valid: %s (%s), %d parameters; drives: %s" % (card.id, card.model, len(card.params),
                                                              ", ".join(drives) or "none")]
        return "\n".join(lines + ["warning: " + w for w in warnings])

    def read_examples(self, path: str) -> str:
        try:
            return carddraft.read_sources(path)
        except (OSError, ValueError) as e:
            return "not read: %s" % e

    def script_cards(self) -> Any:
        """Every card Kelvin can use here, to match the lab's scripts against: Kelvin's own and the lab's. Kept
        while the lab's cards folder stays as it was, as reading every card takes a second. Only read from."""
        from ..virtual_lab import lab_registry
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
            found = lab_scripts.survey(path, self.script_cards())
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

    def _saved_card(self, ref: str) -> Any:
        """A card Kelvin has, by its id (Kelvin's own, or one in ~/Kelvin/cards); None if there is none."""
        from ..virtual_lab import lab_registry
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
                "its readings; it sets nothing." % (instrument, card.model)])
            if not self.gate.auto_simple(request, "trying a card only asks the instrument who it is and reads it") \
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

    def save_card(self, text: str, by: str = "agent") -> str:
        if self.cards_dir is None:
            return "not saved: this Kelvin has no folder for your cards"
        from ..virtual_lab import builtin_ids, kelvin_ids
        try:
            card, path, warnings = carddraft.save(self.cards_dir, text, kelvin_ids(self.collection), by, version=True)
        except ValueError as e:
            return "not saved: %s" % e
        self.refresh_cards([card.id])
        self.gate.log.write("card", by=by, card=card.id, status=card.status, file=path.name)
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
        an instrument already in the lab keeps its connection (only its web page is looked at)."""
        from .. import probe
        from ..discovery import identify, match_card
        try:
            host, _ = probe.host_of(address)
            ip = probe.local_ip(host)
        except ValueError as e:
            return str(e)
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
        for resource in found_here:
            if resource not in self.last_found:
                self.last_found.append(resource)   # found here: try_card and add_instrument may use it
        return "\n".join(lines)

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
        if not self.gate.auto_simple(request, "installing into one of Kelvin's own environments drives nothing; the "
                                              "instrument's commands still go through Kelvin's checks") \
                and not self.gate.ask(request):
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
        return self._do("%s.%s" % (instrument, action), run, stoppable=True)

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
        installed, the instrument switched on or plugged in). Connecting changes nothing on it."""
        inst = self.lab.instruments.get(name)
        if inst is None:
            return "no instrument %r (have: %s)" % (name, ", ".join(self.lab.instruments))
        if inst.enabled:
            return "%s is connected" % name
        if not inst.unavailable:
            return "%s is switched off in the Lab panel: ask_to_switch_on asks the person" % name
        with self.lock:
            try:
                self.lab.reconnect(name)
            except Exception as e:
                self.gate.log.write("reconnect_failed", by=by, instrument=name, error=inst.unavailable)
                return "%s is still not connected: %s" % (name, inst.unavailable or e)
            self.gate.log.write("instrument_connected", by=by, instrument=name)
            self._caps_if_waiting()
            self._refresh()
        health = self.lab.health_check([name])[0]
        return "%s is connected now%s" % (name, ": " + "; ".join(health.problems) if health.problems else
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
        its limits, every output change on it asks them. An address the last scan did not find asks first; moving
        Kelvin off a virtual lab the person chose asks too."""
        from ..adapters import AdapterError, resource_name
        from ..cards import CONNECTION_KEYS
        from ..discovery import Found, add_to_config
        from ..labconfig import ConfigError, load_lab_config
        from ..virtual_lab import build_lab, lab_registry
        if self.home is None:
            return "not added: this Kelvin keeps no lab config (the Kelvin app does)"
        address = (address or "").strip()
        if not address or address.startswith("sim://"):
            return "not added: give the address of an instrument connected to this computer (scan_instruments)"
        lab_file = self.home / "lab.yaml"
        registry = lab_registry(self.home)
        found = self.found_info.get(resource_name(address), {})
        card_id = (card or "").strip() or found.get("card_id") or ""
        if not card_id:
            return "not added: Kelvin has no card for %s yet%s; draft_card writes one from its manual" % (
                address, " (it answered %r)" % found["idn"][:60] if found.get("idn") else "")
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
            if not on_file and lab_file.exists() and not self.lab_info.get("error"):
                asks.append("Kelvin is on the virtual lab, as you chose; adding %s moves it to your instruments "
                            "(%s)." % (new, ", ".join(sorted(cfg.instruments))))
            if asks:
                request = ApprovalRequest("Add %s to the lab?" % new, asks + [
                    "Every output change on it still asks you until you review its limits."])
                simple = len(asks) == 1 and on_file   # only an address the scan missed: it connects and reads
                if not (simple and self.gate.auto_simple(request, "adding an instrument connects it and reads it; "
                                                                  "every output change on it still asks")) \
                        and not self.gate.ask(request):
                    note = " (%s)" % request.note if request.note else ""
                    return "declined: the person did not want %s added%s" % (new, note)
            try:   # on the person's lab: connect just this one; from the virtual lab: their whole lab, with it
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
            return ("added %s (%s, card %s%s) at %s to %s; health: %s. Its outputs keep the maker's limits%s until a "
                    "person reviews them, and every output change on it asks them (auto mode doesn't cover it)."
                    % (new, the_card.model, the_card.id, ", a draft" if the_card.status == "draft" else "", address,
                       self.lab.config.name, "ok" if health.ok else "; ".join(health.problems),
                       " (" + lim + ")" if lim else ""))
        return self._do("adding an instrument", run)

    def refresh_cards(self, ids: Iterable[str]) -> None:
        """After cards changed (the user's, or a new copy of the collection): the card the lab's registry has for each
        of these ids, by the same layers a lab is opened with (virtual_lab.lab_registry). Instruments already
        connected keep theirs until the lab is opened again."""
        from ..virtual_lab import lab_registry
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
                     "them with run_plan as usual (the same checks and approvals apply).")
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
        return pick_runs(self.runs, runs, day, last)

    def report(self, run_ids: List[str], title: str = "", summary: str = "", fmt: str = "pdf",
               notes: Any = (), by: str = "human", references: Any = (), folder: Optional[Path] = None,
               name: str = "", log: bool = True) -> List[Dict[str, Any]]:
        """Write a report of these runs (oldest first) as PDF and/or PowerPoint into reports/ (or folder, as
        name.pdf / name.pptx). Reads only files: it never talks to an instrument, so it can run while a plan runs."""
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
            n = write_pdf(report, path) if f == "pdf" else write_pptx(report, path)
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
                    project: Optional[str] = None) -> str:
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
            ids = self.pick_runs(runs, day, last)
            if not (runs or day or last):
                ids = self.pick_runs(day="today") or self.pick_runs(last=1)
        except (KeyError, ValueError) as e:
            return "no report: %s" % (e.args[0] if e.args else e)
        if not ids:
            return "no report: no runs %s" % ("on %s" % day if day else "yet")
        try:
            files = self.report(ids, title, summary, fmt, notes, by, references)
        except (ValueError, OSError) as e:
            return "no report: %s" % e
        lines = ["report: %s (%d run%s, %s)" % (f["name"], len(ids), "" if len(ids) == 1 else "s",
                                                "%d pages" % f["pages"] if "pages" in f else "%d slides" % f["slides"])
                 for f in files]
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
        if not path.is_file():
            raise KeyError("no report %r" % name)
        return path

    # ------------------------------------------------------------------ watching instruments

    def _new_monitor(self) -> Monitor:
        monitor = Monitor(self.lab, self.gate, self.notifier)
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
        view = next(v for v in self.monitor.list() if v["id"] == w.id)
        self.gate.log.write("watch", by=source, change="added", watch=w.id, rule=view["rule"])
        return view

    def remove_watch(self, watch_id: str, source: str = "person") -> None:
        w = self.monitor.remove(watch_id, source)   # watches from the lab config change only there
        self.gate.log.write("watch", by=source, change="removed", watch=w.id, rule=w.rule())

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

    def switch_lab(self, lab: Lab, envelope: Optional[Envelope], info: Dict[str, Any], by: str = "human") -> List[str]:
        """Control another lab (the user's instruments, or the virtual lab). Refused while a plan runs. The approval
        mode goes back to asking, and the new lab is health-checked right away."""
        with self.lock:
            if self.live and self.live.get("status") == "running":
                raise RuntimeError("a plan is running; wait for it to finish or stop it first")
            old = self.lab
            self.lab = lab
            self.gate = SafetyGate(lab, self.approver, self._new_log(lab.clock), envelope, operator="agent")
            self.engine = RunEngine(self.gate, self._store())
            self.engine.progress = self._on_progress
            self._apply_protection(recheck=True)
            self.lab_info = dict(info)
            running = self.monitor._thread is not None
            self.monitor.stop()
            self.monitor = self._new_monitor()   # watches name instruments of the old lab; start again
            if running:
                self.monitor.start()
            self._status = {}
            self._refresh()
            self.gate.log.write("lab_changed", by=by, lab=lab.config.name, instruments=sorted(lab.instruments),
                                kind=info.get("kind"))
            results = [str(r) for r in self.engine.preflight()]
        if old is not lab:
            old.close()
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
                "instrument on and leaving a virtual lab you chose still ask (what drives nothing, such as installs, "
                "skills and saved files, never waits for you in either mode)"
                % (100 * cfg.auto_fraction))
        if ranges:
            rule += ". Own auto ranges: " + ", ".join(ranges)
        if never:
            rule += ". Never automatic: " + ", ".join(never)
        return {"mode": "auto" if self.gate.auto else "ask", "fraction": cfg.auto_fraction, "rule": rule + "."}

    def set_approval_mode(self, mode: str, by: str = "human") -> Dict[str, Any]:
        """Only the portal and the approval page call this; no agent tool can. It resets to ask on every start."""
        if mode not in ("ask", "auto"):
            raise ValueError("mode must be ask or auto")
        if (mode == "auto") != self.gate.auto:
            self.gate.auto = mode == "auto"
            self.gate.log.write("approval_mode", by=by, mode=mode)
        return self.approval_mode()

    def ui_state(self) -> Dict[str, Any]:
        # keep the panel live while Kelvin is idle; never wait for a running command
        if time.time() - self._status_at > 2.0 and self.lock.acquire(blocking=False):
            try:
                self._refresh()
            finally:
                self.lock.release()
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
                "notices": list(self.notifier.notices)[-20:],
                "instruments": self.instruments(),
                "live": {k: v for k, v in (self.live_view(10 ** 9) or {}).items()
                         if k in ("run_id", "plan", "done", "total", "eta_s", "status")} or None,
                "events": [event_line(e) for e in reversed(tail_events(self.events_path, 25))]}
