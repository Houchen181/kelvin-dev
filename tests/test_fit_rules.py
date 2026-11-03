"""The rules that keep every view of the page fitting its window (the design brief of 2-3 Nov 2026, 1.1 and 1.7),
checked in the page's own files. Questions are asked in the page (Kelvin.askYes, Kelvin.askText), never by the
browser's confirm() or prompt(). Nothing is offset by the old top bar's 57 px: the bar measures itself (--topbar-h).
A length taken from the window is in the page's pixels, calc(N * var(--vh)) or var(--vw), since the page size is a CSS
zoom on <html> that multiplies a raw vh. There is one switch: a button.switch with role=switch. Only a named region
(data-region) scrolls, so each one gets the thin scrollbar and the soft edge where it is cut; what else may scroll is
listed below with its reason. The density tier on <html> (fit-roomy, fit-compact, fit-tight) is set by fitTiers alone,
so it can never disagree with the page size. The morning's variants come from ?v= in the query string (the hash holds
the key), kept as lh.v and shown as html[data-v1] to [data-v4], and each variant's CSS is fenced, so the ones not
chosen can be deleted in one pass."""
import re
from pathlib import Path

import labharness

STATIC = Path(labharness.__file__).parent / "web" / "static"
SCRIPTS = ("app.js", "seq.js", "home.js", "welcome.js")
STYLES = ("app.css", "home.css", "seq.css", "welcome.css")
TIERS = ("fit-roomy", "fit-compact", "fit-tight")

# What may scroll without being a region, and why. Anything else that scrolls carries data-region.
SCROLLS_IN_PLACE = {
    # inside a region, capped in height: code, logs and tables that scroll where they stand (as a region inside a
    # region they would hold the wheel at their ends, and the chat or the run would stop scrolling under the pointer)
    ".cu-log": "a step's lines in the chat",
    ".step-body pre": "a tool step's input and result in the chat",
    ".approval-code": "a command on an approval card",
    ".results": "a run card's results table in the chat",
    ".lv-plan": "a LabVIEW VI read as a plan, in its dialog's body",
    ".exp-code": "an experiment's code, in its dialog's body",
    ".know-body": "a skill's steps on a Settings page",
    ".ra-conds": "Run again's settings table, in the run's region",
    ".lib-plan": "a library scan's plan",
    ".rv-plan pre": "a run's plan, in the run's region",
    ".diff": "a lab config version's diff, in the history list",
    # menus float over the page, outside the frame, each capped at its own height
    ".menu": "popMenu",
    ".slash-menu": "the skills offered after /",
    ".pane-menu": "a pane's own menu (compare, the run picker, a second opinion's models)",
    # the frame's own last resort below the floor (brief 1.1.4): at the promised sizes they never scroll
    ".homeview": "Home scrolls only if even its one-line tiles cannot fit: it never clips",
    ".welcome": "the welcome page, over the whole window, below the floor",
    ".dialog form:not(:has(> .dialog-body, > .set-body))": "a dialog made without a body scrolls as a whole",
    # the schedule as 0.3.39 drew it: kept on the page, never shown
    ".sched-list": "the old schedule, never shown",
    ".queue-view > .q-list": "the old queue, never shown",
    # one element a rule above lands on (.pane-body): the Runs pane's body holds the Sequence, History and the runs,
    # each fitting it with regions of its own; it scrolls only if one of them does not fit, which fitcheck then reports
    "#run-body": "the Runs pane's body, around its regions",
}


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _uncommented_css(name):
    return re.sub(r"/\*.*?\*/", "", _read(name), flags=re.S)


