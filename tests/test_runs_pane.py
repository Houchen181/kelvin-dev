"""The Runs pane's page: Scan · Sequence in its header. The Scan shows the measurement now (the run in progress, or
the last one as it ended, with Run again) over the scans, scripts and experiments to run now or put in the sequence
(seq.js draws those); the Sequence (seq.js) is the queue's one place, with the step row and Paste a plan under its
blocks (GET/POST /api/library, /api/queue, /api/runs/{id}/again). Earlier runs are the Analysis pane's, and Report…
is in its head. 0.3.39's Schedule (Special Measure's window: the scans beside the queue, ⇒ between them) stays on the
page, built and working, never shown."""
import re

import pytest

pytest.importorskip("starlette")

from labharness.web.app import STATIC  # noqa: E402

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


def _js(name):
    """One function of app.js, by its name."""
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


def test_every_id_the_portal_script_uses_is_on_the_page():
    used = set(re.findall(r'\$\("([\w-]+)"\)', JS))
    made = set(re.findall(r'id="([\w-]+)"', PAGE)) | set(re.findall(r'\bid: "([\w-]+)"', JS))   # or made by app.js
    assert len(used) > 300 and not sorted(used - made)


def test_the_header_is_scan_and_sequence_and_the_scan_shows_the_measurement_now():
    head = PAGE[PAGE.index('<section id="runpane"'):PAGE.index('<section id="edpane"')]
    header = head[head.index("<header"):head.index("</header>")]
    seg = re.search(r'<div id="runs-seg"[^>]*>', header).group(0)
    assert 'role="tablist"' in seg and 'aria-label="Scan and sequence"' in seg
    tabs = re.findall(r'<button id="(open-\w+)"[^>]*role="tab"[^>]*aria-controls="([\w-]+)"', header)
    assert tabs == [("open-scan", "scan-view"), ("open-schedule", "seq-view")]
    scan = re.search(r'<button id="open-scan"[^>]*>(.*?)</button>', header).group(0)
    assert 'class="on"' in scan and 'aria-selected="true"' in scan and ">Scan<" in scan and 'tabindex="-1"' not in scan
    sequence = re.search(r'<button id="open-schedule"[^>]*>(.*?)</button>', header).group(0)
    assert 'class="on"' not in sequence and 'aria-selected="false"' in sequence and ">Sequence<" in sequence
    assert 'tabindex="-1"' in sequence and 'id="queue-count"' in sequence   # the queue's badge, on the tab that shows it
    assert 'id="close-runs"' in header and 'data-pane="runpane"' in header
    assert 'id="open-report"' not in header   # Report… is the Analysis pane's: the runs are there
    report = PAGE[PAGE.index('<section id="anpane"'):PAGE.index('<section id="runpane"')]
    view = report[report.index('<div id="an-view"'):report.index('<div id="nb-view"')]   # its Analysis view's bar
    assert 'id="open-report"' in view[:view.index('<div id="an-body"')]
    for gone in ("open-runs", "open-scans", "open-queue", 'id="runs-view"', "runs-history", "runs-menu", "seqpane",
                 "toggle-seq", "close-seq", "open-history", "history-view", "run-tabs", "runs-list", "runs-filter",
                 "runs-items", "allRunsTab", "showRunList", "loadRunList", "renderRunList", "tabsRow", "← All runs"):
        assert gone not in PAGE + JS, gone
    assert "showRunsMenu" not in JS
    for panel in ("scan-view", "seq-view"):
        assert re.search(r'id="%s"[^>]*role="tabpanel"' % panel, head), panel
    assert not re.search(r'<div id="scan-view"[^>]*hidden>', head) and 'view: "scan"' in JS   # the Scan first
    assert re.search(r'<div id="seq-view"[^>]*hidden>', head)
    scan_view = head[head.index('<div id="scan-view"'):head.index('<div id="seq-view"')]
    assert scan_view.index('id="scan-live"') < scan_view.index('id="scan-fns"')   # the measurement now, over what to run
    assert '<p id="scan-idle" class="scan-idle">Nothing running</p>' in scan_view
    assert '<div id="schedule-view" class="schedule-view" hidden>' in head   # 0.3.39's Schedule: never shown
    keys = _js("runsSegKeys")   # the Lab's choices have their own segKeys
    assert all(k in keys for k in ("ArrowRight", "ArrowLeft", "Home", "End"))
    assert '$("runs-seg").addEventListener("keydown", runsSegKeys);' in JS
    assert "const order = Object.keys(VIEWS)," in keys   # both tabs
    assert 'const VIEWS = { scan: ["open-scan", "scan-view"], schedule: ["open-schedule", "seq-view"] };' in JS
    view = _js("showView")
    assert "aria-selected" in view and "tabIndex" in view and "loadQueue()" in view and "seqSight();" in view
    assert '$("scan-idle").hidden = !!active;' in view and "t.body.hidden = t !== active;" in view
    assert "QUEUE.open" not in view and "loadScans()" not in view   # the hidden Schedule's lists are never drawn
    # one run at a time: its view in the Scan; the runs that ended before it go when it comes
    tab = _js("runTab")
    assert '$("scan-live").append(body)' in tab and 'old.status !== "running") closeRunTab(old.runId)' in tab
    assert 'showView("scan")' in _js("selectRun")
    assert "showView(RUNS.view)" in _js("toggleRuns") and "seqSight();" in _js("toggleRuns")
    assert "ANA.runs.find((r) => r.run_id === ANA.run)" in _js("openReport")   # This run: the Analysis pane's
    assert "run: ANA.run || null" in _js("makeReport")
    assert 'if (store("lh.runsView") === "schedule") RUNS.view = "schedule";' in _js("initRunsPane")


