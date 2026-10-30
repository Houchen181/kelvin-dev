"""The Sequence engine's tools in Kelvin's MCP server: the measurement sequence, its function library and Kelvin's Home
page cards, for the in-app chat AI and for any outside MCP client (they share one tool table).

The engine now runs inside Kelvin (labharness/sequence/, wrapped by web/labmaster.py), so the tools talk to it in this
process: there is no panel on a port, no address to show, and a call the engine cannot make comes back in words, never
as a connection error. These tests put a fake facade where the engine would be, so every answer and every engine call
is checked without starting an engine or touching the person's ~/Kelvin; tests/test_sequence_service.py covers the
engine itself, and tests/test_labmaster_bridge.py the facade.
"""
import copy
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest

mcp = pytest.importorskip("mcp")
anyio = pytest.importorskip("anyio")

from mcp import Client  # noqa: E402

from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.agent import server as agent_server  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import Approver, ScriptedApprover  # noqa: E402

LABMASTER_READ = ["labmaster_status", "labmaster_functions", "labmaster_saved", "labmaster_home_cards",
                  "labmaster_read_script", "labmaster_history", "labmaster_run_detail",
                  "labmaster_analysis_scripts", "labmaster_analysis_status", "labmaster_datasets",
                  "labmaster_dataset_files", "labmaster_log", "labmaster_home"]
LABMASTER_ACT = ["labmaster_add_block", "labmaster_update_block", "labmaster_delete_block", "labmaster_control",
                 "labmaster_rename_sequence", "labmaster_import_script", "labmaster_home_add_card",
                 "labmaster_home_remove_card", "labmaster_move_block", "labmaster_duplicate_block",
                 "labmaster_clear_sequence", "labmaster_saved_save", "labmaster_saved_load",
                 "labmaster_saved_delete", "labmaster_write_script", "labmaster_function_update",
                 "labmaster_function_duplicate", "labmaster_function_remove", "labmaster_write_analysis",
                 "labmaster_run_analysis", "labmaster_analysis_cancel"]
LABMASTER_TOOLS = LABMASTER_READ + LABMASTER_ACT
# the sequence reads, the Home page cards, and (v2-C) the library/scripts/History/analysis/data/log: none touches an
# instrument, so a chat runs them without the lab's lock. The sequence's own edits and control stay on it.
SEQUENCE_OFF_LAB = {"labmaster_status", "labmaster_functions", "labmaster_saved", "labmaster_home_cards",
                    "labmaster_home_add_card", "labmaster_home_remove_card", "labmaster_rename_sequence",
                    "labmaster_write_script", "labmaster_read_script", "labmaster_function_update",
                    "labmaster_function_duplicate", "labmaster_function_remove", "labmaster_history",
                    "labmaster_run_detail", "labmaster_analysis_scripts", "labmaster_write_analysis",
                    "labmaster_run_analysis", "labmaster_analysis_status", "labmaster_analysis_cancel",
                    "labmaster_datasets", "labmaster_dataset_files", "labmaster_log", "labmaster_home"}
SEQUENCE_ON_LAB = {"labmaster_add_block", "labmaster_update_block", "labmaster_delete_block", "labmaster_control",
                   "labmaster_import_script", "labmaster_move_block", "labmaster_duplicate_block",
                   "labmaster_clear_sequence", "labmaster_saved_save", "labmaster_saved_load",
                   "labmaster_saved_delete"}
# the calls that change a run: they need the user's own yes (confirm=true) before the engine is asked. start is not
# among them: with the arm switch on it starts directly, and with it off a person answers the approval card
GATED = ("skip", "reset", "end_wait", "set_wait", "next_rep")
# v2-C: what discards or replaces something needs the user's own yes first
GATED_DESTRUCTIVE = (
    ("labmaster_clear_sequence", {}),
    ("labmaster_saved_save", {"name": "Morning", "overwrite": True}),
    ("labmaster_saved_load", {"id": "seq_1", "replace": True}),
    ("labmaster_saved_delete", {"id": "seq_1"}),
    ("labmaster_function_remove", {"id": "blk_wait"}),
    ("labmaster_analysis_cancel", {}),
)
# v2-C: the 11 tools every chat carries (they are what "see the sequence and do the work" needs); the other 23 are
# loaded with find_tools (or by calling one by name), so a step's context does not carry all 34
ALWAYS_ON_LABMASTER = ["labmaster_status", "labmaster_functions", "labmaster_history", "labmaster_add_block",
                       "labmaster_update_block", "labmaster_import_script", "labmaster_write_script",
                       "labmaster_control", "labmaster_rename_sequence", "labmaster_home_add_card",
                       "labmaster_home_remove_card"]
DEFERRED_LABMASTER = set(LABMASTER_TOOLS) - set(ALWAYS_ON_LABMASTER)

SNAPSHOT = {"sequence": {"name": "R(T) demo", "items": [
    {"uid": "it_1", "name": "example_measurement", "kind": "script",
     "target": "scripts/example_measurement.py", "params": {"reps": 1}},
    {"uid": "it_2", "name": "Safe stop", "kind": "safe_stop", "params": {}}]},
    "run_state": "idle", "progress": {"done": 0, "total": 2}, "saved": [], "history_runs": 0}


class FakeHome:
    """Kelvin's own cards file, as web/labmaster.py's HomeStore holds it (never the person's ~/Kelvin/home.json)."""

    def __init__(self):
        self._cards: List[Dict[str, Any]] = []
        self._n = 0

    def cards(self) -> List[Dict[str, Any]]:
        return [dict(c) for c in self._cards]

    def add(self, card: Dict[str, Any]) -> Dict[str, Any]:
        self._n += 1
        made = dict(card, id="card_%d" % self._n)
        self._cards.append(made)
        return dict(made)

    def remove(self, card_id: str) -> Dict[str, Any]:
        if not any(c.get("id") == card_id for c in self._cards):
            raise KeyError(card_id)
        self._cards = [c for c in self._cards if c.get("id") != card_id]
        return {"ok": True, "id": card_id}


