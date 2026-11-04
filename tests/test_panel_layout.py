"""Where the side panels stand, and the sidebar's button (brief 1.2.4-1.2.7). The panels stand side by side in the order
of the top bar's buttons, left to right: Team chat, Analysis, Runs (with the scan editor, which has no button, right
after it), then Lab; the terminal is the split under the chat. Short of room they shrink alike to their least widths;
when one more will not fit even so, the pane looked at least recently (opened, pressed, focused) is put away, its
button flat and what it showed kept (fitLayout in app.js). A pane that cannot stand beside the main column (520 of the
page's own pixels) even alone takes the content area by itself (.solo) and the main column waits out of sight; Chat or
Home brings it back. Nothing stands over or under anything: no floating, no stacking, no scrim.
The sidebar's button is in the top bar at every width and in full screen, and it alone hides or shows the sidebar: the
panels never do, they take the room it leaves. Its name is always "Conversations"; aria-expanded says whether the
sidebar is on the screen. Where the sidebar cannot stand beside the main column (under 784 page px) it takes the
content area as a pane alone does (.side-solo); choosing a chat brings the chat back, and a narrow window never opens
on it."""
import re

import pytest

pytest.importorskip("starlette")

from labharness.web.app import STATIC  # noqa: E402

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
SIDE_PANELS = ("teampanel", "anpane", "runpane", "edpane", "labpanel")


def _js(name):
    """One function of app.js, by its name."""
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


def _at(pane):
    """Where a side panel starts on the page."""
    return re.search(r'<(?:section|aside) id="%s"' % pane, PAGE).start()


def _panes():
    """PANES in app.js, in its order: [(id, its column)]."""
    start = JS.index("const PANES = {")
    return re.findall(r'(\w+): \[\(\) => [\w.]+, "(\w+)", \d+, "lh\.\w+"(?:, \d+)?\]',
                      JS[start:JS.index("\n  };\n", start)])


def _block(css, head):
    """A block of app.css that starts at the line `head` and ends at the first closing brace at a line's start."""
    start = css.index(head)
    return css[start:css.index("\n}\n", start)]


def test_the_panels_stand_in_the_order_of_their_buttons():
    bar = PAGE[PAGE.index('<div class="topbar-right">'):PAGE.index("</header>", PAGE.index('<div class="topbar-right">'))]
    buttons = re.findall(r'<button id="(toggle-[\w-]+)"', bar)
    assert buttons == ["toggle-incognito", "toggle-term", "toggle-team", "toggle-analysis", "toggle-runs", "toggle-lab"]
    opens = dict(re.findall(r'\$\("(toggle-\w+)"\)\.addEventListener\("click", \(\) => pillClick\("(\w+)", ', JS))
    panels = [opens[b] for b in buttons if opens.get(b, "termpane") != "termpane"]   # the terminal: under the chat
    assert panels == ["teampanel", "anpane", "runpane", "labpanel"]
    on_page = sorted(SIDE_PANELS, key=_at)
    assert [p for p in on_page if p != "edpane"] == panels
    assert on_page.index("edpane") == on_page.index("runpane") + 1   # the scan editor: right beside Runs
    assert [p for p, _ in _panes()] == on_page   # their widths are given out left to right
    assert "Team chat, Analysis, Runs with the scan editor beside it, then Lab" in JS   # the comment above PANES


