"""A lab's LabVIEW read to switch it to Kelvin directly (labview.py): NI MAX's aliases and resources, its projects,
the VI summary and its translation into a scan (viscan.py), the scans a person keeps, and LabVIEW's .lvm data files.
Every input here is written as its format is documented or as LabVIEW writes it; nothing was downloaded."""
import json
import os
import time

import pytest

from labharness import labview as lv
from labharness import imports, viscan
from labharness.homelab import builtin_registry
from labharness.imports import DataError
from labharness.plans import load_plan
from labharness.scanlibrary import Library, plan_id

VISACONF = """; NI-VISA configuration, as NI MAX writes it
[ALIASES]
NumAliases = 4
Alias0 = "'ASRL1::INSTR','COM1'"
Alias1 = "'GPIB0::24::INSTR','SMU'"
Alias2 = "'Lock-in SR830','GPIB0::8::INSTR'"
Alias3 = "'nothing here'"
Magnet=TCPIP0::192.168.1.30::7180::SOCKET

[TCPIP-RSRC]
NumOfResources = 1
Resource0 = "TCPIP0::192.168.1.5::inst0::INSTR"
"""
DAQMX = """[DAQmx]
MajorVersion = 18

[DAQmxChannel Gate/Voltage]
PhysicalChanName = Dev1/ao0

[DAQmxDevice Dev1]
ProductType = USB-6211
DevSerialNum = 0x1A2B3C4

[DAQmxCDAQChassis cDAQ1]
ProductType = cDAQ-9178
"""
PROJECT = """<?xml version='1.0' encoding='UTF-8'?>
<Project Type="Project" LVVersion="15008000">
\t<Item Name="My Computer" Type="My Computer">
\t\t<Property Name="server.app.propertiesEnabled" Type="Bool">true</Property>
\t\t<Item Name="Main.vi" Type="VI" URL="../Main.vi"/>
\t\t<Item Name="Measurements" Type="Folder">
\t\t\t<Item Name="IV sweep.vi" Type="VI" URL="../Measurements/IV sweep.vi"/>
\t\t</Item>
\t\t<Item Name="Lab Utilities.lvlib" Type="Library" URL="../Lab Utilities.lvlib"/>
\t\t<Item Name="Dependencies" Type="Dependencies">
\t\t\t<Item Name="instr.lib" Type="Folder">
\t\t\t\t<Item Name="Keithley 2400 Series.lvlib" Type="Library" URL="/&lt;instrlib&gt;/Keithley 2400 Series/Keithley 2400 Series.lvlib"/>
\t\t\t</Item>
\t\t\t<Item Name="vi.lib" Type="Folder">
\t\t\t\t<Item Name="DAQmx Read.vi" Type="VI" URL="/&lt;vilib&gt;/DAQmx/read.llb/DAQmx Read.vi"/>
\t\t\t\t<Item Name="NI_FileType.lvlib" Type="Library" URL="/&lt;vilib&gt;/Utility/lvfile.llb/NI_FileType.lvlib"/>
\t\t\t</Item>
\t\t\t<Item Name="lakeshore.dll" Type="Document" URL="../bin/lakeshore.dll"/>
\t\t</Item>
\t\t<Item Name="Build Specifications" Type="Build"/>
\t</Item>
\t<Item Name="cRIO-9068" Type="RT CompactRIO">
\t\t<Property Name="alias.name" Type="Str">cRIO-9068</Property>
\t\t<Property Name="alias.value" Type="Str">192.168.1.20</Property>
\t\t<Item Name="Chassis" Type="cRIO Chassis">
\t\t\t<Property Name="crio.Type" Type="Str">cRIO-9068</Property>
\t\t\t<Item Name="Mod1" Type="RIO C Series Module">
\t\t\t\t<Property Name="crio.Type" Type="Str">NI 9215</Property>
\t\t\t</Item>
\t\t</Item>
\t\t<Item Name="RT Main.vi" Type="VI" URL="../RT/RT Main.vi"/>
\t</Item>
</Project>
"""


@pytest.fixture(scope="module")
def cards():
    reg = builtin_registry()
    return {"smu": reg.get("keithley/2400"), "lockin": reg.get("srs/sr830"), "daq": reg.get("ni/daqmx"),
            "registry": reg}


def lab_of(cards, **at):
    """The instruments a translation names: {address_key(address): (name, card)}."""
    return {lv.address_key(address): (name, cards[name]) for name, address in at.items()}


# ------------------------------------------------------------------ NI MAX

def test_ni_max_aliases_and_resources_in_either_form(tmp_path):
    conf = lv.read_visaconf(VISACONF)
    assert conf["aliases"] == {"SMU": "GPIB0::24::INSTR", "Lock-in SR830": "GPIB0::8::INSTR",
                               "Magnet": "TCPIP0::192.168.1.30::7180::SOCKET"}   # by form, never by place
    assert conf["ports"] == {"COM1": "ASRL1::INSTR"}                 # NI-VISA's own name for a port: no instrument
    assert "TCPIP0::192.168.1.5::inst0::INSTR" in conf["resources"] and "ASRL1::INSTR" in conf["resources"]
    assert conf["unread"] == ["visaconf.ini line 7"]                  # an alias line with no address in it

    (tmp_path / "visaconf.ini").write_bytes(VISACONF.encode("utf-16"))   # NI's file may be UTF-16
    (tmp_path / "daq.ini").write_text(DAQMX, encoding="utf-8")
    (tmp_path / "other.ini").write_text("[Settings]\nx=1\n", encoding="utf-8")
    m = lv.read_max([tmp_path / "missing.ini", tmp_path / "visaconf.ini"],
                    [tmp_path / "daq.ini", tmp_path / "other.ini"])
    assert m["files"] == [str(tmp_path / "visaconf.ini"), str(tmp_path / "daq.ini")]
    assert m["aliases"]["SMU"] == "GPIB0::24::INSTR"
    assert m["daqmx"] == [{"name": "Dev1", "model": "USB-6211"}]      # a cDAQ chassis holds devices but is none


