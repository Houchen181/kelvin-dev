"""Files in chats beyond pictures: scripts, notebooks and notes a person attaches go to every model as text; and for a
model that sees pictures but can't read PDFs, the Kelvin hub draws a PDF's pages as pictures (read_file's pages, and
the pages with figures of a PDF the person attaches), never from incognito chats or unshared work."""
import asyncio
import base64
import io
import json

import pytest

pytest.importorskip("mcp")
pytest.importorskip("numpy")

from simlab import build_virtual_lab  # noqa: E402
from labharness import folderfiles  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.pictures import png  # noqa: E402
from labharness.web import agent as agent_mod  # noqa: E402
from labharness.web.agent import ChatAgent, notebook_text  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_folder_files import make_pdf  # noqa: E402
from test_web import Scripted  # noqa: E402


class SeesPictures(Scripted):   # a model on OpenCode, OpenRouter...: pictures yes, PDFs no
    sees_images = True


class ReadsPdfs(Scripted):
    sees_images = True
    sees_pdfs = True


def answer(text="Done."):
    return [{"type": "text", "text": text}]


def read_turns(**args):
    return [[{"type": "tool_call", "id": "f1", "name": "read_file", "input": dict(path="manual.pdf", **args)}],
            answer("Figure 3 shows the settling.")]


class Hub:
    """agent.draw, as the Kelvin hub answers: one picture per page it is sent."""

    def __init__(self, fails=None):
        self.fails = fails
        self.sent = []

    def __call__(self, pdf):
        from pypdf import PdfReader
        if self.fails:
            raise RuntimeError(self.fails)
        n = len(PdfReader(io.BytesIO(pdf)).pages)
        self.sent.append(n)
        import numpy as np
        return [("image/png", png(np.full((4, 3, 3), 10 * k, np.uint8))) for k in range(n)]


def day(tmp_path, provider, texts=("what does it say?",), folder=None, attachments=None, draw=None, project=None,
        incognito=False):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    agent.draw = draw
    pid = None
    if project:
        pid = store.create_project("Sample A")["id"]
        store.set_sharing(pid, project)
    chat = store.create("scripted:m1", project=pid, incognito=incognito)
    if folder:
        chat["folder"] = str(folder)
        store.save(chat)
    seen, events = [], []
    stream = provider.stream

    async def keep(model, system, history, tools, emit):
        seen.append(history)
        return await stream(model, system, history, tools, emit)
    provider.stream = keep

    async def emit(e):
        events.append(e)

    async def run():
        await agent.start()
        try:
            for i, text in enumerate(texts):
                agent.claim(chat["id"], "scripted:m1")
                await agent.run_turn(chat["id"], text, "scripted:m1", provider, emit,
                                     attachments=attachments(agent, chat["id"]) if attachments and i == 0 else None)
        finally:
            await agent.close()
    asyncio.run(run())
    return store.load(chat["id"]) if not incognito else None, seen, events


# ------------------------------------------------------------------ text files

NOTEBOOK = json.dumps({"cells": [
    {"cell_type": "markdown", "source": ["# Gate sweep\n", "Sweep the gate, read the lock-in."]},
    {"cell_type": "code", "source": "v = sweep(-1, 1, 41)\nplot(v)", "outputs": [
        {"output_type": "display_data", "data": {"image/png": "iVBORw0KGgoAAAA", "text/plain": "<Figure 640x480>"}}]}],
    "metadata": {}, "nbformat": 4, "nbformat_minor": 5})


def test_scripts_notebooks_and_notes_go_to_every_model_as_text(tmp_path):
    def attach(agent, chat_id):
        out = [agent.attach(chat_id, "sweep.py", b"import pyvisa\nrm = pyvisa.ResourceManager()\n"),
               agent.attach(chat_id, "analysis.ipynb", NOTEBOOK.encode("utf-8")),
               agent.attach(chat_id, "notes.txt", "cooled to 4 K at 10:30 h\n".encode("utf-8"))]
        assert [a["mime"] for a in out] == ["text/plain"] * 3 and all(a["text"] == a["file"] for a in out)
        for name, data in (("firmware.bin", b"\x00\x01\x02"), ("empty.py", b"  \n"), ("broken.ipynb", b"{nope"),
                           ("utf16.py", b"\xff\xfeh\x00i\x00"), ("sweep.py", b"x\x00y")):
            if name == "utf16.py":
                assert agent.attach(chat_id, name, data)["mime"] == "text/plain"   # a UTF-16 file is text
                continue
            with pytest.raises(ValueError):
                agent.attach(chat_id, name, data)
        return agent.check_attachments(chat_id, out)
    provider = Scripted([answer("It opens a VISA session.")])            # sees no pictures: text still goes
    chat, seen, _ = day(tmp_path, provider, attachments=attach)
    message = chat["messages"][0]
    assert [a["name"] for a in message["attachments"]] == ["sweep.py", "analysis.ipynb", "notes.txt"]
    said = seen[0][0]["attached_text"]
    assert '<outside_text from="sweep.py, a file the person attached">\nimport pyvisa' in said
    assert "# %% [markdown]\n# Gate sweep" in said and "# %%\nv = sweep(-1, 1, 41)" in said
    assert "iVBOR" not in said and "Figure 640x480" not in said            # a notebook's saved outputs stay out
    assert "cooled to 4 K" in said


