"""The Runs pane's page: Runs · Scans · Queue in its header, the open runs' tabs in a row below it, the recent runs in
place of the old clock menu, Run again in a finished run's view, and the Scans view of Kelvin's scans and the person's
scripts (GET/POST /api/library, /api/runs/{id}/again)."""
import re

import pytest

pytest.importorskip("starlette")

from labharness.web.app import STATIC  # noqa: E402

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
    """Switching the Runs pane's view never opens or closes another panel; nor does the scan editor, which opens and
    closes on its own."""
    for name in ("showView", "showRunList", "showQueue", "loadRunList", "renderRunList", "loadScans", "renderScans"):
        body = _js(name)
        assert not re.search(r"toggle(Lab|Team|Analysis)\(", body), name
    for name in ("toggleEditor", "openEditor", "renderEditor", "edLoop", "edSave", "edRun", "edRevert", "initEditor",
                 "pickScan", "renderPick", "queuePicked"):
        assert not re.search(r"toggle(Lab|Team|Analysis|Runs)\(", _js(name)), name


def test_the_scan_editor_is_a_side_panel_of_its_own():
    work = PAGE[PAGE.index('<div id="workcol"'):PAGE.index('<div id="sidecol"')]
    assert work.index('<section id="runpane"') < work.index('<section id="edpane"') < work.index('<section id="anpane"')
    pane = PAGE[PAGE.index('<section id="edpane"'):PAGE.index('<section id="anpane"')]
    assert 'id="edpane-resize"' in pane and 'id="close-editor"' in pane and 'aria-labelledby="ed-title"' in pane
    assert re.search(r'edpane: \[\(\) => ED\.open, "workcol", \d+, "lh\.edWidth"\]', JS)
    assert "PANE_W.edpane" in _js("initPanes")
    toggle = _js("toggleEditor")
    assert '$("edpane").hidden = !ED.open' in toggle and "fitLayout()" in toggle
    assert "ED.open" in _js("syncScrim") and "if (ED.open) toggleEditor(false);" in JS   # the scrim closes all, as ever
    opened = _js("openEditor")
    assert 'op: "parts", id: id || null' in opened and "toggleEditor(true)" in opened and "confirm(" in opened
    for label in (">Edit scan<", ">Plan text<", ">+ Loop<", ">Loops<", ">Record<", ">Setup<", ">At the end<",
                  ">As written<", ">Add to queue<", ">Run<", ">Save<", ">Revert<"):
        assert label in pane, label


def test_scans_are_selected_and_added_to_the_queue_in_order():
    """A row is selected by a click (Ctrl adds, Shift a stretch) or the keyboard; the bar adds the selected to the queue
    in the order they were picked (each as its Queue does), runs one, or edits a scan. The rows' own buttons stay."""
    bar = PAGE[PAGE.index('<div id="scans-pick"'):PAGE.index('<div id="scans-check"')]
    for text in ('id="scans-pick-queue"', ">Add to queue<", 'id="scans-pick-run"', ">Run<", 'id="scans-pick-edit"',
                 ">Edit<", 'id="scans-pick-clear"', 'role="toolbar"'):
        assert text in bar, text
    assert PAGE.count('role="grid" aria-multiselectable="true"') == 2 and 'id="scans-new"' in PAGE
    entry = _js("scanEntry")
    assert 'role: "row", "aria-selected": String(at >= 0), tabindex: "-1"' in entry
    assert 'ev.target.closest("button, input, select, textarea, label, a, pre")' in entry   # its own controls
    assert '"range" : ev.ctrlKey || ev.metaKey ? "toggle" : "only"' in entry
    assert 'act("Run",' in entry and 'act("Queue",' in entry and '"lib-order"' in entry
    assert '"edited from " + e.edited_from' in entry and "!e.named" in entry
    keys = _js("scanKeys")
    assert all(k in keys for k in ('" "', '"Enter"', '"ArrowDown"', '"ArrowUp"', '"Escape"'))
    pick = _js("pickScan")
    assert "SCANS.sel.concat(id)" in pick and "SCANS.sel = [id]" in pick


