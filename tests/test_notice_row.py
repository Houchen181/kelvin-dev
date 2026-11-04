"""The notice row and the toast never cover or push away what a person presses. The row holds one alarm nobody has said
Got it to (the newest still on, else the newest cleared), and counts the rest on one row ("11 more alarms") that opens
Lab → Watches with its Alarm history open and in view, each alarm there with its Got it, so the row is never taller than
three rows however many alarms come; it is drawn again only when what it says changed, and the keyboard's place on it
is kept. Home's Lab tile, while an alarm is on, opens the Lab on Watches, and else on the part chosen last; a link to a
part shows it without taking the place of the part the person chose. A toast stands just above the message box (or a
menu open over it), centred on the main column (on Home, or with a panel alone on the screen, above the bottom of what
shows there, or the band at its foot), and is placed again while it shows when the layout, the window or the message
box changes; its link still pressable, and still under reduced motion. Checked in the page's own files."""
import re
from pathlib import Path

import labharness

STATIC = Path(labharness.__file__).parent / "web" / "static"


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _function(js, name):
    """The body of a function of a script, by its name (braces counted)."""
    start = js.index("function %s(" % name)
    i, depth = js.index("{", start), 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start:j + 1]
    raise AssertionError(name)


def _code(text):
    """text without its // comments, so a word in a comment proves nothing."""
    return re.sub(r"//[^\n]*", "", text)


JS = _read("app.js")
CSS = _read("app.css")


def test_the_notice_row_holds_one_alarm_and_counts_the_rest():
    alerts = _code(_function(JS, "renderAlerts"))
    # newest first, as runtime.alarms_view gives them: one row with its Got it, never one per alarm; the one shown is
    # the newest still on, so a live alarm is never hidden behind one that cleared in the night
    assert "const open = (alarms || []).filter((a) => !a.acknowledged);" in alerts
    assert "const first = open.find((a) => !a.cleared) || open[0], more = open.length - 1;" in alerts
    assert "open.map(" not in alerts and "open.slice(" not in alerts
    assert "gotIt(first)" in alerts and 'class: "alert" + (first.cleared ? " cleared" : "")' in alerts
    got = _code(_function(JS, "gotIt"))
    assert '"Got it"' in got and '"/api/alarms/" + a.id + "/ack"' in got and '"data-k": "ok"' in got
    # the rest: one row, a button of its own, saying how many
    assert "if (more > 0)" in alerts
    assert 'h("button", { class: "alert alert-more", type: "button", "data-k": "more"' in alerts
    assert 'more + (more === 1 ? " more alarm" : " more alarms")' in alerts
    assert 'b.addEventListener("click", showAlarms);' in alerts
    # the last stop's card still comes first, and the Lab's history still hears of every alarm
    assert "...(incident ? [incidentCard(incident)] : []), ...rows" in alerts
    assert "renderAlarmHistory(alarms || []);" in alerts


def test_the_notice_row_is_drawn_again_only_when_it_changed_and_keeps_the_keyboards_place():
    """The state comes every few seconds: a row drawn again each time would take the focus from Got it or "n more"."""
    alerts = _code(_function(JS, "renderAlerts"))
    assert "const sig = JSON.stringify([incident || null, first || null, more]);" in alerts
    assert "if (sig === S.alertsShown) return;" in alerts and "S.alertsShown = sig;" in alerts
    assert alerts.index("renderAlarmHistory(") < alerts.index("if (sig === S.alertsShown) return;")   # it still hears
    assert alerts.index("document.title =") < alerts.index("if (sig === S.alertsShown) return;")
    assert "keepPlace(box, () => box.replaceChildren(" in alerts
    keep = _code(_function(JS, "keepPlace"))
    assert "box.contains(a) ? a.getAttribute(\"data-k\")" in keep and "to.focus({ preventScroll: true })" in keep
    # the last stop's buttons: their data-k, and pressed again once the state is back when the card stays
    card = _code(_function(JS, "incidentCard"))
    assert '"data-k": action' in card and "await pollState();\n        b.disabled = false;" in card