def test_a_long_or_large_text_file(tmp_path, monkeypatch):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {})
    chat = store.create("scripted:m1")
    monkeypatch.setattr(agent_mod, "MAX_TEXT_BYTES", 100)
    with pytest.raises(ValueError, match="up to 0 MB"):
        agent.attach(chat["id"], "big.log", b"x" * 101)
    monkeypatch.setattr(agent_mod, "MAX_ATTACHED_CHARS", 10)
    a = agent.attach(chat["id"], "short.md", b"0123456789abcdef")
    message = {"role": "user", "text": "read it", "attachments": agent.check_attachments(chat["id"], [a])}
    [sent] = agent._pictures(chat, [message], Scripted([]), "m1")
    assert "0123456789\n… (the rest is not shown: 16 characters in all)" in sent["attached_text"]


def test_a_notebook_is_its_cells():
    assert notebook_text(NOTEBOOK).startswith("# %% [markdown]\n# Gate sweep\nSweep the gate")
    with pytest.raises(ValueError, match="not a Jupyter notebook"):
        notebook_text('{"cells": "no"}')


# ------------------------------------------------------------------ PDF pages the hub draws

def test_read_file_pages_are_drawn_by_the_hub_for_a_model_that_sees_pictures(tmp_path):
    pytest.importorskip("pypdf")
    folder = tmp_path / "Sample A"
    folder.mkdir()
    make_pdf(folder / "manual.pdf", 8)
    hub = Hub()
    provider = SeesPictures(read_turns(figures=True))
    chat, seen, _ = day(tmp_path / "one", provider, folder=folder, draw=hub)
    assert hub.sent == [agent_mod.MAX_DRAWN]                                 # at most 6 pages at once
    [result] = next(m for m in chat["messages"] if m["role"] == "tool")["results"]
    assert "pages 1-6 of 8, shown as pictures; the rest from pages='7-'" in result["content"]
    assert len(result["images"]) == 6 and "documents" not in result          # kept with the chat, shown to the person
    sent = next(r for m in seen[1] if m["role"] == "tool" for r in m["results"])
    assert [base64.b64decode(p["data"])[:4] for p in sent["images"]] == [b"\x89PNG"] * 6   # and to the model


def test_the_pages_text_when_the_hub_cant_draw_or_the_work_stays_here(tmp_path):
    pytest.importorskip("pypdf")
    folder = tmp_path / "Sample A"
    folder.mkdir()
    make_pdf(folder / "manual.pdf", 3)
    down = Hub(fails="Kelvin isn't signed in to the Kelvin hub")
    chat, _, _ = day(tmp_path / "down", SeesPictures(read_turns(pages="2", figures=True)), folder=folder, draw=down)
    [result] = next(m for m in chat["messages"] if m["role"] == "tool")["results"]
    assert "couldn't be drawn as pictures (Kelvin isn't signed in to the Kelvin hub)" in result["content"]
    assert "Page 2 line" in result["content"] and "images" not in result
    for sharing in ("confidential", "unpublished"):
        hub = Hub()
        chat, _, _ = day(tmp_path / sharing, SeesPictures(read_turns(figures=True)), folder=folder, draw=hub,
                         project=sharing)
        [result] = next(m for m in chat["messages"] if m["role"] == "tool")["results"]
        assert hub.sent == [] and "can't see a PDF's pages" in result["content"]   # never to the hub
    hub = Hub()
    chat, _, _ = day(tmp_path / "text", Scripted(read_turns(figures=True)), folder=folder, draw=hub)
    assert hub.sent == []                                                     # a model that sees no pictures
    chat, _, _ = day(tmp_path / "pdfs", ReadsPdfs(read_turns(figures=True)), folder=folder, draw=hub)
    assert hub.sent == []                                                     # nor one that reads PDFs itself


