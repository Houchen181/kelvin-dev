"""Pictures in the portal's chats: look_at's plots kept with the chat and sent to models that see them (captions to
the rest), and photos and PDFs the person attaches."""
import asyncio
import base64

import pytest

pytest.importorskip("mcp")
pytest.importorskip("numpy")

from labharness import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.pictures import png  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted, client_for, parts  # noqa: E402,F401


class Seeing(Scripted):
    sees_images = True


def look_turns():
    return [[{"type": "tool_call", "id": "l1", "name": "look_at", "input": {"run_id": "last"}}],
            [{"type": "text", "text": "A clean peak."}], [{"type": "text", "text": "Still clean."}]]


def run_day(tmp_path, provider, texts=("how does it look?",), attachments=None):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 4), tmp_path / "data")
    assert "done" in rt.run("plan: gate\nsweep: {smu.V: {from: 0, to: 1, points: 11}}\nmeasure: [lockin.X]")
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")
    events = []

    async def emit(e):
        events.append(e)

    async def day():
        await agent.start()
        try:
            for text in texts:
                agent.claim(chat["id"], "scripted:m1")
                await agent.run_turn(chat["id"], text, "scripted:m1", provider, emit,
                                     attachments=attachments(agent, chat["id"]) if attachments else None)
        finally:
            await agent.close()
    asyncio.run(day())
    return store, store.load(chat["id"]), events, agent


def test_a_plot_is_kept_with_the_chat_and_shown_to_a_model_that_sees(tmp_path):
    provider = Seeing(look_turns())
    seen = []
    stream = provider.stream

    async def keep(model, system, history, tools, emit):
        seen.append(history)
        return await stream(model, system, history, tools, emit)
    provider.stream = keep
    store, chat, events, _ = run_day(tmp_path, provider, ("how does it look?", "and now?"))
    [result] = next(m for m in chat["messages"] if m["role"] == "tool")["results"]
    [kept] = result["images"]
    assert kept["mime"] == "image/png" and store.media(chat["id"], kept["file"])[:4] == b"\x89PNG"
    assert "data" not in kept                                              # the chat keeps the file's name only
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["images"] == [kept["file"]] and "Plot: lockin.X" in end["content"]
    sent = next(r for m in seen[1] if m["role"] == "tool" for r in m["results"])
    assert base64.b64decode(sent["images"][0]["data"])[:4] == b"\x89PNG"  # inline for the model, every step
    assert seen[2][-1]["role"] == "user" and any(r.get("images") for m in seen[2] if m["role"] == "tool"
                                                  for r in m["results"])


def test_a_model_that_cannot_see_gets_the_caption(tmp_path):
    provider = Scripted(look_turns())
    seen = []
    stream = provider.stream

    async def keep(model, system, history, tools, emit):
        seen.append(history)
        return await stream(model, system, history, tools, emit)
    provider.stream = keep
    run_day(tmp_path, provider)
    sent = next(r for m in seen[1] if m["role"] == "tool" for r in m["results"])
    assert "images" not in sent and "this model reads only the caption" in sent["content"]
    assert "Plot: lockin.X" in sent["content"]


def pdf_bytes(tmp_path, words):
    from labharness.reports.pdf import Canvas, write_pdf_pages
    c = Canvas()
    for k in range(12):
        c.text(50, 60 + 20 * k, "%s line %d of a datasheet about the lock-in's time constant" % (words, k), 10)
    path = tmp_path / "sheet.pdf"
    write_pdf_pages([c], path, "Datasheet")
    return path.read_bytes()


def test_attached_photos_and_pdfs(tmp_path):
    pytest.importorskip("pypdf")
    import numpy as np
    photo = png(np.full((6, 8, 3), 200, np.uint8))
    sheet = pdf_bytes(tmp_path, "Settling")

    def attach(agent, chat_id):
        out = [agent.attach(chat_id, "wiring.png", photo), agent.attach(chat_id, "sheet.pdf", sheet)]
        with pytest.raises(ValueError):
            agent.attach(chat_id, "notes.txt", b"just text")
        return agent.check_attachments(chat_id, out)
    provider = Seeing([[{"type": "text", "text": "The wiring looks right."}]])
    seen = []
    stream = provider.stream

    async def keep(model, system, history, tools, emit):
        seen.append(history)
        return await stream(model, system, history, tools, emit)
    provider.stream = keep
    store, chat, events, agent = run_day(tmp_path, provider, ("is this wired right?",), attach)
    message = chat["messages"][0]
    assert [a["name"] for a in message["attachments"]] == ["wiring.png", "sheet.pdf"]
    assert message["attachments"][1]["pages"] == 1 and "outside" not in chat   # marked for the model, no pause
    sent = seen[0][0]
    assert len(sent["images"]) == 1 and base64.b64decode(sent["images"][0]["data"]) == photo
    assert sent["attached_text"].startswith('<outside_text from="sheet.pdf, a PDF the person attached')
    assert "Settling line 3 of a datasheet" in sent["attached_text"]
    from labharness.web.providers.base import user_text
    assert user_text(sent).startswith("is this wired right?\n\n<outside_text")
    with pytest.raises(KeyError):                                       # only this chat's files
        agent.check_attachments(chat["id"], [{"file": "0" * 24 + ".png", "name": "x"}])