# ------------------------------------------------------------------ LabVIEW projects

def test_a_project_lists_its_vis_libraries_dependencies_and_targets(tmp_path):
    (tmp_path / "Measurements").mkdir()
    (tmp_path / "Main.vi").write_bytes(b"RSRC")
    (tmp_path / "Measurements" / "IV sweep.vi").write_bytes(b"RSRC")
    (tmp_path / "Lab.lvproj").write_text(PROJECT, encoding="utf-8")
    p = lv.read_project(tmp_path / "Lab.lvproj")
    assert p["problem"] == "" and p["labview"] == "LabVIEW 2015 (15.0)"
    vis = {v["name"]: v for v in p["vis"]}
    assert vis["Main.vi"]["path"] == str(tmp_path / "Main.vi")          # relative to the project file itself
    assert vis["IV sweep.vi"]["path"] == str(tmp_path / "Measurements" / "IV sweep.vi") and not vis["Main.vi"]["target"]
    assert vis["RT Main.vi"]["target"] == "cRIO-9068"
    assert [lib["name"] for lib in p["libraries"]] == ["Lab Utilities.lvlib"]
    deps = {d["name"]: d for d in p["dependencies"]}
    assert deps["Keithley 2400 Series.lvlib"]["where"] == "instr.lib" and deps["DAQmx Read.vi"]["where"] == "vi.lib"
    assert p["drivers"] == [{"library": "Keithley 2400 Series.lvlib", "maker": "Keithley", "model": "2400",
                             "where": "instr.lib"}]                   # LabVIEW's own (vi.lib) are no drivers
    assert p["uses"] == ["NI-DAQmx", "lakeshore.dll"]
    assert p["targets"] == [{"name": "cRIO-9068", "type": "RT CompactRIO", "address": "192.168.1.20",
                             "modules": [{"name": "Mod1", "model": "NI 9215"}]}]   # its chassis is no module

    for text, why in [('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Project>&a;</Project>', "isn't a"),
                      ("<Library LVVersion='15008000'/>", "isn't a LabVIEW project"), ("<Project>", "well-formed")]:
        (tmp_path / "bad.lvproj").write_text(text, encoding="utf-8")
        assert why in lv.read_project(tmp_path / "bad.lvproj")["problem"]


def test_a_projects_items_outside_its_folder_are_never_looked_at(tmp_path, monkeypatch):
    """Another computer's share (on Windows, looking at one logs on to it), an absolute path, or one leading out of
    the folder being read: told from the URL's text, never looked at, and said."""
    lab = tmp_path / "lab"
    lab.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "secret.vi").write_bytes(b"RSRC")
    (lab / "Main.vi").write_bytes(b"RSRC")
    items = "".join('<Item Name="%s" Type="VI" URL="%s"/>' % (n, u) for n, u in (
        ("Main.vi", "../Main.vi"), ("share.vi", "//kelvin-test.invalid/share/a.vi"), ("abs.vi", "/C/Lab/b.vi"),
        ("up.vi", "../../elsewhere/secret.vi"), ("drive.vi", "C:/elsewhere/secret.vi"),
        ("back.vi", "..\\..\\elsewhere\\secret.vi"), ("com.vi", "../COM3.vi")))
    (lab / "p.lvproj").write_text('<?xml version="1.0"?><Project LVVersion="22308000"><Item Name="My Computer" '
                                  'Type="My Computer">%s</Item></Project>' % items, encoding="utf-8")
    looked = []
    for name in ("stat", "lstat"):
        real = getattr(os, name)
        monkeypatch.setattr(os, name, lambda p, *a, _real=real, **k: looked.append(str(p)) or _real(p, *a, **k))
    p = lv.read_project(lab / "p.lvproj", lab)
    monkeypatch.undo()
    assert {v["name"]: v["path"] for v in p["vis"]} == {"Main.vi": str(lab / "Main.vi"), "share.vi": "", "abs.vi": "",
                                                        "up.vi": "", "drive.vi": "", "back.vi": "", "com.vi": ""}
    assert p["outside"] == ["share.vi", "abs.vi", "up.vi", "drive.vi", "back.vi", "com.vi"]
    assert not [x for x in looked if "kelvin-test" in x or "elsewhere" in x or "COM3" in x]
    found = lv.survey(lab, visaconf=tmp_path / "none.ini", read=lambda f: lv.vi(f))
    assert [s["name"] for s in found.vis] == ["Main.vi"]
    assert "not looked at (outside %s, or on another computer): share.vi, abs.vi" % lab in found.text()


def test_a_project_is_read_whatever_its_depth_and_never_with_a_document_type(tmp_path):
    """Folders nested 1500 deep are read (without recursion); a document type, whatever the file's encoding (a
    UTF-16 one hid it from a search of its bytes), is refused before any entity in it is used."""
    body = '<Item Name="f" Type="Folder">' * 1500 + '<Item Name="a.vi" Type="VI" URL="../a.vi"/>' + "</Item>" * 1500
    (tmp_path / "deep.lvproj").write_text('<Project LVVersion="22308000"><Item Name="My Computer" Type="My Computer">'
                                          '%s</Item></Project>' % body, encoding="utf-8")
    assert [v["name"] for v in lv.read_project(tmp_path / "deep.lvproj")["vis"]] == ["a.vi"]
    entities = '<!ENTITY a "x">' + "".join('<!ENTITY a%d "%s">' % (i, "&a;" * 10) for i in range(8))
    for text in ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE Project [<!ENTITY a "Entity.vi">]>'
                 '<Project><Item Name="My Computer" Type="My Computer"><Item Name="&a;" Type="VI" URL="../x.vi"/>'
                 '</Item></Project>', '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE p [%s]><Project/>' % entities):
        (tmp_path / "typed.lvproj").write_bytes(text.encode("utf-16"))
        p = lv.read_project(tmp_path / "typed.lvproj")
        assert p["problem"] == "not read: it isn't a LabVIEW project (it declares a document type)" and p["vis"] == []


