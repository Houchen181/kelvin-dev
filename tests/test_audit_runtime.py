"""The 0.3.52 audit's findings on the runtime's tools (P1, R1-R10, R16-R18), kept fixed: each test is the audit's
repro (audit-repros/portal, runtime-a, runtime-b) cut to what it showed."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from labharness.agent.approval import Approver
from labharness.agent.runtime import LabRuntime
from simlab import build_virtual_lab

WEB_CARD = """
card: 1
id: acme/femto
model: ACME Femto laser (REST)
kind: laser
status: draft
identify: { query: "GET /v1/About#model", idn: "^FEMTO" }
parameters:
  power:
    label: Output power
    kind: measure
    unit: W
    get: "GET /v1/Basic/ActualOutputPower"
"""


class Recorder(Approver):
    """Every request that reaches a person, and every one Kelvin approved itself; says `answer` to the first."""
    def __init__(self, answer=False):
        self.asked, self.informed, self.answer = [], [], answer

    def approve(self, request):
        self.asked.append(request.title)
        return self.answer

    def inform(self, request, reasons):
        self.informed.append(request.title)


@pytest.fixture
def web_server():
    """A web server on this computer that keeps what it was sent: the path and the Authorization header."""
    seen = []

    class Catch(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            body = json.dumps({"model": "FEMTO-2"} if "About" in self.path else 1.5).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Catch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def runtime(tmp_path):
    made = []

    def make(approver):
        rt = LabRuntime(build_virtual_lab(seed=1), approver, tmp_path / "data")
        made.append(rt)
        return rt
    yield make
    for rt in made:
        rt.close_instruments(5)


def test_p1_an_address_that_names_an_environment_variable_is_a_persons_to_write(runtime, web_server, monkeypatch):
    """try_card sent any environment variable's value (an API key) to any web address the model named, as a bearer
    token, approved by Kelvin itself."""
    port, seen = web_server
    monkeypatch.setenv("KELVIN_AUDIT_SECRET", "sk-audit-0000-not-a-real-key")
    approver = Recorder(answer=True)
    rt = runtime(approver)
    address = "http://127.0.0.1:%d/?auth_env=KELVIN_AUDIT_SECRET" % port
    said = rt.try_card(WEB_CARD, address)
    assert said.startswith("not tried") and "KELVIN_AUDIT_SECRET" in said and "only a person" in said, said
    for spelling in (address.replace("auth_env", "AUTH_ENV"), "http://127.0.0.1:%d/?x=1&token_env=PATH" % port):
        assert rt.try_card(WEB_CARD, spelling).startswith("not tried")
    rt.home = rt.data_dir.parent   # the app's: it keeps a lab config
    said = rt.add_instrument(address, card="acme/femto")
    assert said.startswith("not added") and "KELVIN_AUDIT_SECRET" in said, said
    assert seen == [] and approver.asked == [] and approver.informed == []


def test_p1_a_web_address_the_scan_did_not_find_asks_a_person(runtime, web_server):
    port, seen = web_server
    approver = Recorder(answer=False)
    rt = runtime(approver)
    said = rt.try_card(WEB_CARD, "http://127.0.0.1:%d/" % port)
    assert approver.asked == ["Try a card on http://127.0.0.1:%d/?" % port] and approver.informed == [], said
    assert seen == [], seen
    rt.gate.auto = True   # a person's "approve what's safe" isn't a yes to reach the network
    rt.try_card(WEB_CARD, "http://127.0.0.1:%d/" % port)
    assert len(approver.asked) == 2 and approver.informed == [] and seen == []


# ------------------------------------------------------------------ R1: a person's entry leaves only with a person's yes

LAB_C4 = """lab: Transport lab
instruments:
  lockin:
    card: srs/sr860
    address: "GPIB0::8::INSTR"
  smu:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
    rates: { V: 0.2 }
    guards: { V: [{ read: I, max_abs: 1.0e-9 }] }
    auto: never
