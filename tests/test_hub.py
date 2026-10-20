"""The Kelvin hub's database: accounts with consent, labs with no owner, and the sharing rules on what Kelvins send."""
import json
import re
import sqlite3
import sys
import threading

import pytest

from labharness.hub import db as hubdb
from labharness.hub.client import HubClientError, check_url, collect
from labharness.hub.db import Busy, Hub, HubError, TooMuch, challenge_of, file_parts, loopback, orcid_ok

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


def lab_sees(hub, user):
    seen = hub.shared_with(user["id"])
    return sorted(c["title"] for c in seen["chats"]) + sorted(r["local_id"] for r in seen["runs"])


def test_shared_work_follows_the_lab_a_kelvin_shares_into_now(tmp_path):
    """Review P1: reconnecting with another lab, or none, moves everything that Kelvin shared; leaving a lab is
    for good."""
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    ben = hub.register("ben@example.org", "Ben", "correct horse", consent=True)
    cy = hub.register("cy@example.org", "Cy", "correct horse", consent=True)
    first = hub.create_lab(ada["id"], "First Lab")
    hub.join_lab(ben["id"], first["join_code"])
    _, app = connected(hub, ada, first["id"])
    hub.sync(app, {"projects": [{"id": "p00000001", "name": "P"}],
                   "chats": [{"id": "aaaaaaaaaaaa", "project": "p00000001", "title": "old", "messages": []}],
                   "runs": [{"id": "run1", "project": "p00000001"}]})
    assert lab_sees(hub, ben) == ["old", "run1"]

    _, app = connected(hub, ada, None)                        # reconnected with "No lab"
    assert lab_sees(hub, ben) == []                           # ...takes everything out of First Lab at once
    hub.sync(app, {"projects": [{"id": "p00000001", "name": "P"}],
                   "chats": [{"id": "bbbbbbbbbbbb", "project": "p00000001", "title": "new", "messages": []}]})
    assert lab_sees(hub, ben) == [] and lab_sees(hub, ada) == ["new", "old", "run1"]

    second = hub.create_lab(ada["id"], "Second Lab")
    hub.join_lab(cy["id"], second["join_code"])
    connected(hub, ada, second["id"])                         # reconnected into Second Lab
    assert lab_sees(hub, cy) == ["new", "old", "run1"] and lab_sees(hub, ben) == []

    hub.leave_lab(ada["id"], second["id"])
    hub.join_lab(ada["id"], second["join_code"])              # rejoining doesn't bring old work back
    assert lab_sees(hub, cy) == []


def test_the_hub_refuses_marked_chats_and_runs(tmp_path):
    """Review S6: the hub checks the marks itself, whatever a Kelvin sends."""
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    _, app = connected(hub, ada)
    hub.sync(app, {"chats": [{"id": "aaaaaaaaaaaa", "title": "fine", "messages": []}]})
    done = hub.sync(app, {
        "chats": [{"id": "aaaaaaaaaaaa", "title": "now unpublished", "unpublished": True, "messages": []},
                  {"id": "bbbbbbbbbbbb", "title": "confidential", "confidential": True, "messages": []},
                  {"id": "cccccccccccc", "title": "limited", "rules": {"providers": ["local"]}, "messages": []},
                  {"id": "dddddddddddd", "title": "unshared", "shared": False, "messages": []}],
        "runs": [{"id": "run1", "meta": {"incognito": True}}, {"id": "run2", "incognito": True},
                 {"id": "run3", "meta": {"confidential": True}}, {"id": "run4"}]})
    assert done["chats"] == 0 and done["runs"] == 1 and len(done["refused"]) == 7
    assert lab_sees(hub, ada) == ["run4"]                     # the chat it had before is gone too


def test_times_from_a_kelvin_are_checked(tmp_path):
    """Review S2: nonsense times are stored as now, so no page breaks on them."""
    import time
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    _, app = connected(hub, ada)
    bad = [1e300, -5, float("nan"), float("inf"), "soon", None, 0]
    hub.sync(app, {"chats": [{"id": "c%d" % i, "title": "t", "messages": [], "updated": t} for i, t in enumerate(bad)],
                   "runs": [{"id": "r%d" % i, "created": t} for i, t in enumerate(bad)]})
    seen = hub.shared_with(ada["id"])
    times = [c["updated_at"] for c in seen["chats"]] + [r["created_at"] for r in seen["runs"]]
    assert len(times) == 14 and all(abs(t - time.time()) < 60 for t in times)
    hub.sync(app, {"chats": [{"id": "ok", "title": "t", "messages": [], "updated": 1.7e9}]})
    assert [c["updated_at"] for c in hub.shared_with(ada["id"])["chats"] if c["title"] == "t"][-1] == 1.7e9


