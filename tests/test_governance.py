"""The lab's governance (field tests: a student's account raised the PI's ±8 V limit to ±150 V and deleted
interlocks, config changes were undiffed in the log, and the PI heard of none of it): a versioned lab config with
diffs, rollback and an audit view; the owner told of safety changes; roles (owner, operator, trainee) on top of
the owner's PIN. Without roles, Kelvin is as before."""
import json
import threading
import time

import pytest

from labharness import build_virtual_lab
from labharness.accounts import Accounts, LabRoles, OwnerPin, RoleRefused
from labharness.configversions import (OUTSIDE, ConfigVersions, card_changes, lab_changes, review_changes)
from labharness.gate import ApprovalRequest
from labharness.notify import Notifier, email_from

LAB = '''# the PI's bench
lab: Bench
instruments:
  smu:
    card: keithley/2400   # the gate
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
  lockin:
    card: srs/sr830
    address: "GPIB0::8::INSTR"
'''

CARD = '''card: 1
id: mylab/heater
model: Bench heater
vendor: Us
kind: source
status: draft
parameters:
  P:
    kind: drive
    unit: W
    set: "POW {value}"
    get: "POW?"
    limits: [0, 2]
    safe: 0
raw:
  blocked: ["CAL"]
'''


def changed(found):
    return {c["text"]: c["safety"] for c in found}


# ---------------------------------------------------------------------------------- the changes, in words

def test_the_changes_say_what_widens_kelvins_reach():
    wide = (LAB.replace("[-5, 5]", "[-150, 150]")
            + "interlocks:\n  - {drive: smu.V, require: 'lockin.X < 1'}\n")
    found = changed(lab_changes(LAB, wide))
    assert found == {"smu.V: limits widened, [-5, 5] → [-150, 150]": True,
                     "interlock added: smu.V only when lockin.X < 1": False}
    back = changed(lab_changes(wide, LAB))   # narrowing and taking an interlock out
    assert back == {"smu.V: limits narrowed, [-150, 150] → [-5, 5]": False,
                    "interlock taken out: smu.V only when lockin.X < 1": True}
    more = LAB.replace("    limits: { V: [-5, 5] }\n", "    limits: { V: [-5, 5] }\n    added_by: kelvin\n"
                       "    guards: { V: [{read: I, max_abs: 1.0e-6}] }\n    auto: never\n") + (
        "watch:\n  - {read: lockin.X, above: 1, then: {smu.V: 0}}\nauto_fraction: 0.25\n")
    loose = (more.replace("1.0e-6", "1.0e-3").replace("    added_by: kelvin\n", "").replace("    auto: never\n", "")
             .replace("above: 1,", "above: 5,").replace("0.25", "0.5")
             + "safe_actions: {smu.V: hold}\n")
    found = changed(lab_changes(more, loose))
    assert all(found.values()) and len(found) == 6, found          # each loosens something
    assert "smu: limits marked reviewed (Kelvin's mark taken out: its output changes stop asking each time)" in found
    assert "smu: no longer 'auto: never' (auto mode may cover it)" in found
    assert any(t.startswith("watch changed: lockin.X above 1") for t in found)
    assert "auto mode covers 25% → 50% of the way from safe to each limit" in found
    assert "after a stop smu.V holds where it is (it went to safe)" in found
    assert any(t.startswith("guard on smu.V reading smu.I") for t in found)
    found = changed(lab_changes(LAB, LAB.replace("[-5, 5] }", "[-5, 5] }   # tighter one day")))
    assert found == {"only comments or layout changed": False}
    found = changed(lab_changes(LAB, LAB + "  magnet:\n    card: ami/430\n    address: TCPIP0::1::7180::SOCKET\n"))
    assert found == {"magnet added (card ami/430 at TCPIP0::1::7180::SOCKET; its limits count as reviewed)": True}
    kelvins = LAB + "  magnet:\n    card: ami/430\n    address: X\n    added_by: kelvin\n"
    assert not any(c["safety"] for c in lab_changes(LAB, kelvins))   # Kelvin's mark: every output change asks
    assert changed(lab_changes(LAB.replace("    limits: { V: [-5, 5] }\n", ""), LAB)) == {
        "smu.V: limited to [-5, 5]": False}
    assert changed(lab_changes(LAB, LAB.replace("    limits: { V: [-5, 5] }\n", ""))) == {
        "smu.V: limit [-5, 5] taken out (its card's range applies)": True}


