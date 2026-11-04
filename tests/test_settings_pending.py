"""Settings keep Save and Cancel, checked in the page's own files. One function says what Save sends
(settingsBody), and the same function says what waits for Save: what it would send now that differs from what it sent
as the dialog opened. Save is offered only then; the button beside it reads Close while nothing waits and Cancel once
something does, and a dot on a section's name says where the change is. What acts at once stands in a card of its own
at the top of its page (.set-now): the phone through the hub on Account, Compare models on Models, the skills on
Memory, Phone access, Page size. On the Settings pages a switch acts at once and a tick box waits for Save (or for the
button of its own row); in a row, a skill's switch and a service's Pictures (a choice of three) act at once, and no list
does. The lab's roles picked for each person wait for Save like any field, which sends them to their own endpoint. The
head's × and Esc ask first while something waits, and do nothing while a Save is on its way; Enter in a box never closes the
dialog."""
import re
from pathlib import Path

import labharness

STATIC = Path(labharness.__file__).parent / "web" / "static"


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _function(js, name):
    """The body of a function of the script, by its name (braces counted)."""
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


def _dialog():
    page = _read("index.html")
    return page[page.index('<dialog id="settings-dialog"'):page.index("</dialog>", page.index('<dialog id="settings-dialog"'))]


def _page(name):
    """A Settings page's section, by its data-page."""
    dlg = _dialog()
    start = dlg.index('<section class="set-page" data-page="%s"' % name)
    return dlg[start:dlg.index("</section>", start)]


def _cards(section):
    """The .set-now cards of a page, each as its markup up to its own closing tag (divs counted)."""
    out = []
    for m in re.finditer(r'<div\b[^>]*\bclass="[^"]*\bset-now\b[^"]*"[^>]*>', section):
        depth, i = 0, m.start()
        for t in re.finditer(r"<div\b|</div>", section[i:]):
            depth += 1 if t.group(0) == "<div" else -1
            if depth == 0:
                out.append(section[i:i + t.end()])
                break
    return out


def test_one_function_says_what_save_sends_and_what_waits():
    js = _read("app.js")
    assert js.count("function settingsBody(") == 1
    body = _function(js, "settingsBody")
    save = _function(js, "saveSettings")
    # Save builds no body of its own: what settingsBody gives, the roles split off for their own endpoint
    assert "const sent = settingsBody(), { roles, ...body } = sent," in save and "body." not in save
    diff = _function(js, "settingsDiff")
    assert "settingsBody()" in diff and "SETS.start" in diff and "JSON.stringify(now[k]) !== JSON.stringify(start[k])" in diff
    assert "SETS.start = settingsBody();" in _function(js, "settingsStart")
    # the start is taken once every field is filled: the opening's last step (Claude Code's fields too)
    opening = _function(js, "openSettings")
    assert opening.rstrip()[:-1].rstrip().split("\n")[-1].strip().startswith("settingsStart();")
    assert opening.index("loadClaudeCode();") < opening.index("settingsStart();")
    # every setting Save sends has its field named after it in the dialog, so a change is found on its page (its dot);
    # the keys are the password boxes with a name, sent when typed or removed
    dlg = _dialog()
    sent = set(re.findall(r"\bbody\.(\w+) =", body))
    assert {"answer_words", "learn", "report_kinds", "notify_kinds", "smtp_host", "claude_code", "updates"} <= sent
    for name in sent:
        assert 'name="%s"' % name in dlg, name
    assert 'document.querySelectorAll("#settings-form input[type=password][name]")' in body
    assert "function settingField(name) { return document.querySelector('#settings-form [name=\"' + name + '\"]'); }" in js
    # weighed at every change in the form, and at a key's Remove (which fires no event of its own)
    assert '$("settings-form").addEventListener("input", settingsChanged);' in js
    assert '$("settings-form").addEventListener("change", settingsChanged);' in js
    clear = js[js.index('for (const b of document.querySelectorAll("[data-clear]"))'):]
    clear = clear[:clear.index("\n    }\n")]
    assert 'input.dataset.clear = "1"' in clear and "settingsPending();" in clear
    # a row's Remove is greyed where nothing is set (a key, the webhook's URL, the mail's password), the reason its tip
    assert 'if (clr) { clr.disabled = !src; clr.title = src ? "" : "Nothing to remove"; }' in opening
    # a load that fills the model lists again (a service added or removed) is no change of the person's
    refresh = _function(js, "refreshModels")
    assert "settingsFilled(Object.keys(picked));" in refresh and "settingsPending();" in refresh
    # the welcome redraws the model list in place after a key, a local model or a ChatGPT sign-in: the page lends it
    names = js[js.index("window.Kelvin = {"):]
    assert "refreshModels," in names[:names.index("};")]
    welcome = (STATIC / "welcome.js").read_text(encoding="utf-8")
    assert 'K.api("/api/bootstrap").then((b) => K.refreshModels(b))' in welcome