class FakeEngine:
    """web/labmaster.py as the MCP tools see it: in-process, no network, every call recorded.

    error: set it and every engine call answers {"ok": False, "error": error} - an engine that cannot answer.
    armed_value/armed_error: what a person did with 'AI may start runs' (or that it cannot be read).
    record_error/ensure_error: a recording that fails, to prove the tools' answers never depend on it.
    """

    def __init__(self, home: Optional[FakeHome] = None):
        self.error = ""
        self.armed_value = False
        self.armed_error: Optional[BaseException] = None
        self.record_error: Optional[BaseException] = None
        self.ensure_error: Optional[BaseException] = None
        self.state_dict: Dict[str, Any] = copy.deepcopy(SNAPSHOT)
        self.home = home or FakeHome()
        self.records: List[Dict[str, Any]] = []
        self.named: List[str] = []
        self.control_calls: List[Any] = []
        self.deleted: List[str] = []
        self.renames: List[str] = []
        self.added: List[Dict[str, Any]] = []
        self.updated: List[Any] = []
        # v2-C
        self.moved: List[Any] = []
        self.duplicated: List[str] = []
        self.cleared: List[bool] = []
        self.saves: List[Any] = []
        self.loads: List[Any] = []
        self.saved_deleted: List[Any] = []
        self.script_writes: List[Any] = []
        self.existing_scripts: set = set()
        self.function_updates: List[Any] = []
        self.function_duplicates: List[str] = []
        self.function_removes: List[str] = []
        self.analysis_writes: List[Any] = []
        self.existing_analyses: set = set()
        self.analysis_runs: List[Any] = []
        self.analysis_cancels: List[str] = []
        self.dataset_calls: List[int] = []
        self.dataset_file_calls: List[str] = []
        self.log_calls: List[int] = []

    # -- reads
    def ping(self) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "sim": None, "url": "in-process", "version": None, "error": self.error}
        return {"ok": True, "sim": True, "url": "in-process", "version": "0.1.7", "error": None}

    def state(self) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "url": "in-process", "error": self.error, "state": None}
        return {"ok": True, "url": "in-process", "error": None, "state": copy.deepcopy(self.state_dict)}

    def palette(self) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "palette": [], "rev": ""}
        return {"ok": True, "error": None, "rev": "rev1", "palette": [
            {"id": "blk_wait", "name": "Wait", "kind": "wait", "params": {"minutes": 1}}]}

    def scripts(self) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "scripts": []}
        return {"ok": True, "error": None, "scripts": [
            {"path": "scripts/example_measurement.py", "name": "example_measurement.py"}]}

    def saved(self) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "saved": []}
        return {"ok": True, "error": None, "saved": [
            {"id": "seq_1", "name": "Morning", "saved": 1700000000.0, "n_blocks": 2}]}

    # -- writes
    def _write_error(self) -> Dict[str, Any]:
        return {"ok": False, "error": self.error, "state": None, "message": ""}

    def _ok(self, message: str) -> Dict[str, Any]:
        return {"ok": True, "error": None, "state": copy.deepcopy(self.state_dict), "message": message}

    def seq_add(self, block: Any = "", block_id: str = "", params: Any = None, index: Any = None) -> Dict[str, Any]:
        self.added.append({"block": block, "block_id": block_id, "params": params, "index": index})
        if self.error:
            return self._write_error()
        return self._ok("Added %s." % (block or block_id))

    def seq_update(self, uid: str, params: Any = None, name: str = "") -> Dict[str, Any]:
        self.updated.append({"uid": uid, "params": params, "name": name})
        return self._write_error() if self.error else self._ok("Updated the block.")

    def seq_delete(self, uid: str) -> Dict[str, Any]:
        self.deleted.append(uid)
        return self._write_error() if self.error else self._ok("Removed the block.")

    def seq_rename(self, name: str) -> Dict[str, Any]:
        self.renames.append(name)
        if self.error:
            return self._write_error()
        self.state_dict["sequence"]["name"] = name
        return self._ok("Renamed the sequence.")

    def control(self, action: str, **kw: Any) -> Dict[str, Any]:
        self.control_calls.append((action, kw))
        if self.error:
            return self._write_error()
        return self._ok("The sequence engine carried out %s." % action)

    def safe_stop(self, reason: str = "") -> Dict[str, Any]:
        self.control_calls.append(("safe_stop", {"reason": reason}))
        if self.error:
            return self._write_error()
        return {"ok": True, "error": None, "state": copy.deepcopy(self.state_dict), "message": "Safe stop started."}

    def import_script(self, path: str, name: str = "") -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "entry": None, "palette": None, "message": ""}
        return {"ok": True, "error": None, "entry": {"id": "blk_new", "name": name or "example_measurement"},
                "palette": [{"id": "blk_new"}], "message": 'Added "%s" to the function library.'
                                                          % (name or "example_measurement")}

    # -- v2-C: sequence edits, saved sequences, the library, scripts, History, analysis, data and log
    def seq_move(self, uid: str, index: Any) -> Dict[str, Any]:
        self.moved.append({"uid": uid, "index": index})
        return self._write_error() if self.error else self._ok("Moved the block.")

    def seq_duplicate(self, uid: str) -> Dict[str, Any]:
        self.duplicated.append(uid)
        if self.error:
            return self._write_error()
        items = self.state_dict["sequence"]["items"]
        at = next((k for k, item in enumerate(items) if item.get("uid") == uid), None)
        if at is not None:
            items.insert(at + 1, {"uid": "it_copy", "name": "copy", "status": "pending"})
        return self._ok("Copied it as copy.")

    def seq_clear(self, keep_done: bool = False) -> Dict[str, Any]:
        self.cleared.append(bool(keep_done))
        if self.error:
            return self._write_error()
        self.state_dict["sequence"]["items"] = []
        return self._ok("Cleared the sequence.")

    def saved_save(self, name: str = "", overwrite: bool = False) -> Dict[str, Any]:
        self.saves.append({"name": name, "overwrite": overwrite})
        if self.error:
            return self._write_error()
        saved = self._ok('Saved the sequence as "%s".' % name)
        saved["entry"] = {"id": "seq_9", "name": name}
        return saved

    def saved_load(self, item_id: str = "", name: str = "", append: bool = False,
                   replace: bool = False) -> Dict[str, Any]:
        self.loads.append({"id": item_id, "name": name, "append": append, "replace": replace})
        return self._write_error() if self.error else self._ok('Loaded saved sequence "%s".'
                                                              % (name or item_id))

    def saved_delete(self, item_id: str = "", name: str = "") -> Dict[str, Any]:
        self.saved_deleted.append({"id": item_id, "name": name})
        return self._write_error() if self.error else self._ok('Deleted saved sequence "%s".'
                                                              % (name or item_id))

    def write_script(self, name: str, code: str, overwrite: bool = False) -> Dict[str, Any]:
        fname = str(name or "").strip()
        if not fname.lower().endswith(".py"):
            fname += ".py"
        self.script_writes.append({"name": fname, "code": code, "overwrite": overwrite})
        if self.error:
            return {"ok": False, "error": self.error, "path": "", "name": fname, "params": [], "scan": None,
                    "message": ""}
        if fname in self.existing_scripts and not overwrite:
            return {"ok": False, "name": fname, "path": "scripts/" + fname, "params": [], "scan": None,
                    "message": "",
                    "error": 'write script: "%s" is already there - pass overwrite=true to replace it' % fname}
        self.existing_scripts.add(fname)
        params = [{"name": "n_points", "type": "num", "value": 21, "help": "how many points", "group": "Scan"},
                  {"name": "start", "type": "num", "value": 0.0, "help": "", "group": "Scan"}]
        return {"ok": True, "error": None, "name": fname, "path": "scripts/" + fname, "overwritten": bool(overwrite),
                "scan": {"params": params, "error": None}, "params": params,
                "message": 'Wrote "%s" to the engine\'s scripts folder; the engine finds 2 parameter(s) in it'
                           % fname}

    def read_script(self, path: str) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "path": "", "name": "", "code": "", "params": [],
                    "scan": None, "message": ""}
        return {"ok": True, "error": None, "path": "scripts/demo.py", "name": "demo.py", "has_main": True,
                "code": '"""Demo."""\n\nreps = 3\n\n\ndef main():\n    print(reps)\n',
                "params": [{"name": "reps", "type": "num", "value": 3, "help": "", "group": ""}],
                "scan": {"params": [{"name": "reps"}], "error": None}, "message": ""}

    def function_update(self, entry_id: str, name: Any = None, note: Any = None, params: Any = None,
                        color: Any = None) -> Dict[str, Any]:
        self.function_updates.append({"id": entry_id, "name": name, "note": note, "params": params,
                                      "color": color})
        if self.error:
            return {"ok": False, "error": self.error, "entry": None, "palette": None, "message": ""}
        return {"ok": True, "error": None, "entry": {"id": entry_id, "name": name or "Wait"},
                "palette": [{"id": entry_id}],
                "message": 'Updated the function "%s" (name)' % (name or entry_id)}

    def function_duplicate(self, entry_id: str) -> Dict[str, Any]:
        self.function_duplicates.append(entry_id)
        if self.error:
            return {"ok": False, "error": self.error, "entry": None, "palette": None, "message": ""}
        return {"ok": True, "error": None, "entry": {"id": "blk_copy", "name": "Wait (copy)"},
                "palette": [{"id": "blk_wait"}, {"id": "blk_copy"}],
                "message": 'Copied the function "Wait" as "Wait (copy)" [blk_copy]'}

    def function_remove(self, entry_id: str) -> Dict[str, Any]:
        self.function_removes.append(entry_id)
        if self.error:
            return {"ok": False, "error": self.error, "entry": None, "palette": None, "message": ""}
        return {"ok": True, "error": None, "entry": {"id": entry_id, "name": "Wait"}, "palette": [],
                "message": 'Removed the function "Wait" [%s] from the library; 0 left' % entry_id}

    def history_runs(self, limit: int = 20, query: str = "") -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "runs": []}
        return {"ok": True, "error": None, "runs": [
            {"run_id": "run_1", "name": "R(T) demo", "status": "done", "started": 1700000000.0,
             "finished": 1700000120.0, "at": "2023-11-14T22:13:20", "n_blocks": 2, "n_error": 0,
             "n_datasets": 1, "datasets": ["C:/data/2026-10-30/#001_RT_10-00-00"],
             "blocks": [
                 {"n": 1, "uid": "it_1", "name": "example_measurement", "kind": "script", "status": "done",
                  "params": {"reps": 1, "n_points": 21},
                  "datasets": ["C:/data/2026-10-30/#001_RT_10-00-00"], "message": ""},
                 {"n": 2, "uid": "it_2", "name": "Wait", "kind": "wait", "status": "done",
                  "params": {"minutes": 1}, "datasets": [], "message": ""}]}]}

    def run_detail(self, run_id: str) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "source": "", "run": None}
        if str(run_id) == "kr_1":
            return {"ok": True, "error": None, "source": "kelvin",
                    "run": {"run_id": "kr_1", "name": "laser check", "kind": "plan", "status": "done",
                            "detail": "run 20261030-120000-laser-check done: 5 points",
                            "started": 1700000000.0}}
        return {"ok": True, "error": None, "source": "panel",
                "run": {"run_id": str(run_id), "name": "R(T) demo", "status": "done", "started": 1700000000.0,
                        "finished": 1700000120.0, "n_blocks": 2, "n_datasets": 1,
                        "datasets": ["C:/data/2026-10-30/#001_RT_10-00-00"],
                        "blocks": [{"n": 1, "uid": "it_1", "name": "example_measurement", "kind": "script",
                                    "status": "done", "params": {"reps": 1}, "overrides": {},
                                    "datasets": ["C:/data/2026-10-30/#001_RT_10-00-00"], "message": "",
                                    "started": 1700000000.0}]}}

    def analysis_scripts(self, classes: Any = ()) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "scripts": [], "dirs": [], "templates": []}
        return {"ok": True, "error": None,
                "scripts": [{"id": "my:0:count_rows.py", "name": "count_rows",
                             "file": "my_analysis/count_rows.py", "title": "Count rows",
                             "params": [{"name": "column", "type": "str", "default": "x", "label": "Column",
                                         "help": "", "min": None, "max": None, "choices": None}],
                             "classes": ["RT"], "problems": [], "runnable": True}],
                "dirs": [{"k": 0, "path": "C:/Kelvin/sequence/my_analysis", "exists": True, "n": 1,
                          "default": True}],
                "templates": [{"id": "plot", "title": "Plot each dataset"}]}

    def write_analysis(self, name: str, code: str, overwrite: bool = False) -> Dict[str, Any]:
        fname = str(name or "").strip()
        if not fname.lower().endswith(".py"):
            fname += ".py"
        self.analysis_writes.append({"name": fname, "code": code, "overwrite": overwrite})
        if self.error:
            return {"ok": False, "error": self.error, "path": "", "name": fname, "record": None,
                    "params": [], "message": ""}
        if fname in self.existing_analyses and not overwrite:
            return {"ok": False, "name": fname, "path": "my_analysis/" + fname, "record": None, "params": [],
                    "message": "",
                    "error": 'write analysis: "%s" is already there - pass overwrite=true to replace it' % fname}
        self.existing_analyses.add(fname)
        params = [{"name": "column", "type": "str", "default": "x", "label": "Column", "help": ""}]
        return {"ok": True, "error": None, "path": "C:/Kelvin/sequence/my_analysis/" + fname, "name": fname,
                "overwritten": bool(overwrite),
                "record": {"id": "my:0:" + fname, "name": fname[:-3], "params": params},
                "params": params,
                "message": 'Wrote the analysis script "%s"; the engine finds 1 parameter(s) in it' % fname}

    def run_analysis(self, script: str, datasets: Any = None, run_id: str = "",
                     params: Any = None) -> Dict[str, Any]:
        self.analysis_runs.append({"script": script, "datasets": datasets, "run_id": run_id, "params": params})
        if self.error:
            return {"ok": False, "error": self.error, "job": "", "status": None, "message": ""}
        return {"ok": True, "error": None, "job": "an_1", "status": {"state": "running"},
                "message": 'Started the analysis "count_rows" on 2 dataset(s) (job an_1)'}

    def analysis_status(self) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "status": None}
        return {"ok": True, "error": None, "status": {
            "state": "running", "id": "an_1", "started": 1700000000.0, "finished": None, "done": 0,
            "total": 1,
            "items": [{"script": "my:0:count_rows.py", "label": "count_rows",
                       "paths": ["C:/data/2026-10-30/#001_RT_10-00-00"], "state": "running", "error": None,
                       "out": "", "tail": ["working"], "n_figures": None, "records": [],
                       "analysis": "my:count_rows"}]}}

    def analysis_cancel(self, job_id: str = "") -> Dict[str, Any]:
        self.analysis_cancels.append(job_id)
        if self.error:
            return {"ok": False, "error": self.error, "status": None, "message": ""}
        return {"ok": True, "error": None, "status": {"state": "cancelled", "id": "an_1", "items": []},
                "message": "Stopped the analysis job an_1; the measurement is not touched."}

    def datasets(self, limit: int = 20) -> Dict[str, Any]:
        self.dataset_calls.append(limit)
        if self.error:
            return {"ok": False, "error": self.error, "datasets": []}
        return {"ok": True, "error": None, "root": "C:/data", "datasets": [
            {"path": "C:/data/2026-10-30/#001_RT_10-00-00", "rel": "2026-10-30/#001_RT_10-00-00",
             "day": "2026-10-30", "folder": "#001_RT_10-00-00", "class": "RT", "bytes": 1024,
             "dat": ["a.dat"], "modified": "2026-10-30T10:05:00", "started": "2026-10-30T10:00:00",
             "finished": "2026-10-30T10:05:00", "warn": None}]}

    def dataset_files(self, path: str) -> Dict[str, Any]:
        self.dataset_file_calls.append(path)
        if self.error:
            return {"ok": False, "error": self.error, "path": "", "files": [], "dirs": []}
        return {"ok": True, "error": None, "path": path, "rel": "2026-10-30/#001_RT_10-00-00",
                "files": [{"name": "a.dat", "bytes": 1024, "modified": "2026-10-30T10:05:00"},
                          {"name": "snapshot.json", "bytes": 200, "modified": "2026-10-30T10:05:00"}],
                "dirs": []}

    def log_lines(self, lines: int = 50) -> Dict[str, Any]:
        self.log_calls.append(lines)
        if self.error:
            return {"ok": False, "error": self.error, "lines": [], "seq": 0}
        return {"ok": True, "error": None, "seq": 2, "lines": [
            {"n": 1, "at": "2026-10-30 10:00:00", "lvl": "info", "uid": "", "text": "sequence started"},
            {"n": 2, "at": "2026-10-30 10:00:05", "lvl": "ok", "uid": "it_1",
             "text": "block example_measurement done"}]}

    def snapshot(self) -> Dict[str, Any]:   # the Home page: Kelvin's own, still drawn while the engine cannot answer
        return {"ok": True, "error": None, "greeting": "Good morning - the sequence engine is running in Kelvin.",
                "tiles": [{"id": "measurement", "title": "Measurement", "value": "1/2",
                           "detail": "50% done", "tone": "busy"},
                          {"id": "history", "title": "History", "value": "1", "detail": "1 run",
                           "tone": "idle"}],
                "cards": self.home.cards(), "updated_at": "2026-10-30T10:00:00-04:00"}

    # -- the arm switch, and Kelvin's measurements in the Sequence
    def armed(self) -> bool:
        if self.armed_error is not None:
            raise self.armed_error
        return bool(self.armed_value)

    def record_kelvin_run(self, name: str, kind: str = "measurement", status: str = "done", detail: str = "",
                          run_id: str = "", started: Any = None) -> Dict[str, Any]:
        if self.record_error is not None:
            raise self.record_error
        self.records.append({"name": name, "kind": kind, "status": status, "detail": detail, "run_id": run_id,
                             "started": started})
        return {"ok": True}

    def ensure_sequence_name(self, name: str) -> Dict[str, Any]:
        if self.ensure_error is not None:
            raise self.ensure_error
        self.named.append(name)
        return {"ok": True, "name": name}

    def HomeStore(self) -> FakeHome:   # web/labmaster.py's own store of the Home page cards
        return self.home


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """The engine's facade, faked: the tools answer through it and every call it gets is recorded."""
    fake = FakeEngine()
    monkeypatch.setattr(agent_server, "_labmaster_bridge", lambda: (fake, ""))
    return fake


