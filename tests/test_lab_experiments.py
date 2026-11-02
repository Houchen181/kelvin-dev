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