def test_panels_that_share_a_column_are_neighbours_in_that_order():
    cols = [c for _, c in _panes()]
    assert cols == ["workcol"] * 4 + ["sidecol"]   # Team chat, Analysis, Runs and the editor; then Lab's own
    for col in set(cols):   # one stretch of the order each
        at = [i for i, c in enumerate(cols) if c == col]
        assert at == list(range(at[0], at[-1] + 1)), col
    work = PAGE[PAGE.index('<div id="workcol"'):PAGE.index('<div id="sidecol"')]
    side = PAGE[PAGE.index('<div id="sidecol"'):PAGE.index('<dialog id="settings-dialog"')]
    for pane, col in _panes():   # each in the column app.js counts it in, and in no other
        inside, outside = (work, side) if col == "workcol" else (side, work)
        assert re.search(r'<(?:section|aside) id="%s"' % pane, inside), pane
        assert not re.search(r'<(?:section|aside) id="%s"' % pane, outside), pane
    assert PAGE.index('<div id="workcol"') < PAGE.index('<div id="sidecol"')   # the columns too, left to right
    assert "Team chat, Analysis, Runs and the scan editor (#workcol), then Lab (#sidecol)" in CSS


def _narrow():
    """app.css's narrow screens: from its heading to the full screen's."""
    return CSS[CSS.index("/* ---------- narrow screens"):CSS.index("/* ---------- full screen")]


def _least(touch=False):
    """Each pane's least width, from PANES (on a touch screen, its own where it has one)."""
    start = JS.index("const PANES = {")
    block = JS[start:JS.index("\n  };\n", start)]
    found = re.findall(r'(\w+): \[\(\) => [\w.]+, "\w+", (\d+), "lh\.\w+"(?:, (\d+))?\]', block)
    return {p: int(t if touch and t else w) for p, w, t in found}


def test_nothing_stands_over_or_under_another_pane():
    """No floating, no stacking, no Lab under the others, no scrim (brief 1.2.4): the classes and the rules that did
    it are gone, and no pane's column or the sidebar is placed over the page."""
    for gone in ('"floating"', '"stacked"', '"lab-under"', '"float-two"', "--float-top", "FLOAT_W", '"side-floats"',
                 "side-above", "sideFloats", "syncScrim", '$("scrim")', "colWant", "colLeast"):
        assert gone not in JS, gone
    for gone in (".floating", ".stacked", ".lab-under", ".float-two", "--float-top", ".side-floats", ".side-above"):
        assert gone not in CSS, gone
    for rule in re.findall(r"[^{}]*(?:dock-col|#workcol|#sidecol|\.sidebar)[^{}]*\{[^}]*\}", CSS):
        assert "position: fixed" not in rule and "position: absolute" not in rule, rule
    fit = _js("fitLayout")   # side by side, each as wide as kept, shrunk alike to fit, none below its least
    assert "const got = share(open.map(paneWant), open.map(least), paneRoom());" in fit
    assert 'open.forEach((id, i) => { $(id).style.width = got[i] + "px"; });' in fit
    assert "flex-direction: column" not in _narrow()


def test_the_pane_looked_at_least_recently_makes_room():
    """When one more will not fit beside the main column even at the least widths, the pane looked at least recently
    is put away through its own toggle (its button flat, what it showed kept). Looking is opening it, or a press or the
    focus inside it. Not under full screen (the panes wait under it), nor while the window is being sized or is out of
    sight: once the window rests (and the page size has followed it), the layout makes room."""
    fit = _js("fitLayout")
    assert "for (const id of open) if (!ROOM.shown.includes(id)) look(id);" in fit   # opened: looked at now
    assert "const typing = ROOM.asked ? null : typedIn(open);" in fit
    assert "const away = byLook(open.filter((id) => id !== typing))[0];" in fit
    assert ("if (open.length > 1 && !FULL.length && !ROOM.wait && !document.hidden && ROOM.away !== away &&\n"
            "        sum(open.map(least)) > paneRoom()) {") in fit
    assert "try { TOGGLE[away](false); } finally { ROOM.away = null; }" in fit
    assert "const byLook = (ids) => [...ids].sort((a, b) => (LOOK[a] || 0) - (LOOK[b] || 0));" in JS
    init = _js("initPanes")
    assert '$(id).addEventListener("pointerdown", looked, true);' in init
    assert '$(id).addEventListener("focusin", looked);' in init and "const looked = () => { look(id); layoutSaid(); };" in init
    assert "ROOM.timer = setTimeout(() => { ROOM.wait = false; fitLayout(); }, 160);" in init   # after fitTiers' 50 ms
    assert 'document.addEventListener("visibilitychange", () => { if (!document.hidden) fitLayout(); });' in init
    assert "if (isOpen(id)) look(id);" in _js("paneShow")   # asked for (a Home tile, Analyze): looked at too
    assert 'if (seen) { look("anpane"); ring(' in _js("openAnalysis")
    # the person's most common pairs stand side by side at 1280x720 (90 %: 1422 page px) with the list shown, and on
    # an iPad's 1180 (100 %, touch) with the list hidden; at 1920x1080 all four do not (S32: one makes room)
    least, room = _least(), 1280 / 0.9 - 264 - 520
    assert least["runpane"] + least["labpanel"] <= room and least["runpane"] + least["anpane"] <= room
    touch = _least(touch=True)
    assert touch["runpane"] + touch["labpanel"] <= 1180 - 520 and touch["runpane"] + touch["anpane"] <= 1180 - 520
    assert sum(least[p] for p in ("teampanel", "anpane", "runpane", "labpanel")) > 1920 - 264 - 520