def test_a_cards_limits_safe_values_blocks_and_review_are_kept_apart():
    wider = CARD.replace("[0, 2]", "[0, 20]").replace("safe: 0", "safe: 1").replace('["CAL"]', "[]")
    found = changed(card_changes(CARD, wider))
    assert found == {"mylab/heater P: limits widened, [0, 2] → [0, 20]": True,
                     "mylab/heater P: safe value 0 → 1": True, "mylab/heater: no longer blocks CAL": True}
    assert changed(card_changes(CARD, CARD.replace("[0, 2]", "[0, 1]"))) == {
        "mylab/heater P: limits narrowed, [0, 2] → [0, 1]": False}
    reviewed = CARD.replace("status: draft", "status: lab-verified")
    assert card_changes(CARD, reviewed)[0]["safety"]
    other = reviewed.replace('set: "POW {value}"', 'set: "CURR {value}"')   # a reviewed card's outputs don't ask
    assert changed(card_changes(reviewed, other)) == {"card mylab/heater: its commands or settings changed while it "
                                                      "is marked lab-verified (whoever saves it vouches for it again)":
                                                      True}
    assert not any(c["safety"] for c in card_changes(CARD, CARD.replace('"POW {value}"', '"CURR {value}"')))
    assert not any(c["safety"] for c in card_changes(reviewed, reviewed.replace("[0, 2]", "[0, 1]")
                                                     .replace("Bench heater", "Bench heater, left")))
    assert not any(c["safety"] for c in card_changes(None, CARD))      # a new draft: each change on it asks
    assert card_changes(None, reviewed)[0]["safety"]                    # a new card, marked reviewed
    assert card_changes(reviewed, None)[0]["safety"]                    # Kelvin's own of that id may come back
    assert not card_changes(CARD, None)[0]["safety"]
    assert changed(review_changes("{}", json.dumps({"keithley/2400": {"status": "lab-verified"}}))) == {
        "Kelvin's card keithley/2400 marked reviewed for this lab (its outputs stop asking each time)": True}


# ---------------------------------------------------------------------------------- the history

def test_every_version_is_kept_with_who_when_and_why_and_can_be_put_back(tmp_path):
    v = ConfigVersions(tmp_path)
    assert v.notice() == [] and v.history() == []                      # nothing to keep yet
    (tmp_path / "lab.yaml").write_text(LAB, encoding="utf-8")
    (first,) = v.notice()                                              # as Kelvin found it: no change, no alarm
    assert first["kind"] == "first" and first["changes"] == [] and not first["safety"] and first["by"] == "Kelvin"
    assert v.notice() == []
    (tmp_path / "lab.yaml").write_text(LAB.replace("[-5, 5]", "[-150, 150]"), encoding="utf-8")
    (wide,) = v.sync("human (portal)", "Ben", "trainee", "  a  wider   sweep ")
    assert (wide["by"], wide["account"], wide["role"], wide["why"]) == ("human (portal)", "Ben", "trainee",
                                                                        "a wider sweep")
    assert wide["safety"] and wide["n"] == first["n"] + 1 and wide["at"][:4].isdigit()
    d = v.diff(wide["n"])
    assert d["against"] == first["n"] and "-    limits: { V: [-5, 5] }" in d["diff"] and "+    limits: { V: [-150, 150] }" in d["diff"]
    assert v.restore(first["n"]) == ("lab.yaml", LAB)
    (tmp_path / "lab.yaml").write_text(LAB + "# by hand\n", encoding="utf-8")   # an edit outside Kelvin
    (hand,) = v.notice()
    assert hand["by"] == OUTSIDE and hand["kind"] == "outside" and hand["changes"][0]["text"].startswith(
        "smu.V: limits narrowed")
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "mylab_heater.yaml").write_text(CARD, encoding="utf-8")
    (card,) = v.sync("human (portal)", "Ada", "owner", "", "card")
    assert card["subject"] == "cards/mylab_heater.yaml" and card["title"] == "card mylab/heater"
    (cards / "mylab_heater.yaml").unlink()
    (gone,) = v.sync("human (portal)")
    assert gone["sha"] is None and v.restore(card["n"])[1] == CARD and v.restore(gone["n"])[1] is None
    v.event("auto mode on", "human (portal)", "Ada", "owner")
    assert [e["n"] for e in v.history()] == [6, 5, 4, 3, 2, 1]          # newest first, one numbering
    assert [e["n"] for e in v.history(safety_only=True)] == [6, 2]
    assert [e["n"] for e in v.history(subject="cards")] == [5, 4]
    assert [e["n"] for e in v.history(subject="events")] == [6]
    assert [e["n"] for e in v.history(who="ben")] == [2]
    with pytest.raises(KeyError):
        v.restore(6)                                                    # a safety change has no file to put back
    assert len(list((v.folder / "texts").iterdir())) == 4              # each text once


# ---------------------------------------------------------------------------------- roles

def accounts_with_roles(home):
    acc = Accounts(home)
    acc.create("Ada", "ada-pass")
    acc.create("Ben", "ben-pass")
    acc.create("Cy", "cy-pass")
    acc.create("Dee", "")
    pin = OwnerPin(home)
    roles = LabRoles(home)
    return acc, pin, roles


