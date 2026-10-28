"""Kelvin reads the files in a chat's folder, and only there: PDFs by pages (their text, or whole for a model that reads
PDFs: figures as drawn), text and CSV files, pictures. What it reads is marked as text from outside the lab."""
import io

import pytest

from labharness import folderfiles
from labharness.folderfiles import FolderError


def make_pdf(path, pages, lines=30):
    from labharness.reports.pdf import Canvas, write_pdf_pages
    canvases = []
    for p in range(pages):
        c = Canvas()
        for k in range(lines):
            c.text(40, 40 + 22 * k, "Page %d line %d: the lock-in's time constant and the stage settling" % (p + 1, k), 9)
        canvases.append(c)
    write_pdf_pages(canvases, path, "Manual")
    return path


def test_text_csv_and_pictures_are_read_only_inside_the_chats_folder(tmp_path, monkeypatch):
    folder = tmp_path / "Sample A"
    (folder / "papers").mkdir(parents=True)
    (folder / "20261025-101500-gate_sweep").mkdir()                      # a Kelvin run, as runs land here
    (folder / "20261025-101500-gate_sweep" / "run.h5").write_bytes(b"\x89HDF")
    (folder / "iv.csv").write_text("V,I\n" + "".join("%d,%d\n" % (i, i * i) for i in range(3000)), encoding="utf-8")
    (folder / ".hidden").write_text("x", encoding="utf-8")
    (folder / "scope.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    (folder / "raw.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "secret.txt").write_text("not for Kelvin", encoding="utf-8")
    listed = folderfiles.listing(folder)
    assert listed.splitlines()[1:3] == ["20261025-101500-gate_sweep/  (a Kelvin run: describe_run and get_run_data "
                                        "read it)", "papers/"]
    assert "iv.csv" in listed and ".hidden" not in listed
    first, _ = folderfiles.read(folder, "iv.csv")
    assert first.startswith("iv.csv (") and "characters 0-12000; more from start=12000" in first and "V,I" in first
    second, _ = folderfiles.read(folder, "iv.csv", start=12000)
    assert "characters 12000-" in second and "V,I" not in second
    text, media = folderfiles.read(folder, "scope.png")
    assert text.startswith("scope.png, a picture") and media[0][0] == "image/png"
    monkeypatch.setattr(folderfiles, "MAX_PICTURE_BYTES", 50)
    with pytest.raises(FolderError, match="more than a model takes"):
        folderfiles.read(folder, "scope.png")
    for outside in ("../secret.txt", str(tmp_path / "secret.txt"), "papers/../../secret.txt"):
        with pytest.raises(FolderError, match="outside this chat's folder"):
            folderfiles.read(folder, outside)
    with pytest.raises(FolderError, match="not text, a PDF or a picture"):
        folderfiles.read(folder, "raw.bin")
    with pytest.raises(FolderError, match="describe_run and get_run_data"):
        folderfiles.read(folder, "20261025-101500-gate_sweep/run.h5")
    with pytest.raises(FolderError, match="list_folder shows"):
        folderfiles.read(folder, "nothere.txt")


def test_a_pdf_is_read_by_pages_and_its_pages_shown_whole(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    folder = tmp_path / "papers"
    folder.mkdir()
    make_pdf(folder / "manual.pdf", 30)
    first, media = folderfiles.read(folder, "manual.pdf")
    head = first.splitlines()[0]
    assert head.startswith("manual.pdf, 30 pages; the text of pages 1-") and "more from pages='" in head and not media
    nxt = head.split("more from pages='")[1]
    pages, _, rest = nxt.partition("'")
    start = int(rest.split("start=")[1]) if "start=" in rest else 0
    second, _ = folderfiles.read(folder, "manual.pdf", pages=pages, start=start)   # reads on where it stopped
    page = int(pages.split("-")[0])
    assert second.startswith("manual.pdf, 30 pages; the text of pages %d-" % page)
    assert "[page %d]" % (page + 1) in second and "[page 1]" not in second
    assert (first.splitlines()[-1] + second.splitlines()[1]).count("Page %d line" % page) == 1   # seamless
    whole, media = folderfiles.read(folder, "manual.pdf", pages="3-5,9", figures=True)
    assert whole == "manual.pdf, pages 3-5, 9 of 30, shown whole" and media[0][0] == "application/pdf"
    reader = pypdf.PdfReader(io.BytesIO(media[0][1]))
    assert len(reader.pages) == 4 and "Page 9 line" in reader.pages[3].extract_text()
    many, media = folderfiles.read(folder, "manual.pdf", figures=True)        # 20 pages at most at once
    assert "pages 1-20 of 30, shown whole; the rest from pages='21-'" in many
    for bad in ("0", "31", "5-3", "two"):
        with pytest.raises(FolderError):
            folderfiles.read(folder, "manual.pdf", pages=bad)
