"""The Lab, one part at a time: under the requests waiting, the sample, Hands on the sample and Bring to safe, which
never give way, a strip of tabs Instruments | Watches | Memory | Activity chooses the one part shown, with all the
height left. Every way into a part chooses its tab. The strip is a tab list as the APG's: tabs that control their
panels, one tab stop, the arrow keys, Home and End; the alarms counted on Watches and a dot on Activity for lines not
seen, said to a screen reader too. The chosen part is kept per browser, and a part that shows nothing is never the
chosen one. Wide (full screen), every part stands in its column and the strip steps aside. Nothing folds: what an
earlier Lab's folds kept on a browser is let go. Checked in the page's own files."""
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


def _lab_css():
    css = _read("app.css")
    return css[css.index("/* ===== fit D"):css.index("/* ===== end fit D")]


def test_the_strip_is_the_lab_and_nothing_folds():
    js, page, css = _read("app.js"), _read("index.html"), _read("app.css")
    # one Lab, with nothing to choose between: the strip is built at the start and shows from then on
    assert "initLabTabs();" in _function(js, "init")
    for gone in ("initLabFolds", "LAB_FOLDS", "labTabs()", "labPartOpen", 'store("lh.labFold.', "lab-tabs", "fold-btn"):
        assert gone not in js.replace(_function(js, "initSettingFolds"), ""), gone   # Settings' folds are theirs
    assert "lab-tabs" not in css and not re.search(r"/\* /?v3[A-C] \*/", css) and not re.search(r"// /?v3[A-C]", js)
    lab = page[page.index('<aside id="labpanel"'):page.index("</aside>", page.index('<aside id="labpanel"'))]
    # no part folds; the one fold is Alarm history's disclosure over its list (as an approval's details)
    rest = lab.replace(' aria-controls="alarm-history-fold"', "").replace(
        '<div id="alarm-history-fold" class="k-fold" inert><div class="k-fold-in">', "")
    assert "3C" not in lab and "fold" not in rest
    build = _function(js, "initLabTabs")
    assert "strip.hidden" not in build and '"open"' not in build
    # what an earlier Lab's folds kept on this browser, by part (lh.labFold.<part>) or by heading (lh.fold.<heading>),
    # is let go at the start, quietly; Settings' folds keep theirs (lh.fold.set.<id>)
    assert build.index("forgetLabFolds();") < build.index("for (const part of")
    forget = _function(js, "forgetLabFolds")
    assert "catch (e)" in forget and "toast(" not in forget
    # the keys gathered before any goes (a removal may reorder what is left), then each removed
    assert forget.index("Object.keys(localStorage).filter(") < forget.index("for (const k of old) localStorage.removeItem(k);")
    old = re.compile(re.search(r"\.filter\(\(k\) => /(.+?)/\.test\(k\)\)", forget).group(1))
    for key, gone in (("lh.labFold.instruments", True), ("lh.labFold.activity", True), ("lh.fold.Lab memory", True),
                      ("lh.fold.Instruments", True), ("lh.fold.set.email", False), ("lh.labPart", False),
                      ("lh.folder", False), ("lh.labEveryone", False)):
        assert bool(old.search(key)) == gone, key


def test_every_way_into_a_part_chooses_its_tab():
    js = _read("app.js")
    assert 'chooseLabPart(form.closest(".lab-part").dataset.part);' in _function(js, "partForm")   # Add…, a note's pencil
    assert "partForm(\"labmem-form\", true);" in _function(js, "editLabNote")
    # nothing else chooses a part: every reveal goes through partForm, the strip or its keys (and, wide or narrowing,
    # the part pressed or holding the focus), or a link to the Lab on one part (paneShow: an alarm's)
    callers = [m.start() for m in re.finditer(r"chooseLabPart\(", js)]
    allowed = [_function(js, n) for n in ("partForm", "initLabTabs", "labTabKeys", "chooseLabPart", "labTabRoles",
                                          "paneShow")]
    for at in callers:
        assert any(js.index(body) <= at < js.index(body) + len(body) for body in allowed), js[at - 80:at + 40]
    # a request waiting is asked above every part, so it shows whichever part is chosen
    page = _read("index.html")
    assert page.index('<div id="lab-pending" data-region="lab-part">') < page.index('id="lab-strip"') \
        < page.index('data-part="instruments"')
    assert page.index('class="lab-top"') < page.index('id="lab-strip"')   # the sample and its safety too