@pytest.fixture
def runtime(tmp_path):
    lab = build_virtual_lab(seed=1)
    return LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))


def call(runtime, *calls):
    """Run tool calls over a real MCP client session; return their text results."""
    async def main():
        out = []
        async with Client(build_server(runtime)) as client:
            for name, args in calls:
                result = await client.call_tool(name, args)
                out.append("\n".join(c.text for c in result.content if getattr(c, "type", "") == "text"))
        return out
    return anyio.run(main)


class RecordingApprover(Approver):
    """A person who answers the approval card at once, and remembers what was asked."""

    def __init__(self, answer: bool):
        self.answer = answer
        self.requests: List[Any] = []

    def approve(self, request: Any) -> bool:
        self.requests.append(request)
        request.note = "approved by a person (portal)" if self.answer else "declined by a person (portal)"
        return self.answer


def person_runtime(tmp_path, answer):
    lab = build_virtual_lab(seed=1)
    return LabRuntime(lab, RecordingApprover(answer), tmp_path / "data", demo_envelope(lab))


# ---------------------------------------------------------------- the tool table and where the tools sit


def test_labmaster_tools_are_in_the_table(runtime):
    async def main():
        async with Client(build_server(runtime)) as client:
            return (await client.list_tools()).tools
    tools = {t.name: t for t in anyio.run(main)}
    assert set(LABMASTER_TOOLS) <= set(tools), sorted(set(LABMASTER_TOOLS) - set(tools))
    for name in LABMASTER_READ:
        assert tools[name].annotations.read_only_hint is True, name
    for name in LABMASTER_ACT:
        assert tools[name].annotations.read_only_hint is False, name
    assert tools["labmaster_control"].input_schema["required"] == ["action"]
    assert tools["labmaster_rename_sequence"].input_schema["required"] == ["name"]
    assert tools["labmaster_move_block"].input_schema["required"] == ["uid", "index"]
    assert tools["labmaster_write_script"].input_schema["required"] == ["name", "code"]
    assert tools["labmaster_run_analysis"].input_schema["required"] == ["script"]
    assert {n for n in tools if n.startswith("labmaster_")} == set(LABMASTER_TOOLS)