def test_the_scans_list_is_a_grid_with_one_tab_stop_and_a_selection_the_person_sees():
    """A Shift range only grew; rows the filter hid stayed selected (Add to queue queued them unseen); each row was an
    option holding buttons, and a tab stop of its own."""
    entry, keys, pick, render = _js("scanEntry"), _js("scanKeys"), _js("pickScan"), _js("renderScans")
    view = PAGE[PAGE.index('<div id="scans-view"'):PAGE.index('<div id="queue-view"')]
    assert 'role="listbox"' not in view and 'role: "option"' not in entry   # no buttons inside an option
    assert entry.count('role: "gridcell"') == 4 and 'role: "gridcell"' in _js("scanDetail")
    assert entry.count('tabindex: "-1"') == 3                         # the row and its buttons: reached by the keys
    stop = _js("scanStop")
    assert "li.tabIndex = li === stop ? 0 : -1" in stop and "SCANS.at" in stop and "scanStop();" in render
    assert 'li.addEventListener("focusin"' in entry and "SCANS.at = e.id" in entry
    for key in ('"Home"', '"End"', '"ArrowRight"', '"ArrowLeft"', "btns[0].focus()", "btns[k - 1] || li"):
        assert key in keys, key
    assert "a <= b ? ids.slice(a, b + 1) : ids.slice(b, a + 1).reverse()" in pick   # the range alone: it shrinks too
    assert "SCANS.sel.concat(ids.slice" not in pick
    assert "SCANS.sel = SCANS.sel.filter((id) => seen.has(id))" in render   # a row out of sight leaves the selection
    assert render.index("SCANS.sel = SCANS.sel.filter") < render.index("kept.map(scanEntry)")
    queued = _js("queuePicked")
    assert "for (const e of picked)" in queued and 'op: "run", id: e.id, queue: true' in queued
    assert "busyWhile(picked.map(" in queued and "scanParams(e)" in queued   # a script's values, as its Queue sends
    shown = _js("renderPick")
    assert '$("scans-pick-run").hidden = !one' in shown and 'one.kind === "plan"' in shown and "syncBusy()" in shown
    assert 'label: "Edit", icon: "pencil", onClick: () => openEditor(e.id)' in _js("scanMenu")


def test_the_scan_editor_follows_the_contract():
    """Each change is checked a moment later (the latest answer wins); Save keeps a new scan; Run and Add to queue send
    the scan itself while it is unchanged, else its parts (kept, then run through the queue and its approval); what the
    editor doesn't show as fields stays read-only."""
    changed = _js("edChanged")
    assert "setTimeout(edCheck, 350)" in changed and "clearTimeout(ED.timer)" in changed
    check = _js("edCheck")
    assert 'op: "check", parts: ED.parts' in check and "gen !== ED.gen" in check
    # the check asked before is dropped (its fetch aborted; Kelvin runs an editor's newest only), and none is left
    # waiting once the editor closes or opens another scan
    assert "ED.asking.abort()" in check and "new AbortController()" in check and "signal: asking.signal" in check
    assert "editor: ED.key" in check and "c.superseded" in check and "signal: opts.signal" in _js("api")
    assert "if (!ED.open) edStopCheck();" in _js("toggleEditor") and "edStopCheck();" in _js("openEditor")
    stop = _js("edStopCheck")
    assert "clearTimeout(ED.timer)" in stop and "ED.asking.abort()" in stop
    assert 'op: "save", parts: ED.parts, from: ED.id' in _js("edSave")
    run = _js("edRun")
    assert '{ op: "run", parts: ED.parts, from: ED.id } : { op: "run", id: ED.id }' in run
    assert "body.queue = true" in run
    assert 'busyWhile(queue ? ["ed"] : ["ed", "start"]' in run and "showQueue()" in run
    buttons = _js("edButtons")
    assert "!(dirty || !ED.id)" in buttons and 'BUSY.has("start")' in buttons and "!c.loads" in buttons
    assert "if (ED.parts) edButtons();" in _js("syncBusy")   # a Run waiting for its approval holds the editor's too
    loop = _js("edLoop")
    assert 'loop.mode === "kept"' in loop and '"as written"' in loop and 'h("pre", { class: "ed-kept" }' in loop
    assert '"Outer"' in loop and '"Inner"' in loop and '"Remove"' in loop
    fields = _js("edModeFields")
    for text in ('"from"', '"to"', '"step"', '"points"', '"log"', '"and back"', '"settle"', '"+ Leg"',
                 '"measure every"', 'fixed.has("settle")', 'fixed.has("step")'):
        assert text in fields, text
    render = _js("renderEditor")
    assert '"as written below"' in render and '$("ed-kept-sec").hidden = !p.kept' in render
    assert "ED.parts = JSON.parse(ED.base)" in _js("edRevert")
    assert 'copyText($("ed-plan-text").textContent' in _js("initEditor")
    count = _js("rangeCount")   # as plans.py counts a range: its steps, and `to` itself when they miss it
    assert "Math.floor(Math.abs(b - a) / Math.abs(step) + 1e-9)" in count and "1e-12" in count


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
                 "Every setting is as it was.", "Couldn't read ", "c.why", "s.as_ran !== false", "as_ran_why", "c.differs && c.settable", "c.drive",
                 "restore: n > 0 && restore.checked", "body.version = pick.version", "body.queue = true",
                 "queueFromState(r.queue)", "renderQueue()"):
        assert text in parts, text


