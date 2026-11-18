"""A card one lab drafted and a person there reviewed stayed on that lab's computer, so the next lab drafted it again
(2026-11-17). Kelvin now sends a reviewed card to its public instrument collection (contribute.py): it fills in the
collection's issue form for the person to submit from their own GitHub account, shows exactly what goes and where
first, takes out what is private, and asks a person every time, never itself."""
from __future__ import annotations

import json
import types
import urllib.parse
from pathlib import Path

import pytest

from labharness import carddraft, cardtry, contribute, usbids
from labharness.homelab import DATA as KELVIN_DATA

# the ids the collection's form (.github/ISSUE_TEMPLATE/new-card.yml) has: its own tests hold the same list
FORM_FIELDS = ["instrument", "maker", "model", "kind", "interfaces", "idn", "usb", "firmware", "checked", "credit",
               "card"]

CARD = r"""# Drafted by Kelvin on 2026-11-14 from C:\Users\alice\Downloads\sm2000.pdf with a model.
card: 1
id: acme/sm-2000
model: ACME SM-2000 source-measure unit
vendor: ACME Instruments
kind: source-meter
status: lab-verified
manual: SM-2000 Programming Manual, chapter 6
manual_url: https://www.acme.example/manuals/sm-2000.pdf
notes:
  - "Kim Lab's unit is at TCPIP0::192.168.1.20::inst0::INSTR, on LABPC-07; ask alice (alice@kim-lab.example.edu)."
  - "Its driver is C:\\Program Files\\ACME\\acme.dll; logs in \\\\fileserver\\data and /home/alice/logs; MAC 00:1A:2B:3C:4D:5E."
identify:
  idn: "^ACME,SM-2000,"
  example: "ACME,SM-2000,SN998877,1.02"
error_query: "SYST:ERR?"
parameters:
  V: {label: Voltage, kind: drive, unit: V, set: "SOUR:VOLT {value}", get: "SOUR:VOLT?", limits: [-21, 21], safe: 0,
      ramp: gate, max_rate: 1}
  I: {label: Current, kind: measure, unit: A, get: "SENS:CURR?"}
  output: {label: Output, kind: drive, type: bool, set: "OUTP {value}", get: "OUTP?", safe: false}
"""
PRIVATE = [("TCPIP0::192.168.1.20::inst0::INSTR", "address"), ("192.168.1.20", "address"),
           ("SN998877", "serial number"), ("alice", "name"), ("Kim Lab", "lab"), ("LABPC-07", "host name")]
SECRETS = ["192.168.1.20", "SN998877", "alice", "Kim Lab", "LABPC-07", "fileserver", "kim-lab", "00:1A:2B", "Downloads"]
INDEX = {"instruments": [{"id": "acme/sm2000", "maker": "ACME Instruments", "model": "SM-2000",
                          "name": "ACME SM-2000 source-measure unit", "kind": "source-meter",
                          "interfaces": ["gpib", "usb-tmc"], "identify": {"idn": "^ACME,SM-?2000,"}, "card": None}]}


def card_of(text: str):
    card, problems, _ = carddraft.check(text)
    assert card is not None, problems
    return card


def offer(text: str = CARD, instruments=(), replies=(), ports=None, events=(), index=None, private=PRIVATE):
    card = card_of(text)
    return contribute.prepare(card, text, "this lab's own card", list(instruments), list(replies), ports or {},
                              list(events), index if index is not None else INDEX, private)


def query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# ------------------------------------------------------------------ what a contribution holds

