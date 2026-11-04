"""The rules that keep every view of the page fitting its window (the design brief of 2-3 Nov 2026, 1.1 and 1.7),
checked in the page's own files. Questions are asked in the page (Kelvin.askYes, Kelvin.askText), never by the
browser's confirm() or prompt(). Nothing is offset by the old top bar's 57 px: the bar measures itself (--topbar-h).
A length taken from the window is in the page's pixels, calc(N * var(--vh)) or var(--vw), since the page size is a CSS
zoom on <html> that multiplies a raw vh. There is one switch: a button.switch with role=switch. Only a named region
(data-region) scrolls, so each one gets the thin scrollbar and the soft edge where it is cut; what else may scroll is
listed below with its reason. The density tier on <html> (fit-roomy, fit-compact, fit-tight) is set by fitTiers alone,
so it can never disagree with the page size, which Auto steps by the window (100, 90 or 85 %). The page has one form:
nothing chooses a variant of it."""
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
    # the approvals page loads none of the page's scripts: it asks in its own page too, as askYes does (the PIN, auto mode)
    ui = (Path(labharness.__file__).parent / "agent" / "ui.html").read_text(encoding="utf-8")
    assert not re.findall(r"(?<![\w$.])(?:confirm|prompt)\s*\(|\bwindow\.(?:confirm|prompt)\b", ui)
    ask = _function(ui, "ask")
    assert "e.detail > 1 || Date.now() - opened < 400" in ask and "dlg.showModal();" in ask
    assert 'addEventListener("cancel", (e) => { e.preventDefault(); finish(none); })' in ask
    assert "downOut = e.target === dlg && outside(e)" in ask and "e.target === dlg && downOut" in ask
    assert 'el("h2", { id: "ask-head" }, head)' in ask and '(o.focus === "cancel" ? no : yes)' in ask
    assert 'await ask("Turn on auto mode?' in ui and "alert(" not in ui   # nor a browser box for what went wrong
    # the PIN: the server's reason is the question (its heading, a sentence), and Cancel changes nothing, saying nothing
    pin = _function(ui, "api")
    assert "await ask(why.join(\"\\n\"), { pin: true })" in pin and "s.charAt(0).toUpperCase() + s.slice(1)" in pin
    assert "no.cancelled = true;" in pin and 'if (!err.cancelled) say("Could not change the mode: "' in ui