def test_the_core_tools_stay_and_find_tools_loads_the_rest(tmp_path):
    """The 11 core tools are in every chat's first step; the other 23 load with find_tools (a call by name too)."""
    import asyncio

    from labharness.web.agent import DEFERRED, DEFERRED_NAMES, ChatAgent
    from labharness.web.store import ChatStore

    assert not (set(ALWAYS_ON_LABMASTER) & DEFERRED_NAMES), sorted(set(ALWAYS_ON_LABMASTER) & DEFERRED_NAMES)
    assert DEFERRED_LABMASTER == set(LABMASTER_TOOLS) - set(ALWAYS_ON_LABMASTER)
    assert DEFERRED_LABMASTER <= DEFERRED_NAMES                    # all of them can be loaded
    groups = [what for what, names in DEFERRED.items() if DEFERRED_LABMASTER <= set(names)]
    assert len(groups) == 1, groups                                # one purpose group for "# More tools"
    assert "labmaster" in groups[0].lower()

    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    agent = ChatAgent(rt, ChatStore(tmp_path / "chats"), build_server(rt), {})

    async def go():
        await agent.start()
        try:
            chat = {"tools": []}
            first = {t["name"] for t in agent._lab_tools(chat)}
            assert set(ALWAYS_ON_LABMASTER) <= first
            assert not (DEFERRED_LABMASTER & first), sorted(DEFERRED_LABMASTER & first)
            assert DEFERRED_LABMASTER <= {t["name"] for t in agent.tools}   # they are in the server's table
            context = {"tools": []}
            loaded, _ = agent._find_tools(chat, context, {"query": "labmaster_read_script"})
            assert loaded.startswith("loaded"), loaded
            assert "labmaster_read_script" in {t["name"] for t in agent._lab_tools(chat)}
            assert "labmaster_read_script" in {t["name"] for t in context["tools"]}
            assert "labMaster's saved sequences" in agent._more_tools(chat)
        finally:
            await agent.close()
    asyncio.run(go())


def test_the_sequence_tools_do_not_wait_for_the_lab():
    """The reads, the Home page cards and the library/scripts/History/analysis/data/log touch no instrument (P2: the
    lab's lock waits are off). The sequence's own edits and control stay on the lab, as they change what runs."""
    from labharness.web.agent import OFF_LAB
    assert SEQUENCE_OFF_LAB <= OFF_LAB, sorted(SEQUENCE_OFF_LAB - OFF_LAB)
    assert not (SEQUENCE_ON_LAB & OFF_LAB), sorted(SEQUENCE_ON_LAB & OFF_LAB)


# ---------------------------------------------------------------- in-process semantics


def test_status_shows_the_in_process_engine_version_and_the_arm(runtime, engine):
    text, = call(runtime, ("labmaster_status", {}))
    assert text.startswith("Sequence engine (in-process): SIM, v0.1.7"), text
    assert "127.0.0.1" not in text and "http://" not in text
    assert "AI may start runs off" in text, text
    engine.armed_value = True
    text, = call(runtime, ("labmaster_status", {}))
    assert "AI may start runs on" in text, text


# every tool with the arguments a call needs, so one test can ask all of them what they answer when the engine cannot
TOOL_ARGS: Dict[str, Dict[str, Any]] = {
    "labmaster_status": {},
    "labmaster_functions": {},
    "labmaster_saved": {},
    "labmaster_home_cards": {},
    "labmaster_read_script": {"path": "scripts/demo.py"},
    "labmaster_history": {},
    "labmaster_run_detail": {"run_id": "run_1"},
    "labmaster_analysis_scripts": {},
    "labmaster_analysis_status": {},
    "labmaster_add_block": {"block": "blk_wait"},
    "labmaster_update_block": {"uid": "blk_1", "params": {"minutes": 2}},
    "labmaster_delete_block": {"uid": "it_1"},
    "labmaster_control": {"action": "start"},
    "labmaster_rename_sequence": {"name": "laser check"},
    "labmaster_import_script": {"path": "scripts/example_measurement.py"},
    "labmaster_home_add_card": {"kind": "note", "title": "Test note", "text": "From the test."},
    "labmaster_home_remove_card": {"card_id": "card_missing"},
    "labmaster_move_block": {"uid": "it_1", "index": 0},
    "labmaster_duplicate_block": {"uid": "it_1"},
    "labmaster_clear_sequence": {"confirm": True},
    "labmaster_saved_save": {"name": "Morning"},
    "labmaster_saved_load": {"id": "seq_1"},
    "labmaster_saved_delete": {"id": "seq_1", "confirm": True},
    "labmaster_write_script": {"name": "demo", "code": "reps = 3\n"},
    "labmaster_function_update": {"id": "blk_wait", "name": "Wait 2"},
    "labmaster_function_duplicate": {"id": "blk_wait"},
    "labmaster_function_remove": {"id": "blk_wait", "confirm": True},
    "labmaster_write_analysis": {"name": "count_rows", "code": "PARAMS = {}\n"},
    "labmaster_run_analysis": {"script": "my:0:count_rows.py",
                               "dataset": "C:/data/2026-10-30/#001_RT_10-00-00"},
    "labmaster_analysis_cancel": {"confirm": True},
    "labmaster_datasets": {},
    "labmaster_dataset_files": {"path": "C:/data/2026-10-30/#001_RT_10-00-00"},
    "labmaster_log": {},
    "labmaster_home": {},
}
# the tools that go into the engine (the Home page's cards and the Home page itself are Kelvin's own, and still work)
ENGINE_BACKED = [n for n in LABMASTER_TOOLS
                 if n not in ("labmaster_home_cards", "labmaster_home_add_card",
                              "labmaster_home_remove_card", "labmaster_home")]