def test_roles_build_on_the_pin_and_only_the_owner_changes_them(tmp_path):
    acc, pin, roles = accounts_with_roles(tmp_path)
    assert not roles.on and roles.role("Cy") is None and roles.role(None) is None   # no roles: everyone equal
    with pytest.raises(ValueError, match="PIN first"):
        roles.set({"Ada": "owner"}, pin, None, "Ada", acc)
    pin.set("2468")
    with pytest.raises(PermissionError, match="needs the lab owner's PIN"):
        roles.set({"Ada": "owner"}, pin, None, "Ada", acc)
    with pytest.raises(ValueError, match="owner's role"):
        roles.set({"Ben": "operator"}, pin, "2468", "Ada", acc)
    with pytest.raises(ValueError, match="no account"):
        roles.set({"Ada": "owner", "Zed": "operator"}, pin, "2468", "Ada", acc)
    assert roles.set({"ada": "owner", "Ben": "operator", "Dee": "operator", "Cy": ""}, pin, "2468", "Ada", acc) == {
        "Ada": "owner", "Ben": "operator", "Dee": "operator"}
    assert (roles.role("ADA"), roles.role("Ben"), roles.role("Cy"), roles.role(None)) == (
        "owner", "operator", "trainee", "trainee")                     # no role given, or nobody: a trainee
    assert roles.approvers() == ["Ada", "Ben", "Dee"]
    with pytest.raises(PermissionError, match="only the lab's owner"):
        roles.set({}, pin, "2468", "Ben", acc)                          # an operator with the PIN: still no
    assert roles.set({}, pin, "2468", "Ada", acc) == {} and not roles.on
    roles.path.write_text("{not json", encoding="utf-8")                # a broken file never lifts the roles
    assert roles.on and roles.role("Ada") == "trainee"


def test_a_trainees_yes_is_confirmed_by_an_operators_password_or_the_pin(tmp_path, monkeypatch):
    acc, pin, roles = accounts_with_roles(tmp_path)
    pin.set("2468")
    roles.set({"Ada": "owner", "Ben": "operator", "Dee": "operator"}, pin, "2468", "Ada", acc)
    assert roles.vouch(acc, pin, "ben", "ben-pass") == {"account": "Ben", "role": "operator"}
    assert roles.vouch(acc, pin, pin="2468") == {"pin": True}
    with pytest.raises(PermissionError, match="needs an operator"):
        roles.vouch(acc, pin)
    with pytest.raises(PermissionError, match="not an operator"):
        roles.vouch(acc, pin, "Cy", "cy-pass")                          # another trainee
    with pytest.raises(PermissionError, match="no password"):
        roles.vouch(acc, pin, "Dee", "")                                # anyone could give her name
    with pytest.raises(PermissionError, match="not the lab owner's PIN"):
        roles.vouch(acc, pin, pin="0000")
    for _ in range(LabRoles.TRIES - 1):
        with pytest.raises(PermissionError, match="not Ben's password"):
            roles.vouch(acc, pin, "Ben", "guess")
    with pytest.raises(PermissionError, match="too many wrong"):
        roles.vouch(acc, pin, "Ben", "ben-pass")                        # locked for a while, even the right one
    monkeypatch.setattr(LabRoles, "LOCKED_S", 0.0)
    assert roles.vouch(acc, pin, "Ben", "ben-pass")["account"] == "Ben"
    nopin = OwnerPin(tmp_path / "elsewhere")                            # no PIN set: check() would take anything
    with pytest.raises(PermissionError, match="no lab owner's PIN"):
        roles.vouch(acc, nopin, pin="1234")


# ---------------------------------------------------------------------------------- the owner told

def test_safety_changes_reach_the_owner_and_the_person_only_if_they_ticked_them():
    posts = []
    n = Notifier(post=lambda url, data, headers: posts.append((url, data)), mail=lambda *a: None)
    n.configure("https://hooks.example/mine", ["alarm"])
    n.tell_owner("Bench: smu.V: limits widened", "By Ben")
    assert [n["kind"] for n in n.notices] == ["safety"] and posts == []  # nowhere set for the owner: the list only
    n.configure_owner("https://ntfy.sh/owner-topic", None)
    n.tell_owner("Bench: interlock taken out", "By Cy")
    deadline = time.time() + 5
    while not posts and time.time() < deadline:
        time.sleep(0.02)
    assert [u for u, _ in posts] == ["https://ntfy.sh/owner-topic"]    # not the person's: they didn't tick it
    posts.clear()
    n.configure("https://ntfy.sh/owner-topic", ["safety"])             # the same place: once
    n.tell_owner("Bench: auto mode on", "")
    while not posts and time.time() < deadline + 5:
        time.sleep(0.02)
    time.sleep(0.1)
    assert [u for u, _ in posts] == ["https://ntfy.sh/owner-topic"]
    with pytest.raises(ValueError):
        n.configure_owner("ftp://nope", None)
    assert email_from("smtp.uni.edu", "pi@uni.edu").to == ["pi@uni.edu"]


