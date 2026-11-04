"""Full screen for each panel, and the one click model for opening panels. A button
beside a panel's close button gives that one panel the window below the top bar and the notice row (FULL in app.js,
#app.full in app.css); the button again, Esc, or closing the panel ends it and the layout under it comes back. Another
panel's top-bar button, or a panel opened by code (Analyze, Edit on a scan, a waiting request), takes the screen in its
place and full screen stays; Chat or Home, either side and even the side already chosen, ends it. A panel's top-bar
button is raised exactly when its panel is on the screen; it puts its panel away when that is on the screen, and every
other opener only shows a panel, or rings it when it is there already. Full screen never opens or closes a panel by
itself, and nothing a person must answer is ever out of sight."""
import re

import pytest

pytest.importorskip("starlette")

from labharness.web.app import STATIC  # noqa: E402

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
PANELS = {"termpane": "close-term", "runpane": "close-runs", "edpane": "close-editor",
          "anpane": "close-analysis", "labpanel": "close-lab", "teampanel": "close-team"}


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
    for text in ("if (FULL.length && !isOpen(FULL[0])) FULL.length = 0;", 'app.classList.toggle("full", !!top)',
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
    for kept in ("topbar", "toggle-sidebar", "chrome", "notices", "alerts", "hands-banner", "update-bar"):
        assert kept not in hidden, kept   # the top bar stays whole (its buttons never move), as do alarms
    assert "#app.full:not(.full-term) { flex-direction: column; }" in rules
    assert "width: auto !important" in rules[rules.index("#app.full .full-on"):]   # never the width it had beside
    assert "syncScrim" not in JS   # no scrim anywhere: nothing over the page to dim
    # the panes under it wait there: none is put away to make room while full screen covers them
    assert "!FULL.length && !ROOM.wait" in _js("fitLayout")


def test_one_panel_at_a_time_and_its_button_esc_or_closing_it_ends_it():
    """No stack: another panel takes the screen in place of the one there, and the one that leaves it stays open in
    the layout under it; closing the panel on the screen ends full screen, as Esc and its own button do."""
    assert "const FULL = [];   // the panel in full screen: one at most" in JS
    assert "FULL.push(" not in JS and "FULL.splice(" not in JS and "FULL[FULL.length - 1]" not in JS
    assert "if (FULL[0] === id) FULL.length = 0; else FULL[0] = id;" in _js("toggleFull")
    show = _js("fullShow")   # to the screen wherever it waits: in full screen, in place of the one there
    assert "if (!isOpen(id) || paneVisible(id)) return;" in show and "if (FULL.length) FULL[0] = id;" in show
    assert "FULL.length = 0;" in _js("exitFull")
    # closed while others wait under it (its x, its top-bar button, paneHide): full screen ends before the layout is
    # chosen, so the panes under it make room and a pane alone goes solo at once
    fit = _js("fitLayout")
    clear = "if (FULL.length && !isOpen(FULL[0])) FULL.length = 0;"
    assert fit.index(clear) < fit.index("const typing = ") < fit.index("if (open.length > 1 && !FULL.length")
    assert fit.index(clear) < fit.index("const solo = FULL.length ? null : soloPane();")


def test_a_panels_button_is_raised_exactly_when_its_panel_is_on_the_screen():
    """In full screen only the panel on the screen has a raised button; the others, open out of sight, are flat. So are
    the panels under the list where it takes the content area, and the terminal beside a pane alone."""
    assert ("const paneVisible = (id) => !!TOGGLE[id] && isOpen(id) && (FULL.length ? FULL[0] === id\n"
            '    : !sideSolo() && !(id === "termpane" && soloPane()));') in JS
    pills = JS[JS.index("const PILLS = {"):JS.index("};", JS.index("const PILLS = {"))]
    for pane, pill in (("termpane", "toggle-term"), ("teampanel", "toggle-team"), ("anpane", "toggle-analysis"),
                       ("runpane", "toggle-runs"), ("labpanel", "toggle-lab")):
        assert '%s: "%s"' % (pane, pill) in pills, pane
    sync = _js("syncFull")
    tail = sync[sync.index("for (const [id, pill] of Object.entries(PILLS))"):]
    for text in ("const on = paneVisible(id);", '$(pill).setAttribute("aria-pressed", String(on));',
                 '$(pill).classList.toggle("on", on);'):
        assert text in tail, text
    assert "syncFull();" in _js("fitLayout")   # after every toggle's own lines (fitLayout comes last in each)


def test_one_click_model_for_every_way_of_opening_a_panel():
    """Its own top-bar button puts a panel away when it is on the screen, else shows it; anything else only ever shows
    it (opened; in full screen, to the screen) or, when it is on the screen already, rings its head."""
    for pill, pane, toggle in (("toggle-runs", "runpane", "toggleRuns"), ("toggle-analysis", "anpane", "toggleAnalysis"),
                               ("toggle-lab", "labpanel", "toggleLab"), ("toggle-team", "teampanel", "toggleTeam"),
                               ("toggle-term", "termpane", "toggleTerm")):
        assert '$("%s").addEventListener("click", () => pillClick("%s", %s));' % (pill, pane, toggle) in JS, pill
    assert 'e.preventDefault(); pillClick("termpane", toggleTerm); }' in JS   # Ctrl+`
    pill = _js("pillClick")
    assert "if (paneVisible(id)) toggle(false);\n    else paneShow(id);" in pill
    show = _js("paneShow")
    assert 'if (paneVisible(id) && there) { ring($(id).querySelector(":scope > header")); return true; }' in show
    assert "TOGGLE[id](true)" in show and "fullShow(id);" in show and "(false)" not in show   # never closes it
    assert 'view === "schedule") toggleSeq(true);' in show and 'view === "scan") showScan();' in show
    assert "if (TOGGLE[id] && isOpen(id)) TOGGLE[id](false);" in _js("paneHide")
    ring = _js("ring")
    assert 'el.classList.add("k-ring");' in ring and 'setTimeout(() => el.classList.remove("k-ring"), 650)' in ring
    assert "requestAnimationFrame" not in ring + show + pill
    assert "@keyframes k-ring" in CSS and ".k-ring { animation: k-ring" in CSS
    assert "Object.assign(window.Kelvin, { paneShow, paneHide, paneVisible, ring });" in _js("initPanes")
    # the person's Analyze: to the screen even in full screen or under the list, rung when there; Kelvin's analyze
    # opens the pane where it stands and, under full screen or the list, waits there as a run that starts does
    opened = _js("openAnalysis")
    assert "function openAnalysis(ev) {" in opened
    assert 'const seen = paneVisible("anpane");' in opened
    assert 'if (seen) { look("anpane"); ring($("anpane").querySelector(":scope > header")); }' in opened
    assert 'else if (ev.asked) fullShow("anpane");' in opened and 'else fullShow("anpane");' not in opened
    assert "ANA.follow = false;" in opened
    assert "openAnalysis({ run: runId, asked: true }));" in JS   # a run's Analyze
    assert 'case "analysis": if (here) openAnalysis(ev); break;' in JS   # Kelvin's: not asked
    assert re.search(r'(fullShow|paneShow)\("edpane"\)', _js("openEditor"))   # Edit on a scan, New scan
    assert "fullShow" not in _js("noticeRun") + _js("openRun") + _js("showQueue")   # a run that starts doesn't take it


def test_home_links_follow_the_click_model():
    """Home's tiles, a block's link to a place, Add to sequence and Open on an approval waiting are links to a place:
    Kelvin.paneShow shows the pane, or rings it when it is there, and never hides it. The page's own
    toggles are only the fallback for a page without the click model."""
    home = (STATIC / "home.js").read_text(encoding="utf-8")

    def fn(name):
        start = re.search(r"(async )?function %s\(" % name, home).start()
        return home[start:home.index("\n  }\n", start)]

    assert "if (K.paneShow) K.paneShow(pane, view); else open();" in fn("goTo")
    assert 't.go.addEventListener("click", safe(() => goTo(name)));' in fn("tile")
    act = fn("act")
    assert act.index('op: "add"') < act.index('goTo("seq");')   # the sequence shown once the block's scan is in it
    assert "if (name) goTo(name);" in act[act.index('a.do === "pane"'):]
    assert fn("openApproval").rstrip().endswith('else goTo("lab");')
    start = home.index("const PLACES = {")
    places = home[start:home.index("\n  };\n", start)]
    for toggle in ("K.toggleSeq(", "K.toggleLab(", "K.toggleAnalysis(", "K.openRunList("):
        assert home.count(toggle) == places.count(toggle) == 1, toggle   # only as the fallbacks


def test_chat_or_home_ends_full_screen_from_either_side_even_the_one_chosen():
    """Either side, even the one chosen, brings the main column back (exitFull): full screen ends, and a pane alone or
    the list in its place goes. Home hears of the change once it shows, so home.js no longer ends full screen itself."""
    mode = _js("setMode")
    for text in ("if (was) exitFull();", 'if (change) emit("mode", { mode: S.mode });', "layoutSaid();"):
        assert text in mode, text
    assert "return;" not in mode   # the side already chosen still ends it
    assert mode.index("if (was) exitFull();") < mode.index('emit("mode"')   # Home hears of it once it shows
    home = (STATIC / "home.js").read_text(encoding="utf-8")
    on_mode = home[home.index("function onMode("):home.index("\n  }\n", home.index("function onMode("))]
    assert "exitFull" not in on_mode and "onShow();" in on_mode


def test_nothing_a_person_must_answer_is_out_of_sight_in_full_screen():
    """An approval in the chat on the screen declines itself unless someone answers: full screen ends for it. One the
    Lab panel or the terminal asks brings that panel to the screen. Another chat and a new one need the chat (the
    Analysis pane's Ask Kelvin went with its fields, 2026-11-03)."""
    handle = _js("handle")
    case = handle[handle.index('case "approval":'):]
    assert "if (here && !ev.auto) exitFull();" in case[:case.index("break;")]
    assert re.search(r'(fullShow|paneShow)\("labpanel"\);', _js("pollState"))
    assert 'if (!TERM.open) toggleTerm(true);\n    fullShow("termpane");' in _js("termAsk")
    for name in ("openChat", "newChat", "toggleIncognito"):
        assert "exitFull();" in _js(name), name
    assert "function askAboutAnalysis(" not in JS


def test_esc_ends_full_screen_unless_a_field_a_menu_or_a_dialog_takes_it():
    init = _js("initPanes")
    for text in ('if (e.key !== "Escape" || !FULL.length || e.defaultPrevented) return;',
                 'document.querySelector("dialog[open], .pane-menu:not([hidden])")',
                 'e.target.closest("input, textarea, select, [contenteditable]")',
                 "toggleFull(FULL[0]);"):   # as its own button: the layout under it (a pane alone stays)
        assert text in init, text


def test_a_panel_in_full_screen_keeps_its_content_in_homes_column():
    """On a wide window what a panel holds stands in a column as wide as Home's (1440 px and its padding), centred;
    its head stays across the window, a band at its foot keeps its line across and centres what it holds, and the
    terminal keeps its long lines."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert ("#app.full:not(.full-term) .full-on > :not(header) {\n"
            "  margin-inline: max(0px, calc((100% - 1440px - 2 * var(--pad-page, 24px)) / 2)); }") in css
    foot = css[css.index("#app.full:not(.full-term) .full-on > :is(.ed-foot, .team-form, .team-name-form)"):]
    assert "margin-inline: 0;" in foot[:foot.index("}")]
    assert ("padding-inline: max(var(--foot-x), calc((100% - 1440px - 2 * var(--pad-page, 24px)) / 2 + var(--foot-x)));"
            in foot[:foot.index("}")])
    # each band's side padding is said once, on its own rule, and the full-screen rule only adds to it
    assert "--foot-x:" not in foot[:foot.index("}")]
    team = re.search(r"\n\.team-form, \.team-name-form \{([^}]*)\}", css).group(1)
    assert "--foot-x: var(--sp-3, 12px);" in team and "padding: var(--sp-2, 8px) var(--foot-x) var(--sp-3, 12px);" in team
    eds = re.findall(r"\n\.ed-foot \{([^}]*)\}", css)
    assert "--foot-x: 14px;" in eds[0] and "padding: 8px var(--foot-x) 12px;" in eds[0]
    assert len(eds) == 2 and all("padding: 8px var(--foot-x)" in e for e in eds)
    home = (STATIC / "home.css").read_text(encoding="utf-8")
    assert "max-width: calc(1440px + 2 * var(--pad-page, 24px))" in home   # the same column as Home's


def test_a_full_screen_panels_regions_reach_across_the_side_space():
    """The column is held inside what scrolls, not around it, so the wheel scrolls a panel from the side space too: a
    region that is the panel's body (the Analysis, the scan editor, Team chat's messages) keeps the whole width with
    the column in its padding; the Lab's body holds the column in its padding and its outer parts reach out over the
    side space (--full-side, measured on the Lab with cqw); the Runs pane's body keeps the whole width, the Scan's
    measurement keeps the column and its list holds it in its padding."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    side = "calc((100% - 1440px - 2 * var(--pad-page, 24px)) / 2"
    assert ("#app.full:not(.full-term) .full-on > :is(#an-body, #ed-body) { margin-inline: 0;\n"
            "  padding-inline: max(14px, %s + 14px)); }" % side) in css
    assert ("#app.full:not(.full-term) #teampanel > .team-list { margin-inline: 0;\n"
            "  padding-inline: max(var(--sp-3, 12px), %s + var(--sp-3, 12px))); }" % side) in css
    assert re.search(r"\n\.an-body \{ padding: 12px 14px 20px;", css) and re.search(r"\n\.ed-body \{[^}]*padding: 10px 14px 12px;", css)
    assert "#teampanel .team-list { padding: var(--sp-1, 4px) var(--sp-3, 12px) var(--sp-3, 12px); }" in css
    assert "--full-side: max(0px, calc((100cqw - 1440px - 2 * var(--pad-page, 24px)) / 2));" in css
    assert "#labpanel { container: lab / inline-size;" in css   # cqw is the Lab's width
    assert ("#app.full:not(.full-term) #labpanel > .lab-scroll { margin-inline: 0; "
            "padding-inline: calc(var(--sp-4, 16px) + var(--full-side)); }") in css
    assert "margin-inline-start: calc(-4px - var(--full-side)); padding-inline-start: calc(4px + var(--full-side));" in css
    assert "margin-inline-end: calc(-4px - var(--full-side)); padding-inline-end: calc(4px + var(--full-side));" in css
    assert "#app.full:not(.full-term) #runpane > #run-body { margin-inline: 0; }" in css
    assert "#app.full:not(.full-term) #scan-live { margin-inline: max(0px, %s); }" % (side + ")") in css
    assert "padding-inline: max(var(--sp-3, 12px), %s + var(--sp-3, 12px))); }" % side in css


def test_the_full_screen_sequence_stands_in_its_blocks_column():
    """In full screen the Sequence's bar, blocks, ＋ Add and step stand in the blocks' own column (920 px, seq.css),
    centred, so Start and ⋯ end where the blocks end; the blocks' region keeps the whole width."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    seq = (STATIC / "seq.css").read_text(encoding="utf-8")
    assert ".seq-list > *, .seq-add, .seq-extra { max-width: 920px; }" in seq
    assert ("#app.full:not(.full-term) #seq-view :is(.seq-bar, .seq-list, .seq-extra) {\n"
            "  max-width: none; padding-inline: max(var(--sp-3, 12px), calc((100% - 920px) / 2)); }") in css
    assert ("#app.full:not(.full-term) #seq-view .seq-add { margin-inline: max(var(--sp-3, 12px), "
            "calc((100% - 920px) / 2)); }") in css
    assert 'id="seq-view"' in PAGE and 'id="run-body"' in PAGE