def test_one_file_that_trips_the_survey_up_is_said_and_the_survey_goes_on(tmp_path, cards, monkeypatch):
    """A loop whose count is +Inf, a project the reader raises on, an NI MAX file too big for one: each said in its
    own place, and the survey goes on (it was an error for the whole survey)."""
    monkeypatch.setattr(lv, "VISACONF", tmp_path / "no NI-VISA here" / "visaconf.ini")
    root = lab_folder(tmp_path)
    (root / "Bad.lvproj").write_text(PROJECT, encoding="utf-8")
    (root / "visaconf.ini").write_text(VISACONF + ";" * 50, encoding="utf-8")
    monkeypatch.setattr(lv, "MAX_INI_BYTES", len(VISACONF) + 10)
    real = lv.read_project
    monkeypatch.setattr(lv, "read_project", lambda f, folder=None: real(f, folder) if f.name != "Bad.lvproj" else
                        1 / 0)
    infinite = lv.loop("i", [lv.node("write", resource="GPIB0::24::INSTR", text=":SOUR:VOLT 1")],
                       count={"value": float("inf")})

    def reader(f):
        if f.name == "Main.vi":
            raise RuntimeError("the reader tripped")
        return lv.vi(f, read="full", diagram=[infinite])
    found = lv.survey(root, cards["registry"], read=reader)
    assert [p["problem"] for p in found.projects] == ["not read: division by zero", ""]
    assert found.max["files"] == [str(root / "max export.ini")] and "bigger than" in found.max["unread"][0]
    assert {s["name"]: s["unread"] for s in found.vis}["Main.vi"] == ["not read: the reader tripped"]
    scans = found.translations({lv.address_key("GPIB0::24::INSTR"): ("smu", cards["smu"])})
    t = scans[str(root / "Measurements" / "IV sweep.vi")]
    assert t["plan"] is None and t["gaps"] == ["For loop over i: how many times it runs isn't in the summary"]
    found.view(), found.text()


def test_a_loops_values_are_never_made_past_what_a_scan_takes(tmp_path):
    """range_values counts a loop's values before it makes them: none for a number that isn't finite or for more
    than a plan takes (plans.MAX_POINTS); the survey's walk takes a long loop's first value and extremes only."""
    inf, nan = float("inf"), float("nan")
    for rng in ({"count": {"value": inf}}, {"count": {"value": nan}}, {"count": {"value": 10 ** 9}},
                {"from": {"value": 0}, "to": {"value": inf}, "step": {"value": 1}},
                {"from": {"value": 0}, "to": {"value": 1}, "step": {"value": nan}},
                {"from": {"value": 0}, "to": {"value": 1e12}, "step": {"value": 1}},
                {"from": {"value": 0}, "to": {"value": 1}, "points": {"value": 10 ** 12}},
                {"from": {"value": 0}, "to": {"value": 1}, "points": {"value": 1e300}},
                {"from": {"value": 1e308}, "to": {"value": -1e308}, "step": {"value": 1}},
                {"values": {"value": [0, inf]}},
                {"from": {"control": "Start"}, "to": {"value": 1}, "step": {"value": 1}}):
        t0 = time.perf_counter()
        assert lv.range_values(rng, {"Start": lv.control("Start", inf)}) == [], rng
        assert time.perf_counter() - t0 < 1
    assert lv.range_values({"count": {"value": 10 ** 6}}, {}, most=1000) == [0.0, 0.0, 999999.0]
    assert lv.range_values({"from": {"value": 5}, "to": {"value": -5}, "points": {"value": 10 ** 6}}, {},
                           most=1000) == [5.0, -5.0, 5.0]
    assert lv.range_values({"from": {"value": 0}, "to": {"value": 1}, "step": {"value": 0.5}}, {}, most=1000) == \
        [0.0, 0.5, 1.0]
    assert lv.value_of({"value": 1e308, "scale": 10}, {}) is None


def test_driver_names_give_their_maker_and_model_and_nothing_more():
    assert lv.driver_of("Keithley 2400 Series.lvlib:Configure Source.vi") == {
        "library": "Keithley 2400 Series.lvlib", "maker": "Keithley", "model": "2400", "where": ""}
    assert lv.driver_of("SR830.llb")["model"] == "SR830" and lv.driver_of("SR830.llb")["maker"] == ""
    assert lv.driver_of("Agilent_34401A.lvlib")["model"] == "34401A"
    assert lv.driver_of("Lake Shore 336.lvlib", "instr.lib")["maker"] == "Lake Shore"
    assert lv.driver_of("Tektronix TDS 2000 Series.lvlib")["model"] == "TDS 2000"
    assert lv.driver_of("Keithley 24XX.lvlib")["model"] == "24XX"            # said as it is: no card will claim it
    assert lv.driver_of("Thorlabs Driver.lvlib", "instr.lib") == {
        "library": "Thorlabs Driver.lvlib", "maker": "Thorlabs", "model": "", "where": "instr.lib"}
    assert lv.driver_of("NI_FileType.lvlib", "vi.lib") is None              # LabVIEW's own
    assert lv.driver_of("Lab Utilities.lvlib") is None and lv.driver_of("Cryostat 2019 control.lvlib") is None
    assert [lv.lv_version(v) for v in ("15008000", "8608001", "23308000", "12008004", "x")] == [
        "LabVIEW 2015 (15.0)", "LabVIEW 8.6", "LabVIEW 23.3", "LabVIEW 2012 (12.0)", ""]


# ------------------------------------------------------------------ the VI summary, and a VI as a scan

