"""The Notebook is the Analysis pane's other view (asked for 2026-11-08: "what about fold the notebook into the analysis
panel? i feel the notebook is similar to the repots"): Analysis | Notebook at the pane's top, the view kept per browser,
and no Notebook button in the top bar. Each view has a bar under the pane's head: Analysis its dataset, Analyze again
and Report…; the Notebook its day, Draft, New and the day's download for another notebook, which left the Reports
dialog. A day's page lists the reports made that day and shows the day as the person in the asking chat sees the lab.
A run an entry cites opens in Analysis, and Kelvin's analysis from the chat never takes the pane from an entry being
written."""
from datetime import date

import pytest

pytest.importorskip("starlette")
pytest.importorskip("h5py")

from labharness.web.app import STATIC  # noqa: E402

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _js(name):
    """One function of app.js, by its name."""
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


def _pane():
    start = PAGE.index('<section id="anpane"')
    return PAGE[start:PAGE.index("</section>", start)]


def test_the_analysis_pane_has_two_views_and_the_top_bar_no_notebook_button():
    pane = _pane()
    head = pane[pane.index("<header"):pane.index("</header>")]
    assert '<div id="an-seg" class="seg runs-seg an-seg" role="tablist" aria-label="Analysis and notebook">' in head
    assert head.index('id="open-an-view"') < head.index('id="open-nb-view"')   # Analysis | Notebook
    assert 'aria-controls="an-view">Analysis</button>' in head
    assert 'aria-controls="nb-view" tabindex="-1">Notebook</button>' in head
    for part in ('id="an-again"', 'id="open-report"', 'id="nb-draft"', 'id="nb-prev"'):   # in the views' own bars
        assert part not in head, part
    an = pane[pane.index('<div id="an-view"'):pane.index('<div id="nb-view"')]
    nb = pane[pane.index('<div id="nb-view"'):]
    bar = an[an.index('<div class="pane-bar">'):an.index('<div id="an-body"')]
    for part in ('<select id="an-run" class="an-run" aria-label="Dataset">', 'id="an-again"', 'id="open-report"'):
        assert part in bar, part
    bar = nb[nb.index('<div class="pane-bar">'):nb.index('<div id="nb-body"')]
    for part in ('id="nb-prev"', 'id="nb-day-label"', 'id="nb-date"', 'id="nb-next"', 'id="nb-draft"', 'id="nb-new"',
                 'id="nb-download"'):
        assert part in bar, part
    assert 'role="tabpanel" aria-labelledby="open-nb-view" hidden>' in nb   # Analysis, until a browser kept Notebook
    assert 'id="toggle-notebook"' not in PAGE and 'id="nbpanel"' not in PAGE
    assert 'title="Analysis and notebook" aria-label="Analysis and notebook"' in PAGE   # the top bar's button: both
    # the day's download left the Reports dialog for the Notebook's bar, still as the asking chat sees the lab
    assert "eln-day" not in PAGE and "eln-day" not in JS
    assert '"/api/eln?day=" + encodeURIComponent(day) + nbChat()' in _js("initNotebook")
    assert "this chat's view: never another project's unshared work" in JS[JS.index("const nbChat = "):][:200]


def test_the_view_is_kept_per_browser_and_shows_what_it_holds_when_chosen():
    show = _js("showAnView")
    assert 'store("lh.anView", view);' in show and "if (changed && !quiet && ANA.open) anViewShown();" in show
    assert 'showAnView(store("lh.anView") === "notebook" ? "notebook" : "analysis", true);' in _js("initAnalysis")
    assert '$("an-seg").addEventListener("keydown", anSegKeys);' in _js("initAnalysis")   # arrows, as Scan | Sequence
    assert "anViewShown();" in _js("toggleAnalysis")
    shown = _js("anViewShown")   # an entry being written stays as it is
    assert 'if (ANA.view === "notebook") { if (!NB.edit) loadNotebook(NB.day || nbToday()); return; }' in shown
    # the Notebook on the screen: no analysis starts for a run that ended meanwhile (Analysis catches up when shown),
    # and the day is drawn anew unless an entry is being written or drafted
    refresh = _js("refreshAnalysisRuns")
    assert refresh.index('if (ANA.view === "notebook")') < refresh.index("startAnalysis()")
    assert "if (!NB.edit && !NB.drafting) loadNotebook(NB.day || nbToday());" in refresh
    # a run an entry cites opens in Analysis; Kelvin's analysis never takes the pane from an entry being written
    assert "const nbOpenRun = (id) => openAnalysis({ run: id, asked: true });" in JS
    assert "if (cite) nbOpenRun(cite.dataset.run);" in _js("initNotebook")
    assert ('if (ev.asked || !(ANA.open && ANA.view === "notebook" && NB.edit)) showAnView("analysis", true);'
            in _js("openAnalysis"))
    # the day as the asking chat sees the lab, the latest day asked for drawn, and the day's reports under its entries
    load = _js("loadNotebook")
    assert '"/api/notebook?day=" + encodeURIComponent(day) + nbChat()' in load and "if (NB.day !== day) return;" in load
    draw = _js("drawNotebook")
    assert draw.index('"No entries."') < draw.index("reportFiles(d.reports)") < draw.index('"Record · "')


def test_a_days_page_lists_its_reports_and_keeps_out_what_the_chat_may_not_see(tmp_path):
    from starlette.testclient import TestClient
    from labharness import ScriptedApprover, notebook
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 50), tmp_path / "data")
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"]))
    h = {"X-Kelvin-Token": settings.access_key}
    (rt.reports_dir / "custom").mkdir(parents=True, exist_ok=True)
    (rt.reports_dir / "custom" / "today.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "outside.pdf").write_bytes(b"%PDF-1.4\n")
    rt.gate.log.write("report", by="person", name="custom/today.pdf", runs=1)
    rt.gate.log.write("report", by="person", name="custom/today.pdf", runs=1)   # made again: listed once
    rt.gate.log.write("report", by="person", name="custom/gone.pdf", runs=1)   # deleted since: not listed
    rt.gate.log.write("report", by="person", name="../../outside.pdf", runs=1)   # never outside the reports folder
    today = date.today().isoformat()
    notebook.write(rt.data_dir, {"title": "Shared", "body": "Cooled down.", "day": today}, "human")
    notebook.write(rt.data_dir, {"title": "Secret", "body": "The confidential sample.", "day": today}, "human",
                   whose={"chat": "c9", "confidential": True})
    day = client.get("/api/notebook", headers=h).json()   # the person at the computer: everything
    assert day["reports"] == ["custom/today.pdf"], day
    assert any("custom/today.pdf" in line for line in day["record"])
    assert sorted(e["title"] for e in day["entries"]) == ["Secret", "Shared"]
    assert client.get("/api/notebook?day=2001-01-01", headers=h).json()["reports"] == []
    # asked from another chat (here one that is gone, which sees shared work only): never another chat's unshared work
    seen = client.get("/api/notebook?chat=nope", headers=h).json()
    assert [e["title"] for e in seen["entries"]] == ["Shared"] and seen["reports"] == ["custom/today.pdf"]