def test_scans_follow_the_contract():
    for text in ('op: "import", path', 'op: "rename", id: e.id, name', 'op: "remove", id: e.id', 'op: "run", id: e.id',
                 "body.queue = true", "body.params", "pick_file", ">Your scripts</h3>", ">Scans</h3>"):
        assert text in JS + PAGE, text
    entry = _js("scanEntry")
    assert '"missing"' in entry and '"changed"' in entry and '"not run yet"' in entry and '"ran "' in entry
    menu = _js("scanMenu")
    assert all(label in menu for label in ('"Show"', '"Open last run"', '"Rename"', '"Remove"'))
    assert 'id="scans-path"' in PAGE and 'id="scans-check"' in PAGE   # in a browser, the path typed; the check shown


def test_a_queued_script_says_so():
    assert 'script: "script"' in JS and "Q_KIND[i.kind]" in _js("queueItem") and "i.then_pause" in _js("queueItem")


def test_a_finished_runs_plan_shows_without_reading_the_lab():
    """The run's Plan (its plan_text) stays in its view beside Run again: for data imported from another program, and
    when GET /again fails. Run again doesn't show it twice."""
    view, render = _js("buildRunView"), _js("renderRunView")
    assert '"Plan"' in view and view.index("v.plan,") < view.index("v.again,")
    assert "v.plan.hidden = !tab.final.plan_text" in render and 'querySelector("pre").textContent = tab.final.plan_text' in render
    assert "rv-plan" not in _js("againParts") and "d.text" not in _js("againParts")


def test_run_sends_only_the_values_a_person_set():
    """Show's fields reach Run only when the person changed one: then the values a person set (now, or for the last
    run), never an untouched field, and none the file has now (it follows the file)."""
    run = _js("runScan")
    assert "scanParams(e)" in run and "scriptFields(e).map" not in run
    params = _js("scanParams")
    assert "if (!changed.length) return null;" in params and "e.settable" in params and "e.params" in params
    detail = _js("scanDetail")
    assert "SCANS.params.set(e.id, {})" in detail and "delete edited[f.name]" in detail   # put back: no change


def test_a_start_waits_for_its_answer_and_is_sent_once():
    """While a Run or Run again waits for the queue's approval its button stays disabled, however often Scans is
    redrawn, and no other Run starts; a second click does nothing."""
    busy = _js("busyWhile")
    assert "return null" in busy and "BUSY.add" in busy and "BUSY.delete" in busy and "syncBusy()" in busy
    assert "busyWhile(keys" in _js("runScan") and "busyWhile(keys" in _js("againParts")
    entry = _js("scanEntry")
    assert '["start", key]' in entry and '"data-busy": keys.join(" ")' in entry and "BUSY.has(k)" in entry
    assert '"data-busy": "start " + key' in _js("againParts") and "syncBusy();" in _js("loadAgain")


def test_a_long_run_file_says_it_shows_its_end():
    """Script, Output and Commands come as at most their last 256 KB; X-Kelvin-Cut gives the whole size."""
    files = _js("runFiles")
    assert 'res.headers.get("X-Kelvin-Cut")' in files and '"The last "' in files and "cut.hidden = !whole" in files
