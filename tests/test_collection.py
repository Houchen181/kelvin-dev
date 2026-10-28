"""The instrument collection: a copy of Kelvin's public repository pinned to one commit and checked file by file. Its
cards may replace Kelvin's own but never a card a person reviewed here; it names instruments no card matches and
gives their manuals to the card writer; its experiments' plans are mapped to a lab's own instruments."""
import hashlib
import json
import re
import time
import urllib.error

import pytest
import yaml

from labharness import autocard, build_virtual_lab, builtin_registry, carddraft, demo_envelope, load_plan
from labharness.adapters import set_resource_manager
from labharness.collection import API, COMMIT, FILE, REPO, Collection, adapt, own_versions, text_sha256
from labharness.discovery import identify
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.survey import Survey, reason
from labharness.updater import REPOS
from labharness.virtual_lab import DATA, INSTALLED, build_lab, collection_ids, kelvin_ids, lab_registry
from labharness.yamlio import load_yaml
from test_hardware import Bus, Device

A, B = "a" * 40, "b" * 40   # two commits of the public repository
MANUAL = "https://download.tek.com/manual/622x-901-01.pdf"
IDN = "KEITHLEY INSTRUMENTS INC.,MODEL 6220,4321,D04 /700x"   # a 6220: Kelvin has a card for the 6221 only
WIDGET = """card: 1
id: acme/widget
model: ACME Widget source
vendor: ACME
kind: source-meter
status: community
identify: { idn: "^ACME,WIDGET" }
parameters:
  V: { label: Voltage, kind: drive, unit: V, set: "VOLT {value}", get: "VOLT?", limits: [-10, 10], safe: 0 }
  I: { label: Current, kind: measure, unit: A, get: "CURR?" }
"""
CURRENT_SOURCE = {   # an instrument the collection knows, with no card yet
    "id": "keithley/6220", "maker": "Keithley", "model": "6220", "name": "Keithley 6220 DC current source",
    "kind": "current-source", "identify": {"idn": "^KEITHLEY INSTRUMENTS INC\\.,\\s*MODEL 6220,"}, "card": None,
    "manuals": [{"title": "Model 6220/6221 Reference Manual", "url": MANUAL, "access": "public"},
                {"title": "Service manual", "url": "https://www.tek.com/service-6221.pdf", "access": "sign-in"}]}