def _split_top(text, sep):
    """text split at sep where no bracket is open (a selector list, or a selector's compounds)."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        found = depth == 0 and i >= start and re.match(sep, text[i:])
        if found:
            parts.append(text[start:i])
            start = i + len(found.group(0))
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _rules(css):
    """Every style rule of a stylesheet without comments, nested ones (in @media, @container, @supports) too:
    (selector, declarations)."""
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


def _function(js, name):
    """The body of a function of a script, by its name (braces counted; enough for the functions asked for here)."""
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


def _elements():
    """Each element the page and its scripts make, as (its classes and id as .class / #id, carries data-region)."""
    out = []
    for m in re.finditer(r"<[a-zA-Z][\w-]*(\s[^>]*)?>", _read("index.html")):
        attrs = m.group(1) or ""
        cls = re.search(r'\bclass="([^"]*)"', attrs)
        names = {"." + c for c in (cls.group(1).split() if cls else [])}
        ident = re.search(r'\bid="([^"]*)"', attrs)
        names |= {"#" + ident.group(1)} if ident else set()
        out.append((names, "data-region=" in attrs))
    for name in SCRIPTS:
        for m in re.finditer(r'\bh\(\s*"[\w-]+"\s*,\s*\{([^{}]*)\}', _read(name)):
            attrs = m.group(1)
            cls = re.search(r'\bclass:\s*"([^"]*)"', attrs)
            names = {"." + c for c in (cls.group(1).split() if cls else [])}
            ident = re.search(r'\bid:\s*"([^"]*)"', attrs)
            names |= {"#" + ident.group(1)} if ident else set()
            out.append((names, '"data-region"' in attrs))
    return out


def test_questions_are_asked_in_the_page_never_by_the_browser():
    for name in SCRIPTS + ("index.html",):
        text = _read(name)
        assert not re.findall(r"(?<![\w$.])(?:confirm|prompt)\s*\(|\bwindow\.(?:confirm|prompt)\b", text), name
    js = _read("app.js")
    assert "askYes, askText" in js[js.index("window.Kelvin = {"):]   # lent to home.js, seq.js and welcome.js
    ask = _function(js, "askDialog")
    # a click in the first 0.4 s, or the second of a double-click, is no answer; Esc means no; the backdrop only when
    # the press started on it; Cancel has the focus when OK cannot be undone
    assert "Date.now() - opened < 400" in ask and "e.detail > 1" in ask
    assert 'addEventListener("cancel", (e) => { e.preventDefault(); finish(none); })' in ask
    assert "downOut = e.target === dlg && outside(e)" in ask and "e.target === dlg && downOut" in ask
    assert '(o.danger && o.focus !== "ok") ? no : yes' in ask
    parts = _function(js, "askParts")
    assert 'h("h2", { id }, head)' in parts and '"data-region": "dialog"' in parts   # the first line is the heading


def test_nothing_is_offset_by_the_old_top_bar():
    for name in SCRIPTS + STYLES + ("index.html",):
        assert "57px" not in _read(name), name
    css = _read("app.css")
    assert "padding-top: var(--chrome-h)" in css   # the frame starts below the top bar and the notice row, measured


def test_a_length_from_the_window_is_in_the_pages_pixels():
    css = _read("app.css")
    assert "--vh: calc(1dvh / var(--zoom));" in css and "--vw: calc(1vw / var(--zoom));" in css
    raw = re.compile(r"(?<![\w-])\d*\.?\d+(?:d|s|l)?v(?:h|w|min|max)\b")
    for name in STYLES + ("index.html",) + SCRIPTS:
        text = _uncommented_css(name) if name.endswith(".css") else _read(name)
        text = re.sub(r"--v[hw]:[^;]*;|@supports[^{]*", "", text)   # the two tokens themselves, and their fallback's test
        text = re.sub(r"var\(--v([hw]), 1v\1\)", "", text)   # a token with its fallback
        assert not raw.findall(text), (name, sorted({m.group(0) for m in raw.finditer(text)}))


def test_there_is_one_switch():
    js = "".join(_read(name) for name in SCRIPTS)
    page = _read("index.html")
    switches = re.findall(r'<(\w+)\s[^>]*\brole="switch"[^>]*>', page)
    tags = re.findall(r'<(\w+)\s([^>]*)>', page)
    for tag, attrs in tags:
        cls = (re.search(r'\bclass="([^"]*)"', attrs) or [None, ""])[1].split()
        assert ("switch" in cls) == ('role="switch"' in attrs), attrs   # the one look and its role go together
        if "switch" in cls:
            assert tag == "button" and "aria-checked=" in attrs, attrs
    made = re.findall(r'\bh\(\s*"([\w-]+)"\s*,\s*\{([^{}]*)\}', js)
    for tag, attrs in made:
        cls = (re.search(r'\bclass:\s*"([^"]*)"', attrs) or [None, ""])[1].split()
        assert ("switch" in cls) == ('role: "switch"' in attrs), attrs
        if "switch" in cls:
            assert tag == "button" and '"aria-checked"' in attrs, attrs
    assert len(switches) >= 2 and sum('role: "switch"' in a for _, a in made) >= 3
    assert 'type="checkbox" role="switch"' not in page and "appearance: none" not in _read("app.css")
    # the switch is drawn once: its track and its thumb, at the kit's end; the others only place it
    drawn = [sel for sel, body in _rules(_uncommented_css("app.css")) if re.search(r"(?<![\w-])\.switch(?![\w-])", sel)]
    assert ".switch" in drawn and ".switch::before" in drawn and ".switch > span" in drawn
    for name in STYLES:
        for sel, body in _rules(_uncommented_css(name)):
            assert "role=switch" not in sel.replace('"', "") or ".switch" in sel, (name, sel)


def test_only_a_named_region_scrolls():
    elements = _elements()
    names = {m for m in re.findall(r'data-region(?:="|": ")([\w-]+)"', "".join(_read(n) for n in SCRIPTS + ("index.html",)))}
    assert {"chat", "home-blocks", "seq-blocks", "seq-fns", "history", "run", "lab-part", "team", "dialog",
            "settings-page", "welcome-list", "editor"} <= names
    assert re.search(r"\[data-region\] \{ overflow: auto; min-height: 0;", _read("app.css"))
    unknown = []
    for name in STYLES:
        for sel, body in _rules(_uncommented_css(name)):
            decls = dict((k.strip(), v.strip()) for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d))
            if not any(re.search(r"\b(auto|scroll)\b", decls.get(k, "")) for k in ("overflow", "overflow-y")):
                continue   # it never scrolls up and down (a code line that scrolls sideways may)
            for one in _split_top(sel, ","):
                if "[data-region" in one or one in SCROLLS_IN_PLACE:
                    continue
                subject = re.sub(r":{1,2}[\w-]+(\((?:[^()]|\([^()]*\))*\))?", "", _split_top(one, r"\s*[>+~]\s*|\s+")[-1])
                tokens = set(re.findall(r"[.#][\w-]+", subject))
                carriers = [marked for classes, marked in elements
                            if tokens and tokens <= classes and not classes & set(SCROLLS_IN_PLACE)]
                if not (carriers and all(carriers)):
                    unknown.append((name, one))
    assert not unknown, unknown   # each scrolls without data-region: mark its element, or say here why not
    for sel in SCROLLS_IN_PLACE:   # nothing stale kept here
        if sel.startswith("#"):
            assert 'id="%s"' % sel[1:] in _read("index.html") and "data-region" not in re.search(
                r'<[^>]*\bid="%s"[^>]*>' % sel[1:], _read("index.html")).group(0), sel
        else:
            assert any(sel in _split_top(s, ",") for name in STYLES for s, _ in _rules(_uncommented_css(name))), sel