def test_least_widths_are_what_the_heads_hold_at():
    """A pane is never narrower than its head holds whole (brief 1.1.3: nothing clipped). Runs' head on History is the
    widest: Sequence | History with a two-digit count, Report…, ⤢ and ×, 346 page px at 85, 90 and 100 % (measured);
    on a touch screen its head buttons are 40 px (app.css "fit B2"), and it needs 362. Lab and Analysis hold at 292.
    On a touch screen the scan editor's 40 px fields fit whole only with its two-column rows (a pane of 790 or more):
    it needs 800 there, so on an iPad (1180, with the list or without) it takes the content area alone, and a width
    kept from a desktop never puts it under that (paneWant)."""
    least, touch = _least(), _least(touch=True)
    assert least["runpane"] >= 346 and touch["runpane"] >= 362
    assert least["labpanel"] >= 292 and least["anpane"] >= 292 and least["edpane"] >= 318
    assert 'runpane: [() => RUNS.open, "workcol", 346, "lh.runsWidth", 362],' in JS
    assert touch["edpane"] >= 790 and touch["edpane"] > 1180 - 520 and least["edpane"] == 360
    assert 'const COARSE = window.matchMedia("(pointer: coarse)");' in JS
    assert "const least = (id) => (COARSE.matches && PANES[id][4]) || PANES[id][2];" in JS
    assert "const paneWant = (id) => Math.max(PANE_W[id] || 0, least(id));" in JS
    assert "PANES[id][2]" not in JS.replace("const least = (id) => (COARSE.matches && PANES[id][4]) || PANES[id][2];", "")
    b2 = CSS[CSS.index("/* ===== fit B2"):CSS.index("/* ===== end fit B2")]   # the touch heads' 40 px buttons
    assert "@media (pointer: coarse) {\n  :is(.pane-head, .term-head) > .icon-btn { min-width: 40px; min-height: 40px; }" in b2
    # the Analysis pane's controls and the terminal's input are 40 px on touch too (brief 1.1.9)
    assert ("#anpane .pane-head > .btn.small, #anpane .an-actions > .btn,\n"
            '  #anpane .an-body :is(select, input:not([type="checkbox"])) { min-height: 40px; }') in b2
    assert "#an-more-run { min-width: 40px; min-height: 40px; }" in b2
    assert ".term-in { padding-block: 0 2px; }" in b2 and ".term-in input { min-height: 40px; }" in b2
    # with a mouse the terminal's input is 28 px, the authored floor (brief 1.1.9), its row as tall as before
    assert ".term-in { padding-block: 1px 5px; }\n.term-in input { min-height: 28px; }\n" in b2
    assert b2.index(".term-in input { min-height: 28px; }") < b2.index("@media (pointer: coarse)")   # touch wins