LAB = """lab: Test bench
instruments:
  gate: { card: keithley/2400 }
  lockin_xx: { card: srs/sr830 }
  lockin_xy: { card: srs/sr830 }
  ppms: { card: qd/multivu }
"""
EXPERIMENT = """schema: 1
id: transport/gate-sweep
area: transport
title: Field-effect gate sweep
summary: Tune the carrier density with a gate and record the conductance.
roles:
  gate: { kind: [source-meter, voltage-source], does: Ramps the gate voltage., suggested: [keithley/2400] }
  lockin: { kind: lock-in, does: Reads the four-probe voltage., suggested: [srs/sr830] }
  cryostat: { kind: [cryostat, temperature-controller], does: Holds the temperature., optional: true }
lab: labs/test-bench.yaml
procedure:
  - Sweep a small window first, there and back.
plans:
  - title: Gate sweep from -1 V to 1 V and back
    plan:
      plan: gate_sweep
      preconditions: ["ppms.T < 10"]
      setup: { gate.compliance_I: 1.0e-8, gate.output: on, lockin_xx.time_constant: "300 ms" }
      sweep: { gate.V: { from: -1, to: 1, step: 0.1, back: true, stabilize: { read: ppms.T, within: 0.1, for: 10 } } }
      measure: [lockin_xx.X, lockin_xy.X, gate.I]
      after: { gate.V: 0 }
"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bundled(card_id):
    """A card as Kelvin brings it."""
    with open(builtin_registry().get(card_id).source, encoding="utf-8") as f:
        return f.read()


def with_status(text, status):
    return re.sub(r"(?m)^status:.*$", "status: " + status, text, count=1)


class GitHub:
    """The public repository as Kelvin reaches it: the repository (its own name, after a rename, and whether it is
    public), the newest commit of its default branch, and each commit's files."""

    def __init__(self, name=REPO, private=False):
        self.commits, self.head, self.asked, self.down = {}, None, [], False
        self.name, self.private = name, private

    def publish(self, commit, cards=(), catalog=(), experiments=(), labs=()):
        """A commit with its index.json, the way the collection's kic index writes it. Returns its files."""
        files, instruments = {}, []
        for text in cards:
            card = yaml.safe_load(text)
            path = "instruments/test/%s/%s/card.yaml" % (card["kind"], card["id"].replace("/", "-"))
            files[path] = text.encode()
            instruments.append({"id": card["id"], "name": card["model"], "kind": card["kind"],
                                "identify": card.get("identify"), "manuals": [],
                                "card": {"path": path, "sha256": sha(files[path]), "status": card["status"]}})
        index = {"schema": 1, "instruments": instruments + list(catalog), "experiments": [], "labs": []}
        for text in experiments:
            e = yaml.safe_load(text)
            path = "experiments/%s.yaml" % e["id"]
            files[path] = text.encode()
            index["experiments"].append(dict({k: e[k] for k in ("id", "area", "title", "summary", "roles", "lab")},
                                             path=path, sha256=sha(files[path])))
        for name, text in labs:
            path = "labs/%s.yaml" % name
            files[path] = text.encode()
            index["labs"].append({"id": name, "path": path, "sha256": sha(files[path]),
                                  "instruments": {k: v["card"] for k, v in yaml.safe_load(text)["instruments"].items()}})
        files["index.json"] = json.dumps(index).encode()
        self.commits[commit], self.head = files, commit
        return files

    def __call__(self, url):
        self.asked.append(url)
        if self.down:
            raise urllib.error.URLError("no route to host")
        if url == API % REPO:
            return json.dumps({"full_name": self.name, "private": self.private, "default_branch": "main"}).encode()
        if url == COMMIT % (self.name, "main"):
            return self.head.encode()
        commit, path = url.split("/%s/" % self.name, 1)[1].split("/", 1)
        if path not in self.commits.get(commit, {}):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.commits[commit][path]


def synced(home, **published):
    gh = GitHub()
    gh.publish(A, **published)
    c = Collection(home)
    assert c.sync(gh)["error"] is None
    return c


# ------------------------------------------------------------------ the copy on this computer

def test_the_copy_is_pinned_to_one_commit_and_checked_file_by_file(tmp_path):
    gh = GitHub()
    gh.publish(A, cards=[WIDGET], catalog=[CURRENT_SOURCE], experiments=[EXPERIMENT], labs=[("test-bench", LAB)])
    c = Collection(tmp_path)
    assert c.index() == {} and c.cards() == []                              # nothing before the first update
    status = c.sync(gh)
    assert status["commit"] == A and status["error"] is None and status["cards"] == 1 and status["experiments"] == 1
    raw = [u for u in gh.asked if u.startswith("https://raw.githubusercontent.com/")]
    assert gh.asked[:2] == [API % REPO, COMMIT % (REPO, "main")] and len(raw) == 4   # index.json and three files
    assert all("/%s/%s/" % (REPO, A) in u for u in raw)                      # all of them from that one commit
    assert json.loads((tmp_path / "collection" / "state.json").read_text())["commit"] == A
    assert c.instrument_for("ACME,WIDGET,1,1")["id"] == "acme/widget"

    card = next((tmp_path / "collection" / A[:12]).glob("instruments/**/card.yaml"))
    card.write_bytes(card.read_bytes() + b"# changed on this computer\n")     # checked again whenever it is read
    assert c.cards() == [] and "does not match" in c.status()["left_out"][0]
    gh.asked.clear()
    assert c.sync(gh)["used"] == ["acme/widget"]                            # the next update puts it right ...
    assert [u.rsplit("/", 1)[1] for u in gh.asked[2:]] == ["card.yaml"]      # ... fetching only that file