# ---------------------------------------------------------------------------------- in the runtime

pytest.importorskip("mcp")
from labharness.agent.approval import ApprovalServer, WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402


def runtime_with_roles(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), tmp_path / "data")
    acc, pin, roles = accounts_with_roles(tmp_path / "Kelvin")
    rt.owner, rt.roles, rt.accounts = pin, roles, acc
    rt.versions = ConfigVersions(tmp_path / "Kelvin")
    return rt


def ask_in_background(rt):
    result = {}
    request = ApprovalRequest("Approve drive change on smu?", ["smu.V 0 V → 2 V"])
    t = threading.Thread(target=lambda: result.setdefault("yes", rt.gate.ask(request)))
    t.start()
    entry = rt.approver.wait_for_pending(timeout=5)
    return t, entry, result, request


def test_without_roles_kelvin_is_as_before(tmp_path):
    rt = runtime_with_roles(tmp_path)
    rt.user = "Cy"
    assert rt.role() is None
    t, entry, result, request = ask_in_background(rt)
    assert rt.answer(entry["id"], True, "human (portal)")               # anyone's yes, as before
    t.join(5)
    assert result["yes"] and " with " not in request.note
    assert rt.set_approval_mode("auto", "human (portal)")["mode"] == "auto"   # no PIN set: as before


def test_with_roles_a_trainee_asks_an_operator(tmp_path):
    rt = runtime_with_roles(tmp_path)
    rt.owner.set("2468")
    rt.roles.set({"Ada": "owner", "Ben": "operator"}, rt.owner, "2468", "Ada", rt.accounts)
    rt.user = "Cy"
    assert rt.role() == "trainee"
    t, entry, result, request = ask_in_background(rt)
    with pytest.raises(PermissionError, match="needs an operator"):
        rt.answer(entry["id"], True, "human (portal)")
    with pytest.raises(PermissionError):
        rt.answer(entry["id"], True, "human (portal)", approver={"name": "Ben", "password": "nope"})
    assert rt.approver.wait_for_pending(timeout=1)["id"] == entry["id"]   # still open
    assert rt.answer(entry["id"], True, "human (portal)", approver={"name": "Ben", "password": "ben-pass"})
    t.join(5)
    assert result["yes"] and "approved by Cy (portal) with Ben (operator) at" in request.note
    t, entry, result, request = ask_in_background(rt)
    assert rt.answer(entry["id"], False, "human (portal)")              # a no is anyone's
    t.join(5)
    assert result["yes"] is False
    with pytest.raises(RoleRefused, match="Auto mode is not for a trainee"):
        rt.set_approval_mode("auto", "human (portal)", pin="2468")      # the PIN doesn't make it a trainee's
    rt.hands_on("human (portal)")
    rt.hands_off("human (portal)", pin="2468")                          # unlocking: with the PIN, as for everyone
    rt.user = "Ben"
    assert rt.set_approval_mode("auto", "human (portal)", pin="2468")["mode"] == "auto"
    events = [e["what"] for e in rt.versions.history(subject="events")]
    assert events == ["auto mode on", "Hands on the sample unlocked"]
    told = [n["title"] for n in rt.notifier.notices if n["kind"] == "safety"]
    assert told == ["Virtual Lab: Hands on the sample unlocked", "Virtual Lab: auto mode on"]
    answers = [e for e in rt.gate.log.events() if e["type"] == "answer"]
    assert answers[0]["vouched"] == {"account": "Ben", "role": "operator"}


def test_the_approval_page_takes_the_owners_pin_when_the_lab_has_roles(tmp_path):
    import urllib.request
    rt = runtime_with_roles(tmp_path)
    rt.owner.set("2468")
    rt.roles.set({"Ada": "owner"}, rt.owner, "2468", "Ada", rt.accounts)
    page = ApprovalServer(rt, port=0).start()

    def post(path, body):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (page.port, path), data=json.dumps(body).encode(),
                                     headers={"X-Kelvin-Token": page.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        t, entry, result, request = ask_in_background(rt)              # nobody signs in here: a trainee
        status, body = post("/api/answer", {"id": entry["id"], "approve": True})
        assert status == 403 and body["pin"]
        assert post("/api/answer", {"id": entry["id"], "approve": True, "pin": "2468"})[0] == 200
        t.join(5)
        assert result["yes"] and "with the lab owner's PIN" in request.note
        status, body = post("/api/mode", {"mode": "auto", "pin": "2468"})
        assert status == 403 and "not for a trainee" in body["error"] and not body.get("pin")
    finally:
        page.stop()


# ---------------------------------------------------------------------------------- in the portal

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from labharness.adapters import set_resource_manager  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_hardware import Bus, Device  # noqa: E402


@pytest.fixture
def lab(tmp_path):
    """The PI's bench: a Keithley 2400 on the gate and an SR830 (a fake bus), three people with passwords."""
    set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k"),
                              "GPIB0::8::INSTR": Device("srs/sr830", name="l")}))
    home = tmp_path / "Kelvin"
    acc = Accounts(home)
    for name in ("Ada", "Ben", "Cy"):
        acc.create(name, name.lower() + "-pass")
    runtime = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), home / "lab")
    settings = Settings(home / "settings.json")
    app = create_app(runtime, settings, ChatStore(home / "lab" / "chats", create=False), hosts=["testserver"],
                     accounts=acc, providers={})
    posts = []
    runtime.notifier._post = lambda url, data, headers: posts.append((url, data.decode("utf-8")))
    yield TestClient(app), {"X-Kelvin-Token": settings.access_key}, runtime, settings, home, posts
    set_resource_manager(None)


