"""Short answers (VISION item 9): the model is asked for a word budget and keeps to it itself; Kelvin doesn't
rewrite its answers."""
import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from labharness.web import brevity  # noqa: E402
from test_compare import bot, compare, say  # noqa: E402
from test_web import Scripted, client_for, parts, stream_turn  # noqa: E402,F401

# about 260 words of the kind a model writes when it is not asked to be short
LONG = ("The lock-in signal at 1.5 K shows a clear peak at 2.41 V. " * 4 + "\n\n" +
        "Before going further it is worth recalling why the gate voltage matters here, since the carrier density "
        "in the channel follows it and with it the conductance, the screening and the size of every feature you "
        "see in the sweep. " * 5 + "\n\nIn summary, run 20261023-101500-gate_sweep looks fine; would you like me "
        "to repeat it with a finer step near the peak? Let me know if there is anything else I can help with.")
SHORT = "Peak at **2.41 V** (1.5 K), run 20261023-101500-gate_sweep. Repeat with a finer step near the peak?"
ASKED = "(Answer in at most %d words"


def test_counting_the_words_a_person_reads():
    assert brevity.words("Peak at **2.41 V**, FWHM 0.38 V.") == 7
    assert brevity.words("See [Smith 2021](https://arxiv.org/abs/2101.00001) for I-V curves.") == 6   # not the link
    assert brevity.words("Run it:\n```yaml\nplan: gate_sweep\nsweep: {smu.V: {from: 0, to: 5}}\n```\nThen look.") == 4
    assert brevity.words("温度是一点五开") == 4                       # two CJK characters, one word
    assert brevity.words("") == 0
    assert brevity.words(LONG) > 200 and brevity.words(SHORT) < 30


def test_the_reminder_rides_with_each_message_and_is_never_stored():
    history = [{"role": "user", "text": "sweep the gate"}, {"role": "assistant", "blocks": []},
               {"role": "tool", "results": []}, {"role": "user", "text": "and now?"}]
    sent = brevity.with_reminder(history, 150)
    assert [m.get("brief") for m in sent] == [brevity.REMINDER.format(words=150), None, None,
                                              brevity.REMINDER.format(words=150)]
    assert all("brief" not in m for m in history)                       # the chat as stored is untouched
    assert brevity.with_reminder(history, 0) is history                  # any length: nothing added


def test_the_model_is_asked_for_a_short_answer(parts):
    model = Scripted([say(SHORT)])
    client, headers = client_for(parts, {"scripted": model})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "how did the gate sweep go?", "scripted:m1")
        assert "# Short answers" in model.systems[0] and "under 200 words" in model.systems[0]
        latest = model.asked[0][-1]
        assert latest.startswith("how did the gate sweep go?") and ASKED % 200 in latest
        assert len(model.systems) == 1 and events[-1] == {"type": "turn_end", "reason": "end"}   # one call, no rewrite
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()["messages"]
        assert saved[0]["text"] == "how did the gate sweep go?" and "brief" not in saved[0]


def test_a_long_answer_stays_as_the_model_wrote_it(parts):
    model = Scripted([say(LONG)])
    client, headers = client_for(parts, {"scripted": model})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "derive it in full, every step", "scripted:m1")
        assert not {"condensing", "condensed"} & {e["type"] for e in events} and len(model.systems) == 1
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()["messages"][-1]
        assert saved["blocks"][0]["text"] == LONG and "full" not in saved


def test_earlier_messages_replay_unchanged(parts):
    """Each message carries the same reminder every time, so the request only grows at its end (prompt cache)."""
    model = Scripted([say(SHORT), say(SHORT)])
    client, headers = client_for(parts, {"scripted": model})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        stream_turn(client, headers, chat["id"], "how did it go?", "scripted:m1")
        stream_turn(client, headers, chat["id"], "and the second run?", "scripted:m1")
        assert model.asked[1][0] == model.asked[0][0] and ASKED % 200 in model.asked[1][1]


def test_the_budget_follows_the_setting(parts):
    runtime, settings, store = parts
    settings.update({"answer_words": "off"})
    model = Scripted([say(LONG), say(SHORT)])
    client, headers = client_for(parts, {"scripted": model})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        stream_turn(client, headers, chat["id"], "how did it go?", "scripted:m1")
        assert "Short answers" not in model.systems[0] and "(Answer in" not in model.asked[0][-1]

        settings.update({"answer_words": "100"})
        stream_turn(client, headers, chat["id"], "and now?", "scripted:m1")
        assert "under 100 words" in model.systems[1] and ASKED % 100 in model.asked[1][-1]
        assert client.get("/api/settings", headers=headers).json()["settings"]["answer_words"] == 100


def test_compared_models_are_each_asked_for_short_answers(parts):
    alpha = bot("alpha", [say(LONG)])
    beta = bot("beta", [say("Beta: peak at 2.41 V.")])
    client, headers = client_for(parts, {"alpha": alpha, "beta": beta})
    with client:
        chat = client.post("/api/chats", json={"model": "alpha:m1"}, headers=headers).json()
        events = compare(client, headers, chat["id"], "how did it go?", ["alpha:m1", "beta:m1"])
        assert ASKED % 200 in alpha.asked[0][-1] and ASKED % 200 in beta.asked[0][-1]
        assert not {"condensing", "condensed"} & {e["type"] for e in events}
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()
        lane = saved["messages"][0]["compare"]["lanes"][0]["messages"][-1]
        assert lane["blocks"][-1]["text"] == LONG and "full" not in lane