def test_offline_or_with_a_bad_file_the_copy_kelvin_has_stays(tmp_path):
    gh = GitHub()
    gh.publish(A, cards=[WIDGET], experiments=[EXPERIMENT], labs=[("test-bench", LAB)])
    c = Collection(tmp_path)
    c.sync(gh)
    verified = with_status(WIDGET, "lab-verified")   # a lab checked it on hardware
    files = gh.publish(B, cards=[verified], experiments=[EXPERIMENT], labs=[("test-bench", LAB)])
    path = next(p for p in files if p.endswith("card.yaml"))
    files[path] += b"# not what index.json says\n"
    status = c.sync(gh)
    assert "does not match its SHA-256" in status["error"] and status["commit"] == A
    assert c.cards()[0].status == "community"                              # the copy before stays in use

    gh.down = True
    status = c.sync(gh)
    assert "could not update the instrument collection" in status["error"] and status["commit"] == A
    assert lab_registry(tmp_path, c).get("acme/widget").status == "community"   # offline, from the copy here

    gh.down = False
    gh.publish(B, cards=[verified], experiments=[EXPERIMENT], labs=[("test-bench", LAB)])
    gh.asked.clear()
    status = c.sync(gh)
    assert status["commit"] == B and status["error"] is None and c.cards()[0].status == "lab-verified"
    assert len(gh.asked) == 4       # the repository, its commit, index.json and the card that changed; not the rest
    assert sorted(p.name for p in (tmp_path / "collection").iterdir()) == [B[:12], "state.json"]   # the old copy went


def test_the_index_cannot_point_outside_the_copy(tmp_path):
    gh = GitHub()
    files = gh.publish(A, cards=[WIDGET])
    index = json.loads(files["index.json"])
    index["instruments"][0]["card"]["path"] = "../../settings.json"
    files["index.json"] = json.dumps(index).encode()
    status = Collection(tmp_path).sync(gh)
    assert "not a file of the collection" in status["error"] and status["commit"] is None
    assert not any("settings.json" in u for u in gh.asked) and not (tmp_path / "settings.json").exists()
    for bad in ("instruments/../x.yaml", "/etc/x.yaml", "instruments\\a\\card.yaml", "cards/x.yaml", "labs/x.json"):
        assert not FILE.match(bad), bad
    assert FILE.match("instruments/electronics/lock-in/srs-sr830/card.yaml") and FILE.match("labs/nv-odmr.yaml")
    gh.head = "main"
    assert "which commit is the newest" in Collection(tmp_path).sync(gh)["error"]


def test_only_a_public_repository_is_read_and_a_rename_is_followed(tmp_path):
    private = GitHub(private=True)
    private.publish(A, cards=[WIDGET])
    status = Collection(tmp_path).sync(private)
    assert "not public" in status["error"] and status["commit"] is None
    assert private.asked == [API % REPO]                                     # nothing more is asked of it

    gh = GitHub()
    gh.publish(A, cards=[WIDGET])
    c = Collection(tmp_path)
    assert REPO == REPOS["stable"] != REPOS["private"]          # where published builds update from, not the source
    assert c.sync(gh)["repository"] == REPO
    renamed = GitHub(name="Houchen181/kelvin-public")   # renamed: GitHub sends the old name on to the repository
    renamed.publish(B, cards=[with_status(WIDGET, "lab-verified")])
    status = c.sync(renamed)
    assert status["error"] is None and status["repository"] == "Houchen181/kelvin-public" and status["commit"] == B
    assert renamed.asked[1] == COMMIT % ("Houchen181/kelvin-public", "main")
    assert all("/Houchen181/kelvin-public/%s/" % B in u for u in renamed.asked[2:])   # its files, under its own name
    assert c.cards()[0].status == "lab-verified" and len(Collection(tmp_path).cards()) == 1


# ------------------------------------------------------------------ which card Kelvin uses

