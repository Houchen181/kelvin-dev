"""Persisted experiment data has project/chat ownership before models can read it."""
import json

import pytest

from labharness import build_virtual_lab
from labharness import datascope
from labharness.agent.runtime import LabRuntime
from labharness.gate import ScriptedApprover
from labharness.scanqueue import ScanQueue
from labharness.web.store import ChatStore

ALPHA = {"project": "palpha", "chat": "alpha"}
BETA = {"project": "pbeta", "chat": "beta"}
LINE = "plan: scan\nsweep: {stage.position: {values: [0, 1]}}\nmeasure: [lockin.X]\n"


@pytest.fixture
def runtime(tmp_path):
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover(), tmp_path / "data")


def record(runtime, scope, name="private", finish=True):
    with datascope.use(scope):
        rec = runtime.engine.store.create(name, {"plan": name}, "agent")
        rec.add({"i": 0, "secret_result": 123.456})
        if finish:
            rec.finish(status="done", points=1, planned_points=1)
        runtime.gate.log.write("trip", message=name)
        return rec.run_id


def test_runs_and_events_do_not_cross_projects(runtime):
    private = record(runtime, ALPHA, "SECRET_ALPHA")
    public = record(runtime, BETA, "BETA")
    with datascope.use(BETA):
        assert "SECRET_ALPHA" not in runtime.list_runs()
        assert "SECRET_ALPHA" not in runtime.events()
        assert "123.456" not in runtime.run_data(private)
        assert runtime.pick_runs(last=1) == [public]
        assert runtime.make_report(runs=[private]).startswith("no report:")
    with datascope.use({"project": "palpha", "chat": "another-alpha-chat"}):
        assert "123.456" in runtime.run_data(private)


def test_recent_event_limit_is_applied_after_scope_filtering(runtime):
    with datascope.use(ALPHA):
        runtime.gate.log.write("trip", message="own recent event")
    with datascope.use(BETA):
        for _ in range(5):
            runtime.gate.log.write("trip", message="other project")
    with datascope.use(ALPHA):
        assert "own recent event" in runtime.events(1)
        assert "other project" not in runtime.events(1)


def test_unfiled_chats_are_isolated_and_unowned_runs_are_not_promoted(runtime):
    unowned = record(runtime, None, "legacy")
    own = record(runtime, {"project": None, "chat": "first"}, "first")
    with datascope.use({"project": None, "chat": "second"}):
        assert runtime.list_runs() == "no runs yet"
        assert "123.456" not in runtime.run_data(own)
        assert "123.456" not in runtime.run_data(unowned)
    assert "123.456" in runtime.run_data(unowned)  # the local owner still has their records


def test_ownership_is_saved_before_the_run_finishes(runtime):
    run_id = record(runtime, ALPHA, finish=False)
    meta = json.loads((runtime._run_dir(run_id) / "meta.json").read_text())
    assert meta["scope"] == ALPHA and meta["status"] == "running"
    with datascope.use(BETA), pytest.raises(KeyError):
        runtime._run_dir(run_id)


def test_scoped_mcp_wrapper_applies_ownership_in_the_server_worker(runtime):
    private = record(runtime, ALPHA)
    bound = datascope.ScopedRuntime(runtime, BETA)
    assert "123.456" not in bound.run_data(private)
    assert datascope.current() is None


def test_queued_scans_keep_their_project_in_the_worker_and_after_restart(runtime):
    with datascope.use(ALPHA):
        runtime.queue_add(LINE.replace("scan", "PRIVATE_ALPHA"))
    with datascope.use(BETA):
        runtime.queue_add(LINE.replace("scan", "BETA_SCAN"))
        assert "PRIVATE_ALPHA" not in runtime.queue_show()
        with pytest.raises(KeyError):
            runtime.queue.find(runtime.queue.items[0].id)
    with datascope.use(ALPHA):
        assert runtime.queue_start().startswith("queue started:")
    runtime._queue_thread.join(timeout=5)
    assert not runtime._queue_thread.is_alive()
    alpha, beta = runtime.queue.items
    assert alpha.status == "done" and beta.status == "waiting"
    assert runtime._run_meta(alpha.run_id)["scope"] == ALPHA
    restored = ScanQueue(runtime.queue.path)
    with datascope.use(BETA):
        assert [i.name for i in restored.visible_items()] == ["BETA_SCAN"]
        assert restored.clear_finished() == 0  # cannot delete another project's finished item


def test_confidential_notes_cannot_be_promoted_even_at_storage(tmp_path):
    store = ChatStore(tmp_path / "chats")
    project = store.create_project("secret")
    rules = {"providers": ["local"], "literature": False}
    store.set_rules(project["id"], rules)
    chat = store.create("local:test", project=project["id"])
    with pytest.raises(ValueError, match="confidential"):
        store.remember("global", "secret sample", "profile", project_id=project["id"], chat_id=chat["id"])
    chat["rules"] = rules
    store.save(chat)
    store.delete_project(project["id"])
    with pytest.raises(ValueError, match="confidential"):
        store.remember("global", "secret sample", "preference", chat_id=chat["id"])
    assert store.global_notes() == []


def test_old_global_notes_are_quarantined_when_their_source_becomes_confidential(tmp_path):
    store = ChatStore(tmp_path / "chats")
    project = store.create_project("work")
    chat = store.create("local:test", project=project["id"])
    store.remember("global", "sensitive result", "profile", chat_id=chat["id"])
    assert len(store.recallable_global_notes()) == 1
    store.set_rules(project["id"], {"providers": ["local"]})
    assert store.recallable_global_notes() == []
    assert len(store.global_notes()) == 1  # preserved for the person's review, not silently deleted


def test_queue_failure_reason_does_not_leak_after_the_other_items_are_removed(runtime):
    with datascope.use(ALPHA):
        runtime.queue.reason = "SECRET_ALPHA scan failed"
    with datascope.use(BETA):
        assert "SECRET_ALPHA" not in runtime.queue_show()