def test_what_acts_at_once_stands_in_a_card_at_the_top_of_its_page():
    # Compare models on Models since 2026-11-03: Compare under the message box, off unless switched on
    for page, control in (("account", 'id="relay-on"'), ("models", 'id="compare-on"'), ("phone", 'id="phone-on"'),
                          ("display", 'class="zoom-row"'), ("memory", 'id="global-know"')):
        section = _page(page)
        after = re.sub(r"<!--.*?-->", "", section[section.index("</h3>") + 5:], flags=re.S).lstrip()
        assert after.startswith("<div") and "set-now" in after[:after.index(">")], page   # first, under the title
        cards = _cards(section)
        assert len(cards) == 1 and control in cards[0], page
        assert 'type="checkbox"' not in cards[0], page
    assert "set-now" not in "".join(_page(p) for p in ("instruments", "data", "literature", "notifications", "updates"))
    # Memory: the skills (their switches, Show, Download and Add a skill…) act at once, so they stand in the card at the
    # top; what waits for Save (Learn from conversations, Instructions, About you) is under it, outside the card
    memory = _page("memory")
    skills = _cards(memory)[0]
    for part in ('id="global-know-add"', 'id="global-know-file"', 'id="global-know-count"'):
        assert part in skills, part
    for part in ('id="learn"', 'id="global-instructions"', 'id="global-notes"'):
        assert part not in skills and memory.index(skills) + len(skills) < memory.index(part), part
    # Memory is no section of one field: the card keeps its label (Skills) and the field under it keeps its own
    # (Memory), as Your account under Account's card; only a page's first field, not a card, gives its label to the title
    css = re.sub(r"/\*.*?\*/", "", _read("app.css"), flags=re.S)
    hidden = re.findall(r"([^{}]*?) > \.field-row:first-child > \.field-label \{ visibility: hidden; \}", css)
    one = [s for s in hidden if '[data-page="memory"]' in s]
    assert len(one) == 1 and one[0].strip().endswith("> .field:first-of-type:not(.set-now)"), one
    assert ".set-now:first-of-type + .field" not in css
    assert memory.index('id="skills-field" class="field set-now"') < memory.index('<div class="field">')
    # in their card the skills are its rows, without a frame of their own
    assert ".set-now > #global-know { border: 0; border-radius: 0; background: none; }" in css
    # drawn as the kit's cards are, from its tokens alone (light and dark)
    css = re.sub(r"/\*.*?\*/", "", _read("app.css"), flags=re.S)
    card = re.search(r"\.set-page > \.set-now \{([^}]*)\}", css).group(1)
    for decl in ("border: 1px solid var(--card-line)", "border-radius: var(--radius)", "background: var(--card)",
                 "box-shadow: var(--card-shadow)"):
        assert decl in card, decl
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", card)
    fit_s = _read("app.css")
    assert fit_s.index("/* ===== end fit P ===== */") < fit_s.index("/* ===== fit S: Settings, Save and Cancel ===== */") \
        < fit_s.index("/* ===== end fit S ===== */")


