"""Keeping work apart (VISION point 2), from the overnight test of 2026-10-30: Kelvin's AI never reads Kelvin's own
folder or a file that holds sign-ins; a confidential project's runs are seen only from inside it, by the models its
rules allow; reports and records leave it out elsewhere; phones through the Kelvin hub get lab-wide records without
its names and data; and a run is filed with the project and rules of the chat that started it."""
import asyncio
import json

import pytest

from labharness import ScriptedApprover, folderfiles
from labharness.agent.runtime import LabRuntime, Viewer
from simlab import build_virtual_lab

pytest.importorskip("mcp")
pytest.importorskip("h5py")

from labharness.agent.server import build_server  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted  # noqa: E402

NDA = ("plan: acme_gate\nsetup: {lockin.amplitude: 0.05, smu.output: on}\nsweep: {smu.V: {from: 0, to: 1, points: 6}}\n"
       "settle: 0.1\nmeasure: [lockin.X, smu.I]\nafter: {smu.V: 0}")
OPEN = NDA.replace("acme_gate", "thesis_gate")


def newest(rt, plan):
    """The latest run of this plan (runs started in the same second sort by their names)."""
    from labharness import rundata
    run = [r for r in rt.pick_runs(day="today") if r.endswith("-" + plan)][-1]
    return run, rundata.meta(rt.runs.find(run))


@pytest.fixture
def rt(tmp_path):
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 50), tmp_path / "data")


class Portal:
    """A ChatAgent on a runtime, with chats driven turn by turn by a scripted model (no hub, no network)."""

    def __init__(self, tmp_path, rt):
        self.rt = rt
        self.store = ChatStore(tmp_path / "chats")
        self.agent = ChatAgent(rt, self.store, build_server(rt), {"scripted": Scripted([])})
        rt.project_info = self.agent.project_info
        rt.project_ids = lambda: [None] + [p["id"] for p in self.store.projects()]
        nda = self.store.create_project("Acme NDA")
        self.store.set_rules(nda["id"], {"providers": ["scripted"], "literature": False})
        self.store.set_sharing(nda["id"], "confidential")
        self.nda = nda["id"]
        self.secret = self.store.create("scripted:m1", nda["id"])["id"]   # the NDA project's chat
        self.other = self.store.create("scripted:m1")["id"]               # an ordinary chat

    def turn(self, chat_id, calls, text="ok"):
        """One turn whose model makes these tool calls ([(name, input)]), then answers; the tools' results."""
        provider = Scripted([[{"type": "tool_call", "id": "c%d" % i, "name": n, "input": a}
                              for i, (n, a) in enumerate(calls)], [{"type": "text", "text": text}]])
        self.agent.providers["scripted"] = provider
        events = []

        async def emit(e):
            events.append(e)

        async def go():
            await self.agent.start()
            try:
                self.agent.claim(chat_id, "scripted:m1")
                await self.agent.run_turn(chat_id, "go", "scripted:m1", provider, emit)
            finally:
                await self.agent.close()
        asyncio.run(go())
        return {e["id"]: e["content"] for e in events if e["type"] == "tool_end"}

    def in_turn(self, fn):
        """fn() inside a running agent (its MCP client up), e.g. a second opinion's lookups."""
        async def go():
            await self.agent.start()
            try:
                return await fn()
            finally:
                await self.agent.close()
        return asyncio.run(go())


# ------------------------------------------------------------------ Kelvin's own folder and sign-ins (t10)

def test_read_examples_never_reads_kelvins_own_folder_or_a_file_with_sign_ins(rt, tmp_path):
    home = tmp_path / "Kelvin"
    (home / "users" / "ada" / "chats").mkdir(parents=True)
    (home / "settings.json").write_text('{"access_key": "portal-key-123"}', encoding="utf-8")
    (home / "lab.yaml").write_text("name: Bench\ninstruments: {}\n", encoding="utf-8")
    (home / "users" / "ada" / "chats" / "notes.md").write_text("Ada's chat", encoding="utf-8")
    rt.home = home
    maker = tmp_path / "Maker SDK"
    (maker / "examples").mkdir(parents=True)
    (maker / "examples" / "demo.py").write_text("sdk.connect('COM3')", encoding="utf-8")
    (maker / "README.txt").write_text("Call connect first.", encoding="utf-8")
    (maker / ".env").write_text("OPENAI_API_KEY=sk-env-456", encoding="utf-8")
    (maker / "credentials.json").write_text('{"password": "pw-789"}', encoding="utf-8")
    (maker / ".aws").mkdir()
    (maker / ".aws" / "config.txt").write_text("aws_secret_access_key=aws-000", encoding="utf-8")

    for named in (home / "settings.json", home / "lab.yaml", home, home / "users"):
        out = rt.read_examples(str(named))
        assert out.startswith("not read") and "Kelvin's own folder" in out, named
        assert "portal-key-123" not in out and "Bench" not in out
    for named in (maker / ".env", maker / "credentials.json", maker / ".aws" / "config.txt"):
        out = rt.read_examples(str(named))
        assert out.startswith("not read") and "sign-ins, keys or tokens" in out, named
    # a folder that holds Kelvin's own folder, or files with sign-ins, is read without them
    text = rt.read_examples(str(tmp_path))
    assert "sdk.connect('COM3')" in text and "Call connect first." in text
    for kept in ("portal-key-123", "Bench", "Ada's chat", "sk-env-456", "pw-789", "aws-000"):
        assert kept not in text, kept
    # the lab's scripts are read the same way
    (home / "env").mkdir()
    (home / "env" / "probe.py").write_text("import pyvisa\nrm = pyvisa.ResourceManager()\n", encoding="utf-8")
    assert "Kelvin's own folder" in rt.survey_scripts(str(home / "env"))
    assert "probe.py" not in rt.survey_scripts(str(tmp_path))


