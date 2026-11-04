"""The notice row and the toast never cover or push away what a person presses. The row holds the newest alarm nobody
has said Got it to, and counts the rest on one row ("11 more alarms") that opens Lab → Watches with its Alarm history
open, so it is never taller than three rows however many alarms come. Home's Lab tile, while an alarm is on, opens the
Lab on Watches, and else on the part chosen last; a link to a part shows it without taking the place of the part the
person chose. A toast stands just above the message box, centred on the main column (on Home, or with a panel alone
on the screen, above the bottom of what shows there), its link still pressable, and still under reduced motion.
Checked in the page's own files."""
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


def test_the_notice_row_holds_the_newest_alarm_and_counts_the_rest():
    alerts = _code(_function(JS, "renderAlerts"))
    # newest first, as runtime.alarms_view gives them: one row with its Got it, never one per alarm
    assert "const open = (alarms || []).filter((a) => !a.acknowledged);" in alerts
    assert "open.slice(0, 1).map(" in alerts and "open.map(" not in alerts
    assert '"Got it"' in alerts and '"/api/alarms/" + a.id + "/ack"' in alerts
    # the rest: one row, a button of its own, saying how many
    assert "const more = open.length - 1;" in alerts and "if (more > 0)" in alerts
    assert 'h("button", { class: "alert alert-more", type: "button"' in alerts
    assert 'more + (more === 1 ? " more alarm" : " more alarms")' in alerts
    assert 'b.addEventListener("click", showAlarms);' in alerts
    # the last stop's card still comes first, and the Lab's history still hears of every alarm
    assert "...(incident ? [incidentCard(incident)] : []), ...rows" in alerts
    assert "renderAlarmHistory(alarms || []);" in alerts


def test_n_more_opens_the_lab_on_watches_with_its_alarm_history_open():
    show = _code(_function(JS, "showAlarms"))
    assert 'paneShow("labpanel", "watches");' in show
    assert 'foldOpen(box, $("alarm-history-btn"), $("alarm-history-fold"), true)' in show
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
    assert 'box($("wake-bar")) || box($("composer"))' in place   # just above the box (or the wake bar over it)
    assert 'document.querySelector("#app .main")' in place and 'box($("app"))' in place
    assert "window.innerHeight - over.top + 10" in place and "window.innerHeight - area.bottom + 24" in place
    assert "(area.left + area.width / 2) / z" in place   # centred on the column, in the page's pixels
    assert "const z = pageZoom()" in place and "bottom / z" in place
    rules = {}   # each selector's first rule (the reduced-motion one comes later)
    for s, d in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)):
        rules.setdefault(" ".join(s.split()), d)
    assert "width: max-content" in rules[".toast"] and "max-width: calc(100% - 32px)" in rules[".toast"]
    assert "pointer-events: auto" in rules[".toast.link.show"]   # a link toast keeps its click
    still = CSS[CSS.index("@media (prefers-reduced-motion: reduce) {"):]
    assert ".toast { transform: translate(-50%, 0); transition: opacity .15s; }" in still