def test_a_switch_acts_at_once_and_a_tick_box_waits_for_save():
    js, dlg = _read("app.js"), _dialog()
    # every switch on the pages stands in a card of what acts at once, and turns at a press (swFlip)
    switches = re.findall(r'<button\b[^>]*\bclass="switch"[^>]*>', dlg)
    assert len(switches) == 3
    cards = "".join(c for p in ("account", "models", "phone", "display") for c in _cards(_page(p)))
    for sw in switches:
        assert sw in cards, sw
        assert 'swFlip($("%s"), ' % re.search(r'\bid="([\w-]+)"', sw).group(1) in js
    know = _function(js, "renderKnow")   # a skill's switch, in its row on Memory, acts at once too
    assert 'class: "switch"' in know and "swFlip(on, async (v) =>" in know
    # refused, it turns back (swFlip) and says why under the skill's own line, until it turns
    flip = _function(js, "swFlip")
    assert "try { await save(on); } catch (e) { swSet(sw, !on); }" in flip
    assert 'note.querySelector(".memory-meta").after(why = h("span", { class: "memory-meta bad", role: "status" }));' in know
    assert know.index("why.textContent = e.message;") < know.index("throw e;") < know.index("if (why) why.remove();")
    # every tick box waits for Save (Save sends it), or for the button of its own row, which reads it
    body = _function(js, "settingsBody")
    sent = set(re.findall(r"\bbody\.(\w+) =", body))
    by_button = {"data-move": "useDataFolder", "account-delete-everywhere": "deleteAccount"}
    for box in re.findall(r'<input\b[^>]*\btype="checkbox"[^>]*>', dlg):
        named = re.search(r'\bname="(\w+)"', box)
        ident = re.search(r'\bid="([\w-]+)"', box)
        if named:
            assert named.group(1) in sent, box
        else:
            assert ident and ident.group(1) in by_button, box
            assert '$("%s").checked' % ident.group(1) in _function(js, by_button[ident.group(1)]), box
    assert "swFlip(" not in body and 'role="switch"' not in "".join(re.findall(r'<label class="check[^"]*">.*?</label>', dlg))


def test_the_footer_reads_close_or_cancel_and_save_waits_for_a_change():
    dlg, js = _dialog(), _read("app.js")
    assert 'type="submit"' not in dlg   # nothing in Settings submits its form: Enter in a box took the first one, ×
    assert '<button id="settings-close" class="btn" type="button">Close</button>' in dlg
    assert re.search(r'<button id="save-settings" class="btn" type="button"[^>]*\bdisabled>Save</button>', dlg)
    assert '<button id="settings-x" class="icon-btn" type="button" aria-label="Close">' in dlg
    pending = _function(js, "settingsPending")
    assert "save.disabled = !names.length || SETS.saving;" in pending
    assert 'save.classList.toggle("primary", names.length > 0);' in pending
    assert 'names.length ? "Cancel" : "Close"' in pending
    assert 'close.disabled = $("settings-x").disabled = SETS.saving;' in pending   # nothing leaves while Save is on its way
    assert 'dot.hidden = !pages.has(b.dataset.page);' in pending and "settingsPages(names)" in pending
    assert '.closest(".set-page")' in _function(js, "settingsPages")
    # the dot on a section's name in the list on the left, heard as ", changed"
    assert 'b.append(h("span", { class: "set-dot", hidden: true }, h("span", { class: "sr-only" }, ", changed")));' in js
    css = re.sub(r"/\*.*?\*/", "", _read("app.css"), flags=re.S)
    dot = re.search(r"\.set-dot \{([^}]*)\}", css).group(1)
    assert "background: var(--accent)" in dot and "border-radius: 50%" in dot
    # Close and Cancel only leave: what waits is let go, the fields filled again at the next opening
    assert '$("settings-close").addEventListener("click", () => $("settings-dialog").close());' in js
    assert "keyKept(input);" in _function(js, "openSettings")   # a key removed and not saved: its box as before
    assert "input.placeholder = input.dataset.was;" in _function(js, "keyKept")