def test_the_strip_is_a_tab_list_that_says_what_is_new():
    js, page = _read("app.js"), _read("index.html")
    assert '<div id="lab-strip" class="seg lab-strip" role="tablist" aria-label="Lab parts"></div>' in page
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
    assert "renderWatches(s.watches || [], s.status, s.alarms);" in _function(js, "readState")
    # the head says only what there is: "1 alarm · 2 watched", "2 watched", never "0 alarm"
    assert '[on ? on + (on === 1 ? " alarm" : " alarms") : "",' in render and '.filter(Boolean).join(" · ")' in render
    # Activity's lines are seen only while its part is on the screen, not merely while the Lab is open
    seen = _function(js, "labTabSeen")
    assert 'paneVisible("labpanel") && (LAB_TABS.chosen === "activity" || LAB_TABS.wide)' in seen
    poll = _function(js, "readState")
    assert "LAB_TABS.top = lines.length ? String(lines[0].text) : \"\";" in poll and "labTabSeen();" in poll


def test_the_chosen_part_is_kept_and_a_part_that_shows_nothing_is_never_chosen():
    js = _read("app.js")
    build, choose = _function(js, "initLabTabs"), _function(js, "chooseLabPart")
    assert 'const labTabShown = (id) => LAB_TABS.tabs.has(id) && !$("lab-sec-" + id).hidden;' in js
    assert 'const kept = store("lh.labPart");' in build
    assert "chooseLabPart(labTabShown(kept) ? kept : [...LAB_TABS.tabs.keys()].find(labTabShown));" in build
    assert choose.startswith("function chooseLabPart(id, keep = true) {\n    if (!labTabShown(id)) return;")
    assert 'if (keep) store("lh.labPart", id);' in choose and 'tab.hidden = $("lab-sec-" + k).hidden;' in choose
    assert ".filter((t) => !t.hidden)" in _function(js, "labTabKeys")   # the keys step over a part not shown


def test_the_part_used_last_wide_is_the_one_shown_when_the_lab_narrows():
    # wide (full screen), every part shows: a press or the focus in one chooses it, so that Esc, ⤢ or a narrower window
    # brings back the part that holds the focus instead of hiding it (the focus would drop to the page)
    build = _function(_read("app.js"), "initLabTabs")
    used = build[build.index("const used = (e) => {"):build.index('panel.addEventListener("pointerdown", used);')]
    assert 'e.target.closest("#labpanel .lab-part")' in used
    assert "if (part && part.dataset.part !== LAB_TABS.chosen) chooseLabPart(part.dataset.part);" in used
    assert 'panel.addEventListener("focusin", used);' in build and 'panel.addEventListener("pointerdown", used);' in build
    # and when the Lab narrows, the part that holds the focus is chosen even if no focus event said so (a page focused
    # while its window was not), before the strip hides the others
    roles = _function(_read("app.js"), "labTabRoles")
    at = 'const at = !wide && document.activeElement && document.activeElement.closest("#labpanel .lab-part");'
    assert at in roles and roles.index(at) < roles.index("for (const [id, tab] of LAB_TABS.tabs)")
    assert "if (at && at.dataset.part !== LAB_TABS.chosen) chooseLabPart(at.dataset.part);" in roles


