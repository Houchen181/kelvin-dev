"""Where the side panels stand: in the order of the top bar's buttons, left to right: Team chat, Analysis, Runs (with
the scan editor, which has no button, right after it), then Lab; the terminal is the split under the chat. Short of
room, panels that share a column are neighbours in that order, one above the other (fitLayout in app.js); below
1100 px they float in one column, in that order from the top."""
import re

import pytest

pytest.importorskip("starlette")

from labharness.web.app import STATIC  # noqa: E402

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
SIDE_PANELS = ("teampanel", "anpane", "runpane", "edpane", "labpanel")


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

