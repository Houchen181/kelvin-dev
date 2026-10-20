"""The Kelvin hub's database: accounts with consent, labs with no owner, and the sharing rules on what Kelvins send."""
import json
import re
import sqlite3
import sys

import pytest

from labharness.hub.client import HubClientError, check_url, collect
from labharness.hub.db import Hub, HubError, challenge_of, file_parts, loopback, orcid_ok

INST = {"id": "urn:uuid:5f0c1c55-0000-4000-8000-000000000001", "name": "Lab PC", "platform": "win32",
        "version": "0.3.8"}


def last_code(hub, path):
    """The code in the newest mail with a link to path."""
    found = [re.search(r"%s\?code=([\w-]+)" % re.escape(path), m.read_text(encoding="utf-8"))
             for m in sorted((hub.home / "outbox").glob("*.txt"))]
    return [f for f in found if f][-1].group(1)


def connected(hub, user, lab_id=None, inst=INST):
    """A Kelvin connected for this person, as the API sees it."""
    verifier = "v" * 50
    code = hub.connect_code(user, "http://127.0.0.1:8770/hub/callback", challenge_of(verifier), inst, lab_id)
    token = hub.connect_app(code, verifier, "http://127.0.0.1:8770/hub/callback")["access_token"]
    return token, hub.app(token)


def test_accounts_need_consent_and_keep_only_a_hash(tmp_path):
    hub = Hub(tmp_path)
    with pytest.raises(HubError):
        hub.register("ada@example.org", "Ada", "correct horse", consent=False)
    with pytest.raises(HubError):
        hub.register("ada@example.org", "Ada", "short", consent=True)
    with pytest.raises(HubError):
        hub.register("ada@example.org", "Ada", "correct horse", consent=True, orcid="0000-0002-1825-0098")
    ada = hub.register("Ada@Example.org", "Ada  Lovelace", "correct horse", consent=True, orcid="0000-0002-1825-0097")
    assert ada["email"] == "ada@example.org" and ada["name"] == "Ada Lovelace" and "password_hash" not in ada
    assert ada["consent_version"] == hub.config["consent_version"] and ada["consent_at"]
    with pytest.raises(HubError):
        hub.register("ADA@example.org", "Someone", "another password", consent=True)   # one account per email
    assert hub.authenticate("ada@EXAMPLE.org", "correct horse")["id"] == ada["id"]
    for email, pw in (("ada@example.org", "wrong password"), ("nobody@example.org", "correct horse")):
        with pytest.raises(HubError, match="Wrong email or password"):
            hub.authenticate(email, pw)
    for f in tmp_path.glob("hub.sqlite3*"):
        assert b"correct horse" not in f.read_bytes()
    assert orcid_ok("0000-0002-1694-233X")


def test_email_confirmation_and_password_reset_go_by_mail(tmp_path):
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    hub.send_confirmation(ada)
    assert hub.confirm_email(last_code(hub, "/confirm"))["email_confirmed_at"]
    with pytest.raises(HubError):
        hub.confirm_email(last_code(hub, "/confirm"))              # a code works once
    hub.start_reset("nobody@example.org")                          # says nothing, sends nothing
    assert len(list((tmp_path / "outbox").glob("*.txt"))) == 1
    hub.start_reset("ada@example.org")
    session, _ = hub.new_session(ada["id"])
    hub.finish_reset(last_code(hub, "/reset/new"), "a new password")
    assert hub.authenticate("ada@example.org", "a new password")
    assert hub.session(session) is None                           # a reset signs out every browser