"""


def test_r1_removing_an_instrument_a_person_set_up_asks_them(tmp_path):
    """remove_instrument and add_instrument, both approved by Kelvin itself, put a fresh entry in place of a
    person's: its +-5 V, ramp rate, leakage guard and auto: never were gone, and 20 V was an ordinary request."""
    from labharness.adapters import set_resource_manager
    from labharness.homelab import build_lab, lab_registry
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB0::8::INSTR": Device("srs/sr860", name="sr860"),
                              "GPIB0::24::INSTR": Device("keithley/2400")}))
    try:
        (tmp_path / "lab.yaml").write_text(LAB_C4, encoding="utf-8")
        lab = build_lab(tmp_path / "lab.yaml", registry=lab_registry(tmp_path), partial=True)
        approver = Recorder(answer=False)
        rt = LabRuntime(lab, approver, tmp_path / "data")
        try:
            rt.home, rt.cards_dir, rt.scan_network = tmp_path, tmp_path / "cards", False
            rt.lab_info = {"kind": "file", "path": str(tmp_path / "lab.yaml"), "error": None, "has_file": True}
            said = rt.remove_instrument("smu")
            assert said.startswith("declined"), said
            assert approver.asked == ["Remove smu from the lab?"] and approver.informed == []
            text = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
            assert "limits: { V: [-5, 5] }" in text and "auto: never" in text
            assert rt.lab.instruments["smu"].limits("V") == (-5.0, 5.0)
        finally:
            rt.close_instruments(5)
    finally:
        set_resource_manager(None)


# ------------------------------------------------------------------ R3, R6: signing in after a restart

def test_r3_a_mounted_samples_watch_comes_back_at_sign_in(tmp_path):
    """The watches were restored before the person's mounted sample was read: the one made for D5 was dropped as
    "no longer mounted", D5 still mounted, and taken out of their watches.json."""
    from labharness import ScriptedApprover
    nobody, ada = tmp_path / "nobody", tmp_path / "ada"
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), nobody, None)
    rt.use_user("ada", ada)
    rt.mount(None, {"id": "d5", "name": "D5"}, by="human")
    assert rt.add_watch("tc.T_sample", above=4, note="D5 stays cold", source="agent")["device"] == "D5"
    rt2 = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), nobody, None)   # a restart
    rt2.use_user("ada", ada)
    assert rt2.mounted.get("device_name") == "D5"
    assert rt2.restored["dropped"] == [] and "tc.T_sample above 4 K" in rt2.restored["back"]
    kept = json.loads((ada / "watches.json").read_text(encoding="utf-8"))["labs"][rt2.lab.config.name]
    assert [(v["read"], v["above"], v["device"]) for v in kept] == [("tc.T_sample", 4, "D5")]


def test_r6_a_persons_alarms_come_back_at_sign_in_and_are_never_written_over(tmp_path):
    """After a restart (nobody signed in yet), signing in wrote the monitor's empty list over the person's
    alarms.json, unread: the night's alarm nobody had said Got it to was gone, unseen."""
    from labharness import ScriptedApprover
    from labharness.scanqueue import _written
    nobody, ada = tmp_path / "nobody", tmp_path / "ada"
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), nobody, None)
    rt.use_user("ada", ada)
    rt.add_watch("tc.T_sample", above=5, note="must stay cold", lab_wide=True)
    rt.lab.clock.sleep(1.0)
    rt.lab.read("tc", "T_sample")
    rt.lab.instruments["tc"].backend.state["T_sample"] = 6.0   # the sample warms in the night
    rt.monitor.tick()
    _written(ada / "alarms.json", 5.0)
    rt2 = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), nobody, None)   # a restart
    rt2._incident_check()   # the restart's card goes up before anyone signs in
    rt2.use_user("ada", ada)
    shown = rt2.alarms_view()
    assert [a["text"] for a in shown] == ["tc.T_sample = 6 K is above 5 K (on when Kelvin stopped)"], shown
    assert not shown[0]["acknowledged"]   # theirs to say Got it to: no card said it
    _written(ada / "alarms.json", 5.0)
    kept = json.loads((ada / "alarms.json").read_text(encoding="utf-8"))["alarms"]
    assert [a["text"] for a in kept] == ["tc.T_sample = 6 K is above 5 K (on when Kelvin stopped)"]
    rt2.use_user("ada", ada)   # in again: nothing twice
    assert len(rt2.alarms_view()) == 1