def iv_sweep(path="C:/Lab/IV sweep.vi", resource="SMU"):
    """An I-V sweep as LabVIEW labs write it: reset, compliance and output on, a For loop from Start to Stop setting
    the voltage, a wait, a reading, a spreadsheet file; output off at the end."""
    k = lv.ctl("VISA resource")
    return lv.vi(path, read="full", labview="LabVIEW 2015 (15.0)", controls=[
        lv.control("Start (V)", 0.0), lv.control("Stop (V)", 1.0), lv.control("Step (V)", 0.1),
        lv.control("Compliance (A)", 1e-6), lv.control("Delay (ms)", 200),
        lv.control("VISA resource", resource, type="visa"), lv.control("File", "C:/data/iv.txt", type="path"),
        lv.control("Current", None, type="number", indicator=True)],
        linkage=[lv.link("Simple Error Handler.vi", "vi.lib")],
        visa=[{"resource": resource, "control": "VISA resource"}],
        diagram=[
            lv.node("write", resource=k, text="*RST"),
            lv.node("sequence", frames=[[lv.node("write", resource=k, text=":SENS:CURR:PROT {c}",
                                                 values={"c": lv.ctl("Compliance (A)")})],
                                        [lv.node("write", resource=k, text=":OUTP ON")]]),
            lv.loop("V", [
                lv.node("write", resource=k, text=":SOUR:VOLT {V}", values={"V": lv.at("V")}),
                lv.node("wait", ms=lv.ctl("Delay (ms)")),
                lv.node("query", resource=k, text=":READ?", into="I"),
                lv.node("file", kind="spreadsheet", columns=[{"name": "V", "from": lv.at("V")},
                                                             {"name": "I", "from": lv.reading("I")}]),
            ], start="Start (V)", stop="Stop (V)", step="Step (V)"),
            lv.node("write", resource=k, text=":OUTP OFF"),
            lv.node("call", vi="Simple Error Handler.vi"),
        ])


def test_a_vi_summary_is_plain_json_and_checked():
    s = iv_sweep()
    assert json.loads(json.dumps(s)) == s and lv.validate(s) == []
    assert s["controls"][0] == {"name": "Start (V)", "type": "number", "unit": "V", "default": 0.0, "choices": [],
                                "indicator": False}
    assert lv.vi("C:/Lab/Main.vi")["unread"] == ["not read yet"]
    gone = lv.read_vi("C:/Lab/Main.vi")   # the real reader (vigraph.py): a file it can't read says why
    assert gone["read"] == "no" and gone["unread"][0].startswith("not read: it can't be read") and \
        lv.validate(gone) == []
    assert lv.range_values(s["diagram"][2]["range"], lv.controls_of(s))[-1] == 1.0
    assert len(lv.range_values(s["diagram"][2]["range"], lv.controls_of(s))) == 11
    bad = dict(s, summary=2, read="most", controls=[{"name": "x"}],
               diagram=[{"node": "loop", "kind": "until"}, {"node": "write"}, {"node": "wait", "ms": 5},
                        {"node": "teleport"}])
    problems = lv.validate(bad)
    assert problems[:3] == ["summary: 2 is not layout 1", "read: one of no, names, panel, full",
                            "controls[0]: a control has a name and a type (array, bool, cluster, enum, number, other, "
                            "path, ring, string, visa)"]
    assert any("a loop has kind" in p for p in problems) and any("has its command" in p for p in problems)
    assert any("diagram[2].ms: a value is one of" in p for p in problems)
    assert any("diagram[3]: a node is one of" in p for p in problems)
    assert lv.validate("x") == ["a VI's summary is a mapping"]


def test_an_iv_sweep_vi_becomes_a_scan_with_each_part_said(cards):
    lab = lab_of(cards, smu="GPIB0::24::INSTR")
    t = viscan.translate(iv_sweep(), lab, {"SMU": "GPIB0::24::INSTR"})
    assert t["plan"] == {"plan": "iv_sweep", "description": "From LabVIEW: IV sweep.vi",
                         "setup": {"smu.compliance_I": 1e-6, "smu.output": True},
                         "sweep": {"smu.V": {"from": 0.0, "to": 1.0, "step": 0.1, "settle": 0.2}},
                         "measure": ["smu.I", "smu.V_meas"], "after": {"smu.output": False}}
    load_plan(t["text"])                                                 # Kelvin's own check of a plan, from its text
    assert t["summary"] == "smu.V 0 → 1 V, 11 points · smu.I, smu.V_meas" and t["title"] == "IV sweep"
    said = {m["vi"]: m["plan"] for m in t["mapping"]}
    assert said["For loop over V (from Start (V) = 0 to Stop (V) = 1, step Step (V) = 0.1)"] == \
        "sweep: smu.V: {from: 0, to: 1, step: 0.1}"
    assert said["VISA write to SMU: :SENS:CURR:PROT {c}"] == "setup: smu.compliance_I: 1e-06"
    assert said["VISA write to SMU: :OUTP ON"] == "setup: smu.output: on"
    assert said["Wait Delay (ms) = 200 ms"] == "sweep: smu.V: settle: 0.2 s"
    assert said["VISA query to SMU: :READ?"] == "measure: smu.I, smu.V_meas (one reply holds them)"
    assert said["writes a spreadsheet file"] == \
        "Kelvin records every point (run.h5): V is smu.V, I is one of smu.I and smu.V_meas"
    assert said["LabVIEW's Simple Error Handler.vi"] == "nothing for the scan"
    assert said["controls File"] == "not used by the scan"               # the indicator and the resource aren't
    assert t["gaps"] == ["VISA write to SMU: *RST: a reset, which a scan never sends"]
    assert viscan.translate(iv_sweep(), lab, {"SMU": "GPIB0::24::INSTR"}) == t   # the same every time

    t = viscan.translate(iv_sweep(), {}, {"SMU": "GPIB0::24::INSTR"})     # the instrument isn't in the lab
    assert t["plan"] is None and "VISA write to SMU: :SOUR:VOLT {V}: no instrument of the lab is at SMU" in t["gaps"]
    t = viscan.translate(iv_sweep(resource="GPIB0::24::INSTR"), lab)        # an address needs no alias
    assert t["plan"]["sweep"] == {"smu.V": {"from": 0.0, "to": 1.0, "step": 0.1, "settle": 0.2}}
    t = viscan.translate(iv_sweep(resource="Keithley"), lab, {})            # an alias NI MAX doesn't know
    assert t["plan"] is None and any("no instrument of the lab is at Keithley" in g for g in t["gaps"])