def test_the_portal_keeps_and_serves_them(parts):
    import numpy as np
    from test_web import Scripted as S
    provider = S([[{"type": "text", "text": "Seen."}]])
    client, headers = client_for(parts, {"scripted": provider})
    chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
    photo = png(np.zeros((4, 4), np.uint8))
    r = client.post("/api/chats/%s/attachments" % chat["id"], content=photo,
                    headers=dict(headers, **{"X-File-Name": "scope%20screen.png"}))
    assert r.status_code == 200 and r.json()["name"] == "scope screen.png" and r.json()["mime"] == "image/png"
    got = client.get("/api/chats/%s/media/%s" % (chat["id"], r.json()["file"]), headers=headers)
    assert got.status_code == 200 and got.content == photo and got.headers["content-type"] == "image/png"
    assert client.get("/api/chats/%s/media/../settings.json" % chat["id"], headers=headers).status_code == 404
    bad = client.post("/api/chats/%s/attachments" % chat["id"], content=b"MZ\x90\x00", headers=headers)
    assert bad.status_code == 400 and "pictures" in bad.json()["error"]
    with client.stream("POST", "/api/chats/%s/turn" % chat["id"], headers=headers,
                       json={"text": "look", "model": "scripted:m1", "attachments": [r.json()]}) as res:
        assert res.status_code == 200 and "Seen." in res.read().decode()
    saved = client.get("/api/chats/%s" % chat["id"], headers=headers).json()
    assert saved["messages"][0]["attachments"][0]["file"] == r.json()["file"]
    gone = client.post("/api/chats/%s/turn" % chat["id"], headers=headers,
                       json={"text": "x", "model": "scripted:m1", "attachments": [{"file": "f" * 24 + ".png"}]})
    assert gone.status_code == 400 and "attach it again" in gone.json()["error"]
    client.delete("/api/chats/%s" % chat["id"], headers=headers)
    assert not (parts[2].root / "media" / chat["id"]).exists()


def test_each_provider_puts_pictures_where_it_takes_them():
    from labharness.web.providers import anthropic_provider, chatgpt_provider, openai_provider
    pic = {"mime": "image/png", "data": "iVBORw0KGgo="}
    history = [{"role": "user", "text": "this?", "images": [pic]},
               {"role": "assistant", "provider": "x", "model": "m", "raw": None,
                "blocks": [{"type": "tool_call", "id": "c1", "name": "look_at", "input": {}}]},
               {"role": "tool", "results": [{"id": "c1", "name": "look_at", "content": "Plot: x", "is_error": False,
                                             "images": [pic]}]}]
    a = anthropic_provider.to_messages(history)
    assert a[0]["content"][0]["type"] == "image" and a[0]["content"][1] == {"type": "text", "text": "this?"}
    assert a[2]["content"][0]["content"][1]["source"]["data"] == pic["data"]
    o = openai_provider.to_messages("S", history)
    assert o[1]["content"][1]["image_url"]["url"] == "data:image/png;base64," + pic["data"]
    assert o[3]["role"] == "tool" and o[4]["role"] == "user" and o[4]["content"][1]["type"] == "image_url"
    c = chatgpt_provider.to_input(history, "chatgpt")
    assert c[0]["content"][1]["type"] == "input_image" and c[-1]["content"][1]["type"] == "input_image"
    pytest.importorskip("google.genai")
    from labharness.web.providers import gemini_provider
    g = gemini_provider.to_contents(history)
    assert g[0].parts[0].inline_data.mime_type == "image/png" and g[2].parts[1].inline_data is not None
