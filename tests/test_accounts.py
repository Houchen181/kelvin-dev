"""Accounts: everyone equal, each with their own settings, keys, chats, data folder and record; the lab shared."""
import json

import pytest

from labharness.accounts import Accounts, cipher_for, has_legacy

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402

try:
    import cryptography  # noqa: F401
    CRYPTO = True
except ImportError:
    CRYPTO = False


@pytest.fixture(autouse=True)
def no_keys_in_the_environment(monkeypatch):
    """These tests say whose keys are whose; a key in this computer's environment would answer for everyone."""
    from labharness.web.settings import ENV
    for name in list(ENV.values()) + ["GOOGLE_API_KEY"]:
        monkeypatch.delenv(name, raising=False)


def test_accounts_are_equal_and_keep_passwords_hashed(tmp_path):
    acc = Accounts(tmp_path)
    ada = acc.create("Ada Lovelace", "pw1234")
    acc.create("Ben", "")
    with pytest.raises(ValueError):
        acc.create("ada lovelace", "")            # names are unique, ignoring case
    with pytest.raises(ValueError):
        acc.create("a/b", "")
    with pytest.raises(ValueError):
        acc.create("Cy", "abc")                   # a password, if any, has 4 characters or more
    listed = acc.list()
    assert [a["name"] for a in listed] == ["Ada Lovelace", "Ben"]
    assert listed[0]["password"] and not listed[1]["password"]
    assert "pw1234" not in (tmp_path / "accounts.json").read_text(encoding="utf-8")
    assert all("hash" not in a and "salt" not in a for a in listed)   # the sign-in screen never sees hashes
    assert acc.verify("ada lovelace", "pw1234")["name"] == "Ada Lovelace"
    with pytest.raises(ValueError):
        acc.verify("Ada Lovelace", "wrong")
    with pytest.raises(ValueError):
        acc.verify("Nobody", "")
    assert acc.verify("Ben")["name"] == "Ben"
    assert acc.dir(ada) == tmp_path / "users" / "Ada_Lovelace"
    assert acc.data_dir(ada) == tmp_path / "users" / "Ada_Lovelace" / "data"
    acc.change_password("Ada Lovelace", "pw1234", "")
    assert acc.verify("Ada Lovelace", "")
    acc.remove("Ben")
    assert [a["name"] for a in acc.list()] == ["Ada Lovelace"]
    assert (tmp_path / "users" / "Ben").is_dir()    # forgetting an account keeps its folder
    assert acc.dir(acc.create("Ben", "")) == tmp_path / "users" / "Ben_2"   # ...and a new Ben gets a new one


def test_settings_split_between_the_computer_and_each_person(tmp_path):
    lab_file = tmp_path / "settings.json"
    lab_file.write_text(json.dumps({"anthropic_api_key": "sk-old", "lab": "virtual", "default_model": "x"}))
    s = Settings(lab_file)
    s.adopt(tmp_path / "users" / "ada" / "settings.json")   # the first account takes over the old keys
    lab = json.loads(lab_file.read_text())
    assert "anthropic_api_key" not in lab and lab["access_key"]   # an old "lab" choice (the virtual lab) is no setting now
    s.use(tmp_path / "users" / "ada" / "settings.json")
    assert s.get("anthropic_api_key") == "sk-old" and s.get("default_model") == "x"
    s.update({"openai_api_key": "sk-ada", "auto_setup": "off"})
    assert json.loads(lab_file.read_text()).get("auto_setup") == "off"            # the computer's switch
    assert "openai_api_key" not in json.loads(lab_file.read_text())
    s.use(tmp_path / "users" / "ben" / "settings.json")
    assert s.get("openai_api_key") is None and s.public()["auto_setup"] is False  # Ben's keys are his own
    s.use(None)
    assert s.get("anthropic_api_key") is None


@pytest.mark.skipif(not CRYPTO, reason="needs the cryptography package")
def test_a_password_encrypts_the_keys(tmp_path):
    acc = Accounts(tmp_path)
    ada = acc.create("Ada", "pw1234")
    path = acc.settings_path(ada)
    s = Settings(tmp_path / "settings.json")
    s.use(path, cipher_for("pw1234", ada))
    s.update({"anthropic_api_key": "sk-secret"})
    s.add_service({"preset": "openrouter", "api_key": "sk-svc"})
    raw = path.read_text(encoding="utf-8")
    assert "sk-secret" not in raw and "sk-svc" not in raw and "enc:" in raw
    s.use(path, cipher_for("pw1234", ada))
    assert s.get("anthropic_api_key") == "sk-secret" and s.service_key(s.services[0]) == "sk-svc"
    s.use(path)                                   # without the password: as if there were no key
    assert s.get("anthropic_api_key") is None and s.public()["keys"]["anthropic_api_key"] is None
    s.update({"default_model": "m"})              # ...and saving keeps the sealed key as it was
    s.use(path, cipher_for("pw1234", ada))
    assert s.get("anthropic_api_key") == "sk-secret"


