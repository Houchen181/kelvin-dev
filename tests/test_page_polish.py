"""The page's smaller ways forward (review of 0.3.45, wave 4: 7.6, and wave 2's amber dot).

- Nothing said Kelvin was unreachable: the page went on showing what it last heard, as if all were well.
- One part of the state that threw while drawing skipped every part after it, at every poll (the approval cards and
  the alarms among them); one round of the running run's view that threw ended its polling for good.
- The sign-in screen read what Kelvin was busy with once, and kept saying it.
- An alarm's Got it stayed disabled for good when the acknowledge didn't reach Kelvin.
- An approval card's countdown counted ticks, so it drifted after the computer slept.
- Home's blocks stayed as they were, with no word, when loading them failed.
- A chat's amber "asks for your approval" dot stayed after its card was answered elsewhere and closed by the poll.
"""
import re

import pytest

pytest.importorskip("starlette")

from labharness.web.app import STATIC  # noqa: E402

JS = (STATIC / "app.js").read_text(encoding="utf-8")
HOME = (STATIC / "home.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def js_function(name, text=None):
    text = JS if text is None else text
    start = text.index("function %s(" % name)
    return text[start:text.index("\n  }\n", start)]


# ------------------------------------------------------------------ Kelvin not answering, said

def test_the_page_says_when_kelvin_isnt_answering_and_stops_at_the_first_answer():
    notices = HTML[HTML.index('<div id="notices"'):HTML.index('id="alerts"')]
    assert 'id="unreachable"' in notices and 'role="alert"' in notices and 'id="unreachable-text"' in notices
    assert "const REACH_MS = 10000;" in JS
    reach = js_function("reach")
    assert "if (ok) { REACH.ok = Date.now(); bar.hidden = true; return; }" in reach
    assert "if (quiet * 1000 < REACH_MS) return;" in reach and "Kelvin isn't answering" in reach
    state = js_function("readState")
    asked = state.index('await api("/api/state"')
    assert "const slow = setTimeout(() => reach(false), REACH_MS);" in state[:asked]   # one that hangs says so too
    failed = state[asked:state.index("reach(true);")]
    assert "reach(false); return;" in failed and "finally { clearTimeout(slow); }" in failed


# ------------------------------------------------------------------ one part that fails never stops the rest

def test_one_part_of_the_state_that_fails_to_draw_never_skips_the_rest():
    part = js_function("part")
    assert "try {" in part and "catch (e) { failed(e); return undefined; }" in part
    assert 'typeof r.then === "function" ? r.catch(failed) : r' in part   # an async part's failure is caught too
    state = js_function("readState")
    body = state[state.index("reach(true);") + len("reach(true);"):]
    # every statement of the drawing is a part of its own, but the two that only keep the state
    top = [line.strip() for line in body.splitlines() if re.match(r"^    \S", line)]
    loose = [line for line in top if not line.startswith(("part(", "await part(", "});", "})"))]
    assert loose == ["PROT.busy = s.busy;", "S.last = s;"], loose
    for drawn in ("renderAlerts(s.alarms, s.incident)", "renderWatches(", "makeApproval(p)", "renderInstruments()",
                  "wakeState(s)", "termState(s)", "queueFromState(s.queue)", 'emit("state", s)'):
        before = body[:body.index(drawn)]
        assert before.rfind("part(") > before.rfind("\n    })"), drawn   # inside the part opened last


def test_the_running_runs_view_goes_on_after_a_round_fails_to_draw():
    live = js_function("pollLive")
    guarded = live[live.index("try {", live.index("/api/live")):]
    assert guarded.index("renderRunView(tab);") < guarded.index("catch (e) {")
    failed = guarded[guarded.index("catch (e) {"):]
    assert "console.error(" in failed and "await sleep(1000);" in failed and "continue;" in failed


# ------------------------------------------------------------------ the sign-in screen, Got it, the countdown

def test_the_sign_in_screen_reads_what_kelvin_is_busy_with_again_while_it_shows():
    show = js_function("showSignIn")
    assert "const SIGNIN = { watch: 0 };" in JS
    watch = show[show.index("clearInterval(SIGNIN.watch);"):]
    assert watch.index("clearInterval(SIGNIN.watch);") < watch.index("SIGNIN.watch = setInterval(")   # never two
    every = watch[watch.index("SIGNIN.watch = setInterval("):watch.index("}, 3000);")]
    assert 'if ($("signin").hidden) { clearInterval(SIGNIN.watch); return; }' in every
    assert 'await api("/api/session", { timeout: 8000 }); busy(s); setupNote(s);' in every
    assert "if (setupNote(session)) {" not in show   # the busy line was read again only while a set-up went on


def test_a_got_it_that_didnt_reach_kelvin_can_be_pressed_again():
    got = js_function("gotIt")
    failed = got[got.index("catch (e) {"):]
    assert "if (e.status !== 404) {" in failed and "ok.disabled = false;" in failed
    assert "Got it didn't reach Kelvin" in failed and "/* gone */" not in got


def test_an_approval_cards_countdown_keeps_to_the_clock_after_the_computer_slept():
    card = js_function("makeApproval")
    assert "const until = Date.now() + Math.max(0, Number(ev.seconds_left) || 0) * 1000;" in card
    tick = card[card.index("const tick = () => {"):card.index("tick();")]
    assert "const left = Math.round((until - Date.now()) / 1000);" in tick
    assert "left -= 1" not in card and "let left = ev.seconds_left" not in card


# ------------------------------------------------------------------ Home's blocks, and the amber dot

def test_homes_blocks_say_when_they_couldnt_be_loaded_and_are_tried_again():
    load = js_function("loadBlocks", HOME)
    end = load.index("\n      return;\n    }", load.index("catch (e) {"))   # the catch's own end
    failed = load[load.index("catch (e) {"):end]
    assert 'blocksNote("Couldn\'t load these blocks (" + e.message' in failed
    assert "H.blocksAgain = setTimeout(() => { if (shown()) loadBlocks(ring); }, BLOCKS_AGAIN_MS);" in failed
    done = load[end:]
    assert done.index('blocksNote("")') < done.index("renderBlocks(")   # the note goes once they load
    assert "clearTimeout(H.blocksAgain);" in load and 'K.api("/api/home", { timeout: 15000 })' in load
    assert "p.blocksNote, p.blocks));" in js_function("build", HOME)
    css = (STATIC / "home.css").read_text(encoding="utf-8")
    assert ".home-note {" in css and ".home-note[hidden] { display: none; }" in css


def test_a_chats_amber_dot_goes_with_its_last_open_card_however_it_closes():
    closed = js_function("cardClosed")
    assert "if (!S.cardChat.delete(id) || [...S.cardChat.values()].includes(chatId)) return;" in closed
    assert "if (S.asking.delete(chatId)) renderChats();" in closed
    card = js_function("makeApproval")
    assert card.index("S.approvals.delete(ev.id);") < card.index("cardClosed(ev.id);")   # the poll's close too
    handle = js_function("handle")
    asked = handle[handle.index('case "approval":'):handle.index('case "approval_closed"')]
    assert "if (!here && !ev.auto) {" in asked and "S.cardChat.set(ev.id, chatId);" in asked   # auto mode asks no one
    ended = handle[handle.index('case "approval_closed"'):]
    ended = ended[:ended.index("break;")]
    assert "cardClosed(ev.id);" in ended and "S.asking.delete(chatId)" not in ended
    assert "for (const [id, c] of S.cardChat) if (c === chatId) S.cardChat.delete(id);" in js_function("finishLive")