def test_x_and_esc_ask_while_something_waits_and_enter_closes_nothing():
    js, dlg = _read("app.js"), _dialog()
    leave = _function(js, "settingsLeave")
    assert leave.index("if (settingsPending().length)") < leave.index("const where = settingsWhere();") \
        < leave.index('askYes(where.length ? "Discard your changes on " + andList(where) + "?" : "Discard your changes?",') \
        < leave.index("if (!yes) return;") < leave.index('$("settings-dialog").close();')
    # Keep editing has the focus (danger): Enter or Esc there keeps Settings open. Not "Cancel", which in the footer
    # beside it lets the changes go
    assert '{ ok: "Discard", cancel: "Keep editing", danger: true }' in leave
    assert '$("settings-x").addEventListener("click", settingsLeave);' in js
    # Esc wherever the focus is in Settings (a click on its empty space leaves it on the page); a question over it is
    # answered No in the page. Never the browser's own close: an Esc gives no activation, so after a few the browser
    # closes Settings along with the question it asked, letting what waits go without Discard
    esc = js[js.index('document.addEventListener("keydown", (e) => {   // Esc while Settings is the dialog on top'):]
    esc = esc[:esc.index("\n    });")]
    assert 'e.key !== "Escape"' in esc and "!dlg.open" in esc and 'querySelectorAll("dialog[open]")' in esc
    assert 'if (over.classList.contains("ask-dialog")) { e.preventDefault(); over.close(); }' in esc
    assert esc.index("over.close();") < esc.rindex("e.preventDefault();") < esc.index("settingsLeave();")
    ask = _function(js, "askDialog")   # a question closed so is No: its close event answers it
    assert 'dlg.addEventListener("close", () => finish(none));' in ask
    cancel = js[js.index('$("settings-dialog").addEventListener("cancel"'):]
    assert cancel[:cancel.index("});")].count("settingsLeave();") == 1
    enter = _function(js, "settingsEnter")
    assert 'e.key !== "Enter"' in enter and "e.preventDefault();" in enter and '.closest(".key-row, .service-form")' in enter
    assert 'querySelectorAll("button:not([data-clear])")' in enter and "go.click();" in enter   # never a key's Remove
    assert 'box.enterKeyHint === "next"' in enter
    assert '$("settings-form").addEventListener("keydown", settingsEnter);' in js
    assert '$("settings-form").addEventListener("submit", (e) => e.preventDefault());' in js
    # Current password goes on to the new one, and an empty New password presses nothing: Change from it would take the
    # password off
    assert re.search(r'<input id="pw-old"[^>]*\benterkeyhint="next"', dlg)
    assert 'if (!row || (box.id === "pw-new" && !box.value)) return;' in enter
    assert enter.index('box.id === "pw-new" && !box.value') < enter.index("go.click();")


def test_a_password_is_removed_only_when_asked():
    # Change with an empty new password takes a local account's password off (accounts.py), and the API keys are then
    # kept unencrypted: asked first, Cancel focused (danger)
    change = _function(_read("app.js"), "changePassword")
    ask = change.index('askYes("Remove your password?\\n"')
    assert '!$("pw-new").value && user.password && !user.central' in change[:ask]
    assert '{ ok: "Remove", danger: true }' in change and "))) return;" in change[ask:]
    assert ask < change.index('await api("/api/account"')


def test_a_reload_from_settings_says_what_it_lets_go():
    # Account → Use a Kelvin hub (or change) reloads the page: with something waiting for Save its question says so
    js = _read("app.js")
    link = _function(js, "hubLink")
    assert 'const lost = $("settings-dialog").open ? settingsWhere() : [];' in link
    assert '(lost.length ? "\\nThe page then reloads, and your changes on " + andList(lost) + " are lost." : "")' in link
    assert link.index("const lost") < link.index("location.reload();")
    where = _function(js, "settingsWhere")
    assert "settingsPages(settingsDiff())" in where and 'querySelectorAll(".set-nav button")' in where
    # the roles are emptied at each opening and filled again as they are saved: picks not saved are let go with the
    # rest of what waited
    opening = _function(js, "openSettings")
    assert '$("roles-list").replaceChildren();' in opening and '$("roles-list").hidden = true;' in opening
    assert opening.index('$("roles-list").replaceChildren();') < opening.index("rolesState();")


