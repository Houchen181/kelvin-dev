"""The Sequence engine's tools in Kelvin's MCP server: the measurement sequence, its function library and Kelvin's Home
page cards, for the in-app chat AI and for any outside MCP client (they share one tool table).

The engine now runs inside Kelvin (labharness/sequence/, wrapped by web/labmaster.py), so the tools talk to it in this
process: there is no panel on a port, no address to show, and a call the engine cannot make comes back in words, never
as a connection error. These tests put a fake facade where the engine would be, so every answer and every engine call
is checked without starting an engine or touching the person's ~/Kelvin; tests/test_sequence_service.py covers the
engine itself, and tests/test_labmaster_bridge.py the facade.
"""
import copy
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
from labharness.gate import Approver  # noqa: E402

LABMASTER_READ = ["labmaster_status", "labmaster_functions", "labmaster_saved", "labmaster_home_cards"]
LABMASTER_ACT = ["labmaster_add_block", "labmaster_update_block", "labmaster_delete_block", "labmaster_control",
                 "labmaster_rename_sequence", "labmaster_import_script", "labmaster_home_add_card",
                 "labmaster_home_remove_card"]
LABMASTER_TOOLS = LABMASTER_READ + LABMASTER_ACT
# the sequence reads and the Home page cards touch no instrument: a chat runs them without the lab's lock
SEQUENCE_OFF_LAB = {"labmaster_status", "labmaster_functions", "labmaster_saved", "labmaster_home_cards",
                    "labmaster_home_add_card", "labmaster_home_remove_card", "labmaster_rename_sequence"}
# the calls that change a run: they need the user's own yes (confirm=true) before the engine is asked. start is not
# among them: with the arm switch on it starts directly, and with it off a person answers the approval card
GATED = ("skip", "reset", "end_wait", "set_wait", "next_rep")

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
        self.snapshot: Dict[str, Any] = copy.deepcopy(SNAPSHOT)
        self.home = home or FakeHome()
        self.records: List[Dict[str, Any]] = []
        self.named: List[str] = []
        self.control_calls: List[Any] = []
        self.deleted: List[str] = []
        self.renames: List[str] = []
        self.added: List[Dict[str, Any]] = []
        self.updated: List[Any] = []

    # -- reads
    def ping(self) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "sim": None, "url": "in-process", "version": None, "error": self.error}
        return {"ok": True, "sim": True, "url": "in-process", "version": "0.1.7", "error": None}

    def state(self) -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "url": "in-process", "error": self.error, "state": None}
        return {"ok": True, "url": "in-process", "error": None, "state": copy.deepcopy(self.snapshot)}

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
        return {"ok": True, "error": None, "state": copy.deepcopy(self.snapshot), "message": message}

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
        self.snapshot["sequence"]["name"] = name
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
        return {"ok": True, "error": None, "state": copy.deepcopy(self.snapshot), "message": "Safe stop started."}

    def import_script(self, path: str, name: str = "") -> Dict[str, Any]:
        if self.error:
            return {"ok": False, "error": self.error, "entry": None, "palette": None, "message": ""}
        return {"ok": True, "error": None, "entry": {"id": "blk_new", "name": name or "example_measurement"},
                "palette": [{"id": "blk_new"}], "message": 'Added "%s" to the function library.'
                                                          % (name or "example_measurement")}

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
    assert {n for n in tools if n.startswith("labmaster_")} == set(LABMASTER_TOOLS)


def test_the_chat_ai_has_them_from_its_first_step():
    """Not among the rarer tools find_tools loads: a chat sees every one of these in its first step."""
    from labharness.web.agent import DEFERRED_NAMES
    assert not set(LABMASTER_TOOLS) & DEFERRED_NAMES


def test_the_sequence_tools_do_not_wait_for_the_lab():
    """The reads, the Home page cards and the rename touch no instrument (P2: the lab's lock waits are off). The
    start/control calls still stay on the lab, as they change what runs."""
    from labharness.web.agent import OFF_LAB
    assert SEQUENCE_OFF_LAB <= OFF_LAB, sorted(SEQUENCE_OFF_LAB - OFF_LAB)
    assert "labmaster_control" not in OFF_LAB and "labmaster_add_block" not in OFF_LAB


# ---------------------------------------------------------------- in-process semantics


def test_status_shows_the_in_process_engine_version_and_the_arm(runtime, engine):
    text, = call(runtime, ("labmaster_status", {}))
    assert text.startswith("Sequence engine (in-process): SIM, v0.1.7"), text
    assert "127.0.0.1" not in text and "http://" not in text
    assert "AI may start runs off" in text, text
    engine.armed_value = True
    text, = call(runtime, ("labmaster_status", {}))
    assert "AI may start runs on" in text, text


def test_every_tool_answers_in_words_when_the_engine_cannot(runtime, engine):
    """The engine is in this process: a failure is its own words, never a panel, a port or a start command."""
    engine.error = "the sequence engine could not start: its sequence folder cannot be written"
    engine.armed_value = True   # start goes straight to the engine: no approval card in this test
    out = call(runtime,
               ("labmaster_status", {}),
               ("labmaster_functions", {}),
               ("labmaster_saved", {}),
               ("labmaster_home_cards", {}),
               ("labmaster_add_block", {"block": "blk_wait"}),
               ("labmaster_update_block", {"uid": "blk_1", "params": {"minutes": 2}}),
               ("labmaster_delete_block", {"uid": "it_1"}),
               ("labmaster_control", {"action": "start"}),
               ("labmaster_rename_sequence", {"name": "laser check"}),
               ("labmaster_import_script", {"path": "scripts/example_measurement.py"}),
               ("labmaster_home_add_card", {"kind": "note", "title": "Test note", "text": "From the test."}),
               ("labmaster_home_remove_card", {"card_id": "card_missing"}))
    assert len(out) == len(LABMASTER_TOOLS)
    for name, text in zip(LABMASTER_TOOLS, out):
        assert isinstance(text, str) and text.strip(), name
        assert "Traceback" not in text, (name, text)
        for stale in ("http://", "127.0.0.1", "panel not running", "panel_server.py", "python -u panel_server"):
            assert stale not in text, (name, stale, text)
    said = dict(zip(LABMASTER_TOOLS, out))
    for name in ("labmaster_status", "labmaster_functions", "labmaster_saved", "labmaster_add_block",
                 "labmaster_update_block", "labmaster_delete_block", "labmaster_control",
                 "labmaster_rename_sequence", "labmaster_import_script"):
        assert "the sequence engine could not" in said[name], (name, said[name])
    # the Home page is Kelvin's own file, not the engine's: its cards still work while the engine cannot
    assert said["labmaster_home_add_card"].startswith("card on the home page: Test note")
    assert said["labmaster_home_remove_card"].startswith("not removed")


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


def test_update_refuses_an_empty_change(runtime, engine):
    (text,) = call(runtime, ("labmaster_update_block", {"uid": "blk_1"}))
    assert text.startswith("not updated: pass params"), text


def test_delete_of_a_failed_block_needs_the_users_yes(runtime, engine):
    """The engine's own MCP rule: removing a FAILED block decides the person's Retry/Skip, so it is their call."""
    engine.snapshot["sequence"]["items"] = [
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