def test_a_collection_card_replaces_kelvins_own_and_keeps_its_status(tmp_path):
    fixed = with_status(bundled("srs/sr830"), "lab-verified")               # checked on hardware by a lab
    draft = with_status(bundled("keithley/2400"), "draft")                  # changed, and not reviewed yet
    c = synced(tmp_path, cards=[fixed, draft, WIDGET])
    reg = lab_registry(tmp_path, c)
    assert reg.get("srs/sr830").status == "lab-verified" and reg.get("srs/sr830").source.startswith(str(c.dir))
    assert reg.get("acme/widget").status == "community"                     # and a model Kelvin has no card for
    assert sorted(c.status()["newer"]) == ["keithley/2400", "srs/sr830"] and len(c.status()["used"]) == 3
    assert "acme/widget" in kelvin_ids(c) and "acme/widget" not in kelvin_ids()

    set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k2400")}))
    try:   # on real hardware the collection's draft asks a person before each output change, like any draft
        lab = build_lab('lab: Bench\ninstruments:\n  smu: { card: keithley/2400, address: "GPIB0::24::INSTR" }\n',
                        registry=reg)
        gate = SafetyGate(lab, ScriptedApprover([False]))
        declined = gate.set("smu", "V", 1.0)
        assert lab.instruments["smu"].card.status == "draft" and not declined.ok
        assert "its card is a draft" in str(gate.approver.requests[0])
        assert gate.get("smu", "I").ok
        lab.close()
    finally:
        set_resource_manager(None)


def test_a_copy_of_an_older_kelvin_card_never_replaces_the_newer_one(tmp_path):
    mercury = bundled("oxford/mercury-itc")
    older = mercury.replace('manual_url: "https://www.mymercurysupport.com/sites/default/files/59_UMC0071_EN_02.pdf"',
                            "# manual_url: none: Oxford Instruments (now Quantum Design Oxford) publishes no public "
                            "copy; choose the PDF in Lab setup")
    assert older != mercury and text_sha256(older.encode()) in own_versions()["oxford/mercury-itc"]   # Kelvin 0.3.24's
    c = synced(tmp_path, cards=[older])
    status = c.status()
    assert c.cards() == [] and status["cards"] == 1 and status["used"] == status["newer"] == status["left_out"] == []
    card = lab_registry(tmp_path, c).get("oxford/mercury-itc")
    assert card.manual_url == "https://www.mymercurysupport.com/sites/default/files/59_UMC0071_EN_02.pdf"
    assert card.source == builtin_registry().get("oxford/mercury-itc").source         # Kelvin's own


def test_every_version_of_kelvins_own_cards_is_recorded():
    listed = json.loads((DATA / "card_versions.json").read_text(encoding="utf-8"))["cards"]
    for card in builtin_registry():
        if card.id.startswith("virtual/"):
            continue
        with open(card.source, "rb") as f:
            digest = text_sha256(f.read())
        assert digest in listed.get(card.id, []), \
            'src/labharness/data/card_versions.json: add "%s" to the list for %s, its new version' % (digest, card.id)


def model(text, name):
    return re.sub(r"(?m)^model:.*$", "model: " + name, text, count=1)


def test_nothing_replaces_a_card_a_person_reviewed_here(tmp_path):
    widget_v2 = model(WIDGET, "ACME Widget source, 2nd edition")
    c = synced(tmp_path, cards=[with_status(bundled("srs/sr830"), "lab-verified"),
                                model(bundled("keithley/2450"), "Keithley 2450, fixed in the collection"), widget_v2])
    cards = tmp_path / "cards"
    with pytest.raises(ValueError, match="already has a card acme/widget"):   # a new card can't take the collection's id
        carddraft.save(cards, with_status(WIDGET, "draft"), kelvin_ids(c))
    cards.mkdir()
    ours = model(with_status(bundled("srs/sr830"), "lab-verified"), "Our SR830")
    (cards / "srs_sr830.yaml").write_text(ours, encoding="utf-8")                              # reviewed here
    (cards / "keithley_2450.yaml").write_text(model(with_status(bundled("keithley/2450"), "draft"), "Our draft"),
                                              encoding="utf-8")                                 # a draft
    (cards / "acme_widget.yaml").write_bytes(WIDGET.encode())                                   # copied by kic install
    (cards / INSTALLED).write_text(json.dumps({"installed": {"acme_widget.yaml": sha(WIDGET.encode())}}))

    reg = lab_registry(tmp_path, c)
    assert reg.get("srs/sr830").model == "Our SR830"                             # theirs: nothing replaces it
    assert reg.get("keithley/2450").model == "Keithley 2450, fixed in the collection"   # a draft gives way
    assert reg.get("acme/widget").model.endswith("2nd edition")                  # kic's copy, unchanged: the collection's
    listed = {x["id"]: x for x in carddraft.user_cards(cards, collection_ids(c))}
    assert "uses that instead of this draft" in listed["keithley/2450"]["warnings"][0]
    assert not any("instead" in w for w in listed["srs/sr830"]["warnings"])

    (cards / "acme_widget.yaml").write_bytes(WIDGET.replace("ACME,WIDGET", "ACME,WIDGET,").encode())
    assert lab_registry(tmp_path, c).get("acme/widget").model == "ACME Widget source"   # changed here: theirs now

    off = lab_registry(tmp_path)                    # the collection switched off: Kelvin's own cards and theirs
    assert off.get("srs/sr830").model == "Our SR830"
    assert off.get("keithley/2450").model == "Our draft"   # this lab's version of Kelvin's card: it asks until reviewed
    assert off.get("keithley/2450").status == "draft"

    card, _, _ = carddraft.save(cards, model(ours, "Our SR830, edited"), kelvin_ids(c))
    assert card.model == "Our SR830, edited"                                     # they can change their own
    with pytest.raises(ValueError, match="a person has reviewed srs/sr830"):
        carddraft.save(cards, with_status(ours, "draft"), kelvin_ids(c), by="kelvin")