def test_the_pane_being_typed_in_is_not_put_away_for_a_pane_that_came_by_itself():
    """A run that starts (noticeRun opens Runs) or Kelvin's analyze never puts away the pane the person is typing in:
    the focus in a field inside it. A pane the person or a waiting request asked for (paneShow, a top-bar button) still
    makes room as ever, so a request is never out of sight."""
    typed = _js("typedIn")
    assert "const a = document.activeElement;" in typed
    assert '(a.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName))' in typed
    assert "return open.find((id) => $(id).contains(a)) || null;" in typed
    show = _js("paneShow")
    assert "ROOM.asked = true;" in show and "} finally { ROOM.asked = was; }" in show
    assert show.index("ROOM.asked = true;") < show.index("TOGGLE[id](true)") < show.index("fullShow(id);")
    assert "asked: false" in JS[JS.index("const LOOK = {}, ROOM = {"):]
    assert "paneShow" not in _js("noticeRun") + _js("openRun")   # a run that starts: not asked for


def test_a_pane_that_cannot_stand_beside_the_main_column_takes_the_content_area_alone():
    """Solo (brief 1.2.4): the one open pane that cannot stand beside the 520 px main column takes the content area,
    beside the sidebar where that stands, and the main column (chat, Home, the terminal) waits out of sight, so a
    phone never shows the chat's suggestions under a pane. Chat or Home brings it back: the pane is put away."""
    assert "return open.length === 1 && paneRoom() < least(open[0]) ? open[0] : null;" in _js("soloPane")
    fit = _js("fitLayout")
    assert "const solo = FULL.length ? null : soloPane();" in fit and 'app.classList.toggle("solo", !!solo);' in fit
    narrow = _narrow()
    assert ("#app.solo .main, #app.solo .pane-resize, #app.side-solo .main, #app.side-solo .dock-col "
            "{ display: none !important; }") in narrow
    assert ("#app.solo .dock-col:not([hidden]), #app.solo .dock-col > :not([hidden]) { flex: 1 1 auto; "
            "width: auto !important;") in narrow
    leave = _js("exitFull")   # Chat or Home (setMode), another chat, an approval, Ask Kelvin: the main column back
    for text in ("FULL.length = 0;", "if (sideSolo()) SIDE.hidden = true;", "fitLayout();", "const solo = soloPane();",
                 "if (solo) TOGGLE[solo](false);"):
        assert text in leave, text
    # the panes that waited under full screen make room first, and only then is the one left alone put away: two
    # left by a swap in full screen (a phone) never leave one alone in the chat's place
    assert leave.index("fitLayout();") < leave.index("const solo = soloPane();")
    assert "else fitLayout()" not in leave
    assert "if (was) exitFull();" in _js("setMode")
    # the terminal lives in the main column: off the screen beside a pane alone; shown, it brings the main column back
    assert '!(id === "termpane" && soloPane())' in JS
    show = _js("fullShow")
    assert 'const solo = !FULL.length && id === "termpane" ? soloPane() : null;' in show
    assert show.index("fitLayout();") < show.index("const solo = ") and "else fitLayout()" not in show
    # a pane alone with the list hidden already has what full screen gives: its ⤢ is not shown (it would change nothing)
    assert "#app.solo.side-hidden .full-btn { display: none; }" in narrow


def test_the_sidebar_button_is_in_the_top_bar_at_every_width():
    left = PAGE[PAGE.index('<div class="topbar-left">'):PAGE.index('<div id="chat-title"')]
    btn = re.search(r'<button id="toggle-sidebar"[^>]*>', left).group(0)
    for text in ('aria-controls="sidebar"', "aria-expanded="):
        assert text in btn, text
    assert "title=" not in btn   # its name eases in as the other top-bar buttons' names do, never the browser's tip
    rules = re.findall(r"([^{}]*#toggle-sidebar[^{}]*)\{([^}]*)\}", CSS)
    assert not [sel for sel, body in rules if "display" in body]   # never hidden: not by the width, nor in full screen
    assert '$("toggle-sidebar").addEventListener("click", toggleSidebar);' in JS