def test_no_function_is_declared_twice():
    # parts written side by side: a second `function name(` in the same script silently replaces the first (both are
    # hoisted), as the Lab's segKeys(seg, choose) once replaced the Runs tabs' segKeys(e), whose arrow keys then threw
    for name in SCRIPTS:
        names = re.findall(r"^\s*(?:async\s+)?function\s+([\w$]+)\s*\(", _read(name), flags=re.M)
        assert not sorted({n for n in names if names.count(n) > 1}), name


def test_the_density_tier_is_set_by_fit_tiers_alone():
    js = _read("app.js")
    fit = _function(js, "fitTiers")
    assert all(t in fit for t in TIERS) and 'root.classList.remove("fit-roomy", "fit-compact", "fit-tight");' in fit
    assert "window.innerHeight / z" in fit   # the layout's height: the real window's over the page size
    assert re.search(r'addEventListener\("resize", \(\) => \{ clearTimeout\(FIT\.timer\); FIT\.timer = setTimeout\(\(\) => '
                     r'\{ fitTiers\(\);', js) and "\n  fitTiers();\n" in js   # on loading, and 50 ms after a resize
    rest = js.replace(fit, "") + "".join(_read(name) for name in SCRIPTS[1:])
    writes = re.findall(r"classList\.(?:add|remove|toggle|replace)\(([^)]*)\)|className\s*\+?=([^;\n]*)|"
                        r"setAttribute\(\s*\"class\"([^)]*)\)", rest)
    assert not [w for w in writes if any(t in "".join(w) for t in TIERS)]
    assert not re.search(r'class="[^"]*\bfit-(?:roomy|compact|tight)\b', _read("index.html"))
    for name in SCRIPTS:   # read anywhere, set nowhere else
        for m in re.finditer(r"fit-(?:roomy|compact|tight)", _read(name).replace(fit, "")):
            line = _read(name).replace(fit, "")[:m.end()].rsplit("\n", 1)[-1]
            assert "classList.contains(" in line or line.lstrip().startswith("//"), (name, line)


def test_the_variants_come_from_the_query_string_and_are_fenced():
    js = _read("app.js")
    read = _function(js, "readVariants")
    assert 'new URLSearchParams(location.search).get("v")' in read and "location.hash" not in read
    assert 'store("lh.v", given.trim())' in read and 'store("lh.v")' in read   # kept on this browser; ?v= alone forgets
    assert 'setAttribute("data-v" + n, v[n])' in read and "/^([1-4])([A-C])$/" in read
    # what shows without a choice: 1A Steps, 2A one row of three tiles, 3C the Lab's folds (3A is the strip)
    assert re.search(r'const v = \{ 1: "A", 2: "A", 3: "C", 4: "A" \};', read)
    assert "const V = readVariants();" in js and "V, fitTiers" in js[js.index("window.Kelvin = {"):]
    for name in SCRIPTS:
        for n, x in re.findall(r'\bV\[(\d)\] === "(\w)"', _read(name)):
            assert n in "1234" and x in "ABC", (name, n, x)
    for name in STYLES:
        css = _read(name)
        comments = [m.span() for m in re.finditer(r"/\*.*?\*/", css, flags=re.S)]
        opens = re.findall(r"/\* v([1-4][A-C]) \*/", css)
        assert opens == re.findall(r"/\* /v([1-4][A-C]) \*/", css), name   # each fence closed, in order
        for m in re.finditer(r'\[data-v([1-4])="?([A-C])"?\]', css):
            if any(a <= m.start() < b for a, b in comments):
                continue
            v = m.group(1) + m.group(2)
            fence = re.search(r"/\* v%s \*/.*?/\* /v%s \*/" % (v, v), css, flags=re.S)
            assert fence and fence.start() < m.start() < fence.end(), (name, v)