def sign_in(client, h, name):
    r = client.post("/api/session", headers=h, json={"name": name, "password": name.lower() + "-pass"})
    assert r.status_code == 200, r.text


def wait_for(posts, n=1):
    deadline = time.time() + 5
    while len(posts) < n and time.time() < deadline:
        time.sleep(0.02)
    return posts


def test_a_trainee_can_widen_nothing_and_the_owner_hears_of_what_others_widen(lab):
    client, h, runtime, settings, home, posts = lab
    with client:
        sign_in(client, h, "Ada")
        assert client.post("/api/lab", headers=h, json={"yaml": LAB}).json()["ok"]
        assert client.get("/api/roles", headers=h).json()["on"] is False
        r = client.post("/api/roles", headers=h, json={"roles": {"Ada": "owner"}})
        assert r.status_code == 400 and "PIN first" in r.json()["error"]
        client.post("/api/owner-pin", headers=h, json={"pin": "2468"})
        r = client.post("/api/roles", headers=h, json={"roles": {"Ada": "owner", "Ben": "operator"}})
        assert r.status_code == 403 and r.json()["pin"]
        roles = client.post("/api/roles", headers=h, json={"roles": {"Ada": "owner", "Ben": "operator"},
                                                           "pin": "2468"}).json()
        assert roles["on"] and {p["name"]: p["role"] for p in roles["people"]} == {
            "Ada": "owner", "Ben": "operator", "Cy": "trainee"}
        settings.update({"webhook": "https://ntfy.sh/ada-secret"})
        r = client.post("/api/roles/notify", headers=h, json={"use": "mine", "pin": "2468"})
        assert r.status_code == 200 and r.json()["owner_notify"] == ["ntfy.sh"]
        r = client.post("/api/owner-pin", headers=h, json={"pin": "", "old": "2468"})
        assert r.status_code == 409 and (home / "owner.json").exists()   # roles change with the PIN: it stays

        sign_in(client, h, "Cy")                                          # the trainee
        r = client.post("/api/instruments/smu/limits", headers=h, json={"limits": {"V": [-150, 150]}, "pin": "2468"})
        assert r.status_code == 403 and r.json()["role"] == "trainee" and "not for a trainee" in r.json()["error"]
        r = client.post("/api/lab", headers=h, json={"yaml": LAB.replace("[-5, 5]", "[-150, 150]"), "pin": "2468"})
        assert r.status_code == 403 and r.json()["role"] == "trainee"
        r = client.post("/api/mode", headers=h, json={"mode": "auto", "pin": "2468"})
        assert r.status_code == 403 and r.json()["role"] == "trainee" and not runtime.gate.auto
        assert runtime.lab.instruments["smu"].limits("V") == (-5.0, 5.0)
        assert "[-5, 5]" in (home / "lab.yaml").read_text(encoding="utf-8")
        t, entry, result, request = ask_in_background(runtime)
        r = client.post("/api/approvals/" + entry["id"], headers=h, json={"approve": True})
        assert r.status_code == 403 and r.json()["approver"] and r.json()["approvers"] == ["Ada", "Ben"]
        r = client.post("/api/approvals/" + entry["id"], headers=h,
                        json={"approve": True, "approver": {"name": "Ben", "password": "ben-pass"}})
        assert r.status_code == 200
        t.join(5)
        assert result["yes"] and "with Ben (operator)" in request.note
        lines = [e["text"] for e in client.get("/api/lab-log", headers=h).json()["events"]]
        assert any("Cy (portal) with Ben (operator) approved: Approve drive change on smu?" in x for x in lines)

        sign_in(client, h, "Ben")                                         # the operator: as before, with the PIN
        r = client.post("/api/instruments/smu/limits", headers=h, json={"limits": {"V": [-8, 8]}})
        assert r.status_code == 403 and r.json()["pin"]
        r = client.post("/api/instruments/smu/limits", headers=h,
                        json={"limits": {"V": [-8, 8]}, "pin": "2468", "why": "the new device takes 8 V"})
        assert r.status_code == 200 and runtime.lab.instruments["smu"].limits("V") == (-8.0, 8.0)
        (n,) = r.json()["versions"]
        told = wait_for(posts)
        assert told[0][0] == "https://ntfy.sh/ada-secret"                 # the owner's, while Ben is signed in
        assert "smu.V: limits widened, [-5, 5] → [-8, 8]" in told[0][1] and "Ben (operator" in told[0][1]
        history = client.get("/api/config/history", headers=h).json()
        top = history["entries"][0]
        assert (top["n"], top["account"], top["role"], top["why"], top["kind"]) == (
            n, "Ben", "operator", "the new device takes 8 V", "limits")
        assert top["changes"] == [{"text": "smu.V: limits widened, [-5, 5] → [-8, 8]", "safety": True}]
        first = next(e for e in history["entries"] if e.get("subject") == "lab.yaml" and e["n"] < n)
        assert first["account"] == "Ada" and first["kind"] == "config"
        shown = client.get("/api/config/history/%d" % n, headers=h).json()
        assert "+    limits: { V: [-8, 8] }" in shown["diff"] and shown["entry"]["account"] == "Ben"
        safety = client.get("/api/config/history?safety=1", headers=h).json()["entries"]
        assert [e["n"] for e in safety] == [n, first["n"]]   # the PIN set, roles given: narrowing, so not these
        assert {c["text"]: c["safety"] for c in first["changes"]} == {
            "smu added (card keithley/2400 at GPIB0::24::INSTR; its limits count as reviewed)": True,
            "lockin added (card srs/sr830 at GPIB0::8::INSTR; its limits count as reviewed)": True}
        r = client.post("/api/roles", headers=h, json={"roles": {"Ben": "owner"}, "pin": "2468"})
        assert r.status_code == 403 and "only the lab's owner" in r.json()["error"]

        r = client.post("/api/config/rollback", headers=h, json={"n": first["n"]})   # going back: like a change
        assert r.status_code == 403 and r.json()["pin"]
        r = client.post("/api/config/rollback", headers=h, json={"n": first["n"], "pin": "2468", "why": "too much"})
        assert r.status_code == 200 and r.json()["ok"], r.text
        assert runtime.lab.instruments["smu"].limits("V") == (-5.0, 5.0)
        assert (home / "lab.yaml").read_text(encoding="utf-8") == LAB
        back = client.get("/api/config/history?subject=lab.yaml", headers=h).json()["entries"][0]
        assert back["kind"] == "rollback" and back["rollback"] == first["n"] and back["why"].startswith(
            "rolled back to #%d: too much" % first["n"])
        assert back["changes"] == [{"text": "smu.V: limits narrowed, [-8, 8] → [-5, 5]", "safety": False}]
        r = client.post("/api/config/rollback", headers=h, json={"n": first["n"], "pin": "2468"})
        assert r.status_code == 409                                       # it is that version already

        sign_in(client, h, "Cy")
        r = client.post("/api/config/rollback", headers=h, json={"n": n, "pin": "2468"})
        assert r.status_code == 403 and r.json()["role"] == "trainee"   # never a trainee's
        (home / "lab.yaml").write_text(LAB.replace("[-5, 5]", "[-50, 50]"), encoding="utf-8")   # by hand
        hand = client.get("/api/config/history", headers=h).json()["entries"][0]
        assert hand["by"] == "outside Kelvin" and hand["safety"]
        assert any("[-5, 5] → [-50, 50]" in text for _, text in wait_for(posts, 2)[1:])


