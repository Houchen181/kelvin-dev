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
    assert 'btn("Back", "ghost", () => goTo(W.step - 1)' in build
    assert 'btn("Next", "primary", () => forward()' in build


def test_the_step_dots_are_targets_of_their_own():
    css = re.sub(r"/\*.*?\*/", "", _read("welcome.css"), flags=re.S)
    assert re.search(r"\.wl-dot \{ width: 30px; height: 30px;", css)
    touch = css[css.index("@media (pointer: coarse)"):]
    assert ".wl-dot { width: 40px; height: 40px; }" in touch
    assert re.search(r"\.wl-skip[^{]*\{ min-height: 40px; \}", touch)
    # welcome.css comes last, so a dot's size in app.css would never apply: it lives here alone
    assert ".wl-dot" not in re.sub(r"/\*.*?\*/", "", _read("app.css"), flags=re.S)
