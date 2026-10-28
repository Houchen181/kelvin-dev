"""Problems people reported on GitHub (Houchen181/kelvin issues #2, #4, #5): Kelvin should drive LightField as an
instrument, not through its window; read a manual PDF in a folder the person names; and know where its own files are."""
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from labharness import ScriptedApprover, build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402


def runtime(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    rt.home = tmp_path / "Kelvin"
    return rt


def manual_pdf(path, words="SOURce:FREQuency <f>  sets the AOM's drive frequency"):
    from labharness.reports.pdf import Canvas, write_pdf_pages
    pages = []
    for k in range(3):
        c = Canvas()
        for n in range(20):
            c.text(40, 40 + 24 * n, "%s (page %d, line %d)" % (words, k + 1, n), 9)
        pages.append(c)
    write_pdf_pages(pages, path, "AOM manual")
    return path


def test_lightfield_is_named_as_an_instrument_and_takes_lightfield_6s_frames_setting(tmp_path, monkeypatch):
    """#2: asked to take a spectrum with LightField, Kelvin went to the screen."""
    from labharness.lightfield import LightFieldCamera
    rt = runtime(tmp_path)
    assert "LightField is installed" not in rt.lab_status()
    monkeypatch.setattr("labharness.lightfield.root", lambda: str(tmp_path / "LightField"))
    monkeypatch.setattr("labharness.agent.runtime.sys.platform", "win32")
    status = rt.lab_status()
    assert "LightField is installed here" in status and "add_instrument with address lightfield://" in status
    assert "card princeton/lightfield" in status
    both = SimpleNamespace(ExperimentSettings=SimpleNamespace(AcquisitionFramesToStore="LF6",
                                                              FrameSettingsFramesToStore="LF5"))
    old = SimpleNamespace(ExperimentSettings=SimpleNamespace(FrameSettingsFramesToStore="LF5"))
    assert LightFieldCamera._frames_setting(SimpleNamespace(api=both, name="spec")) == "LF6"   # as its samples set it
    assert LightFieldCamera._frames_setting(SimpleNamespace(api=old, name="spec")) == "LF5"


def test_a_manual_pdf_in_a_folder_the_person_names(tmp_path):
    """#4: given the folder with the AOM's manual, Kelvin said it reads scripts and text, not PDFs."""
    pytest.importorskip("pypdf")
    rt = runtime(tmp_path)
    folder = tmp_path / "AOM manual"
    folder.mkdir()
    manual_pdf(folder / "AOM_manual.pdf")
    read = rt.read_examples(str(folder))
    assert "=== AOM_manual.pdf ===" in read and "[page 2]" in read and "SOURce:FREQuency" in read
    kept = rt.save_manual(str(folder), maker="Acme", model="AOM-200")
    assert kept.startswith("kept ") and "a PDF of 3 pages" in kept and str(tmp_path / "Kelvin" / "manuals") in kept
    manual_pdf(folder / "datasheet.pdf", "Rise time 10 ns")
    assert rt.save_manual(str(folder)).startswith("2 PDFs in %s; name the manual's file: AOM_manual.pdf, "
                                                   "datasheet.pdf" % folder)


def test_kelvin_knows_where_its_files_are(tmp_path):
    """#5: asked where the run catalog is, Kelvin said its tools show only run IDs."""
    import asyncio
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore
    rt = runtime(tmp_path)
    assert "data folder: %s (runs/<run id>/run.h5, catalog.sqlite" % rt.data_dir in rt.lab_status()
    rt.cards_dir = tmp_path / "Kelvin" / "cards"
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {})

    async def system():
        await agent.start()
        try:
            return agent.context(store.create("x:y"))["system"]
        finally:
            await agent.close()
    text = asyncio.run(system())
    assert "# Kelvin's files on this computer" in text and "The data folder, %s: runs/<run id>/" % rt.data_dir in text
    assert "catalog.sqlite (the catalog of every run" in text and "instruments/<name>/" in text
    assert "The lab's own instrument cards: %s." % rt.cards_dir in text and "import_data" in text
