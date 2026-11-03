"""The Lab, one part at a time (option 3A of the morning of 3 Nov 2026, now the everyday Lab): under the requests
waiting, the sample, Hands on the sample and Bring to safe, which never give way, a strip of tabs Instruments | Watches |
Memory | Activity chooses the one part shown, with all the height left. Without a choice in ?v= it is the strip; ?v=3C
keeps the folds. Every way into a part chooses its tab (3C: opens its fold). The strip is a tab list as the APG's:
tabs that control their panels, one tab stop, the arrow keys, Home and End; the alarms counted on Watches and a dot on
Activity for lines not seen, said to a screen reader too. The chosen part is kept per browser, and a part that shows
nothing is never the chosen one. Wide (full screen), every part stands in its column and the strip steps aside.
Checked in the page's own files; the page itself is probed in Chrome by the night's harness."""
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


def _v3a_css():
    css = _read("app.css")
    fit_d = css[css.index("/* ===== fit D"):css.index("/* ===== end fit D")]
    return fit_d[fit_d.index("/* v3A */"):fit_d.index("/* /v3A */")]


def test_the_strip_is_the_lab_without_a_choice_and_3c_keeps_the_folds():
    js = _read("app.js")
    # the strip follows option 3 alone: 3A without ?v= (readVariants' default), not only when ?v=3A named it
    assert 'const labTabs = () => V[3] === "A";' in js
    assert re.search(r"const v = \{[^}]*\b3: \"A\"", js), "3A is what shows without a choice"
    assert "3[AC]" in _function(js, "readVariants")   # ?v=3C is still taken
    folds = _function(js, "initLabFolds")
    assert folds.index("if (labTabs()) { initLabTabs(); return; }") < folds.index("for (const part of")
    # 3C, unchanged: each part folds under its heading, kept by its id, Instruments open on a fresh browser
    assert '"lh.labFold." + id' in folds and 'h("button", { class: "fold-btn"' in folds and "body.inert = folded" in folds
    assert 'apply(store(key) === null ? id !== "instruments" : store(key) === "1");' in folds
    assert "initLabFolds();" in _function(js, "init")


def test_every_way_into_a_part_chooses_its_tab_or_opens_its_fold():
    js = _read("app.js")
    reveal = _function(js, "labPartOpen")
    assert reveal.index("if (labTabs()) { chooseLabPart(part.dataset.part); return; }") < reveal.index("LAB_FOLDS.get(")
    assert "labPartOpen(form.closest(\".lab-part\"));" in _function(js, "partForm")   # Add… and a note's pencil
    assert "partForm(\"labmem-form\", true);" in _function(js, "editLabNote")
    # nothing else opens a fold or chooses a part: every reveal goes through labPartOpen, the strip or its keys
    outside = js.replace(_function(js, "initLabFolds"), "").replace(reveal, "")
    assert "LAB_FOLDS.get(" not in outside and 'store("lh.labFold."' not in outside
    callers = [m.start() for m in re.finditer(r"chooseLabPart\(", js)]
    allowed = [_function(js, n) for n in ("labPartOpen", "initLabTabs", "labTabKeys", "chooseLabPart")]
    for at in callers:
        assert any(js.index(body) <= at < js.index(body) + len(body) for body in allowed), js[at - 80:at + 40]
    # a request waiting is asked above every part, so it shows whichever part is chosen
    page = _read("index.html")
    assert page.index('<div id="lab-pending" data-region="lab-part">') < page.index('id="lab-strip"') \
        < page.index('data-part="instruments"')
    assert page.index('class="lab-top"') < page.index('id="lab-strip"')   # the sample and its safety too


