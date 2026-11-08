"""What came of runs, kept with where it came from, and the runs packaged for others (phases 4-6 of the ELN plan,
2026-11-07): a saved figure has a record naming its runs by uuid and seal and is drawn again from raw data; a kept
fit names Kelvin's version and the AI; run.h5 carries NeXus's view; an RO-Crate packages runs with their seals,
samples, procedures, figures and entries; one search finds anything kept; a digest by code."""
import json
import os
import re
import stat
import zipfile

import pytest

from labharness import datatools, derivations, fitting, rundata
from labharness._version import __version__

pytest.importorskip("mcp")
h5py = pytest.importorskip("h5py")

GATE = ("plan: gate\nsetup: {lockin.amplitude: 0.05, smu.output: on}\nsweep: {smu.V: {from: 0, to: 3, points: 61}}\n"
        "settle: 0.5\nmeasure: [lockin.X, smu.I]\nafter: {smu.V: 0}")


@pytest.fixture
def rt(tmp_path):
    from labharness import ScriptedApprover
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab
    r = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 50), tmp_path / "data")
    r.home = tmp_path
    return r


def gate_run(rt):
    rt.run(GATE)
    return rt.pick_runs(last=1)[-1]


def test_run_h5_carries_nexus_s_view_without_copying_anything(rt):
    run = gate_run(rt)
    with h5py.File(str(rt._run_dir(run) / "run.h5"), "r") as f:
        assert f.attrs["default"] == "entry" and f["entry"].attrs["NX_class"] == "NXentry"
        data = f["entry/data"]
        assert data.attrs["NX_class"] == "NXdata" and data.attrs["signal"] == "lockin.X"
        assert list(data.attrs["axes"]) == ["smu.V"]
        assert isinstance(data.get("lockin.X", getlink=True), h5py.SoftLink)
        assert (data["lockin.X"][()] == f["points/lockin.X"][()]).all() and f["points/smu.V"].attrs["units"] == "V"
        assert f["entry/entry_identifier"][()].decode() == rundata.meta(rt._run_dir(run))["uuid"]
        assert f["entry/instrument"].attrs["NX_class"] == "NXinstrument" and "settings_at_start" in f["entry/instrument"]
    assert len(rundata.load(rt._run_dir(run)).rows) == 61   # Kelvin reads it as before


def test_a_kept_fit_names_its_versions_and_ai(rt):
    from labharness.agent.runtime import Viewer
    run = gate_run(rt)
    with rt.viewing(Viewer(chat="c1", model="anthropic:claude-opus-5-5")):
        out = datatools.fit_run(rt, run, "lorentzian", y="lockin.X", name="peak")
    said = out[-1] if isinstance(out, tuple) else out
    assert "derived.h5 as /fits/peak" in said, said
    kept = fitting.stored(rt._run_dir(run))[0]
    assert kept["versions"]["kelvin"] == __version__ and "numpy" in kept["versions"]
    assert kept["ai"] == {"model": "anthropic:claude-opus-5-5", "chat": "c1"}


def test_a_saved_figure_has_its_record_and_is_drawn_again_from_raw_data(rt):
    run = gate_run(rt)
    png, text = datatools.plot_runs(rt, [{"run": "last", "y": "lockin.X"}], title="the peak", name="peak")
    assert "\nits record: " in text
    tag = derivations.png_tag(png)
    folder = rt.data_dir / "figures"
    path, record = derivations.find_figure(folder, tag.split()[1])
    assert tag.split()[0] == record["id"] and record["spec"]["curves"][0]["run"] == run   # "last" as the run it was
    assert record["runs"] == [{"run": run, "uuid": rundata.meta(rt._run_dir(run))["uuid"],
                               "sha256": rundata.sealed(rt._run_dir(run))}]
    assert derivations.find_figure(folder, record["id"])[0] == path   # by its id too
    png2, said = datatools.rebuild_figure(rt, path.stem)
    assert said.startswith("rebuilt %s from 1 run, each as it was sealed" % path.stem) and png2
    h5 = rt._run_dir(run) / "run.h5"
    os.chmod(str(h5), stat.S_IREAD | stat.S_IWRITE)
    with open(h5, "ab") as f:
        f.write(b"\0")
    with pytest.raises(datatools.ToolError, match="not rebuilt: run %s changed since it was sealed" % run):
        datatools.rebuild_figure(rt, path.stem)


def test_an_ro_crate_packages_runs_with_their_seals_samples_procedures_and_entries(rt):
    run = gate_run(rt)
    rt.update_sample("D3", kind="device", parent="chip 2")
    rt.user = "ann"
    eid = rt.write_entry("Gate sweep", "Swept to 3 V [run:%s]." % run).split()[2]
    rt.sign_entry(eid)
    said = rt.export_crate(entries=[eid], samples=["D3"], title="The peak", license="CC-BY-4.0",
                           orcid="0000-0002-1825-0097")
    assert said.startswith("wrote ") and "1 run, described in ro-crate-metadata.json" in said, said
    zpath = re.match(r"wrote (.+\.zip): ", said).group(1)
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        meta = json.loads(z.read("ro-crate-metadata.json"))
        assert z.read("runs/%s/run.h5" % run) == (rt._run_dir(run) / "run.h5").read_bytes()
    assert {"runs/%s/run.h5" % run, "runs/%s/run.h5.sha256" % run, "notebook/%s.json" % eid, "samples/D3.json"} <= names
    graph = {g["@id"]: g for g in meta["@graph"]}
    root = graph["./"]
    assert root["name"] == "The peak" and root["license"] == {"@id": "https://creativecommons.org/licenses/by/4.0/"}
    assert root["author"] == {"@id": "https://orcid.org/0000-0002-1825-0097"}
    f = graph["runs/%s/run.h5" % run]
    assert f["sha256"] == rundata.sealed(rt._run_dir(run)) and f["identifier"].startswith("urn:uuid:")
    assert {"name": "smu.V", "unitText": "V", "unitCode": "V", "@type": "PropertyValue"} in f["variableMeasured"]
    action = graph["#run-%s" % run]
    assert action["@type"] == "CreateAction" and action["result"] == {"@id": "runs/%s/run.h5" % run}
    assert graph["notebook/%s.json" % eid]["author"] == [{"@id": "https://orcid.org/0000-0002-1825-0097"}]
    assert graph["#sample-D3"]["isBasedOn"] == {"@id": "#sample-chip 2"}
    assert not (rt.data_dir / "exports" / zpath.split(os.sep)[-1].replace(".zip", "")).exists()   # only the zip
    assert rt.export_crate().startswith("not written: name the runs")


def test_one_search_finds_anything_kept_and_a_digest_by_code(rt):
    run = gate_run(rt)
    rt.annotate(run, flag="key", text="the peak at 1.5 V, Fig. 3")
    rt.record_step("wire-bonded pins 1-8 on chip 2", sample="D3")
    rt.update_sample("chip 2", kind="chip", location="fridge 2")
    rt.write_entry("Bonding day", "Bonded chip 2.")
    found = rt.search_records("chip 2")
    assert "[steps]" in found and "[samples] chip 2 (chip, fridge 2)" in found and "[entries]" in found, found
    assert "[runs] %s: gate, done, flagged key" % run in rt.search_records("Fig. 3")
    assert rt.search_records("chip 2", kinds="samples").count("\n") == 1
    assert rt.search_records("zzz") == "nothing found for 'zzz'"
    d = rt.digest("1d")
    assert "1 run (1 done)" in d and "key [run:%s]" % run in d and "1 notebook entry, 1 still to sign" in d, d
    assert "1 step by hand and observation recorded" in d
