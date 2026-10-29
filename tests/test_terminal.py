"""The portal's terminal pane (a person's own instrument commands, through the same gate) and the data folder."""
import json
import threading

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from test_web import Scripted, client_for, human, parts  # noqa: E402,F401  (fixture)


def events(runtime):
    return [json.loads(line) for line in runtime.events_path.read_text(encoding="utf-8").splitlines()]


def test_terminal_sends_a_persons_commands_through_the_gate(parts):
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        def term(**body):
            return client.post("/api/terminal", json=body, headers=headers).json()
        r = term(instrument="lockin", op="raw", arg="*IDN?")
        assert r["ok"] and r["action"] == "read" and "LOCK-IN" in r["value"]
        r = term(instrument="smu", op="raw", arg="CALibration:ZERO")
        assert not r["ok"] and r["action"] == "refused" and "blocked" in r["message"]
        r = term(instrument="smu", op="set", arg="V", value=99)
        assert not r["ok"] and "limit" in r["message"]                              # lab limits hold

        human(runtime.approver, [True])                                             # the y in the terminal
        r = term(instrument="smu", op="raw", arg="SOURce:VOLTage 0.2")
        assert r["ok"] and "approved" in r["message"], r
        assert runtime.gate._undo == []                                             # not Kelvin's to undo
        human(runtime.approver, [False])
        r = term(instrument="smu", op="set", arg="V", value=0.4)
        assert not r["ok"] and r["action"] == "declined"
        r = term(instrument="smu", op="set", arg="V", value=0)                     # toward safe: no question
        assert r["ok"] and "toward safe" in r["message"]

        mine = [e for e in events(runtime) if e.get("by") == "human (terminal)"]
        assert {e["type"] for e in mine} >= {"command", "ask"} and not any(e.get("by") == "agent" for e in events(runtime))

        assert "V  drive  V" in term(instrument="smu", op="describe")["message"]
        assert "simulated" in term(instrument="smu", op="manual", arg="trigger")["message"]
        assert "no instrument" in term(instrument="nope", op="raw", arg="*IDN?")["message"]
        assert term(instrument="smu", op="raw", arg="  ")["action"] == "refused"
        assert term(instrument="smu", op="erase", arg="x")["action"] == "refused"
        assert client.post("/api/terminal", json={"instrument": "smu", "arg": "x" * 3000}, headers=headers).status_code == 400

        held = threading.Event()
        release = threading.Event()

        def busy():
            with runtime.lock:
                runtime.busy = "running plan gate_sweep"
                held.set()
                release.wait(5)
            runtime.busy = None
        threading.Thread(target=busy, daemon=True).start()
        held.wait(5)
        r = term(instrument="lockin", op="raw", arg="*IDN?")                       # never lands inside a run
        release.set()
        assert r["action"] == "busy" and "gate_sweep" in r["message"]


def test_the_data_folder_can_move_with_what_is_saved(parts, tmp_path):
    runtime, settings, store = parts
    runtime.engine.store.root.mkdir(parents=True, exist_ok=True)
    for name in ("20261001-120000-sweep", "20261002-120000-map"):
        (runtime.data_dir / "runs" / name).mkdir()
        (runtime.data_dir / "runs" / name / "meta.json").write_text("{}", encoding="utf-8")
    (runtime.data_dir / "reports").mkdir()
    (runtime.data_dir / "reports" / "week.pdf").write_bytes(b"%PDF-")
    runtime.gate.log.write("note", by="test")
    old = runtime.data_dir
    new = tmp_path / "lab server" / "Kelvin data"
    clash = new / "runs" / "20261002-120000-map"
    clash.mkdir(parents=True)                                     # a run of that name is there already

    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        now = client.get("/api/data-folder", headers=headers).json()
        assert now["path"] == str(old) and now["runs"] == 2 and now["reports"] == 1
        bad = client.post("/api/data-folder", json={"path": "relative/folder"}, headers=headers)
        assert bad.status_code == 400 and "full path" in bad.json()["error"]
        afile = tmp_path / "a-file"
        afile.write_text("x", encoding="utf-8")
        bad = client.post("/api/data-folder", json={"path": str(afile)}, headers=headers)
        assert bad.status_code == 400 and "cannot write" in bad.json()["error"]

        r = client.post("/api/data-folder", json={"path": str(new), "move": True}, headers=headers).json()
        assert r["path"] == str(new) and r["moved"] == 3 and r["kept"] == ["runs/20261002-120000-map"]
        assert (new / "runs" / "20261001-120000-sweep" / "meta.json").exists() and (new / "reports" / "week.pdf").exists()
        assert (old / "runs" / "20261002-120000-map").exists()                     # nothing overwritten
        assert settings.get("data_dir") == str(new)
        assert runtime.events_path == new / "events.jsonl"
        assert any(e["type"] == "data_folder" for e in events(runtime))
        assert any(e.get("type") == "note" for e in events(runtime))                # the record came along

        client.post("/api/terminal", json={"instrument": "lockin", "arg": "*IDN?"}, headers=headers)
        assert any(e.get("target") == "lockin: *IDN?" for e in events(runtime))     # new commands land there
        assert [x["run_id"] for x in runtime.runs_index()] == ["20261002-120000-map", "20261001-120000-sweep"]

        back = tmp_path / "plain"
        r = client.post("/api/data-folder", json={"path": str(back)}, headers=headers).json()
        assert r["moved"] == 0 and (new / "runs" / "20261001-120000-sweep").exists()   # without move, data stay


def test_kelvin_starts_on_the_chosen_folder_or_says_why_not(tmp_path):
    from labharness.web.app import data_folder_for
    from labharness.web.settings import Settings
    settings = Settings(tmp_path / "settings.json")
    assert data_folder_for(settings, tmp_path) == (tmp_path / "lab", None)
    settings.set_data_dir(str(tmp_path / "chosen"))
    assert data_folder_for(settings, tmp_path) == (tmp_path / "chosen", None)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a folder", encoding="utf-8")
    settings.set_data_dir(str(blocked))
    folder, problem = data_folder_for(Settings(tmp_path / "settings.json"), tmp_path)
    assert folder == tmp_path / "lab" and "can't be written" in problem
    settings.update({"data_dir": str(tmp_path / "sneaky")})                        # only through the runtime
    assert settings.get("data_dir") == str(blocked)


def test_the_portal_has_the_terminal_pane():
    from labharness.web.app import STATIC
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for el in ('id="toggle-term"', 'id="termpane"', 'id="term-input"', 'id="data-path"'):
        assert el in page
    assert '"/api/terminal"' in js and "function termSlash" in js and "termState(s);" in js
    # the terminal claims the approvals its own commands wait for before the Lab panel does
    assert js.index("termState(s);") < js.index("const open = new Set(s.pending")