def test_nested_loops_repeats_and_time_series(cards):
    lab = lab_of(cards, smu="GPIB0::24::INSTR", lockin="GPIB0::8::INSTR")
    gate = lv.loop("V", [lv.node("write", resource="GPIB0::24::INSTR", text=":SOUR:VOLT {V}",
                                 values={"V": lv.at("V", scale=0.001)}),     # a mV control, sent in V
                         lv.node("query", resource="GPIB0::8::INSTR", text="OUTP? 1", into="X")],
                   start="Start (mV)", stop="Stop (mV)", points=5)
    amp = lv.loop("A", [lv.node("write", resource="GPIB0::8::INSTR", text="SLVL {a}", values={"a": lv.at("A")}),
                        lv.node("wait", s=lv.const(2)), gate], values="Amplitudes (V)")
    s = lv.vi("C:/Lab/map.vi", read="full", controls=[lv.control("Start (mV)", -500), lv.control("Stop (mV)", 500),
                                                      lv.control("Amplitudes (V)", [0.004, 0.1]),
                                                      lv.control("Repeats", 3)],
              diagram=[lv.loop("n", [amp], count="Repeats")])
    t = viscan.translate(s, lab)
    assert t["plan"]["repeat"] == 3 and list(t["plan"]["sweep"]) == ["lockin.amplitude", "smu.V"]   # outer first
    assert t["plan"]["sweep"]["lockin.amplitude"] == {"values": [0.004, 0.1], "settle": 2.0}
    assert t["plan"]["sweep"]["smu.V"] == {"from": -0.5, "to": 0.5, "points": 5}
    assert t["plan"]["measure"] == ["lockin.X"] and t["gaps"] == []
    load_plan(t["plan"])

    watch = lv.vi("C:/Lab/watch.vi", read="full", controls=[lv.control("N", 60)], diagram=[
        lv.node("write", resource="GPIB0::8::INSTR", text="SLVL 0.5"),
        lv.loop("i", [lv.node("query", resource="GPIB0::8::INSTR", text="OUTP? 3", into="R"),
                      lv.node("wait", ms=lv.const(1000))], count="N")])
    t = viscan.translate(watch, lab)
    assert t["plan"]["time"] == {"every": 1.0, "points": 60} and t["plan"]["setup"] == {"lockin.amplitude": 0.5}
    assert t["plan"]["measure"] == ["lockin.R"]

    once = lv.vi("C:/Lab/read.vi", read="full", diagram=[lv.node("query", resource="GPIB0::8::INSTR", text="OUTP? 1")])
    assert viscan.translate(once, lab)["plan"]["measure"] == ["lockin.X"]   # no loop: one point


def test_a_drivers_call_translates_once_its_commands_are_known(cards):
    lab = lab_of(cards, smu="GPIB0::24::INSTR")
    level = "Keithley 2400 Series.lvlib:Configure 100% Source.vi"
    s = lv.vi("C:/Lab/drive.vi", read="full", linkage=[lv.link(level, "instr.lib")], diagram=[lv.loop("V", [
        lv.node("call", vi=level, resource="GPIB0::24::INSTR", inputs={"Source Level": lv.at("V")},
                sends=[{"text": ":SOUR:VOLT:LEV {lvl}", "values": {"lvl": lv.at("V")}}]),
        lv.node("call", vi="Keithley 2400 Series.lvlib:Read.vi", resource="GPIB0::24::INSTR",
                sends=[{"text": ":READ?", "query": True}], into="I"),
        lv.node("call", vi="Keithley 2400 Series.lvlib:Close.vi", resource="GPIB0::24::INSTR")],
        start=-1, stop=1, step=0.5)])
    assert s["linkage"][0]["maker"] == "Keithley" and s["linkage"][0]["model"] == "2400"
    t = viscan.translate(s, lab)
    assert t["plan"]["sweep"] == {"smu.V": {"from": -1, "to": 1, "step": 0.5}}
    assert t["plan"]["measure"] == ["smu.I", "smu.V_meas"]
    assert {"vi": "%s on GPIB0::24::INSTR: :SOUR:VOLT:LEV {lvl}" % level,
            "plan": "sets smu.V at each step (the sweep)"} in t["mapping"]
    assert t["gaps"] == ["calls Keithley 2400 Series.lvlib:Close.vi: what it sends to the instrument isn't known"]
    found = lv.Survey(folder="C:/Lab", vis=[s])
    smu = lv._instruments(found, cards["registry"])[0]
    assert smu.address == "GPIB0::24::INSTR" and smu.card["id"] == "keithley/2400" and level in smu.calls
    assert smu.card["limits"]["V"] == (-1.0, 1.0)