def test_every_tool_answers_in_words_when_the_engine_cannot(runtime, engine):
    """The engine is in this process: a failure is its own words, never a panel, a port or a start command."""
    engine.error = "the sequence engine could not start: its sequence folder cannot be written"
    engine.armed_value = True   # start goes straight to the engine: no approval card in this test
    calls = [(name, TOOL_ARGS[name]) for name in LABMASTER_TOOLS]
    out = call(runtime, *calls)
    assert len(out) == len(LABMASTER_TOOLS)
    for name, text in zip(LABMASTER_TOOLS, out):
        assert isinstance(text, str) and text.strip(), name
        assert "Traceback" not in text, (name, text)
        for stale in ("http://", "127.0.0.1", "panel not running", "panel_server.py", "python -u panel_server"):
            assert stale not in text, (name, stale, text)
    said = dict(zip(LABMASTER_TOOLS, out))
    for name in ENGINE_BACKED:
        assert "the sequence engine could not" in said[name], (name, said[name])
    # the Home page is Kelvin's own file, not the engine's: its cards still work while the engine cannot
    assert said["labmaster_home_add_card"].startswith("card on the home page: Test note")
    assert said["labmaster_home_remove_card"].startswith("not removed")
    assert said["labmaster_home"].startswith("Kelvin Home: ")


def test_the_bridge_imports():
    """The tools' way into the engine (web/labmaster.py) is there; this fails with why when it does not import."""
    lm, why = agent_server._labmaster_bridge()
    assert lm is not None, why


# ---------------------------------------------------------------- the arm switch: a person's decision


def test_start_when_the_person_armed_it_goes_straight_through(tmp_path, engine):
    """'AI may start runs' on (a person switched it on in the Sequence page): no approval card, the engine starts."""
    runtime = person_runtime(tmp_path, True)
    engine.armed_value = True
    (text,) = call(runtime, ("labmaster_control", {"action": "start"}))
    assert runtime.approver.requests == [], "the arm was on, yet a person was asked"
    assert engine.control_calls == [("start", {})], engine.control_calls
    assert "AI may start runs" in text, text
    assert "carried out start" in text, text


def test_start_without_the_arm_asks_a_person_first(tmp_path, engine):
    """With the arm switch off, the AI still cannot start: the call blocks on a person's card, then starts."""
    runtime = person_runtime(tmp_path, True)
    engine.armed_value = False
    (text,) = call(runtime, ("labmaster_control", {"action": "start"}))
    cards = runtime.approver.requests
    assert len(cards) == 1, "start reached the engine without asking a person"
    card = cards[0]
    assert "labMaster" in card.title and "sequence" in card.title
    assert any("R(T) demo" in line for line in card.items), card.items
    assert any("example_measurement" in line for line in card.items), card.items
    assert any("safety gate" in line for line in card.items), card.items   # says what cannot be checked
    assert any("AI may start runs" in line for line in card.items), card.items   # how to stop being asked
    assert engine.control_calls == [("start", {})], engine.control_calls
    assert "carried out start" in text, text
    assert "approved" in text, text          # the answer says a person's card was what started it


def test_start_declined_by_the_person_starts_nothing(tmp_path, engine):
    runtime = person_runtime(tmp_path, False)
    engine.armed_value = False
    (text,) = call(runtime, ("labmaster_control", {"action": "start"}))
    assert engine.control_calls == [], "the engine was asked to start although the person declined"
    assert text.startswith("not started: "), text
    assert "declined" in text, text


def test_start_without_a_person_to_ask_starts_nothing(tmp_path, engine):
    runtime = person_runtime(tmp_path, True)
    runtime.approver = None          # no approval card on this Kelvin: the AI still cannot start anything
    engine.armed_value = False
    (text,) = call(runtime, ("labmaster_control", {"action": "start"}))
    assert engine.control_calls == [], engine.control_calls
    assert text.startswith("not started: "), text
    assert "person" in text


def test_an_arm_switch_that_cannot_be_read_asks_a_person(tmp_path, engine):
    """An engine that cannot say whether the arm is on is not an arm: the person is asked, as with it off."""
    runtime = person_runtime(tmp_path, True)
    engine.armed_error = RuntimeError("state.json unreadable")
    (text,) = call(runtime, ("labmaster_control", {"action": "start"}))
    assert len(runtime.approver.requests) == 1, "an unreadable arm switch started the sequence"
    assert engine.control_calls == [("start", {})], engine.control_calls
    assert "approved" in text, text


# ---------------------------------------------------------------- the rest of the control tool


@pytest.mark.parametrize("action", GATED)
def test_gated_control_needs_the_users_yes_first(runtime, engine, action):
    (text,) = call(runtime, ("labmaster_control", {"action": action}))
    assert text.startswith("needs the user's confirmation first: sequence " + action), text
    assert "confirm=true" in text
    assert engine.control_calls == [], engine.control_calls


def test_stop_and_safe_stop_need_no_confirmation(runtime, engine):
    """Only a run needs a person: stopping one never waits for a confirm the AI could give itself."""
    stopped, safe = call(runtime, ("labmaster_control", {"action": "stop"}),
                         ("labmaster_control", {"action": "safe_stop"}))
    assert [c[0] for c in engine.control_calls] == ["stop", "safe_stop"], engine.control_calls
    for text in (stopped, safe):
        assert "confirmation first" not in text, text
        assert "carried out" in text or "Safe stop" in text, text


def test_control_refuses_what_it_does_not_know(runtime, engine):
    (text,) = call(runtime, ("labmaster_control", {"action": "explode"}))
    assert text.startswith("not run: unknown action"), text
    assert engine.control_calls == []


def test_set_wait_needs_its_minutes(runtime, engine):
    (text,) = call(runtime, ("labmaster_control", {"action": "set_wait", "confirm": True}))
    assert text.startswith("not run: set_wait needs minutes > 0"), text


# ---------------------------------------------------------------- starting after a failed block: the person's choice


def test_start_after_a_failed_block_passes_the_persons_choice(runtime, engine):
    """The engine refuses a bare Start while a block failed: failed='retry'|'skip' and the uids go through to it."""
    engine.armed_value = True
    (started,) = call(runtime, ("labmaster_control", {"action": "start", "failed": "retry",
                                                     "failed_uids": ["it_failed"]}))
    assert engine.control_calls == [("start", {"failed": "retry", "failed_uids": ["it_failed"]})], \
        engine.control_calls
    assert "carried out start" in started, started


def test_start_after_a_failed_block_needs_its_uids(runtime, engine):
    engine.armed_value = True
    (text,) = call(runtime, ("labmaster_control", {"action": "start", "failed": "skip"}))
    assert text.startswith("not run: failed='skip' needs failed_uids"), text
    assert engine.control_calls == [], engine.control_calls


def test_the_failed_choice_is_retry_or_skip(runtime, engine):
    engine.armed_value = True
    (text,) = call(runtime, ("labmaster_control", {"action": "start", "failed": "maybe",
                                                   "failed_uids": ["it_failed"]}))
    assert text.startswith("not run: failed must be 'retry' or 'skip'"), text
    assert engine.control_calls == [], engine.control_calls


def test_a_bare_start_the_engine_refuses_is_said_in_words(runtime, engine):
    engine.armed_value = True
    engine.error = "start: block #1 failed - start with failed='retry' or 'skip' and failed_uids"
    (text,) = call(runtime, ("labmaster_control", {"action": "start"}))
    assert text.startswith("not run: "), text
    assert "failed" in text and "retry" in text, text


def test_update_refuses_an_empty_change(runtime, engine):
    (text,) = call(runtime, ("labmaster_update_block", {"uid": "blk_1"}))
    assert text.startswith("not updated: pass params"), text


def test_delete_of_a_failed_block_needs_the_users_yes(runtime, engine):
    """The engine's own MCP rule: removing a FAILED block decides the person's Retry/Skip, so it is their call."""
    engine.state_dict["sequence"]["items"] = [
        {"uid": "it_failed", "name": "Scan", "status": "error", "message": "GPIB timeout"},
        {"uid": "it_pending", "name": "Wait", "status": "pending"}]
    refused, pending = call(runtime, ("labmaster_delete_block", {"uid": "it_failed"}),
                            ("labmaster_delete_block", {"uid": "it_pending"}))
    assert refused.startswith("needs the user's confirmation first: sequence delete"), refused
    assert "block #1 'Scan' failed" in refused and "GPIB timeout" in refused, refused
    assert engine.deleted == ["it_pending"], engine.deleted   # a pending block: no confirm wanted


def test_add_block_needs_a_function(runtime, engine):
    (text,) = call(runtime, ("labmaster_add_block", {"block": "  "}))
    assert text.startswith("not added: say which function"), text


# ---------------------------------------------------------------- naming the sequence


