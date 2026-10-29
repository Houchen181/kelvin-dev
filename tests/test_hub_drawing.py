"""What the Kelvin hub does for signed-in Kelvins besides accounts (hub/drawing.py): which models see pictures (from
models.dev, once a day), and PDF pages drawn as pictures in a process of its own (hub/pdfdraw.py), keeping nothing."""
import base64
import json
import struct
import subprocess
import time
import zlib
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from labharness import central  # noqa: E402
from labharness.hub import drawing, pdfdraw  # noqa: E402
from test_hub_web import make, through  # noqa: E402

CATALOG = {
    "opencode": {"models": {"deepseek-v4.1-flash": {"modalities": {"input": ["text", "image"]}},
                            "glm-5.3": {"modalities": {"input": ["text"]}},
                            "mystery": {}}},
    "deepseek": {"models": {"deepseek-flash": {"modalities": {"input": ["text", "image"]}}}},
    "someone-else": {"models": {"x": {"modalities": {"input": ["text", "image"]}}}},
}
KELVIN = {"id": "urn:uuid:one", "name": "Lab PC", "platform": "win32", "version": "0.4.0"}


def signed_in(tmp_path):
    hub, client = make(tmp_path, sharing=False)
    r = client.post("/api/register", json={"name": "Ada", "email": "ada@example.org", "password": "correct horse",
                                           "consent": True, "installation": KELVIN})
    return hub, client, r.json()["access_token"]


def a_png():
    return pdfdraw.png(1, 1, 3, [b"\x00\x80\xff"])


def test_png_rows_are_written_as_they_are():
    data = pdfdraw.png(2, 2, 3, [b"\x01\x02\x03\x04\x05\x06", b"\x07\x08\x09\x0a\x0b\x0c"])
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    assert struct.unpack(">IIBB", data[16:26]) == (2, 2, 8, 2)                 # 2 x 2, 8 bits, RGB
    idat = data.index(b"IDAT")
    size = struct.unpack(">I", data[idat - 4:idat])[0]
    rows = b"\x00\x01\x02\x03\x04\x05\x06" + b"\x00\x07\x08\x09\x0a\x0b\x0c"   # each row: filter 0, then the row
    assert zlib.decompress(data[idat + 4:idat + 4 + size]) == rows
    assert data.endswith(b"IEND\xaeB`\x82")


def test_which_models_see_pictures_from_models_dev(tmp_path):
    assert drawing.pictures_in(CATALOG) == {"opencode": {"yes": ["deepseek-v4.1-flash"], "no": ["glm-5.3"]},
                                            "deepseek": {"yes": ["deepseek-flash"], "no": []}}
    reads = []

    def read():
        reads.append(1)
        return CATALOG
    kept = drawing.PictureList(tmp_path, read)
    assert kept.current()["services"]["opencode"]["yes"] == ["deepseek-v4.1-flash"]
    assert kept.current()["at"] and len(reads) == 1                            # once a day
    again = drawing.PictureList(tmp_path, read)
    assert again.current()["services"]["deepseek"]["yes"] == ["deepseek-flash"] and len(reads) == 1   # kept on disk

    def down():
        raise OSError("models.dev is down")
    stale = drawing.PictureList(tmp_path, down)
    stale._kept["at"] = time.time() - drawing.LIST_EVERY - 1
    assert stale.current()["services"]["opencode"]["no"] == ["glm-5.3"]        # what it had, tried again later
    assert stale._kept["tried"] > stale._kept["at"]


def test_the_api_needs_a_signed_in_kelvin(tmp_path, monkeypatch):
    monkeypatch.setattr(drawing, "_read_models_dev", lambda: CATALOG)
    hub, client, token = signed_in(tmp_path)
    auth = {"Authorization": "Bearer " + token}
    assert client.get("/api/models/pictures").status_code == 401
    assert client.post("/api/pdf/pages", content=b"%PDF-1.4").status_code == 401
    r = client.get("/api/models/pictures", headers=auth)
    assert r.status_code == 200 and r.json()["services"]["opencode"]["no"] == ["glm-5.3"]
    assert (tmp_path / "hub" / drawing.LIST_FILE).exists()
    assert "draws_pdfs" in client.get("/api/health").json()


