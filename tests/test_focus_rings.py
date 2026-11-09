"""The focus ring is the keyboard's. A press of the mouse or a finger draws no ring, glow or coloured border on what it
pressed (a button, a list, a switch, a tab, a menu item), nor on what the page gives the focus after it (a menu closing
back onto its button, a dialog's first button): app.js marks <html> k-pointer on a pointer press and clears it on the
next key, and app.css then takes every outline away and leaves out the glows drawn for the keyboard. A text field
keeps its caret and its border colour while it is typed in, and the message box's border follows its field, never a
button of its row; a checkbox or radio pressed keeps no glow. Shift, Ctrl, Alt or the Windows key leave the mark (Alt+Tab
away and back is not the keyboard moving about the page), and a ring drawn by a hover waits for a pointer that hovers,
as a touch screen keeps :hover on what was tapped. Checked in the page's own files."""
import re
from pathlib import Path

import labharness

STATIC = Path(labharness.__file__).parent / "web" / "static"
STYLES = ("app.css", "home.css", "seq.css", "welcome.css")
# what a rule draws when it marks the focus: a ring, a glow, a coloured border or fill, or a tip shown
DRAWS = re.compile(r"(?:^|;)\s*(outline(?:-color)?|box-shadow|border(?:-color)?|background(?:-color)?)\s*:\s*([^;]+)")
TEXT_TYPE = re.compile(r'\[type="?(?:text|search|number|email|url|password|tel)"?\]')
NO_BOX = re.compile(r':not\([^()]*\[type="?checkbox"?\][^()]*\[type="?radio"?\]')