def test_a_contribution_has_what_the_collection_files_a_card_by():
    c = offer(instruments=[{"name": "smu", "address": "USB0::0x05E6::0x2450::04412345::INSTR"},
                           {"name": "smu2", "address": "COM7"}, {"name": "smu3", "address": "serial://COM3"}],
              replies=["ACME,SM-2000,SN998877,1.02"], ports={"ASRL7::INSTR": "1313:80F8", "ASRL3::INSTR": "0403:6001"})
    f = c.fields
    assert f["instrument"] == "acme/sm2000"                       # the collection knows the model by its reply
    assert (f["maker"], f["model"], f["kind"]) == ("ACME Instruments", "SM-2000", "source-meter")
    assert f["interfaces"] == "usb-tmc, usb, rs-232"              # COM3 behind a generic FTDI chip: RS-232
    assert f["idn"] == "ACME,SM-2000,<serial number>,1.02" and f["firmware"] == "1.02"
    assert f["usb"] == "05e6:2450, 1313:80f8"                     # never the generic chip's 0403:6001
    assert "credit" not in f or not f["credit"]                   # the person's to type, never Kelvin's
    assert c.title == "Card: ACME Instruments SM-2000" and not c.copy
    q = query(c.url)
    assert c.url.startswith("https://github.com/%s/issues/new?" % contribute.REPO)
    assert q["template"] == "new-card.yml" and q["title"] == c.title and q["card"] == c.yaml
    assert {k: q[k] for k in ("instrument", "maker", "model", "kind", "interfaces", "idn", "usb", "firmware")} == \
        {k: f[k] for k in ("instrument", "maker", "model", "kind", "interfaces", "idn", "usb", "firmware")}
    assert list(contribute.FIELDS) == FORM_FIELDS                  # the ids the collection's form has


def test_without_the_collection_the_fields_come_from_the_card():
    c = offer(index={})
    assert c.fields["instrument"] == "" and c.fields["model"] == "SM-2000" and c.fields["maker"] == "ACME Instruments"
    assert c.fields["interfaces"] == c.fields["usb"] == c.fields["idn"] == ""   # nothing seen: nothing said


def test_nothing_private_leaves_the_lab():
    c = offer(replies=["ACME,SM-2000,SN998877,1.02"],
              instruments=[{"name": "smu", "address": "TCPIP0::192.168.1.20::inst0::INSTR"}])
    everything = "\n".join([c.yaml, c.url, urllib.parse.unquote(c.url), c.title] + list(c.fields.values()) +
                           contribute.preview(c))
    for secret in SECRETS:
        assert secret.lower() not in everything.lower(), secret
    assert "C:\\\\Program Files\\\\ACME\\\\acme.dll" in c.yaml     # the maker's own folder says nothing of the lab
    assert "<lab>'s unit is at <address>, on <host name>; ask <name> (<e-mail address>)" in c.yaml
    assert "logs in <file path> and <file path>; MAC <MAC address>" in c.yaml
    said = ", ".join(c.removed)
    for what in ("an address of the lab's", "a serial number", "this computer's name", "a person's name",
                 "the lab's name", "a file path (3)", "an e-mail address", "a MAC address"):
        assert what in said, said
    kept = card_of(c.yaml)                                          # still a card, with its id and identity
    assert kept.id == "acme/sm-2000" and kept.idn_example == "ACME,SM-2000,<serial number>,1.02"


def test_a_card_that_doesnt_work_without_what_is_private_is_not_offered():
    text = CARD.replace("id: acme/sm-2000", "id: kimlab/sm-2000")
    with pytest.raises(ValueError, match="holds what stays in this lab .*the lab's name.*Lab, Cards…, Edit"):
        offer(text, private=[("kimlab", "lab")])


