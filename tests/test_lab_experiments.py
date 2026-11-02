"""The lab's experiments (Plan 3): kept translations, every version kept and never changed, approved by name; the
tools that describe, translate, keep, edit and run them; auto mode and the queue covering an approved version."""
import pytest

from labharness import expstore

GOOD = '''
from kelvin import lab, action, record   # VI: n1
V_STOP = 1.0                             # VI: c10
@action("scan", main=True)
def scan():
    for v in [0.0, 0.5, V_STOP]:         # VI: n2
        lab.set("smu.V", v)              # VI: n3
        record(V=v, X=lab.get("lockin.X"))   # VI: n4, c11
'''


def kept(tmp_path):
    s = expstore.Store(tmp_path / "experiments")
    s.keep(s.draft(GOOD, {"vi": {"name": "x.vi"}}, by="agent"), "iv-scan", by="ana")
    return s


# ------------------------------------------------------------------ the store (Task 6)

def test_a_kept_draft_becomes_version_1_approved_by_whoever_kept_it(tmp_path):
    s = expstore.Store(tmp_path / "experiments")
    d = s.draft(GOOD, {"vi": {"name": "x.vi"}}, by="agent")
    assert s.draft_path(d).read_text(encoding="utf-8") == GOOD
    v = s.keep(d, "iv-scan", by="ana")
    assert (v.number, v.made_by, v.approved_by, v.approved) == (1, "agent", "ana", True)
    assert v.path.read_text(encoding="utf-8") == GOOD and v.translation() == {"vi": {"name": "x.vi"}}
    assert s.names() == ["iv-scan"] and s.has("iv-scan") and not s.has("other")
    with pytest.raises(KeyError):
        s.draft_path(d)                                  # a kept draft is gone from the drafts


def test_an_edit_is_a_new_version_not_approved_until_a_person_says_so(tmp_path):
    s = kept(tmp_path)
    v2 = s.add("iv-scan", GOOD.replace("0.5", "0.25"), {}, by="agent", note="finer", edited_from=1)
    assert (v2.number, v2.edited_from, v2.note, v2.approved) == (2, 1, "finer", False)
    assert s.get("iv-scan").number == 2 and s.get("iv-scan", 1).approved
    assert "-    for v in [0.0, 0.5" in s.diff("iv-scan", 1, 2) and "+    for v in [0.0, 0.25" in s.diff("iv-scan", 1, 2)
    assert s.approve("iv-scan", 2, "ben").approved_by == "ben" and s.get("iv-scan", 2).approved


def test_an_edit_of_an_older_version_is_refused(tmp_path):
    s = kept(tmp_path)
    s.add("iv-scan", GOOD + "\n", {}, by="agent", edited_from=1)
    with pytest.raises(ValueError, match="edit v2"):
        s.add("iv-scan", GOOD, {}, by="agent", edited_from=1)
    with pytest.raises(ValueError, match="new version"):
        s.add("iv-scan", GOOD, {}, by="agent")            # a kept name takes a version edited from its latest


def test_a_version_changed_on_disk_is_no_longer_approved(tmp_path):
    s = kept(tmp_path)
    s.get("iv-scan").path.write_bytes((GOOD + "import os\n").encode("utf-8"))
    v = s.get("iv-scan")
    assert not v.intact and not v.approved
    with pytest.raises(ValueError, match="changed"):
        s.approve("iv-scan", 1, "ana")


def test_a_version_is_written_whole_with_the_files_line_ends_as_given(tmp_path):
    s = kept(tmp_path)
    v = s.get("iv-scan")
    assert v.path.read_bytes() == GOOD.encode("utf-8") and v.intact      # LF kept: its fingerprint holds
    assert sorted(p.name for p in v.folder.iterdir()) == ["experiment.py", "translation.json", "version.json"]
    assert not [p for p in (tmp_path / "experiments" / "iv-scan").iterdir() if p.name.startswith(".")]


@pytest.mark.parametrize("bad", ["", "IV", "../x", "x" * 65, "a.py", ".drafts", "a b"])
def test_a_name_kelvin_cant_keep_is_said(tmp_path, bad):
    with pytest.raises(ValueError, match="name"):
        expstore.Store(tmp_path).add(bad, GOOD, {}, by="ana")