def test_labs_have_no_owner_and_join_by_code(tmp_path):
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    ben = hub.register("ben@example.org", "Ben", "correct horse", consent=True)
    lab = hub.create_lab(ada["id"], "Smith Lab", "Harvard")
    assert re.match(r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$", lab["join_code"])
    with pytest.raises(HubError):
        hub.join_lab(ben["id"], "AAAA-BBBB-CCCC")
    hub.join_lab(ben["id"], lab["join_code"].lower().replace("-", " "))
    assert hub.labs_of(ben["id"])[0]["people"] == ["Ada", "Ben"]
    columns = [r[1] for r in sqlite3.connect(str(tmp_path / "hub.sqlite3")).execute("PRAGMA table_info(lab_members)")]
    assert columns == ["lab_id", "user_id", "joined_at"]          # everyone equal: no role
    hub.leave_lab(ada["id"], lab["id"])
    hub.leave_lab(ben["id"], lab["id"])
    with pytest.raises(HubError):
        hub.lab(lab["id"])                                         # nobody left: the lab is gone


def test_connecting_a_kelvin_needs_the_verifier_and_a_loopback_callback(tmp_path):
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    uri = "http://127.0.0.1:8770/hub/callback"
    code = hub.connect_code(ada, uri, challenge_of("v" * 50), INST, None)
    with pytest.raises(HubError):
        hub.connect_app(code, "w" * 50, uri)                        # wrong verifier...
    with pytest.raises(HubError):
        hub.connect_app(code, "v" * 50, uri)                        # ...and the code is used up
    code = hub.connect_code(ada, uri, challenge_of("v" * 50), INST, None)
    with pytest.raises(HubError):
        hub.connect_app(code, "v" * 50, "http://127.0.0.1:9999/other")
    token, app = connected(hub, ada)
    assert token.startswith("khub_") and app["user"]["id"] == ada["id"] and app["installation_id"] == INST["id"]
    token2, _ = connected(hub, ada)                                # connecting again replaces the old token
    assert hub.app(token) is None and hub.app(token2)
    assert len(hub.connections(ada["id"])) == 1
    for bad in ("https://127.0.0.1:8770/cb", "http://localhost:8770/cb", "http://evil.example:80/cb",
                "http://127.0.0.1/cb", "http://u:p@127.0.0.1:8770/cb", "http://127.0.0.1:8770/cb#x"):
        with pytest.raises(HubError):
            loopback(bad)
    assert loopback(uri) == uri


def test_sync_shares_only_what_may_be_shared(tmp_path):
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    ben = hub.register("ben@example.org", "Ben", "correct horse", consent=True)
    cy = hub.register("cy@example.org", "Cy", "correct horse", consent=True)
    lab = hub.create_lab(ada["id"], "Smith Lab")
    hub.join_lab(ben["id"], lab["join_code"])
    _, app = connected(hub, ada, lab["id"])
    done = hub.sync(app, {
        "projects": [{"id": "p00000001", "name": "CrSBr magnons"},
                     {"id": "p00000002", "name": "Secret sample", "rules": {"providers": ["local"], "literature": False}},
                     {"id": "p00000003", "name": "Unpublished thing", "unpublished": True}],
        "chats": [{"id": "aaaaaaaaaaaa", "title": "Unfiled", "messages": [{"role": "user", "text": "hi"}]},
                  {"id": "bbbbbbbbbbbb", "project": "p00000001", "title": "Filed", "messages": []},
                  {"id": "cccccccccccc", "project": "p00000002", "title": "Confidential", "messages": []},
                  {"id": "dddddddddddd", "title": "Incognito", "incognito": True, "messages": []}],
        "runs": [{"id": "run1", "project": "p00000001", "device": "D1", "meta": {"plan": "gate_sweep"}},
                 {"id": "run2", "project": "p00000002", "device": "D2"}],
    })
    assert (done["projects"], done["withheld"], done["chats"], done["runs"]) == (1, 2, 2, 1)
    assert {r.get("chat") or r.get("run") for r in done["refused"]} == {"cccccccccccc", "dddddddddddd", "run2"}
    raw = sqlite3.connect(str(tmp_path / "hub.sqlite3")).execute("SELECT name FROM projects").fetchall()
    assert sorted(n for (n,) in raw) == ["", "", "CrSBr magnons"]   # names of unshared projects never kept

    upload = tmp_path / "tmp" / "u1"
    upload.write_bytes(b"V,I\n0,0\n")
    hub.store_run_file(app, "run1", "data.csv", upload, 8)
    for bad in ("../x", "a/../../x", "", "CON.txt", ".hidden", "a/b/c/d/e"):
        with pytest.raises(HubError):
            file_parts(bad)

    seen = hub.shared_with(ben["id"])                              # a labmate sees it
    assert {c["title"] for c in seen["chats"]} == {"Unfiled", "Filed"} and len(seen["runs"]) == 1
    run = hub.run(ben["id"], seen["runs"][0]["id"])
    assert run["files"] == ["data.csv"] and run["meta"] == {"plan": "gate_sweep"}
    unfiled = next(c for c in seen["chats"] if c["title"] == "Unfiled")
    assert hub.chat(ben["id"], unfiled["id"])["messages"] == [{"role": "user", "text": "hi"}]
    assert hub.shared_with(cy["id"]) == {"projects": [], "chats": [], "runs": []}   # someone outside does not
    with pytest.raises(HubError):
        hub.run(cy["id"], run["id"])

    hub.sync(app, {"projects": [{"id": "p00000001", "shared": False}]})   # Ada makes the project confidential
    assert [c["title"] for c in hub.shared_with(ben["id"])["chats"]] == ["Unfiled"]
    assert hub.shared_with(ben["id"])["runs"] == [] and not (tmp_path / run["files_path"]).exists()

    hub.leave_lab(ben["id"], lab["id"])                            # leaving a lab: its work is out of view
    assert hub.shared_with(ben["id"])["chats"] == []
    hub.sync(app, {"removed": {"chats": ["aaaaaaaaaaaa"]}})
    assert hub.shared_with(ada["id"])["chats"] == []


def test_export_and_delete_take_everything(tmp_path):
    import zipfile
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    lab = hub.create_lab(ada["id"], "Solo Lab")
    _, app = connected(hub, ada, lab["id"])
    hub.sync(app, {"chats": [{"id": "aaaaaaaaaaaa", "title": "Mine", "messages": [{"role": "user", "text": "x"}]}],
                   "runs": [{"id": "run1"}]})
    upload = tmp_path / "tmp" / "u1"
    upload.write_bytes(b"1,2\n")
    hub.store_run_file(app, "run1", "data.csv", upload, 4)
    path = hub.export(ada["id"])
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        account = json.loads(z.read("account.json"))
    assert "password_hash" not in account and "salt" not in account and account["email"] == "ada@example.org"
    assert any(n.startswith("chats/") for n in names) and any(n.endswith("files/data.csv") for n in names)
    hub.delete_user(ada["id"])
    with pytest.raises(HubError):
        hub.user(ada["id"])
    con = sqlite3.connect(str(tmp_path / "hub.sqlite3"))
    for table in ("chats", "runs", "tokens", "lab_members", "labs", "projects"):
        assert con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] == 0, table
    assert not (tmp_path / "files" / str(ada["id"])).exists()
    assert con.execute("SELECT COUNT(*) FROM audit WHERE ip IS NOT NULL OR detail != '{}'").fetchone()[0] == 0