def with_figure(path, on=(1,), side=200, form=None):
    """A text PDF (make_pdf) with a picture of side x side pixels placed on the given pages (0-based), or a form of
    form = (width, height) points."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, StreamObject
    writer = PdfWriter(clone_from=PdfReader(str(path)))
    for i in on:
        x = StreamObject()
        if form:
            x.set_data(b"0 0 m 10 10 l S")
            x.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                      NameObject("/BBox"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(form[0]),
                                                        FloatObject(form[1])])})
        else:
            x.set_data(b"\x80" * side * side)
            x.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                      NameObject("/Width"): NumberObject(side), NameObject("/Height"): NumberObject(side),
                      NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                      NameObject("/BitsPerComponent"): NumberObject(8)})
        page = writer.pages[i]
        resources = page["/Resources"].get_object() if "/Resources" in page else DictionaryObject()
        resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Fig1"): writer._add_object(x)})
        page[NameObject("/Resources")] = resources
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_figure_pages_are_the_pages_with_pictures_or_big_drawings(tmp_path):
    pytest.importorskip("pypdf")
    from pypdf import PdfReader
    plain = make_pdf(tmp_path / "plain.pdf", 3)
    assert folderfiles.figure_pages(plain.read_bytes(), 6) == ([], b"")
    pages, pdf = folderfiles.figure_pages(with_figure(plain, on=(1, 2)), 6)
    assert pages == [1, 2] and len(PdfReader(io.BytesIO(pdf)).pages) == 2
    assert folderfiles.figure_pages(with_figure(plain, on=(0, 1, 2)), 2)[0] == [0, 1]   # the first `most`
    assert folderfiles.figure_pages(with_figure(plain, side=32), 6)[0] == []           # a logo, not a figure
    assert folderfiles.figure_pages(with_figure(plain, form=(400, 300)), 6)[0] == [1]  # a placed drawing
    assert folderfiles.figure_pages(with_figure(plain, form=(60, 40)), 6)[0] == []     # a small one


def test_an_attached_pdfs_figures_are_drawn_once_and_shown_with_its_text(tmp_path):
    pytest.importorskip("pypdf")
    paper = with_figure(make_pdf(tmp_path / "paper.pdf", 3), on=(1,))
    hub = Hub()

    def attach(agent, chat_id):
        return agent.check_attachments(chat_id, [agent.attach(chat_id, "paper.pdf", paper)])
    provider = SeesPictures([answer("Figure 1 shows a peak."), answer("Still a peak.")])
    chat, seen, _ = day(tmp_path / "a", provider, ("what is in figure 1?", "and the units?"), attachments=attach,
                        draw=hub)
    assert hub.sent == [1]                                                    # drawn once, kept for the next turns
    [a] = chat["messages"][0]["attachments"]
    assert [(d["page"], d["mime"]) for d in a["drawn"]] == [(2, "image/png")]
    for history in seen:
        first = history[0]
        assert len(first["images"]) == 1 and "page 2 also as pictures" in first["attached_text"]
        assert "Page 2 line 3" in first["attached_text"]                     # with the PDF's text

    for provider, name in ((Scripted([answer()]), "text"), (ReadsPdfs([answer()]), "pdfs")):
        hub = Hub()
        chat, seen, _ = day(tmp_path / name, provider, attachments=attach, draw=hub)
        assert hub.sent == [] and "drawn" not in chat["messages"][0]["attachments"][0]
    hub = Hub(fails="This Kelvin hub can't draw PDF pages yet.")
    chat, seen, events = day(tmp_path / "old", SeesPictures([answer()]), attachments=attach, draw=hub)
    assert any(e["type"] == "note" and "paper.pdf: its figures couldn't be drawn as pictures (This Kelvin hub "
               "can't draw PDF pages yet.)" in e["text"] for e in events)
    assert "figures are not included" in seen[0][0]["attached_text"] and "images" not in seen[0][0]


def test_nothing_from_an_incognito_chat_goes_to_the_hub(tmp_path):
    pytest.importorskip("pypdf")
    paper = with_figure(make_pdf(tmp_path / "paper.pdf", 3), on=(1,))
    hub = Hub()

    def attach(agent, chat_id):
        return agent.check_attachments(chat_id, [agent.attach(chat_id, "paper.pdf", paper)])
    day(tmp_path / "i", SeesPictures([answer()]), attachments=attach, draw=hub, incognito=True)
    assert hub.sent == []