def test_an_unknown_experiment_or_version_is_said(tmp_path):
    s = kept(tmp_path)
    with pytest.raises(KeyError, match="no experiment"):
        s.get("nope")
    with pytest.raises(KeyError, match="no version 7"):
        s.get("iv-scan", 7)
    with pytest.raises(ValueError):
        s.draft_path("../../etc")


# ------------------------------------------------------------------ the tools: describe, translate, keep, edit, run (Task 7)

import re  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

from test_script_runs import _python, folder, lab, script_runtime, write  # noqa: E402,F401 (fixtures)
from test_translation import fenced, scripted  # noqa: E402

VI = Path(__file__).parent / "fixtures" / "labview" / "keysight_u2722" / "SMU_IV single.vi"
NOTES_K = {"inputs": {}, "not_translated": [], "columns": {"X": "X"}}
LAB_MAP = ('- {call: "Agilent U2722.lvlib:Configure Output.vi", becomes: card, card: virtual/source-meter, '
           'sets: [V, output]}\n')


def person(rt, answer=True):
    """A person who answers every card, keeping what each asked (its title and lines), until the test ends."""
    asked = []

    def run():
        while True:
            e = rt.approver.wait_for_pending(timeout=0.3)
            if e:
                asked.append(e["title"] + "\n" + "\n".join(e["items"]))
                rt.approver.answer(e["id"], answer)
    threading.Thread(target=run, daemon=True).start()
    return asked


def model_of(answer):
    return lambda purpose, attribution: scripted(answer if purpose == "translate" else '{"findings": []}')


@pytest.fixture
def rt_keysight(lab, folder, tmp_path):
    rt = script_runtime(lab, folder, tmp_path, timeout=30)
    rt.experiments = expstore.Store(tmp_path / "experiments")
    rt.labview_map = tmp_path / "labview-map.yaml"
    rt.labview_map.write_text(LAB_MAP, encoding="utf-8")
    rt.vi_model = model_of(fenced(GOOD, NOTES_K))
    return rt


def drafted(rt):
    said = rt.translate_vi(str(VI))
    m = re.search(r"draft (d[0-9]{8}-[0-9]{6}-[0-9a-f]{6})", said)
    assert m, said
    return m.group(1), said


@pytest.fixture
def rt_kept(rt_keysight):
    draft, _ = drafted(rt_keysight)
    rt_keysight.asked = person(rt_keysight)
    assert rt_keysight.keep_experiment(draft, "iv-scan").startswith("kept")
    return rt_keysight


def test_describe_vi_gives_the_description_as_text(rt_keysight):
    said = rt_keysight.describe_vi(str(VI))
    assert "while loop (stops when" in said and 'VI "SMU_IV single.vi"' in said


def test_translate_keep_and_run_an_experiment_by_name(rt_keysight):
    rt = rt_keysight
    draft, said = drafted(rt)
    assert "keep_experiment" in said and "not mentioned by the translation" in said      # its warnings are said
    asked = person(rt)
    assert rt.keep_experiment(draft, "iv-scan").startswith("kept")
    assert "Keep iv-scan in the lab's experiments?" in asked[0] and "SMU_IV single.vi" in asked[0]
    v = rt.experiments.get("iv-scan")
    assert v.approved and v.translation()["vi"]["name"] == "SMU_IV single.vi" and v.translation()["lines"]["n3"]
    assert "done" in rt.run_script("iv-scan"), asked
    assert "version 1 of the lab's experiment iv-scan" in asked[-1]


def test_keep_is_refused_while_something_blocks(rt_keysight):
    rt = rt_keysight
    rt.vi_model = model_of(fenced(GOOD + "import os\n", NOTES_K))
    draft, said = drafted(rt)
    assert "imports os" in said
    assert rt.keep_experiment(draft, "iv-scan").startswith("not kept") and not rt.experiments.has("iv-scan")


def test_a_person_who_says_no_keeps_nothing(rt_keysight):
    draft, _ = drafted(rt_keysight)
    person(rt_keysight, answer=False)
    assert rt_keysight.keep_experiment(draft, "iv-scan").startswith("not kept")
    assert not rt_keysight.experiments.has("iv-scan")