def test_n_more_opens_the_lab_on_watches_with_its_alarm_history_open_and_in_view():
    show = _code(_function(JS, "showAlarms"))
    assert 'paneShow("labpanel", "watches");' in show
    # open at once (no unfolding under the eyes), then scrolled to the top of the part, in the page's pixels
    assert 'foldOpen(box, btn, $("alarm-history-fold"), true, { detail: 0 })' in show
    assert ("region.scrollTop += (btn.getBoundingClientRect().top - region.getBoundingClientRect().top) / pageZoom() - 4;"
            in show)
    assert 'btn.closest("[data-region]")' in show and "scrollIntoView" not in show   # never the page itself
    # each alarm nobody answered has its Got it there, and a press keeps the keyboard's place on the list
    hist = _code(_function(JS, "renderAlarmHistory"))
    assert "a.acknowledged ? null : gotIt(a)" in hist and 'keepPlace($("alarm-history-list"),' in hist
    assert ".alarm-history li { display: flex;" in CSS and ".alarm-history li > .btn { flex: none; }" in CSS
    # a button that looks like one: a whole-row target of 28 px, 40 on touch
    assert ".alert-more { min-height: 30px;" in CSS and ".alert-more:hover {" in CSS
    touch = CSS[CSS.index("@media (pointer: coarse) {   /* touch: every target 40 px or more */"):]
    assert "#notices :is(.btn, button.link, .alert-more) { min-height: 40px; }" in touch[:touch.index("\n}\n")]


def test_a_link_to_a_lab_part_shows_it_without_taking_the_place_of_the_part_chosen():
    show = _code(_function(JS, "paneShow"))
    assert ('if (id === "labpanel" && view) chooseLabPart(view === "kept" ? store("lh.labPart") : view, false);'
            in show)
    choose = _code(_function(JS, "chooseLabPart"))
    assert choose.startswith("function chooseLabPart(id, keep = true) {")
    assert 'if (keep) store("lh.labPart", id);' in choose
    assert choose.count('store("lh.labPart"') == 1


def test_homes_lab_tile_opens_on_watches_while_an_alarm_is_on():
    home = _read("home.js")
    go = _code(_function(home, "goTo"))
    assert 'const view = name === "lab" ? (alarmsOn(K.S.last).length ? "watches" : "kept") : place;' in go
    assert "K.paneShow(pane, view)" in go
    # an alarm on is one not cleared yet, as the tile's chip counts them (a Got it does not end one)
    assert "const alarmsOn = (s) => ((s && s.alarms) || []).filter((a) => a && !a.cleared);" in home


def test_a_toast_stands_above_the_message_box_centred_on_the_main_column():
    toast = _code(_function(JS, "toast"))
    assert "try { placeToast(t); } catch (e) { t.removeAttribute(\"style\"); }" in toast
    place = _code(_function(JS, "placeToast"))
    # just above the box, or the wake bar or a menu open over it; with a panel alone, above the band at its foot
    assert 'tops("#composer, #wake-bar, #composer :is(.slash-menu, .pane-menu)")' in place
    assert 'tops("#app :is(.ed-foot, .team-form, .team-name-form)")' in place
    assert 'document.querySelector("#app .main")' in place and 'box($("app"))' in place
    assert "const foot = term && term.top - area.top > 80 ? term.top : area.bottom;" in place   # Home: the terminal's top
    assert "const top = over.length ? Math.min(...over) - 10 : foot - 24;" in place
    assert "(area.left + area.width / 2) / z" in place   # centred on the column, in the page's pixels
    assert "const z = pageZoom()" in place and "(window.innerHeight - top) / z" in place
    rules = {}   # each selector's first rule (the reduced-motion one comes later)
    for s, d in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)):
        rules.setdefault(" ".join(s.split()), d)
    assert "width: max-content" in rules[".toast"] and "max-width: calc(100% - 32px)" in rules[".toast"]
    assert "pointer-events: auto" in rules[".toast.link.show"]   # a link toast keeps its click
    still = CSS[CSS.index("@media (prefers-reduced-motion: reduce) {"):]
    assert ".toast { transform: translate(-50%, 0); transition: opacity .15s; }" in still


def test_a_toast_showing_is_placed_again_when_what_it_stands_over_moves():
    """A panel opened, Chat after Home, the window, the message box growing (a file taken) or a menu opening over it:
    a toast placed once would then lie over the composer's controls, and a link toast would take their clicks."""
    toast = _code(_function(JS, "toast"))
    assert "document.body.append(t); watchToast(); }" in toast   # watched from the first toast on
    watch = _code(_function(JS, "watchToast"))
    assert 'const t = document.querySelector(".toast.show");' in watch and "placeToast(t)" in watch
    assert 'document.addEventListener("kelvin:layout", again);' in watch
    assert 'window.addEventListener("resize", again);' in watch
    assert "new ResizeObserver(again)" in watch
    for el in ('document.querySelector("#app .main")', '$("composer")', '$("wake-bar")', '$("slash-menu")',
               '$("compare-menu")'):
        assert el in watch, el
    # a drop of files: the ones taken show first, then why one was not, so the toast stands above them
    add = _code(_function(JS, "addFiles"))
    assert "toast(" not in add[:add.index("renderAttachments();")]
    assert add.rstrip().endswith("renderAttachments();\n    if (refused) toast(refused);\n  }")