def test_a_chats_file_tools_never_read_or_save_a_file_with_sign_ins(tmp_path):
    folder = tmp_path / "Sample A"
    (folder / ".ssh").mkdir(parents=True)
    (folder / ".env").write_text("TOKEN=abc", encoding="utf-8")
    (folder / "secret project").mkdir()   # a folder of the person's own may be named so
    (folder / "secret project" / "iv.csv").write_text("V,I\n0,0\n", encoding="utf-8")
    for path in (".env", ".ssh/id_ed25519", "api_token.txt"):
        with pytest.raises(folderfiles.FolderError, match="sign-ins, keys or tokens"):
            folderfiles.inside(folder, path)
    with pytest.raises(folderfiles.FolderError, match="sign-ins, keys or tokens"):
        folderfiles.save(folder, ".ssh/authorized_keys", "ssh-ed25519 AAAA")
    text, _ = folderfiles.read(folder, "secret project/iv.csv")
    assert "0,0" in text


# ------------------------------------------------------------------ a confidential project's runs (t06, t09)

def test_only_the_projects_own_chats_see_a_confidential_runs_name_and_data(rt, tmp_path):
    p = Portal(tmp_path, rt)
    ran = p.turn(p.secret, [("run_plan", {"plan": NDA})])
    run, meta = newest(rt, "acme_gate")   # stamped with the chat that ran it and the rules it ran under
    assert "acme_gate" in ran["c0"]
    assert (meta["project"], meta["chat"], meta["confidential"]) == (p.nda, p.secret, True)
    assert meta["rules"]["providers"] == ["scripted"]

    looked = p.turn(p.other, [("list_runs", {}), ("find_runs", {"text": "acme"}), ("describe_run", {"run_id": run}),
                              ("get_run_data", {"run_id": run}), ("run_provenance", {"run_id": run}),
                              ("activity", {}), ("recent_events", {"n": 50}),
                              ("query_runs", {"sql": "SELECT run_id, plan, project_name FROM runs"}),
                              ("compute", {"run_id": run, "expression": "mean(smu.I)"}),
                              ("make_report", {"day": "today"}), ("export_eln", {"day": "today"})])
    for call, text in looked.items():   # named by its id, it is said to be unshared work, and no more
        assert "Acme NDA" not in text and (call in ("c2", "c3", "c4", "c8") or "acme" not in text.lower()), call
        assert call not in ("c2", "c3", "c4", "c8") or "unshared work" in text and "smu" not in text, (call, text)
    assert "not shown here" in looked["c0"] and "not shown here" in looked["c1"]
    assert "not shown here" in looked["c5"] and "not shown here" in looked["c7"]
    assert "no report" in looked["c9"] and "not shown here" in looked["c9"]

    inside = p.turn(p.secret, [("list_runs", {}), ("describe_run", {"run_id": run}), ("activity", {})])
    assert run in inside["c0"] and "acme_gate" in inside["c1"] and "acme_gate" in inside["c2"]

    # a second opinion or a Compare lane with a model the project's rules don't allow sees none of it
    chat = p.store.load(p.secret)

    async def lookups():
        allowed = await p.agent._tool("list_runs", {}, p.agent.viewer_for(chat))
        barred = await p.agent._tool("list_runs", {}, p.agent.viewer_for(chat, "openai:gpt-5"))
        hub = await p.agent._tool("describe_run", {"run_id": run}, p.agent.viewer_for(chat, "hub-anthropic:opus"))
        mcp = await p.agent._tool("list_runs", {}, None)   # an agent over MCP: no chat, no project
        return [r.content[0].text for r in (allowed, barred, hub, mcp)]
    allowed, barred, hub, mcp = p.in_turn(lookups)
    assert run in allowed and run not in barred and run not in mcp and "unshared work" in hub


