"""The chat window as the user asked for it on 2026-11-03, checked in the page's own files: Kelvin opens on the chat
(never Home), Compare hides until Settings → Models switches it on (off by default), an answer is headed Kelvin alone
(only Compare's side-by-side lanes say their model, as comparing them is the point), a message's Copy is its icon
alone, and an answer has no Second opinion button under it."""
import re
from pathlib import Path

import pytest

import labharness
from labharness.web.settings import Settings

STATIC = Path(labharness.__file__).parent / "web" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
WELCOME = (STATIC / "welcome.js").read_text(encoding="utf-8")


def _fn(name, js=JS):
    """The body of a function of the script, by its name (braces counted)."""
    start = js.index("function %s(" % name)
    i, depth = js.index("{", start), 0
    for j in range(i, len(js)):
        depth += {"{": 1, "}": -1}.get(js[j], 0)
        if depth == 0:
            return js[start:j + 1]
    raise AssertionError(name)


def _class(name):
    start = JS.index("class %s {" % name)
    return JS[start:JS.index("\n  }\n", start)]


def test_kelvin_opens_on_the_chat_whichever_side_was_left_on():
    assert 'setMode("chat", true);' in _fn("initMode")
    assert 'store("lh.mode"' not in JS   # the side is neither kept nor read back
    assert '<button id="mode-chat" class="on"' in PAGE and 'aria-selected="true">Chat</button>' in PAGE
    finish = _fn("finish", WELCOME)   # the first start's Done or Skip: the chat, not Home
    assert 'safe(K.setMode, "chat");' in finish and '"home"' not in finish
    assert "if (S.mode !== \"home\") $(\"input\").focus();" in _fn("init")


def test_compare_is_off_by_default_and_a_switch_in_settings_models_shows_it(tmp_path):
    s = Settings(tmp_path / "settings.json")
    assert s.public()["compare_models"] is False
    s.update({"compare_models": "on"})
    assert s.public()["compare_models"] is True and Settings(tmp_path / "settings.json").public()["compare_models"]
    s.update({"compare_models": "off"})
    assert s.public()["compare_models"] is False
    # under the message box it is hidden until then; the switch stands at the top of Models, acting at once
    assert re.search(r'<div id="compare-wrap" class="pane-menu-wrap compare-wrap" hidden>', PAGE)
    dlg = PAGE[PAGE.index('<dialog id="settings-dialog"'):]
    start = dlg.index('<section class="set-page" data-page="models"')
    models = dlg[start:dlg.index("</section>", start)]
    assert models.index('id="compare-on"') < models.index('id="default-model"')
    assert 'role="switch"' in models[models.index('id="compare-field"'):models.index('id="hub-usage"')]
    assert '<span class="field-label">Compare models</span>' in models
    apply = _fn("applyCompare")
    assert '$("compare-wrap").hidden = !on;' in apply and "S.compare = [];" in apply and "compareLabel();" in apply
    assert 'body: { compare_models: on ? "on" : "off" }' in _fn("setCompare") and "applyCompare();" in _fn("setCompare")
    assert 'swFlip($("compare-on"), setCompare);' in JS and "applyCompare();" in _fn("saveSettings")
    assert 'swSet($("compare-on"), compareOn());' in _fn("openSettings")
    assert "compare_models" not in _fn("settingsBody")   # a switch: never one of what waits for Save
    # off, a message goes to the chosen model alone, and continuing an old comparison doesn't switch it back on
    assert "const extra = compareOn() ? S.compare.filter((k) => k !== model) : [];" in _fn("sendMessage")
    assert "if (compareOn() && last && last.compare)" in _fn("chooseLane")


def test_the_settings_route_takes_the_switch_at_once(tmp_path):
    pytest.importorskip("mcp")
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness import ScriptedApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.store import ChatStore
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"]))
    h = {"X-Kelvin-Token": settings.access_key}
    assert client.get("/api/bootstrap", headers=h).json()["settings"]["compare_models"] is False
    r = client.post("/api/settings", json={"compare_models": "on"}, headers=h)
    assert r.status_code == 200 and r.json()["settings"]["compare_models"] is True
    assert client.get("/api/bootstrap", headers=h).json()["settings"]["compare_models"] is True


def test_an_answer_is_headed_kelvin_alone_and_compares_lanes_say_their_model():
    reply = _class("Reply")
    assert 'this.who = h("span", {}, "Kelvin");' in reply and "setModel(key) { if (key) this.model = key; }" in reply
    assert '"Kelvin · " +' not in JS and "this.who.textContent" not in JS
    lanes = _class("CompareView")
    assert 'h("span", { class: "lane-model" }, S.labels[key] || key.split(":").pop())' in lanes
    assert ".lane-model {" in CSS


def test_a_messages_copy_is_its_icon_alone_and_ticks_once_copied():
    finish = _class("Reply")[_class("Reply").index("finish() {"):]
    bubble = _fn("userBubble")
    for part in (finish, bubble):
        button = re.search(r'h\("button", \{ class: "link soft copy-btn"[^\n]*\n?', part).group(0)
        assert 'title: "Copy"' in button and '"aria-label": "Copy ' in button and button.rstrip().endswith('icon("copy"));')
        assert ', "Copy")' not in part   # no word beside the icon
    copy = _fn("copyText")
    assert 'if (btn.classList.contains("copy-btn"))' in copy and 'btn.replaceChildren(icon("check"));' in copy
    assert 'btn.title = "Copied";' in copy and 'btn.replaceChildren(icon("copy")); btn.title = "Copy";' in copy
    assert 'said.textContent = "Copied";' in copy   # a Copy that has its word (the experiment's code) keeps it
    assert ".msg-tools > .copy-btn { padding: 5px; border-color: transparent; background: none;" in CSS
    assert ".msg-tools > .copy-btn.copied { color: var(--ok); }" in CSS


def test_an_answer_has_no_second_opinion_button():
    assert "function offerReview(" not in JS and "function askReview(" not in JS
    assert '"Second opinion")' not in JS and "offerReview(" not in JS
    assert "offerRecheck(S.lastReply);" in _fn("renderChat")   # a second opinion given before keeps its re-check
    send = _fn("sendMessage")
    assert 'for (const el of document.querySelectorAll(".msg-actions")) el.remove();' in send   # the latest answer's only
    assert '<label for="reviewer" title="Another model reviews every plan before it runs">Second opinion on plans</label>' in PAGE
