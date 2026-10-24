"""Reports in a clear structure: project -> device (sample) -> scan. Each scan's report sits in its run folder; each
project has a folder with its master report, a report per device, an index of every scan and a dated history."""
import csv

import pytest

from labharness import ScriptedApprover, build_virtual_lab
from labharness.reports import kinds
from test_reports import check_pdf, check_pptx, pdf_strings

SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 4, step: 0.2} }
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""

D3 = {"id": "d3", "name": "D3", "material": "graphene/hBN", "description": "Hall bar, 8 contacts"}
D4 = {"id": "d4", "name": "D4", "material": "graphene", "description": ""}
OLD = {"id": "d9", "name": "Broken chip", "material": "", "description": "", "retired": True}


def runtime(tmp_path, answers=20):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * answers), tmp_path / "data", None)
    projects = {"p1": {"name": "Graphene", "devices": [D3, D4, OLD], "notes": [("decision", "Measure at 4 K first")],
                       "references": ["Novoselov et al. 2004"], "summary": ""}}
    rt.project_info = lambda pid: dict(projects.get(pid, {"name": "General", "devices": [], "notes": [],
                                                           "references": [], "summary": ""}), id=pid)
    rt.project_ids = lambda: [None, "p1"]
    rt.report_delay = 3600          # the tests flush by hand
    return rt, projects


def test_every_run_records_its_project_and_device(tmp_path):
    rt, _ = runtime(tmp_path)
    rt.mount("p1", D3)
    rt.run(SWEEP)
    rt.run(SWEEP.replace("plan: gate_sweep", "plan: gate_sweep\ndevice: D4"))   # another device on the same chip
    rt.unmount()
    rt.attribution = {"project": "p1"}                                          # a chat in the project, nothing mounted
    rt.run(SWEEP)
    rt.attribution = {}
    rt.run(SWEEP)                                                               # outside every project: General
    got = [(d.project_name, d.device_name) for d in rt._digests()]
    assert got == [("Graphene", "D3"), ("Graphene", "D4"), ("Graphene", ""), ("General", "")]
    assert "mounted by human" in str(rt._mounts(rt.gate.log.events(), "D3"))
    assert rt._load_mounted() == {} and rt.mounted == {}                       # unmounted: nothing kept


def test_the_scan_report_is_one_short_page(tmp_path):
    rt, _ = runtime(tmp_path)
    rt.mount("p1", D3)
    rt.run(SWEEP)
    folder = rt._run_dir(rt.runs_index(1)[0]["run_id"])
    assert check_pdf(folder / "report.pdf") == 1
    text = " ".join(pdf_strings(folder / "report.pdf"))
    for want in ("SCAN REPORT", "gate_sweep on D3", "Graphene", "Result", "Conditions", "Swept", "Measured",
                 "Approvals and alarms", "run.h5 (every point"):
        assert want in text, want
    for gone in ("Contents", "1. Result", "Appendix", "plan: gate_sweep", "Next:"):   # the plan is in run.h5
        assert gone not in text, gone
    assert text.count("approved: Run plan gate_sweep") == 1                     # its approval, once
    from labharness import rundata
    kept = rundata.load(folder, with_rows=False).snapshots                      # every setting, start and end
    assert kept["start"]["smu"]["output"] is False and kept["end"]["smu"]["output"] is True
    slides = check_pptx(folder / "report.pptx")
    assert "SCAN REPORT" in slides[0] and "has a peak" in slides[0] and len(slides) <= 5