def test_runs_are_filed_with_their_chat_never_under_another_projects_confidential_sample(rt, tmp_path):
    p = Portal(tmp_path, rt)
    device = p.store.add_device(p.nda, "Acme-HfO2-D1")
    rt.mount(p.nda, device)
    told = p.turn(p.other, [("run_plan", {"plan": OPEN})])["c0"]
    _, meta = newest(rt, "thesis_gate")
    assert meta["project"] is None and meta["device"] is None and meta["chat"] == p.other
    assert "filed under General without the mounted sample" in told and "Acme" not in told
    incognito = p.store.create("scripted:m1", incognito=True)["id"]
    p.turn(incognito, [("run_plan", {"plan": OPEN.replace("thesis_gate", "quick_check")})])
    _, meta = newest(rt, "quick_check")
    assert meta["incognito"] is True and meta["project"] is None and meta["device"] is None
    p.turn(p.secret, [("run_plan", {"plan": NDA})])   # the project's own chat: under its sample, as before
    _, meta = newest(rt, "acme_gate")
    assert (meta["project"], meta["device_name"]) == (p.nda, "Acme-HfO2-D1")
    # and the mounted sample's name, with its protection, is that work's own
    rt.protect(dict(device, protection={"limits": {"smu.V": [-2, 2]}}))
    status = p.turn(p.other, [("lab_status", {})])["c0"]
    assert "Acme" not in status


# ------------------------------------------------------------------ the portal's reports and records (t06)

def test_the_portals_day_records_reports_and_analysis_list_follow_the_chat_they_are_made_from(rt, tmp_path):
    import io
    import zipfile
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    p = Portal(tmp_path, rt)
    p.turn(p.secret, [("run_plan", {"plan": NDA})])
    p.turn(p.other, [("run_plan", {"plan": OPEN})])
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, p.store, hosts=["testserver"], providers={"scripted": Scripted([])}))
    h = {"X-Kelvin-Token": settings.access_key}

    def plans(query):
        return {r["plan"]: r.get("unshared", False) for r in client.get("/api/runs?" + query, headers=h).json()["runs"]}
    assert plans("limit=20") == {"acme_gate": True, "thesis_gate": False}   # the person's own list, marked
    assert plans("limit=20&chat=" + p.other) == {"thesis_gate": False}     # what the Analysis pane hands that chat
    assert plans("limit=20&chat=" + p.secret) == {"acme_gate": True, "thesis_gate": False}
    assert plans("limit=20&chat=") == {"thesis_gate": False}

    r = client.get("/api/eln?day=today&chat=" + p.other, headers=h)
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        text = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith(".md"))
        assert all("acme" not in n for n in z.namelist())
    assert "acme" not in text and "thesis_gate" in text and "1 run of unshared work" in text
    r = client.get("/api/eln?day=today&chat=" + p.secret, headers=h)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert any("acme_gate" in n for n in z.namelist())

    made = client.post("/api/reports", headers=h, json={"scope": "today", "chat_id": p.other}).json()
    assert (made["runs"], made["left"]) == (1, 1)
    made = client.post("/api/reports", headers=h, json={"scope": "today", "chat_id": p.secret}).json()
    assert (made["runs"], made["left"]) == (2, 0)


def test_writing_a_confidential_projects_summary_never_goes_through_the_hub(rt, tmp_path):
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    store = ChatStore(tmp_path / "chats")
    p = store.create_project("Hub-only confidential")
    store.set_sharing(p["id"], "confidential")   # its model rules left open
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, store, hosts=["testserver"], providers={"scripted": Scripted([])}))
    r = client.post("/api/projects/%s/summary" % p["id"], headers={"X-Kelvin-Token": settings.access_key},
                    json={"model": "hub-anthropic:claude-opus-5-5"})
    assert r.status_code == 400 and "Kelvin hub" in r.json()["error"]
    assert store.summary_of(p["id"]) == ""


# ------------------------------------------------------------------ phones through the Kelvin hub (t09)