def test_the_strip_is_a_tab_list_that_says_what_is_new():
    js, page = _read("app.js"), _read("index.html")
    assert '<div id="lab-strip" class="seg lab-strip" role="tablist" aria-label="Lab parts" hidden></div>' in page
    build = _function(js, "initLabTabs")
    assert 'role: "tab", id: "lab-tab-" + id, "aria-controls": part.id' in build
    assert 'h("span", { class: "lab-tab-mark", "aria-hidden": "true", hidden: true }), h("span", { class: "sr-only" })' in build
    # one name per part in every layout: each tab is named as its part's heading, which shows where every part does
    assert 'h("span", { class: "lab-tab-name" }, h3.textContent.trim())' in build and "short" not in build
    lab = page[page.index('<aside id="labpanel"'):page.index("</aside>", page.index('<aside id="labpanel"'))]
    assert '<h3 id="lab-h-memory">Memory</h3>' in lab and "Lab memory" not in lab
    assert 'strip.addEventListener("keydown", labTabKeys);' in build and "segKeys(strip" not in build
    assert "new ResizeObserver(labTabRoles).observe($(\"labpanel\"))" in build
    choose = _function(js, "chooseLabPart")
    assert 'tab.setAttribute("aria-selected", String(on));' in choose and "tab.tabIndex = on ? 0 : -1;" in choose
    # the keys: ← → (round), Home and End, chosen at once and focused; ↑ ↓ left to the page (a horizontal strip)
    keys = _function(js, "labTabKeys")
    assert "{ ArrowLeft: i - 1, ArrowRight: i + 1, Home: 0, End: tabs.length - 1 }[e.key]" in keys
    assert "ArrowUp" not in keys and "e.preventDefault();" in keys and "tab.focus();" in keys
    assert "(to + tabs.length) % tabs.length" in keys and "e.altKey || e.ctrlKey || e.metaKey" in keys
    # the parts are tabpanels labelled by their tabs while the strip shows; wide, sections under their headings
    roles = _function(js, "labTabRoles")
    assert 'getComputedStyle($("lab-strip")).display === "none"' in roles
    assert 'part.setAttribute("role", "tabpanel"); part.tabIndex = 0;' in roles
    assert 'part.setAttribute("aria-labelledby", wide ? "lab-h-" + id : tab.id);' in roles
    # the marks, also said: "Watches, 2 alarms", "Activity, new lines"; written only when they change
    mark = _function(js, "labTabMark")
    assert 'm.dataset.mark === String(mark || "")' in mark and 'tab.querySelector(".sr-only").textContent' in mark
    assert '", new lines"' in mark and '" alarm" : " alarms"' in mark
    # Watches counts the alarms on, of every kind (an instrument gone silent, an interlock, a stalled set point, not only
    # a watch's reading), as Home does: the same number on its tab and its head, and none once each has cleared
    render = _function(js, "renderWatches")
    assert "function renderWatches(watches, status, alarms) {" in render and "w.alarm).length" not in render
    assert "const on = (alarms || []).filter((a) => a && !a.cleared).length" in render
    assert 'labTabMark("watches", on);' in render and '$("watch-summary").classList.toggle("bad", on > 0);' in render
    assert "const alarmsOn = (s) => ((s && s.alarms) || []).filter((a) => a && !a.cleared);" in _read("home.js")
    assert "renderWatches(s.watches || [], s.status, s.alarms);" in _function(js, "pollState")
    # the head says only what there is: "1 alarm · 2 watched", "2 watched", never "0 alarm"
    assert '[on ? on + (on === 1 ? " alarm" : " alarms") : "",' in render and '.filter(Boolean).join(" · ")' in render
    # Activity's lines are seen only while its part is on the screen, not merely while the Lab is open
    seen = _function(js, "labTabSeen")
    assert 'paneVisible("labpanel") && (LAB_TABS.chosen === "activity" || LAB_TABS.wide)' in seen
    poll = _function(js, "pollState")
    assert "LAB_TABS.top = lines.length ? String(lines[0].text) : \"\";" in poll and "labTabSeen();" in poll


def test_the_chosen_part_is_kept_and_a_part_that_shows_nothing_is_never_chosen():
    js = _read("app.js")
    build, choose = _function(js, "initLabTabs"), _function(js, "chooseLabPart")
    assert 'const labTabShown = (id) => LAB_TABS.tabs.has(id) && !$("lab-sec-" + id).hidden;' in js
    assert 'const kept = store("lh.labPart");' in build
    assert "chooseLabPart(labTabShown(kept) ? kept : [...LAB_TABS.tabs.keys()].find(labTabShown));" in build
    assert choose.startswith("function chooseLabPart(id) {\n    if (!labTabShown(id)) return;")
    assert 'store("lh.labPart", id);' in choose and 'tab.hidden = $("lab-sec-" + k).hidden;' in choose
    assert ".filter((t) => !t.hidden)" in _function(js, "labTabKeys")   # the keys step over a part not shown


def test_the_part_used_last_wide_is_the_one_shown_when_the_lab_narrows():
    # wide (full screen), every part shows: a press or the focus in one chooses it, so that Esc, ⤢ or a narrower window
    # brings back the part that holds the focus instead of hiding it (the focus would drop to the page)
    build = _function(_read("app.js"), "initLabTabs")
    used = build[build.index("const used = (e) => {"):build.index('panel.addEventListener("pointerdown", used);')]
    assert 'e.target.closest("#labpanel .lab-part")' in used
    assert "if (part && part.dataset.part !== LAB_TABS.chosen) chooseLabPart(part.dataset.part);" in used
    assert 'panel.addEventListener("focusin", used);' in build and 'panel.addEventListener("pointerdown", used);' in build