def test_rename_sequence_names_it(runtime, engine):
    (text,) = call(runtime, ("labmaster_rename_sequence", {"name": "  laser check  "}))
    assert engine.renames == ["laser check"], engine.renames
    assert "renamed the sequence: laser check" in text, text


def test_rename_sequence_refuses_an_empty_name(runtime, engine):
    (text,) = call(runtime, ("labmaster_rename_sequence", {"name": "   "}))
    assert text.startswith("not renamed: give the sequence a name"), text
    assert engine.renames == [], engine.renames


def test_rename_sequence_says_what_the_engine_said(runtime, engine):
    engine.error = "the sequence engine could not save the sequence"
    (text,) = call(runtime, ("labmaster_rename_sequence", {"name": "laser check"}))
    assert text.startswith("not renamed: "), text
    assert "the sequence engine could not" in text, text


# ---------------------------------------------------------------- the Home page cards


def test_home_cards_round_trip(runtime, engine):
    added, listed, removed = call(runtime,
                                  ("labmaster_home_add_card", {"kind": "note", "title": "Test note",
                                                               "text": "From the test."}),
                                  ("labmaster_home_cards", {}),
                                  ("labmaster_home_remove_card", {"card_id": "card_1"}))
    assert added.startswith("card on the home page: Test note"), added
    assert "Test note" in listed and "card_1" in listed, listed
    assert removed == "removed from the home page: card_1", removed


def test_home_card_needs_a_known_kind_and_a_title(runtime, engine):
    bad_kind, no_title, no_items = call(runtime,
                                        ("labmaster_home_add_card", {"kind": "poster", "title": "x"}),
                                        ("labmaster_home_add_card", {"kind": "note", "title": ""}),
                                        ("labmaster_home_add_card", {"kind": "checklist", "title": "Steps"}))
    assert bad_kind.startswith("not added: kind must be one of")
    assert no_title.startswith("not added: a card needs a title")
    assert no_items.startswith("not added: a checklist card needs items")


# ---------------------------------------------------------------- the AI's measurements go into the Sequence

TODAY = datetime.now().strftime("%Y-%m-%d")
SUCCESS_PLAN = ("run 20261030-120000-laser-check done: 5 points, 2.0 s\n"
                "rows: get_run_data(run_id='20261030-120000-laser-check')")
FAILED_PLAN = "run 20261030-120001-laser-check aborted after 2/5 points: stop requested"


def test_a_plan_run_is_recorded_in_the_sequence_and_names_it(runtime, engine, monkeypatch):
    monkeypatch.setattr(runtime, "run", lambda plan, dry_run=False: SUCCESS_PLAN)
    (text,) = call(runtime, ("run_plan", {"plan": "plan: laser check\nmeasure: [smu.I]"}))
    assert text.startswith("run 20261030-120000-laser-check done"), text   # the tool's answer is unchanged
    assert engine.records == [{"name": "laser check", "kind": "plan", "status": "done",
                               "detail": SUCCESS_PLAN.splitlines()[0],
                               "run_id": "20261030-120000-laser-check", "started": None}], engine.records
    assert engine.named == ["laser check--" + TODAY], engine.named


def test_an_aborted_run_is_recorded_as_failed(runtime, engine, monkeypatch):
    monkeypatch.setattr(runtime, "run", lambda plan, dry_run=False: FAILED_PLAN)
    call(runtime, ("run_plan", {"plan": "plan: laser check\nmeasure: [smu.I]"}))
    assert [r["status"] for r in engine.records] == ["failed"], engine.records
    assert engine.records[0]["name"] == "laser check", engine.records
    assert engine.records[0]["run_id"] == "20261030-120001-laser-check", engine.records
    assert engine.named == ["laser check--" + TODAY], engine.named


def test_a_plan_that_never_ran_is_not_a_measurement(runtime, engine, monkeypatch):
    monkeypatch.setattr(runtime, "run", lambda plan, dry_run=False: "invalid plan: plan: a name is required")
    (text,) = call(runtime, ("run_plan", {"plan": "measure: [smu.I]"}))
    assert text.startswith("invalid plan:"), text
    assert engine.records == [] and engine.named == [], (engine.records, engine.named)


def test_a_rehearsal_is_not_recorded(runtime, engine, monkeypatch):
    monkeypatch.setattr(runtime, "run",
                        lambda plan, dry_run=False: "REHEARSAL ONLY, on the simulated lab: nothing was sent")
    call(runtime, ("run_plan", {"plan": "plan: laser check", "dry_run": True}))
    assert engine.records == [] and engine.named == [], (engine.records, engine.named)


def test_a_script_run_is_recorded_under_the_scripts_name(runtime, engine, monkeypatch):
    answer = ("script example_measurement done: 1.0 s, 3 instrument commands through the gate\n"
              "saved in C:\\data\\runs\\20261030-120000-example: script.py (as it ran), output.txt")
    monkeypatch.setattr(runtime, "run_script",
                        lambda script, params=None, args=None, drives=None, mark=None: answer)
    (text,) = call(runtime, ("run_script", {"script": "scripts/example_measurement.py"}))
    assert text.startswith("script example_measurement done"), text
    assert engine.records[0]["name"] == "example_measurement", engine.records
    assert engine.records[0]["kind"] == "script" and engine.records[0]["status"] == "done", engine.records
    assert engine.records[0]["run_id"] == "20261030-120000-example", engine.records
    assert engine.named == ["example_measurement--" + TODAY], engine.named


def test_a_script_that_never_started_is_not_recorded(runtime, engine, monkeypatch):
    monkeypatch.setattr(runtime, "run_script",
                        lambda script, params=None, args=None, drives=None, mark=None: "script not run: refused")
    call(runtime, ("run_script", {"script": "scripts/example_measurement.py"}))
    assert engine.records == [], engine.records


def test_a_recipe_run_is_recorded_under_the_recipes_name(runtime, engine, monkeypatch):
    answer = ("recipe hall-sweep (2 stages), values: x=1.0\n"
              "stage A: run 20261030-120002-a passed\ndone; values: x=1.0")
    monkeypatch.setattr(runtime, "run_recipe", lambda recipe, values=None: answer)
    call(runtime, ("run_recipe", {"recipe": "recipe: hall-sweep\nstages:\n- name: A\n  plan: plan: x"}))
    assert engine.records[0]["name"] == "hall-sweep", engine.records
    assert engine.records[0]["kind"] == "recipe" and engine.records[0]["status"] == "done", engine.records
    assert engine.named == ["hall-sweep--" + TODAY], engine.named


def test_a_recipe_that_stopped_is_recorded_as_failed(runtime, engine, monkeypatch):
    answer = ("recipe hall-sweep (2 stages), values: x=1.0\n"
              "stopped at stage B: run 20261030-120003-b did not pass x > 2")
    monkeypatch.setattr(runtime, "run_recipe", lambda recipe, values=None: answer)
    call(runtime, ("run_recipe", {"recipe": "recipe: hall-sweep\nstages:\n- name: A\n  plan: plan: x"}))
    assert engine.records[0]["status"] == "failed", engine.records
    assert engine.named == ["hall-sweep--" + TODAY], engine.named


def test_a_queued_plan_is_recorded_as_queued(runtime, engine, monkeypatch):
    """queue_add files a measurement in the Sequence page as queued; naming the sequence waits for it to run."""
    monkeypatch.setattr(runtime, "queue_add",
                        lambda item, position=None, note="": "added plan laser check as #1\nqueue: 1 item")
    (text,) = call(runtime, ("queue_add", {"item": "plan: laser check\nmeasure: [smu.I]"}))
    assert text.startswith("added plan laser check"), text
    assert engine.records[0]["name"] == "laser check", engine.records
    assert engine.records[0]["kind"] == "queue" and engine.records[0]["status"] == "queued", engine.records
    assert engine.named == [], engine.named


def test_recording_a_run_never_changes_the_answer(runtime, engine, monkeypatch):
    """The measurement already happened: a Sequence that cannot record it must not fail the run."""
    monkeypatch.setattr(runtime, "run", lambda plan, dry_run=False: SUCCESS_PLAN)
    engine.record_error = RuntimeError("state/kelvin_runs.json unreadable")
    engine.ensure_error = OSError("the sequence folder is read-only")
    (text,) = call(runtime, ("run_plan", {"plan": "plan: laser check\nmeasure: [smu.I]"}))
    assert text.startswith("run 20261030-120000-laser-check done"), text
    assert "Traceback" not in text and "could not" not in text, text
    assert engine.records == [] and engine.named == [], (engine.records, engine.named)


