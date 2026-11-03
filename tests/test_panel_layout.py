"""Where the side panels stand, and the sidebar's button. The panels stand in the order of the top bar's buttons, left to
right: Team chat, Analysis, Runs (with the scan editor, which has no button, right after it), then Lab; the terminal
is the split under the chat. Short of room, panels that share a column are neighbours in that order, one above the
other: Team chat, Analysis, Runs and the scan editor in one column, beside Lab's, so every set of panels without Team
chat stands and stacks as it did (fitLayout in app.js); at 1100 of the page's own pixels or less (the window's divided
by the page size) they float in one column, in that order from the top, each open one an equal share of its height.
The sidebar's button is in the top bar at every width and in full screen, and it alone hides or shows the sidebar
(brief 1.2.6): the panels never do, they take the room it leaves. Its name is always "Conversations"; aria-expanded
says whether the sidebar is on the screen. Where the page has no room for it beside, the sidebar stands over the
page, below the top bar, so the same button hides it; the scrim under it hides it too, and nothing else."""
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
    return re.findall(r'(\w+): \[\(\) => [\w.]+, "(\w+)", \d+, "lh\.\w+"\]', JS[start:JS.index("\n  };\n", start)])


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
    # where they float, one column, the first above the second; with only Team chat or Lab open, as narrow as they are
    narrow = _narrow()
    assert (".app.floating:not(:has(.dock-col > .runpane:not([hidden]))) .dock-col { width: min(380px, "
            "calc(100 * var(--vw, 1vw))) !important; }") in narrow
    assert "#sidecol { width" not in narrow and ".app.float-two #workcol, .app.float-two #sidecol {" not in narrow
    assert "Team chat, Analysis, Runs and the scan editor (#workcol), then Lab (#sidecol)" in CSS
    assert "Team chat, Analysis, Runs and the scan editor share a column, one above the other, beside Lab's" in JS


def _narrow():
    """app.css's narrow screens: from its heading to the full screen's."""
    return CSS[CSS.index("/* ---------- narrow screens"):CSS.index("/* ---------- full screen")]


def test_where_they_float_each_open_panel_has_an_equal_share_of_the_height():
    """At FLOAT_W (1100) of the page's own pixels or less both columns float, the first above the second: the first's
    part of the height is as many shares as it has panels open, so with Team chat, Analysis, Runs and Lab open each has
    a quarter, never Lab a half. Below the top bar and the notice row (--chrome-h), so no alarm is covered."""
    narrow = _narrow()
    assert (".app.floating.float-two #workcol { bottom: calc((100% - var(--chrome-h, 52px)) * "
            "(1 - var(--float-top, .5))); }") in narrow
    assert (".app.floating.float-two #sidecol { top: calc(var(--chrome-h, 52px) + (100% - var(--chrome-h, 52px)) * "
            "var(--float-top, .5)); }") in narrow
    assert "50%" not in narrow and "@media (max-width: 1100px)" not in narrow   # the page's pixels, not the window's
    assert ".app.floating .dock-col > * { width: auto !important; flex: 1 1 0; min-height: 0; }" in narrow
    assert ".app.floating .dock-col { position: fixed; right: 0; top: var(--chrome-h, 52px);" in narrow
    fit = _js("fitLayout")
    assert "const floating = w <= FLOAT_W," in fit and 'app.classList.toggle("floating", floating);' in fit
    assert 'if (both) app.style.setProperty("--float-top", colPanes("workcol").length / open.length);' in fit