def test_pages_are_drawn_within_limits_and_never_kept(tmp_path, monkeypatch):
    hub, client, token = signed_in(tmp_path)
    auth = {"Authorization": "Bearer " + token, "Content-Type": "application/pdf"}
    monkeypatch.setattr(drawing, "can_draw", lambda: False)
    r = client.post("/api/pdf/pages", headers=auth, content=b"%PDF-1.4 pages")
    assert r.status_code == 501 and "pypdfium2" in r.json()["error"]           # a hub without the drawing library
    monkeypatch.setattr(drawing, "can_draw", lambda: True)
    sent = []

    def draw(pdf):
        sent.append(pdf)
        if not pdf.startswith(b"%PDF-"):
            raise drawing.DrawError("that is not a PDF of at most 10 MB")
        return [{"mime": "image/png", "data": base64.b64encode(a_png()).decode()}]
    monkeypatch.setattr(drawing, "draw", draw)
    r = client.post("/api/pdf/pages", headers=auth, content=b"%PDF-1.4 pages")
    assert r.status_code == 200 and base64.b64decode(r.json()["pages"][0]["data"]) == a_png()
    assert client.post("/api/pdf/pages", headers=auth, content=b"<html>").status_code == 422
    monkeypatch.setattr(drawing, "MAX_PDF", 16)
    assert client.post("/api/pdf/pages", headers=auth, content=b"%PDF-1.4 " + b"x" * 16).status_code == 413
    stored = [p for p in (tmp_path / "hub").rglob("*") if p.is_file() and p.suffix not in (".sqlite3", ".json")
              and "-wal" not in p.name and "-shm" not in p.name and p.name != "hub.log"]
    assert not [p for p in stored if p.read_bytes().startswith(b"%PDF")]     # nothing of the pages is kept
    hub.config["consent_version"] = "2099-01-01"
    assert client.post("/api/pdf/pages", headers=auth, content=b"%PDF-1.4").status_code == 403   # new terms first


def test_kelvin_asks_the_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(drawing, "_read_models_dev", lambda: CATALOG)
    monkeypatch.setattr(drawing, "can_draw", lambda: True)
    page = {"mime": "image/png", "data": base64.b64encode(a_png()).decode()}
    monkeypatch.setattr(drawing, "draw", lambda pdf: [page])
    hub, client, token = signed_in(tmp_path)
    kelvin = central.Central("https://hub.test", http=through(client))
    assert kelvin.pictures(token)["services"]["deepseek"]["yes"] == ["deepseek-flash"]
    assert kelvin.draw_pages(token, b"%PDF-1.4 pages") == [("image/png", a_png())]
    monkeypatch.setattr(drawing, "draw", lambda pdf: [dict(page, data=base64.b64encode(b"<script>").decode())])
    with pytest.raises(central.CentralError, match="weren't pictures"):
        kelvin.draw_pages(token, b"%PDF-1.4 pages")
    monkeypatch.setattr(drawing, "can_draw", lambda: False)
    with pytest.raises(central.CentralError, match="pypdfium2"):              # said as it is, not "unreachable"
        kelvin.draw_pages(token, b"%PDF-1.4 pages")
    older = central.Central("https://hub.test", http=lambda *a: (404, {"error": "not found"}))
    with pytest.raises(central.CentralError, match="can't draw PDF pages yet"):
        older.draw_pages(token, b"%PDF-1.4")
    with pytest.raises(central.CentralError, match="no list of models"):
        older.pictures(token)


def test_drawing_runs_in_its_own_process_with_a_time_limit(monkeypatch):
    with pytest.raises(drawing.DrawError, match="not a PDF"):
        drawing.draw(b"<html>")
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(returncode=2, stdout=b"", stderr=b"the PDF couldn't be drawn: Failed to load document")
    monkeypatch.setattr(drawing.subprocess, "run", run)
    with pytest.raises(drawing.DrawError, match="Failed to load document"):
        drawing.draw(b"%PDF-1.4 broken")
    [(args, kw)] = calls
    assert args[1:] == ["-m", "labharness.hub.pdfdraw"] and kw["timeout"] == drawing.DRAW_S and kw["input"]

    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])
    monkeypatch.setattr(drawing.subprocess, "run", slow)
    with pytest.raises(drawing.DrawError, match="longer than"):
        drawing.draw(b"%PDF-1.4 huge")
    monkeypatch.setattr(drawing.subprocess, "run", lambda args, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({"pages": [{"mime": "image/png", "data": "AA=="}]}).encode(), stderr=b""))
    assert drawing.draw(b"%PDF-1.4 fine") == [{"mime": "image/png", "data": "AA=="}]


def test_real_pages_are_drawn(tmp_path):
    """With pypdfium2 installed (the hub's own extra), a PDF Kelvin made is drawn, through the separate process."""
    pytest.importorskip("pypdfium2")
    pytest.importorskip("pypdf")
    from test_folder_files import make_pdf
    pdf = make_pdf(tmp_path / "manual.pdf", 2).read_bytes()
    pages = drawing.draw(pdf)
    assert len(pages) == 2
    data = base64.b64decode(pages[0]["data"])
    width, height = struct.unpack(">II", data[16:24])
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and max(width, height) == pdfdraw.LONGEST