def test_a_project_folder_with_its_master_and_device_reports(tmp_path):
    rt, projects = runtime(tmp_path)
    rt.mount("p1", D3)
    rt.run(SWEEP)
    rt.run(SWEEP)
    rt.mount("p1", D4)
    rt.run(SWEEP)
    written = rt.flush_reports()
    assert written == {"Graphene": ["devices/D3.pdf", "devices/D3.pptx", "devices/D4.pdf", "devices/D4.pptx",
                                    "Graphene_master.pdf", "Graphene_master.pptx", "index.csv"]}   # no retired chip
    folder = rt.reports_dir / "Graphene"
    master = " ".join(pdf_strings(folder / "Graphene_master.pdf"))
    for want in ("MASTER REPORT", "1. Summary", "2. Goals and decisions", "Decision: Measure at 4 K first",
                 "3. Devices compared", "graphene/hBN", "4. Main results and figures", "5. Timeline",
                 "6. Open questions and issues", "7. References", "Novoselov et al. 2004", "8. Index",
                 "devices/D3.pdf", "3 scans on 2 devices (D3, D4)"):
        assert want in master, want
    device = " ".join(pdf_strings(folder / "devices" / "D3.pdf"))
    for want in ("DEVICE REPORT", "1. The device", "Hall bar, 8 contacts", "2. Mount history", "mounted by human",
                 "3. Results by measurement", "4. Trends", "(2 scans)", "5. Ranges explored", "smu.V", "6. Problems",
                 "No problems recorded.", "7. Scan index"):
        assert want in device, want
    rows = list(csv.reader(open(folder / "index.csv", encoding="utf-8")))
    assert rows[0][:3] == ["started", "device", "measurement"] and [r[1] for r in rows[1:]] == ["D3", "D3", "D4"]
    assert sorted(p.suffix for p in (folder / "history").iterdir()) == [".pdf", ".pptx"]
    check_pptx(folder / "Graphene_master.pptx")
    projects["p1"]["summary"] = "We compare two Hall bars."                   # the person's own opening
    rt.refresh_reports(["p1"], "pdf")
    assert "We compare two Hall bars." in " ".join(pdf_strings(folder / "Graphene_master.pdf"))


def test_kinds_can_be_switched_off_and_runs_before_this_version_still_count(tmp_path):
    rt, _ = runtime(tmp_path)
    rt.run(SWEEP)
    old = rt.runs_index(1)[0]["run_id"]
    import json
    h5py = pytest.importorskip("h5py")
    with h5py.File(rt._run_dir(old) / "run.h5", "r+") as f:                 # as Kelvin 0.3.4 kept it: no project
        m = json.loads(f.attrs["meta_json"])
        for k in ("project", "project_name", "device", "device_name"):
            m.pop(k, None)
            if k in f.attrs:
                del f.attrs[k]
        f.attrs["meta_json"] = json.dumps(m)
    rt.project_chat_runs = lambda pid: [old] if pid == "p1" else []          # a chat in the project ran it
    rt.report_kinds = {"scan", "project"}
    written = rt.refresh_reports(["p1"])
    assert not any(f.startswith("devices/") for f in written["Graphene"])
    assert "1 scan" in " ".join(pdf_strings(rt.reports_dir / "Graphene" / "Graphene_master.pdf"))


def test_reports_tree_paths_and_kinds_on_request(tmp_path):
    rt, _ = runtime(tmp_path)
    rt.mount("p1", D3)
    rt.run(SWEEP)
    out = rt.make_report(kind="device", device="d3", project="p1", fmt="pdf")
    assert out.splitlines()[0] == "report: Graphene/devices/D3.pdf"
    assert rt.make_report(kind="project", project="p1", fmt="both").splitlines()[:2] == [
        "report: Graphene/Graphene_master.pdf", "report: Graphene/Graphene_master.pptx"]
    assert rt.make_report(kind="device", device="nope", project="p1").startswith("no report: no device")
    assert rt.make_report(kind="scan").startswith("scan report: runs/")
    rt.make_report(runs=[rt.runs_index(1)[0]["run_id"]], fmt="pdf")
    tree = rt.reports_tree()
    graphene = next(p for p in tree["projects"] if p["folder"] == "Graphene")
    assert graphene["project"] == "p1" and graphene["name"] == "Graphene"
    assert "Graphene/devices/D3.pdf" in graphene["devices"] and tree["custom"][0].startswith("custom/")
    assert rt.report_path("Graphene/devices/D3.pdf").is_file()
    for bad in ("Graphene/../events.jsonl", "../x.pdf", "Graphene/devices/../../x.pdf", "a/b/c/d.pdf", ".x/a.pdf"):
        with pytest.raises(KeyError):
            rt.report_path(bad)


def test_old_formatted_results_are_read_back_as_numbers():
    assert kinds.result_number({"value": "12.5 ± 0.4 mV"}) == (0.0125, 0.0004, "V")
    assert kinds.result_number({"value": "1.502 ± 0.004 V"})[0] == 1.502
    assert kinds.result_number({"num": 3.0, "err": None, "unit": "K", "value": "3 K"}) == (3.0, None, "K")
    assert kinds.result_number({"value": "no number"}) == (None, None, "")
    assert kinds.safe_name("My study / v2") == "My_study_v2"