def test_what_stays_in_the_lab_is_what_kelvin_knows_of_it(tmp_path, monkeypatch):
    home = tmp_path / "Kelvin"
    folder = home / "instruments" / "smu"
    folder.mkdir(parents=True)
    folder.joinpath("instrument.json").write_text(json.dumps({
        "card": "acme/sm-2000", "identity": "ACME,SM-2000,SN112233,1.02",
        "serial_number": {"serial": "SN112233", "card": "acme/sm-2000", "address": "COM7"}}), encoding="utf-8")
    lab = types.SimpleNamespace(
        config=types.SimpleNamespace(name="Kim Lab", instruments={
            "smu": types.SimpleNamespace(card="acme/sm-2000", address="lib://COM7?python=D:/py32/python.exe"),
            "dmm": types.SimpleNamespace(card="x/y", address="USB0::0x2A8D::0x0101::MY5700123::INSTR"),
            "sim": types.SimpleNamespace(card="x/z", address="sim://twin")}),
        instruments={})
    rt = types.SimpleNamespace(lab=lab, home=home, user="Alice Kim", data_dir=tmp_path / "data",
                               found_info={"TCPIP0::10.0.0.7::5025::SOCKET": {"idn": "ACME,X,SN445566,2.0"}})
    monkeypatch.setattr(contribute.socket, "gethostname", lambda: "LABPC-07")
    values = dict(contribute.private_values(rt))
    for value, what in [("lib://COM7?python=D:/py32/python.exe", "address"), ("D:/py32/python.exe", "file path"),
                        ("MY5700123", "serial number"), ("SN112233", "serial number"), ("SN445566", "serial number"),
                        ("10.0.0.7", "address"), ("LABPC-07", "host name"), ("Alice Kim", "name"), ("Kim Lab", "lab"),
                        (str(home), "file path")]:
        assert values.get(value) == what, value
    assert "sim://twin" not in values and "twin" not in values      # a simulation is nowhere
    replies, serials = contribute.identities(rt, "acme/sm-2000", [{"name": "smu", "address": "COM7"}])
    assert replies == ["ACME,SM-2000,SN112233,1.02"] and serials == ["SN112233"]


# ------------------------------------------------------------------ the check on hardware, from the lab's record

def event(kind, wall="2026-11-14T10:00:00.000", **fields):
    return dict(fields, type=kind, wall=wall)


RECORD = [
    event("instrument_added", "2026-11-02T09:00:00.000", instrument="smu", card="acme/sm-2000",
          address="GPIB0::24::INSTR"),
    event("card_try", "2026-11-02T09:05:00.000", card="acme/sm-2000", address="GPIB0::24::INSTR", answered=3,
          matches=True, not_tried=1, set_check=True),
    event("card_try", card="acme/sm-2000", address="sim://smu", answered=3),            # a simulation: no check
    event("card_try", card="other/card", address="GPIB0::5::INSTR", answered=9),        # another card
    event("command", method="get", action="read", ok=True, target="smu.V", value=0.0),
    event("command", method="get", action="read", ok=True, target="smu.I", value=1e-9),
    event("command", method="get", action="read", ok=False, target="smu.I"),            # failed: no check
    event("command", method="set", action="set", ok=True, target="smu.V", value=2.5),
    event("command", method="set", action="set", ok=True, target="smu.V", value=5.0),
    event("command", method="safe", action="set", ok=True, target="smu.V", value=0),
    event("command", method="set", action="set", ok=True, target="smu.output", value=True),
    event("command", method="set", action="refused", ok=False, target="smu.V", value=30),   # refused: never sent
    event("command", method="get", action="read", ok=True, target="lockin.X", value=1.0),  # another instrument
    event("command", "2026-11-03T09:00:00.000", method="set", action="set", ok=True, target="old.V", value=9.0),
    event("instrument_changed", "2026-11-05T09:00:00.000", instrument="old", card="acme/sm-2000", address="sim://old"),
    event("command", "2026-11-14T11:00:00.000", method="set", action="set", ok=True, target="old.V", value=7.0),
]


