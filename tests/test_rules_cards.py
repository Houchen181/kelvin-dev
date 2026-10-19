"""Confidential projects (which models may see them) and cards for new instruments drafted from their manuals."""
import pytest

from labharness import carddraft
from labharness.virtual_lab import lab_registry

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from labharness.web import rules  # noqa: E402
from test_compare import bot, say, tool  # noqa: E402
from test_web import client_for, parts, stream_turn  # noqa: E402,F401

LINE = "plan: scan\nsweep: {stage.position: {from: 0, to: 2, step: 1}}\nmeasure: [lockin.X]\n"
LOCAL_ONLY = {"providers": ["local"], "literature": False}


def test_confidential_tools_cannot_write_global_memory_or_lookup_papers(parts):
    runtime, settings, store = parts
    outbound = []
    runtime.literature.fetch = lambda url: outbound.append(url) or b"{}"
    local = bot("local", [tool("remember", {"scope": "global", "kind": "profile", "text": "SECRET_SAMPLE"}),
                          say("not promoted"), tool("save_paper", {"paper": "doi:10.1234/SECRET_SAMPLE"}),
                          say("not fetched")])
    client, headers = client_for(parts, {"local": local})
    with client:
        project = store.create_project("secret")
        store.set_rules(project["id"], LOCAL_ONLY)
        chat = store.create("local:m1", project=project["id"])
        events = stream_turn(client, headers, chat["id"], "remember", "local:m1")
        result = next(e for e in events if e["type"] == "tool_end")
        assert result["is_error"] and "cannot write global memory" in result["content"]
        assert store.global_notes() == []
        events = stream_turn(client, headers, chat["id"], "keep paper", "local:m1")
        assert next(e for e in events if e["type"] == "tool_end")["is_error"]
        assert outbound == [] and "save_paper" not in local.tool_names[-1]


def test_portal_models_cannot_read_another_projects_runs_reports_or_log(parts):
    import copy
    runtime, _, store = parts
    local = bot("local", [tool("run_plan", {"plan": LINE.replace("scan", "SECRET_SCAN")}), say("done")])
    cloud = bot("openai", [])
    histories = []
    original_stream = cloud.stream

    async def capture(model, system, history, tools, emit):
        histories.append(copy.deepcopy(history))
        return await original_stream(model, system, history, tools, emit)

    cloud.stream = capture
    client, headers = client_for(parts, {"local": local, "openai": cloud})
    with client:
        secret = store.create_project("secret")
        store.set_rules(secret["id"], LOCAL_ONLY)
        chat = store.create("local:m1", project=secret["id"])
        stream_turn(client, headers, chat["id"], "scan", "local:m1")
        run_id = runtime.runs_index()[0]["run_id"]
        assert runtime._run_meta(run_id)["scope"]["project"] == secret["id"]
        cloud.turns = [tool("list_runs", {}), tool("get_run_data", {"run_id": run_id}, "c2"),
                       tool("recent_events", {}, "c3"), tool("make_report", {"runs": [run_id]}, "c4"), say("done")]
        public = store.create("openai:m1")
        events = stream_turn(client, headers, public["id"], "read data", "openai:m1")
        results = {e["name"]: e for e in events if e["type"] == "tool_end"}
        assert results["list_runs"]["content"] == "no runs yet"
        assert "no run" in results["get_run_data"]["content"]
        assert "SECRET_SCAN" not in results["recent_events"]["content"]
        assert results["make_report"]["content"].startswith("no report:")
        assert not any("lockin.X" in r.get("content", "") for h in histories for m in h
                       if m.get("role") == "tool" for r in m.get("results", []))


def test_incognito_can_use_tools_without_a_missing_policy_error(parts):
    runtime, _, store = parts
    runtime.search_papers = lambda *a, **k: "no papers"
    model = bot("openai", [tool("search_papers", {"query": "physics"}), say("done")])
    client, headers = client_for(parts, {"openai": model})
    with client:
        chat = store.create("openai:m1", incognito=True)
        events = stream_turn(client, headers, chat["id"], "look up physics", "openai:m1")
        assert next(e for e in events if e["type"] == "tool_end")["content"] == "no papers"
        assert not any(e["type"] == "error" for e in events)


