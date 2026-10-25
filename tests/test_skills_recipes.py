"""Lab know-how: skills (procedures in words) and recipes (staged measurements that check their own results), kept
per person and per project once a person approves, and Kelvin's own skills that come with it."""
import pytest

pytest.importorskip("numpy")

from labharness import ScriptedApprover, build_virtual_lab, recipes  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402

GATE = """recipe: Find Gate Peak
description: Coarse, then fine gate sweep around the largest signal.
values: {span: 1}
stages:
  - name: coarse
    plan: |
      plan: coarse_gate
      sweep: {smu.V: {from: {{-span}}, to: {{span}}, points: 11}}
      measure: [lockin.X]
    pass: count(lockin.X) >= 11
    keep: {center: "at_max(lockin.X, smu.V)"}
  - name: fine
    plan: |
      plan: fine_gate
      sweep: {smu.V: {from: {{center - 0.1}}, to: {{center + 0.1}}, points: 5}}
      measure: [lockin.X]
"""


def runtime(tmp_path):
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 200), tmp_path / "data")


def test_a_recipe_reads_writes_and_fills_in():
    r = recipes.parse(GATE)
    assert r.name == "find-gate-peak" and r.values == {"span": 1.0} and [s.name for s in r.stages] == ["coarse", "fine"]
    assert r.stages[0].keep == {"center": "at_max(lockin.X, smu.V)"} and r.stages[0].passes.startswith("count(")
    again = recipes.parse(recipes.dump(r))
    assert again == r and "plan: |" in recipes.dump(r)
    assert "from: -1, to: 1" in recipes.render(r.stages[0].plan, {"span": 1.0})
    assert "from: 0.15, to: 0.35" in recipes.render(r.stages[1].plan, {"span": 1.0, "center": 0.25})
    assert recipes.start_values(r, {"span": 2}) == {"span": 2.0}
    for bad, why in [(GATE.replace("{{-span}}", "{{-width}}"), "no value 'width'"),
                     (GATE.replace("retries", "x") + "    retries: 9\n", "retries is 0 to 5"),
                     (GATE.replace("recipe: Find Gate Peak", "recipe: ''"), "a name is required"),
                     (GATE.replace("keep: {center:", "keep: {9center:"), "not a value name"),
                     (GATE.replace("pass: count(lockin.X) >= 11", "pass: import os"), "stage coarse")]:
        with pytest.raises(recipes.RecipeError) as e:
            recipes.parse(bad)
        assert why in str(e.value), (why, str(e.value))
    with pytest.raises(recipes.RecipeError):
        recipes.start_values(r, {"spam": 3})


def test_a_recipe_runs_its_stages_through_the_gate(tmp_path):
    rt = runtime(tmp_path)
    out = rt.run_recipe(GATE)
    assert out.splitlines()[0] == "recipe find-gate-peak (2 stages), values: span = 1"
    assert "stage coarse: run " in out and "passed (count(lockin.X) >= 11); kept center = " in out
    assert out.splitlines()[-1].startswith("done; values: span = 1, center = ")
    runs = rt.pick_runs(last=5)
    assert len(runs) == 2 and runs[0].endswith("coarse_gate") and runs[1].endswith("fine_gate")
    center = rt.value_of(runs[0], "at_max(lockin.X, smu.V)")
    assert rt.value_of(runs[1], "min(smu.V)") == pytest.approx(center - 0.1)


def test_a_stage_that_does_not_pass_is_tried_again_then_stops(tmp_path):
    rt = runtime(tmp_path)
    failing = GATE.replace("pass: count(lockin.X) >= 11", "pass: max(lockin.X) > 1\n    retries: 1\n    retry: {span: span * 2}")
    out = rt.run_recipe(failing)
    assert "did not pass max(lockin.X) > 1 (try 1 of 2)" in out and "(try 2 of 2)" in out
    assert out.splitlines()[-1] == "stopped: stage coarse did not pass after 2 tries; values: span = 2"
    first, second = rt.pick_runs(last=5)
    assert rt.value_of(first, "max(smu.V)") == pytest.approx(1) and rt.value_of(second, "max(smu.V)") == pytest.approx(2)
    assert rt.run_recipe(GATE, {"spam": 1}).startswith("recipe not run: spam is not one of this recipe's values")


def test_checking_a_recipe_against_the_lab(tmp_path):
    rt = runtime(tmp_path)
    recipe, problem = rt.check_recipe(GATE)
    assert recipe is not None and problem == ""
    assert rt.check_recipe(GATE.replace("measure: [lockin.X]\n    pass", "measure: [lockin.Q]\n    pass"))[1] == \
        "stage coarse: this lab has no lockin.Q"
    assert "stages" in rt.check_recipe("recipe: x\nstages: []")[1]


# ------------------------------------------------------------------ in the portal

pytest.importorskip("starlette")
pytest.importorskip("mcp")
from test_compare import bot, say, tool  # noqa: E402
from test_web import client_for, human, parts, stream_turn  # noqa: E402,F401