def test_the_check_on_hardware_comes_from_the_labs_record():
    card = card_of(CARD)
    text = contribute.hardware_summary(RECORD, card, ["smu", "old"])
    assert text.startswith("From this lab's Kelvin record, 2026-11-02 to 2026-11-14:"), text
    assert "Tried with Kelvin's Try it once on the instrument; the last time it answered as the card expects, " \
           "3 readings answered, 1 not tried, its set command checked." in text
    assert "Read on the instrument: I, V (2 reads)." in text
    assert "Set on the instrument: V (0 V to 5 V), output (on)." in text
    assert "9 V" not in text and "7 V" not in text      # before the record placed it, and while it was simulated
    assert contribute.hardware_summary(RECORD, card, []) .count("\n") == 1   # only the try, with no instrument here
    assert contribute.hardware_summary([], card, ["smu"]) == ""
    c = offer(instruments=[{"name": "smu", "address": "GPIB0::24::INSTR"}], events=RECORD)
    assert c.fields["checked"] == text and query(c.url)["checked"] == text and c.fields["interfaces"] == "gpib"


def test_the_record_read_is_the_labs_and_never_unshared_work(tmp_path):
    lines = [event("command", method="get", action="read", ok=True, target="smu.V", value=1.0),
             event("command", method="get", action="read", ok=True, target="smu.I", value=1.0,
                   whose={"project": "p1", "confidential": True}),
             event("ask", request="something"), event("card_try", card="acme/sm-2000", address="COM7", answered=2)]
    shared = tmp_path / "instrument-log.jsonl"
    shared.write_text("\n".join(json.dumps(e) for e in lines) + "\nnot json\n", encoding="utf-8")
    rt = types.SimpleNamespace(shared_log=shared, events_path=tmp_path / "events.jsonl",
                               unshared_work=lambda whose: bool(whose and whose.get("confidential")))
    read = list(contribute.lab_record(rt))
    assert [(e["type"], e.get("target")) for e in read] == [("command", "smu.V"), ("card_try", None)]


def test_a_try_keeps_what_it_found_in_the_record():
    result = {"matches": True, "readings": [{"name": "V", "value": 1.0}, {"name": "I", "error": "timeout"},
                                            {"name": "T", "skipped": "starts a measurement"}],
              "write_check": {"name": "range", "ok": False, "error": "-113"}}
    assert cardtry.recorded(result) == {"answered": 1, "failed": 1, "not_tried": 1, "matches": True,
                                        "set_check": False}
    assert cardtry.recorded({"readings": []}) == {"answered": 0, "failed": None, "not_tried": None, "matches": None,
                                                  "set_check": None}


# ------------------------------------------------------------------ the link, and the clipboard past its length

def test_the_form_link_is_filled_in_and_a_long_card_goes_by_the_clipboard():
    short = offer()
    assert not short.copy and query(short.url)["card"] == short.yaml
    long_card = CARD + "".join("  p%d: {kind: measure, get: \"MEAS:VOLT%d?\", unit: V, label: \"Reading %d\"}\n"
                               % (i, i, i) for i in range(200))
    long = offer(long_card)
    assert long.copy and "card" not in query(long.url) and query(long.url)["maker"] == "ACME Instruments"
    signin = len(contribute.SIGNIN) + len(urllib.parse.quote(long.url, safe=""))
    assert len(long.url) <= contribute.MOST and signin <= contribute.MOST   # GitHub and its sign-in page take it
    assert any("copies it, and you paste it" in i for i in contribute.preview(long))
    huge = {"maker": "ACME", "checked": "\n".join("- line %d of what was checked, and more words" % i
                                                  for i in range(400))}
    url, copy, used = contribute.form_url(huge, "card: 1\n" * 2000, "Card: ACME")
    assert copy and contribute.fits(url) and query(url)["checked"] == used["checked"]   # the preview shows the same
    assert used["checked"].endswith("\n…") and len(used["checked"]) <= contribute.SUMMARY_MOST


def test_on_windows_a_long_link_opens_through_a_page_here():
    opened = []
    long_url = "https://github.com/x/y/issues/new?card=" + "a" * 3000
    assert contribute.open_form(long_url, opened.append, windows=True) is False   # list.append says None
    assert opened[-1].startswith("file:") and opened[-1].endswith("kelvin-send-card.html")
    text = contribute.launcher(long_url).read_text(encoding="utf-8")
    assert 'http-equiv="refresh" content="0; url=%s"' % long_url in text and 'href="%s"' % long_url in text
    contribute.open_form("https://github.com/x/y/issues/new?a=1", opened.append, windows=True)
    contribute.open_form(long_url, opened.append, windows=False)
    assert opened[-2:] == ["https://github.com/x/y/issues/new?a=1", long_url]   # short, or not Windows: the link