def test_when_even_two_columns_do_not_fit_lab_goes_under_the_others():
    """Never a panel over another (the Lab column used to float over Runs and hide its head's buttons): one column for
    all, as wide as the widest wants, each open panel an equal share of the height."""
    fit = _js("fitLayout")
    assert "const under = stacked && both && side + CHAT_MIN + stackLeast > w;" in fit
    assert 'app.classList.toggle("lab-under", under);' in fit and "float-lab" not in JS + CSS
    assert "const want = under ? [Math.max(colWant(\"workcol\"), colWant(\"sidecol\"))] : cols.map(colWant);" in fit
    assert 'if (under) $("sidecol").style.width = got[0] + "px";' in fit
    assert "#app.lab-under:not(.full) #workcol { align-self: flex-start; height: calc(100% * var(--float-top, .5)); }" in CSS
    under = CSS[CSS.index("#app.lab-under:not(.full) #sidecol {"):]
    under = under[:under.index("}")]
    assert "position: fixed; right: 0; bottom: 0;" in under and "box-shadow" not in under
    assert "top: calc(var(--chrome-h, 52px) + (100% - var(--chrome-h, 52px)) * var(--float-top, .5));" in under
    assert "for (const p of under ? openPanes() : colPanes(col))" in _js("sizePane")   # one column: one width


def test_the_sidebar_button_is_in_the_top_bar_at_every_width():
    left = PAGE[PAGE.index('<div class="topbar-left">'):PAGE.index('<div id="chat-title"')]
    btn = re.search(r'<button id="toggle-sidebar"[^>]*>', left).group(0)
    for text in ('aria-controls="sidebar"', "aria-expanded="):
        assert text in btn, text
    assert "title=" not in btn   # its name eases in as the other top-bar buttons' names do, never the browser's tip
    rules = re.findall(r"([^{}]*#toggle-sidebar[^{}]*)\{([^}]*)\}", CSS)
    assert not [sel for sel, body in rules if "display" in body]   # never hidden: not by the width, nor in full screen
    assert '$("toggle-sidebar").addEventListener("click", toggleSidebar);' in JS


def test_over_the_page_the_list_starts_below_the_top_bar_so_its_button_closes_it():
    """Where the page has no room for it beside (.side-floats: under SIDEBAR_W + CHAT_MIN of the page's own pixels) the
    list stands over the page below the top bar and the notice row, so it never covers the button that showed it. The
    scrim under it hides the list, and nothing else: no panel closes with it."""
    narrow = _narrow()
    assert (".app.side-floats .sidebar { position: fixed; left: 0; top: var(--chrome-h, 52px); bottom: 0; z-index: 30;"
            in narrow)
    assert ".app.side-floats.side-hidden .sidebar { transform: translateX(-100%); visibility: hidden;" in narrow
    assert not re.search(r"\.sidebar \{[^}]*\btop: 0", CSS)
    assert "const sideFloats = () => layoutW() < SIDEBAR_W + CHAT_MIN;" in JS
    assert 'app.classList.toggle("side-floats", sideFloats());' in _js("fitLayout")
    chrome = re.search(r"\n\.chrome \{[^}]*\}", CSS).group(0)   # the top bar and the notice row stay above all
    assert "position: fixed; top: 0; left: 0; right: 0;" in chrome and "z-index: 35;" in chrome
    assert "background: rgba(0, 0, 0, 0.35); z-index: 20; }" in CSS   # the scrim, under them
    assert '$("scrim").addEventListener("click", closeSidebar);' in JS
    assert '$("scrim").hidden = FULL.length > 0 || SIDE.hidden || !sideFloats();' in _js("syncScrim")
    assert "function closeSidebar() { if (sideFloats() && !SIDE.hidden && !FULL.length) toggleSidebar(); }" in JS
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
    assert "const side = sideBeside();" in fit and "const stacked = !floating && side + CHAT_MIN + rowLeast > w;" in fit
    assert 'app.classList.toggle("side-hidden", SIDE.hidden);' in fit and "syncSide();" in fit
    assert "const sideBeside = () => (SIDE.hidden || sideFloats() ? 0 : SIDEBAR_W);" in JS
    assert "return layoutW() - sideBeside() - CHAT_MIN;" in _js("paneRoom")
    assert JS.count("SIDEBAR_W") == 3   # set, and counted in sideBeside and sideFloats alone
    said = _js("syncSide")   # one name, whatever it shows; aria-expanded says which
    assert 'say = "Conversations"' in said
    assert 'b.setAttribute("aria-expanded", String(!SIDE.hidden && !FULL.length));' in said
    assert 'b.setAttribute("aria-label", say);' in said and "b.dataset.tip = say;" in said and "title" not in said
    assert "Hide conversations" not in JS and "Show conversations" not in JS
    assert "\n.app.side-hidden:not(.side-floats) .sidebar { margin-left: -264px; visibility: hidden; }" in CSS
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
    edge = _js("edgeResize")
    assert "set((r.right - ev.clientX + grab) / z)" in edge and "getBoundingClientRect().width / layoutZoom() +" in edge
    assert "grab = e.clientX - r.left" in edge   # the edge stays where it was taken, under the pointer
    term = _js("initTerminal")
    assert "setTermHeight((bottom - ev.clientY) / z)" in term and "Math.min(240, layoutH() * 0.3)" in term


