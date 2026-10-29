"""Kelvin reads the files in a chat's folder in a chat: list_folder and read_file, only for a chat with a folder of its
own; PDF pages whole to a model that reads PDFs (Claude, GPT, Gemini), their text to the rest; what is read pauses auto
mode until the person writes."""
import asyncio
import base64

import pytest

from test_folder_files import make_pdf

pytest.importorskip("mcp")
from simlab import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted  # noqa: E402


class ReadsPdfs(Scripted):
    sees_images = True
    sees_pdfs = True


def chat_day(tmp_path, provider, folder, texts=("what does the manual say?",), attachments=None):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")
    if folder:
        chat["folder"] = str(folder)
        store.save(chat)
    seen = []
    stream = provider.stream

    async def keep(model, system, history, tools, emit):
        seen.append(history)
        return await stream(model, system, history, tools, emit)
    provider.stream = keep

    async def emit(e):
        pass

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
    return store, store.load(chat["id"]), seen


def read_turns(**args):
    return [[{"type": "tool_call", "id": "f1", "name": "read_file", "input": dict(path="manual.pdf", **args)}],
            [{"type": "text", "text": "Figure 3 shows the settling."}]]


def test_a_model_that_reads_pdfs_gets_the_pages_whole_and_auto_mode_waits(tmp_path):
    pytest.importorskip("pypdf")
    folder = tmp_path / "Sample A"
    folder.mkdir()
    make_pdf(folder / "manual.pdf", 6)
    provider = ReadsPdfs(read_turns(pages="2-3", figures=True))
    store, chat, seen = chat_day(tmp_path, provider, folder)
    assert {"list_folder", "read_file"} <= set(provider.tool_names[0])          # a chat with a folder has them
    assert "list_folder and read_file read the files in this folder" in provider.systems[0]
    [result] = next(m for m in chat["messages"] if m["role"] == "tool")["results"]
    assert result["content"].startswith('<outside_text from="manual.pdf, a file in this chat\'s folder">')
    [kept] = result["documents"]
    assert kept["mime"] == "application/pdf" and "data" not in kept               # the chat keeps the file's name
    sent = next(r for m in seen[1] if m["role"] == "tool" for r in m["results"])
    assert base64.b64decode(sent["documents"][0]["data"])[:5] == b"%PDF-"       # the pages, for the model
    assert chat["outside"] == "a file in the chat's folder"                     # auto mode asks until the person writes


def test_another_model_gets_the_text_and_a_chat_without_a_folder_reads_nothing(tmp_path):
    pytest.importorskip("pypdf")
    folder = tmp_path / "Sample B"
    folder.mkdir()
    make_pdf(folder / "manual.pdf", 3)
    provider = Scripted(read_turns(figures=True))
    store, chat, seen = chat_day(tmp_path, provider, folder)
    [result] = next(m for m in chat["messages"] if m["role"] == "tool")["results"]
    assert "Page 2 line" in result["content"] and "can't see a PDF's pages" in result["content"]
    assert "documents" not in result
    nowhere = Scripted(read_turns())
    store, chat, seen = chat_day(tmp_path / "x", nowhere, None)
    assert "read_file" not in nowhere.tool_names[0]                               # nothing to read: no tools
    [result] = next(m for m in chat["messages"] if m["role"] == "tool")["results"]
    assert result["is_error"] and "no folder of its own" in result["content"]


def test_an_attached_pdf_goes_whole_to_a_model_that_reads_pdfs(tmp_path):
    pytest.importorskip("pypdf")
    sheet = make_pdf(tmp_path / "sheet.pdf", 2).read_bytes()

    def attach(agent, chat_id):
        return agent.check_attachments(chat_id, [agent.attach(chat_id, "sheet.pdf", sheet)])
    provider = ReadsPdfs([[{"type": "text", "text": "Its figure shows the settling."}]])
    store, chat, seen = chat_day(tmp_path, provider, None, ("what does it show?",), attach)
    sent = seen[0][0]
    assert base64.b64decode(sent["documents"][0]["data"]) == sheet and sent["documents"][0]["name"] == "sheet.pdf"
    assert "given whole above" in sent["attached_text"] and "Page 1 line" not in sent["attached_text"]


def test_each_provider_puts_pdfs_where_it_takes_them():
    from labharness.web.providers import anthropic_provider, openai_provider
    doc = {"mime": "application/pdf", "data": "JVBERi0x", "name": "manual (pages).pdf"}
    history = [{"role": "user", "text": "this?", "documents": [doc]},
               {"role": "assistant", "provider": "x", "model": "m", "raw": None,
                "blocks": [{"type": "tool_call", "id": "c1", "name": "read_file", "input": {}}]},
               {"role": "tool", "results": [{"id": "c1", "name": "read_file", "content": "pages 2-3", "is_error": False,
                                             "documents": [doc]}]}]
    a = anthropic_provider.to_messages(history)
    assert a[0]["content"][0] == {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                                  "data": doc["data"]}}
    assert a[2]["content"][0]["type"] == "tool_result" and a[2]["content"][-1]["type"] == "document"
    o = openai_provider.to_messages("S", history)
    assert o[1]["content"][1] == {"type": "file", "file": {"filename": "manual (pages).pdf",
                                                            "file_data": "data:application/pdf;base64,JVBERi0x"}}
    assert o[3]["role"] == "tool" and o[4]["role"] == "user" and o[4]["content"][-1]["type"] == "file"
    pytest.importorskip("google.genai")
    from labharness.web.providers import gemini_provider
    g = gemini_provider.to_contents(history)
    assert g[0].parts[0].inline_data.mime_type == "application/pdf"
    assert g[2].parts[-1].inline_data.mime_type == "application/pdf"
