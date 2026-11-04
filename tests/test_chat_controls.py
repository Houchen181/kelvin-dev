"""The chat column's controls follow one rule per kind of control, checked in
the page's own files. Incognito is a mode of the chat, a toggle button: on, even from Home, it shows the chat; off, or
another chat opened meanwhile, forgets the incognito chat, asking "Forget this chat?" first once it holds messages,
and off goes back to the chat before; nothing of it is remembered. popMenu draws a checkable item in both states and
says it with aria-checked: a box for a setting (menuitemcheckbox), a ring with a dot on the chosen one for a choice
(menuitemradio, grouped under its heading). A tool step and a decided request are disclosures (aria-expanded and
aria-controls, a constant label, a turning chevron; what they hold opens by height and is inert while shut). A project
in the sidebar folds even when it holds the open chat; the sidebar's lab name is words, not a control."""
import re
from pathlib import Path

import labharness

STATIC = Path(labharness.__file__).parent / "web" / "static"


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _function(js, name):
    """The text of a function of a script, by its name (braces counted)."""
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


def _section(css, name):
    """A builder's section of app.css: from its "===== fit NAME" line to its end line."""
    start = css.index("/* ===== fit %s:" % name)
    return css[start:css.index("/* ===== end fit %s ===== */" % name, start)]


def test_incognito_is_a_mode_of_the_chat():
    js = _read("app.js")
    toggle = _function(js, "toggleIncognito")
    # on, from anywhere: the chat shows (from Home too), full screen ends, the chat open before is kept to go back to
    assert "exitFull();" in toggle and 'setMode("chat", true);' in toggle
    on = toggle[toggle.index("INCOG.before = "):]
    assert "S.chatId ? { id: S.chatId } : { project: SIDE.newProject }" in on and "if (S.chatId) newChat();" in on
    assert on.index("INCOG.before = ") < on.index("if (S.chatId) newChat();") < on.index("S.incognito = true;")
    # off: asked first (Cancel keeps it), then forgotten, then the chat before again (or a new chat where it was);
    # a chat or project deleted meanwhile is not gone back to (no 404, no stale project for the next message)
    off = toggle[toggle.index("if (S.incognito) {"):toggle.index("INCOG.before = ")]
    assert off.index("await mayLeaveIncognito()") < off.index("leaveIncognito();") < off.index("openChat(id)")
    assert "const id = S.chats.some((c) => c.id === back.id) ? back.id : null;" in off
    assert "if (back.id && !S.chats.some((c) => c.id === back.id)) await refreshChats();" in off   # listed late: read again
    assert "newChat(S.projects.some((p) => p.id === back.project) ? back.project : null)" in off
    ask = _function(js, "mayLeaveIncognito")
    assert "if (!incogSaid()) return true;" in ask and 'askYes("Forget this chat?' in ask and "danger: true" in ask
    assert "S.incognito && !!S.chatId && !!thread.querySelector(\".msg\")" in js   # messages: only then asked
    # opening another chat, or a new one, asks the same, before anything changes
    opened = _function(js, "openChat")
    assert opened.index("if (!(await mayLeaveIncognito())) return false;") < opened.index("await api(")
    assert "return true;" in opened[-40:]
    # a new chat: a promise of whether it started one (false: the incognito chat was kept), at once when nothing is asked,
    # so a caller that fills the new chat's box waits for it (Make recipes…) and never fills the kept incognito chat's
    fresh = _function(js, "newChat")
    guard = fresh[:fresh.index("exitFull();")]
    assert "if (incogSaid())" in guard and "return mayLeaveIncognito().then((ok) => ok && (leaveIncognito(), newChat(projectId, ask)));" in guard
    assert fresh.rstrip("}").rstrip().endswith("return Promise.resolve(true);")
    recipes = _function(js, "recipesFromScripts")
    assert "async function recipesFromScripts" in js and recipes.index("if (!(await newChat())) return;") < recipes.index('$("input").value =')
    # forgotten as it is left; never remembered: no key of the browser's store holds it
    leave = _function(js, "leaveIncognito")
    assert 'api("/api/chats/" + S.chatId, { method: "DELETE" })' in leave and "INCOG.before = null" in leave
    assert not re.search(r'store\("lh\.[\w.]*incog', js, re.I)
    # its label and its tip are its name, on or off; its state is aria-pressed, never an open pane's .on
    apply = _function(js, "applyIncognito")
    assert 'b.setAttribute("aria-pressed", String(S.incognito));' in apply and 'b.title = "Incognito chat"' in apply
    assert 'classList.toggle("on"' not in apply
    assert "Leave incognito" not in js and 'aria-label="Incognito chat"' in _read("index.html")
    # the map the AI reads names it as the page does
    guide = (STATIC.parent.parent / "data" / "portal.md").read_text(encoding="utf-8")
    assert "the **Incognito chat**, **Terminal**" in guide and "**Incognito**," not in guide


