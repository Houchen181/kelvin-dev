"""Reports: runs written up as PDF and PowerPoint, from what Kelvin already keeps."""
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import date
from pathlib import PurePosixPath

import pytest

from simlab import build_virtual_lab, demo_envelope
from labharness.agent.approval import WebApprover
from labharness.agent.runtime import LabRuntime
from labharness.reports.chart import Chart, Series, layout, scale_of, tick_labels, ticks

LINE = """
plan: stage_scan
sweep: {stage.position: {from: 0, to: 10, step: 1}}
measure: [lockin.X, laser.P_detected]
"""
MAP = """
plan: tc_map
sweep:
  lockin.tc: {values: [0.1, 0.3]}
  stage.position: {from: 0, to: 4, step: 1}
measure: [lockin.X]
"""


@pytest.fixture
def runtime(tmp_path):
    lab = build_virtual_lab(seed=1)
    return LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))


def pdf_strings(path):
    """The text drawn on every page, in order."""
    data = path.read_bytes()
    out = []
    for m in re.finditer(rb"stream\n(.*?)\nendstream", data, re.S):
        try:
            ops = zlib.decompress(m.group(1))
        except zlib.error:
            continue
        out += [s.decode("cp1252") for s in re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", ops)]
    return [s.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\") for s in out]


def check_pdf(path):
    data = path.read_bytes()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[start:].startswith(b"xref")
    count = int(re.match(rb"xref\n0 (\d+)", data[start:]).group(1))
    offsets = [int(x) for x in re.findall(rb"(\d{10}) 00000 n", data[start:])]
    assert len(offsets) == count - 1
    for i, off in enumerate(offsets, 1):                    # every object is where the table says
        assert data[off:].startswith(b"%d 0 obj" % i)
    return int(re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", data).group(1))


A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def check_pptx(path):
    """Every part is well-formed XML, typed, and every relationship points at a part; returns each slide's text."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        trees = {n: ET.fromstring(z.read(n)) for n in names if n.endswith((".xml", ".rels"))}
        typed = {o.get("PartName") for o in trees["[Content_Types].xml"] if o.get("PartName")}
        assert {"/" + n for n in names if n.endswith(".xml") and not n.startswith("[")} <= typed
        assert {p[1:] for p in typed} <= names
        for n, tree in trees.items():
            if not n.endswith(".rels"):
                continue
            base = PurePosixPath(n).parent.parent
            for rel in tree:
                target = str(PurePosixPath(*(base / rel.get("Target")).parts))
                parts = []
                for p in target.split("/"):
                    if p == "..":
                        parts.pop()
                    elif p and p != ".":
                        parts.append(p)
                assert "/".join(parts) in names, (n, rel.get("Target"))
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        ids = [s.get("id") for s in trees["ppt/presentation.xml"].iter(
            "{http://schemas.openxmlformats.org/presentationml/2006/main}sldId")]
        assert len(ids) == len(slides) == len(set(ids))
        for n in slides:   # shape ids are unique on each slide
            shape_ids = [e.get("id") for e in trees[n].iter("{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr")]
            assert len(shape_ids) == len(set(shape_ids)), n
        return [" ".join(t.text or "" for t in trees[n].iter(A + "t")) for n in slides]


def test_ticks_scales_and_labels():
    assert ticks(0, 4) == [0, 1, 2, 3, 4]
    assert ticks(0.13, 0.91) == pytest.approx([0, 0.2, 0.4, 0.6, 0.8, 1.0])
    assert ticks(5, 5)[0] < 5 < ticks(5, 5)[-1]                   # a flat line still gets an axis
    assert scale_of([0, 2e-5, 4e-5], "V") == (1e-6, "µV")     # one prefix for the whole axis
    assert tick_labels([0, 2e-5, 4e-5], 1e-6) == ["0", "20", "40"]
    assert tick_labels([0, 0.25, 0.5], 1) == ["0", "0.25", "0.50"]
    geo = layout(Chart("lockin.X", "smu.V", "V", "V", [Series("a", [0, 1, None, 3], [1e-6, 2e-6, 5, 1e-6]),
                                                        Series("b", [0, 1], [float("nan"), 3e-6])]),
                 300, 200, lambda s, size: len(s) * size * 0.5)
    assert geo.title == "lockin.X (µV)" and geo.x_label == "smu.V (V)"
    assert [len(seg) for seg in geo.lines[0][2]] == [2, 1]          # a missing point breaks the line
    assert [name for name, *_ in geo.legend] == ["a", "b"]
    left, top, w, h = geo.box
    assert all(left <= x <= left + w and top - 0.01 <= y <= top + h + 0.01 for _, _, segs in geo.lines
               for seg in segs for x, y in seg)


def test_runs_written_up_as_pdf_and_slides(runtime):
    runtime.run(LINE)
    runtime.run(MAP)
    out = runtime.make_report(last=2, title="Stage scans", summary="We scanned the stage.\n\nNothing moved.",
                              fmt="both", notes=[("decision", "Scan the stage in 1 mm steps")])
    lines = out.splitlines()
    assert re.match(r"report: custom/\d{8}-\d{6}-Stage_scans\.pdf \(2 runs, \d+ pages\)", lines[0]), out   # on request
    assert re.match(r"report: custom/\d{8}-\d{6}-Stage_scans\.pptx \(2 runs, \d+ slides\)", lines[1]), out
    pdf = runtime.report_path(lines[0].split()[1])
    pages = check_pdf(pdf)
    text = " ".join(pdf_strings(pdf))
    assert pages >= 2 and "page %d of %d" % (pages, pages) in text
    for want in ("Stage scans", "We scanned the stage.", "Nothing moved.", "1. stage_scan", "2. tc_map",
                 "Decision: Scan the stage in 1 mm steps", "lockin.tc = 100 ms", "lockin.tc = 300 ms",
                 "stage.position (mm)", "Virtual Lab (simulated instruments)", "Lab log", "Plan"):
        assert want in text, want
    slides = check_pptx(pdf.with_suffix(".pptx"))
    assert "Stage scans" in slides[0] and "Summary" in slides[1] and "We scanned the stage." in slides[1]
    assert "Project notes" in slides[2]
    assert any(s.startswith("stage_scan") for s in slides) and any("tc_map: conditions" in s for s in slides)
    assert "Lab log" in slides[-1]
    assert "report custom/%s (2 runs)" % pdf.name in runtime.events(5)
    assert [r["name"] for r in runtime.reports_index()] == ["custom/" + n for n in sorted([pdf.name, pdf.with_suffix(".pptx").name], reverse=True)]


def test_a_report_survives_a_lab_without_the_runs_instruments(runtime, monkeypatch):
    runtime.run(LINE)

    def gone(plan):
        raise KeyError("stage is not in this lab")
    monkeypatch.setattr(runtime.engine, "columns", gone)
    files = runtime.report(runtime.pick_runs(last=1), fmt="pdf")
    text = " ".join(pdf_strings(runtime.report_path(files[0]["name"])))
    assert "stage_scan" in text and "stage.position" in text      # plotted, just without units


def test_choosing_runs(runtime):
    assert runtime.make_report() == "no report: no runs yet"
    runtime.run(LINE)
    runtime.run(MAP)
    first, second = runtime.pick_runs(last=5)
    assert runtime.pick_runs([second, first, second]) == [first, second]
    assert runtime.pick_runs(day="today") == [first, second] == runtime.pick_runs(day=date.today().isoformat())
    assert runtime.pick_runs(day="yesterday") == []
    with pytest.raises(KeyError):
        runtime.pick_runs(["20200101-000000-nope"])
    assert runtime.make_report(day="last week").startswith("no report: day must be")
    assert runtime.make_report(runs=["nope"]).startswith("no report: no run")
    assert runtime.make_report(fmt="docx") == "no report: format must be pdf, pptx or both"
    assert "(2 runs" in runtime.make_report()                         # nothing chosen: today's runs
    for bad in ("../events.jsonl", "x.exe", "..pdf", "", "a/b.pdf"):
        with pytest.raises(KeyError):
            runtime.report_path(bad)


def test_kelvin_writes_a_report_from_mcp(runtime):
    pytest.importorskip("mcp")
    from test_agent import call
    runtime.run(LINE)
    [out] = call(runtime, ("make_report", {"format": "pptx", "title": "Quick look"}))
    assert re.match(r"report: custom/\d{8}-\d{6}-Quick_look\.pptx \(1 run, \d+ slides\)", out), out
    check_pptx(runtime.report_path(out.split()[1]))


def test_a_chat_reports_its_projects_runs_and_notes(runtime, tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted, client_for, stream_turn
    run = lambda i, plan: [{"type": "tool_call", "id": "r%d" % i, "name": "run_plan", "input": {"plan": plan}}]
    report = [{"type": "tool_call", "id": "m1", "name": "make_report", "input": {"summary": "Two scans."}}]
    done = [{"type": "text", "text": "Done."}]
    store = ChatStore(tmp_path / "chats")
    bot = Scripted([run(1, LINE), done, run(2, MAP), done, report, done])
    client, headers = client_for((runtime, Settings(tmp_path / "settings.json"), store), {"scripted": bot})
    with client:
        project = client.post("/api/projects", json={"name": "Stage study"}, headers=headers).json()
        store.add_note(project["id"], "The stage sticks near 7 mm.", "quirk")
        a = client.post("/api/chats", json={"model": "scripted:m1", "project": project["id"]}, headers=headers).json()
        b = client.post("/api/chats", json={"model": "scripted:m1", "project": project["id"]}, headers=headers).json()
        stream_turn(client, headers, a["id"], "scan the stage", "scripted:m1")
        runtime.run(MAP.replace("tc_map", "elsewhere"))              # a run from outside the project
        stream_turn(client, headers, b["id"], "now the map", "scripted:m1")
        events = stream_turn(client, headers, b["id"], "write it up for my advisor", "scripted:m1")
        end = next(e for e in events if e["type"] == "tool_end" and e["name"] == "make_report")
        assert not end["is_error"] and "(2 runs" in end["content"]
        assert "make_report" in bot.tool_names[0]
        name = end["content"].split()[1]
        text = " ".join(pdf_strings(runtime.report_path(name)))
        assert "Stage study report" in text and "Two scans." in text and "Quirk: The stage sticks near 7 mm." in text
        assert "stage_scan" in text and "tc_map" in text and "elsewhere" not in text


def test_report_api(runtime, tmp_path, monkeypatch):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    import labharness.web.app as web_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted, client_for
    opened = []
    monkeypatch.setattr(web_app, "open_path", opened.append)
    store = ChatStore(tmp_path / "chats")
    client, headers = client_for((runtime, Settings(tmp_path / "settings.json"), store), {"scripted": Scripted([])})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        r = client.post("/api/reports", json={"scope": "today"}, headers=headers)
        assert r.status_code == 400 and "no runs" in r.json()["error"]
        runtime.run(LINE)
        assert client.post("/api/reports", json={"scope": "chat", "chat_id": chat["id"]}, headers=headers).status_code == 400
        assert client.post("/api/reports", json={"scope": "chat", "chat_id": "nope"}, headers=headers).status_code == 404
        assert client.post("/api/reports", json={"scope": "run", "run": "nope"}, headers=headers).status_code == 404
        assert client.post("/api/reports", json={"scope": "everything"}, headers=headers).status_code == 400
        r = client.post("/api/reports", json={"scope": "last", "last": 3, "format": "both", "title": "Mine"},
                        headers=headers).json()
        assert r["runs"] == 1 and [f["format"] for f in r["files"]] == ["pdf", "pptx"] and "path" not in r["files"][0]
        pdf, pptx = (f["name"] for f in r["files"])
        got = client.get("/api/reports/" + pdf, headers=headers)
        assert got.status_code == 200 and got.headers["content-type"] == "application/pdf" and got.content.startswith(b"%PDF")
        assert "attachment" in got.headers["content-disposition"] and pdf.split("/")[-1] in got.headers["content-disposition"]
        assert client.get("/api/reports/" + pptx, headers=headers).headers["content-type"].endswith("presentationml.presentation")
        assert client.get("/api/reports/" + pdf).status_code in (401, 403)       # the access key is needed
        assert client.get("/api/reports/..%2Fevents.jsonl", headers=headers).status_code == 404
        assert client.post("/api/reports/%s/open" % pptx, json={}, headers=headers).json() == {"ok": True}
        assert opened == [runtime.reports_dir / pptx]
        assert [x["name"] for x in client.get("/api/reports", headers=headers).json()["reports"]] == sorted([pdf, pptx], reverse=True)
        run_id = runtime.pick_runs(last=1)[0]
        r = client.post("/api/reports", json={"scope": "run", "run": run_id, "format": "pptx"}, headers=headers).json()
        assert r["runs"] == 1 and r["files"][0]["slides"] >= 4