# ------------------------------------------------------------------ R9, R2: the sample in the setup is the lab's

GATE = {"gates": [{"drive": "smu.V", "dielectric": "hBN", "thickness_nm": 20, "leak": "smu.I"}]}


def _app_like(folder, kelvin, approver=None):
    """A runtime as the app makes it: Kelvin's own folder known (it keeps the sample in the setup for everyone)."""
    from labharness import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(seed=1), approver or ScriptedApprover([True] * 50), folder)
    rt.lab_memory_dir = kelvin
    rt.sample_in_setup()
    return rt


def test_r9_the_mounted_samples_protection_holds_after_the_data_folder_changes_and_a_restart(tmp_path):
    from labharness import protection
    rt = _app_like(tmp_path / "data", tmp_path / "Kelvin")
    rt.user = "ada"
    rt.mount(None, {"id": "d4", "name": "D4", "protection": protection.normalize(GATE, rt.lab)})
    rt.set_data_dir(tmp_path / "new", move=True)
    rt.close_instruments(5)
    rt2 = _app_like(tmp_path / "new", tmp_path / "Kelvin")   # the restart: nobody signed in yet
    try:
        assert rt2.lab.instruments["smu"].limits("V") == (-6.0, 6.0)
        r = rt2.gate.set("smu", "V", 12)
        assert not r.ok and "outside device" in r.message and "[-6, 6] V" in r.message, r.message
        rt2.use_user("ada", tmp_path / "new")   # theirs, in the folder they chose
        assert rt2.mounted.get("device_name") == "D4" and rt2.lab.instruments["smu"].limits("V") == (-6.0, 6.0)
        assert json.loads((tmp_path / "new" / "mounted.json").read_text(encoding="utf-8"))["device_name"] == "D4"
    finally:
        rt2.close_instruments(5)


def test_r9_another_persons_device_stays_protected_while_someone_else_signs_in(tmp_path):
    from labharness import protection
    rt = _app_like(tmp_path / "nobody", tmp_path / "Kelvin")
    try:
        rt.use_user("ada", tmp_path / "ada")
        rt.mount(None, {"id": "d4", "name": "D4", "protection": protection.normalize(GATE, rt.lab)})
        rt.use_user("ben", tmp_path / "ben")
        smu = rt.lab.instruments["smu"]
        assert rt.mounted == {} and smu.limits("V") == (-6.0, 6.0)   # his runs are his; her device is protected
        assert rt.mounted_view()["device_name"] == "ada's sample" and rt.mounted_view()["another_person"]
        r = rt.gate.set("smu", "V", 12)
        assert not r.ok and "[-6, 6] V" in r.message, r.message
        rt.use_user(None, tmp_path / "nobody")
        assert smu.limits("V") == (-6.0, 6.0)
        rt.use_user("ben", tmp_path / "ben")
        rt.mount(None, {"id": "d9", "name": "D9"})   # he takes hers out and puts his in
        assert smu.limits("V") == (-15.0, 15.0)
        rt.use_user("ada", tmp_path / "ada")   # hers is out of the setup: nothing of hers is mounted, his holds
        assert rt.mounted == {} and not (tmp_path / "ada" / "mounted.json").exists()
        assert rt.mounted_view()["device_name"] == "ben's sample"
    finally:
        rt.close_instruments(5)


LAB_C3 = """lab: Transport lab
instruments:
  smu:
    card: keithley/2400
    limits: { V: [-20, 20] }
  tc:
    card: lakeshore/336
"""