def test_incognito_has_its_own_on_look_never_the_open_panes():
    css = _read("app.css")
    mine = _section(css, "I")
    rule = re.search(r'\.topbar \.topbar-right #toggle-incognito\[aria-pressed="true"\] \{([^}]*)\}', mine)
    assert rule and "dashed" in rule.group(1) and "color-mix(in srgb, var(--text)" in rule.group(1)
    # its id outweighs a pressed pill's raised look (the open panes', classes only) wherever that stands, and the panes'
    # .on is never set on it (test_incognito_is_a_mode_of_the_chat)
    assert "#toggle-incognito[" in rule.group(0)
    assert "body.incognito .composer { border-style: dashed;" in css   # the composer's, which it matches
    assert "#toggle-incognito.on { background: var(--text)" not in css


def test_pop_menu_draws_and_says_its_checkable_items():
    js = _read("app.js")
    menu = _function(js, "popMenu")
    assert 'const role = it.role || ("checked" in it ? "menuitemradio" : "menuitem")' in menu
    assert '"aria-checked": checks ? String(!!it.checked) : null' in menu
    assert '"menu-check " + (role === "menuitemradio" ? "menu-radio" : "menu-box")' in menu   # a ring or a box, both states
    assert 'role: "group", "aria-labelledby": head ? head.id : null' in menu       # a choice: a group named by its heading
    assert 'icon("check")' in menu and "it.checked ? icon(\"check\")" not in menu  # the old tick only when on is gone
    for key in ('"Home"', '"End"', '"Escape"', '"Tab"'):
        assert key in menu, key
    # a menu button: it says it opens a menu, and is marked expanded while its menu is open, not after
    assert 'anchor.setAttribute("aria-haspopup", "menu");' in menu and 'anchor.setAttribute("aria-expanded", "true");' in menu
    assert 'menuFrom.setAttribute("aria-expanded", "false")' in _function(js, "closeMenu")
    assert '"aria-haspopup": name === "dots" ? "menu" : null' in _function(js, "rowButton")   # a row's ⋯, before it opens
    # its users: the sequence's (On a failure: radios; Kelvin replies at once: a checkbox), the chat's project, the folder;
    # a choice always has one chosen: No project for a chat outside every project, Data folder for the data folder
    seq = _read("seq.js")
    assert seq.count('role: "menuitemradio"') >= 2 and 'role: "menuitemcheckbox"' in seq
    chat = _function(js, "chatMenu")
    assert 'role: "menuitemradio", checked: c.project === p.id' in chat
    assert '{ label: "No project", role: "menuitemradio", checked: loose,' in chat and "Remove from project" not in js
    folder = _function(js, "folderMenu")
    assert 'role: "menuitemradio", checked: FOLDER.own && same(p, FOLDER.path)' in folder
    assert '{ label: "Data folder", role: "menuitemradio", checked: !FOLDER.own,' in folder and "Use the data folder" not in js
    assert "if (FOLDER.own && !recent.some((p) => same(p, FOLDER.path))) recent.unshift(FOLDER.path);" in folder
    mine = _section(_read("app.css"), "I")
    for rule in ('.menu-check.menu-radio::after', '.menu-item[aria-checked="true"] .menu-check.menu-box { background: var(--accent); }',
                 '.menu-item[aria-checked="true"] .menu-check svg, .menu-item[aria-checked="true"] .menu-check.menu-radio::after { opacity: 1; }'):
        assert rule in mine, rule


