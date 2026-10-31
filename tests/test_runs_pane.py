"""The Runs pane's page: Runs · Scans · Queue in its header, the open runs' tabs in a row below it, the recent runs in
place of the old clock menu, Run again in a finished run's view, and the Scans view of Kelvin's scans and the person's
scripts (GET/POST /api/library, /api/runs/{id}/again)."""
import re

from labharness.web.app import STATIC

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _js(name):
    """One function of app.js, by its name."""
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


def test_every_id_the_portal_script_uses_is_on_the_page():
    used = set(re.findall(r'\$\("([\w-]+)"\)', JS))
    made = set(re.findall(r'id="([\w-]+)"', PAGE)) | set(re.findall(r'\bid: "([\w-]+)"', JS))   # or made by app.js
    assert len(used) > 300 and not sorted(used - made)


def test_the_header_is_runs_scans_queue_and_the_open_runs_have_a_row_below_it():
    head = PAGE[PAGE.index('<section id="runpane"'):PAGE.index('<section id="anpane"')]
    header = head[head.index("<header"):head.index("</header>")]
    seg = re.search(r'<div id="runs-seg"[^>]*>', header).group(0)
    assert 'role="tablist"' in seg
    tabs = re.findall(r'<button id="(open-\w+)"[^>]*role="tab"[^>]*aria-controls="([\w-]+)"', header)
    assert tabs == [("open-runs", "runs-view"), ("open-scans", "scans-view"), ("open-queue", "queue-view")]
    assert 'id="queue-count"' in header and 'id="open-report"' in header and 'id="close-runs"' in header
    assert "runs-history" not in PAGE + JS and "runs-menu" not in PAGE + JS and "showRunsMenu" not in JS
    row = re.search(r'<div id="run-tabs"[^>]*>', head).group(0)
    assert head.index(row) > head.index("</header>") and " hidden" in row   # shown while runs are open
    for panel in ("runs-view", "scans-view", "queue-view"):
        assert re.search(r'id="%s"[^>]*role="tabpanel"' % panel, head), panel
    keys = _js("segKeys")
    assert all(k in keys for k in ("ArrowRight", "ArrowLeft", "Home", "End"))
    view = _js("showView")
    assert "aria-selected" in view and "tabIndex" in view and "loadRunList()" in view and "loadScans()" in view


def test_the_side_panels_stay_independent():
    """Switching the Runs pane's view never opens or closes another panel."""
    for name in ("showView", "showRunList", "showQueue", "loadRunList", "renderRunList", "loadScans", "renderScans"):
        body = _js(name)
        assert not re.search(r"toggle(Lab|Team|Analysis)\(", body), name


def test_the_recent_runs_list_opens_a_run_and_a_run_view_goes_back_to_it():
    listed = _js("renderRunList")
    assert "openRun(r.run_id, r.plan)" in listed and "RUNS.dismissed.delete(r.run_id)" in listed
    assert 'api("/api/runs?limit=' in _js("loadRunList")
    assert "← All runs" in _js("buildRunView") and "showRunList" in _js("buildRunView")


def test_run_again_follows_the_contract():
    view = _js("buildRunView")
    assert '"Run again"' in view and "loadAgain(tab)" in view and "Add to the queue" not in view   # its own now
    load = _js("loadAgain")
    assert '"/api/runs/" + encodeURIComponent(tab.runId) + "/again"' in load
    parts = _js("againParts")
    for text in ('"Run again"', '"Add to the queue"', '"As it ran then"', '"As it is now"', "Restore these ",
                 "Every setting is as it was.", "Couldn't read the lab now.", "c.differs && c.settable", "c.drive",
                 "restore: n > 0 && restore.checked", "body.version = pick.version", "body.queue = true",
                 "queueFromState(r.queue)", "renderQueue()"):
        assert text in parts, text


def test_scans_follow_the_contract():
    for text in ('op: "import", path', 'op: "rename", id: e.id, name', 'op: "remove", id: e.id', 'op: "run", id: e.id',
                 "body.queue = true", "body.params", "pick_file", ">Your scripts</h3>", ">Kelvin's scans</h3>"):
        assert text in JS + PAGE, text
    entry = _js("scanEntry")
    assert '"missing"' in entry and '"changed"' in entry and '"not run yet"' in entry and '"ran "' in entry
    menu = _js("scanMenu")
    assert all(label in menu for label in ('"Show"', '"Open last run"', '"Rename"', '"Remove"'))
    assert 'id="scans-path"' in PAGE and 'id="scans-check"' in PAGE   # in a browser, the path typed; the check shown


def test_a_queued_script_says_so():
    assert 'script: "script"' in JS and "Q_KIND[i.kind]" in _js("queueItem") and "i.then_pause" in _js("queueItem")