def test_the_disk_share_holds_under_parallel_uploads(tmp_path, monkeypatch):
    """Review S3: bytes are reserved before a file streams in; chats count; only a few uploads at once."""
    import time
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    _, app = connected(hub, ada)
    hub.sync(app, {"runs": [{"id": "run1"}]})
    hub.config["quota_gb_per_user"] = 1500 / 1024 ** 3        # 1500 bytes
    used = Hub._used
    monkeypatch.setattr(Hub, "_used", staticmethod(lambda con, uid: time.sleep(0.05) or used(con, uid)))   # a race
    stored, refused = [], []

    def upload(i):
        path = tmp_path / "tmp" / ("u%d" % i)
        path.write_bytes(b"x" * 1000)
        try:
            stored.append(hub.store_run_file(app, "run1", "f%d.bin" % i, path, 1000))
        except HubError as e:
            refused.append(e)

    threads = [threading.Thread(target=upload, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(stored) == 1 and len(refused) == 7 and all(isinstance(e, (TooMuch, Busy)) for e in refused)
    assert sum(f.stat().st_size for f in (tmp_path / "files").rglob("*") if f.is_file()) == 1000

    hub.config["quota_gb_per_user"] = 10_000 / 1024 ** 3
    first = hub.reserve_upload(app, "run1", "a.bin", 10)
    second = hub.reserve_upload(app, "run1", "b.bin", 10)
    with pytest.raises(Busy):
        hub.reserve_upload(app, "run1", "c.bin", 10)          # at most two at once
    hub.release_upload(second)
    with pytest.raises(TooMuch):
        hub.reserve_upload(app, "run1", "d.bin", 9000)         # 1000 stored + 10 held + 9000 > 10000
    hub.release_upload(first)
    done = hub.sync(app, {"chats": [{"id": "big", "title": "t", "messages": ["y" * 9500]}]})
    assert done["chats"] == 0 and "share" in done["refused"][0]["why"]   # chats count toward the share too


def test_account_ids_are_never_reused_and_deletion_leaves_nothing(tmp_path, caplog):
    """Review S4; hub.log never gets an address in the first place (review H4)."""
    caplog.set_level("INFO", logger="kelvin.hub")
    hub = Hub(tmp_path)
    bob = hub.register("bob@example.org", "Bob", "correct horse", consent=True)
    connected(hub, bob, inst=dict(INST, id="urn:uuid:5f0c1c55-0000-4000-8000-0000000000bb"))
    hub.send_confirmation(bob)
    hub.config["mail"] = dict(hub.config["mail"], smtp_host="127.0.0.1", smtp_port=1)   # a mail that fails
    hub.send_confirmation(bob)
    hub.config["mail"] = dict(hub.config["mail"], smtp_host="")
    hub.delete_user(bob["id"])
    carol = hub.register("carol@example.org", "Carol", "correct horse", consent=True)
    assert carol["id"] != bob["id"]
    import zipfile
    with zipfile.ZipFile(hub.export(carol["id"])) as z:            # Carol's record is hers alone
        assert [a["action"] for a in json.loads(z.read("activity.json"))] == ["registered"]
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        assert con.execute("SELECT COUNT(*) FROM installations").fetchone()[0] == 0
    assert not list((tmp_path / "outbox").glob("*.txt"))
    assert caplog.records and not any("bob@example.org" in r.getMessage() for r in caplog.records)


def test_an_old_database_gets_ids_that_are_never_reused(tmp_path):
    old_schema = hubdb.SCHEMA.replace("id INTEGER PRIMARY KEY AUTOINCREMENT,", "id INTEGER PRIMARY KEY,")
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        con.executescript(old_schema)
        con.execute("PRAGMA user_version = 1")
        for i in (1, 2):
            con.execute("INSERT INTO users (id, email, name, consent_version, consent_at, created_at) VALUES "
                        "(?, ?, 'X', 'v', 0, 0)", (i, "u%d@example.org" % i))
        con.execute("INSERT INTO audit (at, user_id, actor, action) VALUES (0, 7, 'user', 'account deleted')")
    hub = Hub(tmp_path)
    assert hub.user(2)["email"] == "u2@example.org"
    assert hub.register("new@example.org", "New", "correct horse", consent=True)["id"] == 8
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == hubdb.SCHEMA_VERSION
        assert "AUTOINCREMENT" in con.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()[0]


def test_one_use_codes_work_once_even_at_the_same_moment(tmp_path):
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    code = hub.new_code("confirm", ada["id"])
    won = []

    def take():
        try:
            won.append(hub.take_code("confirm", code))
        except HubError:
            pass

    threads = [threading.Thread(target=take) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1


def test_passwords_resets_and_names(tmp_path):
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        assert con.execute("SELECT iterations FROM users").fetchone()[0] == hubdb.ITERATIONS == 600_000
        salt = bytes(16)
        con.execute("UPDATE users SET password_hash = ?, salt = ?, iterations = 200000",
                    (hubdb._hash("correct horse", salt, 200_000), salt.hex()))
    hub.authenticate("ada@example.org", "correct horse")      # an older hash is redone at the next sign-in
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        assert con.execute("SELECT iterations FROM users").fetchone()[0] == 600_000
    assert hub.authenticate("ada@example.org", "correct horse")["id"] == ada["id"]

    token, _ = connected(hub, ada)
    hub.start_reset("ada@example.org")
    code = last_code(hub, "/reset/new")
    with pytest.raises(HubError):
        hub.finish_reset(code, "short")                       # a password that won't do keeps the link working
    hub.finish_reset(code, "a better password")
    assert hub.app(token) is None                             # a reset by mail disconnects every Kelvin

    for bad in ("NUL .txt", "CON .csv", "aux.log", "plot.png "):
        with pytest.raises(HubError):
            file_parts(bad)
    assert file_parts("plot 1.png") == ["plot 1.png"]
    for bad in ("http://[", " http://127.0.0.1:8770/cb", "http://127.0.0.1:8770/c b", "http://127.0.0.1:99999/cb",
                "http://127.0.0.1:8770/cb\x00"):
        with pytest.raises(HubError):
            loopback(bad)


def test_a_sign_in_never_undoes_a_reset_that_lands_meanwhile(tmp_path, monkeypatch):
    """Review H2: the rehash of an old 200k hash must not write the old password back over a reset."""
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "old password", consent=True)
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        salt = bytes(16)
        con.execute("UPDATE users SET password_hash = ?, salt = ?, iterations = 200000",
                    (hubdb._hash("old password", salt, 200_000), salt.hex()))
    hub.start_reset("ada@example.org")
    code = last_code(hub, "/reset/new")
    real, landed = hubdb._hash, []

    def racing(password, salt, iterations=hubdb.ITERATIONS):
        if password == "old password" and iterations == hubdb.ITERATIONS and not landed:
            landed.append(True)   # the rehash is being made: the reset lands right now
            hub.finish_reset(code, "new password")
        return real(password, salt, iterations)

    monkeypatch.setattr(hubdb, "_hash", racing)
    hub.authenticate("ada@example.org", "old password")
    assert landed
    monkeypatch.setattr(hubdb, "_hash", real)
    assert hub.authenticate("ada@example.org", "new password")["id"] == ada["id"]
    with pytest.raises(HubError):
        hub.authenticate("ada@example.org", "old password")


def test_an_old_hash_takes_as_long_to_check(tmp_path, monkeypatch):
    """Review H6: a wrong password costs a full current hash, old account or none, so timing says nothing."""
    hub = Hub(tmp_path)
    hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        salt = bytes(16)
        con.execute("UPDATE users SET password_hash = ?, salt = ?, iterations = 200000",
                    (hubdb._hash("correct horse", salt, 200_000), salt.hex()))
    real, spent = hubdb._hash, []
    monkeypatch.setattr(hubdb, "_hash", lambda pw, salt, it=hubdb.ITERATIONS: spent.append(it) or real(pw, salt, it))
    for email in ("ada@example.org", "nobody@example.org"):
        spent.clear()
        with pytest.raises(HubError):
            hub.authenticate(email, "wrong password")
        assert sum(spent) == hubdb.ITERATIONS


def test_re_reading_hub_json_never_resets_it(tmp_path):
    """Review H3: hub.json gone or half written while the hub runs: keep the settings, write nothing."""
    import os
    import time
    hub = Hub(tmp_path)
    hub.ensure_operator_key()
    hub.config["base_url"] = "https://hub.example.org"
    hub.save_config()
    want = dict(hub.config)
    path = tmp_path / "hub.json"
    path.write_text('{"base_url": "https://hub.exa', encoding="utf-8")   # an editor mid-save
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert hub.is_operator("khop_x") is False and hub.config == want
    path.unlink()                                                     # ...or between delete and rename
    hub.refresh_config()
    assert hub.config == want and not path.exists()


def test_reconnecting_with_a_new_installation_id_folds_the_old_one_in(tmp_path):
    """Review A1: nothing stays behind under an earlier installation id of the same person's Kelvin."""
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    ben = hub.register("ben@example.org", "Ben", "correct horse", consent=True)
    lab = hub.create_lab(ada["id"], "Lab")
    hub.join_lab(ben["id"], lab["join_code"])
    computer = "urn:uuid:5f0c1c55-0000-4000-8000-0000000000cc"
    old_token, old = connected(hub, ada, lab["id"], inst=dict(INST, id=computer))   # an id from before per-person ids
    hub.sync(old, {"projects": [{"id": "p00000001", "name": "Secret soon"}],
                   "chats": [{"id": "aaaaaaaaaaaa", "project": "p00000001", "title": "c1", "messages": []},
                             {"id": "bbbbbbbbbbbb", "title": "c2", "messages": []}],
                   "runs": [{"id": "run1", "project": "p00000001"}]})
    upload = tmp_path / "tmp" / "u"
    upload.write_bytes(b"1,2\n")
    hub.store_run_file(old, "run1", "data.csv", upload, 4)
    hub.sync(old, {"chats": [{"id": "cccccccccccc", "title": "c3", "messages": []}]})

    new_id = computer + ":0123456789ab"
    _, new = connected(hub, ada, lab["id"], inst=dict(INST, id=new_id))   # the computer part is folded in by itself
    hub.sync(new, {"projects": [{"id": "p00000001", "name": "Secret soon"}],   # Kelvin sends everything again
                   "chats": [{"id": "aaaaaaaaaaaa", "project": "p00000001", "title": "c1", "messages": []}]})
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        for table, n in (("projects", 1), ("chats", 3), ("runs", 1)):
            assert con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] == n, table   # one copy each
        assert {r[0] for r in con.execute("SELECT installation_id FROM chats")} == {new_id}
    assert hub.app(old_token) is None                                 # the old connection is over
    assert hub.run(ben["id"], hub.shared_with(ben["id"])["runs"][0]["id"])["files"] == ["data.csv"]

    hub.sync(new, {"projects": [{"id": "p00000001", "shared": False}]})   # now confidential: nothing of it stays
    seen = hub.shared_with(ben["id"])
    assert sorted(c["title"] for c in seen["chats"]) == ["c2", "c3"]
    assert seen["runs"] == [] and seen["projects"] == []

    elsewhere = "urn:uuid:5f0c1c55-0000-4000-8000-0000000000dd:0123456789ab"   # named as previous by the Kelvin
    code = hub.connect_code(ada, "http://127.0.0.1:8770/cb", challenge_of("v" * 50), dict(INST, id=elsewhere), None,
                            [new_id])
    hub.connect_app(code, "v" * 50, "http://127.0.0.1:8770/cb")
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        assert {r[0] for r in con.execute("SELECT installation_id FROM chats")} == {elsewhere}
    with pytest.raises(HubError):
        hubdb.previous_ids(",".join("urn:uuid:%08d" % i for i in range(6)), elsewhere)   # at most 5
    with pytest.raises(HubError):
        hubdb.previous_ids("bad id!", elsewhere)


def test_chats_keep_their_size_and_an_old_database_learns_it(tmp_path):
    """Review H1: a person's share is summed from an index, not by reading every chat."""
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    _, app = connected(hub, ada)
    hub.sync(app, {"chats": [{"id": "aaaaaaaaaaaa", "title": "t", "messages": ["µ" * 100]}]})
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        size = con.execute("SELECT bytes, LENGTH(CAST(messages AS BLOB)) FROM chats").fetchone()
        assert size[0] == size[1] > 200
        plan = " ".join(r[-1] for r in con.execute("EXPLAIN QUERY PLAN SELECT SUM(bytes) FROM chats WHERE user_id = 1"))
        assert "COVERING INDEX" in plan
        con.execute("UPDATE chats SET bytes = 0")
        con.execute("PRAGMA user_version = 2")
    Hub(tmp_path)                                                     # schema 2 -> 3 works the sizes out
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        assert con.execute("SELECT bytes FROM chats").fetchone()[0] == size[1]
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3


def test_folding_never_files_shared_work_into_a_project_made_private_since(tmp_path):
    """Review round 3: the old id shared project p with a chat and a run, and the new id has marked p not shared since.
    The old copies go; they are never re-filed into p, where a labmate would see them until the next sync."""
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    ben = hub.register("ben@example.org", "Ben", "correct horse", consent=True)
    lab = hub.create_lab(ada["id"], "Lab")
    hub.join_lab(ben["id"], lab["join_code"])
    old_id, new_id = "urn:uuid:5f0c1c55-0000-4000-8000-0000000000c1", "urn:uuid:5f0c1c55-0000-4000-8000-0000000000c2"
    _, old = connected(hub, ada, lab["id"], inst=dict(INST, id=old_id))
    hub.sync(old, {"projects": [{"id": "p00000001", "name": "Thesis"}],
                   "chats": [{"id": "aaaaaaaaaaaa", "project": "p00000001", "title": "c1", "messages": []}],
                   "runs": [{"id": "run1", "project": "p00000001"}]})
    upload = tmp_path / "tmp" / "u"
    upload.write_bytes(b"1,2\n")
    hub.store_run_file(old, "run1", "data.csv", upload, 4)
    _, new = connected(hub, ada, lab["id"], inst=dict(INST, id=new_id))
    hub.sync(new, {"projects": [{"id": "p00000001", "shared": False}]})   # marked private on the new id
    assert [c["title"] for c in hub.shared_with(ben["id"])["chats"]] == ["c1"]   # still there, under the old id
    cb = "http://127.0.0.1:8770/hub/callback"
    code = hub.connect_code(ada, cb, challenge_of("v" * 50), dict(INST, id=new_id), lab["id"], [old_id])
    hub.connect_app(code, "v" * 50, cb)                                   # the old id is folded in
    seen = hub.shared_with(ben["id"])
    assert seen["chats"] == [] and seen["runs"] == [] and seen["projects"] == []
    with sqlite3.connect(str(tmp_path / "hub.sqlite3")) as con:
        assert con.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert not list((tmp_path / "files").rglob("data.csv"))              # its files went too


def test_a_connect_code_outlives_a_busy_hub_but_not_a_wrong_verifier(tmp_path, monkeypatch):
    """Review round 3: the code is used up with the connection itself, so a busy hub (or a failure while folding)
    leaves it for a retry; a wrong verifier still uses it up at once."""
    monkeypatch.setattr(hubdb, "LOCK_WAIT", 0.2)
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    cb = "http://127.0.0.1:8770/hub/callback"
    code = hub.connect_code(ada, cb, challenge_of("v" * 50), INST, None)
    lock = hub._lock_for(ada["id"])
    lock.acquire()                                                        # as during one of Ada's long syncs
    try:
        with pytest.raises(Busy):
            hub.connect_app(code, "v" * 50, cb)
    finally:
        lock.release()
    assert hub.connect_app(code, "v" * 50, cb)["access_token"].startswith("khub_")   # the same code, now
    with pytest.raises(HubError, match="expired or was already used"):
        hub.connect_app(code, "v" * 50, cb)                               # and only once
    code = hub.connect_code(ada, cb, challenge_of("v" * 50), INST, None)
    with pytest.raises(HubError, match="code_verifier"):
        hub.connect_app(code, "w" * 50, cb)
    with pytest.raises(HubError, match="expired or was already used"):
        hub.connect_app(code, "v" * 50, cb)                               # a wrong verifier used it up


def test_deleting_an_account_frees_the_database_before_its_files_go(tmp_path, monkeypatch):
    """Review round 3: the files of a large account can take a long time to delete; nobody else's sign-in or sync
    may wait for that, so the deletion is committed first."""
    hub = Hub(tmp_path)
    ada = hub.register("ada@example.org", "Ada", "correct horse", consent=True)
    _, app = connected(hub, ada)
    hub.sync(app, {"runs": [{"id": "run1"}]})
    upload = tmp_path / "tmp" / "u"
    upload.write_bytes(b"1,2\n")
    hub.store_run_file(app, "run1", "data.csv", upload, 4)
    seen = {}
    real = hubdb.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        con = sqlite3.connect(str(tmp_path / "hub.sqlite3"), timeout=0.5)
        try:                                                              # someone else writing meanwhile
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
            seen["write"] = "ok"
        except sqlite3.OperationalError as e:
            seen["write"] = str(e)
        finally:
            con.close()
        return real(path, *args, **kwargs)
    monkeypatch.setattr(hubdb.shutil, "rmtree", rmtree)
    hub.delete_user(ada["id"])
    assert seen["write"] == "ok" and not list((tmp_path / "files").rglob("data.csv"))