def test_where_it_cannot_stand_beside_the_main_column_the_list_takes_the_content_area():
    """Under SIDEBAR_W + CHAT_MIN (784) of the page's own pixels the list, shown, takes the content area as a pane alone
    does (.side-solo), below the top bar and the notice row: not over the page, no scrim. What was there waits out of
    sight: a chat chosen, Chat or Home, a pane's button or ☰ again brings it back; hiding it so is for now, the
    person's choice stays ☰'s (lh.sideHidden). A narrow window (a phone) never opens on the list."""
    assert "const sideNarrow = () => layoutW() < SIDEBAR_W + CHAT_MIN;" in JS
    assert "const sideSolo = () => !SIDE.hidden && !FULL.length && sideNarrow();" in JS
    assert 'app.classList.toggle("side-solo", sideSolo());' in _js("fitLayout")
    assert "#app.side-solo .sidebar { flex: 1 1 auto; width: auto; border-right: 0; }" in _narrow()
    assert not re.search(r"\.sidebar \{[^}]*\btop: 0", CSS)
    chrome = re.search(r"\n\.chrome \{[^}]*\}", CSS).group(0)   # the top bar and the notice row stay above all
    assert "position: fixed; top: 0; left: 0; right: 0;" in chrome and "z-index: 35;" in chrome
    assert "function closeSidebar() { if (sideSolo()) { SIDE.hidden = true; fitLayout(); } }" in JS   # not kept
    assert "if (sideSolo()) SIDE.hidden = true;" in _js("fullShow")   # a pane asked for: in the list's place
    assert ": !sideSolo() &&" in JS   # under the list, no pane is on the screen (its button flat)
    assert "if (sideNarrow()) SIDE.hidden = true;" in _js("initPanes")   # opening narrow: the chat or Home, not the list
    for gone in ("if (S.labOpen) toggleLab(false);", "if (RUNS.open) toggleRuns(false);", "if (ED.open) toggleEditor(false);"):
        assert gone not in JS, gone


def test_only_its_button_hides_the_sidebar_and_the_panels_take_the_room_it_leaves():
    toggle = _js("toggleSidebar")
    for text in ("SIDE.hidden = FULL.length ? false : !SIDE.hidden;", "FULL.length = 0;",   # in full screen: shows it
                 'store("lh.sideHidden", SIDE.hidden ? "1" : "0");', "fitLayout();"):
        assert text in toggle, text
    assert 'hidden: store("lh.sideHidden") === "1"' in JS   # kept for next time
    assert '"compact"' not in JS and "side-open" not in JS + CSS and ".compact" not in CSS   # the layout never hides it
    fit = _js("fitLayout")   # its width counts only while it stands beside the page
    assert 'app.classList.toggle("side-hidden", SIDE.hidden);' in fit and "syncSide();" in fit
    assert "const sideBeside = () => (SIDE.hidden || sideNarrow() ? 0 : SIDEBAR_W);" in JS
    assert "return layoutW() - sideBeside() - CHAT_MIN;" in _js("paneRoom")
    assert JS.count("SIDEBAR_W") == 3   # set, and counted in sideBeside and sideNarrow alone
    said = _js("syncSide")   # one name, whatever it shows; aria-expanded says which
    assert 'say = "Conversations"' in said
    assert 'b.setAttribute("aria-expanded", String(!SIDE.hidden && !FULL.length));' in said
    assert 'b.setAttribute("aria-label", say);' in said and "b.dataset.tip = say;" in said and "title" not in said
    assert "Hide conversations" not in JS and "Show conversations" not in JS
    assert "\n.app.side-hidden .sidebar { margin-left: -264px; visibility: hidden; }" in CSS
    # the top bar spans the window at every width (.chrome): it never starts where the sidebar ends, nor moves with it
    assert "@media (min-width: 1101px)" not in CSS and "left: 264px" not in CSS and "57px" not in CSS


