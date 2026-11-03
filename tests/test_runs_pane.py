"""The Runs pane's page: Schedule · History in its header, the open runs' tabs in a row below it; the Schedule as
Special Measure's window: Kelvin's scans and the person's scripts beside the queue (below it when narrow), ⇒ between
them, the step row under the scans; the recent runs and Run again in the History (GET/POST /api/library, /api/queue,
/api/runs/{id}/again)."""
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


def test_the_header_is_schedule_and_history_and_the_open_runs_have_a_row_below_it():
    head = PAGE[PAGE.index('<section id="runpane"'):PAGE.index('<section id="anpane"')]
    header = head[head.index("<header"):head.index("</header>")]
    seg = re.search(r'<div id="runs-seg"[^>]*>', header).group(0)
    assert 'role="tablist"' in seg
    tabs = re.findall(r'<button id="(open-\w+)"[^>]*role="tab"[^>]*aria-controls="([\w-]+)"', header)
    assert tabs == [("open-schedule", "schedule-view"), ("open-history", "history-view")]
    schedule = re.search(r'<button id="open-schedule"[^>]*>(.*?)</button>', header).group(0)
    assert 'class="on"' in schedule and 'aria-selected="true"' in schedule and ">Schedule<" in schedule
    assert 'id="queue-count"' in schedule   # the queue's badge, on the tab that shows it
    assert ">History</button>" in header and 'tabindex="-1"' in header.split('id="open-history"')[1].split(">")[0]
    assert 'id="open-report"' in header and 'id="close-runs"' in header and 'data-pane="runpane"' in header
    for gone in ("open-runs", "open-scans", "open-queue", 'id="runs-view"', "runs-history", "runs-menu"):
        assert gone not in PAGE + JS, gone
    assert "showRunsMenu" not in JS
    row = re.search(r'<div id="run-tabs"[^>]*>', head).group(0)
    assert head.index(row) > head.index("</header>") and " hidden" in row   # shown while runs are open
    for panel in ("schedule-view", "history-view"):
        assert re.search(r'id="%s"[^>]*role="tabpanel"' % panel, head), panel
    assert re.search(r'<div id="history-view"[^>]*hidden>', head) and 'view: "schedule"' in JS   # the Schedule first
    keys = _js("segKeys")
    assert all(k in keys for k in ("ArrowRight", "ArrowLeft", "Home", "End"))
    view = _js("showView")
    assert "aria-selected" in view and "tabIndex" in view and "loadRunList()" in view and "loadScans()" in view
    assert "loadQueue()" in view and 'QUEUE.open = view === "schedule"' in view
    assert '$("history-view").append(body)' in _js("runTab") and 'showView("history")' in _js("selectRun")
    assert "showView(RUNS.view)" in _js("toggleRuns")   # opened: the view it shows, as it is now
    assert 'RUNS.view === "history"' in _js("openReport")   # This run: the one the History shows


def test_the_side_panels_stay_independent():
    """Switching the Runs pane's view never opens or closes another panel; nor does the scan editor, which opens and
    closes on its own."""
    for name in ("showView", "showRunList", "showQueue", "loadRunList", "renderRunList", "loadScans", "renderScans",
                 "toggleRuns", "renderQueue", "noticeRun"):
        body = _js(name)
        assert not re.search(r"toggle(Lab|Team|Analysis)\(", body), name
    for name in ("toggleEditor", "openEditor", "renderEditor", "edTitle", "edLoop", "edSave", "edRun", "edRevert",
                 "initEditor", "pickScan", "renderPick", "queuePicked", "editScan", "editQueued", "editStep", "stepGo",
                 "pasteOpen", "queueKeys", "queueDrops", "initSchedule"):
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
                  ">As written<", ">Add to queue<", ">Run<", ">Save<", ">Save as scan<", ">Revert<"):
        assert label in pane, label
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
    assert 'busyWhile(queue ? ["ed"] : ["ed", "start"]' in run and "showQueue()" in run
    buttons = _js("edButtons")
    assert "!(ED.item ? dirty : dirty || !ED.id)" in buttons and 'BUSY.has("start")' in buttons and "!c.loads" in buttons
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


# ------------------------------------------------------------------ the Schedule (Special Measure's window)

def test_the_schedule_stands_the_scans_beside_the_queue_with_an_arrow_between():
    """The scans and the queue side by side when the pane has room (and in full screen), else the scans above the
    queue; ⇒ between them (⇓ when one is above the other); the step row and Paste a plan under the scans."""
    view = PAGE[PAGE.index('<div id="schedule-view"'):PAGE.index('<div id="history-view"')]
    order = ['<section id="scans-view"', 'id="step-row"', 'id="queue-paste-open"', 'class="sched-mid"',
             '<button id="sched-add"', '<section id="queue-view"']
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
    col = PAGE[PAGE.index('<section id="queue-view"'):PAGE.index('<div id="history-view"')]
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
                 '<button id="step-add" class="sched-arrow small"', 'id="step-save"', 'id="step-cancel"'):
        assert text in row, text
    assert '<button id="queue-paste-open" class="link soft"' in PAGE and ">Paste a plan</button>" in PAGE
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


def test_a_run_that_starts_while_the_person_types_in_the_schedule_only_gets_its_tab():
    """A queued scan that starts takes the pane to its run, as ever, unless the person is typing in the schedule (a
    step, a filter, a name) or dragging in it: then it gets its tab and the schedule stays."""
    notice = _js("noticeRun")
    assert "if (!scheduling()) openRun(runId, plan);" in notice and 'setTabStatus(tab, "running")' in notice
    busy = _js("scheduling")
    assert 'RUNS.view === "schedule"' in busy and "QUEUE.dragging" in busy
    assert 'at.closest("input, textarea, select")' in busy and '$("schedule-view").contains(at)' in busy