def test_3c_is_fenced_to_go_in_one_pass():
    # 3A is the everyday Lab; 3C's folds are kept for the morning's comparison only, their code fenced (brief §2)
    js, css = _read("app.js"), _read("app.css")
    fenced = lambda text, a, b: re.sub(re.escape(a) + r".*?" + re.escape(b), "", text, flags=re.S)
    block = js[js.index("// the Lab panel's parts"):js.index("  // /v3A")]
    rest = fenced(block, "// v3C\n", "// /v3C\n")
    assert rest.count("// v3C") == 0 and block.count("// v3C\n") == 3 == block.count("// /v3C\n")
    for only_3c in ("LAB_FOLDS", "lh.labFold.", "fold-btn", "body.inert", 'classList.toggle("still"'):
        assert only_3c in block and only_3c not in rest, only_3c
    # without the fences, every way into a part is the strip's
    assert "if (labTabs()) { initLabTabs(); return; }" in rest and "if (labTabs()) { chooseLabPart(part.dataset.part); return; }" in rest
    fit_d = css[css.index("/* ===== fit D"):css.index("/* ===== end fit D")]
    rest = re.sub(r"/\* (?:v3A|/v3A) \*/", "", fenced(fit_d, "/* v3C */", "/* /v3C */"))
    for only_3c in ("0fr", ".fold-btn {", ".lab-part.still", "visibility: hidden", "grid-template-rows .12s"):
        assert only_3c in fit_d and only_3c not in rest, only_3c
    # what 3A keeps: a part's heading over its body, the body's row taking the rest, the body shown
    assert ("#labpanel .lab-part { display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: auto 1fr; "
            "min-width: 0; min-height: 0;") in rest
    assert "#labpanel .lab-part-body { min-height: 0; margin-inline: -4px; padding-inline: 4px; }" in rest


def test_the_lab_small_fixes_hold():
    css, js = _read("app.css"), _read("app.js")
    fit_d = re.sub(r"/\*.*?\*/", "", css[css.index("/* ===== fit D"):css.index("/* ===== end fit D")], flags=re.S)
    mouse, touch = fit_d[:fit_d.index("@media (pointer: coarse) {\n  #labpanel { --lab-row")], \
        fit_d[fit_d.index("@media (pointer: coarse) {\n  #labpanel { --lab-row"):]
    # New device… on one line; All | Active and Mine | Everyone 30 px sides (24 px or more at 85 %)
    assert "#labpanel .lab-sample > .btn { flex: none; white-space: nowrap; }" in mouse
    assert "#labpanel .seg:not(.lab-strip) > button { min-height: 30px; }" in mouse
    # on touch: the choices, a row's buttons (a watch's ×, a note's pencil and bin) and Alarm history 40 px
    touch = touch[:touch.index("\n}\n")]
    assert "#labpanel .seg:not(.lab-strip) > button { min-height: 40px; }" in touch
    assert "#labpanel .row-btn { min-width: 40px; }" in touch and "#labpanel .alarm-history summary { min-height: 40px;" in touch
    # a narrow Lab's marks sit wholly above their tabs' names, and the strip leaves them room under Hands on the sample
    narrow = re.sub(r"/\*.*?\*/", "", _v3a_css(), flags=re.S)
    narrow = narrow[narrow.index("@container lab (max-width: 371px)"):]
    narrow = narrow[:narrow.index("\n}\n")]
    assert "#labpanel.lab-tabs .lab-strip { margin-top: 4px; }" in narrow
    assert "top: calc(50% - 27px); right: -4px; min-width: 16px; height: 16px;" in narrow
    assert "#labpanel.lab-tabs .lab-tab-mark.dot { top: calc(50% - 19px); right: -1px; min-width: 8px; height: 8px; padding: 0; }" in narrow
    assert "Number(mark) === 1" in _function(js, "labTabMark")


def test_the_strip_fits_every_lab_and_keeps_the_rules():
    css = _v3a_css()
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # targets: 30 px with a mouse (28 or more at every page size), 40 px on touch
    assert re.search(r"#labpanel\.lab-tabs \.lab-strip > button \{[^}]*min-height: 30px;", bare)
    touch = bare[bare.index("@media (pointer: coarse)"):]
    assert "#labpanel.lab-tabs .lab-strip > button { min-height: 40px; }" in touch
    # a name is cut only as the last resort; a narrow Lab puts the count or the dot on the tab's corner instead
    assert "#labpanel .lab-tab-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; }" in bare
    narrow = bare[bare.index("@container lab (max-width: 371px)"):]
    assert "#labpanel.lab-tabs .lab-tab-mark { position: absolute;" in narrow
    # wide: the strip steps aside and every part shows in its column
    wide = bare[bare.index("@container lab (min-width: 900px)"):]
    assert "#labpanel.lab-tabs .lab-strip { display: none; }" in wide
    assert "#labpanel.lab-tabs .lab-part:not(.chosen) { display: grid; }" in wide
    # less motion: only the colour changes, in .15 s; the thumb's own slide is off there already (the kit's rules)
    calm = bare[bare.index("@media (prefers-reduced-motion: reduce)"):]
    assert "#labpanel.lab-tabs .lab-strip > button { transition: color .15s ease; }" in calm
    # colours from tokens only, in light and dark
    for rule in re.findall(r"\{[^}]*\}", bare):
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rule), rule
    # a media or container query comes after the rules it overrides (the person's gotcha)
    assert bare.index("@container lab (max-width: 371px)") > bare.index("#labpanel .lab-tab-mark.dot {")
    assert bare.index("@media (pointer: coarse)") > bare.index("#labpanel.lab-tabs .lab-strip > button {")
