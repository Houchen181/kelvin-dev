"""Full screen for each panel: a button beside its close button gives the panel the window below the top bar and hides
the chat (FULL in app.js, #app.full in app.css); the button again, or Esc, brings the chat back. Full screen never opens
or closes a panel, and nothing a person must answer is ever out of sight."""
import re

import pytest

pytest.importorskip("starlette")

from labharness.web.app import STATIC  # noqa: E402

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
PANELS = {"termpane": "close-term", "runpane": "close-runs", "edpane": "close-editor", "anpane": "close-analysis",
          "labpanel": "close-lab", "teampanel": "close-team"}


def _js(name):
    """One function of app.js, by its name."""
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


def _head(pane):
    """A panel's header."""
    start = PAGE.index("<header", PAGE.index('id="%s"' % pane))
    return PAGE[start:PAGE.index("</header>", start)]


def test_each_panel_has_a_full_screen_button_beside_its_close_button():
    assert PAGE.count('class="icon-btn full-btn"') == len(PANELS)
    for pane, close in PANELS.items():
        head = _head(pane)
        btn = re.search(r'<button class="icon-btn full-btn" type="button" data-pane="(\w+)" aria-label="Full screen" '
                        r'aria-pressed="false" title="Full screen">', head)
        assert btn and btn.group(1) == pane, pane
        assert 'id="%s"' % close in re.search(r"<button[^>]*>", head[btn.end():]).group(0), pane   # the next one
        assert 'class="i-max"' in head and 'class="i-min"' in head, pane
    assert 'b.addEventListener("click", () => toggleFull(b.dataset.pane))' in _js("initPanes")


def test_full_screen_hides_the_chat_and_the_other_panels_and_never_opens_or_closes_one():
    sync = _js("syncFull")
    for text in ("if (!isOpen(FULL[i])) FULL.splice(i, 1);", 'app.classList.toggle("full", !!top)',
                 'app.classList.toggle("full-term", top === "termpane")', '"full-on", id === top', '"full-in"',
                 'b.setAttribute("aria-pressed", String(on))'):
        assert text in sync, text
    assert "syncFull();" in _js("fitLayout") and "fitLayout();" in _js("toggleTerm")   # every panel's toggle lays out
    for name in ("syncFull", "toggleFull", "fullShow", "exitFull"):
        assert not re.search(r"toggle(Runs|Editor|Analysis|Lab|Team|Term)\(", _js(name)), name
    rules = CSS[CSS.index("/* ---------- full screen"):]
    hidden = rules[:rules.index("{ display: none !important; }")]
    for sel in ("#app.full .sidebar", "#app.full .main > .scroller", "#app.full .main > .composer-wrap",
                "#app.full:not(.full-term) .main > .termpane", "#app.full .dock-col:not(.full-in)",
                "#app.full .dock-col > :not(.full-on)", "#app.full .pane-resize"):
        assert sel in hidden, sel
    for kept in ("topbar", "alerts", "hands-banner", "update-bar"):
        assert kept not in hidden, kept   # the top bar's buttons and badges stay, as do alarms
    assert "#app.full:not(.full-term) { flex-direction: column; }" in rules
    assert "width: auto !important" in rules[rules.index("#app.full .full-on"):]   # never the width it had beside
    assert "FULL.length > 0 ||" in _js("syncScrim")   # no scrim over it on a narrow window


def test_in_full_screen_a_panel_the_person_asks_for_takes_the_screen_and_closing_it_gives_it_back():
    for pill, pane, toggle in (("toggle-runs", "runpane", "toggleRuns"), ("toggle-analysis", "anpane", "toggleAnalysis"),
                               ("toggle-lab", "labpanel", "toggleLab"), ("toggle-team", "teampanel", "toggleTeam"),
                               ("toggle-term", "termpane", "toggleTerm")):
        assert '$("%s").addEventListener("click", () => pillClick("%s", %s));' % (pill, pane, toggle) in JS, pill
    assert 'e.preventDefault(); pillClick("termpane", toggleTerm); }' in JS   # Ctrl+`
    pill = _js("pillClick")
    assert "if (FULL.length && isOpen(id) && FULL[FULL.length - 1] !== id) { fullShow(id); return; }" in pill
    assert "toggle();\n    if (isOpen(id)) fullShow(id);" in pill   # an open one out of sight isn't closed unseen
    show = _js("fullShow")
    assert "if (!FULL.length || !isOpen(id)) return;" in show and "FULL.push(id);" in show
    assert _js("openEditor").count('fullShow("edpane");') == 2   # Edit on a scan, New scan
    assert "fullShow" not in _js("noticeRun") + _js("openRun") + _js("showQueue")   # a run that starts doesn't take it


def test_nothing_a_person_must_answer_is_out_of_sight_in_full_screen():
    """An approval in the chat on the screen declines itself unless someone answers: full screen ends for it. One the
    Lab panel or the terminal asks brings that panel to the screen. Ask Kelvin, another chat and a new one need the chat."""
    handle = _js("handle")
    case = handle[handle.index('case "approval":'):]
    assert "if (here && !ev.auto) exitFull();" in case[:case.index("break;")]
    assert 'if (!S.labOpen) toggleLab(true);\n      fullShow("labpanel");' in _js("pollState")
    assert 'if (!TERM.open) toggleTerm(true);\n    fullShow("termpane");' in _js("termAsk")
    for name in ("openChat", "newChat", "askAboutAnalysis", "toggleIncognito"):
        assert "exitFull();" in _js(name), name


def test_esc_ends_full_screen_unless_a_field_a_menu_or_a_dialog_takes_it():
    init = _js("initPanes")
    for text in ('if (e.key !== "Escape" || !FULL.length || e.defaultPrevented) return;',
                 'document.querySelector("dialog[open], .pane-menu:not([hidden])")',
                 'e.target.closest("input, textarea, select, [contenteditable]")', "exitFull();"):
        assert text in init, text