def test_the_approvals_page_shows_the_mode_that_holds():
    """While a change of the approval mode is on its way (auto mode asked, the PIN asked, then sent), the poll leaves
    the select alone; when it ends, the select shows what holds, though it kept the focus: the mode chosen, or the one
    before after a no or a failure. A state asked for before the change ended is not drawn."""
    ui = (Path(labharness.__file__).parent / "agent" / "ui.html").read_text(encoding="utf-8")
    render = _function(ui, "render")
    assert "if (!MODE.busy && $(\"mode\").value !== MODE.now) $(\"mode\").value = MODE.now;" in render
    assert "document.activeElement" not in render   # the focus no longer decides it: after a question it is back there
    change = ui[ui.index('$("mode").addEventListener("change"'):ui.index('$("stop").addEventListener')]
    assert change.index("MODE.busy = true;") < change.index("await ask(") < change.index('await api("/api/mode"')
    assert 'MODE.now = (await api("/api/mode", { mode })).mode || mode;' in change   # the mode chosen, as the server says
    end = change[change.index("} finally {"):]
    assert "MODE.busy = false;" in end and "MODE.ends += 1;" in end and "sel.value = MODE.now;" in end
    assert "if (ends !== MODE.ends) return;" in _function(ui, "refresh")


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
    assert {"chat", "home-blocks", "seq-blocks", "seq-fns", "run", "lab-part", "team", "dialog",
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


def test_the_page_has_one_form():
    """Nothing chooses a variant of the page: no script reads one from the query string (?v=) or keeps one, <html>
    carries no data-v attribute, nothing reads V[n], no code is fenced as a variant's (/* v2B */ ... /* /v2B */,
    // v3C ... // /v3C), and no variant is named, in the code (v2B) or in a comment (4B alone). An lh.v a browser
    still holds from an earlier version is removed once at load, quietly, and read nowhere."""
    js = _read("app.js")
    for name in SCRIPTS + STYLES + ("index.html",):
        text = _read(name)
        assert not re.findall(r"\bv[1-4][A-D]\b", text), name                         # no variant named
        comments = re.findall(r"//[^\n]*|/\*.*?\*/|<!--.*?-->", text, flags=re.S)
        assert not [c for c in comments if re.search(r"\b[1-4][A-D]\b", c)], name   # nor by its id in a comment
    for name in SCRIPTS + ("index.html",):
        text = _read(name)
        assert "readVariants" not in text and "Kelvin.V" not in text, name
        assert not re.search(r"\bV\[", text), name                                   # no variant read
        assert not re.search(r"data-v\d|dataset\.v\d", text), name                   # no data-v on <html>
        assert not re.search(r"""["']data-v["']\s*\+|documentElement\.(?:setAttribute\(\s*["']data-v|dataset\.v)""",
                             text), name                                             # nor one built from a number
        assert not re.search(r"""\.get\(\s*["']v["']\s*\)|\?v=""", text), name       # no ?v=
        assert not re.search(r"^\s*// /?v\d[A-Z]\b", text, flags=re.M), name         # no fenced variant code
    for name in STYLES:
        css = _read(name)
        assert not re.search(r"\[data-v\d", css), name
        assert not re.search(r"/\* /?v\d[A-Z] \*/", css), name
        assert "?v=" not in css, name
    names = js[js.index("window.Kelvin = {"):]
    names = names[:names.index("};")]
    assert not re.search(r"[{,]\s*V\s*[,}]", re.sub(r"//[^\n]*", "", names))     # no V in the Kelvin namespace
    assert re.findall(r"""["']lh\.v["']""", js) == ['"lh.v"'] and 'localStorage.removeItem("lh.v")' in js


def test_auto_page_size_steps_by_the_window():
    """Auto, the page size without a choice: 100 % on a big window (1600 x 900 or more), on a touch screen and under
    1000 px wide; 85 % under 680 px tall; 90 % otherwise. It reads the real window, which the zoom does not change, so
    it never feeds back on itself. A choice in Settings -> Page size (80 to 150 %) overrides it on this browser."""
    js = _read("app.js")
    auto = _function(js, "autoZoom")
    assert "const w = window.innerWidth, h = window.innerHeight;" in auto
    assert 'if (w < 1000 || window.matchMedia("(pointer: coarse)").matches) return 1;' in auto
    assert re.findall(r"return ([^;]+);", auto) == ["1", "w >= 1600 && h >= 900 ? 1 : h < 680 ? 0.85 : 0.9"]
    assert "pageChoice() || autoZoom()" in _function(js, "fitTiers")
    assert "const SIZES = [0.8, 0.85, 0.9, 1, 1.1, 1.25, 1.5];" in js


def test_home_fitted_holds_five_columns_of_blocks():
    """Home below the big screen's rule: a column of up to 1440 px, five columns of blocks in it in every density tier,
    from the grid's least width and its gap (--sp-4, each tier's own; the column's padding is outside these, and the
    grid's -4 px margin and 4 px padding cancel)."""
    css = _read("home.css")
    assert ".home-col { box-sizing: border-box; height: 100%; max-width: calc(1440px + 2 * var(--pad-page, 24px));" in css
    grid = re.search(r"\.home-blocks \{[^}]*grid-template-columns: repeat\(auto-fill, minmax\((\d+)px, 1fr\)\);[^}]*"
                     r"gap: var\(--sp-4, (\d+)px\); margin: -4px; padding: 4px; \}", css)
    least = int(grid.group(1))
    app = _uncommented_css("app.css")
    gaps = [int(re.search(r"(?:^|\n):root%s \{[^}]*--sp-4: (\d+)px;" % sel, app).group(1))
            for sel in ("", r"\.fit-compact", r"\.fit-tight")]
    assert int(grid.group(2)) == gaps[0]   # the fallback is the roomy tier's
    for gap in gaps:
        assert (1440 + gap) // (least + gap) == 5, gap


def test_home_on_a_big_screen():
    """Where Home is big it takes the big monitor's look: a column of 1040 px with 32 px sides, the greeting on a line
    of its own at 34 px over what goes on at 16, the 2 + 1 large tiles (at least 210 px tall, padding 24 26 18, title
    20 on a line of 28, the targets' floor, number 44, 18 px apart, 26 px between the parts) and three columns of
    blocks (280 px least, 16 apart, padding 18 18 14, radius 15, value 26, text 14). Big is Home's own box as wide as
    that column, so the blocks always stand three to a row, and 1200 tall, where three rows of them stand whole under
    full tiles (three runs, three readings and the line: 1193 px, measured at 2560 x 1440): the 1440p monitor, in the
    app or a Chrome tab, and not a 1080p screen (Home 1028 tall), a 1440 x 900 laptop at 90 % (948) or a portrait
    tablet (820 wide), which keep the fitted Home's nine blocks. The number's line holds its figures, so nothing spills
    at 110 or 125 %, and its row's margin gives back what that adds. No "Overview" line, middle dot or tile footers,
    the blocks keep their region, and below the rule nothing of it applies. Its selectors are the fitted Home's, so it
    comes last in home.css and wins where it holds (always the roomy tier: the tiers' own rules never meet it)."""
    css = _uncommented_css("home.css")
    bigs = list(re.finditer(r"@container home \(min-width: (\d+)px\) and \(min-height: (\d+)px\) \{(.*?)\n\}", css,
                            flags=re.S))
    assert len(bigs) == 1   # one rule on Home's own box
    big = bigs[0]
    assert not css[big.end():].strip()   # the last rule of home.css
    rules = dict(_rules(big.group(3)))
    assert not [sel for sel in rules if sel.startswith("html") or "data-v" in sel], list(rules)

    def of(sel):
        return dict(re.findall(r"([\w-]+):\s*([^;]+);", rules[sel]))
    d = big.group(0)
    col = of(".home-col")
    assert col["max-width"] == "1040px" and col["padding"] == "34px 32px 32px" and col["gap"] == "26px"
    assert "height" not in col   # the whole height still, so the blocks take what is left
    assert int(big.group(1)) == int(col["max-width"][:-2]) and int(big.group(2)) == 1200   # as wide as its column
    assert of(".home-greet") == {"flex-basis": "100%", "font-size": "34px"}   # a line of its own
    assert of(".home-state")["font-size"] == of(".home-labname")["font-size"] == "16px"
    assert of(".home-tiles") == {"grid-template-columns": "repeat(2, minmax(0, 1fr))", "gap": "18px"}
    assert of(".home-tile-lab") == {"grid-column": "1 / -1"}
    tile = of(".home-tile")
    assert tile["min-height"] == "210px" and tile["padding"] == "24px 26px 18px" and tile["max-height"] == "none"
    assert of(".home-tile-go") == {"font-size": "20px", "line-height": "28px"}   # the title's button: 28 px
    # the number's line holds its figures (1.14 em), and its row's margin keeps the tile as tall as it was
    assert of(".home-kpi-n") == {"font-size": "44px", "line-height": "1.15"} and of(".home-kpi")["margin"] == "-2px 0"
    blocks = of(".home-blocks")
    assert blocks == {"grid-template-columns": "repeat(auto-fill, minmax(280px, 1fr))", "gap": "16px"}   # the region's own
    width = int(col["max-width"][:-2]) - 2 * int(col["padding"].split()[1][:-2])
    least, gap = int(re.search(r"minmax\((\d+)px", blocks["grid-template-columns"]).group(1)), int(blocks["gap"][:-2])
    assert width == 976 and (width + gap) // (least + gap) == 3   # three columns in the 976 px
    block = of(".kcard.home-block")
    assert block["padding"] == "18px 18px 14px" and block["border-radius"] == "15px"
    assert of(".hk-value") == {"font-size": "26px"} and of(".hk-text")["font-size"] == "14px"
    sizes = [int(x) for x in re.findall(r"font-size: (\d+)px", d)]
    assert sizes and min(sizes) >= 12   # authored text 12 px or more
    assert not re.search(r"#[0-9a-fA-F]{3}|rgba?\(|transform|transition|animation", d)   # tokens; no lift, no motion
    # no "Overview" line, no middle dot, no tile footers
    home = _read("home.js")
    assert "Overview" not in home and "home-tile-foot" not in home and "home-eyebrow" not in home
    assert '"· "' not in css and "home-tile-foot" not in css
    assert '"data-region": "home-blocks"' in home   # the blocks scroll in their region, never the page


def test_the_sequences_dialogs_keep_their_head_and_buttons():
    """New function, Sweep and Save as (seq.js dialog()) are made as every dialog is: the head, a body that scrolls
    (.dialog-body, the region "dialog") and the buttons, so on a short window only what is between them scrolls. The
    lists inside the body keep to about a third of a short window, so the wheel still finds the body around them."""
    seq = _function(_read("seq.js"), "dialog")
    assert seq.index('h("div", { class: "dialog-head" }') < \
        seq.index('h("div", { class: "dialog-body", "data-region": "dialog" }, ...body)') < \
        seq.index('h("div", { class: "dialog-foot" }, ...foot)')
    assert re.search(r"\.dialog form:has\(> \.dialog-body\) \{ grid-template-rows: auto minmax\(0, 1fr\) auto; \}",
                     _read("app.css"))
    css = _uncommented_css("seq.css")
    for sel, most in ((".seq-files", 248), (".seq-ticks", 210)):
        rule = re.search(r"(?:^|\n)%s \{ max-height: min\(%dpx, calc\(35 \* var\(--vh\)\)\);([^}]*)\}" % (re.escape(sel), most), css)
        assert rule and "grid-auto-rows: max-content;" in rule.group(1), sel   # held to its height, its rows never squash
    assert ".seq-dialog .dialog-body { grid-auto-rows: max-content; }" in css   # nor the parts of the body itself
    touch = css[css.index("@media (pointer: coarse)"):]
    assert ".seq-file, .seq-tick { min-height: 40px; }" in touch and ".seq-swatch { width: 40px; height: 40px; }" in touch