def test_the_parts_are_the_strip_s_tab_panels_and_nothing_opens_or_shuts():
    # one layout, no class to choose it: what waits, the sample and its safety, the strip, then the chosen part, its
    # body always there with the rest of the height (nothing goes from 0fr to 1fr, nothing is inert)
    lab = re.sub(r"/\*.*?\*/", "", _lab_css(), flags=re.S)
    for gone in ("0fr", ".fold-btn", ".lab-part.open", ".lab-part.still", "visibility: hidden", "grid-template-rows .12s",
                 "lab-tabs"):
        assert gone not in lab, gone
    assert ("#labpanel .lab-scroll { display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: auto auto auto;"
            in lab)
    assert "#labpanel .lab-strip { grid-row: 3; display: flex;" in lab
    assert ("#labpanel .lab-part { display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: auto 1fr; "
            "min-width: 0; min-height: 0; }") in lab
    assert "#labpanel .lab-part:not(.chosen) { display: none; }" in lab
    assert "#labpanel .lab-part-head h3 { display: none; }" in lab   # its tab names it
    assert "#labpanel .lab-part-body { grid-row: 2; min-height: 0; margin-inline: -4px; padding-inline: 4px; }" in lab
    assert "body.inert" not in _read("app.js")
    # wide: the strip steps aside and every part shows under its heading, a line between the parts of a column; each
    # rule after the one it overrides (the person's gotcha: a query before its rule loses)
    at = lab.index("@container lab (min-width: 900px)")
    wide = lab[at:lab.index("\n}\n", at)]
    for rule in ("#labpanel .lab-strip { display: none; }", "#labpanel .lab-part:not(.chosen) { display: grid; }",
                 "#labpanel .lab-part-head h3 { display: block; }",
                 "#labpanel .lab-col-2 > .lab-part + .lab-part { border-top: 1px solid var(--line); }"):
        assert rule in wide, rule
    for sel in ("#labpanel .lab-strip {", "#labpanel .lab-part:not(.chosen) {", "#labpanel .lab-part-head h3 {",
                "#labpanel .lab-scroll {"):
        assert lab.index(sel) < at, sel


def test_the_lab_small_fixes_hold():
    js = _read("app.js")
    fit_d = re.sub(r"/\*.*?\*/", "", _lab_css(), flags=re.S)
    mouse, touch = fit_d[:fit_d.index("@media (pointer: coarse) {\n  #labpanel { --lab-row")], \
        fit_d[fit_d.index("@media (pointer: coarse) {\n  #labpanel { --lab-row"):]
    # New device… on one line; the strip's tabs, All | Active and Mine | Everyone 30 px tall (24 px or more at 85 %)
    assert "#labpanel .lab-sample > .btn { flex: none; white-space: nowrap; }" in mouse
    assert "#labpanel .seg > button { min-height: 30px; }" in mouse
    # on touch: those, a row's buttons (a watch's ×, a note's pencil and bin), Alarm history (a button.link) and a
    # waiting request's Approve and Decline 40 px
    touch = touch[:touch.index("\n}\n")]
    assert "#labpanel .seg > button { min-height: 40px; }" in touch
    assert "#labpanel .row-btn { min-width: 40px; }" in touch
    assert "#labpanel :is(.btn.small, button.link, .approval-actions .btn, .row-btn, .inst-tools input, .mount-select) " \
        "{ min-height: 40px; }" in touch
    page = _read("index.html")
    assert '<button id="alarm-history-btn" class="link disclose" type="button" aria-expanded="false" ' \
        'aria-controls="alarm-history-fold">' in page and "<summary>Alarm history" not in page
    # a narrow Lab's marks sit wholly above their tabs' names, and the strip leaves them room under Hands on the sample
    narrow = fit_d[fit_d.index("@container lab (max-width: 371px)"):]
    narrow = narrow[:narrow.index("\n}\n")]
    assert "#labpanel .lab-strip { margin-top: 4px; }" in narrow
    assert "top: calc(50% - 27px); right: -4px; min-width: 16px; height: 16px;" in narrow
    assert "#labpanel .lab-tab-mark.dot { top: calc(50% - 19px); right: -1px; min-width: 8px; height: 8px; padding: 0; }" in narrow
    assert "Number(mark) === 1" in _function(js, "labTabMark")