def test_widths_are_the_pages_own_pixels_at_any_page_size():
    """CSS zoom (the page size) leaves the window's size and the pointer in the screen's pixels: every width taken from
    them is divided by Kelvin.zoom() before it lays out a panel (brief 1.1.8)."""
    zoom = _js("layoutZoom")
    assert 'typeof k.zoom === "function" ? Number(k.zoom()) : 1' in zoom and "z > 0 && isFinite(z) ? z : 1" in zoom
    assert "const layoutW = () => window.innerWidth / layoutZoom();" in JS
    assert "const layoutH = () => window.innerHeight / layoutZoom();" in JS
    for name in ("paneRoom", "fitLayout", "sizePane", "edgeResize", "initPanes", "restoreLayout", "setTermHeight",
                 "initTerminal"):
        body = _js(name)
        assert "innerWidth" not in body and "innerHeight" not in body, name
    assert "getBoundingClientRect().width / layoutZoom()" in _js("sizePane")
    # whole pixels: a width shown, read back and given again stays as it is (a drag never creeps narrower)
    assert "const out = want.map((w, i) => Math.floor(lo[i] + slack[i] * k));" in _js("share")
    assert "out[out.length - 1] += Math.max(0, Math.floor(room - sum(out)));" in _js("share")
    assert "const most = Math.floor(paneRoom() - sum(others.map((p) => PANE_W[p])));" in _js("sizePane")
    assert "PANE_W[id] = Math.round(" in _js("sizePane")
    edge = _js("edgeResize")
    assert "set((r.right - ev.clientX + grab) / z)" in edge and "getBoundingClientRect().width / layoutZoom() +" in edge
    assert "grab = e.clientX - r.left" in edge   # the edge stays where it was taken, under the pointer
    term = _js("initTerminal")
    assert "setTermHeight((bottom - ev.clientY) / z)" in term and "Math.min(240, layoutH() * 0.3)" in term


def test_the_layout_is_kept_per_browser():
    """The panels open (in the order they were looked at), full screen and Sequence | History (brief 1.2.7), never in
    the link; back once the page is ready, the ones looked at last first while they stand beside the main column at
    their least widths. Never a pane that cannot stand there even alone (K11: on a phone a pane alone is not brought
    back), nor the scan editor. Every change is told to the parts kept apart (kelvin:layout)."""
    said = _js("layoutSaid")
    for text in ("if (LAYOUT.back && kept !== LAYOUT.kept) {", "if (said === LAYOUT.said) return;", 'emit("layout", now);',
                 'const panes = byLook(openPanes()).filter((id) => id !== "edpane");',
                 'store("lh.panes", JSON.stringify(panes));',
                 'store("lh.full", now.full && now.full !== "edpane" ? now.full : "");', 'store("lh.runsView", RUNS.view);'):
        assert text in said, text
    assert "panes: Object.keys(TOGGLE).filter(paneVisible), full: FULL[0] || null," in said
    back = _js("restoreLayout")
    assert "let room = paneRoom() - sum(openPanes().map(least));" in back
    assert "for (const id of [...kept].reverse()) {" in back   # the newest look first
    assert ('if (!PANES[id] || id === "edpane" || isOpen(id) || back.includes(id) || least(id) > room) continue;'
            in back)
    assert "room -= least(id);" in back and "back.unshift(id);" in back
    assert 'if (full && full !== "edpane" && TOGGLE[full] && isOpen(full)) FULL[0] = full;' in back
    assert "LAYOUT.back = true;" in back and "location" not in said + back
    init = _js("initPanes")
    assert 'document.addEventListener("kelvin:ready", restoreLayout, { once: true });' in init
    assert 'document.addEventListener("kelvin:seq", layoutSaid);' in init
    assert "layoutSaid();" in _js("fitLayout") and "layoutSaid();" in _js("setMode")