def test_r2_a_rename_follows_into_another_persons_mounted_sample_and_watches(tmp_path):
    """Ada renamed smu to smu_gate: Ben's mounted D7 (+-3 V on smu.V) and his watches kept the old name in his folder,
    and at his next sign-in the protection held on nothing: 5 V on the gate was an ordinary request."""
    from labharness import ScriptedApprover
    from labharness.discovery import rename_in_config
    from labharness.homelab import build_lab, lab_registry
    for kelvin in (True, False):   # the lab's own record of the sample in the setup, and a script's (none)
        home = tmp_path / ("Kelvin" if kelvin else "script")
        home.mkdir()
        nobody, ada, ben = home / "nobody", home / "ada", home / "ben"
        registry = lab_registry(home)
        rt = LabRuntime(build_lab(LAB_C3, None, 0, registry, lambda lab: [], True, True), ScriptedApprover([]),
                        nobody, None)
        try:
            rt.home = home if kelvin else None
            rt.lab_memory_dir = home if kelvin else None
            rt.use_user("ben", ben)
            rt.mount(None, {"id": "d7", "name": "D7", "protection": {"limits": {"smu.V": [-3, 3]}, "gates": []}},
                     by="human")
            rt.add_watch("smu.I", above=1e-6, lab_wide=True)
            rt.use_user("ada", ada)
            lab = build_lab(rename_in_config(LAB_C3, "smu", "smu_gate"), None, 0, registry, lambda lab: [], True,
                            True)
            rt.switch_lab(lab, None, {"kind": "file", "path": "", "error": None, "has_file": True}, "human (portal)",
                          None, {"smu_gate": "smu"})
            rt.instrument_renamed("smu", "smu_gate", "human (portal)")
            assert rt.renamed_in_folder(ben, "smu", "smu_gate")   # the app: each person's data folder
            rt.use_user("ben", ben)
            assert rt.mounted["protection"]["limits"] == {"smu_gate.V": [-3, 3]} and rt.protection_missing == []
            assert rt.lab.instruments["smu_gate"].limits("V") == (-3.0, 3.0)
            kept = json.loads((ben / "watches.json").read_text(encoding="utf-8"))["labs"]["Transport lab"]
            assert [v["read"] for v in kept] == ["smu_gate.I"]
        finally:
            rt.close_instruments(5)


def test_r2_a_protection_that_lost_what_it_held_on_holds_every_drive(tmp_path):
    """What a rename can't reach (a folder on a share that was away, the lab config edited by hand): the protection
    names an instrument the lab no longer has, so nothing moves away from safe until a person sees to it."""
    from labharness import ScriptedApprover
    from labharness.discovery import rename_in_config
    from labharness.homelab import build_lab, lab_registry
    registry = lab_registry(tmp_path)
    rt = LabRuntime(build_lab(LAB_C3, None, 0, registry, lambda lab: [], True, True), ScriptedApprover([]),
                    tmp_path / "nobody", None)
    try:
        rt.use_user("ben", tmp_path / "ben")
        rt.mount(None, {"id": "d7", "name": "D7", "protection": {"limits": {"smu.V": [-3, 3]}, "gates": []}})
        rt.use_user("ada", tmp_path / "ada")
        lab = build_lab(rename_in_config(LAB_C3, "smu", "smu_gate"), None, 0, registry, lambda lab: [], True, True)
        rt.switch_lab(lab, None, {"kind": "file", "path": "", "error": None, "has_file": True}, "human (portal)", None,
                      {"smu_gate": "smu"})
        rt.use_user("ben", tmp_path / "ben")   # his folder never heard of the rename
        assert rt.protection_missing == ["smu.V"]
        r = rt.gate.set("smu_gate", "V", 5)
        assert not r.ok and "no longer has" in r.message, r.message
        assert any("no longer has" in line for line in rt._protection_lines())
    finally:
        rt.close_instruments(5)