def test_a_run_asked_for_is_the_scans_or_the_analysis_panes():
    """The run the Scan shows (running, or the last to end) opens there; any other in the Analysis pane: a chat's
    Show data, Home's runs, a block's Open run. A finished run's view has × (put away) and no way back to a list."""
    run = _js("openRun")
    assert "if (!RUNS.tabs.has(runId)) { openAnalysis({ run: runId }); return; }" in run and "selectRun(runId);" in run
    assert "openAnalysis({});" in _js("openRunList")
    assert "openRun(card.runId, card.plan);" in _js("runCard")
    view, render = _js("buildRunView"), _js("renderRunView")
    assert "closeRunTab(tab.runId)" in view and "v.stop, v.close)" in view
    assert 'v.close.hidden = running || tab.status === "finishing";' in render
    home = (STATIC / "home.js").read_text(encoding="utf-8")
    assert 'runs: ["anpane", null, () => K.openRunList()],' in home


def test_the_queue_is_the_sequence_and_a_run_shows_on_the_scan():
    """One UI for the queue: the Sequence (seq.js), the Runs pane's second tab; Home's Sequence opens it (toggleSeq).
    What starts a run (Run, Run again, a lab experiment's Run, the editor's Run) opens the pane on the Scan, where it
    shows as it runs, with the queue's answer as its line. The step row and Paste a plan are the Sequence's; what they
    add, and what the queue says, reaches it at once (kelvin:queue). 0.3.39's Schedule stays on the page, never
    shown."""
    show = _js("showQueue")
    assert 'RUNS.view = "schedule";' in show and 'if (RUNS.open) showView("schedule"); else toggleRuns(true);' in show
    assert "seqSight(message);" in show and "fullShow" not in show
    seq = _js("toggleSeq")
    assert "showQueue(message)" in seq and "toggleRuns(false)" in seq
    sight = _js("seqSight")
    assert 'const open = RUNS.open && RUNS.view === "schedule", scan = RUNS.open && RUNS.view === "scan";' in sight
    assert 'emit("seq", { open, opened: open && !RUNS.seqOpen, scan, message: message || "" });' in sight
    assert '$("open-schedule").addEventListener("click", () => showView("schedule"));' in JS
    assert '$("open-scan").addEventListener("click", () => showView("scan"));' in JS
    scan = _js("showScan")
    assert 'RUNS.view = "scan";' in scan and 'if (RUNS.open) showView("scan"); else toggleRuns(true);' in scan
    assert "showQueue();" not in JS and "showScan();" in _js("paneShow")   # every Run says what the queue said
    assert len(re.findall(r'showScan\(\(r\.message \|\| ""\)\.split\("\\n"\)\[0\]\)', JS)) == 4
    assert not re.findall(r'showQueue\(\(r\.message', JS)
    assert "#open-schedule { display: none; }" not in CSS
    assert "#toggle-runs .seq-badge:not([hidden]) + .live-dot { display: none; }" in CSS   # the count stands for the dot
    assert "initSchedule();" in _js("initRunsPane")   # the step row and Paste a plan are its code
    assert 'emit("queue", { queue: r });' in _js("stepGo")
    op = _js("queueOp")
    assert 'emit("queue", { queue: r, message: quiet ? "" : r.message || "" });' in op
    assert 'emit("queue", { message: e.message });' in op
    seqjs = (STATIC / "seq.js").read_text(encoding="utf-8")
    assert 'document.addEventListener("kelvin:queue"' in seqjs and 'K.$("seq-view")' in seqjs
    assert 'K.$("step-row")' in seqjs and 'K.$("queue-paste")' in seqjs and 'pill = K.$("toggle-runs")' in seqjs
    assert '"Built-in"' not in seqjs and "WAIT_TEXT" not in seqjs   # a wait is the step row's


def test_the_side_panels_stay_independent():
    """Switching the Runs pane's view never opens or closes another panel; nor does the scan editor, which opens and
    closes on its own."""
    for name in ("showView", "showQueue", "showScan", "loadScans", "renderScans", "toggleRuns", "renderQueue",
                 "noticeRun"):
        body = _js(name)
        assert not re.search(r"toggle(Lab|Team|Analysis)\(", body), name
    for name in ("toggleEditor", "openEditor", "renderEditor", "edTitle", "edLoop", "edSave", "edRun", "edRevert",
                 "initEditor", "pickScan", "renderPick", "queuePicked", "editScan", "editQueued", "editStep", "stepGo",
                 "pasteOpen", "queueKeys", "queueDrops", "initSchedule"):
        assert not re.search(r"toggle(Lab|Team|Analysis|Runs)\(", _js(name)), name