def test_devices_mounting_and_reports_in_the_portal(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted, client_for
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 5), tmp_path / "data", None)
    rt.report_delay = 3600
    store = ChatStore(tmp_path / "chats")
    client, headers = client_for((rt, Settings(tmp_path / "settings.json"), store), {"scripted": Scripted([])})
    with client:
        p = client.post("/api/projects", json={"name": "Graphene"}, headers=headers).json()
        r = client.post("/api/projects/%s/devices" % p["id"], json={"name": "D3", "material": "graphene/hBN"},
                        headers=headers).json()
        d3 = r["devices"][0]
        assert r["folder"] == "Graphene" and r["master"] == []
        assert client.post("/api/projects/%s/devices" % p["id"], json={"name": "d3"}, headers=headers).status_code == 400
        assert client.post("/api/projects/general/devices", json={"name": "Test chip"}, headers=headers).status_code == 200
        assert client.patch("/api/projects/%s/devices/%s" % (p["id"], d3["id"]), json={"description": "8 contacts"},
                            headers=headers).json()["description"] == "8 contacts"
        m = client.post("/api/mount", json={"project": p["id"], "device": "D3"}, headers=headers).json()
        assert m["mounted"]["device_name"] == "D3" and [c["name"] for c in m["choices"]] == ["General", "Graphene"]
        assert client.get("/api/state", headers=headers).json()["mounted"]["project_name"] == "Graphene"
        rt.run(SWEEP)
        assert client.put("/api/projects/%s/summary" % p["id"], json={"text": "Gate response of D3."},
                          headers=headers).json()["summary"] == "Gate response of D3."
        written = client.post("/api/reports/refresh", json={"project": p["id"]}, headers=headers).json()["written"]
        assert "Graphene_master.pdf" in written["Graphene"]
        tree = client.get("/api/reports/tree", headers=headers).json()
        assert tree["projects"][0]["devices"][0] == "Graphene/devices/D3.pdf"
        got = client.get("/api/reports/Graphene/Graphene_master.pdf", headers=headers)
        assert got.status_code == 200 and got.content.startswith(b"%PDF")
        assert client.get("/api/reports/Graphene%2F..%2F..%2Fsettings.json", headers=headers).status_code == 404
        assert "Gate response of D3." in " ".join(pdf_strings(rt.reports_dir / "Graphene" / "Graphene_master.pdf"))
        assert client.post("/api/mount", json={}, headers=headers).json()["mounted"] is None


def test_kelvin_adds_and_mounts_devices_from_a_chat(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted, client_for, stream_turn
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 5), tmp_path / "data", None)
    rt.report_delay = 3600
    store = ChatStore(tmp_path / "chats")
    call = lambda i, name, args: [{"type": "tool_call", "id": "c%d" % i, "name": name, "input": args}]   # noqa: E731
    done = [{"type": "text", "text": "Done."}]
    bot = Scripted([call(1, "add_device", {"name": "D7", "material": "MoS2", "description": "4 contacts"}), done,
                    call(2, "mount_device", {"device": "d7"}), done,
                    call(3, "run_plan", {"plan": SWEEP}), done,
                    call(4, "devices", {}), done])
    client, headers = client_for((rt, Settings(tmp_path / "settings.json"), store), {"scripted": bot})
    with client:
        p = client.post("/api/projects", json={"name": "TMDs"}, headers=headers).json()
        chat = client.post("/api/chats", json={"model": "scripted:m1", "project": p["id"]}, headers=headers).json()
        for text in ("add D7, a MoS2 flake with 4 contacts", "I mounted D7", "sweep the gate", "which devices?"):
            events = stream_turn(client, headers, chat["id"], text, "scripted:m1")
        end = [e for e in events if e["type"] == "tool_end"][-1]
        assert "D7 (MoS2)" in end["content"] and "mounted now: D7 (TMDs)" in end["content"]
        assert {"add_device", "mount_device", "devices"} <= set(bot.tool_names[0])
        assert [(d.project_name, d.device_name) for d in rt._digests()] == [("TMDs", "D7")]
        assert rt.attribution == {}                                            # cleared when the turn ended


def test_the_report_kinds_setting(tmp_path):
    from labharness.web.settings import Settings
    s = Settings(tmp_path / "settings.json")
    assert s.public()["report_kinds"] == ["scan", "device", "project"]
    s.update({"report_kinds": "scan,project,bogus"})
    assert s.public()["report_kinds"] == ["scan", "project"]
    s.update({"report_kinds": "none"})
    assert s.public()["report_kinds"] == []
    s.update({"report_kinds": "scan,device,project"})
    assert s.public()["report_kinds"] == ["scan", "device", "project"]