def test_it_slides_on_the_motion_curve_and_never_for_less_motion():
    """The panels widen or narrow on the sidebar's curve, so its width and theirs change alike and the conversation
    never drops below its least width meanwhile. Where the list takes the content area it comes and goes at once, as
    a pane alone does: content never waits."""
    motion = CSS[CSS.index("/* motion, as labMaster moves"):]
    assert ".app.side-slide .sidebar { transition: margin-left .42s var(--ease-switch), visibility .42s; }" in motion
    assert ".app.side-slide .topbar" not in CSS   # the top bar spans the window: nothing in it slides with the sidebar
    assert ".app.side-slide :is(.dock-col, .dock-col > *) { transition: width .42s var(--ease-switch); }" in motion
    calm = motion[motion.index("@media (prefers-reduced-motion: reduce)"):]
    assert re.search(r"#app \.sidebar,[^{]*\{ transition: none; \}", calm)
    toggle = _js("toggleSidebar")   # for the button's change alone: never as the panels make room, nor on loading
    assert toggle.index("if (!sideNarrow()) {") < toggle.index('app.classList.add("side-slide");') < toggle.index("fitLayout();")
    assert 'setTimeout(() => app.classList.remove("side-slide"), 500)' in toggle
    assert JS.count('"side-slide"') == 2
    for name in ("toggleSidebar", "syncSide", "fitLayout", "paneRoom", "soloPane", "restoreLayout", "initPanes"):
        assert "requestAnimationFrame" not in _js(name), name


def test_a_pane_comes_in_once_and_never_again_when_it_is_in_sight_again():
    """The panes' fade-in (k-pane-in) plays when one opens, not again when full screen ends or a pane alone or the
    list goes and it is in sight again: once it has played the pane keeps animation: none until it closes. It is a fade
    of .15 s on the curve for what comes in (brief 1.5.1-1.5.3): content never waits, and nothing moves, so the same
    from the keyboard (Ctrl+`, Esc)."""
    init = _js("initPanes")
    assert 'for (const id of ["termpane", ...Object.keys(PANES)]) {' in init
    assert 'el.addEventListener("animationend", (e) => { if (e.target === el) el.style.animation = "none"; });' in init
    fit = _js("fitLayout")   # closed: it comes in again next time
    assert ('for (const id of ["termpane", ...Object.keys(PANES)]) if ($(id).hidden) $(id).style.animation = "";'
            in fit)
    motion = CSS[CSS.index("/* motion, as labMaster moves"):]
    assert ":is(#runpane, #edpane, #anpane, #termpane, #labpanel, #teampanel) { animation: k-pane-in .15s var(--ease-in); }" in motion
    assert "@keyframes k-pane-in { from { opacity: 0; } }" in motion   # no slide
    assert "translateX(18px)" not in CSS


def test_the_chat_and_home_never_come_in_again_when_the_main_column_is_back():
    """The chat or Home in sight again after full screen, a pane alone or the list in its place (a resize, Esc, a chat
    chosen) is just there: nothing moves when the layout re-fits (brief 1.5.3). Only a real switch of Chat | Home lets
    the view come in."""
    assert 'const mainOut = (app) => ["full", "solo", "side-solo"].some((c) => app.classList.contains(c));' in JS
    fit = _js("fitLayout")
    assert "const app = $(\"app\"), open = openPanes(), out = mainOut(app);" in fit
    back = 'if (out && !mainOut(app)) for (const v of ["scroller", "homeview"]) $(v).style.animation = "none";'
    assert back in fit and fit.index("syncFull();") < fit.index(back)   # after full screen's classes are set
    mode = _js("setMode")
    switch = 'for (const v of ["scroller", "homeview"]) $(v).style.animation = "";'
    assert switch in mode and mode.index("if (change) {") < mode.index(switch) < mode.index("if (was) exitFull();")
