"""The focus ring is the keyboard's. A press of the mouse or a finger draws no ring, glow or coloured border on what it
pressed (a button, a list, a switch, a tab, a menu item), nor on what the page gives the focus after it (a menu closing
back onto its button, a dialog's first button): app.js marks <html> k-pointer on a pointer press and clears it on the
next key, and app.css then takes every outline away and leaves out the glows drawn for the keyboard. A text field
keeps its caret and its border colour while it is typed in, and the message box's border follows its field, never a
button of its row. Checked in the page's own files."""
import re
from pathlib import Path

import labharness

STATIC = Path(labharness.__file__).parent / "web" / "static"
STYLES = ("app.css", "home.css", "seq.css", "welcome.css")
# what a rule draws when it marks the focus: a ring, a glow, a coloured border or fill, or a tip shown
DRAWS = re.compile(r"(?:^|;)\s*(outline(?:-color)?|box-shadow|border(?:-color)?|background(?:-color)?)\s*:\s*([^;]+)")
# a text field, where the focus is shown while it is typed in: never a list, a button or a box
TEXT_FIELD = re.compile(r"^(?:input(?![^\s]*\[type=\"?(?:checkbox|radio|range|button|submit|file|color))|textarea|"
                        r"\.(?:side-input|seq-name|pane-filter)\b|\.(?:kfield|ed-in):where\(:not\(select\)\))")


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
    assert ('document.addEventListener("keydown", () => document.documentElement.classList.remove("k-pointer"), true);'
            in js)
    # marked, every outline goes, over each control's own ring; a text field and a drop target keep theirs
    rule = [(s, d) for s, d in _rules(_read("app.css")) if s.startswith("html.k-pointer :focus-visible")]
    assert len(rule) == 1, rule
    sel, decls = rule[0]
    assert "outline: none !important" in decls
    assert "textarea" in sel and 'input:is(:not([type]), [type="text"]' in sel and ".drop-on" in sel


def test_no_plain_focus_rule_rings_a_control_pressed_by_the_mouse():
    """:focus (not -visible) matches after a mouse press: it may draw only on a text field."""
    bad = []
    for name, sel, decls in _all_rules():
        if not _drawn(decls):
            continue
        for one in _split(sel, ","):
            for compound in _split(re.sub(r"\s*[>+~]\s*", " ", one), " "):
                # :not(:focus) marks nothing; a box that holds a text field (the message box) follows that field
                plain = re.sub(r":not\([^()]*\)|:has\((?:#input|textarea|input)\b[^()]*\)", "", compound)
                if re.search(r":focus(?![-\w])", plain) and not TEXT_FIELD.match(compound):
                    bad.append((name, one))
    assert not bad, bad


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
                ":where(html:not(.k-pointer)) .an-body select:focus-visible",
                ":where(html:not(.k-pointer) select).ed-in:focus-visible",
                ":where(html:not(.k-pointer) select).kfield:focus-visible"):
        assert sel in css, sel
    # a text field still shows it is typed in, and a field said to be wrong stays red when it has the focus
    assert ".kfield:where(:not(select)):focus" in css and ".ed-in:where(:not(select)):focus" in css
    rules = dict(_rules(css))
    assert "border-color: var(--danger)" in rules[".kfield.bad"]
    assert list(rules).index(".kfield.bad") > list(rules).index(
        ".kfield:where(:not(select)):focus, :where(html:not(.k-pointer) select).kfield:focus-visible")
