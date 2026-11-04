"""The welcome page's steps (welcome.js), checked in its own file. Next applies its own step and goes on; Done applies
every step, a step reached by its dot included, and shows the first one that refuses, its refusal on its row. Back and
the dots go only to a step already seen and apply, import or save nothing on the way. Skip, in the head, ends the page.
The step dots are targets of 30 px (24 px or more at every page size), 40 px on touch, set in welcome.css alone."""
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


JS = _read("welcome.js")
SAVES = ("apply(", "importTicked(", "saveKey(", "saveLocal(", "addFound(", "K.api(", "finish(")


def test_done_applies_every_step_and_next_only_its_own():
    fwd = _code(_function(JS, "forward"))
    assert "const last = W.step === STEPS.length - 1;" in fwd
    # the steps it applies: every one when last (Done), else only the one on screen (Next)
    loop = re.search(r"for \(const i of last \? STEPS\.map\(\(s, k\) => k\) : \[W\.step\]\) \{(.*?)\n      \}", fwd, re.S)
    assert loop, "Done must apply every step, Next only its own"
    body = loop.group(1)
    assert "at = i;" in body and "ok = await apply(STEPS[i][0]);" in body and "if (!ok) break;" in body
    assert body.index("at = i;") < body.index("apply(")   # the step that refuses is the one shown
    # all applied on Done: the end; else the next step, or the one that refused
    assert "if (ok && last) { finish(); return; }" in fwd
    assert "toStep(ok ? W.step + 1 : at);" in fwd
    assert fwd.index("if (ok && last)") < fwd.index("toStep(")
    # one press at a time: nothing while a step is being applied
    assert "if (!W.open || W.moving || W.busy) return;" in fwd


def test_back_and_the_dots_go_only_to_a_step_seen_and_save_nothing():
    go = _code(_function(JS, "goTo"))
    guard = re.search(r"if \(([^)]*)\) return;", go)
    assert guard and "target > W.seen" in guard.group(1) and "target < 0" in guard.group(1)
    assert "W.moving" in guard.group(1) and "W.busy" in guard.group(1)
    assert "toStep(target);" in go
    to = _code(_function(JS, "toStep"))
    assert "W.seen = Math.max(W.seen, step);" in to
    for part, code in (("goTo", go), ("toStep", to)):
        for call in SAVES:
            assert call not in code, (part, call)
    # a dot is offered only for a step already seen (or none while busy); Back is not on the first step
    foot = _code(_function(JS, "footer"))
    assert "e.dots.forEach((d, i) => { d.disabled = busy || i > W.seen; });" in foot
    assert "e.back.hidden = W.step === 0;" in foot
    assert 'e.next.textContent = W.step === STEPS.length - 1 ? "Done" : "Next";' in foot


def test_skip_sits_in_the_head_and_ends_the_page():
    build = _code(_function(JS, "build"))
    skip = re.search(r'const skip = h\("button", \{ class: "wl-skip"[^}]*onclick: \(\) => safe\(finish\) \}, "Skip"\);', build)
    assert skip, "Skip ends the page (finish)"
    head = build[build.index('h("header", { class: "wl-head" }'):build.index('h("div", { class: "wl-steps" }')]
    assert re.search(r"\bskip\)", head), "Skip stands in the head"
    assert "onclick: () => safe(goTo, i)" in build   # a dot goes to its step by goTo, its rules
    assert 'btn("Back", "", () => goTo(W.step - 1)' in build   # outlined, as every action
    assert 'btn("Next", "primary", () => forward()' in build


def test_the_step_dots_are_targets_of_their_own():
    css = re.sub(r"/\*.*?\*/", "", _read("welcome.css"), flags=re.S)
    assert re.search(r"\.wl-dot \{ width: 30px; height: 30px;", css)
    touch = css[css.index("@media (pointer: coarse)"):]
    assert ".wl-dot { width: 40px; height: 40px; }" in touch
    assert re.search(r"\.wl-skip[^{]*\{ min-height: 40px; \}", touch)
    # welcome.css comes last, so a dot's size in app.css would never apply: it lives here alone
    assert ".wl-dot" not in re.sub(r"/\*.*?\*/", "", _read("app.css"), flags=re.S)


def test_the_card_is_centred_on_a_tall_window_and_nothing_moves_from_step_to_step():
    """One height for the column on every step (the tallest step's), centred by its auto margins when the window has
    room, and the window's height from the top when it has not: the head, the dots and Skip stay in one place, and the
    page never scrolls (only the list in the card does)."""
    css = re.sub(r"/\*.*?\*/", "", _read("welcome.css"), flags=re.S)
    page = re.search(r"\.welcome \{([^}]*)\}", css).group(1)
    assert "display: flex;" in page and "flex-direction: column;" in page and "inset: 0;" in page
    col = re.search(r"\.wl-col \{([^}]*)\}", css).group(1)
    assert re.search(r"--wl-h: (\d+)px;", col) and int(re.search(r"--wl-h: (\d+)px;", col).group(1)) >= 744
    assert "height: min(100%, var(--wl-h));" in col and "margin: auto;" in col and "flex: none;" in col
    assert "min-height: 420px;" in col   # below its floor the welcome scrolls as a whole rather than clipping
    # with the scripts in one column (a phone) the tallest step is taller: the phone keeps its whole height for it
    narrow = css[css.index("@media (max-width: 639.98px)"):]
    assert int(re.search(r"\.wl-col \{ --wl-h: (\d+)px; \}", narrow[:narrow.index("\n}")]).group(1)) >= 1000
    # a tablet's 40 px dots, Skip and Next need a little more; never the phone's (its own query comes first)
    touch = css[css.index("@media (pointer: coarse) and (min-width: 640px)"):]
    assert int(re.search(r"\.wl-col \{ --wl-h: (\d+)px; \}", touch[:touch.index("\n}")]).group(1)) >= 776
    # nothing in the steps sets the column's height, so no step moves it
    assert "wl-col" not in _code(_function(JS, "render")) and ".style.height" not in _code(JS)


def test_a_lab_with_more_scripts_makes_the_column_taller_once_on_the_first_step():
    """--wl-h fits 15 scripts; a lab with more (thirty, two folders) would see its list cut on a tall window. The
    first step makes the column as tall as its list needs when the list comes in: only there, and never shorter, so the
    head, the dots and Skip still stand in one place from step to step."""
    fit = _code(_function(JS, "fitColumn"))
    assert 'if (W.step !== 0 || !region || !region.clientHeight) return;' in fit
    assert "const over = region.scrollHeight - region.clientHeight;" in fit and "if (over <= 1) return;" in fit
    assert "const z = Number(K.zoom && K.zoom()) || 1" in fit   # the screen's pixels, divided by the page size
    assert 'if (need > parseFloat(cs.getPropertyValue("--wl-h"))) col.style.setProperty("--wl-h",' in fit
    assert "48 - parseFloat(cs.paddingTop)" in fit and "clamp(12px, 4 * var(--vh), 48px)" in _read("welcome.css")
    # the list alone gives way when the column is short, so what it lacks is all the column lacks
    assert ".wl-block > .wl-row { flex: none; }" in _read("welcome.css")
    # measured when a step is drawn and when the scripts come in; nothing else sets it
    assert "fitColumn();" in _code(_function(JS, "render")) and "fitColumn();" in _code(_function(JS, "loadFiles"))
    assert _code(JS).count("--wl-h") == 2 and _code(JS).count("fitColumn()") == 3