def test_the_scan_editor_is_a_side_panel_of_its_own():
    """It has no button of its own: it stands right after Runs, in Runs' column (tests/test_panel_layout.py)."""
    work = PAGE[PAGE.index('<div id="workcol"'):PAGE.index('<div id="sidecol"')]
    assert work.index('<section id="anpane"') < work.index('<section id="runpane"') < work.index('<section id="edpane"')
    pane = PAGE[PAGE.index('<section id="edpane"'):PAGE.index('<div id="sidecol"')]
    assert 'id="edpane-resize"' in pane and 'id="close-editor"' in pane and 'aria-labelledby="ed-title"' in pane
    assert re.search(r'edpane: \[\(\) => ED\.open, "workcol", \d+, "lh\.edWidth"(, \d+)?\]', JS)   # its touch least
    assert "PANE_W.edpane" in _js("initPanes")
    toggle = _js("toggleEditor")
    assert '$("edpane").hidden = !ED.open' in toggle and "fitLayout()" in toggle
    assert "edpane: toggleEditor," in JS and "if (ED.open) toggleEditor(false);" not in JS   # the scrim closes no panel
    opened = _js("openEditor")
    assert 'op: "parts", id: id || null' in opened and "await edLeave()" in opened
    # to the screen like every opener that is not its own button: there already, its head rings (the click model)
    assert 'Kelvin.paneShow("edpane")' in opened and "toggleEditor(true)" not in opened
    # with changes, × and another scan ask in the page: Save, Discard or Keep editing (never confirm())
    leave = _js("edLeave")
    assert "Kelvin.askYes(" in leave and '{ ok: "Save", alt: "Discard"' in leave and 'cancel: "Keep editing"' in leave
    assert "await edSave(false);" in leave and "return !edDirty();" in leave and "confirm(" not in leave
    assert "edDirty() && !(await edLeave())" in _js("closeEditor") and '"click", closeEditor' in _js("initEditor")
    for label in (">Edit scan<", ">Plan text<", ">Loops<", ">Setup<", ">At the end<", ">As written<",
                  ">Add to sequence<", ">Run<", ">Save<", ">Save as scan<", ">Revert<"):
        assert label in pane, label
    # how many loops, a stepper: + one more outside the others, − the outermost goes (each card has its ✕ too)
    assert 'id="ed-fewer"' in pane and '<output id="ed-n"' in pane and 'id="ed-add-loop"' in pane
    assert "ED.parts.loops.unshift(blankLoop());" in _js("initEditor") and "p.loops.shift()" in _js("initEditor")
    for label in ('"Change"', '"Record"', '"+ Change another"', '"+ Record"'):   # each loop's card says these
        assert label in JS, label
    assert re.search(r'<button id="ed-save-as"[^>]* hidden>', pane)   # only for a scan waiting in the queue


def test_scans_are_selected_and_added_to_the_queue_in_order():
    """A row is selected by a click (Ctrl adds, Shift a stretch) or the keyboard; ⇒ adds the selected to the queue in
    the order they were picked (each as its Queue does), Edit (Enter) edits a scan or shows a script's settings. The
    rows' own buttons stay; the bar that did this above the list is gone."""
    top = PAGE[PAGE.index('<div class="scans-top">'):PAGE.index('<form id="scans-path"')]
    for text in ('id="scans-new"', ">New scan<", 'id="scans-import"', ">Import a script or VI…<", 'id="scans-filter"',
                 'id="scans-edit"', ">Edit<"):
        assert text in top, text
    assert "scans-pick" not in PAGE + JS + CSS and PAGE.count('role="grid" aria-multiselectable="true"') == 2
    entry = _js("scanEntry")
    assert 'role: "row", "aria-selected": String(at >= 0), tabindex: "-1"' in entry
    assert 'ev.target.closest("button, input, select, textarea, label, a, pre")' in entry   # its own controls
    assert '"range" : ev.ctrlKey || ev.metaKey ? "toggle" : "only"' in entry
    assert 'act("Run",' in entry and 'act("Queue",' in entry and '"lib-order"' in entry
    assert '"edited from " + e.edited_from' in entry and "!e.named" in entry
    keys = _js("scanKeys")
    assert all(k in keys for k in ('" "', '"Enter"', '"ArrowDown"', '"ArrowUp"', '"Escape"'))
    assert 'else if (ev.key === "Enter") editScan(e);' in keys and 'ev.key === "Enter" && ev.shiftKey' in keys
    edit = _js("editScan")   # a scan in the editor; a script: its settings, in its row
    assert "openEditor(e.id)" in edit and "SCANS.shown.add(e.id)" in edit and ".lib-params input" in edit
    assert "if (picked.length === 1) editScan(picked[0])" in JS
    pick = _js("pickScan")
    assert "SCANS.sel.concat(id)" in pick and "SCANS.sel = [id]" in pick
    assert 'SCAN_ROWS = "#scans-view li.lib[data-id]"' in JS   # a lab experiment's row is never selected
    for name in ("scanStop", "pickScan", "scanKeys"):
        assert "SCAN_ROWS" in _js(name) and '"#scans-view li.lib"' not in _js(name), name