def test_handing_over_copies_only_what_the_link_cant_carry():
    copied, opened = [], []
    short = offer()
    assert contribute.hand_over(short, lambda u: opened.append(u) or True, lambda t: copied.append(t) or True) == \
        (True, False)
    long = offer(CARD + "".join("  q%d: {kind: measure, get: \"MEAS%d?\"}\n" % (i, i) for i in range(300)))
    assert contribute.hand_over(long, lambda u: opened.append(u) or True, lambda t: copied.append(t) or True) == \
        (True, True)
    assert copied == [long.yaml] and opened == [short.url, long.url]


# ------------------------------------------------------------------ what can't be sent

def write(folder: Path, text: str) -> Path:
    card = card_of(text)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / carddraft.file_for(card.id)
    path.write_text(text, encoding="utf-8")
    return path


def test_drafts_and_cards_with_nothing_new_are_refused(tmp_path):
    cards = tmp_path / "cards"
    write(cards, CARD.replace("status: lab-verified", "status: draft"))
    with pytest.raises(ValueError, match="acme/sm-2000 is a draft: a person reviews a card before"):
        contribute.reviewed_card("acme/sm-2000", cards)
    (cards / "acme_sm-2000.yaml").write_text(CARD.replace("safe: 0,", ""), encoding="utf-8")   # a drive, no safe value
    with pytest.raises(ValueError, match="has problems to fix first"):
        contribute.reviewed_card("acme/sm-2000", cards)
    with pytest.raises(KeyError, match="no card 'nobody/none'"):
        contribute.reviewed_card("nobody/none", cards)
    with pytest.raises(ValueError, match="srs/sr830 is Kelvin's own card as it is, so there is nothing new"):
        contribute.reviewed_card("srs/sr830", cards)                 # one of Kelvin's, never changed here
    kelvins = (KELVIN_DATA / "cards" / "instruments" / "srs_sr830.yaml").read_text(encoding="utf-8")
    write(cards, kelvins.replace("status: community", "status: lab-verified"))
    with pytest.raises(ValueError, match="Kelvin's own card as it is.*Hardware verification form: https://github"):
        contribute.reviewed_card("srs/sr830", cards)                 # reviewed here, but the same card
    changed = (cards / "srs_sr830.yaml").read_text(encoding="utf-8").replace(
        'manual: "SR830 DSP Lock-In Amplifier manual, chapter 5 (Remote programming)"',
        'manual: "SR830 DSP Lock-In Amplifier manual, revision 2.5, chapter 5 (Remote programming)"', 1)
    assert changed != kelvins.replace("status: community", "status: lab-verified")
    (cards / "srs_sr830.yaml").write_text(changed, encoding="utf-8")
    card, text, origin = contribute.reviewed_card("srs/sr830", cards)
    assert card.id == "srs/sr830" and origin == "this lab's version of Kelvin's card" and text == changed