def test_what_doesnt_translate_is_said_and_never_guessed(cards):
    lab = lab_of(cards, smu="GPIB0::24::INSTR", daq="nidaq://Dev1")

    def scan(*diagram, controls=()):
        return viscan.translate(lv.vi("C:/Lab/x.vi", read="full", controls=list(controls), diagram=list(diagram)),
                                lab)
    sweep = lv.loop("V", [lv.node("write", resource="GPIB0::24::INSTR", text=":SOUR:VOLT {V}",
                                  values={"V": lv.at("V")}),
                          lv.node("query", resource="GPIB0::24::INSTR", text=":READ?")], start=0, stop=1, step=0.5)
    for node, said in [(lv.node("state_machine", states=["Init", "Measure", "Stop"]),
                        "a state machine (Init, Measure, Stop): which state runs next is decided as it runs"),
                       (lv.node("event", events=["Start: Value Change"]), "an event structure (Start: Value Change)"),
                       (lv.node("parallel", branches=[[sweep], [sweep]]), "code that runs in parallel")]:
        t = scan(node)
        assert t["plan"] is None and t["text"] == "" and t["gaps"][0].startswith(said)
    t = scan(lv.node("call_library", library="lakeshore.dll", function="SetHeater"),
             lv.node("call", vi="Helper.vi"), lv.node("property", of="VISA", property="Timeout"), sweep)
    assert t["plan"]["sweep"]["smu.V"] == {"from": 0, "to": 1, "step": 0.5}   # what translates still does
    assert t["gaps"] == ["a Call Library node (lakeshore.dll, SetHeater): a scan can't call a DLL",
                         "calls Helper.vi: what it sends to the instrument isn't known",
                         "a property node (VISA Timeout): not carried over"]
    t = scan(lv.loop("i", [lv.node("query", resource="GPIB0::24::INSTR", text=":READ?")], kind="while",
                     until="Stop button"))
    assert t["plan"] is None and t["gaps"] == [
        "While loop over i: it runs until Stop button; a scan needs its end (a range, or time: for:)"]
    assert "2 loops one after another" in scan(sweep, sweep)["gaps"][0]
    t = viscan.translate(lv.vi("C:/Lab/x.vi"), lab)
    assert t["plan"] is None and t["gaps"] == ["its block diagram isn't read (not read yet)"]
    t = scan(lv.node("write", resource="GPIB0::24::INSTR", text=":SENS:CURR:PROT {c}", values={"c": lv.reading("I")}),
             lv.loop("V", [lv.node("query", resource="GPIB0::24::INSTR", text=":READ?"),
                           lv.node("write", resource="GPIB0::24::INSTR", text=":SOUR:VOLT {V}",
                                   values={"V": lv.at("V")})], start=0, stop=1, step=0.5))
    assert "set from the reading I as it runs" in t["gaps"][0] and "read before For loop over V's set" in t["gaps"][1]
    assert t["plan"] is None and "it reads nothing Kelvin can measure" in t["gaps"][-1]
    t = scan(lv.node("wait", ms=lv.const(500)), lv.node("write", resource="GPIB0::24::INSTR", text=":SYST:BEEP"),
             sweep)
    assert t["gaps"] == ["Wait 500 ms before the loops: a scan waits after a set (settle:)",
                         "VISA write to GPIB0::24::INSTR: :SYST:BEEP: not on smu's card (keithley/2400)"]

    case = lv.node("case", selector=lv.ctl("Zero at the end"), cases={
        "True": [lv.node("write", resource="GPIB0::24::INSTR", text=":SOUR:VOLT 0")], "False": []})
    t = scan(sweep, case, controls=[lv.control("Zero at the end", True)])
    assert t["plan"]["after"] == {"smu.V": 0.0}                          # the case its control's saved value picks
    assert {"vi": "case structure on Zero at the end = True", "plan": "its True case"} in t["mapping"]
    case["selector"] = {"computed": "I > 1 µA"}
    t = scan(sweep, case)
    assert "after" not in t["plan"] and t["gaps"] == [
        "a case structure on I > 1 µA (worked out in the VI): which case runs is known only as it runs"]

    daq = scan(lv.loop("g", [lv.node("daq", op="write", channel="Dev1/ao0", value=lv.at("g")),
                             lv.node("daq", op="read", channel="Dev1/ai0", into="V")], values=lv.const([0, 1, 2])))
    assert daq["plan"]["sweep"] == {"daq.ao0": {"values": [0, 1, 2]}} and daq["plan"]["measure"] == ["daq.ai0"]
    t = scan(lv.loop("g", [lv.node("daq", op="write", channel="Dev1/ao7", value=lv.at("g"))], values=lv.const([0])))
    assert t["plan"] is None and "NI-DAQmx write Dev1/ao7: not on daq's card (ni/daqmx)" in t["gaps"]
    assert viscan.on_target(lv.vi("C:/RT/x.vi", read="full"), "cRIO-9068")["gaps"] == [
        "it runs on cRIO-9068, a hardware target of its project, not on this computer"]


def test_a_loops_stop_condition_is_carried_over_or_keeps_it_from_being_a_scan(cards):
    """A loop that steps the voltage but also stops on a condition (the current passing a limit, say) is never
    swept to its end as if it didn't: a reading compared with a value becomes its sweep's until:, a Stop button is
    Kelvin's Stop, its last value is where the sweep ends; any other condition gives no scan, and says why."""
    lab = lab_of(cards, smu="GPIB0::24::INSTR", lockin="GPIB0::8::INSTR")

    def ramp(**loop):
        return lv.vi("C:/Lab/protect.vi", read="full", controls=[
            lv.control("Start (V)", 0.0), lv.control("Stop (V)", 20.0), lv.control("Step (V)", 0.5),
            lv.control("Limit", 0.002)],
            diagram=[lv.loop("V", [lv.node("write", resource="GPIB0::24::INSTR", text=":SOUR:VOLT {V}",
                                           values={"V": lv.at("V")}),
                                   lv.node("query", resource="GPIB0::8::INSTR", text="OUTP? 1", into="X"),
                                   lv.node("query", resource="GPIB0::24::INSTR", text=":READ?", into="I")],
                             start="Start (V)", stop="Stop (V)", step="Step (V)", **loop)])
    sweep = {"from": 0.0, "to": 20.0, "step": 0.5}
    for until in ("the reading I", "a condition worked out on the diagram (the output of Greater?)"):
        t = viscan.translate(ramp(kind="while", until=until), lab)   # in words only: nothing exact to carry over
        assert t["plan"] is None and t["gaps"] == ["While loop over V: it can also stop early (%s): a scan can't stop "
                                                   "the same way" % until]
    t = viscan.translate(ramp(until="the reading I", stop_if={"read": "I", "op": ">", "than": lv.const(1e-3)}), lab)
    assert t["plan"] is None and "For loop over V: it can also stop early" in t["gaps"][0]   # one of a reply's two
    t = viscan.translate(ramp(kind="while", until="X > Limit", stop_if={"read": "X", "op": ">",
                                                                       "than": lv.ctl("Limit")}), lab)
    assert t["plan"]["sweep"] == {"smu.V": dict(sweep, until="lockin.X > 0.002")} and t["gaps"] == []
    assert {"vi": "While loop over V: it stops once X > Limit = 0.002",
            "plan": "sweep: smu.V: until: lockin.X > 0.002"} in t["mapping"]
    assert load_plan(t["plan"]).axes[0].until is not None
    t = viscan.translate(ramp(kind="while", until="the Stop button", stop_if={"button": "Stop"}), lab)
    assert t["plan"]["sweep"] == {"smu.V": sweep}
    assert {"vi": "While loop over V: its Stop button",
            "plan": "Kelvin's Stop (a person stops the run)"} in t["mapping"]
    t = viscan.translate(ramp(kind="while", until="i = its samples - 1", stop_if={"last": True}), lab)
    assert t["plan"]["sweep"] == {"smu.V": sweep}
    assert {"vi": "While loop over V: it stops after its last value", "plan": "where the sweep ends"} in t["mapping"]
    bad = ramp(kind="while", until="x", stop_if={"read": "X", "op": "≥", "than": lv.const(1)})
    assert lv.validate(bad) == ["diagram[0].stop_if: {button: name}, {last: true} or {read: name, op: > >= < <= == "
                                "!=, than: value}"]


