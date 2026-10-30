"""The labMaster bridge (labharness/web/labmaster.py) and its portal routes (test_web.py's app, a fake panel).

The panel is a real HTTP server in a thread - standard library only - so the bridge's urllib code, its
JSON and its status-code handling are the ones that run. Nothing here reaches the labMaster on 8770: every
test points KELVIN_LABMASTER_URL at its fake, or at a port nobody listens on.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.web import labmaster  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402

NOW = time.time()


# ---------------------------------------------------------------- the fake labMaster panel

class FakePanel:
    """A labMaster panel's API: the endpoints the bridge uses, as panel_server.py and panel_engine.py answer them."""

    def __init__(self):
        self.rev = "rev-1"
        self.n = 0
        self.palette = [
            {"id": "blk_wait", "name": "Wait", "kind": "wait", "target": "", "params": {"minutes": 1},
             "overrides": {}, "override_base": {}, "note": "", "color": ""},
            {"id": "blk_safe_stop", "name": "Safe stop", "kind": "safe_stop", "target": "", "params": {},
             "overrides": {}, "override_base": {}, "note": "", "color": ""},
        ]
        self.scripts = [
            {"path": "scripts/example_measurement.py", "name": "example_measurement.py", "has_main": True,
             "main": "n_points=21, start=0.0, stop=1.0"},
            {"path": "scripts/sweep.py", "name": "sweep.py", "has_main": True, "main": "volts=1.0"},
        ]
        self.items = []
        self.saved = []
        self.history = []
        self.run_state = "idle"
        self.fault = None
        self.calls = []          # every request the bridge made: {"method", "path", "query", "body"}
        self.fail_once = {}      # path -> (status, payload), answered instead of the real one, once
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                code, payload = panel.handle("GET", self.path, None)
                panel.reply(self, code, payload)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    body = None
                code, payload = panel.handle("POST", self.path, body)
                panel.reply(self, code, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- the wire

    @staticmethod
    def reply(handler, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def handle(self, method: str, path: str, body):
        route, _, query = path.partition("?")
        self.calls.append({"method": method, "path": route, "query": query, "body": body})
        if route in self.fail_once:
            return self.fail_once.pop(route)
        if method == "GET":
            return self.get(route, query)
        return self.post(route, body if isinstance(body, dict) else {})

    def get(self, path: str, query: str):
        if path == "/api/ping":
            return 200, {"ok": True, "sim": True}
        if path == "/api/version":
            return 200, {"version": "0.1.7-fake"}
        if path == "/api/state":
            return 200, self.snapshot()
        if path == "/api/palette":
            return 200, {"palette": list(self.palette), "rev": self.rev}
        if path == "/api/scripts":
            return 200, {"scripts": list(self.scripts)}
        if path == "/api/saved":
            return 200, {"saved": list(self.saved)}
        if path == "/api/history":
            limit = 60
            for part in query.split("&"):
                if part.startswith("limit="):
                    limit = int(part.split("=", 1)[1] or 60)
            return 200, {"version": "h1", "runs": list(self.history)[:limit]}
        return 404, {"error": "no such endpoint: %s" % path}

    def post(self, path: str, body: dict):
        if path == "/api/palette":
            palette = body.get("palette")
            if not isinstance(palette, list) or not all(isinstance(e, dict) for e in palette):
                return 400, {"error": "the function library was not saved: the request did not carry it as a "
                                       "list of blocks"}
            if str(body.get("base_rev")) != str(self.rev):
                return 409, {"error": "the function list was not saved: it changed after this page loaded it "
                                       "(a function was added elsewhere - a Duplicate, another tab)"}
            saved = []
            for entry in palette:
                eid = str(entry.get("id") or "")
                if not eid:
                    self.n += 1
                    eid = "blk_new_%d" % self.n
                saved.append({"id": eid, "name": str(entry.get("name") or "unnamed").strip(),
                              "kind": str(entry.get("kind") or "script"),
                              "target": str(entry.get("target") or "").replace("\\", "/"),
                              "params": entry.get("params") or {}, "overrides": entry.get("overrides") or {},
                              "override_base": entry.get("override_base") or {},
                              "note": entry.get("note") or "", "color": entry.get("color") or ""})
            self.palette = saved
            self.rev = "rev-%d" % (int(self.rev.split("-")[-1]) + 1)
            return 200, {"ok": True, "palette": list(saved), "rev": self.rev}
        if path == "/api/seq/add":
            block_id = body.get("block_id")
            if block_id not in {e["id"] for e in self.palette}:
                return 400, {"error": "missing or unknown key: 'unknown block %r'" % block_id}
            entry = next(e for e in self.palette if e["id"] == block_id)
            item = {"uid": "it_%d" % (len(self.items) + 1), "name": entry["name"], "kind": entry["kind"],
                    "params": body.get("params") or dict(entry.get("params") or {}), "status": "pending"}
            at = body.get("index")
            if isinstance(at, int) and 0 <= at <= len(self.items):
                self.items.insert(at, item)
            else:
                self.items.append(item)
            return 200, {"ok": True, "item": item, "state": self.snapshot()}
        if path == "/api/seq/update":
            item = self._item(body.get("uid"))
            if item is None:
                return 400, {"error": "missing or unknown key: 'unknown block'"}
            patch = body.get("patch") or {}
            if isinstance(patch.get("params"), dict):
                item["params"] = patch["params"]
            if str(patch.get("name") or "").strip():
                item["name"] = str(patch["name"]).strip()
            if patch.get("reset"):
                item["status"] = "pending"
            return 200, {"ok": True, "warnings": [], "state": self.snapshot()}
        if path == "/api/seq/delete":
            if self._item(body.get("uid")) is None:
                return 400, {"error": "missing or unknown key: 'unknown block'"}
            self.items = [i for i in self.items if i["uid"] != body.get("uid")]
            return 200, {"ok": True, "state": self.snapshot()}
        if path == "/api/seq/move":
            item = self._item(body.get("uid"))
            if item is None or not isinstance(body.get("index"), int):
                return 400, {"error": "missing or unknown key"}
            self.items.remove(item)
            self.items.insert(max(0, min(body["index"], len(self.items))), item)
            return 200, {"ok": True, "state": self.snapshot()}
        if path == "/api/seq/duplicate":
            item = self._item(body.get("uid"))
            if item is None:
                return 400, {"error": "missing or unknown key"}
            copy = dict(item, uid="it_%d" % (len(self.items) + 1), name=item["name"] + " (copy)")
            self.items.insert(self.items.index(item) + 1, copy)
            return 200, {"ok": True, "item": copy, "warnings": [], "state": self.snapshot()}
        if path == "/api/seq/clear":
            self.items = [i for i in self.items if body.get("keep_done") and i.get("status") == "done"]
            return 200, {"ok": True, "state": self.snapshot()}
        if path == "/api/seq/rename":
            self.sequence_name = str(body.get("name") or "").strip()
            return 200, {"ok": True, "state": self.snapshot()}
        if path == "/api/control":
            action = str(body.get("action") or "").lower()
            if action not in ("start", "pause", "pause_rep", "next_rep", "skip", "stop", "reset", "end_wait",
                              "set_wait"):
                return 400, {"error": "unknown action %r" % action}
            if action == "start":
                self.run_state = "running"
            elif action in ("stop", "reset"):
                self.run_state = "idle"
            return 200, {"ok": True, "state": self.snapshot()}
        if path == "/api/safe_stop":
            self.run_state = "idle"
            return 200, {"ok": True, "result": {"message": "safe stop done"},
                         "state": self.snapshot()}
        if path == "/api/saved/save":
            name = str(body.get("name") or "").strip()
            old = next((s for s in self.saved if s["name"] == name), None)
            if old and not body.get("overwrite"):
                return 409, {"error": 'a saved sequence called "%s" already exists, so nothing was saved. '
                                      'Choose another name, or save again with overwrite=true to replace it.'
                                      % name}
            entry = {"id": "seq_%d" % (len(self.saved) + 1), "name": name, "saved": NOW,
                     "n_blocks": len(self.items), "items": list(self.items)}
            self.saved = [s for s in self.saved if s["name"] != name] + [entry]
            return 200, {"ok": True, "entry": entry, "state": self.snapshot()}
        if path == "/api/saved/load":
            entry = next((s for s in self.saved if s["id"] == body.get("id")), None)
            if entry is None:
                return 400, {"error": "missing or unknown key: 'unknown saved sequence'"}
            if body.get("append"):
                self.items = self.items + [dict(i) for i in entry["items"]]
            elif not body.get("replace") and self.items:
                return 409, {"error": 'loading "%s" would replace a sequence that has progress, so nothing was '
                                      'loaded. Append it instead (append=true), or load it again with '
                                      'replace=true to discard that.' % entry["name"]}
            else:
                self.items = [dict(i) for i in entry["items"]]
            return 200, {"ok": True, "state": self.snapshot()}
        if path == "/api/saved/delete":
            self.saved = [s for s in self.saved if s["id"] != body.get("id")]
            return 200, {"ok": True, "state": self.snapshot()}
        return 404, {"error": "no such endpoint: %s" % path}

    def _item(self, uid):
        return next((i for i in self.items if i["uid"] == uid), None)

    def snapshot(self) -> dict:
        """ENGINE.snapshot() as far as the bridge reads it (panel_engine.py:6741-6803)."""
        total = len(self.items)
        done = sum(1 for i in self.items if i.get("status") in ("done", "skipped"))
        current = next((i for i in self.items if i.get("status") == "running"), None)
        following = next((i for i in self.items if i.get("status") == "pending"), None)
        return {"sim": True, "run_state": self.run_state, "message": "", "fault": self.fault,
                "sequence": {"name": getattr(self, "sequence_name", "Batch"), "items": list(self.items),
                             "loaded_from": None, "at_stake": {"any": False}},
                "progress": {"index": 0, "done": done, "total": total},
                "current": {"uid": current["uid"], "name": current["name"]} if current else None,
                "next": {"uid": following["uid"], "name": following["name"]} if following else None,
                "wait_seconds_left": None, "pause_after_block": False, "pause_after_rep": False,
                "next_after_rep": None, "safe_stop_running": False, "in_wait": False, "trial": None,
                "saved": [{k: s[k] for k in ("id", "name", "saved", "n_blocks")} for s in self.saved],
                "palette_rev": self.rev,
                "history_runs": len({r.get("run_id") for r in self.history}),
                "history_last": self.history[-1].get("finished") if self.history else None,
                "chat_unread": 0, "panel_version": "0.1.7-fake"}


# ---------------------------------------------------------------- fixtures

def closed_port() -> int:
    """A port nothing listens on (its socket was closed again): connecting there is refused at once."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@pytest.fixture
def fake_panel(monkeypatch):
    panel = FakePanel()
    monkeypatch.setenv(labmaster.BASE_URL_ENV, panel.url)
    yield panel
    panel.close()


@pytest.fixture
def dead_panel(monkeypatch):
    """KELVIN_LABMASTER_URL at a port nobody listens on. The bridge's own 4 s TIMEOUT is checked in
    test_reads_a_live_panel; here it is shortened: a refused connect on this Windows waits ~2 s of SYN
    retries inside the connect before it gives up, and the suite needs no four second stall per call."""
    url = "http://127.0.0.1:%d" % closed_port()
    monkeypatch.setenv(labmaster.BASE_URL_ENV, url)
    monkeypatch.setattr(labmaster, "TIMEOUT", 0.2)
    return url


@pytest.fixture
def portal(tmp_path):
    """Kelvin's portal as test_web.py builds it: no network, no keys, its own home folder (tmp_path)."""
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    store = ChatStore(tmp_path / "chats")
    app = create_app(runtime, settings, store, hosts=["testserver"])
    return TestClient(app), {"X-Kelvin-Token": settings.access_key}


def get(client, path, headers):
    answer = client.get(path, headers=headers)
    assert answer.status_code == 200, (path, answer.status_code, answer.text)
    return answer.json()


def post(client, path, headers, body=None):
    answer = client.post(path, headers=headers, json=body if body is not None else {})
    assert answer.status_code == 200, (path, answer.status_code, answer.text)
    return answer.json()


# ---------------------------------------------------------------- reading the panel (never raises)

def test_reads_a_live_panel(fake_panel):
    assert labmaster.panel_url() == fake_panel.url
    assert labmaster.TIMEOUT <= 4.0
    ping = labmaster.ping()
    assert ping == {"ok": True, "sim": True, "url": fake_panel.url, "version": "0.1.7-fake", "error": None}
    state = labmaster.state()
    assert state["ok"] and state["error"] is None and state["url"] == fake_panel.url
    assert state["state"]["sequence"] == {"name": "Batch", "items": [], "loaded_from": None,
                                          "at_stake": {"any": False}}
    library = labmaster.palette()
    assert library["ok"] and library["rev"] == "rev-1"
    assert [e["id"] for e in library["palette"]] == ["blk_wait", "blk_safe_stop"]
    found = labmaster.scripts()
    assert found["ok"] and found["scripts"][0]["path"] == "scripts/example_measurement.py"
    assert labmaster.saved() == {"ok": True, "error": None, "saved": []}
    assert labmaster.history(5) == {"ok": True, "error": None, "runs": []}


def test_a_down_panel_answers_in_words_never_raises(dead_panel, tmp_path):
    ping = labmaster.ping()
    assert ping["ok"] is False and ping["url"] == dead_panel and ping["error"]
    assert ping["sim"] is None and ping["version"] is None
    for answer in (labmaster.state(), labmaster.palette(), labmaster.scripts(), labmaster.saved(),
                   labmaster.history(3), labmaster.seq_add(block_id="blk_wait"), labmaster.control("stop")):
        assert answer["ok"] is False, answer
        assert answer["error"], answer
    assert labmaster.state()["state"] is None
    assert labmaster.palette()["palette"] == [] and labmaster.palette()["rev"] == ""
    assert labmaster.scripts()["scripts"] == []
    assert labmaster.import_script("scripts/example_measurement.py")["ok"] is False
    assert labmaster.safe_stop("a test")["ok"] is False
    assert labmaster.saved_save("a test")["ok"] is False
    # /api/home with the panel down makes ONE panel call (ping), so the page never waits on the 4 s timeout of
    # every tile: it answers within one panel timeout and says why nothing is live.
    started = time.monotonic()
    snapshot = labmaster.snapshot(labmaster.HomeStore(tmp_path / "home.json"))
    assert time.monotonic() - started < 2.0
    assert snapshot["ok"] is True and snapshot["panel"]["ok"] is False
    assert [t["id"] for t in snapshot["tiles"]] == ["measurement", "history", "saved", "functions", "panel"]
    assert all(t["tone"] == "down" for t in snapshot["tiles"])
    assert snapshot["cards"] == [] and snapshot["greeting"]


# ---------------------------------------------------------------- HomeStore (Kelvin's own file)

def test_home_store_add_update_remove(tmp_path):
    path = tmp_path / "Kelvin" / "home.json"
    store = labmaster.HomeStore(path)
    assert store.cards() == []

    card = store.add({"kind": "metric", "title": "Temperature", "text": "21.4 C", "source": "person"})
    assert card["id"].startswith("card_") and card["kind"] == "metric" and card["title"] == "Temperature"
    assert card["source"] == "person" and card["items"] == [] and card["created"]
    assert path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["cards"][0]["id"] == card["id"]
    assert not list(path.parent.glob("*.tmp")), "the write must land whole: no temporary file left"

    # another reader of the same file sees it (the app and the MCP tools use one file)
    assert [c["id"] for c in labmaster.HomeStore(path).cards()] == [card["id"]]

    changed = store.update(card["id"], title="Temperature (lab)", text="21.6 C", items=["a", "b"])
    assert changed["id"] == card["id"] and changed["title"] == "Temperature (lab)"
    assert changed["items"] == ["a", "b"] and changed["created"] == card["created"]
    assert changed["kind"] == "metric" and changed["source"] == "person"

    # a card nobody sent still gets the fields the page draws; a kind it does not know is a note
    extra = store.add({"title": ""})
    assert extra["kind"] == "note" and extra["source"] == "ai" and extra["id"]
    # a model's runaway answer is cut to a card a page can draw, not a megabyte in home.json
    big = store.add({"title": "x" * 500, "text": "y" * 30000, "items": ["i"] * 500})
    assert len(big["title"]) == 200 and len(big["text"]) == 20000 and len(big["items"]) == 100
    with pytest.raises(KeyError):
        store.update("card_missing", title="x")
    with pytest.raises(KeyError):
        store.remove("card_missing")

    assert store.remove(card["id"]) == {"ok": True, "id": card["id"]}
    assert [c["id"] for c in store.cards()] == [extra["id"], big["id"]]
    # a file that cannot be read is an empty page, not a crash
    path.write_text("{not json", encoding="utf-8")
    assert store.cards() == []


def test_home_store_snapshot_cards(fake_panel, tmp_path):
    store = labmaster.HomeStore(tmp_path / "home.json")
    store.add({"kind": "note", "title": "Calibrate the VNA", "text": "before Friday"})
    snapshot = labmaster.snapshot(store)
    assert snapshot["ok"] is True and snapshot["panel"]["ok"] is True
    assert [c["title"] for c in snapshot["cards"]] == ["Calibrate the VNA"]
    assert snapshot["updated_at"]


# ---------------------------------------------------------------- snapshot()'s five tiles

def test_snapshot_tiles_from_the_panels_state(fake_panel, tmp_path):
    fake_panel.items = [{"uid": "it_1", "name": "Ramp", "status": "done", "params": {}},
                        {"uid": "it_2", "name": "Sweep", "status": "running", "params": {}},
                        {"uid": "it_3", "name": "Wait", "status": "pending", "params": {}}]
    fake_panel.run_state = "running"
    fake_panel.history = [{"run_id": "r1", "name": "Batch", "finished": NOW},
                          {"run_id": "r2", "name": "Batch", "finished": NOW}]
    fake_panel.saved = [{"id": "seq_1", "name": "Morning", "saved": NOW, "n_blocks": 2, "items": []}]
    fake_panel.sequence_name = "Evening batch"
    store = labmaster.HomeStore(tmp_path / "home.json")
    store.add({"kind": "checklist", "title": "Tonight", "items": ["switch on", "sweep"]})

    snapshot = labmaster.snapshot(store)
    assert snapshot["ok"] is True and snapshot["greeting"]
    tiles = {t["id"]: t for t in snapshot["tiles"]}
    assert set(tiles) == {"measurement", "history", "saved", "functions", "panel"}

    assert tiles["measurement"]["value"] == "1/3"
    assert tiles["measurement"]["tone"] == "busy"
    assert "33% done" in tiles["measurement"]["detail"]
    assert "now: Sweep" in tiles["measurement"]["detail"]
    assert "running" in tiles["measurement"]["detail"]

    assert tiles["history"]["value"] == "2"
    assert "2 runs" in tiles["history"]["detail"] and "last finished" in tiles["history"]["detail"]

    assert tiles["saved"]["value"] == "1" and "Morning" in tiles["saved"]["detail"]

    # the function library (the palette: what a sequence is built from) - not the .py files on disk
    assert tiles["functions"]["value"] == "2" and "Wait" in tiles["functions"]["detail"]
    assert tiles["functions"]["tone"] == "idle"

    assert tiles["panel"]["value"] == "SIM" and tiles["panel"]["tone"] == "ok"
    assert "0.1.7-fake" in tiles["panel"]["detail"] and fake_panel.url in tiles["panel"]["detail"]

    assert [c["title"] for c in snapshot["cards"]] == ["Tonight"]


def test_snapshot_marks_a_fault_and_an_empty_sequence(fake_panel, tmp_path):
    fake_panel.fault = "safe stop did not finish"
    snapshot = labmaster.snapshot(labmaster.HomeStore(tmp_path / "home.json"))
    tiles = {t["id"]: t for t in snapshot["tiles"]}
    assert tiles["measurement"]["value"] == "0/0" and tiles["measurement"]["tone"] == "warn"
    assert "fault" in tiles["measurement"]["detail"]

    fake_panel.fault = None
    tiles = {t["id"]: t for t in labmaster.snapshot()["tiles"]}
    assert tiles["measurement"]["tone"] == "idle"
    assert "no blocks" in tiles["measurement"]["detail"]


# ---------------------------------------------------------------- import_script: the whole library is written back

def test_import_script_writes_the_whole_function_library_back(fake_panel):
    before_ids = [e["id"] for e in fake_panel.palette]
    before_rev = fake_panel.rev

    result = labmaster.import_script("scripts/example_measurement.py", name="My sweep")
    assert result["ok"] is True and result["error"] is None
    entry = result["entry"]
    assert entry["name"] == "My sweep" and entry["kind"] == "script"
    assert entry["target"] == "scripts/example_measurement.py"
    assert entry["params"] == {"reps": 1}                    # "+ New function"'s starting values
    assert entry["id"] and entry["id"] not in before_ids      # the panel gave it its id

    # the whole library went back: what was there is untouched, the new function first (the page unshifts it)
    assert [e["id"] for e in fake_panel.palette] == [entry["id"]] + before_ids
    write = [c for c in fake_panel.calls if c["method"] == "POST" and c["path"] == "/api/palette"][-1]
    assert write["body"]["base_rev"] == before_rev
    assert [e["id"] for e in write["body"]["palette"]] == [""] + before_ids
    assert len(write["body"]["palette"]) == len(before_ids) + 1
    assert fake_panel.rev != before_rev
    # the panel's answer is the library as saved: the entry the page shows is the one the panel gave
    assert [e["id"] for e in result["palette"]] == [entry["id"]] + before_ids
    assert labmaster.palette()["palette"][0]["id"] == entry["id"]


def test_import_script_takes_a_file_name_and_refuses_what_it_cannot_know(fake_panel):
    by_name = labmaster.import_script("sweep.py")
    assert by_name["ok"] is True
    assert by_name["entry"]["name"] == "sweep"
    assert by_name["entry"]["target"] == "scripts/sweep.py"
    assert "already had 0 function" not in by_name["message"]

    missing = labmaster.import_script("scripts/not_there.py")
    assert missing["ok"] is False and "not a .py file" in missing["error"]
    assert missing["entry"] is None
    assert labmaster.import_script("")["ok"] is False

    # a revision that changed under us: the panel (409) refuses, and the library is not saved over
    fake_panel.fail_once["/api/palette"] = (409, {"error": "the function list was not saved: it changed after this "
                                                          "page loaded it"})
    stale = labmaster.import_script("scripts/example_measurement.py")
    assert stale["ok"] is False and "changed after this page loaded it" in stale["error"]
    assert stale["entry"] is None
    assert all(e["name"] != "example_measurement" for e in fake_panel.palette)


# ---------------------------------------------------------------- the sequence writes

def test_sequence_writes_and_control(fake_panel):
    added = labmaster.seq_add(block_id="blk_wait", params={"minutes": 2})
    assert added["ok"] is True and added["error"] is None
    items = added["state"]["sequence"]["items"]
    assert len(items) == 1 and items[0]["name"] == "Wait" and items[0]["params"] == {"minutes": 2}

    uid = items[0]["uid"]
    updated = labmaster.seq_update(uid, params={"minutes": 5}, name="Longer wait")
    assert updated["ok"] is True
    assert updated["state"]["sequence"]["items"][0]["name"] == "Longer wait"
    assert updated["state"]["sequence"]["items"][0]["params"] == {"minutes": 5}

    copied = labmaster.seq_duplicate(uid)
    assert copied["ok"] is True and len(copied["state"]["sequence"]["items"]) == 2
    moved = labmaster.seq_move(uid, 1)
    assert moved["ok"] is True and moved["state"]["sequence"]["items"][1]["uid"] == uid
    deleted = labmaster.seq_delete(uid)
    assert deleted["ok"] is True and len(deleted["state"]["sequence"]["items"]) == 1
    renamed = labmaster.seq_rename("Evening batch")
    assert renamed["ok"] is True and renamed["state"]["sequence"]["name"] == "Evening batch"
    cleared = labmaster.seq_clear()
    assert cleared["ok"] is True and cleared["state"]["sequence"]["items"] == []
    # by name (the panel adds by id; the bridge looks a name up in the library first)
    by_name = labmaster.seq_add(block="Safe stop")
    assert by_name["ok"] is True and by_name["state"]["sequence"]["items"][0]["name"] == "Safe stop"

    # refusals that never reach the panel
    assert labmaster.seq_add()["ok"] is False
    assert labmaster.seq_add(block="no such function")["ok"] is False
    assert "no function" in labmaster.seq_add(block="no such function")["error"]
    assert labmaster.seq_update(uid)["ok"] is False
    assert labmaster.seq_update("")["ok"] is False
    assert labmaster.seq_move(uid, "soon")["ok"] is False
    assert labmaster.seq_delete("")["ok"] is False
    assert labmaster.seq_rename("")["ok"] is False
    # the panel's own refusal comes back in words
    refused = labmaster.seq_delete("it_unknown")
    assert refused["ok"] is False and refused["error"]

    started = labmaster.control("start")
    assert started["ok"] is True and started["state"]["run_state"] == "running"
    assert labmaster.control("stop")["ok"] is True
    assert labmaster.control("teleport")["ok"] is False
    safe = labmaster.control("safe_stop", reason="a test")
    assert safe["ok"] is True and safe["message"] == "safe stop done"
    assert any(c["path"] == "/api/safe_stop" for c in fake_panel.calls)


# ---------------------------------------------------------------- the saved sequences

def test_saved_sequences(fake_panel):
    fake_panel.items = [{"uid": "it_1", "name": "Wait", "status": "pending", "params": {}}]
    saved = labmaster.saved_save("Tonight", overwrite=False)
    assert saved["ok"] is True and "Tonight" in saved["message"]
    assert saved["state"]["saved"][0]["name"] == "Tonight"

    again = labmaster.saved_save("Tonight")
    assert again["ok"] is False and "already exists" in again["error"]
    replaced = labmaster.saved_save("Tonight", overwrite=True)
    assert replaced["ok"] is True

    listed = labmaster.saved()
    assert listed["ok"] is True and [s["name"] for s in listed["saved"]] == ["Tonight"]
    assert set(listed["saved"][0]) == {"id", "name", "saved", "n_blocks"}
    saved_id = listed["saved"][0]["id"]

    # the live sequence already has progress: a plain load is refused, append or replace go through
    fake_panel.items[0]["status"] = "done"
    refused = labmaster.saved_load(item_id=saved_id)
    assert refused["ok"] is False and "would replace" in refused["error"]
    appended = labmaster.saved_load(item_id=saved_id, append=True)
    assert appended["ok"] is True and len(appended["state"]["sequence"]["items"]) == 2
    replaced_load = labmaster.saved_load(item_id=saved_id, replace=True)
    assert replaced_load["ok"] is True and len(replaced_load["state"]["sequence"]["items"]) == 1
    # by name, when no other saved sequence has that name
    by_name = labmaster.saved_load(name="Tonight", replace=True)
    assert by_name["ok"] is True
    assert labmaster.saved_load(name="No such name")["ok"] is False
    assert labmaster.saved_load(item_id=saved_id, append=True, replace=True)["ok"] is False

    removed = labmaster.saved_delete(item_id=saved_id)
    assert removed["ok"] is True and labmaster.saved()["saved"] == []
    assert labmaster.saved_delete(name="No such name")["ok"] is False   # never sent: no such name to delete


# ---------------------------------------------------------------- the portal routes

def test_home_route_gives_the_page_with_the_panel_live(portal, fake_panel):
    client, headers = portal
    page = get(client, "/api/home", headers)
    assert page["ok"] is True and page["panel"]["ok"] is True
    assert [t["id"] for t in page["tiles"]] == ["measurement", "history", "saved", "functions", "panel"]
    assert page["cards"] == [] and page["greeting"]
    assert page["updated_at"]


def test_home_route_still_gives_the_page_with_the_panel_down(portal, dead_panel):
    client, headers = portal
    page = get(client, "/api/home", headers)
    assert page["ok"] is True and page["panel"]["ok"] is False
    assert all(t["tone"] == "down" for t in page["tiles"])
    state = get(client, "/api/labmaster/state", headers)
    assert state["ok"] is False and state["state"] is None and state["url"] == dead_panel
    assert post(client, "/api/labmaster/seq", headers, {"action": "add", "block_id": "blk_wait"})["ok"] is False
    assert post(client, "/api/labmaster/control", headers, {"action": "start"})["ok"] is False
    assert post(client, "/api/home/cards", headers, {"title": "still works"})["ok"] is True


def test_home_cards_routes(portal, fake_panel, tmp_path):
    client, headers = portal
    made = post(client, "/api/home/cards", headers,
                {"kind": "note", "title": "Calibrate", "text": "the VNA before Friday", "source": "person"})
    assert made["ok"] is True and made["card"]["id"].startswith("card_")
    card_id = made["card"]["id"]
    assert json.loads((tmp_path / "home.json").read_text(encoding="utf-8"))["cards"][0]["id"] == card_id
    assert [c["id"] for c in get(client, "/api/home", headers)["cards"]] == [card_id]

    changed = post(client, "/api/home/cards/%s" % card_id, headers, {"title": "Calibrated", "text": "done"})
    assert changed["ok"] is True and changed["card"]["title"] == "Calibrated"
    assert changed["card"]["id"] == card_id

    unknown = post(client, "/api/home/cards/card_missing", headers, {"title": "x"})
    assert unknown["ok"] is False and "not found" in unknown["error"]
    deleted = client.delete("/api/home/cards/%s" % card_id, headers=headers)
    assert deleted.status_code == 200 and deleted.json() == {"ok": True}
    assert get(client, "/api/home", headers)["cards"] == []
    again = client.delete("/api/home/cards/%s" % card_id, headers=headers)
    assert again.status_code == 200 and again.json()["ok"] is False


def test_labmaster_read_routes(portal, fake_panel):
    client, headers = portal
    fake_panel.items = [{"uid": "it_1", "name": "Wait", "status": "pending", "params": {}}]
    fake_panel.history = [{"run_id": "r1", "name": "Batch", "started": NOW, "finished": NOW, "sim": True,
                           "n_blocks": 2, "n_error": 1, "n_datasets": 2, "blocks": [{"huge": "x" * 5000}]}]
    state = get(client, "/api/labmaster/state", headers)
    assert state["ok"] is True and state["state"]["sequence"]["items"][0]["name"] == "Wait"
    assert get(client, "/api/labmaster/palette", headers)["rev"] == fake_panel.rev
    assert get(client, "/api/labmaster/scripts", headers)["scripts"][0]["name"] == "example_measurement.py"
    assert get(client, "/api/labmaster/saved", headers)["saved"] == []
    runs = get(client, "/api/labmaster/history?limit=1", headers)
    assert runs["ok"] is True and runs["runs"][0]["run_id"] == "r1"
    assert "blocks" not in runs["runs"][0]          # normalized: counts, not every history row


def test_labmaster_sequence_routes(portal, fake_panel):
    client, headers = portal
    added = post(client, "/api/labmaster/seq", headers,
                 {"action": "add", "block_id": "blk_wait", "params": {"minutes": 3}})
    assert added["ok"] is True and added["state"]["sequence"]["items"][0]["params"] == {"minutes": 3}
    uid = added["state"]["sequence"]["items"][0]["uid"]
    by_name = post(client, "/api/labmaster/seq", headers, {"action": "add", "block": "Safe stop"})
    assert by_name["ok"] is True and len(by_name["state"]["sequence"]["items"]) == 2

    for body in ({"action": "update", "uid": uid, "params": {"minutes": 4}, "name": "Longer"},
                 {"action": "duplicate", "uid": uid},
                 {"action": "move", "uid": uid, "index": 1},
                 {"action": "delete", "uid": uid},
                 {"action": "rename", "name": "Through the portal"},
                 {"action": "clear"}):
        answer = post(client, "/api/labmaster/seq", headers, body)
        assert answer["ok"] is True, (body, answer)
    assert post(client, "/api/labmaster/seq", headers, {"action": "warp"})["ok"] is False
    assert post(client, "/api/labmaster/seq", headers, {"action": "add", "block": "nope"})["ok"] is False


def test_labmaster_control_route(portal, fake_panel):
    client, headers = portal
    started = post(client, "/api/labmaster/control", headers, {"action": "start"})
    assert started["ok"] is True and started["state"]["run_state"] == "running"
    assert post(client, "/api/labmaster/control", headers, {"action": "stop", "reason": "test"})["ok"] is True
    safe = post(client, "/api/labmaster/control", headers, {"action": "safe_stop", "reason": "test"})
    assert safe["ok"] is True
    assert post(client, "/api/labmaster/control", headers, {"action": "teleport"})["ok"] is False
    assert post(client, "/api/labmaster/control", headers, {})["ok"] is False


def test_labmaster_saved_routes(portal, fake_panel):
    client, headers = portal
    fake_panel.items = [{"uid": "it_1", "name": "Wait", "status": "pending", "params": {}}]
    saved = post(client, "/api/labmaster/saved", headers, {"action": "save", "name": "Morning"})
    assert saved["ok"] is True and saved["state"]["saved"][0]["name"] == "Morning"
    saved_id = saved["state"]["saved"][0]["id"]

    refused = post(client, "/api/labmaster/saved", headers, {"action": "save", "name": "Morning"})
    assert refused["ok"] is False and "already exists" in refused["error"]
    replaced = post(client, "/api/labmaster/saved", headers,
                    {"action": "save", "name": "Morning", "overwrite": True})
    assert replaced["ok"] is True
    saved_id = replaced["state"]["saved"][0]["id"]      # replacing a saved sequence gives it a new id

    loaded = post(client, "/api/labmaster/saved", headers, {"action": "load", "id": saved_id, "replace": True})
    assert loaded["ok"] is True and loaded["state"]["sequence"]["items"][0]["name"] == "Wait"
    by_name = post(client, "/api/labmaster/saved", headers, {"action": "load", "name": "Morning", "replace": True})
    assert by_name["ok"] is True
    missing = post(client, "/api/labmaster/saved", headers, {"action": "load", "id": "seq_nope"})
    assert missing["ok"] is False and missing["error"]

    deleted = post(client, "/api/labmaster/saved", headers, {"action": "delete", "id": saved_id})
    assert deleted["ok"] is True
    assert get(client, "/api/labmaster/saved", headers)["saved"] == []
    unknown = post(client, "/api/labmaster/saved", headers, {"action": "warp"})
    assert unknown["ok"] is False and "unknown saved action" in unknown["error"]


def test_labmaster_import_route(portal, fake_panel):
    client, headers = portal
    imported = post(client, "/api/labmaster/import", headers,
                    {"path": "scripts/example_measurement.py", "name": "Through the portal"})
    assert imported["ok"] is True
    assert imported["entry"]["name"] == "Through the portal"
    assert fake_panel.palette[0]["id"] == imported["entry"]["id"]
    assert post(client, "/api/labmaster/import", headers, {"path": "scripts/nope.py"})["ok"] is False


def test_the_old_routes_still_answer(portal, fake_panel):
    """New routes are appended; an existing one (the portal's own state) still answers as it did."""
    client, headers = portal
    old = get(client, "/api/state", headers)
    assert "queue" in old and "instruments" in old and "lab" in old
    assert client.get("/static/app.js").status_code == 200