def test_r8_a_rename_reaches_an_expressions_every_name():
    from labharness.discovery import renamed_keys, renamed_plan_text
    plan = "derive: { R: smu.V/smu.I, d: tc.T_sample-smu.I, e: (smu.V)-smu.I }\nsweep: { smu.V: [0, 1] }\n"
    assert renamed_plan_text(plan, "smu", "smu_gate") == (
        "derive: { R: smu_gate.V/smu_gate.I, d: tc.T_sample-smu_gate.I, e: (smu_gate.V)-smu_gate.I }\n"
        "sweep: { smu_gate.V: [0, 1] }\n")
    for address in ("TCPIP0::lab-smu.local::INSTR", "sim://smu.x", "http://host/smu.json/x", "GPIB0::smu.V"):
        assert renamed_keys(address, "smu", "smu_gate") == address


# ------------------------------------------------------------------ R10: inspect_serial asks who it is, nothing else

def test_r10_inspect_serial_sends_one_identity_query_and_records_it(tmp_path, monkeypatch):
    from labharness import ScriptedApprover, probe
    from labharness.agent.runtime import tail_events
    from test_probe import SerialDevice
    device = SerialDevice(9600, "none", 8, b"KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C32\r\n")
    monkeypatch.setattr(probe, "_open_serial", device.opener)
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    try:
        for query in ("SOUR:VOLT 20;OUTP ON;*IDN?", "++addr 24\nOUTP ON\n*IDN?", "OUTP ON?", "*IDN?;*RST"):
            assert "only an identity query" in rt.inspect_serial("COM99", query, 9600), query
        assert device.heard == []
        assert "COM99 answers *IDN? at 9600 baud" in rt.inspect_serial("COM99", "*IDN?", 9600)
        assert {data for *_, data in device.heard} == {b"*IDN?\r\n"}
        said = [e for e in tail_events(rt.events_path, 50) if e.get("type") == "inspect_serial"]
        assert [(e["port"], e["query"]) for e in said] == [("COM99", "*IDN?")]
    finally:
        rt.close_instruments(5)


def test_r10_inspect_serial_is_not_marked_read_only():
    pytest.importorskip("mcp")
    import inspect

    from labharness.agent import server
    text = inspect.getsource(server)
    at = text.index("def inspect_serial(")
    assert "annotations=ACT" in text[text.rindex("@server.tool", 0, at):at]


# ------------------------------------------------------------------ R16: kelvin-mcp's runtime has no app home

def test_r16_procedures_and_search_work_without_the_apps_home(tmp_path):
    from labharness import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "agents")
    try:
        rt.lab_memory_dir = tmp_path / "Kelvin"   # as kelvin-mcp sets it; no home
        assert rt.home is None
        assert "no procedures yet" in rt.procedures()
        sop = "procedure: cooldown\nsteps:\n  - name: load\n    by: hand\n    do: load the chip\n"
        assert rt.save_procedure(sop).startswith("kept procedure cooldown v1")
        assert (tmp_path / "Kelvin" / "procedures").is_dir()
        assert "recorded step" in rt.record_step("loaded the chip", procedure="cooldown", step="load")
        assert isinstance(rt.search_records("gate"), str)
    finally:
        rt.close_instruments(5)


# ------------------------------------------------------------------ R4, R5, R7: the web and the manuals

def test_r4_read_web_page_never_reaches_this_computer_or_the_lab_network(runtime, web_server):
    """The model's read_web_page made a GET to a relay's web switch on 127.0.0.1 (turning it on) and got its page
    back, with nobody asked."""
    port, seen = web_server
    rt = runtime(Recorder())
    for url in ("http://127.0.0.1:%d/relay/0?turn=on" % port, "http://localhost:%d/relay/0?turn=on" % port,
                "http://192.168.1.10/", "http://169.254.10.10/", "http://[::1]:%d/" % port):
        said = rt.read_web_page(url)
        assert said.startswith("could not read") and "internet only" in said, (url, said)
    assert "this computer" in rt.read_web_page("http://127.0.0.1:%d/" % port)
    assert seen == []