def test_a_cards_wider_limits_and_review_are_the_owners_and_kept_too(lab):
    client, h, runtime, settings, home, posts = lab
    with client:
        sign_in(client, h, "Ada")
        assert client.post("/api/cards", headers=h, json={"yaml": CARD}).status_code == 200   # a new draft
        client.post("/api/owner-pin", headers=h, json={"pin": "2468"})
        r = client.post("/api/cards", headers=h, json={"yaml": CARD.replace("[0, 2]", "[0, 1]")})
        assert r.status_code == 200                                       # narrowing: no PIN
        wide = CARD.replace("[0, 2]", "[0, 50]")
        r = client.post("/api/cards", headers=h, json={"yaml": wide})
        assert r.status_code == 403 and r.json()["pin"]
        assert "[0, 1]" in (home / "cards" / "mylab_heater.yaml").read_text(encoding="utf-8")
        r = client.post("/api/cards", headers=h, json={"yaml": CARD.replace("status: draft", "status: lab-verified")})
        assert r.status_code == 403 and r.json()["pin"]                   # marked reviewed in the text: the PIN too
        assert client.post("/api/cards", headers=h, json={"yaml": wide, "pin": "2468"}).status_code == 200
        cards = client.get("/api/config/history?subject=cards", headers=h).json()["entries"]
        assert [e["changes"][0]["text"] for e in cards] == [
            "mylab/heater P: limits widened, [0, 1] → [0, 50]", "mylab/heater P: limits narrowed, [0, 2] → [0, 1]",
            "card mylab/heater added (a draft: each change on it asks)"]
        r = client.post("/api/config/rollback", headers=h, json={"n": cards[1]["n"], "pin": "2468"})
        assert r.status_code == 200 and "[0, 1]" in (home / "cards" / "mylab_heater.yaml").read_text(encoding="utf-8")
        assert runtime.lab.registry.get("mylab/heater").params["P"].limits == (0.0, 1.0)
        reviewed = client.post("/api/cards/status", headers=h, json={"id": "mylab/heater", "status": "lab-verified",
                                                                    "pin": "2468"})
        assert reviewed.status_code == 200
        r = client.post("/api/cards/delete", headers=h, json={"id": "mylab/heater"})
        assert r.status_code == 403 and r.json()["pin"]                   # a reviewed card: the owner's to delete
        assert client.post("/api/cards/delete", headers=h, json={"id": "mylab/heater", "pin": "2468"}).status_code == 200
        last = client.get("/api/config/history?subject=cards", headers=h).json()["entries"][:2]
        assert last[0]["changes"][0]["safety"] and last[0]["gone"] and "lab-verified" in last[1]["changes"][0]["text"]


