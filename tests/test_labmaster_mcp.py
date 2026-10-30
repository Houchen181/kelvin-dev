"""labMaster's tools in Kelvin's MCP server: the panel's measurement sequence, its function library and Kelvin's Home
page cards, for the in-app chat AI and for any outside MCP client (they share one tool table).

The panel is not needed for the tool table itself: these tests build the server without one, and the tests that call a
tool point the bridge at a port nothing listens on, then check that the answer is readable text (never an exception)
and says how to start the panel. The Home page lives in the person's ~/Kelvin/home.json: the tests use a file of
their own, never that one.
"""
import re
import socket
from typing import Any, List

import pytest

mcp = pytest.importorskip("mcp")
anyio = pytest.importorskip("anyio")

from mcp import Client  # noqa: E402

from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import Approver  # noqa: E402

LABMASTER_READ = ["labmaster_status", "labmaster_functions", "labmaster_saved", "labmaster_home_cards"]
LABMASTER_ACT = ["labmaster_add_block", "labmaster_update_block", "labmaster_delete_block", "labmaster_control",
                 "labmaster_import_script", "labmaster_home_add_card", "labmaster_home_remove_card"]
LABMASTER_TOOLS = LABMASTER_READ + LABMASTER_ACT
# the calls that change a run: they need the user's own yes (confirm=true) before the panel is asked. start is not
# among them: it asks a person on their approval card, and stop/safe_stop need no confirmation (they only end a run)
GATED = ("skip", "reset", "end_wait", "set_wait", "next_rep")
START = "python -u panel_server.py --sim --port 8770"
DEFAULT_URL = "http://127.0.0.1:8770"


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


def bridge():
    """web/labmaster.py, which the tools import when they first need it."""
    return pytest.importorskip("labharness.web.labmaster")