def test_r4_a_redirect_to_this_computer_is_refused_too(monkeypatch):
    """A public page's redirect sends the request on: each address it goes to is checked."""
    from labharness import profiles
    hits = []

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://localhost:%d/secret" % self.server.server_address[1])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real = profiles.public_host
    monkeypatch.setattr(profiles, "public_host",   # 127.0.0.1 stands in for a public site here; nothing else does
                        lambda host, port=443: None if host == "127.0.0.1" else real(host, port))
    try:
        with pytest.raises(ValueError, match="this computer"):
            profiles.http_get("http://127.0.0.1:%d/start" % server.server_address[1])
        assert hits == ["/start"]   # the redirect's address was never asked
    finally:
        server.shutdown()
        server.server_close()


def test_r5_save_manual_reads_only_what_the_file_tools_may(tmp_path):
    """save_manual kept an HTML file from another person's chats folder (which read_examples refuses) as a manual,
    and search_manual then handed its text back."""
    from labharness import ScriptedApprover
    home = tmp_path / "Kelvin"
    notes = home / "accounts" / "ben" / "chats" / "notes.html"
    notes.parent.mkdir(parents=True)
    notes.write_text("<html><body>%s</body></html>" % ("Ben's unpublished result: Tc = 41.7 K. " * 100),
                     encoding="utf-8")
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), home / "lab")
    try:
        rt.home = home
        said = rt.save_manual(str(notes))
        assert said.startswith("not kept") and "Kelvin's own folder" in said, said
        assert not (home / "manuals").exists() or not list((home / "manuals").iterdir())
    finally:
        rt.close_instruments(5)


def test_r7_a_page_with_little_text_never_takes_the_place_of_the_manual_kept(tmp_path):
    """The old manual was deleted before the new one was indexed; the new one, a sign-in page, was then deleted too:
    the instrument was left with no manual."""
    from types import SimpleNamespace

    from labharness import profiles
    base = profiles.folder(tmp_path, "smu")
    base.mkdir(parents=True)
    (base / "manual.pdf").write_bytes(b"%PDF-1.4 the maker's manual a person put here")
    with pytest.raises(ValueError, match="almost no text"):
        profiles.store_manual(tmp_path, "smu", SimpleNamespace(manual="", model="2400"),
                              b"<html><body>Please sign in</body></html>", "https://maker.example/manual")
    assert sorted(p.name for p in base.iterdir()) == ["manual.pdf"]
    assert (base / "manual.pdf").read_bytes().startswith(b"%PDF-1.4 the maker's manual")


# ------------------------------------------------------------------ R17, R18: who wrote it, and where a run goes

def test_r17_an_agent_over_mcp_writes_the_lab_records_as_an_ai(tmp_path):
    """An agent over MCP has no model of its own: what it wrote was kept as by="human", with no AI named, and the ask
    after text from outside the lab was skipped for it."""
    from labharness import ScriptedApprover
    from labharness.agent.runtime import tail_events
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "agents")
    try:
        with rt.viewing(rt.viewer_of(None)):   # kelvin-mcp's other clients: no portal chat's token
            assert rt._actor_now() == {"model": rt.MCP_AGENT, "chat": None}
            assert "recorded" in rt.record_step("loaded the chip")
        step = [e for e in tail_events(rt.events_path, 20) if e.get("type") == "step"][-1]
        assert (step.get("ai") or {}).get("model") == rt.MCP_AGENT and step.get("by") != "human", step
    finally:
        rt.close_instruments(5)


def test_r18_an_incognito_chats_run_goes_under_no_sample_general_or_not(tmp_path):
    from labharness import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    try:
        rt.mount(None, {"id": "g1", "name": "G1", "protection": {}})   # a sample of the General workspace
        rt.attribution = {"chat": "chat-I", "incognito": True}
        meta = rt._attribute(None)
        assert meta["project"] is None and meta["device"] is None and meta["device_name"] is None, meta
        assert meta["incognito"] and "incognito" in meta["_apart"]
        rt.attribution = {"chat": "chat-G"}   # a General chat's run goes under the General sample, as before
        assert rt._attribute(None)["device_name"] == "G1"
    finally:
        rt.close_instruments(5)