def test_the_layout_is_kept_per_browser():
    """The panels open, full screen and Sequence | History (brief 1.2.7), never in the link; back once the page is
    ready, but not the scan editor, nor a panel that cannot stand beside the main column now. Every change is told to
    the parts kept apart (kelvin:layout)."""
    said = _js("layoutSaid")
    for text in ("if (LAYOUT.back) {", "if (sig === LAYOUT.said) return;", 'emit("layout", now);',
                 'store("lh.panes", JSON.stringify(openPanes().filter((id) => id !== "edpane")));',
                 'store("lh.full", now.full && now.full !== "edpane" ? now.full : "");', 'store("lh.runsView", RUNS.view);'):
        assert text in said, text
    assert "panes: Object.keys(TOGGLE).filter(paneVisible), full: FULL[0] || null," in said
    back = _js("restoreLayout")
    assert ('if (id === "edpane" || !kept.includes(id) || isOpen(id) || layoutW() <= FLOAT_W || '
            "paneRoom() < PANES[id][2]) continue;") in back
    assert 'if (full && full !== "edpane" && TOGGLE[full] && isOpen(full)) FULL[0] = full;' in back
    assert "LAYOUT.back = true;" in back and "location" not in said + back
    init = _js("initPanes")
    assert 'document.addEventListener("kelvin:ready", restoreLayout, { once: true });' in init
    assert 'document.addEventListener("kelvin:seq", layoutSaid);' in init
    assert "layoutSaid();" in _js("fitLayout") and "layoutSaid();" in _js("setMode")


def test_it_slides_on_the_motion_curve_and_never_for_less_motion():
    """The panels widen or narrow on the sidebar's curve, so its width and theirs change alike and the conversation
    never drops below its least width meanwhile. Over the page it slides in from the left on the same curve."""
    motion = CSS[CSS.index("/* motion, as labMaster moves"):]
    assert ".app.side-slide .sidebar { transition: margin-left .42s var(--ease-switch), visibility .42s; }" in motion
    assert ".app.side-slide .topbar" not in CSS   # the top bar spans the window: nothing in it slides with the sidebar
    assert ".app.side-slide :is(.dock-col, .dock-col > *) { transition: width .42s var(--ease-switch); }" in motion
    assert ".app.side-slide.side-floats .sidebar { transition: transform .42s var(--ease-switch), visibility .42s; }" in CSS
    calm = motion[motion.index("@media (prefers-reduced-motion: reduce)"):]
    assert re.search(r"#app \.sidebar,[^{]*\{ transition: none; \}", calm)   # beside the page or over it alike
    toggle = _js("toggleSidebar")   # for the button's change alone: never as the panels make room, nor on loading
    assert toggle.index('app.classList.add("side-slide");') < toggle.index("fitLayout();")
    assert 'setTimeout(() => app.classList.remove("side-slide"), 500)' in toggle
    assert JS.count('"side-slide"') == 2
    for name in ("toggleSidebar", "syncSide", "fitLayout", "paneRoom"):
        assert "requestAnimationFrame" not in _js(name), name