def legacy_home(tmp_path):
    """Kelvin as one person used it before accounts: keys in settings.json, chats and runs in virtual-lab."""
    (tmp_path / "settings.json").write_text(json.dumps({"anthropic_api_key": "sk-old", "default_model": "demo:x"}))
    old = ChatStore(tmp_path / "virtual-lab" / "chats")
    chat = old.create("demo:x")
    chat["title"] = "Old chat"
    old.save(chat)
    (tmp_path / "virtual-lab" / "runs").mkdir()
    return tmp_path


def portal(home, **kw):
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), home / "lab", demo_envelope(lab))
    settings = Settings(home / "settings.json")
    store = ChatStore(home / "lab" / "chats", create=False)
    app = create_app(runtime, settings, store, hosts=["testserver"], accounts=Accounts(home), **kw)
    return TestClient(app), {"X-Kelvin-Token": settings.access_key}, runtime, settings


def test_sign_in_gives_each_person_their_own_things(tmp_path):
    home = legacy_home(tmp_path)
    assert has_legacy(home)
    client, h, runtime, settings = portal(home)
    r = client.get("/api/bootstrap", headers=h)
    assert r.status_code == 401 and r.json()["signin"]            # nothing before someone signs in
    s = client.get("/api/session", headers=h).json()
    assert s["accounts_on"] and s["user"] is None and s["accounts"] == [] and s["legacy"]

    r = client.post("/api/accounts", headers=h, json={"name": "Ada", "password": "pw1234"})
    assert r.status_code == 200 and r.json()["user"] == {"name": "Ada", "password": True, "email": None, "central": False}
    boot = client.get("/api/bootstrap", headers=h).json()
    assert [c["title"] for c in boot["chats"]] == ["Old chat"]     # the first account takes over the old chats
    assert boot["account"]["user"]["name"] == "Ada"
    assert runtime.data_dir == home / "virtual-lab"                # ...and the old runs
    assert settings.get("anthropic_api_key") == "sk-old"
    assert "sk-old" not in (home / "settings.json").read_text()
    if CRYPTO:
        assert "sk-old" not in (home / "users" / "Ada" / "settings.json").read_text()
    client.post("/api/mode", headers=h, json={"mode": "auto"})
    assert runtime.gate.auto
    runtime.gate.log.write("note", text="Ada was here")

    r = client.post("/api/accounts", headers=h, json={"name": "Ben", "password": ""})   # anyone can add a person
    assert r.json()["user"]["name"] == "Ben"
    boot = client.get("/api/bootstrap", headers=h).json()
    assert boot["chats"] == [] and settings.get("anthropic_api_key") is None
    assert runtime.data_dir == home / "users" / "Ben" / "data"
    assert not runtime.gate.auto                                     # Ada's auto mode is not Ben's
    runtime.gate.log.write("note", text="Ben was here")
    ada_record = (home / "virtual-lab" / "events.jsonl").read_text()
    ben_record = (home / "users" / "Ben" / "data" / "events.jsonl").read_text()
    assert "Ada was here" in ada_record and "Ben was here" not in ada_record
    assert "Ben was here" in ben_record and "Ada was here" not in ben_record
    shared = [json.loads(line) for line in (home / "instrument-log.jsonl").read_text().splitlines()]
    assert {(e.get("text"), e.get("user")) for e in shared if e["type"] == "note"} == \
        {("Ada was here", "Ada"), ("Ben was here", "Ben")}            # the lab's log has everyone, by name
    log = client.get("/api/lab-log", headers=h).json()["events"]
    assert log[0]["user"] == "Ben"

    assert client.delete("/api/session", headers=h).json()["user"] is None
    assert client.get("/api/chats", headers=h).status_code == 401
    assert client.post("/api/session", headers=h, json={"name": "Ada", "password": "nope"}).status_code == 401
    r = client.post("/api/session", headers=h, json={"name": "ada", "password": "pw1234"})
    assert r.status_code == 200
    assert [c["title"] for c in client.get("/api/chats", headers=h).json()] == ["Old chat"]
    assert settings.get("anthropic_api_key") == "sk-old"


def test_one_person_without_a_password_goes_straight_in(tmp_path):
    Accounts(tmp_path).create("Ada", "")
    client, h, runtime, _ = portal(tmp_path)
    assert client.get("/api/bootstrap", headers=h).status_code == 200
    assert runtime.user == "Ada" and runtime.data_dir == tmp_path / "users" / "Ada" / "data"