def test_a_poll_keeps_a_press_the_focus_and_what_was_switched():
    js, css = _read("app.js"), _read("app.css")
    # the instrument rows are kept by name and drawn again only when what they draw changed (rowSig); the readings that
    # move at every poll are written in place; nothing is replaced while a pointer is down on the list
    draw = _function(js, "renderInstruments")
    assert "rowSig(inst)" in draw and "PANEL.rows.get(inst.name)" in draw and "instHead(inst)" in draw
    assert "if (PANEL.pressed && Date.now() - PANEL.pressed < 5000) { PANEL.later = true; return; }" in draw
    assert "shown.map(instrumentRow)" not in js and "holdInstruments();" in js
    assert "inst.enabled, PANEL.switching.get(inst.name), inst.state" in _function(js, "rowSig")
    # a press holds the list's redraws until its click (a tap's comes a moment after pointerup), or 400 ms after it is
    # let go; a cancelled press lets go at once
    hold = _function(js, "holdInstruments")
    for text in ('list.addEventListener("pointerdown", () => { clearTimeout(letGo); PANEL.pressed = Date.now(); });',
                 'document.addEventListener("pointerup", () => { if (PANEL.pressed) letGo = setTimeout(release, 400); }, true);',
                 'document.addEventListener("pointercancel", () => { if (PANEL.pressed) release(); }, true);',
                 'list.addEventListener("click", () => { if (PANEL.pressed) setTimeout(release, 0); });',
                 "if (PANEL.later) { PANEL.later = false; renderInstruments(); }"):
        assert text in hold, text
    # a poll sent before a switch was answered does not turn the switch back: that instrument keeps what the page had
    poll = _function(js, "readState")
    assert poll.index("const sent = Date.now();") < poll.index('api("/api/state"')
    assert "freshInstruments(s.instruments, sent)" in poll
    assert "PANEL.switchedAt.set(name, Date.now());" in _function(js, "switchInstrument")
    fresh = _function(js, "freshInstruments")
    assert "PANEL.switchedAt.get(i.name) > sent && was.has(i.name)" in fresh
    assert "{ ...i, enabled: was.get(i.name).enabled, state: was.get(i.name).state }" in fresh
    # the requests waiting are changed in place, so a focused Approve or Decline keeps its focus
    assert '$("lab-pending").replaceChildren' not in js
    # Add…'s form shut from inside (Watch, Keep, Cancel) gives the focus back to its Add…
    assert "if (!open && btn && form.contains(document.activeElement)) btn.focus({ preventScroll: true });" \
        in _function(js, "partForm")
    # a request's countdown in minutes from 90 s and in hours from 90 min (one escalated to a phone may wait 12 hours),
    # changing unit at the same points in both languages
    assert '"Declines itself in " + inTime(left)' in _function(js, "makeApproval")
    assert 'function inTime(s) { return s < 90 ? s + " s" : s < 5400 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h"; }' in js
    assert ('(n < 90 ? n + " 秒" : n < 5400 ? Math.round(n / 60) + " 分钟" : (n / 3600).toFixed(1) + " 小时")'
            ' + "后自动拒绝"') in js
    # an instrument's "!" badge: 12 px words on the danger fill, in its own ink (white is 3.5:1 in dark)
    assert ".inst-count.err { background: var(--danger); color: var(--danger-ink); }" in css
    # the Sample select, cut, says so
    rule = css[css.index(".mount-select {"):]
    assert "text-overflow: ellipsis;" in rule[:rule.index("}")]


def test_the_strip_fits_every_lab_and_keeps_the_rules():
    bare = re.sub(r"/\*.*?\*/", "", _lab_css(), flags=re.S)
    strip = bare[bare.index("#labpanel .lab-strip {"):bare.index("#labpanel .lab-col {")]
    # targets: 30 px with a mouse (28 or more at every page size), 40 px on touch, as every choice in the Lab (above):
    # the strip's own rules leave its height to those
    assert "min-height" not in strip
    # a name is cut only as the last resort; a narrow Lab puts the count or the dot on the tab's corner instead
    assert "#labpanel .lab-tab-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; }" in strip
    narrow = bare[bare.index("@container lab (max-width: 371px)"):]
    narrow = narrow[:narrow.index("\n}\n")]
    assert "#labpanel .lab-tab-mark { position: absolute;" in narrow
    # less motion: only the colour changes, in .15 s; the thumb's own slide is off there already (the kit's rules). The
    # rule wins over the kit's ':is(#mode-seg, #runs-seg, .seg).has-thumb > button' (.25 s) by its specificity, not by
    # where the block stands
    calm = bare[bare.index("@media (prefers-reduced-motion: reduce)"):]
    assert "#labpanel .lab-strip.has-thumb > button { transition: color .15s ease; }" in calm
    assert ":is(#mode-seg, #runs-seg, .seg).has-thumb > button { transition: color .25s ease; }" in _read("app.css")
    # colours from tokens only, in light and dark: every rule of the Lab and Team chat
    for rule in re.findall(r"\{[^}]*\}", bare):
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rule), rule
    # a media or container query comes after the rules it overrides (the person's gotcha)
    assert bare.index("@container lab (max-width: 371px)") > bare.index("#labpanel .lab-tab-mark.dot {")
    assert bare.index("@media (pointer: coarse)") > bare.index("#labpanel .seg > button {")
    assert bare.index("@media (prefers-reduced-motion: reduce)") > bare.index("#labpanel .lab-strip > button {")