def test_custom_provider_policy_has_a_readable_description():
    assert rules.describe({"providers": ["openrouter"]}) == "only openrouter may see it"


def test_standalone_mcp_workspace_does_not_inherit_portal_data(parts):
    import asyncio
    from mcp import Client
    from labharness import datascope
    from labharness.agent.server import build_server
    runtime, _, _ = parts
    with datascope.use({"project": "psecret", "chat": "private"}):
        rec = runtime.engine.store.create("private", {"plan": "private"}, "agent")
        rec.add({"secret_result": 123.456})
        rec.finish(status="done", points=1)

    async def check():
        async with Client(build_server(runtime, scope={"project": None, "chat": "external-mcp"})) as client:
            result = await client.call_tool("get_run_data", {"run_id": rec.run_id})
            text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
            assert "123.456" not in text and "no run" in text

    asyncio.run(check())


def test_rule_sets_only_get_stricter():
    assert rules.normalize(None) == {"providers": None, "literature": True}
    assert rules.normalize({"providers": ["local", "anthropic"]})["providers"] == ["anthropic", "local"]
    assert rules.normalize({"providers": list(rules.PROVIDERS)})["providers"] == list(rules.PROVIDERS)  # never widened
    assert rules.normalize({"providers": ["openrouter", "local"]})["providers"] == ["local", "openrouter"]
    with pytest.raises(ValueError):
        rules.normalize({"providers": ["Sky Net"]})
    merged = rules.merge({"providers": ["anthropic", "local"]}, {"providers": ["local", "gemini"], "literature": False})
    assert merged == {"providers": ["local"], "literature": False}
    assert rules.merge(None, {"providers": None}) == {"providers": None, "literature": True}
    assert rules.allows(LOCAL_ONLY, "local:llama") and not rules.allows(LOCAL_ONLY, "anthropic:claude")
    assert rules.allows(LOCAL_ONLY, "demo:demo")                      # the scripted demo never leaves the computer
    assert rules.describe(LOCAL_ONLY) == "only local models may see it; no literature search"
    assert rules.describe(None) == "any model" and not rules.restrictive(None) and rules.restrictive(LOCAL_ONLY)


def test_a_confidential_project_keeps_other_models_out(parts):
    runtime, settings, store = parts
    settings.update({"reviewer": "anthropic:m1"})
    local = bot("local", [tool("search_papers", {"query": "our secret sample"}), say("Local answer."),
                          tool("run_plan", {"plan": LINE}, "p1"), say("Ran it.")])
    cloud = bot("anthropic", [say("verdict: go")])
    client, headers = client_for(parts, {"local": local, "anthropic": cloud})
    with client:
        project = client.post("/api/projects", json={"name": "Secret sample"}, headers=headers).json()
        bad = client.patch("/api/projects/" + project["id"], json={"rules": {"providers": ["skynet"]}}, headers=headers)
        assert bad.status_code == 400
        p = client.patch("/api/projects/" + project["id"], json={"rules": LOCAL_ONLY}, headers=headers).json()
        assert p["rules"] == LOCAL_ONLY
        chat = client.post("/api/chats", json={"model": "local:m1", "project": project["id"]}, headers=headers).json()

        r = client.post("/api/chats/%s/turn" % chat["id"], json={"text": "hi", "model": "anthropic:m1"}, headers=headers)
        assert r.status_code == 400 and "only local models may see it" in r.json()["error"]
        events = stream_turn(client, headers, chat["id"], "what is known about our sample?", "local:m1")
        refused = next(e for e in events if e["type"] == "tool_end")
        assert refused["is_error"] and "keeps its work off the internet" in refused["content"]
        assert "search_papers" not in local.tool_names[0] and "Confidential project" in local.systems[0]
        assert client.get("/api/chats/" + chat["id"], headers=headers).json()["rules"] == LOCAL_ONLY
        assert store.load(chat["id"])["rules"] == LOCAL_ONLY           # stamped on the chat itself

        events = stream_turn(client, headers, chat["id"], "scan it", "local:m1")
        opinion = next(e for e in events if e["type"] == "review")
        assert "No second opinion" in opinion["text"] and "may not see this project" in opinion["text"]
        assert cloud.prompts == []                                      # the plan never went to the cloud reviewer
        assert client.post("/api/chats/%s/review" % chat["id"], json={"model": "anthropic:m1"},
                           headers=headers).status_code == 400
        assert client.post("/api/chats/%s/compare" % chat["id"], json={"text": "x", "models": ["local:m1", "anthropic:m1"]},
                           headers=headers).status_code == 400

        client.patch("/api/chats/" + chat["id"], json={"project": None}, headers=headers)   # moved out of the project
        assert client.post("/api/chats/%s/turn" % chat["id"], json={"text": "hi", "model": "anthropic:m1"},
                           headers=headers).status_code == 400
        client.patch("/api/chats/" + chat["id"], json={"project": project["id"]}, headers=headers)
        client.delete("/api/projects/" + project["id"], headers=headers)                     # or the project deleted
        assert client.post("/api/chats/%s/turn" % chat["id"], json={"text": "hi", "model": "anthropic:m1"},
                           headers=headers).status_code == 400
        assert [c for c in client.get("/api/chats", headers=headers).json() if c["id"] == chat["id"]][0]["rules"] == LOCAL_ONLY

        other = client.post("/api/chats", json={"model": "anthropic:m1"}, headers=headers).json()
        assert client.get("/api/chats/" + other["id"], headers=headers).json()["rules"] == {"providers": None, "literature": True}