def test_kelvins_own_changes_are_kept_as_its_own(lab):
    client, h, runtime, settings, home, posts = lab
    with client:
        sign_in(client, h, "Ada")
        assert client.post("/api/lab", headers=h, json={"yaml": LAB}).json()["ok"]
        runtime.last_found = ["GPIB0::7::INSTR"]
        set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k"),
                                  "GPIB0::8::INSTR": Device("srs/sr830", name="l"),
                                  "GPIB0::7::INSTR": Device("keithley/2182a", name="n")}))
        said = runtime.add_instrument("GPIB0::7::INSTR", "keithley/2182a", "nvm")
        assert said.startswith("added nvm"), said
        top = client.get("/api/config/history", headers=h).json()["entries"][0]
        assert top["by"] == "Kelvin (agent)" and top["account"] == "Ada" and top["kind"] == "added"
        assert top["changes"][0]["text"].startswith("nvm added") and not top["safety"]   # Kelvin's mark: it asks


def test_uninstalling_takes_the_pin_the_roles_and_the_history_too(tmp_path):
    from labharness.uninstall import remove_data
    home = tmp_path / "Kelvin"
    acc, pin, roles = accounts_with_roles(home)
    pin.set("2468")
    roles.set({"Ada": "owner"}, pin, "2468", "Ada", acc)
    roles.set_notify({"webhook": "https://ntfy.sh/secret", "email": {"password": "smtp-secret"}}, pin, "2468", "Ada")
    (home / "lab.yaml").write_text(LAB, encoding="utf-8")
    ConfigVersions(home).notice()
    remove_data(home, revoke=False)
    for gone in ("owner.json", "roles.json", "config-history", "accounts.json", "lab.yaml"):
        assert not (home / gone).exists(), gone                         # the owner's copied secrets go with them


def test_the_portal_asks_who_confirms_and_shows_the_history_and_the_roles():
    from labharness.web.app import STATIC
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "function askApprover(why, names, pinSet)" in js and "data.approver && opts.body" in js
    assert "e.data && e.data.approver" in js                            # a card not confirmed stays open
    for id_ in ("history-dialog", "history-list", "lab-history", "roles-box", "roles-edit", "owner-notify-mine",
                "lab-why"):
        assert 'id="%s"' % id_ in page, id_
    assert 'value="safety"' in page                                      # a person may tick safety changes too


# ---------------------------------------------------------------------------------- on labsim

from labsim_bench import labsim_bench  # noqa: E402,F401  (the fixture)

TRANSPORT = """lab: labsim transport
instruments:
  gate:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5], compliance_I: [1e-09, 1e-06] }
"""


def test_on_labsim_a_trainees_gate_voltage_waits_for_an_operator(labsim_bench, tmp_path):
    """The field tests' student, on instruments Kelvin takes for real (labsim's transport bench): the gate's 2400
    moves away from safe only once an operator confirms the trainee's yes, and the record says who confirmed it."""
    labsim_bench("transport")
    from labharness.virtual_lab import build_lab
    (tmp_path / "lab.yaml").write_text(TRANSPORT, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml", partial=True)
    try:
        assert lab.unavailable == {}, lab.unavailable
        rt = LabRuntime(lab, WebApprover(timeout=20), tmp_path / "data")
        acc, pin, roles = accounts_with_roles(tmp_path / "Kelvin")
        rt.owner, rt.roles, rt.accounts = pin, roles, acc
        pin.set("2468")
        roles.set({"Ada": "owner", "Ben": "operator"}, pin, "2468", "Ada", acc)
        rt.user = "Cy"
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("r", rt.set("gate", "output", True)))
        t.start()
        entry = rt.approver.wait_for_pending(timeout=20)
        assert entry is not None and "gate.output" in " ".join(entry["items"])
        with pytest.raises(PermissionError, match="needs an operator"):
            rt.answer(entry["id"], True, "human (portal)")
        assert lab.read("gate", "output") is False                       # nothing moved on the trainee's yes alone
        assert rt.answer(entry["id"], True, "human (portal)", approver={"name": "Ben", "password": "ben-pass"})
        t.join(30)
        assert "approved" in result["r"] and lab.read("gate", "output") is True, result
        answer = [e for e in rt.gate.log.events() if e["type"] == "answer"][-1]
        assert answer["account"] == "Cy" and answer["vouched"] == {"account": "Ben", "role": "operator"}
    finally:
        lab.close()