def closed_url():
    """A URL on this computer with nothing listening on it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return "http://127.0.0.1:%d" % port


def point_at(monkeypatch, url):
    """Make the bridge talk to url alone: its env override, its panel_url and any address it resolved at import."""
    lm = bridge()
    monkeypatch.setenv(getattr(lm, "BASE_URL_ENV", "KELVIN_LABMASTER_URL"), url)
    monkeypatch.setattr(lm, "panel_url", lambda: url)
    for name, value in list(vars(lm).items()):
        if isinstance(value, str) and value.rstrip("/") == DEFAULT_URL:
            monkeypatch.setattr(lm, name, url)
    return lm


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
    assert {n for n in tools if n.startswith("labmaster_")} == set(LABMASTER_TOOLS)


def test_the_chat_ai_has_them_from_its_first_step():
    """Not among the rarer tools find_tools loads: a chat sees every one of these in its first step."""
    from labharness.web.agent import DEFERRED_NAMES
    assert not set(LABMASTER_TOOLS) & DEFERRED_NAMES


def test_every_tool_answers_when_the_panel_is_not_running(runtime, tmp_path, monkeypatch):
    lm = point_at(monkeypatch, closed_url())
    home_file = tmp_path / "home.json"          # never the person's own ~/Kelvin/home.json
    store = lm.HomeStore
    monkeypatch.setattr(lm, "HomeStore", lambda: store(home_file))

    out = call(runtime,
               ("labmaster_status", {}),
               ("labmaster_functions", {}),
               ("labmaster_saved", {}),
               ("labmaster_home_cards", {}),
               ("labmaster_add_block", {"block": "blk_wait"}),
               ("labmaster_update_block", {"uid": "blk_1", "params": {"minutes": 2}}),
               ("labmaster_delete_block", {"uid": "blk_1"}),
               ("labmaster_control", {"action": "stop"}),
               ("labmaster_import_script", {"path": "scripts/example_measurement.py"}),
               ("labmaster_home_add_card", {"kind": "note", "title": "Test note", "text": "From the test."}),
               ("labmaster_home_remove_card", {"card_id": "card_missing"}))
    assert len(out) == len(LABMASTER_TOOLS)
    for name, text in zip(LABMASTER_TOOLS, out):
        assert isinstance(text, str) and text.strip(), name
        assert "Traceback" not in text, (name, text)

    said = dict(zip(LABMASTER_TOOLS, out))
    for name in ("labmaster_status", "labmaster_functions", "labmaster_saved", "labmaster_add_block",
                 "labmaster_update_block", "labmaster_delete_block", "labmaster_control",
                 "labmaster_import_script"):
        assert "labMaster panel not running at " in said[name], (name, said[name])
        assert START.split()[0] in said[name] and "--port 8770" in said[name], (name, said[name])
    assert "Kelvin home page" in said["labmaster_home_cards"]
    assert "card on the home page: Test note" in said["labmaster_home_add_card"]
    assert said["labmaster_home_remove_card"].startswith("not removed") or "home page" in \
        said["labmaster_home_remove_card"]

    card_id = re.search(r"\[([^\[\]]+)\]", said["labmaster_home_add_card"]).group(1)
    listed, removed = call(runtime, ("labmaster_home_cards", {}),
                           ("labmaster_home_remove_card", {"card_id": card_id}))
    assert "Test note" in listed                                    # the person sees it on the Home page
    assert removed.startswith("removed from the home page: " + card_id), removed


@pytest.mark.parametrize("action", GATED)
def test_gated_control_needs_the_users_yes_first(runtime, action):
    bridge()
    (text,) = call(runtime, ("labmaster_control", {"action": action}))
    assert text.startswith("needs the user's confirmation first: labMaster " + action), text
    assert "confirm=true" in text


def test_confirmed_control_goes_to_the_panel(runtime, monkeypatch):
    url = closed_url()
    point_at(monkeypatch, url)
    (text,) = call(runtime, ("labmaster_control", {"action": "stop"}))
    assert "labMaster panel not running at " + url in text, text


def test_stop_and_safe_stop_need_no_confirmation(runtime, monkeypatch):
    """Only a run needs a person: stopping one never waits for a confirm the AI could give itself."""
    url = closed_url()
    point_at(monkeypatch, url)
    stopped, safe = call(runtime, ("labmaster_control", {"action": "stop"}),
                         ("labmaster_control", {"action": "safe_stop"}))
    for text in (stopped, safe):
        assert "labMaster panel not running at " + url in text, text
        assert "confirmation first" not in text, text


class RecordingApprover(Approver):
    """A person who answers the approval card at once, and remembers what was asked."""

    def __init__(self, answer: bool):
        self.answer = answer
        self.requests: List[Any] = []

    def approve(self, request: Any) -> bool:
        self.requests.append(request)
        request.note = "approved by a person (portal)" if self.answer else "declined by a person (portal)"
        return self.answer


@pytest.fixture
def panel_state(monkeypatch):
    """The panel's own answer for the tests that must not reach a real one: two blocks and nothing running."""
    lm = bridge()
    snapshot = {"sequence": {"name": "R(T) demo", "items": [
        {"uid": "it_1", "name": "example_measurement", "kind": "script",
         "target": "scripts/example_measurement.py", "params": {"reps": 1}},
        {"uid": "it_2", "name": "Safe stop", "kind": "safe_stop", "params": {}}]}}
    monkeypatch.setattr(lm, "state", lambda: {"ok": True, "url": "u", "error": None, "state": snapshot})
    return snapshot


def person_runtime(tmp_path, answer):
    lab = build_virtual_lab(seed=1)
    return LabRuntime(lab, RecordingApprover(answer), tmp_path / "data", demo_envelope(lab))


def test_start_asks_a_person_and_runs_only_after_a_yes(tmp_path, panel_state, monkeypatch):
    """The AI cannot start the panel's sequence: the call blocks on a person's approval card, and the panel is asked
    only after that yes."""
    runtime = person_runtime(tmp_path, True)
    lm = bridge()
    asked: List[Any] = []
    monkeypatch.setattr(lm, "control",
                        lambda action, **kw: (asked.append((action, kw)),
                                              {"ok": True, "error": None, "state": {}, "message": "started"})[1])
    (text,) = call(runtime, ("labmaster_control", {"action": "start"}))
    cards = runtime.approver.requests
    assert len(cards) == 1, "start reached the panel without asking a person"
    card = cards[0]
    assert "labMaster" in card.title and "sequence" in card.title
    assert any("R(T) demo" in line for line in card.items), card.items
    assert any("example_measurement" in line for line in card.items), card.items
    assert any("safety gate" in line for line in card.items), card.items     # says what cannot be checked
    assert any("only a person" in line.lower() for line in card.items), card.items
    assert asked == [("start", {})], asked
    assert "started" in text, text
    assert "approved" in text, text          # the answer says a person's card was what started it