# ------------------------------------------------------------------ cards

GOOD = """card: 1
id: acme/sm-2000
model: ACME SM-2000 Source-Meter
vendor: ACME
kind: source-meter
status: draft
manual: "SM-2000 manual, chapter 7"
identify: { idn: "^ACME,SM-2000" }
error_query: "SYSTem:ERRor?"
parameters:
  V:
    label: Voltage
    kind: drive
    unit: V
    set: "SOURce:VOLTage {value}"
    get: "SOURce:VOLTage?"
    limits: [-20, 20]
    safe: 0
    ramp: gate
    max_rate: 1
  I:
    label: Current
    kind: measure
    unit: A
    get: "MEASure:CURRent?"
"""
MANUAL = "SM-2000 programming. SOURce:VOLTage <v> sets the output voltage, -20 to 20 V. " * 6


def test_checking_and_keeping_cards(tmp_path):
    card, problems, warnings = carddraft.check(GOOD)
    assert card.id == "acme/sm-2000" and problems == [] and warnings == []
    assert carddraft.check("card: [1")[1][0].startswith("the YAML does not parse")
    assert "model: required" in carddraft.check("card: 1\nid: x/y\n")[1]
    _, _, warnings = carddraft.check(GOOD.replace('identify: { idn: "^ACME,SM-2000" }\n', ""))
    assert warnings == ["no identify.idn: a scan cannot recognise this instrument"]
    assert carddraft.extract_yaml("Here it is:\n```yaml\ncard: 1\nid: a/b\n```\nNotes.") == "card: 1\nid: a/b\n"

    folder = tmp_path / "cards"
    builtin = {"keithley/2400"}
    with pytest.raises(ValueError, match="must have status: draft"):
        carddraft.save(folder, GOOD.replace("status: draft", "status: lab-verified"), builtin, by="agent")
    with pytest.raises(ValueError, match="already has a card keithley/2400"):
        carddraft.save(folder, GOOD.replace("acme/sm-2000", "keithley/2400"), builtin)
    card, path, _ = carddraft.save(folder, GOOD, builtin, by="agent")
    assert path.name == "acme_sm-2000.yaml" and card.status == "draft"
    assert carddraft.set_status(folder, "acme/sm-2000", "lab-verified").status == "lab-verified"
    with pytest.raises(ValueError, match="a person has reviewed acme/sm-2000"):
        carddraft.save(folder, GOOD, builtin, by="agent")               # Kelvin never undoes a person's review
    (folder / "broken.yaml").write_text("card: 1\nid: [", encoding="utf-8")
    listed = {c["file"]: c for c in carddraft.user_cards(folder)}
    assert listed["acme_sm-2000.yaml"]["status"] == "lab-verified" and listed["broken.yaml"]["status"] == "broken"
    (folder / "clash.yaml").write_text(GOOD.replace("acme/sm-2000", "keithley/2400"), encoding="utf-8")
    registry = lab_registry(tmp_path)
    assert "acme/sm-2000" in registry and registry.get("keithley/2400").vendor != "ACME"   # built-ins win
    carddraft.delete(folder, "acme/sm-2000")
    with pytest.raises(KeyError):
        carddraft.delete(folder, "acme/sm-2000")