def _text_field(compound):
    """A compound that matches a text field alone, where the focus shows while it is typed in: a textarea, a field class
    of the page's own, or an input that names a text type or leaves out the checkbox and the radio (a bare input may be
    a box, which a press would leave glowing). Never a list, a button or a box."""
    if re.match(r"textarea\b|\.(?:side-input|seq-name|pane-filter)\b|\.(?:kfield|ed-in):where\(:not\(select\)\)", compound):
        return True
    return bool(re.match(r"input\b", compound) and (TEXT_TYPE.search(compound) or NO_BOX.search(compound)))


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _rules(css):
    """Every style rule (nested ones too), without comments: (selector, declarations)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, stack, start = [], [], 0
    for i, c in enumerate(css):
        if c == "{":
            stack.append([css[start:i].strip(), i + 1, False])
            start = i + 1
        elif c == "}":
            prelude, body, nested = stack.pop()
            if stack:
                stack[-1][2] = True
            if not nested and not prelude.startswith("@"):
                out.append((" ".join(prelude.split()), css[body:i]))
            start = i + 1
    return out


def _split(text, sep):
    """text split at sep where no bracket is open (a selector list, or a selector's compounds)."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(text):
        depth += c in "([" or -(c in ")]")
        if depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            start = i + len(sep)
    return [p.strip() for p in parts + [text[start:]] if p.strip()]


def _drawn(decls):
    """What the declarations draw: (property, value), leaving out what only takes a mark away."""
    return [(p, v.strip()) for p, v in DRAWS.findall(decls)
            if not re.fullmatch(r"(?:none|0|0px|transparent)(?:\s*!important)?", v.strip())]


def _all_rules():
    return [(name, sel, decls) for name in STYLES for sel, decls in _rules(_read(name))]


def test_a_press_marks_the_page_and_the_next_key_clears_it():
    js = _read("app.js")
    assert ('document.addEventListener("pointerdown", () => document.documentElement.classList.add("k-pointer"), true);'
            in js)
    keys = js[js.index('document.addEventListener("keydown", (e) => {'):]
    keys = keys[:keys.index("}, true);")]
    # a modifier alone or held keeps the mark: Alt+Tab away and back does not ring a list picked with the mouse
    assert ("if (e.ctrlKey || e.metaKey || e.altKey || /^(Shift|Control|Alt|AltGraph|Meta|OS|CapsLock)$/.test(e.key)) return;"
            in keys)
    assert keys.index("return;") < keys.index('document.documentElement.classList.remove("k-pointer");')
    # marked, every outline goes, over each control's own ring; a text field and a drop target keep theirs
    rule = [(s, d) for s, d in _rules(_read("app.css")) if s.startswith("html.k-pointer :focus-visible")]
    assert len(rule) == 1, rule
    sel, decls = rule[0]
    assert "outline: none !important" in decls
    assert "textarea" in sel and 'input:is(:not([type]), [type="text"]' in sel and ".drop-on" in sel


def _plain_focus_rings(rules):
    """The selectors that draw on plain :focus on something that is not a text field alone."""
    bad = []
    for name, sel, decls in rules:
        if not _drawn(decls):
            continue
        for one in _split(sel, ","):
            for compound in _split(re.sub(r"\s*[>+~]\s*", " ", one), " "):
                # :not(:focus) marks nothing; a box that holds a text field (the message box) follows that field
                plain = re.sub(r":not\([^()]*\)|:has\((?:#input|textarea|input)\b[^()]*\)", "", compound)
                if re.search(r":focus(?![-\w])", plain) and not _text_field(compound):
                    bad.append((name, one))
    return bad


def test_no_plain_focus_rule_rings_a_control_pressed_by_the_mouse():
    """:focus (not -visible) matches after a mouse press: it may draw only on a text field."""
    bad = _plain_focus_rings(_all_rules())
    assert not bad, bad


def test_the_check_takes_a_bare_input_for_one_that_may_be_a_checkbox():
    """A checkbox or a radio takes the focus when it is clicked: a glow on a bare input would stay after the press."""
    glow = "border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft);"
    for bad in (".field input:focus", '.field input[type="checkbox"]:focus', "button:focus", ".menu-item:focus"):
        assert _plain_focus_rings([("x", bad, glow)]), bad
    for ok in ('.field input:where(:not([type="checkbox"], [type="radio"], [type="range"])):focus',
               '.x input[type="search"]:focus', ".field textarea:focus", ".kfield:where(:not(select)):focus"):
        assert not _plain_focus_rings([("x", ok, glow)]), ok
    # the page's field glow: a text field's alone, every box and round choice left out
    css = _read("app.css")
    assert (':is(.field, .add-row, .inst-tools) input:where(:not([type="checkbox"], [type="radio"], [type="range"], '
            '[type="file"], [type="color"])):focus,') in css
    assert ".field input:focus" not in css


def test_a_ring_drawn_by_a_hover_waits_for_a_pointer_that_hovers():
    """A touch screen keeps :hover on what was tapped: the notice row's "n more" and Home's tiles ring only under a
    pointer that hovers."""
    for name, sel in (("app.css", ".alert-more:hover"), ("home.css", ".home-tile:hover")):
        css = re.sub(r"/\*.*?\*/", "", _read(name), flags=re.S)
        assert css.count(sel) == 1, (name, sel)
        at = css.index(sel)
        block = css[css.rindex("@media", 0, at):at]
        assert block.startswith("@media (hover: hover) {") and "}" not in block, (name, sel)


def test_nothing_rings_because_the_focus_is_somewhere_inside():
    """:focus-within matches after a press on any button inside: it draws nothing (the message box follows its field)."""
    for name, sel, decls in _all_rules():
        if ":focus-within" in sel:
            assert not [p for p, _ in _drawn(decls)], (name, sel)
    css = _read("app.css")
    assert ".composer:focus-within" not in css
    assert ".composer:has(#input:focus) { border-color:" in css


def test_a_glow_a_fill_or_a_tip_for_the_focus_is_drawn_for_the_keyboard_only():
    """:focus-visible matches a list (select) even after a mouse press, and after a press the page may move the focus
    by script: whatever it draws besides the outline (which k-pointer takes away) waits for a key."""
    bad = []
    for name, sel, decls in _all_rules():
        for one in _split(sel, ","):
            if ":focus-visible" not in one:
                continue
            drawn = [p for p, _ in _drawn(decls) if not p.startswith("outline")]
            tip = "::after" in one and re.search(r"opacity\s*:\s*1", decls)
            if (drawn or tip) and "html:not(.k-pointer)" not in one:
                bad.append((name, one))
    assert not bad, bad


def test_every_list_rings_for_the_keyboard_and_keeps_its_border_for_the_mouse():
    css = _read("app.css")
    for sel in (":where(html:not(.k-pointer)) :is(.field select, .add-row select, .mount-select):focus-visible",
                ":where(html:not(.k-pointer)) .pane-bar select:focus-visible",
                ":where(html:not(.k-pointer) select).ed-in:focus-visible",
                ":where(html:not(.k-pointer) select).kfield:focus-visible"):
        assert sel in css, sel
    # a text field still shows it is typed in, and a field said to be wrong stays red when it has the focus
    assert ".kfield:where(:not(select)):focus" in css and ".ed-in:where(:not(select)):focus" in css
    rules = dict(_rules(css))
    assert "border-color: var(--danger)" in rules[".kfield.bad"]
    assert list(rules).index(".kfield.bad") > list(rules).index(
        ".kfield:where(:not(select)):focus, :where(html:not(.k-pointer) select).kfield:focus-visible")