def test_a_tool_step_and_a_decided_request_are_disclosures():
    js = _read("app.js")
    fold = _function(js, "foldOpen")
    assert 'toggle.setAttribute("aria-expanded", String(open));' in fold and "fold.inert = !open;" in fold
    assert 'el.classList.toggle("still", !!e && !e.detail);' in fold   # from the keyboard: at once, the chevron too
    step = _function(js, "makeStep")
    assert '"aria-expanded": "false", "aria-controls": id' in step
    assert 'h("div", { class: "k-fold", id, inert: true }' in step and "foldOpen(el, head, fold, " in step
    assert "body.hidden" not in step   # the label and the head stay; only the part under it opens and shuts
    card = _function(js, "foldable")
    for text in ('title.setAttribute("aria-expanded", "false");', 'title.setAttribute("aria-controls", id);',
                 'title.title = "Details";', "title.append(chev);", 'h("div", { class: "k-fold", id, inert: true }',
                 'if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(e); }'):
        assert text in card, text
    assert "Show the details" not in js
    # a translated card's language is a choice of its own (the kit's .seg, its words kept, the chosen one aria-checked),
    # beside the title: never a button inside the title's disclosure, and moved out of it when the card is decided
    lang = _function(js, "localizeCard")
    assert 'h("div", { class: "seg card-lang", role: "radiogroup", "aria-label": "Card language" })' in lang
    assert 'role: "radio"' in lang and "segKeys(pick, choose);" in lang and "textContent = inEnglish" not in lang
    assert '(head && head.getAttribute("role") === "button" ? head : parts.title).after(pick);' in lang
    assert 'const pick = title.querySelector(".card-lang");' in card and "if (pick) title.after(pick);" in card
    css = _read("app.css")
    mine = _section(css, "I")
    assert ".approval.done > .card-lang { grid-column: 2; grid-row: 1; }" in mine
    # one column that never grows past its place (minmax(0, 1fr)): a long line or name is cut or wraps, never wider
    assert ".k-fold { display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: 0fr; opacity: 0;" in mine
    assert ".proj-fold { display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: 1fr; }" in mine
    assert ".open > .k-fold { grid-template-rows: 1fr; opacity: 1;" in mine
    assert ".still > .k-fold, .step.still .chev, .approval.done.still .approval-title > svg.chev { transition: none; }" in mine
    assert ".step.open > .step-head { background: var(--active); }" in mine          # pressed while open
    assert ".approval.done.open .approval-title { background: var(--active); }" in css
    assert ".approval.done.open .approval-title > svg.chev { transform: rotate(90deg); }" in css
    reduce = mine[mine.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".k-fold, .open > .k-fold { transition: opacity .15s ease; }" in reduce and ".chev, .chev-s" in reduce
    assert ".proj-fold.k-open { animation: none; }" in reduce   # a project's chats: no height either


def test_a_project_folds_with_the_open_chat_in_it_and_the_lab_name_is_words():
    js = _read("app.js")
    group = _function(js, "projectGroup")
    assert "const open = !SIDE.collapsed.has(p.id)," in group and "|| holdsActive" not in group
    assert '"aria-expanded": String(open), "aria-controls": listId' in group and "hidden: !open" in group
    assert "holdsActive && (!S.chatId || !open)" in group   # folded with the open chat in it: its head looks chosen
    assert "unfoldProject(chat.project);" in _function(js, "openChat")   # a chat opened from elsewhere shows in its project
    render = _function(js, "renderChats")
    assert 'closest("#chat-list [data-key]")' in render and "again.focus(" in render   # the focus stays on its row
    # a row's own keys only: Enter or Space on a project's ＋ or ⋯ are theirs (a new chat in it, its menu), never a fold;
    # a chat's row takes both, used up, so the key never goes on to answer the question it may ask (Forget this chat?)
    assert 'if (e.target !== head || (e.key !== "Enter" && e.key !== " ")) return;' in group
    item = _function(js, "chatItem")
    keys = item[item.index('if (e.target !== item || (e.key !== "Enter" && e.key !== " ")) return;'):]
    assert keys.index("e.preventDefault();") < keys.index("openChat(c.id);")
    # a row's name tip: its whole name, only while the row cuts it
    tip = _function(js, "nameTip")
    assert "t.scrollWidth > t.clientWidth" in tip and 'row.removeAttribute("title")' in tip
    assert "nameTip(item, " in item and "nameTip(head, p.name);" in group
    assert "title: c.title" not in item and "title: p.name" not in group
    # the lab's name in the sidebar is words: the top bar's Lab is the control
    assert '.lab-chip").addEventListener' not in js and ".lab-chip { cursor: pointer; }" not in _read("app.css")
    chip = re.search(r'<div class="lab-chip"[^>]*>', _read("index.html")).group(0)
    assert "role=" not in chip and "tabindex" not in chip


def test_the_chat_column_fits_and_its_targets_are_big_enough():
    css = _read("app.css")
    mine = _section(css, "I")
    assert ".empty { padding-top: clamp(16px, calc(6 * var(--vh)), 72px);" in mine
    assert ".hero-mark { width: 56px; height: 56px;" in mine and ":root.fit-tight .hero-mark { width: 40px; height: 40px;" in mine
    assert 'width="56" height="56"' in re.search(r'<img class="hero-mark"[^>]*>', _read("index.html")).group(0)
    assert re.search(r"\.suggestion \{[^}]*min-height: 48px", mine)
    assert ".sidebar .row-btn { min-width: 30px; min-height: 30px; }" in mine   # 24 px or more at 85 %
    assert ".composer textarea { min-height: 30px; padding-block: 4px; }" in mine   # the message box, a target too (was 26.5)
    assert ".sidebar button.link { min-height: 28px; }" in mine and ".composer-row :is(select, .compare-btn) { min-height: 30px; }" in mine
    assert ".attach-x { display: inline-grid; place-items: center; min-width: 28px; min-height: 28px;" in mine   # an attachment's ×
    touch = mine[mine.index("@media (pointer: coarse)"):]
    touch = touch[:touch.index("\n}\n")]
    for rule in (".sidebar .row-btn { min-width: 40px; min-height: 40px; }", ".menu-item { min-height: 44px; }",
                 ".composer-row :is(select, .compare-btn) { min-height: 40px; }", ".composer-row .send { width: 40px; height: 40px; }",
                 ".side-input { min-height: 40px; }", ".attach-x { min-width: 40px; min-height: 40px; }",
                 ".wake-bar .btn, :is(.compare-menu, .review-menu) .pane-menu-item { min-height: 40px; }",
                 ".slash-item { min-height: 40px;"):
        assert rule in touch, rule
    assert ".count" not in touch   # a project's count shows on touch as it does with a mouse
    phone = mine[mine.index("@container (max-width: 480px)"):]
    assert ".composer-row { flex-wrap: wrap;" in phone[:phone.index("\n}\n")]   # never squeezed: two rows
    # the queries come last in the section, after the rules they override
    first = min(mine.index(q) for q in ("@container", "@media"))
    assert all(line.startswith(("@", "}", "/*", " ")) for line in mine[first:].splitlines() if line.strip())