# ------------------------------------------------------------------ the survey

def lab_folder(tmp_path):
    """A lab's LabVIEW folder as labs keep it: a project, its VIs, a copy of a driver in instr.lib, NI MAX's
    visaconf.ini copied from the lab computer, and a configuration exported from MAX."""
    root = tmp_path / "LabVIEW"
    (root / "Measurements").mkdir(parents=True)
    (root / "Main.vi").write_bytes(b"RSRC")
    (root / "Measurements" / "IV sweep.vi").write_bytes(b"RSRC")
    (root / "Lab.lvproj").write_text(PROJECT, encoding="utf-8")
    driver = root / "instr.lib" / "Stanford Research Systems SR830"
    driver.mkdir(parents=True)
    (driver / "Initialize.vi").write_bytes(b"RSRC")
    (root / "visaconf.ini").write_text(VISACONF, encoding="utf-8")
    (root / "max export.ini").write_text(DAQMX, encoding="utf-8")
    return root


def test_the_survey_reads_max_projects_and_vis_never_running_one(tmp_path, cards, monkeypatch):
    monkeypatch.setattr(lv, "VISACONF", tmp_path / "no NI-VISA here" / "visaconf.ini")
    root = lab_folder(tmp_path)
    found = lv.survey(root, cards["registry"])
    assert found.max["files"] == [str(root / "visaconf.ini"), str(root / "max export.ini")]
    assert [s["name"] for s in found.vis] == ["Main.vi", "IV sweep.vi"] and found.driver_vis == 1
    assert all(s["read"] == "no" and s["unread"] == ["not read: it is cut short (4 bytes): only the start of a "
                                                     "LabVIEW file"] for s in found.vis)   # each said, with why
    rows = {i.address or i.calls[0]: i for i in found.instruments}
    assert rows["GPIB0::24::INSTR"].names == ["SMU"] and rows["GPIB0::24::INSTR"].card is None   # SMU names no model
    assert rows["GPIB0::8::INSTR"].card["id"] == "srs/sr830"                # its alias names the model
    assert rows["GPIB0::8::INSTR"].card["why"] == "SR830 named in LabVIEW"
    assert "Stanford Research Systems SR830" in rows["GPIB0::8::INSTR"].calls   # the driver is this instrument's
    assert rows["nidaq://Dev1"].card["id"] == "ni/daqmx" and rows["nidaq://Dev1"].card["why"] == \
        "an NI-DAQmx device (USB-6211)"
    keithley = rows["Keithley 2400 Series.lvlib"]                           # a driver whose address the VIs give
    assert keithley.address == "" and keithley.card["id"] == "keithley/2400" and keithley.where == ["Lab.lvproj"]
    assert "ASRL1::INSTR" not in rows and "TCPIP0::192.168.1.5::inst0::INSTR" in rows   # a port is no instrument
    view = found.view()
    assert view["kind"] == "labview" and view["projects"][0]["drivers"] == ["Keithley 2400 Series.lvlib"]
    assert view["projects"][0]["targets"] == ["cRIO-9068"] and found.translations({}) == {}
    text = found.text()
    assert "never run" in text and "not read: Main.vi (not read: it is cut short (4 bytes)" in text
    assert "target cRIO-9068 (RT CompactRIO, 192.168.1.20): modules NI 9215: its VIs run there" in text

    def reader(path):   # a reader that reads the I-V sweep (until Kelvin's own does)
        return iv_sweep(path, resource="SMU") if path.name == "IV sweep.vi" else lv.read_vi(path)
    found = lv.survey(root, cards["registry"], read=reader)
    smu = next(i for i in found.instruments if i.address == "GPIB0::24::INSTR")
    assert smu.card["id"] == "keithley/2400" and smu.card["limits"]["V"] == (0.0, 1.0)   # from its commands
    assert not any(i.address == "" and i.card and i.card["id"] == "keithley/2400" for i in found.instruments)
    scans = found.translations(lab_of(cards, smu="GPIB0::24::INSTR"))
    assert list(scans) == [str(root / "Measurements" / "IV sweep.vi")]
    assert scans[str(root / "Measurements" / "IV sweep.vi")]["plan"]["sweep"]["smu.V"]["to"] == 1.0
    assert "scan iv_sweep: smu.V 0 → 1 V, 11 points" in found.text(scans)

    with pytest.raises(FileNotFoundError):
        lv.survey(tmp_path / "nope")
    with pytest.raises(ValueError, match="Kelvin's own folder"):
        lv.survey(root, own=[tmp_path])


def test_a_scan_kept_from_labview_is_the_persons(tmp_path, cards, monkeypatch):
    lib = Library(tmp_path / "library.json")
    t = viscan.translate(iv_sweep(), lab_of(cards, smu="GPIB0::24::INSTR"), {"SMU": "GPIB0::24::INSTR"})
    entry = lib.keep_scan(t["text"], t["title"], t["summary"], "IV sweep.vi", "C:/Lab/IV sweep.vi")
    assert entry["id"] == plan_id(t["text"]) and entry["kind"] == "plan" and entry["source"] == "labview"
    assert (entry["name"], entry["vi"], entry["path"], entry["count"]) == ("IV sweep", "IV sweep.vi",
                                                                           "C:/Lab/IV sweep.vi", 0)
    ran = lib.note_plan(t["text"], "iv_sweep", t["summary"], "20261101-120000-iv_sweep")
    assert ran["id"] == entry["id"] and ran["source"] == "labview" and ran["count"] == 1   # its runs are its own
    monkeypatch.setattr("labharness.scanlibrary.MAX_ENTRIES", 2)
    for k in range(3):
        lib.note_plan("plan: p%d\n" % k, "p%d" % k, "", "run-%d" % k)
    assert entry["id"] in {e["id"] for e in lib.entries()}                 # never pruned, as a person's script