def test_a_services_pictures_is_a_choice_that_acts_at_once():
    # no list on the Settings pages acts at once: a service's Pictures (Auto | On | Off) is the kit's choice of three
    # (.seg, aria-checked, the arrows), kept at a press (PATCH), on a line of its own in its row
    js = _read("app.js")
    svc = _function(js, "renderServices")
    assert 'h("select"' not in svc and "new Option(" not in svc
    assert 'h("div", { class: "seg svc-pics", role: "radiogroup", "aria-label": "Pictures for " + s.name })' in svc
    assert 'role: "radio"' in svc and 'b.setAttribute("aria-checked", String(on));' in svc
    assert '[["", "Auto", ' in svc and '["on", "On", ' in svc and '["off", "Off", ' in svc
    assert "segKeys(pictures, choose);" in svc and "thumb(pictures);" in svc
    assert '{ method: "PATCH", body: { pictures: v1 } }' in svc and 'pictures.setAttribute("aria-busy", "true");' in svc
    assert 'catch (e) { $("svc-hint").textContent = e.message; want = s.pictures || ""; }' in svc   # turned back
    # every list (select) in Settings is named after the setting Save sends from it
    sent = set(re.findall(r"\bbody\.(\w+) =", _function(js, "settingsBody")))
    for sel in re.findall(r"<select\b[^>]*>", _dialog()):
        named = re.search(r'\bname="(\w+)"', sel)
        assert (named and named.group(1) in sent) or 'id="svc-preset"' in sel, sel   # the service form's preset: Add's
    css = re.sub(r"/\*.*?\*/", "", _read("app.css"), flags=re.S)
    assert ".seg.svc-pics > button { min-height: 30px;" in css


def test_saving_closes_and_a_refusal_keeps_what_waits():
    js = _read("app.js")
    # × and Esc do nothing while a Save is on its way (Close and × are greyed): its answer closes Settings or says why
    assert 'if (SETS.asking || SETS.saving || !$("settings-dialog").open) return;' in _function(js, "settingsLeave")
    save = _function(js, "saveSettings")
    ok = save[save.index("try {"):save.index("} catch (e) {")]
    refused = save[save.index("} catch (e) {"):save.index("} finally {")]
    assert 'await api("/api/settings", { body });' in ok and "SETS.start = sent;" in ok
    assert '$("settings-dialog").close();' in ok and 'toast(!withRoles ? "Settings saved." :' in ok
    assert '$("settings-msg").textContent = (withRoles ? "Roles saved. " : "") + e.message;' in refused
    assert "close()" not in refused and "SETS.start" not in refused
    after = save[save.index("} finally {"):]
    assert "SETS.saving = false;" in after
    # focus lost to the greyed Save (a PIN asked on the way) goes to what the refusal is about: a role, or Save
    assert "const lost = !document.activeElement || document.activeElement === document.body;" in after
    assert 'if ($("settings-dialog").open && lost) {' in after
    assert ('const to = $("roles-msg").textContent ? $("roles-list").querySelector("select:enabled") : '
            '$("save-settings");') in after
    assert after.index("settingsPending();") < after.index("to.focus();")   # once Save is offered again