def test_the_collections_cards_as_they_are_have_nothing_new(tmp_path):
    theirs = CARD.replace("status: lab-verified", "status: community")
    collection = types.SimpleNamespace(
        instrument=lambda iid: {"id": iid, "card": {"path": "instruments/x/card.yaml", "sha256": "0" * 64}}
        if iid == "acme/sm-2000" else None,
        read=lambda path, digest: theirs.encode("utf-8"))
    cards = tmp_path / "cards"
    with pytest.raises(ValueError, match="the collection's card as it is"):
        contribute.reviewed_card("acme/sm-2000", cards, collection=collection)   # not even here
    path = write(cards, CARD)                                         # reviewed here, the same card otherwise
    with pytest.raises(ValueError, match="the collection's card as it is"):
        contribute.reviewed_card("acme/sm-2000", cards, collection=collection)
    path.write_text(CARD.replace("unit: A", "unit: A, description: the source's current"), encoding="utf-8")
    assert contribute.reviewed_card("acme/sm-2000", cards, collection=collection)[2] == \
        "this lab's version of the collection's card"
    import hashlib
    path.write_text(theirs, encoding="utf-8")                         # as kic install copied it, untouched
    (cards / ".kelvin-instruments.json").write_text(json.dumps({"installed": {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()}}), encoding="utf-8")
    with pytest.raises(ValueError, match="the collection's card as it is"):
        contribute.reviewed_card("acme/sm-2000", cards)              # with the collection off, too


def test_generic_usb_serial_chips_are_known():
    assert usbids.is_generic("0403:6001") and usbids.is_generic("10C4:EA60 ") and usbids.is_generic("1a86:55d4")
    assert not usbids.is_generic("1313:8078") and not usbids.is_generic("")


# ------------------------------------------------------------------ the agent's tool and the portal's button

def runtime(tmp_path, answers):
    pytest.importorskip("mcp")
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover(answers), tmp_path / "data")
    rt.cards_dir, rt.home = tmp_path / "Kelvin" / "cards", tmp_path / "Kelvin"
    write(rt.cards_dir, CARD)
    return rt


def test_the_tool_asks_a_person_every_time_and_never_itself(tmp_path, monkeypatch):
    opened, copied = [], []
    monkeypatch.setattr(contribute, "open_form", lambda url, opener=None: opened.append(url) or True)
    monkeypatch.setattr(contribute, "copy_text", lambda text: copied.append(text) or True)
    rt = runtime(tmp_path, [False, True])
    rt.user = "alice"                                                 # the person signed in: never named publicly
    rt.gate.auto = True                                               # auto mode on: still a person's yes

    def never(*args, **kwargs):
        raise AssertionError("Kelvin approved it itself")
    monkeypatch.setattr(rt.gate, "auto_simple", never)
    monkeypatch.setattr(rt.gate, "auto_approve", never)
    said = rt.contribute_card("acme/sm-2000")
    assert said.startswith("declined: the card stays on this computer") and not opened
    request = rt.approver.requests[0]
    assert request.title == "Send your card acme/sm-2000 (ACME SM-2000 source-measure unit) to Kelvin's public " \
                            "instrument collection?"
    items = "\n".join(request.items)
    assert "a public issue in github.com/%s" % contribute.REPO in items and "your own GitHub account" in items
    assert "Maker: ACME Instruments" in items and "Kind: source-meter" in items and "Credit: left empty" in items
    assert "The card, as it goes:\ncard: 1\nid: acme/sm-2000" in items.replace("# Drafted by Kelvin on 2026-11-14 "
                                                                                "from <file path> with a model.\n", "")
    assert "alice" not in items and "192.168.1.20" not in items
    said = rt.contribute_card("acme/sm-2000")
    assert said.startswith("Opened the collection's form in the person's browser") and len(opened) == 1
    assert "Nothing is sent until the person submits the form on GitHub" in said and not copied
    rt.gate.log.settle()
    offered = [e for e in rt.gate.log.events() if e["type"] == "card_offered"]
    assert len(offered) == 1 and offered[0]["card"] == "acme/sm-2000" and offered[0]["opened"] is True
    asks = [e for e in rt.gate.log.events() if e["type"] == "ask"]
    assert [e["answer"] for e in asks] == ["declined", "approved"]   # both a person's answers, in the record
    write(rt.cards_dir, CARD.replace("status: lab-verified", "status: draft"))
    assert rt.contribute_card("acme/sm-2000").startswith("not offered: acme/sm-2000 is a draft")   # nothing asked
    assert rt.contribute_card("srs/sr830").startswith("not offered: srs/sr830 is Kelvin's own card as it is")
    assert len(rt.approver.requests) == 2