def test_phones_through_the_hub_get_the_labs_state_without_unshared_works_names(tmp_path, monkeypatch):
    import threading
    import time
    from labharness.agent.approval import WebApprover
    from labharness.gate import ApprovalRequest
    from test_team_and_analysis import _relay
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=30), tmp_path / "data")
    p = Portal(tmp_path, rt)
    device = p.store.add_device(p.nda, "Acme-HfO2-D1")
    rt.mount(p.nda, device)

    def approve_all():   # a person at the computer says yes to the NDA chat's run
        while not done.is_set():
            entry = rt.approver.wait_for_pending(timeout=0.2)
            if entry:
                rt.approver.answer(entry["id"], True)
    done = threading.Event()
    threading.Thread(target=approve_all, daemon=True).start()
    p.turn(p.secret, [("run_plan", {"plan": NDA}),
                      ("watch", {"reading": "tc.T_sample", "above": 5, "note": "Acme-HfO2-D1 must stay below 5 K"}),
                      ("queue_add", {"item": NDA, "note": "Acme wafer W17 first pass"})])
    done.set()
    ask = _relay(rt, tmp_path, monkeypatch)

    # a request of the NDA work waits for a person
    asked = threading.Event()

    def nda_request():
        with rt.viewing(p.agent.viewer_for(p.store.load(p.secret))):
            asked.set()
            rt.gate.ask(ApprovalRequest("Run plan acme_gate: 6 points", ["smu.V 0 V → 1 V on Acme-HfO2-D1"]))
    threading.Thread(target=nda_request, daemon=True).start()
    asked.wait(5)
    deadline = time.time() + 5
    while not rt.approver.pending_list() and time.time() < deadline:
        time.sleep(0.05)

    status, raw = ask("GET", "/api/state")
    assert status == 200 and b"acme" not in raw.lower() and b"Acme" not in raw, raw[:2000]
    state = json.loads(raw)
    assert state["live"]["status"] == "done" and state["live"]["run_id"] == "unshared"   # after it ended too
    (card,) = state["pending"]
    assert card["title"] == "A request of unshared work" and card["id"]                  # that one waits
    assert state["mounted"]["device_name"] == "a sample of unshared work"
    (mine,) = [w for w in state["watches"] if w["source"] == "agent"]   # its rule stays, its note goes
    assert mine["note"] == "" and "above 5" in mine["rule"]
    assert any(w["note"] for w in state["watches"] if w["source"] == "lab")   # the lab's own keep theirs
    assert state["queue"]["items"][0]["name"] == "an item of unshared work"
    assert any(n["title"] == "A notice of unshared work" for n in state["notices"])
    status, raw = ask("GET", "/api/live")
    assert status == 200 and b"acme" not in raw.lower() and json.loads(raw)["live"]["rows"] == []
    status, raw = ask("GET", "/api/watches")
    assert status == 200 and b"Acme" not in raw
    # declining works from the phone; approving a card the phone never saw doesn't
    status, raw = ask("POST", "/api/approvals/" + card["id"], {"approve": True})
    assert status == 403 and b"Tailscale" in raw
    status, raw = ask("POST", "/api/approvals/" + card["id"], {"approve": False})
    assert status == 200


# ------------------------------------------------------------------ at a glance (t09)

def test_a_confidential_project_and_its_chats_show_a_lock_and_one_switch_sets_it(rt, tmp_path):
    from starlette.testclient import TestClient
    from labharness.web.app import STATIC, create_app
    from labharness.web.settings import Settings
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "lockMark(chatPrivacy(c))" in js and "lockMark(privacyOf(p.rules, p))" in js   # the sidebar's locks
    assert 'id="project-confidential"' in page and '$("project-confidential").addEventListener' in js
    assert "privacy.label" in js and '"Unpublished"' in js                                 # the chip says which
    store = ChatStore(tmp_path / "chats")
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, store, hosts=["testserver"], providers={"scripted": Scripted([])}))
    h = {"X-Kelvin-Token": settings.access_key}
    p = client.post("/api/projects", headers=h, json={"name": "Acme NDA"}).json()
    chat = client.post("/api/chats", headers=h, json={"model": "scripted:m1", "project": p["id"]}).json()
    # the one switch sends both settings, which keep the form projects have always had
    r = client.patch("/api/projects/" + p["id"], headers=h, json={
        "rules": {"providers": ["local"], "literature": False}, "sharing": "confidential"})
    assert r.json()["confidential"] is True and r.json()["rules"] == {"providers": ["local"], "literature": False}
    listed = {x["id"]: x for x in client.get("/api/projects", headers=h).json()}
    assert listed[p["id"]]["confidential"] and listed[p["id"]]["rules"]["providers"] == ["local"]
    (mine,) = [c for c in client.get("/api/chats", headers=h).json() if c["id"] == chat["id"]]
    assert mine["project"] == p["id"]        # what the lock beside the chat is drawn from
    client.patch("/api/chats/" + chat["id"], headers=h, json={"project": None})   # out of it, it keeps the marks
    (mine,) = [c for c in client.get("/api/chats", headers=h).json() if c["id"] == chat["id"]]
    assert mine.get("confidential") is True and mine["rules"]["providers"] == ["local"]