def test_the_scans_list_is_a_grid_with_one_tab_stop_and_a_selection_the_person_sees():
    """A Shift range only grew; rows the filter hid stayed selected (Add to queue queued them unseen); each row was an
    option holding buttons, and a tab stop of its own."""
    entry, keys, pick, render = _js("scanEntry"), _js("scanKeys"), _js("pickScan"), _js("renderScans")
    view = PAGE[PAGE.index('<section id="scans-view"'):PAGE.index('<section id="queue-view"')]
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
    assert "SCANS.sel = pickedScans().map((e) => e.id)" in _js("renderPick") and "syncBusy()" in _js("renderPick")
    shown = _js("schedButtons")   # ⇒ for what is selected, never while it is on its way; Edit for one
    assert '$("sched-add").disabled = !picked.length' in shown and "BUSY.has(" in shown
    assert '$("scans-edit").disabled = picked.length !== 1' in shown and "schedButtons();" in _js("syncBusy")
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
    save = _js("edSave")
    assert 'op: "save", parts: ED.parts, from }' in save and "const from = ED.item ? ED.lib : ED.id;" in save
    run = _js("edRun")
    assert '{ op: "run", parts: ED.parts, from: ED.id } : { op: "run", id: ED.id }' in run
    assert "body.queue = true" in run
    assert 'busyWhile(queue ? ["ed"] : ["ed", "start"]' in run
    assert r'showScan((r.message || "").split("\n")[0])' in run   # the Scan shows it run, with what the queue said
    buttons = _js("edButtons")
    assert "!(ED.item ? dirty : dirty || !ED.id)" in buttons and 'BUSY.has("start")' in buttons and "!c.loads" in buttons
    assert "if (ED.parts) edButtons();" in _js("syncBusy")   # a Run waiting for its approval holds the editor's too
    loop = _js("edLoop")
    assert 'loop.mode === "kept"' in loop and '"as written"' in loop and 'h("pre", { class: "ed-kept" }' in loop
    assert '"Outer"' in loop and '"Inner"' in loop and '"Remove"' in loop
    fields = _js("edModeFields")
    for text in ('"from"', '"to"', '"step"', '"points"', '"+ Leg"', '"measure every"', 'fixed.has("step")'):
        assert text in fields, text
    waits = _js("edWaits")
    for text in ('"Wait"', '"Start wait"', '"and back"', '"log"', 'fixed.has("settle")'):
        assert text in waits, text
    render = _js("renderEditor")
    assert '"as written below"' in render and '$("ed-kept-sec").hidden = !p.kept' in render
    assert "ED.parts = JSON.parse(ED.base)" in _js("edRevert")
    assert 'copyText($("ed-plan-text").textContent' in _js("initEditor")
    count = _js("rangeCount")   # as plans.py counts a range: its steps, and `to` itself when they miss it
    assert "Math.floor(Math.abs(b - a) / Math.abs(step) + 1e-9)" in count and "1e-12" in count
    # the footer's estimate: each bit whole ("~5.8 min" never split), so a narrow footer breaks only after the dot
    assert r'$("ed-status").textContent = bits.map((b) => b.replace(/ /g, "\u00a0")).join(" · ");' in _js("renderEdCheck")