def test_the_roles_picked_wait_for_save_like_any_field():
    """Lab roles (Instruments, with accounts): a role to pick for each person, shown as saved and greyed until the
    owner's PIN is set. The picks are fields of the dialog: a change puts the dot on Instruments, Close reads Cancel, × and Esc
    ask, and the dialog's Save sends them to the roles' own endpoint before the rest (the PIN asked there, by api());
    refused, nothing else is sent, the reason shows under the roles and their page is shown."""
    js, dlg, page = _read("app.js"), _dialog(), _page("instruments")
    # no Save roles nor a Cancel of their own: one Save per dialog
    assert 'id="roles-save"' not in dlg and 'id="roles-cancel"' not in dlg and 'id="roles-actions"' not in dlg
    assert "roles-save" not in js and "roles-cancel" not in js and "roles-actions" not in js
    # the picks sit in a fieldset named after what Save sends, so settingsPages finds their page (the dot)
    assert re.search(r'<fieldset id="roles-list" name="roles"[^>]*\bhidden></fieldset>', page)
    # shown at once, as every field is: no button to show them first
    assert 'id="roles-edit"' not in dlg and "roles-edit" not in js and "Give roles" not in js + dlg
    assert "function editRoles" not in js and "function rolesButton" not in js
    assert re.search(r'<span id="roles-msg" class="hint bad" role="status"></span>', page)
    body = _function(js, "settingsBody")
    assert "body.roles = rolesPicked();" in body
    picked = _function(js, "rolesPicked")
    assert "if (!picks.length) return undefined;" in picked   # no one to pick for: nothing waits
    assert "if (sel.value) roles[sel.dataset.person] = sel.value;" in picked   # no role: left out (a trainee)
    # shown as they are saved: nothing waits until a pick changes
    fill = _function(js, "rolesFill")
    assert 'sel.value = r.roles[p.name] || "";' in fill and '"data-person": p.name' in fill
    assert "people = r && r.accounts ? r.people : [];" in fill and '$("roles-list").hidden = !people.length;' in fill
    assert fill.index("replaceChildren(") < fill.index('settingsFilled(["roles"]);')
    # filled only while empty (once per opening): a later look at the roles (a PIN set, Where my notifications go,
    # the roles kept by Save) never lets go of a pick that waits; greyed without a PIN, the reason its tip
    state = _function(js, "rolesState")
    assert 'if (!$("roles-list").children.length) rolesFill();' in state
    assert '$("roles-list").disabled = !r.pin;' in state
    assert '$("roles-list").title = r.pin ? "" : "Roles need the lab owner\'s PIN";' in state
    assert js.count("rolesFill();") == 1   # called from rolesState alone
    # a pick is a change in the form like any other: weighed at its change event, found on Instruments
    assert '$("settings-form").addEventListener("change", settingsChanged);' in js
    assert '<section class="set-page" data-page="instruments"' in dlg and '<fieldset id="roles-list"' in page
    # Save: the roles first, to /api/roles (api() asks the owner's PIN on its 403 and tries again), only when they
    # changed; kept, they no longer wait; then the rest, as before
    save = _function(js, "saveSettings")
    assert 'withRoles = settingsDiff().includes("roles");' in save
    assert 'await api("/api/roles", { body: { roles } }).catch((e) => { e.roles = true; throw e; });' in save
    assert save.index('api("/api/roles"') < save.index("SETS.start.roles = roles;") < save.index('api("/api/settings"')
    api = js[js.index("async function api(path, opts = {}) {"):js.index("function askPin(")]
    assert "if (res.status === 403 && data && data.pin && opts.body && !opts.noPin)" in api
    # refused: under the roles, their page shown, the message at the bottom cleared; nothing else was sent
    refused = save[save.index("} catch (e) {"):save.index("} finally {")]
    assert 'if (e.roles) {' in refused and '$("roles-msg").textContent = e.message;' in refused
    assert 'settingsPage("instruments");' in refused and "close()" not in refused
    assert '$("roles-msg").textContent = "";' in save[:save.index("try {")]   # cleared at each Save
    # the roles' own endpoint is unchanged: the PIN, and once there are roles an owner signed in
    gov = (STATIC.parent / "governance.py").read_text(encoding="utf-8")
    assert 'Route("/api/roles", roles_view, methods=["GET", "POST"])' in gov