def test_an_agent_edit_is_a_new_version_and_its_first_run_asks_with_the_diff(rt_kept):
    rt = rt_kept
    said = rt.save_experiment("iv-scan", GOOD.replace("0.5", "0.25"), note="finer")
    assert "version 2" in said and "not approved" in said, said
    assert "done" in rt.run_script("iv-scan")
    assert "version 2 of the lab's experiment iv-scan is new" in rt.asked[-1] and "0.25" in rt.asked[-1]
    assert rt.experiments.get("iv-scan", 2).approved


def test_an_edit_that_blocks_is_not_saved(rt_kept):
    said = rt_kept.save_experiment("iv-scan", GOOD + "import os\n")
    assert said.startswith("not saved") and "imports os" in said and rt_kept.experiments.get("iv-scan").number == 1


def test_a_run_of_a_version_runs_that_version_again(rt_kept):
    rt = rt_kept
    rt.save_experiment("iv-scan", GOOD.replace("0.5", "0.25"))
    v1 = rt.experiments.get("iv-scan", 1)
    assert rt._prepare_script(str(v1.path)).path == v1.path          # Run again of v1's run: v1, as it ran
    assert rt._prepare_script("iv-scan").path == rt.experiments.get("iv-scan", 2).path


def test_the_labs_experiments_are_everyones(rt_kept, lab, folder, tmp_path):
    other = script_runtime(lab, folder, tmp_path / "ben", timeout=30)   # another account, the same lab
    other.user = "ben"
    other.experiments = expstore.Store(rt_kept.experiments.root)
    person(other)
    assert "iv-scan" in other.experiments_view() and "done" in other.run_script("iv-scan")


def test_an_experiments_view_says_its_versions_actions_and_checks(rt_kept):
    said = rt_kept.experiments_view("iv-scan")
    assert "version 1" in said and "scan (main)" in said and "SMU_IV single.vi" in said


def test_a_new_experiment_an_agent_writes_itself_is_checked_and_saved_as_a_draft(rt_keysight):
    said = rt_keysight.save_experiment("iv-two", GOOD, notes=NOTES_K, vi=str(VI))
    assert re.search(r"draft d[0-9]{8}", said) and "keep_experiment" in said, said
    assert rt_keysight.save_experiment("iv-two", GOOD).startswith("not saved")    # no VI to check it against


def test_without_a_model_translate_vi_says_how_an_agent_does_it_itself(rt_keysight):
    rt_keysight.vi_model = None
    said = rt_keysight.translate_vi(str(VI))
    assert "describe_vi" in said and "save_experiment" in said


def test_the_new_tools_are_grouped_for_the_chat():
    pytest.importorskip("mcp")
    from labharness.web.agent import DEFERRED
    assert set(DEFERRED["translating LabVIEW into experiments"]) == {
        "describe_vi", "translate_vi", "save_experiment", "keep_experiment", "experiments"}


def test_a_projects_rules_keep_the_vi_from_a_model_they_exclude(lab, folder, tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    from test_web import Scripted

    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    model = Scripted([[{"type": "text", "text": fenced(GOOD, NOTES_K)}]])
    rt = script_runtime(lab, folder, tmp_path, timeout=30)
    settings = Settings(tmp_path / "settings.json")
    store = ChatStore(tmp_path / "chats")
    app = create_app(rt, settings, store, hosts=["testserver"], providers={"scripted": model})
    rt.experiments = expstore.Store(tmp_path / "experiments")
    h = {"X-Kelvin-Token": settings.access_key}
    with TestClient(app) as client:
        p = client.post("/api/projects", headers=h, json={"name": "Acme NDA"}).json()
        client.patch("/api/projects/" + p["id"], headers=h, json={"rules": {"providers": ["local"]}})
        chat = client.post("/api/chats", headers=h, json={"model": "scripted:m1", "project": p["id"]}).json()
        with rt.attributed({"chat": chat["id"], "project": p["id"], "rules": {"providers": ["local"]}}):
            said = rt.translate_vi(str(VI))
    assert said.startswith("not translated") and "not allowed" in said, said
    assert len(model.turns) == 1                                   # nothing was sent to the model