# ------------------------------------------------------------------ instruments without a card

def test_a_scan_names_an_instrument_the_collection_knows(tmp_path):
    c = synced(tmp_path, catalog=[CURRENT_SOURCE])
    bus = Bus({"GPIB0::12::INSTR": Device(idn=IDN)})
    found = identify("GPIB0::12::INSTR", builtin_registry(), bus, catalog=c)
    assert found.card_id is None and found.collection == "keithley/6220" and found.kind == "current-source"
    assert found.model == "Keithley 6220 DC current source" and found.manuals == [MANUAL]   # public ones only
    assert found.suggest_name() == "isrc" and MANUAL in found.line() and "keithley/6220" in found.line()
    assert reason([found], Survey()).startswith("The Keithley 6220 DC current source answered, but Kelvin")
    plain = identify("GPIB0::12::INSTR", builtin_registry(), bus)          # the collection off: as before
    assert plain.collection == "" and plain.model == "" and "no card matches" in plain.line()
    assert all("?" in cmd for cmd in bus.devices["GPIB0::12::INSTR"].sent)   # only asked who it is


def test_setup_names_what_it_found_but_has_no_card_for(tmp_path):
    from labharness.autosetup import AutoSetup
    from test_autosetup import FakeSettings, runtime_for
    rt = runtime_for(tmp_path)
    unknown = [{"address": "GPIB0::12::INSTR", "idn": IDN, "card_id": None, "collection": "keithley/6220",
                "model": CURRENT_SOURCE["name"], "kind": "current-source", "how": "no card yet"}]
    status = AutoSetup(rt, FakeSettings(auto_cards="off"), tmp_path, scan=lambda: unknown).run()
    assert "no card for it yet (Keithley 6220 DC current source)" in status["message"]


COMMANDS = ("<p>SOURce:CURRent:AMPLitude &lt;n&gt; sets the amplitude; SOURce:CURRent:AMPLitude? reads it. "
            "OUTPut:STATe ON|OFF switches the output. SYSTem:ERRor? reads the error queue.</p>")
REFERENCE = ("<html><body><h1>Model 6220 and 6221 reference manual</h1>%s</body></html>" % (COMMANDS * 60)).encode()


def test_the_card_writer_takes_the_manual_the_collection_links():
    asked = []

    def fetch(url):
        asked.append(url)
        if url == MANUAL:
            return REFERENCE
        raise OSError("HTTP Error 404: Not Found")
    m = autocard.find_manual("KEITHLEY INSTRUMENTS INC.", "MODEL 6221", lambda system, prompt: pytest.fail("guessed"),
                             fetch, known=[MANUAL])
    assert m["url"] == MANUAL and m["score"] >= autocard.GOOD and asked == [MANUAL]   # no guessing where it is

    suggested = []

    def ask(system, prompt):
        suggested.append(prompt)
        return MANUAL, "test:model"
    moved = "https://download.tek.com/manual/moved.pdf"
    m = autocard.find_manual("KEITHLEY INSTRUMENTS INC.", "MODEL 6221", ask, fetch, known=[moved])
    assert m["url"] == MANUAL and asked[-2:] == [moved, MANUAL] and len(suggested) == 1   # a moved link: looks further