# ------------------------------------------------------------------ LabVIEW's measurement files (.lvm)

LVM = """LabVIEW Measurement\t
Writer_Version\t2\t
Reader_Version\t2\t
Separator\tTab\t
Decimal_Separator\t.\t
Multi_Headings\tNo\t
X_Columns\tOne\t
Time_Pref\tRelative\t
Operator\tsomeone\t
Description\t"a gate sweep
over two lines"\t
Date\t2026/10/30\t
Time\t14:50:30.6875\t
***End_of_Header***\t
\t
Channels\t2\t
Samples\t3\t3\t
Date\t2026/10/30\t2026/10/30\t
Time\t14:50:30.6875\t14:50:30.6875\t
Y_Unit_Label\tVolts\tAmps\t
X_Dimension\tTime\tTime\t
X0\t0.0000000000000000E+0\t0.0000000000000000E+0\t
Delta_X\t1.000000E-3\t1.000000E-3\t
***End_of_Header***\t
X_Value\tVoltage\tCurrent\tComment
0.000000\t0.1\t1.0E-6\tfirst point
0.001000\t0.2\t2.0E-6
0.002000\t0.3\t3.0E-6
"""


def lvm_without_x(text):
    """The same file with X_Columns No: no X column, x from X0 and Delta_X."""
    text = text.replace("X_Columns\tOne", "X_Columns\tNo").replace("X_Value\tVoltage", "Voltage")
    return "\n".join(line.split("\t", 1)[1] if line[:1].isdigit() else line for line in text.splitlines()) + "\n"


def test_labview_measurement_files_become_runs(tmp_path):
    def read(name, text, encoding="utf-8"):
        (tmp_path / name).write_text(text, encoding=encoding)
        return imports.read(tmp_path / name)
    t = read("gate.lvm", LVM)
    assert (t.x_key, t.x_unit, t.x) == ("time", "s", [0.0, 0.001, 0.002])
    assert t.columns == {"gate.voltage": ("V", [0.1, 0.2, 0.3]), "gate.current": ("A", [1e-6, 2e-6, 3e-6])}
    assert t.notes == ["gate.lvm: a LabVIEW measurement file, started 2026-10-30 14:50:30"]   # no words of its own
    assert read("nox.lvm", lvm_without_x(LVM)).x == [0.0, 0.001, 0.002]
    blank = LVM.replace("X_Columns\tOne", "X_Columns\tNo")   # as NI writes it: the X column's place left blank
    blank = "\n".join("\t" + line.split("\t", 1)[1] if line[:1].isdigit() else line for line in blank.splitlines())
    t = read("blank.lvm", blank + "\n")
    assert t.x == [0.0, 0.001, 0.002] and t.columns["blank.current"] == ("A", [1e-6, 2e-6, 3e-6])
    two = read("two.lvm", LVM + LVM.split("***End_of_Header***\t\n", 1)[1].replace("0.00", "0.01"))
    assert two.x == [0.0, 0.001, 0.002, 0.01, 0.011, 0.012] and two.columns["two.segment"][1] == [1, 1, 1, 2, 2, 2]
    assert "2 segments one after another" in two.notes[0]
    multi = LVM.replace("X_Columns\tOne", "X_Columns\tMulti").replace("X_Value\tVoltage\tCurrent",
                                                                       "X_Value\tVoltage\tX_Value\tCurrent")
    rows = ["0.000000\t0.1\t0.000000\t1.0E-6", "0.001000\t0.2\t0.001000\t2.0E-6", "0.002000\t0.3\t0.002000\t3.0E-6"]
    multi = multi.split("Comment\n")[0] + "Comment\n" + "\n".join(rows) + "\n"
    assert read("multi.lvm", multi).columns["multi.current"][1] == [1e-6, 2e-6, 3e-6]
    with pytest.raises(DataError, match="different x values"):
        read("apart.lvm", multi.replace("0.002000\t3.0E-6", "0.005000\t3.0E-6"))
    comma = LVM.replace("Separator\tTab", "Separator\tTab").replace("Decimal_Separator\t.", "Decimal_Separator\t,")
    comma = comma.replace("0.000000\t0.1", "0,000000\t0,1").replace("0.001000\t0.2", "0,001000\t0,2")
    comma = comma.replace("0.002000\t0.3", "0,002000\t0,3")
    assert read("comma.lvm", comma).columns["comma.voltage"][1] == [0.1, 0.2, 0.3]
    csv = LVM.replace("\t", ",").replace("Separator,Tab", "Separator,Comma")
    assert read("csv.lvm", csv).x == [0.0, 0.001, 0.002]
    absolute = LVM.replace("Time_Pref\tRelative", "Time_Pref\tAbsolute").replace("0.00", "3876216630.00")
    t = read("abs.lvm", absolute)
    assert t.x == [0.0, pytest.approx(0.001, abs=1e-6), pytest.approx(0.002, abs=1e-6)]
    assert t.notes[1] == "abs.lvm: time as seconds from 2026-10-30 14:50:30 UTC"   # seconds since 1904, LabVIEW's
    assert read("utf16.lvm", LVM, "utf-16").x == [0.0, 0.001, 0.002]
    assert read("plain.lvm", "0\t1.5\n1\t2.5\n2\t3.5\n").columns["plain.y1"][1] == [1.5, 2.5, 3.5]   # no headers
    with pytest.raises(DataError, match="header never ends"):
        read("cut.lvm", LVM.split("***End_of_Header***")[0])
    assert imports.takes("old run.lvm", b"LabVIEW Measurement")
