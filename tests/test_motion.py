"""Motion, as labMaster moves: what switches on one curve (quick out, a long settle), what appears on another. A
switch's raised thumb slides to the one turned on; a top-bar button's name eases in under it (the buttons never move);
what a fold opens, a panel, a view, a dialog, a block's editor and a new block come in; a block that moves glides from
where it stood. None of it when the person asks for less motion, and nothing waits on requestAnimationFrame (paused
in a hidden pane)."""
import pytest

pytest.importorskip("starlette")

from labharness.web.app import STATIC  # noqa: E402

JS = (STATIC / "app.js").read_text(encoding="utf-8")
SEQ = (STATIC / "seq.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
SEQ_CSS = (STATIC / "seq.css").read_text(encoding="utf-8")


def _fn(src, name):
    start = src.index("function %s(" % name)
    return src[start:src.index("\n  }\n", start)]


def test_a_switch_thumb_slides_to_the_one_turned_on():
    thumb = _fn(JS, "thumb")
    for text in ('seg.querySelector(":scope > button.on")', "on.offsetLeft", "on.offsetWidth", 'classList.add("has-thumb")',
                 "new MutationObserver(place)", "new ResizeObserver(place)", 't.classList.add("still")'):
        assert text in thumb, text
    assert 'for (const seg of document.querySelectorAll(".mode-seg, .seg")) thumb(seg);' in _fn(JS, "initMotion")
    assert "initMotion();" in JS and "thumb," in JS[JS.index("window.Kelvin = {"):]   # seq.js's switches too
    assert "K.thumb(box)" in SEQ   # the step's kind: Set | Wait | Settle
    motion = CSS[CSS.index("/* motion, as labMaster moves"):]
    assert ":root { --ease-switch: cubic-bezier(.2, .75, .2, 1); --ease-in: cubic-bezier(.2, 0, 0, 1); }" in motion
    assert "transition: transform .42s var(--ease-switch), width .42s var(--ease-switch)" in motion
    assert ".seg-thumb.still { transition: none; }" in motion   # placed, not slid, when it comes into sight
    assert ":is(#mode-seg, #runs-seg, .seg).has-thumb > button.on { background: none;" in motion
    # nothing moves for the keyboard: the arrows on Chat | Home move its thumb still, as segKeys does
    keys = _fn(JS, "initMode")
    keys = keys[keys.index('$("mode-seg").addEventListener("keydown"'):]
    assert keys.index('t.classList.add("still")') < keys.index("setMode(")


def test_the_top_bar_buttons_never_move_and_say_their_names():
    init = _fn(JS, "initMotion")
    assert 'querySelectorAll(".topbar-right .pill")' in init and "p.dataset.tip = p.title;" in init
    assert 'removeAttribute("title")' in init and 'attributeFilter: ["title"]' in init   # Incognito's changes
    motion = CSS[CSS.index("/* motion, as labMaster moves"):]
    tip = motion[motion.index(".topbar-right .pill[data-tip]::after, #toggle-sidebar[data-tip]::after {"):]
    assert "content: attr(data-tip); position: absolute;" in tip and "pointer-events: none" in tip
    assert "transition-delay: .12s" in motion
    assert (".topbar .topbar-right .pill { transition: background-color .25s ease, color .25s ease, border-color .25s ease, "
            "box-shadow .25s ease; }") in motion   # their colours fade; nothing that moves them
    # the menu button's name (Conversations, always) too, in the same tip, from its left at the window's edge
    assert "b.dataset.tip = say;" in _fn(JS, "syncSide")
    assert "#toggle-sidebar { position: relative; }" in motion
    assert "#toggle-sidebar[data-tip]::after { left: 0; transform: translate(0, -4px); }" in motion
    assert "#toggle-sidebar[data-tip]:focus-visible::after { opacity: 1; transform: none;" in motion
    # on hover only where a pointer hovers: a touch screen keeps :hover on what was tapped, and the name would stay
    hover = motion[motion.index("@media (hover: hover) {"):]
    hover = hover[:hover.index("\n}\n")]
    for text in (".topbar-right .pill[data-tip]:hover::after { opacity: 1;", "#toggle-sidebar[data-tip]:hover::after { opacity: 1;"):
        assert text in hover, text
    assert ":is(:hover, :focus-visible)::after" not in motion


def test_what_comes_in_and_what_moves():
    motion = CSS[CSS.index("/* motion, as labMaster moves"):]
    for text in (":is(#runpane, #edpane, #anpane, #termpane, #labpanel, #teampanel) { animation: k-pane-in",
                 ":is(#seq-view, #history-view, .run-view, #homeview, #scroller, .seq-fns) { animation: k-view-in",
                 "dialog[open] { animation: k-pop-in", ".k-in, .seq-ed.seq-ed-in { animation: k-reveal",
                 ".seq-card.seq-in { animation: k-card-in"):
        assert text in motion, text
    # a view swaps at once and only fades in (.15 s); what comes in takes .2 s or less, with no scale
    assert "@keyframes k-view-in { from { opacity: 0; } }" in motion and "scale(" not in motion
    assert "k-view-in .15s" in motion and "k-pop-in .2s" in motion and "k-card-in .2s" in motion
    assert "scroll-behavior: smooth" not in CSS   # a scroll the page asks for (the chat to its end) lands at once
    ease = _fn(JS, "easeIn")
    assert 'el.classList.add("k-in")' in ease and 'el.classList.remove("k-in")' in ease and "e.target !== el" in ease
    assert "if (!folded) easeIn(part);" in JS   # Settings' folds
    # the Lab's parts never move: the one chosen on the strip shows at once, under the strip's thumb, which slides as
    # every .seg's does; nothing in the Lab opens by height
    lab = CSS[CSS.index("/* ===== fit D"):CSS.index("/* ===== end fit D")]
    assert "#labpanel .lab-part:not(.chosen) { display: none; }" in lab and "grid-template-rows ." not in lab
    assert 'id="lab-strip" class="seg lab-strip"' in (STATIC / "index.html").read_text(encoding="utf-8")
    flip_from, flip_to = _fn(SEQ, "flipFrom"), _fn(SEQ, "flipTo")
    assert "if (!SEQ.drawn || calm()) return null;" in flip_from and "getBoundingClientRect().top" in flip_from
    assert "c.el.animate(" in flip_to and "rise(c.el)" in flip_to and "id === skip" in flip_to
    assert "const stood = flipFrom(list);" in _fn(SEQ, "renderList") and "flipTo(stood);" in _fn(SEQ, "renderList")
    assert "SEQ.drawn = false;" in _fn(SEQ, "onSeq")   # drawn as it is when it comes into sight
    assert 'SEQ.opening === it.id' in _fn(SEQ, "fillCard")   # an editor eases open when opened, not on every redraw


def test_none_of_it_when_the_person_asks_for_less_motion():
    motion = CSS[CSS.index("/* motion, as labMaster moves"):]
    calm = motion[motion.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".seg-thumb, .topbar-right .pill[data-tip]::after, #toggle-sidebar[data-tip]::after" in calm
    assert "transition: none" in calm
    assert "dialog[open]" in calm and "animation: none" in calm and ".seq-card" in calm
    # nothing slides, pulses, spins or grows; colours fade over .15 s or less (reduced motion)
    for text in (".toast { transform: translate(-50%, 0); transition: opacity .15s; }",
                 ".live-dot, #open-history.live::after { animation: none; }", ".spinner { animation: none;",
                 ".bar i, .setup-wait-bar span { transition: none; }", "button.link { transition-duration: .15s; }"):
        assert text in calm, text
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in SEQ
    for src, names in ((JS, ("thumb", "easeIn", "initMotion")), (SEQ, ("flipFrom", "flipTo", "rise", "stepKinds"))):
        for name in names:
            assert "requestAnimationFrame" not in _fn(src, name), name
    assert ".seq-step-head { display: flex;" in SEQ_CSS   # the switch and Add on one line, the fields under them
