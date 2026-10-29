"""Kelvin setting up connected instruments by itself: each marked as added by Kelvin (every output change asks a
person until they review its limits), and each instrument's folder with its identity, connection, settings and manual
(fetched once, then searched)."""
import json
import time

import pytest

from labharness import builtin_registry, profiles
from labharness.adapters import AdapterError, set_resource_manager
from labharness.autosetup import AutoSetup, draft_found, reviewed
from labharness.cards import CardError, parse_card
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.labconfig import ConfigError, load_lab_config
from labharness.homelab import build_lab, empty_lab
from test_hardware import Bus, Device


def tiny_pdf(pages):
    """A PDF whose pages hold these lines (Helvetica), for pypdf to read back."""
    def esc(line):
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids, n = [], 4
    for lines in pages:
        text = b"BT /F1 9 Tf 11 TL 30 810 Td " + b" ".join(b"(" + esc(line) + b") Tj T*" for line in lines) + b" ET"
        objs[n] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
                   b"/Contents %d 0 R >>" % (n + 1))
        objs[n + 1] = b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream"
        kids.append(n)
        n += 2
    objs[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for i in sorted(objs):
        offsets[i] = len(out)
        out += b"%d 0 obj\n" % i + objs[i] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for i in sorted(objs):
        out += b"%010d 00000 n \n" % offsets[i]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


FILLER = ["This chapter describes the front panel and how the instrument is used in the laboratory."] * 24
MANUAL = [
    ["Chapter 1 Getting started", "Set the sensitivity so the signal fits; sensitivity and reserve interact."] + FILLER,
    ["5-6 Remote Programming", "OFLT (?) {i} The OFLT command sets or queries the time constant.",
     "i = 0 is 10 us, i = 10 is 1 s, i = 19 is 30 ks."] + FILLER,
    ["5-7 Gain commands", "SENS (?) {i} The SENS command sets or queries the full scale range.",
     "STRT starts data storage; TRIG is the software trigger for the buffer."] + FILLER,
]


@pytest.fixture
def pdf():
    pytest.importorskip("pypdf")
    return tiny_pdf(MANUAL)


@pytest.fixture
def bench():
    bus = Bus({
        "GPIB0::24::INSTR": Device("keithley/2400", name="k2400"),
        "GPIB0::8::INSTR": Device("srs/sr830", name="sr830"),
        "ASRL3::INSTR": Device("lakeshore/336", serial={"baud_rate": 57600, "data_bits": 7, "parity": "odd"}, name="ls"),
        "GPIB0::5::INSTR": Device(idn="ACME,WIDGET 9000,1,1.0"),
    })
    set_resource_manager(bus)
    yield bus
    set_resource_manager(None)


def scanned(bus):
    """What runtime.scan_found gives: discovery's findings with a suggested name each."""
    from labharness.discovery import scan
    out, taken = [], []
    for f in scan(builtin_registry(), bus):
        name = f.suggest_name(taken) if f.card_id else None
        taken += [name] if name else []
        out.append(dict(vars(f), suggested=name))
    return out


def drive_commands(bus):
    """Commands sent to the bus that would set a drive (a voltage, current, output...)."""
    hits = []
    for dev in bus.devices.values():
        for spec in (dev.card.drives if dev.card else []):
            if spec.set_cmd is not None:
                hits += [c for c in dev.sent if "?" not in c and spec.set_cmd.header_prefix().match(c)]
    return hits


# ------------------------------------------------------------------ identity and cards

def test_identity_replies_are_split_into_maker_model_serial_firmware():
    assert profiles.parse_idn("Stanford_Research_Systems,SR830,s/n12345,ver1.07") == {
        "maker": "Stanford_Research_Systems", "model": "SR830", "serial": "s/n12345", "firmware": "ver1.07"}
    assert profiles.parse_idn("IDN:OXFORD INSTRUMENTS:MERCURY ITC:1234:2.5.03")["model"] == "MERCURY ITC"
    assert profiles.parse_idn("Keysight Technologies,34465A,MY5,A.03.01-02:40")["firmware"] == "A.03.01-02:40"
    assert profiles.parse_idn("") == {}


def test_cards_say_where_the_makers_publish_their_manuals():
    makers = ("americanmagnetics.com", "coherent.com", "tek.com", "lakeshore.com", "qdusa.com", "thinksrs.com",
              "thorlabs.com", "yokogawa.com", "zhinst.com", "swabianinstruments.com", "rohde-schwarz.com", "keysight.com",
              "rigoltech.com", "svdcdn.com", "novatechsales.com", "windfreaktech.com",
              "mymercurysupport.com",   # Oxford Instruments' Mercury support site
              "mks.com", "buschgroup.com")   # Newport's and Pfeiffer Vacuum's parent companies, which publish theirs
    real = [c for c in builtin_registry() if not c.id.startswith("virtual/") and c.manual]
    with_url = [c for c in real if c.manual_url]
    assert len(with_url) >= 28
    assert builtin_registry().get("oxford/mercury-itc").manual_url == \
        "https://www.mymercurysupport.com/sites/default/files/59_UMC0071_EN_02.pdf"   # public now; 0.3.24 had none
    for c in with_url:   # the makers' own sites, nobody's mirror
        host = c.manual_url.split("/")[2]
        assert c.manual_url.startswith("https://") and host.endswith(makers), c.id
    assert builtin_registry().get("keysight/33500b").manual_url == ""   # behind a sign-in: the person chooses the PDF
    card = {"card": 1, "id": "x/y", "model": "Y", "kind": "multimeter", "manual_url": "http://example.com/m.pdf",
            "params": {"V": {"kind": "measure", "get": "MEAS?", "unit": "V"}}}
    with pytest.raises(CardError, match="manual_url"):
        parse_card(card, "test")


# ------------------------------------------------------------------ manuals

def test_a_manual_is_fetched_once_indexed_and_searched_by_page(tmp_path, pdf):
    card = builtin_registry().get("srs/sr830")
    assert card.manual_url == "https://www.thinksrs.com/downloads/pdfs/manuals/SR830m.pdf"
    asked = []

    def fetch(url):
        asked.append(url)
        return pdf
    m = profiles.fetch_manual(tmp_path, "lockin", card, fetch)
    assert m["pages"] == 3 and m["file"] == "manual.pdf" and m["url"] == card.manual_url
    assert profiles.fetch_manual(tmp_path, "lockin", card, fetch)["pages"] == 3 and asked == [card.manual_url]
    base = tmp_path / "instruments" / "lockin"
    assert (base / "manual.pdf").read_bytes() == pdf and (base / "manual.txt").exists()

    hits = profiles.search_manual(tmp_path, "lockin", "time constant OFLT")
    assert hits[0]["page"] == 2 and "OFLT (?) {i} The OFLT command" in hits[0]["text"][:80]
    assert profiles.search_manual(tmp_path, "lockin", "SENS")[0]["page"] == 3   # the mnemonic, not "sensitivity"
    assert profiles.search_manual(tmp_path, "lockin", "data storage buffer trigger")[0]["page"] == 3
    assert profiles.search_manual(tmp_path, "lockin", "nonexistentword") == []
    with pytest.raises(ValueError):
        profiles.search_manual(tmp_path, "lockin", "?")
    with pytest.raises(FileNotFoundError):
        profiles.search_manual(tmp_path, "smu", "anything")
    with pytest.raises(KeyError):
        profiles.folder(tmp_path, "../etc")


def test_a_sign_in_page_is_not_kept_as_the_manual(tmp_path, pdf):
    card = builtin_registry().get("srs/sr830")
    gate_page = b"<html><body><h1>Please sign in to download</h1></body></html>"
    with pytest.raises(ValueError, match="almost no text"):
        profiles.fetch_manual(tmp_path, "lockin", card, lambda url: gate_page)
    base = tmp_path / "instruments" / "lockin"
    assert not (base / "manual.html").exists() and "manual" not in json.loads((base / "instrument.json").read_text())
    with pytest.raises(ValueError, match="did not return"):
        profiles.fetch_manual(tmp_path, "lockin", card, lambda url: b"\x00binary")
    assert profiles.fetch_manual(tmp_path, "lockin", card, lambda url: pdf)["pages"] == 3   # the next try works

    gated = builtin_registry().get("keysight/34465a")
    with pytest.raises(LookupError, match="manual.pdf"):
        profiles.fetch_manual(tmp_path, "dmm", gated, lambda url: pdf)
    with pytest.raises(ValueError):
        profiles.save_manual(tmp_path, "dmm", gated, b"not a pdf")
    assert profiles.save_manual(tmp_path, "dmm", gated, pdf)["pages"] == 3   # the person's own copy
    assert profiles.info(tmp_path, "dmm")["manual"]["added_by"] == "a person"


# ------------------------------------------------------------------ added by Kelvin: asks until reviewed

def test_a_lab_kelvin_set_up_asks_before_every_output_change_until_reviewed(bench):
    reg = builtin_registry()
    text = draft_found(scanned(bench), reg)
    assert text.count("added_by: kelvin") == 3 and "widget" not in text.lower()   # only instruments Kelvin has cards for
    cfg = load_lab_config(text, reg)
    assert set(cfg.instruments) == {"smu", "lockin", "tc"} and sorted(cfg.unreviewed()) == ["lockin", "smu", "tc"]
    lab = build_lab(text, registry=reg)
    approver = ScriptedApprover([False])
    gate = SafetyGate(lab, approver)
    assert gate.get("smu", "I").ok and gate.set("lockin", "time_constant", "300 ms").ok      # reading, configuring
    declined = gate.set("smu", "V", 1.0)                                                # asks, saying why
    assert declined.action == "declined" and "Kelvin added it to the lab itself" in approver.requests[0].items[-1]
    items, grants, problems = gate.plan_approval({"smu.V": [0.0, 1.0]})
    assert not problems and "isn't sure about smu" in items[0] and "smu.V" in grants
    assert drive_commands(bench) == []

    opened = reviewed(text)
    assert "added_by" not in opened and not load_lab_config(opened, reg).unreviewed()
    lab2 = build_lab(opened, registry=reg)
    approver2 = ScriptedApprover([True])
    assert SafetyGate(lab2, approver2).set("smu", "V", 1.0).ok                      # asks, as usual
    assert len(approver2.requests[0].items) == 1                                    # without the not-sure line
    with pytest.raises(ConfigError, match="added_by"):
        load_lab_config(text.replace("added_by: kelvin", "added_by: alice", 1), reg)

    older = opened.replace("lab: My lab\n", "lab: My lab\nobserve_only: true   # older versions wrote this\n")
    assert sorted(load_lab_config(older, reg).unreviewed()) == ["lockin", "smu", "tc"]   # the whole lab, as before
    assert reviewed(older) == opened
    with pytest.raises(ConfigError, match="observe_only"):
        load_lab_config(older.replace("observe_only: true", "observe_only: maybe"), reg)


# ------------------------------------------------------------------ auto-setup

def runtime_for(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(empty_lab(), WebApprover(timeout=5), tmp_path / "data")   # no instruments set up yet
    rt.home = tmp_path
    return rt


class FakeSettings(dict):
    def update(self, values):
        super().update(values)


def test_first_start_sets_up_every_instrument_it_knows(tmp_path, bench, pdf):
    rt = runtime_for(tmp_path)
    settings = FakeSettings()
    fetched = []
    setup = AutoSetup(rt, settings, tmp_path, fetch=lambda url: fetched.append(url) or pdf)
    status = setup.run()
    assert status["state"] == "done", status
    assert rt.lab_info["kind"] == "file" and rt.lab_info["auto"]
    assert sorted(rt.lab.config.unreviewed()) == ["lockin", "smu", "tc"] and status["unreviewed"]
    assert "added_by: kelvin" in (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    assert rt.setup_status is setup.status and "3 instruments set up" in status["message"]
    assert any(n["kind"] == "setup" for n in rt.notifier.notices)

    assert profiles.known(tmp_path) == ["lockin", "smu", "tc"]
    smu = profiles.info(tmp_path, "smu")
    assert smu["card"] == "keithley/2400" and smu["parsed"]["model"] == "MODEL 2400" and smu["address"] == "GPIB0::24::INSTR"
    assert profiles.info(tmp_path, "tc")["answered_with"].startswith("57600 7O")   # the framing that answered
    lockin = profiles.settings_of(tmp_path, "lockin")["settings"]
    assert {"time_constant", "sensitivity", "X"} <= set(lockin) and "shown" in lockin["time_constant"]
    assert sorted(fetched) == sorted({rt.lab.instruments[n].card.manual_url for n in ("smu", "lockin", "tc")})
    assert drive_commands(bench) == []   # found, connected and read: nothing driven

    info = rt.instrument_info("lockin")
    assert "Stanford" in info and "manual:" in info and "3 pages" in info and "settings read at" in info
    assert "[lockin manual, p. 2]" in rt.search_manual("lockin", "OFLT time constant")
    assert "not reviewed by a person yet" in rt.lab_status() and "not sure yet" in rt.describe("smu")
    rt.gate.approver = ScriptedApprover([False])
    assert rt.set("smu", "V", 1.0).startswith("declined") and "isn't sure about smu" in str(rt.gate.approver.requests[0])
    assert "no instrument" in rt.instrument_info("nope")

    # the next start: the lab is the user's now; Kelvin only refreshes the folders and never rewrites lab.yaml
    before = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    again = AutoSetup(rt, settings, tmp_path, fetch=lambda url: fetched.append(url) or pdf,
                      scan=lambda: pytest.fail("no scan once the user has a lab")).run()
    assert again["state"] == "done" and len(fetched) == 3                              # manuals kept
    assert (tmp_path / "lab.yaml").read_text(encoding="utf-8") == before


def test_nothing_to_set_up_leaves_the_lab_without_instruments(tmp_path):
    rt = runtime_for(tmp_path)
    unknown = [{"address": "GPIB0::5::INSTR", "idn": "ACME,WIDGET", "card_id": None, "how": "no card matches"}]
    status = AutoSetup(rt, FakeSettings(), tmp_path, scan=lambda: unknown).run()
    assert "no card" in status["message"] and rt.lab_info["kind"] == "none" and not (tmp_path / "lab.yaml").exists()

    def no_visa():
        raise AdapterError("no VISA library")
    assert "could not look for instruments" in AutoSetup(rt, FakeSettings(), tmp_path, scan=no_visa).run()["message"]


def test_while_kelvin_is_busy_the_found_lab_opens_at_the_next_idle_moment(tmp_path, bench):
    """It once switched under the person's first answer (a whole turn ran on the lab it left), or not at all."""
    rt = runtime_for(tmp_path)
    settings = FakeSettings()
    answering = iter([True] * 4)                                          # the first answer takes a moment
    setup = AutoSetup(rt, settings, tmp_path, scan=lambda: scanned(bench),
                      can_switch=lambda: not next(answering, False))
    status = setup.run()
    assert rt.lab_info["kind"] == "file" and next(answering, None) is None   # switched once the answer ended
    assert (tmp_path / "lab.yaml").exists() and status["state"] == "done"


def test_portal_sets_up_on_start_and_a_person_reviews_the_limits(tmp_path, bench, pdf, monkeypatch):
    pytest.importorskip("starlette")
    from labharness.web import app as webapp
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted
    from starlette.testclient import TestClient

    monkeypatch.setattr(profiles, "http_get", lambda url: pdf)
    opened = []
    monkeypatch.setattr(webapp, "open_path", opened.append)
    rt = runtime_for(tmp_path)
    settings = Settings(tmp_path / "settings.json")
    app = webapp.create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"],
                            providers={"scripted": Scripted([])}, auto_setup=True)
    headers = {"X-Kelvin-Token": settings.access_key}
    with TestClient(app) as client:
        deadline = time.time() + 30
        while client.get("/api/instruments/setup", headers=headers).json()["state"] != "done":
            assert time.time() < deadline, "setting up took too long"
            time.sleep(0.05)
        view = client.get("/api/lab", headers=headers).json()
        assert sorted(view["unreviewed"]) == ["lockin", "smu", "tc"] and view["info"]["kind"] == "file"
        assert view["yaml"].count("added_by: kelvin") == 3
        folders = {f["name"]: f for f in view["folders"]}
        assert folders["lockin"]["manual"] and folders["lockin"]["manual_pages"] == 3
        assert folders["smu"]["parsed"]["model"] == "MODEL 2400"
        state = client.get("/api/state", headers=headers).json()
        assert sorted(state["unreviewed"]) == ["lockin", "smu", "tc"] and state["setup"]["state"] == "done"

        assert client.post("/api/instruments/lockin/open", json={"what": "manual"}, headers=headers).json()["ok"]
        assert client.post("/api/instruments/tc/open", json={"what": "folder"}, headers=headers).json()["ok"]
        assert [p.name for p in opened] == ["manual.pdf", "tc"]
        assert client.post("/api/instruments/nope/manual", json={}, headers=headers).status_code == 404
        bad = client.put("/api/instruments/tc/manual", content=b"hello", headers=headers)
        assert bad.status_code == 400 and "not a PDF" in bad.json()["error"]
        mine = client.put("/api/instruments/tc/manual", content=pdf, headers=headers).json()
        assert mine["ok"] and mine["manual"]["pages"] == 3
        again = client.post("/api/instruments/smu/manual", json={"force": True}, headers=headers).json()
        assert again["ok"] and again["manual"]["url"].startswith("https://download.tek.com/")

        connected = rt.lab
        r = client.post("/api/lab", json={"reviewed": True}, headers=headers).json()
        assert r["ok"] and r["unreviewed"] == [] and not rt.lab.config.unreviewed()
        assert rt.lab is connected                              # only the marks went: nothing connected again
        assert "added_by" not in (tmp_path / "lab.yaml").read_text(encoding="utf-8")
        events = [json.loads(line) for line in (tmp_path / "data" / "events.jsonl").read_text().splitlines()]
        assert [sorted(e["instruments"]) for e in events if e.get("type") == "lab_reviewed"] == [["lockin", "smu", "tc"]]
        assert any(e.get("by") == "Kelvin auto-setup" for e in events if e.get("type") == "lab_changed")
        assert client.post("/api/lab", json={"reviewed": True}, headers=headers).json()["ok"]   # reviewed already: fine

        # the file says more than the marks (a person tightened a limit in another editor): Kelvin connects it anew
        text = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
        edited = text.replace("V: [-210, 210]", "V: [-2, 2]").replace(
            'address: "GPIB0::24::INSTR"', 'address: "GPIB0::24::INSTR"\n    added_by: kelvin')
        assert edited.count("V: [-2, 2]") == 1 and "added_by" in edited
        (tmp_path / "lab.yaml").write_text(edited, encoding="utf-8")
        r = client.post("/api/lab", json={"reviewed": True}, headers=headers).json()
        assert r["ok"] and rt.lab is not connected and rt.lab.instruments["smu"].limits("V") == (-2.0, 2.0)
        assert not rt.lab.config.unreviewed() and "added_by" not in (tmp_path / "lab.yaml").read_text(encoding="utf-8")

        shown = client.get("/api/settings", headers=headers).json()["settings"]
        assert shown["auto_setup"] and shown["manuals"]                                   # on unless switched off
        shown = client.post("/api/settings", json={"auto_setup": "off", "manuals": "no"}, headers=headers).json()
        assert not shown["settings"]["auto_setup"] and not shown["settings"]["manuals"]
        assert settings.get("auto_setup") == "off" and settings.get("manuals") == "off"
    assert drive_commands(bench) == []


def test_kelvin_can_read_the_folders_over_mcp(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import INSTRUCTIONS, build_server
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), tmp_path / "data")   # the test lab's lock-in
    rt.home = tmp_path

    async def tools():
        async with Client(build_server(rt)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}
    listed = anyio.run(tools)
    for name in ("instrument_info", "search_manual"):
        assert listed[name].annotations.read_only_hint and not listed[name].annotations.open_world_hint
    assert "search_manual" in INSTRUCTIONS
    assert "simulated" in rt.instrument_info("lockin") and "simulated" in rt.search_manual("lockin", "OFLT")


def test_the_first_setup_uses_the_labs_scripts(tmp_path, bench, pdf):
    bench.devices["ASRL9::INSTR"] = Device(silent=True)   # says nothing to who-are-you, as many serial devices
    folder = tmp_path / "scripts"
    folder.mkdir()
    (folder / "iv.py").write_text('import pyvisa\nbias = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")\n'
                                  'for v in [0, 0.5, 1.0]:\n    bias.write(":SOUR:VOLT %g" % v)\n', encoding="utf-8")
    (folder / "temp.py").write_text('import serial\ncryo = serial.Serial("COM9", 57600)   # Lake Shore 336\n'
                                    'cryo.write(b"SETP 1,4.2")\n', encoding="utf-8")
    rt = runtime_for(tmp_path)
    setup = AutoSetup(rt, FakeSettings(), tmp_path, fetch=lambda url: pdf)
    assert setup.use_scripts(str(folder))["instruments"] == 2       # read (never run) before the scan drafts the lab
    status = setup.run()
    assert status["state"] == "done" and status["first"], status
    text = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    cfg = load_lab_config(text, builtin_registry())
    assert cfg.instruments["bias"].card == "keithley/2400"           # the scripts' name for it
    assert cfg.instruments["bias"].limits["V"] == (0, 1) and "from your scripts" in text   # the values they use
    cryo = cfg.instruments["cryo"]                                    # one the scan could not tell, by the scripts
    assert cryo.card == "lakeshore/336" and cryo.address == "ASRL9::INSTR" and cryo.connection["baud_rate"] == 57600
    assert {"lockin", "tc"} <= set(cfg.instruments) and "GPIB0::5::INSTR" not in text   # the rest as without scripts
    assert sorted(cfg.unreviewed()) == ["bias", "cryo", "lockin", "tc"]   # every one still asks until reviewed
    assert any("bias (" in n["text"] for n in rt.notifier.notices if n["kind"] == "setup")   # named as the lab does
    assert drive_commands(bench) == []
