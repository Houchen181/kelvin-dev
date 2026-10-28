"""Short answers (VISION item 9): a budget in the prompt, long answers condensed with the full one kept."""
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


def test_counting_the_words_a_person_reads():
    assert brevity.words("Peak at **2.41 V**, FWHM 0.38 V.") == 7
    assert brevity.words("See [Smith 2021](https://arxiv.org/abs/2101.00001) for I-V curves.") == 6   # not the link
    assert brevity.words("Run it:\n```yaml\nplan: gate_sweep\nsweep: {smu.V: {from: 0, to: 5}}\n```\nThen look.") == 4
    assert brevity.words("温度是一点五开") == 4                       # two CJK characters, one word
    assert brevity.words("") == 0
    assert brevity.words(LONG) > 200 and brevity.words(SHORT) < 30


def test_a_long_answer_is_condensed_and_the_full_one_kept(parts):
    model = Scripted([say(LONG), say(SHORT)])
    client, headers = client_for(parts, {"scripted": model})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "how did the gate sweep go?", "scripted:m1")
        kinds = [e["type"] for e in events]
        assert kinds.index("condensing") < kinds.index("condensed") < kinds.index("turn_end")
        done = next(e for e in events if e["type"] == "condensed")
        assert done["text"] == SHORT and done["full_words"] == brevity.words(LONG) and done["words"] < 30
        assert "# Short answers" in model.systems[0] and "under 200 words" in model.systems[0]
        assert model.systems[1].startswith("You shorten answers") and "at most 200 words" in model.systems[1]
        assert "how did the gate sweep go?" in model.prompts[1][0] and LONG.strip() in model.prompts[1][0]
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()["messages"][-1]
        assert [b["text"] for b in saved["blocks"] if b["type"] == "text"] == [SHORT]
        assert saved["full"] == LONG.strip() and saved["full_words"] == brevity.words(LONG)


def test_a_short_answer_is_left_alone(parts):
    model = Scripted([say(SHORT)])
    client, headers = client_for(parts, {"scripted": model})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "how did it go?", "scripted:m1")
        assert not {"condensing", "condensed"} & {e["type"] for e in events} and len(model.systems) == 1


def test_a_long_answer_the_user_asked_for_stays(parts):
    model = Scripted([say(LONG), say("KEEP: you asked for the full derivation")])
    client, headers = client_for(parts, {"scripted": model})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "derive it in full, every step", "scripted:m1")
        done = next(e for e in events if e["type"] == "condensed")
        assert done["kept"] == "you asked for the full derivation" and "text" not in done
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()["messages"][-1]
        assert saved["blocks"][0]["text"] == LONG and saved["kept"] and "full" not in saved


def test_when_condensing_fails_the_full_answer_stays(parts):
    model = Scripted([say(LONG)])                                    # nothing left to answer the condensing with
    client, headers = client_for(parts, {"scripted": model})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "how did it go?", "scripted:m1")
        assert next(e for e in events if e["type"] == "condensed") == {"type": "condensed"}
        assert events[-1] == {"type": "turn_end", "reason": "end"}
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()["messages"][-1]
        assert saved["blocks"][0]["text"] == LONG and "full" not in saved


def test_the_budget_follows_the_setting(parts):
    runtime, settings, store = parts
    settings.update({"answer_words": "off"})
    model = Scripted([say(LONG)])
    client, headers = client_for(parts, {"scripted": model})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "how did it go?", "scripted:m1")
        assert "Short answers" not in model.systems[0] and "condensing" not in {e["type"] for e in events}

        settings.update({"answer_words": "100"})
        model.turns = [say(" ".join(["word"] * 150)), say(SHORT)]
        events = stream_turn(client, headers, chat["id"], "and now?", "scripted:m1")
        assert "under 100 words" in model.systems[1] and "at most 100 words" in model.systems[2]
        assert next(e for e in events if e["type"] == "condensed")["text"] == SHORT
        assert client.get("/api/settings", headers=headers).json()["settings"]["answer_words"] == 100


def test_compared_answers_are_condensed_each(parts):
    alpha = bot("alpha", [say(LONG), say("Alpha, short: peak at 2.41 V.")])
    beta = bot("beta", [say("Beta: peak at 2.41 V.")])
    client, headers = client_for(parts, {"alpha": alpha, "beta": beta})
    with client:
        chat = client.post("/api/chats", json={"model": "alpha:m1"}, headers=headers).json()
        events = compare(client, headers, chat["id"], "how did it go?", ["alpha:m1", "beta:m1"])
        condensed = [e for e in events if e["type"] == "condensed"]
        assert [(e["lane"], e["text"]) for e in condensed] == [(0, "Alpha, short: peak at 2.41 V.")]
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()
        lane = saved["messages"][0]["compare"]["lanes"][0]["messages"][-1]
        assert lane["full"] == LONG.strip() and lane["blocks"][-1]["text"] == "Alpha, short: peak at 2.41 V."