def test_a_trainee_unlocks_with_the_pin_but_never_takes_a_protected_sample_out(lab):
    client, h, runtime, settings, home, posts = lab
    gate = {"gates": [{"drive": "smu.V", "dielectric": "hBN", "thickness_nm": 20, "leak": "smu.I"}]}
    with client:
        sign_in(client, h, "Ada")
        client.post("/api/owner-pin", headers=h, json={"pin": "2468"})
        client.post("/api/roles", headers=h, json={"roles": {"Ada": "owner"}, "pin": "2468"})
        sign_in(client, h, "Cy")                                          # a trainee: the samples are his own
        p = client.post("/api/projects", json={"name": "Graphene"}, headers=h).json()
        url = "/api/projects/%s/devices" % p["id"]
        d4 = client.post(url, json={"name": "D4"}, headers=h).json()["devices"][0]
        client.post(url, json={"name": "D5"}, headers=h)
        r = client.patch("%s/%s" % (url, d4["id"]), json={"protection": gate}, headers=h)
        assert r.status_code == 200                                       # protecting narrows: a trainee may
        assert client.post("/api/mount", json={"project": p["id"], "device": "D4"}, headers=h).status_code == 200
        r = client.patch("%s/%s" % (url, d4["id"]), json={"protection": {}, "pin": "2468"}, headers=h)
        assert r.status_code == 403 and r.json()["role"] == "trainee"   # lifting it: never a trainee's
        assert client.post("/api/hands", json={"on": True}, headers=h).status_code == 200
        r = client.post("/api/hands", json={"on": False, "project": p["id"], "device": "D5", "pin": "2468"}, headers=h)
        assert r.status_code == 403 and r.json()["role"] == "trainee" and runtime.hands is not None
        r = client.post("/api/hands", json={"on": False, "project": p["id"], "device": "D4", "pin": "2468"}, headers=h)
        assert r.status_code == 200 and runtime.hands is None and runtime.mounted.get("device_name") == "D4"
        unlocked = [e for e in runtime.versions.history(subject="events") if e["what"] == "Hands on the sample unlocked"]
        assert unlocked and unlocked[0]["account"] == "Cy" and unlocked[0]["role"] == "trainee"


def test_rolling_a_card_back_never_marks_the_text_in_use_reviewed(lab):
    """A card's commands change only at reconnect: rolling the file back to a reviewed version must not mark the
    draft text the instrument still uses as reviewed."""
    from pathlib import Path
    client, h, runtime, settings, home, posts = lab
    with client:
        sign_in(client, h, "Ada")
        assert client.post("/api/lab", headers=h, json={"yaml": LAB}).json()["ok"]
        client.post("/api/owner-pin", headers=h, json={"pin": "2468"})
        text = Path(runtime.lab.registry.get("keithley/2400").source).read_text(encoding="utf-8")
        reviewed = text.replace("status: community", "status: lab-verified")
        draft = text.replace("status: community", "status: draft") + "\n# trying a new trigger\n"
        r = client.post("/api/cards", headers=h, json={"yaml": reviewed, "version": True, "pin": "2468"})
        assert r.status_code == 200, r.text
        assert client.post("/api/cards", headers=h, json={"yaml": draft, "version": True}).status_code == 200
        runtime.reconnect_instrument("smu", "human")
        assert runtime.lab.instruments["smu"].card.status == "draft"     # the draft text is in use now
        first = client.get("/api/config/history?subject=cards", headers=h).json()["entries"][-1]
        assert first["changes"][0]["text"].startswith("keithley/2400") or first["title"] == "card keithley/2400"
        r = client.post("/api/config/rollback", headers=h, json={"n": first["n"], "pin": "2468"})
        assert r.status_code == 200 and r.json()["reconnect"] == ["smu"]
        assert runtime.lab.instruments["smu"].card.status == "draft"     # until reconnect, still the draft's text
        runtime.reconnect_instrument("smu", "human")
        assert runtime.lab.instruments["smu"].card.status == "lab-verified"