DRAFTED = """```yaml
card: 1
id: keithley/model-6221
model: Model 6221
kind: source
status: community
identify: { idn: "^SOMETHING ELSE" }
error_query: "SYSTem:ERRor?"
parameters:
  I:
    label: Current
    kind: drive
    unit: A
    set: "SOURce:CURRent:AMPLitude {value}"
    get: "SOURce:CURRent:AMPLitude?"
    limits: [-0.105, 0.105]
    safe: 0
  output:
    label: Output
    kind: drive
    type: bool
    set: "OUTPut:STATe {value}"
    get: "OUTPut:STATe?"
    safe: off
    enables: [I]
```"""


def test_a_drafted_card_is_named_as_the_collection_names_it(tmp_path):
    c = synced(tmp_path, catalog=[CURRENT_SOURCE])
    prompts = []

    def ask(system, prompt):
        prompts.append(prompt)
        assert system != autocard.SUGGEST_SYSTEM                             # the collection said where the manual is
        return DRAFTED, "test:model"
    r = autocard.make_card({"address": "GPIB0::12::INSTR", "idn": IDN}, tmp_path / "cards", ask,
                           lambda url: REFERENCE if url == MANUAL else pytest.fail(url), kelvin_ids(c), catalog=c)
    assert r["card_id"] == "keithley/6220" and r["kind"] == "current-source", r
    assert r["model"] == "Keithley 6220 DC current source" and r["manual"]["url"] == MANUAL
    assert "keithley/6220 in Kelvin's instrument collection" in prompts[0] and "4321" not in prompts[0]
    card, problems, _ = carddraft.check((tmp_path / "cards" / "keithley_6220.yaml").read_text(encoding="utf-8"))
    assert problems == [] and card.status == "draft" and card.manual_url == MANUAL
    assert card.idn_pattern == CURRENT_SOURCE["identify"]["idn"]           # the collection's, which fits the reply

    gh = GitHub()   # later the collection gets a reviewed card for the model: it replaces Kelvin's draft
    reviewed = DRAFTED.strip("`\n").replace("yaml\n", "", 1).replace("keithley/model-6221", "keithley/6220").replace(
        "kind: source", "kind: current-source")
    gh.publish(B, cards=[reviewed])
    c.sync(gh)
    assert lab_registry(tmp_path, c).get("keithley/6220").status == "community"


# ------------------------------------------------------------------ experiments

def test_experiment_plans_are_mapped_to_the_labs_instruments(tmp_path):
    c = synced(tmp_path, experiments=[EXPERIMENT], labs=[("test-bench", LAB)])
    assert [e["id"] for e in c.experiments("gate sweep")] == ["transport/gate-sweep"] and c.experiments("raman") == []
    doc = c.experiment("transport/gate-sweep")
    assert doc["written_for"] == {"gate": "keithley/2400", "lockin_xx": "srs/sr830", "lockin_xy": "srs/sr830",
                                  "ppms": "qd/multivu"}
    reg = builtin_registry()
    kind_of = lambda cid: reg.get(cid).kind if cid in reg else ""   # noqa: E731
    lab = build_virtual_lab(seed=1)
    a = adapt(doc, {n: i.card for n, i in lab.instruments.items()}, kind_of)
    assert a["names"] == {"gate": "smu", "lockin_xx": "lockin", "ppms": "tc"} and a["missing"] == ["lockin_xy"]
    plan = a["plans"][0]["plan"]
    assert plan["preconditions"] == ["tc.T < 10"] and plan["sweep"]["smu.V"]["stabilize"]["read"] == "tc.T"
    assert plan["measure"] == ["lockin.X", "lockin_xy.X", "smu.I"] and list(plan["setup"])[0] == "smu.compliance_I"
    problems = a["plans"][0]["problems"]
    assert any(p.startswith("lockin has no time_constant (virtual/lock-in has") for p in problems)
    assert any(p.startswith("tc has no T (") for p in problems)
    assert {r["role"]: r["here"] for r in a["roles"]} == {"gate": ["smu"], "lockin": ["lockin"], "cryostat": ["tc"]}
    assert load_plan(plan).name == "gate_sweep"                             # still a plan Kelvin reads

    same = {"gate": reg.get("keithley/2400"), "lockin_xx": reg.get("srs/sr830"), "lockin_xy": reg.get("srs/sr830"),
            "ppms": reg.get("qd/multivu")}
    a = adapt(doc, same, kind_of)                    # a lab with the cards the plans were written for: as written
    assert a["names"] == {n: n for n in same} and a["missing"] == [] and a["plans"][0]["problems"] == []
    assert a["plans"][0]["plan"] == load_yaml(EXPERIMENT)["plans"][0]["plan"]