# ---------------------------------------------------------------- v2-C: the sequence beyond add/update/delete


def test_move_and_duplicate_a_block(runtime, engine):
    moved, duplicated = call(runtime, ("labmaster_move_block", {"uid": "it_1", "index": 2}),
                             ("labmaster_duplicate_block", {"uid": "it_1"}))
    assert engine.moved == [{"uid": "it_1", "index": 2}], engine.moved
    assert "moved" in moved.lower() and "confirmation first" not in moved, moved
    assert engine.duplicated == ["it_1"], engine.duplicated
    assert "it_copy" in duplicated, duplicated      # the copy's uid is given back, to change or move it


def test_move_needs_a_uid(runtime, engine):
    (no_uid,) = call(runtime, ("labmaster_move_block", {"uid": " ", "index": 0}))
    assert no_uid.startswith("not moved: say which block"), no_uid
    assert engine.moved == [], engine.moved


def test_clear_needs_the_users_yes(runtime, engine):
    (refused,) = call(runtime, ("labmaster_clear_sequence", {}))
    assert refused.startswith("needs the user's confirmation first: sequence clear"), refused
    assert "confirm=true" in refused and engine.cleared == [], engine.cleared
    (done,) = call(runtime, ("labmaster_clear_sequence", {"confirm": True, "keep_done": True}))
    assert engine.cleared == [True], engine.cleared
    assert "cleared" in done.lower(), done


@pytest.mark.parametrize("tool,args", GATED_DESTRUCTIVE)
def test_destructive_tools_need_the_users_yes_first(runtime, engine, tool, args):
    (text,) = call(runtime, (tool, args))
    assert text.startswith("needs the user's confirmation first: sequence "), text
    assert "confirm=true" in text
    assert not any((engine.cleared, engine.saves, engine.loads, engine.saved_deleted,
                    engine.function_removes, engine.analysis_cancels)), "a destructive call reached the engine"


def test_saved_sequences_save_load_delete(runtime, engine):
    saved, loaded, deleted = call(runtime,
                                  ("labmaster_saved_save", {"name": "Morning"}),
                                  ("labmaster_saved_load", {"id": "seq_1", "append": True}),
                                  ("labmaster_saved_delete", {"id": "seq_1", "confirm": True}))
    assert engine.saves == [{"name": "Morning", "overwrite": False}], engine.saves
    assert engine.loads == [{"id": "seq_1", "name": "", "append": True, "replace": False}], engine.loads
    assert engine.saved_deleted == [{"id": "seq_1", "name": ""}], engine.saved_deleted
    assert "seq_9" in saved and "labmaster_saved_load" in saved, saved   # where to go on from the save
    for text in (saved, loaded, deleted):
        assert "confirmation first" not in text, text


def test_saved_load_refuses_append_and_replace_together(runtime, engine):
    (text,) = call(runtime, ("labmaster_saved_load", {"id": "seq_1", "append": True, "replace": True}))
    assert text.startswith("not loaded: append and replace are different"), text
    assert engine.loads == []


# ---------------------------------------------------------------- v2-C: the function library's own edits


def test_function_update_duplicate_remove(runtime, engine):
    updated, copied, removed = call(runtime,
                                    ("labmaster_function_update", {"id": "blk_wait", "name": "Wait 2",
                                                                   "note": "two minutes"}),
                                    ("labmaster_function_duplicate", {"id": "blk_wait"}),
                                    ("labmaster_function_remove", {"id": "blk_copy", "confirm": True}))
    assert engine.function_updates[0]["name"] == "Wait 2", engine.function_updates
    assert engine.function_updates[0]["note"] == "two minutes", engine.function_updates
    assert "Updated the function" in updated and "library now has 1 function(s)" in updated, updated
    assert engine.function_duplicates == ["blk_wait"] and "Copied the function" in copied, copied
    assert engine.function_removes == ["blk_copy"], engine.function_removes
    assert "Removed the function" in removed, removed


def test_write_script_returns_the_parameters_it_found(runtime, engine):
    (text,) = call(runtime, ("labmaster_write_script", {"name": "demo_scan",
                                                       "code": '"""Demo."""\nn_points = 21\n'}))
    assert engine.script_writes[0]["name"] == "demo_scan.py", engine.script_writes
    assert "parameters: n_points (num = 21)" in text, text
    assert "scripts/demo_scan.py" in text, text


def test_write_script_asks_before_replacing_one_there(runtime, engine):
    engine.existing_scripts = {"demo_scan.py"}
    refused, replaced = call(runtime,
                             ("labmaster_write_script", {"name": "demo_scan", "code": "x=1\n",
                                                         "overwrite": True}),
                             ("labmaster_write_script", {"name": "demo_scan", "code": "x=2\n",
                                                         "overwrite": True, "confirm": True}))
    assert refused.startswith("needs the user's confirmation first: sequence write script"), refused
    assert "demo_scan.py" in refused, refused
    assert [w["overwrite"] for w in engine.script_writes] == [False, True], engine.script_writes
    assert "Wrote" in replaced, replaced


def test_read_script_returns_the_code_and_its_parameters(runtime, engine):
    (text,) = call(runtime, ("labmaster_read_script", {"path": "scripts/demo.py"}))
    assert text.startswith("script scripts/demo.py (main())"), text
    assert "reps (num = 3)" in text and "```python" in text and "def main():" in text, text


# ---------------------------------------------------------------- v2-C: History, analysis, data, log and Home


def test_history_lists_the_runs_with_their_blocks(runtime, engine):
    (text,) = call(runtime, ("labmaster_history", {"limit": 5}))
    assert text.startswith("sequence History: 1 run(s), newest first"), text
    assert "R(T) demo - done" in text and "run_id run_1" in text, text
    assert "- example_measurement (script; done; params reps=1, n_points=21" in text, text
    assert "dataset C:/data" in text, text


def test_history_says_when_there_is_nothing(runtime, engine):
    engine.history_runs = lambda limit=20, query="": {"ok": True, "error": None, "runs": []}
    (text,) = call(runtime, ("labmaster_history", {"query": "nothing"}))
    assert "no runs in the sequence engine's History yet" in text, text
    assert 'matching "nothing"' in text, text


def test_run_detail_panel_and_kelvin(runtime, engine):
    panel, kelvin = call(runtime, ("labmaster_run_detail", {"run_id": "run_1"}),
                         ("labmaster_run_detail", {"run_id": "kr_1"}))
    assert panel.startswith("run run_1: R(T) demo - done"), panel
    assert "1. example_measurement (script; done" in panel and "dataset: C:/data" in panel, panel
    assert kelvin.startswith("Kelvin's own measurement kr_1: laser check - done"), kelvin
    assert "kind plan" in kelvin and "5 points" in kelvin, kelvin


def test_analysis_scripts_list_their_params_and_where(runtime, engine):
    (text,) = call(runtime, ("labmaster_analysis_scripts", {}))
    assert text.startswith("analysis scripts: 1 in C:/Kelvin/sequence/my_analysis"), text
    assert "my:0:count_rows.py: count_rows (1 parameter(s): column (str = 'x')" in text, text
    assert "classes RT" in text and "templates for a new script" in text, text


def test_write_analysis_returns_its_record(runtime, engine):
    (text,) = call(runtime, ("labmaster_write_analysis", {"name": "count_rows",
                                                         "code": "PARAMS = {}\n"}))
    assert engine.analysis_writes[0]["name"] == "count_rows.py", engine.analysis_writes
    assert "parameters: column (str = 'x')" in text, text
    assert "labmaster_run_analysis(script='my:0:count_rows.py')" in text, text


def test_write_analysis_asks_before_replacing_one_there(runtime, engine):
    engine.existing_analyses = {"count_rows.py"}
    refused, replaced = call(runtime,
                             ("labmaster_write_analysis", {"name": "count_rows", "code": "x=1\n",
                                                           "overwrite": True}),
                             ("labmaster_write_analysis", {"name": "count_rows", "code": "x=2\n",
                                                           "overwrite": True, "confirm": True}))
    assert refused.startswith("needs the user's confirmation first: sequence write analysis"), refused
    assert [w["overwrite"] for w in engine.analysis_writes] == [False, True], engine.analysis_writes
    assert "Wrote the analysis script" in replaced, replaced