def test_kelvins_own_skills_are_listed_and_read(parts):
    runtime_, settings, store = parts
    main = bot("main", [tool("use_skill", {"name": "lock-in-measurement"}), say("Set τ first."),
                        tool("use_skill", {"name": "nope"}, "c2"), say("No such skill.")])
    client, headers = client_for(parts, {"main": main})
    with client:
        chat = client.post("/api/chats", json={"model": "main:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "set up the lock-in", "main:m1")
        stream_turn(client, headers, chat["id"], "and that?", "main:m1")
        listed = client.get("/api/know?scope=global", headers=headers).json()["items"]
    read = next(e for e in events if e["type"] == "tool_end")
    assert read["content"].startswith("Skill lock-in-measurement (Kelvin's own): Setting up a lock-in")
    assert "10τ (24)" in read["content"]
    assert "Skills:\n- " in main.systems[0] and "- lock-in-measurement: Setting up a lock-in" in main.systems[0]
    assert "save_skill" in main.tool_names[0] and "use_skill" in main.tool_names[0]
    missing = [r for m in store.load(chat["id"])["messages"] if m["role"] == "tool" for r in m["results"]][-1]
    assert missing["is_error"] and "no skill 'nope' here (there are: " in missing["content"]
    assert {i["name"] for i in listed if i["scope"] == "kelvin"} >= {"set-up-an-instrument", "check-a-result"}


def test_a_skill_and_a_recipe_are_kept_once_a_person_approves(parts):
    runtime_, settings, store = parts
    project = client_project = None
    steps = "1. Ramp the gate to 0 V.\n2. Sweep ±1 V in 11 points.\n3. Keep the peak."
    main = bot("main", [tool("save_skill", {"name": "Gate Check", "description": "Checking the gate before a run",
                                            "steps": steps}), say("Kept."),
                        tool("save_recipe", {"recipe": GATE}, "c2"), say("Kept it too."),
                        tool("save_skill", {"name": "declined", "description": "x", "steps": "y"}, "c3"), say("Ok."),
                        tool("run_recipe", {"recipe": "find-gate-peak", "values": {"span": 0.5}}, "c4"), say("Ran."),
                        tool("run_recipe", {"recipe": "unknown-one"}, "c5"), say("Hm.")])
    client, headers = client_for(parts, {"main": main})
    with client:
        project = client.post("/api/projects", json={"name": "Graphene"}, headers=headers).json()
        chat = client.post("/api/chats", json={"model": "main:m1", "project": project["id"]}, headers=headers).json()
        human(runtime_.approver, [True])
        first = stream_turn(client, headers, chat["id"], "keep that procedure", "main:m1")
        human(runtime_.approver, [True])
        stream_turn(client, headers, chat["id"], "and the recipe", "main:m1")
        human(runtime_.approver, [False])
        stream_turn(client, headers, chat["id"], "and this", "main:m1")
        human(runtime_.approver, [True] * 30)
        ran = stream_turn(client, headers, chat["id"], "run it", "main:m1")
        stream_turn(client, headers, chat["id"], "and the other", "main:m1")
        mine = client.get("/api/know?scope=%s" % project["id"], headers=headers).json()["items"]
        everyone = client.get("/api/know?scope=global", headers=headers).json()["items"]
        gone = client.delete("/api/know/skill/%s/gate-check" % project["id"], headers=headers)
        kelvins = client.delete("/api/know/skill/global/check-a-result", headers=headers)
        client.delete("/api/projects/%s" % project["id"], headers=headers)
    ask = next(e for e in first if e["type"] == "approval")
    assert ask["title"] == "Keep the skill “gate-check” for this project?" and ask["items"][1] == "1. Ramp the gate to 0 V."
    assert {(i["kind"], i["name"]) for i in mine} == {("skill", "gate-check"), ("recipe", "find-gate-peak")}
    assert not [i for i in everyone if i["scope"] == "global"]                  # kept for the project only
    results = [r for m in store.load(chat["id"])["messages"] if m["role"] == "tool" for r in m["results"]]
    assert results[2]["content"] == "not kept: the person declined"
    assert results[3]["content"].startswith("recipe find-gate-peak (2 stages), values: span = 0.5")
    assert "done; values:" in results[3]["content"]
    assert results[4]["is_error"] and "no kept recipe 'unknown-one' (kept: find-gate-peak)" in results[4]["content"]
    assert "- gate-check: Checking the gate before a run (this project's)" in main.systems[-1]
    assert gone.status_code == 200 and kelvins.status_code == 404
    assert not (store.root / "recipes" / project["id"]).exists()                # it went with its project
    assert any(e["type"] == "approval" for e in ran)


def test_unshared_work_keeps_its_know_how_in_its_project(parts):
    runtime_, settings, store = parts
    main = bot("main", [tool("save_skill", {"name": "x", "description": "d", "steps": "s", "scope": "global"}),
                        say("Ok.")])
    client, headers = client_for(parts, {"main": main})
    with client:
        project = client.post("/api/projects", json={"name": "Secret"}, headers=headers).json()
        client.patch("/api/projects/%s" % project["id"], json={"sharing": "confidential"}, headers=headers)
        chat = client.post("/api/chats", json={"model": "main:m1", "project": project["id"]}, headers=headers).json()
        stream_turn(client, headers, chat["id"], "keep it everywhere", "main:m1")
        incognito = client.post("/api/chats", json={"model": "main:m1", "incognito": True}, headers=headers).json()
    [result] = [r for m in store.load(chat["id"])["messages"] if m["role"] == "tool" for r in m["results"]]
    assert result["is_error"] and "stays in its project (scope project)" in result["content"]
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent
    agent = ChatAgent(runtime_, store, build_server(runtime_), {"main": main})
    context = agent.context(store.load(incognito["id"]))
    names = [t["name"] for t in context["tools"]]
    assert "use_skill" in names and "save_skill" not in names and "- check-a-result:" in context["system"]