def test_the_tool_is_one_kelvin_has_and_off_where_a_project_keeps_off_the_internet(tmp_path):
    import asyncio
    rt = runtime(tmp_path, [])
    from labharness.agent.server import build_server
    from labharness.web.agent import DEFERRED_NAMES, WEB_TOOLS

    async def tools():
        from mcp import Client
        async with Client(build_server(rt)) as c:
            r = await c.list_tools()
            return {t.name: t for t in getattr(r, "tools", r)}
    tool = asyncio.run(tools())["contribute_card"]
    assert "approval card" in tool.description and "never approves it itself" in tool.description
    assert tool.annotations.open_world_hint and not tool.annotations.read_only_hint
    assert "contribute_card" in DEFERRED_NAMES and "contribute_card" in WEB_TOOLS


def test_the_portal_shows_what_goes_and_only_the_owner_sends_it(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from simlab import build_virtual_lab
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import STATIC, create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    handed = []
    monkeypatch.setattr(contribute, "hand_over", lambda c: handed.append(c) or (True, c.copy))
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), tmp_path / "data")
    home = tmp_path / "Kelvin"
    settings = Settings(home / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(home / "chats"), hosts=["testserver"], providers={}))
    h = {"X-Kelvin-Token": settings.access_key}
    write(home / "cards", CARD)
    write(home / "cards", CARD.replace("acme/sm-2000", "acme/sm-3000").replace("lab-verified", "draft"))
    with client:
        r = client.post("/api/cards/contribute", json={"id": "acme/sm-2000"}, headers=h)
        assert r.status_code == 200, r.text
        preview = r.json()
        assert preview["question"].startswith("Send your card acme/sm-2000") and preview["ticket"]
        assert preview["items"][-1] == "The card, as it goes:\n" + preview["yaml"].rstrip("\n")
        assert query(preview["url"])["card"] == preview["yaml"] and not preview["copy"]
        r = client.post("/api/cards/contribute", json={"id": "acme/sm-3000"}, headers=h)
        assert r.status_code == 409 and "is a draft" in r.json()["error"]
        r = client.post("/api/cards/contribute", json={"id": "srs/sr830"}, headers=h)
        assert r.status_code == 409 and "nothing new" in r.json()["error"]
        r = client.post("/api/cards/contribute", json={"ticket": preview["ticket"], "open": False}, headers=h)
        assert r.status_code == 200 and not handed                    # a browser opened it itself: only recorded
        r = client.post("/api/cards/contribute", json={"ticket": preview["ticket"], "open": False}, headers=h)
        assert r.status_code == 410                                   # a ticket goes once
        client.post("/api/owner-pin", json={"pin": "2468"}, headers=h)
        r = client.post("/api/cards/contribute", json={"id": "acme/sm-2000"}, headers=h)
        assert r.status_code == 403 and r.json()["pin"]               # the lab owner's, as all that leaves the lab
        r = client.post("/api/cards/contribute", json={"id": "acme/sm-3000"}, headers=h)
        assert r.status_code == 409 and "is a draft" in r.json()["error"]   # said at once, no PIN asked for it
        r = client.post("/api/cards/contribute", json={"id": "acme/sm-2000", "pin": "2468"}, headers=h)
        ticket = r.json()["ticket"]
        r = client.post("/api/cards/contribute", json={"ticket": ticket, "open": True}, headers=h)
        assert r.json()["opened"] is True and handed[0].url == preview["url"]   # what was shown is what goes
    rt.gate.log.settle()
    offered = [e for e in rt.gate.log.events() if e["type"] == "card_offered"]
    assert [e["where"] for e in offered] == ["the person's browser", "this computer's browser"]
    assert all(e["by"] == "human (portal)" for e in offered)
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '"Send to the collection"' in js and "function sendCard(c)" in js and "function offerDialog(r, go)" in js