def test_run_again_follows_the_contract():
    view = _js("buildRunView")
    assert '"Run again"' in view and "loadAgain(tab)" in view and "Add to sequence" not in view   # its own now
    load = _js("loadAgain")
    assert '"/api/runs/" + encodeURIComponent(tab.runId) + "/again"' in load
    parts = _js("againParts")
    for text in ('"Run again"', '"Add to sequence"', '"As it ran then"', '"As it is now"', "Restore these ",
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


# ------------------------------------------------------------------ the Schedule (Special Measure's window)

def test_the_schedule_stands_the_scans_beside_the_queue_with_an_arrow_between():
    """The scans and the queue side by side when the pane has room (and in full screen), else the scans above the
    queue; ⇒ between them (⇓ when one is above the other). Never shown now; the step row and Paste a plan, once under
    its scans, are the Sequence's."""
    view = PAGE[PAGE.index('<div id="schedule-view"'):PAGE.index('<section id="edpane"')]
    assert 'id="step-row"' not in view and 'id="queue-paste-open"' not in view
    order = ['<section id="scans-view"', 'class="sched-mid"', '<button id="sched-add"', '<section id="queue-view"']
    assert [view.index(t) for t in order] == sorted(view.index(t) for t in order), order
    arrow = re.search(r'<button id="sched-add"[^>]*>(.*?)</button>', view).group(0)
    assert '<span class="to-side">⇒</span><span class="to-below">⇓</span>' in arrow and " disabled" in arrow
    assert 'aria-label="Add the selected to the queue"' in arrow
    rules = CSS[CSS.index(".schedule-view {"):]
    assert rules.startswith(".schedule-view { container-type: inline-size;")   # the pane's width decides, not the window's
    assert ".sched-arrow .to-side { display: none; }" in rules                # one above the other: ⇓
    wide = rules[rules.index("@container (min-width: 700px)"):]
    wide = wide[:wide.index("\n}\n")]
    for text in ("grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)", ".sched-arrow .to-side { display: inline; }",
                 ".sched-arrow .to-below { display: none; }"):
        assert text in wide, text
    assert ".sched-list { flex: 1 1 auto;" in CSS and ".queue-view > .q-list { flex: 1 1 auto;" in CSS   # each scrolls
    assert "#open-schedule.live .q-badge" in CSS and "#open-queue" not in CSS
    for gone in ("queue-onerror", "queue-clear", "queue-saved", "queue-load", "queue-note", "queue-pos", "buildQueueView",
                 "QUEUE.built", "queueEditing"):
        assert gone not in PAGE + JS, gone


def test_the_queue_column_is_special_measures_queue():
    """Start and Pause prominent (Special Measure's RUN and PAUSE); Edit, ↑ ↓ ✕ above the list for the selected item;
    the list a grid with one tab stop, an item selected by a click (again: none) or the arrows, with u, d, Delete and
    Enter; Again and Open on an item; what a failure does, Clear finished, Save as… and Load in a small menu."""
    col = PAGE[PAGE.index('<section id="queue-view"'):PAGE.index('<section id="edpane"')]
    assert re.search(r'<button id="queue-start" class="btn primary q-big"[^>]*>Start</button>', col)
    assert re.search(r'<button id="queue-pause" class="btn q-big"[^>]*>Pause</button>', col)
    for text in ('id="queue-edit"', ">Edit<", 'id="queue-up"', 'id="queue-down"', 'id="queue-remove"', 'id="queue-more"',
                 'aria-haspopup="menu"', '<ul id="queue-list" class="q-list" role="grid"', 'id="queue-state"',
                 'id="queue-reason"', 'id="queue-msg"'):
        assert text in col, text
    item = _js("queueItem")
    assert 'role: "row", "aria-selected": String(on), tabindex: "-1"' in item and 'draggable: "true"' in item
    assert 'act("Again",' in item and 'act("Open",' in item and 'act("Edit"' not in item and 'act("✕"' not in item
    assert "pickItem(on ? null : i.id)" in item and 'e.target.closest("button")' in item
    keys = _js("queueKeys")
    for text in ('e.key === "u" && n > 1', 'e.key === "d" && n < count', '"Delete"', 'i.status !== "running") removeItem(i)',
                 'e.key === "Enter") editQueued(i)', '"ArrowDown"', '"ArrowUp"', '"Home"', '"End"', 'e.key === " "',
                 '"Escape" && QUEUE.sel'):
        assert text in keys, text
    render = _js("renderQueue")   # the focused item keeps the keyboard wherever it moved (u, d), never its old place
    assert "rows.find((li) => li.dataset.id === focused.dataset.id)" in render and "stop.tabIndex = 0" in render
    assert "if (QUEUE.sel && !items.some((i) => i.id === QUEUE.sel)) QUEUE.sel = null" in render
    buttons = _js("queueButtons")
    for text in ('$("queue-start").disabled = q.state === "running" || q.state === "pausing" || !q.waiting || BUSY.has("start")',
                 '$("queue-pause").disabled = q.state !== "running"', '!i || i.status === "running" || !!i.restores',
                 '$("queue-remove").disabled = !i || i.status === "running"'):
        assert text in buttons, text
    init = _js("initSchedule")
    assert 'busyWhile(["start"], () => {' in init and 'queueOp({ op: "start" })' in init   # one start at a time
    assert "queueButtons();" in _js("syncBusy")
    menu = _js("queueMenu")
    for text in ('heading: "On a failure"', 'label: "Pause"', 'label: "Go on"', 'op: "on_error", value: "pause"',
                 'op: "on_error", value: "continue"', 'label: "Clear finished"', 'op: "clear"', 'label: "Save as…"',
                 'op: "save"', 'heading: "Load"', 'op: "load", name: s'):
        assert text in menu, text
    remove = _js("removeItem")   # the item after it is selected in its place, so Delete goes on down the list
    assert 'op: "remove", item: i.id' in remove and "items[k + 1] || items[k - 1]" in remove


def test_the_arrow_and_a_drop_put_scans_where_the_person_put_them():
    """⇒: the selected scans and scripts after the selected queue item (last when none is), in the order selected, the
    selection moving to the last one put there; a drop: where it is dropped; a queue item dragged: moved there."""
    assert "return n >= 0 ? n + 2 : null;" in _js("insertAt")   # after item n (1 = first): place n + 1
    assert '$("sched-add").addEventListener("click", () => queuePicked(insertAt()));' in JS
    queued = _js("queuePicked")
    assert "if (at) body.position = at + names.length;" in queued   # in the order selected, each after the one before
    assert "if (at && !ids) followInsert(r.queue, at + names.length - 1);" in queued
    assert "if (it && QUEUE.sel) QUEUE.sel = it.id;" in _js("followInsert")   # with none selected, the next goes last too
    drops = _js("queueDrops")
    assert "queuePicked(k + 1, ids)" in drops and "moveTo(item, k)" in drops and "e.preventDefault()" in drops
    assert "y < r.top + r.height / 2" in _js("dropIndex")
    assert "const position = (k > from ? k - 1 : k) + 1;" in _js("moveTo")   # before row k, counted without it
    drag = _js("dragScan")   # a row with the rest of the selection, in the order picked; never from its fields
    assert "SCANS.sel.includes(e.id) ? SCANS.sel : [e.id]" in drag and '"input, textarea, select, pre, .lib-detail"' in drag
    assert 'DRAG_SCANS = "text/x-kelvin-scans", DRAG_ITEM = "text/x-kelvin-item"' in JS
    assert "if (QUEUE.dragging) return;" in _js("renderQueue")   # nothing redrawn under the pointer until the drop
    for name in ("queueAddOrSave", "stepGo"):   # a step or a pasted item goes there too
        assert "position: at" in _js(name) and "followInsert(r, at)" in _js(name), name


# what the step row writes (stepText) for: Set tc.setpoint 5 (in K), Set smu.output on, Wait 1.5 h, and Wait until
# tc.T_sample settles within 0.05 for 60 s (no timeout given)
ROW_STEPS = ["step: set tc.setpoint to 5 K\nset: { tc.setpoint: 5 }",
             "step: set smu.output to on\nset: { smu.output: on }",
             "step: wait 1.5 h\nwait: 5400",
             "step: wait until tc.T_sample settles\nstabilize: { read: tc.T_sample, within: 0.05, for: 60 }"]


def test_the_step_row_writes_the_queues_own_step_format_and_kelvin_checks_it(tmp_path):
    """Special Measure's command box, typed: Set, Wait or Wait until settled, written as scanqueue.py's step (as
    QUEUE_STEP: its name, then set, wait or stabilize, as flow mappings), never a format of its own; Kelvin checks each
    as it is added, its limits too. The row's settable keys are the scan editor's (op parts)."""
    row = PAGE[PAGE.index('<div id="step-row"'):PAGE.index('<div class="step-foot">')]
    assert re.search(r'<select id="step-kind"[^>]*><option value="set">Set</option><option value="wait">Wait</option>'
                     r'<option value="settle">Wait until settled</option></select>', row)
    for text in ('id="step-key"', 'list="step-set-keys"', 'id="step-value"', 'id="step-time"', 'id="step-time-unit"',
                 'id="step-read"', 'list="step-read-keys"', 'id="step-within"', 'id="step-for"', 'id="step-timeout"',
                 '<button id="step-add" class="btn small primary"', ">Add</button>", 'id="step-save"', 'id="step-cancel"'):
        assert text in row, text
    assert PAGE.index('<div id="seq-view"') < PAGE.index('<div id="step-row"') < PAGE.index('<div id="schedule-view"')
    assert '<button id="queue-paste-open" class="btn small"' in PAGE and ">Paste a plan</button>" in PAGE   # a button that looks like one
    assert re.search(r'<form id="queue-paste" class="q-paste" hidden>', PAGE) and 'id="queue-text"' in PAGE
    assert (r'const QUEUE_STEP = "step: cool to 2 K\nset: { tc.T_set: 2 }\nstabilize: { read: tc.T, within: 0.02, '
            r'for: 120, timeout: 7200 }\nwait: 600";') in JS
    text = _js("stepText")
    for part in (r'return "step: " + yamlName(said) + "\nset: { " + yamlWord(key) + ": " + yamlWord(value) + " }";',
                 r'+ "\nwait: " + String(Number((Number(t) * Number(unit.value)).toPrecision(12)));',
                 r'return "step: " + yamlName("wait until " + read + " settles") + "\nstabilize: { " + keys.join(", ") + " }";',
                 '["read: " + yamlWord(read), "within: " + within]', '[["for", v("step-for")], ["timeout", v("step-timeout")]]'):
        assert part in text, part
    assert r'const yamlWord = (s) => (/^[\w.+-]+$/.test(s) ? s : JSON.stringify(s));' in JS   # else a quoted string
    assert 'api("/api/library", { body: { op: "parts", id: null } })' in _js("labKeys")   # the editor's own choices
    assert "!p.action" in _js("labKeys")   # a card's action is no value to set
    pytest.importorskip("mcp")
    from labharness import ScriptedApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.scanqueue import load_step
    from labharness.yamlio import load_yaml
    from simlab import build_virtual_lab
    steps = [load_step(t) for t in ROW_STEPS]
    assert [s.name for s in steps] == ["set tc.setpoint to 5 K", "set smu.output to on", "wait 1.5 h",
                                       "wait until tc.T_sample settles"]
    assert steps[0].sets == [("tc", "setpoint", 5)] and steps[1].sets == [("smu", "output", True)]
    assert steps[2].wait == 5400 and not steps[2].sets and steps[3].stabilize.key == "tc.T_sample"
    assert (steps[3].stabilize.within, steps[3].stabilize.duration, steps[3].stabilize.timeout) == (0.05, 60, 900)
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data", None)
    for t in ROW_STEPS:
        assert rt.queue_add(t, by="human").startswith("added step"), t
    assert rt.queue_add("step: set smu.V to 99 V\nset: { smu.V: 99 }", by="human").startswith(
        "not added: smu.V 99 V is outside the limits")
    # what the row quotes (a value or a name with more than a word in it) reads back as it was typed
    assert load_yaml('step: "set smu.V to 1, 2"\nset: { smu.V: "1, 2" }') == {"step": "set smu.V to 1, 2",
                                                                            "set": {"smu.V": "1, 2"}}


def test_edit_on_a_queued_item_opens_it_where_it_can_be_edited():
    """A scan in the scan editor, bound to its item ("Queue item #n"; Save puts it back, Save as scan keeps it in the
    library instead, with no Run or Add to queue); a step the row can show in the step row (Save to #n); anything else
    as written. A running item is fixed, as is a step that restores another's settings."""
    edit = _js("editQueued")
    for text in ('if (i.status === "running" || i.restores) return;', 'if (i.kind === "plan") openEditor(null, i.id);',
                 'else if (i.kind === "step") editStep(i);', "else pasteOpen(true, i);"):
        assert text in edit, text
    step = _js("editStep")
    assert 'op: "fields", item: i.id' in step and "if (!f) { pasteOpen(true, i); return; }" in step
    assert '"Save to #"' in step and "STEP.editing = i.id" in step
    fields = _js("stepFields")   # one set, a wait or a settle the row shows whole; else null: as written
    assert "keys.length !== 1" in fields and '["read", "within", "for", "timeout"]' in fields
    assert 'op: "edit", item: STEP.editing, text' in _js("stepGo")
    opened = _js("openEditor")
    assert "ED.item === (item || null)" in opened and "item ? { item } : {}" in opened and "ED.lib = d.library" in opened
    title = _js("edTitle")
    assert '"Queue item " + (n ? "#" + n : "(removed)")' in title and '$("ed-save-as").hidden = !ED.item' in title
    assert '$("ed-queue").hidden = $("ed-run").hidden = !!ED.item' in title and "if (ED.item) edTitle();" in JS
    save = _js("edSave")
    assert 'op: "text", parts: ED.parts' in save and 'op: "edit", item: ED.item, text' in save
    assert "if (ED.item && !asScan)" in save
    assert '$("ed-save-as").addEventListener("click", () => edSave(true));' in JS
    assert "ED.item = ED.lib = null;" in _js("edSaved")   # kept as a scan: the editor edits that scan now
    assert "dirty : dirty || !ED.id" in _js("edButtons")   # an item saved unchanged would run again


def test_a_run_that_starts_while_the_sequence_is_shown_waits_in_the_scan():
    """A run that starts takes the pane to it (the Scan), unless the pane shows the Sequence or Runs would not fit
    beside the panes open: with the Sequence shown the Sequence stays (its blocks show their runs), and Scan's dot says
    a run is running. A run Kelvin ran that ended before the pane saw it follows the same rule (showNewRun)."""
    notice, new = _js("noticeRun"), _js("showNewRun")
    assert 'setTabStatus(runTab(runId, plan), "running");' in notice and "showNewRun(runId, plan);" in notice
    assert "if (scheduling()) return;" in new
    assert new.index("if (scheduling()) return;") < new.index("openRun(runId, plan);")
    assert 'return RUNS.open && RUNS.view === "schedule";' in _js("scheduling")
    # the Scan has it all the same (shown when Scan is opened), and a run that ended before it goes once it is over
    assert new.index("RUNS.active = runId;") < new.index("if (scheduling()) return;")
    assert '[...RUNS.tabs.values()].find((t) => t.status === "running") || null;' in _js("showView")
    assert 'find((t) => t.status === "running" || t.status === "finishing")' in _js("closeRunTab")   # never an old one
    assert "if (ended && RUNS.active !== tab.runId && RUNS.tabs.has(RUNS.active)) closeRunTab(tab.runId);" in _js("loadFinal")
    dots = _js("liveDots")
    assert '$("runs-dot").hidden = !live;' in dots and '$("open-scan").classList.toggle("live", live);' in dots
    assert "#open-scan.live::after" in CSS and "#open-scan.live.on::after { display: none; }" in CSS


def test_a_scripts_settings_in_the_scan_go_as_typed():
    """A script's settings show exactly as its file (or its last run) has them, never rounded; ▶ and ＋ send the
    values typed for the settings it still has (and those it last ran with), none when nothing was typed for one."""
    seqjs = (STATIC / "seq.js").read_text(encoding="utf-8")
    inputs = seqjs[seqjs.index("function fnInputs("):seqjs.index("function scriptFields(")]
    assert 'typeof x.value === "string" ? x.value : JSON.stringify(x.value)' in inputs and "shown(x.value)" not in inputs
    params = seqjs[seqjs.index("function fnParams("):seqjs.index("function yq(")]
    assert "if (!fields.some((x) => x.name in typed)) return null;" in params


def test_the_lab_panels_rename_takes_one_click():
    """Rename and Cancel keep the focus in the name's box (nothing is redrawn under the pointer between press and
    release); the panel draws again at its next poll once the box lets go."""
    tools = _js("instTools")
    assert 'b.addEventListener("mousedown", (e) => e.preventDefault())' in tools and '"blur"' not in tools
    assert 'PANEL.drawLater = !!had && had.startsWith("rename-field ")' in _js("renderInstruments")



def test_key_results_wait_for_a_width_and_nothing_calls_a_function_that_is_gone():
    """A run that finished out of sight has its key results judged when its view first has a width; the Analysis
    picker keeps main's words for a run's time, and Plan text opens with edEase; the report and Revert are outlined
    buttons."""
    keys = _js("keysOnce")
    assert keys.index("if (!w) return;") < keys.index("v.keysPending = false;")
    assert "getBoundingClientRect().width / layoutZoom()" in keys   # the width in the page's pixels
    # judged when it is shown, in full screen, or when the Scan is shown again over the Sequence: then after the
    # layout that shows it (toggleRuns tells what is in sight before it lays the panes out)
    assert "const keysNow = () => keysOnce(RUNS.tabs.get(RUNS.active));" in JS
    assert 'document.addEventListener("kelvin:layout", keysNow);' in JS
    assert 'document.addEventListener("kelvin:seq", () => queueMicrotask(keysNow));' in JS
    toggle = _js("toggleRuns")
    assert toggle.index("showView(RUNS.view)") < toggle.index("fitLayout();")
    assert "startedText" not in JS or "function startedText" in JS   # no call to a function that is gone
    assert "function edEase(el) {" in JS   # Plan text's opening, which edPlan calls
    for bid in ("open-report", "ed-revert"):   # buttons look like buttons: outlined, not ghosts
        assert re.search(r'<button id="%s" class="btn small" ' % bid, PAGE), bid


def test_a_finished_runs_plot_keeps_a_landscape_shape_and_its_analysis_takes_the_height_left():
    """Each plot is never taller than 3:4 of its width, however many there are: each is a container, its drawing 3/4 of
    its own width less 48 px (its head, legend and padding take up to 64), standing at the top of its row; a shorter
    row takes it whole (max-height 100 %) and its drawing gives way down to 120, so a small screen keeps what it has.
    The height a plot leaves goes to the key results and the whole assessment, which open by themselves where there is room,
    judged once (keysOnce): beside the plots the key results (14 rows or fewer, a window not short) and the whole
    assessment when the side then holds it; above the plots both, else the key results alone, when the plots keep 2/5
    of their width in height and stay above their least; else neither. On a 2560 x 1440 monitor Runs opens 960 wide,
    so its plot, key results and Next show without a press."""
    plot = re.search(r"\n\.rv-charts \.plot\.fill \{([^}]*)\}", CSS).group(1)
    assert "container-type: inline-size;" in plot and "align-self: start;" in plot and "max-height: 100%;" in plot
    drawing = re.search(r"\n\.rv-charts \.plot\.fill > \.plot-svg \{([^}]*)\}", CSS).group(1)
    assert "flex: 0 1 auto;" in drawing and "height: max(120px, calc(75cqi - 48px));" in drawing
    assert "min-height: 120px;" in drawing
    assert ":only-child" not in CSS[CSS.index("/* ===== fit E"):]   # one rule for one plot or many
    assert ".rv-charts > .plots { flex: 1 1 0; min-height: 0; grid-auto-rows: minmax(160px, 1fr); }" in CSS
    assert ".plots { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }" in CSS
    assert ".rv-charts { flex: 1 1 0; min-height: 160px;" in CSS

    def card(width, row):   # a plot's height: its drawing (in its content box) and up to 64 px, never over its row
        drawing = max(120, 0.75 * (width - 26) - 48)
        return min(row, drawing + 64)
    # at 2560 x 1440, Runs 960: one plot beside the analysis (its column 600 to 620 px)
    w = 959 - 24
    col = w - 8 - min(max(280, 0.34 * w), 420)
    assert 600 < col < 620 and card(col, 1000) <= 0.75 * col
    # Runs kept at 720, a run of two values: two plots of 342 side by side, each landscape (it was 342 x 540)
    two = (695 - 10) / 2
    assert two >= 300 and card(two, 540) <= 0.75 * two and card(two, 540) < two
    # a small screen: a row shorter than 3:4 is taken whole, as before
    assert card(560, 200) == 200
    keys = _js("keysOnce")
    beside = keys[keys.index("if (w >= 900) {"):keys.index("const least = ")]
    assert 'if (document.documentElement.classList.contains("fit-tight")) return;' in beside
    assert "open(true, true);" in beside and "if (v.side.scrollHeight > v.side.clientHeight + 1) v.whole(false);" in beside
    assert "const keys = v.keysRows > 0 && v.keysRows <= 14;" in keys
    assert "return c.height >= c.width * 0.4 && c.height / z > least + 1;" in keys
    assert "const least = parseFloat(getComputedStyle(v.charts).minHeight) || 0;" in keys
    tries = keys[keys.index("for (const all of [true, false]) {"):]
    assert tries.index("open(true, all);") < tries.index("if (room()) return;") < tries.index("open(false, false);")
    assert "rvOpen(v.keys, k, null, true)" in keys   # opened still: no easing, nothing moves
    # judged by the view drawn whole: the plots and the files first
    final = JS[JS.index("if (tab.final && !v.finalDrawn) {"):]
    final = final[:final.index("return;")]
    assert final.index("v.charts.replaceChildren(runCharts(tab.final));") < final.index("keysOnce(tab);")
    assert "if (a) { v.keysPending = true; v.keysRows = a.table ? a.rows : 0; v.whole = a.whole; }" in final
    # the whole assessment without a press only when there is more than its two lines
    block = _js("analysisBlock")
    assert "if (on && !more2 && text.scrollHeight <= text.clientHeight + 1) return;" in block
    assert "return { box, table, rows: rows.length, whole };" in block


def test_save_as_offers_the_sequences_name_as_a_file_name_may_hold_it():
    """Save as… suggests the sequence's name, what a file name can't hold made a space: "Gate scan, cold" is offered
    as "Gate scan cold", never "Gate scan- cold". The page's own replacements are applied here, in order."""
    seq = (STATIC / "seq.js").read_text(encoding="utf-8")
    save = seq[seq.index("function openSave() {"):]
    line = save[save.index("const fit = (s) =>"):save.index(".slice(0, 61).trim();")]
    steps = re.findall(r'\.replace\(/((?:[^/\\]|\\.)+)/(g?), "([^"]*)"\)', line)
    assert len(steps) == 3

    def fit(s):
        for pattern, every, by in steps:
            s = re.sub(pattern, by, s, count=0 if every else 1)
        return s[:61].strip()
    assert fit("Gate scan, cold") == "Gate scan cold" and fit("  ,Hello/world  ") == "Hello world"
