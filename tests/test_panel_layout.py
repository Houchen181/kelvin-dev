"""Where the side panels stand, and the sidebar's button. The panels stand in the order of the top bar's buttons, left to
right: Team chat, Analysis, Runs (with the scan editor, which has no button, right after it), then Lab; the terminal
is the split under the chat. Short of room, panels that share a column are neighbours in that order, one above the
other (fitLayout in app.js); below 1100 px they float in one column, in that order from the top. The sidebar's button
is in the top bar at every width: where the sidebar stands beside the page it hides it and shows it again, kept for
next time, and the panels take its room; where the sidebar floats it opens it over the page, as before."""
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
    # below 1100 px one column, the first above the second; with only Team chat or Lab open, as narrow as they are
    narrow = _block(CSS, "@media (max-width: 1100px)")
    assert ".app.float-two #workcol { bottom: 50%; }" in narrow and ".app.float-two #sidecol { top: 50%; }" in narrow
    assert (".app:not(:has(.dock-col > .runpane:not([hidden]))) .dock-col { width: min(380px, 100vw) !important; }"
            in narrow)
    assert "#sidecol { width" not in narrow and ".app.float-two #workcol, .app.float-two #sidecol {" not in narrow
    assert "Team chat, Analysis, Runs and the scan editor (#workcol), then Lab (#sidecol)" in CSS


def test_the_sidebar_button_is_in_the_top_bar_at_every_width():
    left = PAGE[PAGE.index('<div class="topbar-left">'):PAGE.index('<div id="chat-title"')]
    btn = re.search(r'<button id="toggle-sidebar"[^>]*>', left).group(0)
    for text in ('aria-controls="sidebar"', 'aria-expanded="true"', 'aria-label="Hide conversations"'):
        assert text in btn, text
    assert CSS.count("#toggle-sidebar") == 1   # never hidden by the window's width ...
    assert "#app.full .sidebar, #app.full #toggle-sidebar," in CSS   # ... only in full screen, with the sidebar
    assert '$("toggle-sidebar").addEventListener("click", toggleSidebar);' in JS


def test_beside_the_page_it_hides_the_sidebar_and_the_panels_take_its_room():
    toggle = _js("toggleSidebar")
    assert 'if (sideFloats()) app.classList.toggle("side-open");' in toggle   # floating: over the page, as before
    for text in ("SIDE.hidden = !SIDE.hidden;", 'store("lh.sideHidden", SIDE.hidden ? "1" : "0");', "fitLayout();",
                 'if (!SIDE.hidden && sideFloats()) app.classList.add("side-open");'):   # no room: over the page
        assert text in toggle, text
    assert 'const sideFloats = () => window.innerWidth <= 820 || $("app").classList.contains("compact");' in JS
    assert 'hidden: store("lh.sideHidden") === "1"' in JS   # kept for next time
    fit = _js("fitLayout")   # its width counts only while it stands beside the page
    assert "const side = w > 820 && !SIDE.hidden ? SIDEBAR_W : 0;" in fit
    assert 'app.classList.toggle("side-hidden", SIDE.hidden);' in fit and "syncSide();" in fit
    assert "w <= 820 || SIDE.hidden || $(\"app\").classList.contains(\"compact\") ? 0 : SIDEBAR_W" in _js("paneRoom")
    assert JS.count("SIDEBAR_W") == 3   # set, and counted in paneRoom and fitLayout alone
    assert 'function closeSidebar() { $("app").classList.remove("side-open"); syncScrim(); syncSide(); }' in JS
    said = _js("syncSide")   # the button says what it will do
    assert 'shown ? "Hide conversations" : "Show conversations"' in said
    assert 'b.setAttribute("aria-expanded", String(shown));' in said and 'b.setAttribute("aria-label", say);' in said
    assert "sideFloats() ? $(\"app\").classList.contains(\"side-open\") : !SIDE.hidden" in said
    assert ("@media (min-width: 821px) { .app.side-hidden:not(.compact) .sidebar { margin-left: -264px; "
            "visibility: hidden; } }") in CSS
    wide = _block(CSS, "@media (min-width: 1101px)")   # the fixed top bar starts where the sidebar ends
    assert "left: 264px;" in wide and "#app:is(.compact, .side-hidden):not(.full) .topbar { left: 0; }" in wide


def test_it_slides_on_the_motion_curve_and_never_for_less_motion():
    motion = CSS[CSS.index("/* motion, as labMaster moves"):]
    assert ".app.side-slide .sidebar { transition: margin-left .42s var(--ease-switch), visibility .42s; }" in motion
    assert ".app.side-slide .topbar { transition: left .42s var(--ease-switch); }" in motion
    calm = motion[motion.index("@media (prefers-reduced-motion: reduce)"):]
    assert "#app .sidebar, #app .topbar { transition: none; }" in calm
    toggle = _js("toggleSidebar")   # for the button's change alone: never as the panels make room, nor on loading
    assert toggle.index('app.classList.add("side-slide");') < toggle.index("fitLayout();")
    assert 'setTimeout(() => app.classList.remove("side-slide"), 500)' in toggle
    assert JS.count('"side-slide"') == 2
    for name in ("toggleSidebar", "syncSide", "fitLayout", "paneRoom"):
        assert "requestAnimationFrame" not in _js(name), name