def test_run_analysis_starts_a_job(runtime, engine):
    (text,) = call(runtime, ("labmaster_run_analysis", {"script": "count_rows",
                                                       "dataset": ["C:/data/a", "C:/data/b"],
                                                       "params": {"column": "y"}}))
    assert engine.analysis_runs[0]["script"] == "count_rows", engine.analysis_runs
    assert engine.analysis_runs[0]["params"] == {"column": "y"}, engine.analysis_runs
    assert "job id an_1" in text, text


def test_analysis_status_shows_the_job(runtime, engine):
    (running,) = call(runtime, ("labmaster_analysis_status", {"job_id": "an_1"}))
    assert running.startswith("analysis running: job an_1, 0 of 1 run(s) done"), running
    assert "my:0:count_rows.py on 1 dataset(s); running" in running, running
    assert "log: working" in running, running
    engine.analysis_status = lambda: {"ok": True, "error": None, "status": {"state": "idle"}}
    (idle,) = call(runtime, ("labmaster_analysis_status", {}))
    assert idle.startswith("analysis: idle"), idle


def test_analysis_cancel_needs_the_users_yes(runtime, engine):
    refused, cancelled = call(runtime, ("labmaster_analysis_cancel", {"job_id": "an_1"}),
                              ("labmaster_analysis_cancel", {"job_id": "an_1", "confirm": True}))
    assert refused.startswith("needs the user's confirmation first: sequence cancel analysis"), refused
    assert "Stopped the analysis job an_1" in cancelled, cancelled
    assert engine.analysis_cancels == ["an_1"], engine.analysis_cancels


def test_datasets_and_their_files(runtime, engine):
    datasets, files = call(runtime, ("labmaster_datasets", {"limit": 5}),
                           ("labmaster_dataset_files", {"path": "C:/data/2026-10-30/#001_RT_10-00-00"}))
    assert engine.dataset_calls == [5], engine.dataset_calls
    assert datasets.startswith("data folder C:/data: 1 dataset(s), newest first"), datasets
    assert "2026-10-30/#001_RT_10-00-00 (RT; 1024 bytes of .dat; 1 .dat file(s)" in datasets, datasets
    assert files.startswith("dataset 2026-10-30/#001_RT_10-00-00: 2 file(s), 0 subfolder(s)"), files
    assert "a.dat, 1024 bytes" in files and "snapshot.json" in files, files


def test_log_and_home(runtime, engine):
    log, home = call(runtime, ("labmaster_log", {"lines": 5}), ("labmaster_home", {}))
    assert engine.log_calls == [5], engine.log_calls
    assert log.startswith("the sequence engine's log, newest 2 row(s)"), log
    assert "[ok] <it_1> block example_measurement done" in log, log
    assert home.startswith("Kelvin Home: Good morning"), home
    assert "- Measurement: 1/2" in home and "AI cards: 0" in home, home
    assert "http://" not in log and "127.0.0.1" not in home


# ---------------------------------------------------------------- v2-C against the real engine: real files


@pytest.fixture
def real_engine(tmp_path, monkeypatch):
    """The real web/labmaster.py over a real engine in a temporary home: the tools write real scripts there."""
    from labharness.sequence import service as sequence_service
    import labharness.web.labmaster as labmaster
    monkeypatch.delenv("KELVIN_SEQUENCE_DIR", raising=False)
    sequence_service.reset_service()
    sequence_service.service(tmp_path / "Kelvin")
    monkeypatch.setattr(agent_server, "_labmaster_bridge", lambda: (labmaster, ""))
    try:
        yield tmp_path / "Kelvin" / "sequence"
    finally:
        sequence_service.reset_service()


def test_the_tools_write_a_script_the_engine_can_scan(runtime, real_engine):
    written, read_back = call(runtime,
                              ("labmaster_write_script",
                               {"name": "demo_scan",
                                "code": '"""Demo."""\nn_points = 21\n\n\ndef main():\n    print(n_points)\n'}),
                              ("labmaster_read_script", {"path": "scripts/demo_scan.py"}))
    assert (real_engine / "scripts" / "demo_scan.py").is_file()
    assert "parameters: n_points (num = 21)" in written, written
    assert read_back.startswith("script scripts/demo_scan.py (main())"), read_back
    assert "print(n_points)" in read_back, read_back


def test_write_script_refuses_paths_out_of_the_folder(runtime, real_engine):
    banned = call(runtime,
                  ("labmaster_write_script", {"name": "../escape.py", "code": "x = 1\n"}),
                  ("labmaster_write_script", {"name": "sub/dir.py", "code": "x = 1\n"}),
                  ("labmaster_write_script", {"name": "..\\escape.py", "code": "x = 1\n"}))
    for text in banned:
        assert "must be a plain file name" in text, text
    assert not (real_engine.parent / "escape.py").exists()
    assert not (real_engine / "sub").exists()


def test_write_script_needs_the_yes_to_replace_one_there(runtime, real_engine):
    first, refused, replaced = call(runtime,
                                    ("labmaster_write_script", {"name": "demo", "code": "x = 1\n"}),
                                    ("labmaster_write_script", {"name": "demo", "code": "x = 2\n",
                                                                "overwrite": True}),
                                    ("labmaster_write_script", {"name": "demo", "code": "x = 3\n",
                                                                "overwrite": True, "confirm": True}))
    assert "Wrote" in first, first
    assert refused.startswith("needs the user's confirmation first: sequence write script"), refused
    assert (real_engine / "scripts" / "demo.py").read_text(encoding="utf-8") == "x = 3\n"


def test_the_analysis_script_round_trip_against_the_real_engine(runtime, real_engine):
    written, listed = call(runtime,
                           ("labmaster_write_analysis",
                            {"name": "count_rows",
                             "code": 'PARAMS = {"column": "x"}\n\n\ndef main():\n    pass\n'}),
                           ("labmaster_analysis_scripts", {}))
    assert "parameters: column (str = 'x')" in written, written
    assert (real_engine / "my_analysis" / "count_rows.py").is_file()
    assert "my:0:count_rows.py" in listed and "count_rows" in listed, listed


def test_the_functions_round_trip_against_the_real_engine(runtime, real_engine):
    updated, duplicated = call(runtime,
                               ("labmaster_function_update", {"id": "blk_wait", "name": "Wait (2 min)",
                                                              "note": "two minutes"}),
                               ("labmaster_function_duplicate", {"id": "blk_wait"}))
    assert "library now has 2 function(s)" in updated, updated
    assert "Copied the function" in duplicated, duplicated
    copy_id = re.search(r"\[(blk_[0-9a-f]+)\]", duplicated).group(1)
    (removed,) = call(runtime, ("labmaster_function_remove", {"id": copy_id, "confirm": True}))
    assert "library now has 2 function(s)" in removed, removed
    (functions,) = call(runtime, ("labmaster_functions", {}))
    assert "Wait (2 min)" in functions and "two minutes" in functions, functions


def test_datasets_and_files_against_the_real_engine(runtime, real_engine):
    folder = real_engine / "data" / "2026-10-30" / "#001_RT_10-00-00"
    folder.mkdir(parents=True)
    (folder / "a.dat").write_text("a,b\n1,2\n", encoding="utf-8")
    (folder / "snapshot.json").write_text(
        '{"loop": {"ts_start": "2026-10-30 10:00:00", "ts_end": "2026-10-30 10:00:05"}}', encoding="utf-8")
    listed, files = call(runtime, ("labmaster_datasets", {}),
                         ("labmaster_dataset_files", {"path": str(folder)}))
    assert "2026-10-30/#001_RT_10-00-00 (RT;" in listed, listed
    assert "a.dat" in files and "snapshot.json" in files, files
    outside_dir = real_engine.parent          # Kelvin's folder: absolute, exists, outside the data folder
    (outside,) = call(runtime, ("labmaster_dataset_files", {"path": str(outside_dir)}))
    assert outside.startswith("not listed:") and "outside the data folder" in outside, outside