def test_start_declined_by_the_person_starts_nothing(tmp_path, panel_state, monkeypatch):
    runtime = person_runtime(tmp_path, False)
    lm = bridge()
    asked: List[Any] = []
    monkeypatch.setattr(lm, "control",
                        lambda action, **kw: (asked.append(action),
                                              {"ok": True, "error": None, "state": {}, "message": "x"})[1])
    (text,) = call(runtime, ("labmaster_control", {"action": "start"}))
    assert asked == [], "the panel was asked to start although the person declined"
    assert text.startswith("not started: "), text
    assert "declined" in text, text


def test_start_without_a_person_to_ask_starts_nothing(tmp_path, panel_state):
    runtime = person_runtime(tmp_path, True)
    runtime.approver = None          # no approval card on this Kelvin: the AI still cannot start anything
    (text,) = call(runtime, ("labmaster_control", {"action": "start"}))
    assert text.startswith("not started: "), text
    assert "person" in text


def test_control_refuses_what_it_does_not_know(runtime):
    bridge()
    (text,) = call(runtime, ("labmaster_control", {"action": "explode"}))
    assert text.startswith("not run: unknown action"), text


def test_set_wait_needs_its_minutes(runtime):
    bridge()
    (text,) = call(runtime, ("labmaster_control", {"action": "set_wait", "confirm": True}))
    assert text.startswith("not run: set_wait needs minutes > 0"), text


def test_update_refuses_an_empty_change(runtime):
    bridge()
    (text,) = call(runtime, ("labmaster_update_block", {"uid": "blk_1"}))
    assert text.startswith("not updated: pass params"), text


def test_delete_of_a_failed_block_needs_the_users_yes(runtime, monkeypatch):
    """The panel's own MCP rule: removing a FAILED block decides the person's Retry/Skip, so it is their call."""
    lm = point_at(monkeypatch, closed_url())
    snapshot = {"sequence": {"items": [
        {"uid": "it_failed", "name": "Scan", "status": "error", "message": "GPIB timeout"},
        {"uid": "it_pending", "name": "Wait", "status": "pending"}]}}
    monkeypatch.setattr(lm, "state", lambda: {"ok": True, "url": "u", "error": None, "state": snapshot})
    refused, pending = call(runtime, ("labmaster_delete_block", {"uid": "it_failed"}),
                            ("labmaster_delete_block", {"uid": "it_pending"}))
    assert refused.startswith("needs the user's confirmation first: labMaster delete"), refused
    assert "block #1 'Scan' failed" in refused and "GPIB timeout" in refused, refused
    assert pending.startswith("labMaster panel not running at "), pending   # a pending block: no confirm wanted


def test_add_block_needs_a_function(runtime):
    bridge()
    (text,) = call(runtime, ("labmaster_add_block", {"block": "  "}))
    assert text.startswith("not added: say which function"), text


def test_home_card_needs_a_known_kind_and_a_title(runtime, tmp_path, monkeypatch):
    lm = bridge()
    store = lm.HomeStore
    monkeypatch.setattr(lm, "HomeStore", lambda: store(tmp_path / "home.json"))
    bad_kind, no_title, no_items = call(runtime,
                                        ("labmaster_home_add_card", {"kind": "poster", "title": "x"}),
                                        ("labmaster_home_add_card", {"kind": "note", "title": ""}),
                                        ("labmaster_home_add_card", {"kind": "checklist", "title": "Steps"}))
    assert bad_kind.startswith("not added: kind must be one of")
    assert no_title.startswith("not added: a card needs a title")
    assert no_items.startswith("not added: a checklist card needs items")