def test_people_sign_in_while_the_instruments_are_set_up(tmp_path):
    import threading
    client, h, runtime, _ = portal(tmp_path)
    held, done = threading.Event(), threading.Event()

    def setup():   # the start-up setup holds the lab for minutes: connecting, scanning, reading every setting
        with runtime.lock:
            runtime.setup_status = {"state": "reading", "message": "Reading the settings of smu"}
            held.set()
            done.wait(10)
    thread = threading.Thread(target=setup)
    thread.start()
    held.wait(5)
    try:
        shown = client.get("/api/session", headers=h).json()["setup"]          # the sign-in screen says so
        assert shown == {"state": "reading", "message": "Reading the instruments' settings"}   # not which ones
        r = client.post("/api/accounts", headers=h, json={"name": "Ada", "password": "pw1234"})
        assert r.status_code == 200 and runtime.user == "Ada"                  # in at once, not "Kelvin is busy"
        assert runtime.data_dir == tmp_path / "users" / "Ada" / "data"
        runtime.setup_status = {"state": "done"}                               # something else holds the lab
        r = client.post("/api/accounts", headers=h, json={"name": "Ben", "password": ""})
        assert r.status_code == 409 and "busy" in r.json()["error"] and runtime.user == "Ada"
    finally:
        done.set()
        thread.join(5)
    assert client.post("/api/session", headers=h, json={"name": "Ben"}).status_code == 200
    assert client.get("/api/session", headers=h).json()["setup"] is None


def test_a_running_queue_keeps_its_person(tmp_path):
    acc = Accounts(tmp_path)
    acc.create("Ada", "")
    acc.create("Ben", "")
    client, h, runtime, _ = portal(tmp_path)
    assert client.post("/api/session", headers=h, json={"name": "Ada"}).status_code == 200
    runtime.queue.state = "running"                                  # Ada's overnight queue
    client.delete("/api/session", headers=h)
    assert runtime.user == "Ada"                                     # it goes on, recorded as hers
    r = client.post("/api/session", headers=h, json={"name": "Ben"})
    assert r.status_code == 409 and "Ada" in r.json()["error"]
    assert client.get("/api/session", headers=h).json()["busy_user"] == "Ada"
    assert client.post("/api/session", headers=h, json={"name": "Ada"}).status_code == 200   # back to watch it
    client.delete("/api/session", headers=h)
    assert client.post("/api/runs/stop", headers=h, json={"queue": True}).status_code == 200   # anyone may stop it
    assert runtime.queue.state == "pausing"
    runtime.queue.state = "idle"
    assert client.post("/api/session", headers=h, json={"name": "Ben"}).status_code == 200
    assert runtime.user == "Ben"


def test_phones_and_password_changes_are_per_person(tmp_path):
    acc = Accounts(tmp_path)
    acc.create("Ada", "pw1234")
    acc.create("Ben", "")
    client, h, runtime, settings = portal(tmp_path)
    client.post("/api/session", headers=h, json={"name": "Ada", "password": "pw1234"})
    settings.add_device("Ada's phone", "watch", "h" * 64)
    client.post("/api/session", headers=h, json={"name": "Ben"})
    assert settings.devices == []
    client.post("/api/session", headers=h, json={"name": "Ada", "password": "pw1234"})
    assert [d["name"] for d in settings.devices] == ["Ada's phone"]
    settings.update({"anthropic_api_key": "sk-ada"})
    r = client.post("/api/account", headers=h, json={"old": "pw1234", "new": "newpass"})
    assert r.status_code == 200
    assert client.post("/api/account", headers=h, json={"old": "wrong", "new": "x1234"}).status_code == 400
    client.delete("/api/session", headers=h)
    client.post("/api/session", headers=h, json={"name": "Ada", "password": "newpass"})
    assert settings.get("anthropic_api_key") == "sk-ada"            # the keys follow the new password


def test_a_phone_stays_paired_while_its_person_is_away(tmp_path):
    from labharness.web.remote import token_hash
    acc = Accounts(tmp_path)
    acc.create("Ada", "")
    acc.create("Ben", "")
    client, h, runtime, settings = portal(tmp_path)
    client.post("/api/session", headers=h, json={"name": "Ada"})
    settings.add_device("Ada's phone", "watch", token_hash("phone-token"))
    guard = client.app
    guard.remote.port = 8771                       # as if the network listener were running
    phone = TestClient(guard, base_url="http://192.168.1.50:8771")
    ph = {"X-Kelvin-Token": "phone-token"}
    assert phone.get("/api/state", headers=ph).status_code == 200
    client.post("/api/session", headers=h, json={"name": "Ben"})
    r = phone.get("/api/state", headers=ph)         # Ben's chats and runs are not Ada's phone's to see
    assert r.status_code == 401 and r.json()["signin"] and "Ada" in r.json()["error"]   # ...but it stays paired
    assert "signin" not in phone.get("/api/state", headers={"X-Kelvin-Token": "stranger"}).json()
    client.delete("/api/session", headers=h)
    r = phone.get("/api/state", headers=ph)
    assert r.status_code == 401 and r.json()["signin"]
    client.post("/api/session", headers=h, json={"name": "Ada"})
    assert phone.get("/api/state", headers=ph).status_code == 200