def test_operator_key_disable_and_backups(tmp_path):
    hub = Hub(tmp_path)
    path = hub.ensure_operator_key()
    key = path.read_text(encoding="utf-8").split()[-1]
    assert hub.is_operator(key) and not hub.is_operator("khop_wrong") and key not in hub.config_path.read_text()
    assert hub.ensure_operator_key() is None
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    token, _ = connected(hub, ada)
    hub.set_disabled(ada["id"], True)
    assert hub.app(token) is None
    with pytest.raises(HubError, match="disabled"):
        hub.authenticate("ada@example.org", "correct horse")
    hub.config["backups_kept"] = 2
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        assert hub.backup(day)
    assert hub.backup("2026-10-03") is None                         # once a day
    kept = sorted(p.name for p in (tmp_path / "backups").glob("*.sqlite3"))
    assert kept == ["hub-2026-10-02.sqlite3", "hub-2026-10-03.sqlite3"]
    copy = sqlite3.connect(str(tmp_path / "backups" / kept[-1]))
    assert copy.execute("SELECT email FROM users").fetchall() == [("ada@example.org",)]
    assert hub.stats()["people"] == 1 and hub.stats()["disabled"] == 1


@pytest.mark.skipif(sys.version_info < (3, 10), reason="the portal's chat store needs Python 3.10")
def test_a_kelvin_sends_only_its_shared_chats(tmp_path):
    from labharness.web.store import ChatStore
    store = ChatStore(tmp_path / "chats")
    open_project = store.create_project("Open")
    secret = store.create_project("Secret")
    store.set_rules(secret["id"], {"providers": ["local"], "literature": False})
    for title, project in (("unfiled", None), ("open", open_project["id"]), ("secret", secret["id"])):
        chat = store.create("demo:x", project=project)
        chat["title"] = title
        chat["messages"] = [{"role": "user", "text": title}]
        store.save(chat)
    out = collect(store)
    assert sorted(c["title"] for c in out["chats"]) == ["open", "unfiled"]
    assert {"id": secret["id"], "shared": False} in out["projects"]
    assert "Secret" not in json.dumps(out)                          # not even the name leaves the computer
    assert check_url("https://hub.example.org/") == "https://hub.example.org"
    for bad in ("http://hub.example.org", "ftp://x", ""):
        with pytest.raises(HubClientError):
            check_url(bad)