def test_kelvin_finds_experiments_over_mcp(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from test_agent import call
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    rt.collection = synced(tmp_path, experiments=[EXPERIMENT], labs=[("test-bench", LAB)])
    listed, one, unknown = call(rt, ("experiment_guide", {"query": "gate"}),
                                ("experiment_guide", {"experiment": "transport/gate-sweep"}),
                                ("experiment_guide", {"experiment": "transport/nothing"}))
    assert "transport/gate-sweep: Field-effect gate sweep" in listed and "gate: source-meter or voltage-source" in listed
    assert "gate -> smu, lockin_xx -> lockin, ppms -> tc" in one and "not in this lab: lockin_xy" in one
    assert "stabilize: {read: tc.T, within: 0.1, for: 10}" in one and "preconditions: [tc.T < 10]" in one
    assert "check: lockin has no time_constant" in one and "- Sweep a small window first, there and back." in one
    assert "no experiment 'transport/nothing'" in unknown
    rt.collection = None
    assert "not switched on" in call(rt, ("experiment_guide", {}))[0]


# ------------------------------------------------------------------ the Settings switch

def test_switched_off_kelvin_stays_off_the_network(tmp_path, monkeypatch):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    from labharness import collection
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    gh = GitHub()
    gh.publish(A, cards=[WIDGET], catalog=[CURRENT_SOURCE])
    monkeypatch.setattr(collection, "http_get", gh)
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    assert settings.public()["collection"] is True                          # on unless switched off
    settings.update({"collection": "off"})
    app = create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], providers={},
                     sync_collection=True)
    h = {"X-Kelvin-Token": settings.access_key}
    with TestClient(app) as client:
        r = client.post("/api/collection", json={"op": "sync"}, headers=h)
        assert r.status_code == 409 and gh.asked == [] and rt.collection is None   # off: Kelvin doesn't go there
        assert client.get("/api/collection", headers=h).json()["on"] is False

        client.post("/api/settings", json={"collection": "on"}, headers=h)   # on: Kelvin fetches it at once,
        deadline = time.time() + 15                                          # and a card for a new model is usable
        while not any(c["id"] == "acme/widget" for c in client.get("/api/lab", headers=h).json()["cards"]):
            assert time.time() < deadline, "the collection was not fetched"
            time.sleep(0.05)
        status = client.get("/api/collection", headers=h).json()
        assert status["on"] and status["commit"] == A and status["cards"] == 1 and status["instruments"] == 2
        assert rt.collection is not None and gh.asked[0] == collection.API % collection.REPO
        r = client.post("/api/cards", json={"yaml": with_status(WIDGET, "draft")}, headers=h)
        assert r.status_code == 400 and "already has a card acme/widget" in r.json()["error"]

        client.post("/api/settings", json={"collection": "off"}, headers=h)
        assert rt.collection is None and client.get("/api/settings", headers=h).json()["settings"]["collection"] is False
        asked = len(gh.asked)
        assert client.post("/api/collection", json={"op": "sync"}, headers=h).status_code == 409
        assert len(gh.asked) == asked