def test_kelvin_drafts_a_card_over_mcp(parts, tmp_path):
    runtime, _, _ = parts
    from test_agent import call
    runtime.cards_dir = tmp_path / "cards"
    guide, checked, bad, saved = call(runtime, ("card_guide", {}), ("check_card", {"card": GOOD}),
                                      ("check_card", {"card": "card: 1"}), ("save_card", {"card": GOOD}))
    assert "schema version 1" in guide and "id: keithley/2400" in guide           # the format and a full example
    assert checked == "valid: acme/sm-2000 (ACME SM-2000 Source-Meter), 2 parameters; drives: V"
    assert bad.startswith("not valid:")
    assert saved.startswith("saved draft card acme/sm-2000") and "simulation-only" in saved
    assert "acme/sm-2000" in runtime.lab.registry and "card acme/sm-2000 saved (draft)" in runtime.events(3)
    assert call(runtime, ("save_card", {"card": GOOD.replace("status: draft", "status: lab-verified")}))[0].startswith(
        "not saved: a card Kelvin writes must have status: draft")


def test_cards_in_the_portal(parts):
    runtime, settings, store = parts
    broken = "```yaml\ncard: 1\nid: acme/sm-2000\n```"
    drafter = bot("drafter", [say("Here:\n" + broken), say("Fixed:\n```yaml\n" + GOOD + "```\nI left out the trigger system.")])
    client, headers = client_for(parts, {"drafter": drafter})
    with client:
        assert runtime.cards_dir == settings.path.parent / "cards"
        assert client.get("/api/cards", headers=headers).json()["cards"] == []
        assert client.post("/api/cards/draft", json={"manual": "too short"}, headers=headers).status_code == 400
        r = client.post("/api/cards/draft", json={"manual": MANUAL, "name": "ACME SM-2000", "model": "drafter:m1"},
                        headers=headers).json()
        assert r["problems"] == [] and r["id"] == "acme/sm-2000" and r["notes"] == "I left out the trigger system."
        assert "ACME SM-2000" in drafter.prompts[0][0] and "SOURce:VOLTage <v>" in drafter.prompts[0][0]
        assert "model: required" in drafter.prompts[1][0]                       # repaired with the checker's words
        assert client.post("/api/cards/check", json={"yaml": "card: 1"}, headers=headers).json()["problems"]

        saved = client.post("/api/cards", json={"yaml": r["yaml"]}, headers=headers).json()
        assert saved == {"id": "acme/sm-2000", "file": "acme_sm-2000.yaml", "status": "draft", "warnings": []}
        assert any(c["id"] == "acme/sm-2000" for c in client.get("/api/lab", headers=headers).json()["cards"])
        assert client.post("/api/cards/status", json={"id": "acme/sm-2000", "status": "lab-verified"},
                           headers=headers).json() == {"id": "acme/sm-2000", "status": "lab-verified"}
        renamed = GOOD.replace("acme/sm-2000", "acme/sm-2000b").replace("status: draft", "status: lab-verified")
        client.post("/api/cards", json={"yaml": renamed, "replace": "acme/sm-2000"}, headers=headers)
        ids = [c["id"] for c in client.get("/api/cards", headers=headers).json()["cards"]]
        assert ids == ["acme/sm-2000b"] and "acme/sm-2000" not in runtime.lab.registry
        assert client.post("/api/cards/delete", json={"id": "acme/sm-2000b"}, headers=headers).json() == {"ok": True}
        assert "acme/sm-2000b" not in runtime.lab.registry
