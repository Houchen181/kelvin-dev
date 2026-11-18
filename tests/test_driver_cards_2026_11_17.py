"""Cards written from open driver code (2026-11-17). When Kelvin has no card for an instrument and finds no manual
with a command reference, it drafts the card from a driver: one the instrument collection lists, or one of the open
driver libraries on GitHub has, read as text at a fixed commit. find_card and the scan name the drivers the
collection knows. Offline: GitHub, the model and the makers' sites are fakes."""
import json
import re
import urllib.parse
import urllib.request

import pytest

from labharness import autocard, carddraft, drivercode, uninstall
from labharness.discovery import Found, look_up

SHA1, SHA2 = "1" * 40, "2" * 40
IDN = "ACME,WIDGET 9000,SN12345,1.0"


class FakeGitHub:
    """GitHub as drivercode reaches it: each repository with its default branch, its newest commit and its files at
    that commit; the API's answers (a repository, a commit, a file list, a folder) and raw files, everything asked
    recorded. A raw file is there only at its commit, never at a branch's name."""

    def __init__(self):
        self.repos, self.asked, self.down = {}, [], False

    def add(self, repo, files, commit=SHA1, branch="main", license="MIT"):
        self.repos[repo.lower()] = {"name": repo, "branch": branch, "license": license, "commit": commit,
                                    "files": dict(files)}

    def __call__(self, url):
        self.asked.append(url)
        if self.down:
            raise OSError("HTTP Error 403: rate limit exceeded")
        api = re.match(r"^https://api\.github\.com/repos/([^/]+/[^/]+?)(/.*)?$", url.split("?")[0])
        raw = re.match(r"^https://raw\.githubusercontent\.com/([^/]+/[^/]+)/([^/]+)/(.+)$", url)
        repo = self.repos.get((api or raw).group(1).lower()) if api or raw else None
        if repo is None:
            raise OSError("HTTP Error 404: Not Found")
        files = repo["files"]
        if raw:
            path = urllib.parse.unquote(raw.group(3))
            if raw.group(2) != repo["commit"] or path not in files:
                raise OSError("HTTP Error 404: Not Found")
            return files[path] if isinstance(files[path], bytes) else files[path].encode()
        rest = api.group(2) or ""
        if not rest:
            return json.dumps({"full_name": repo["name"], "default_branch": repo["branch"], "private": False,
                               "license": {"spdx_id": repo["license"]}}).encode()
        commit = re.match(r"^/commits/(.+)$", rest)
        if commit:
            if urllib.parse.unquote(commit.group(1)) not in ("HEAD", repo["branch"], repo["commit"]):
                raise OSError("HTTP Error 422: No commit found for the ref")
            return repo["commit"].encode()
        tree = re.match(r"^/git/trees/(\w+)$", rest)
        if tree and tree.group(1) == repo["commit"]:
            return json.dumps({"sha": "t", "truncated": False, "tree": [
                {"path": p, "type": "blob", "size": len(t)} for p, t in files.items()]}).encode()
        folder = re.match(r"^/contents/(.+)$", rest)
        if folder:
            here = urllib.parse.unquote(folder.group(1))
            return json.dumps([{"path": p, "type": "file", "size": len(t)} for p, t in files.items()
                               if p.rsplit("/", 1)[0] == here]).encode()
        raise OSError("HTTP Error 404: Not Found")


FRAMEWORK = "class Instrument:\n    '''PyMeasure's own instrument class: FRAMEWORK, nothing of an instrument.'''\n"
PYMEASURE = {   # a PyMeasure-like tree
    "pymeasure/__init__.py": "",
    "pymeasure/instruments/instrument.py": FRAMEWORK,
    "pymeasure/instruments/keithley/__init__.py": "from .keithley2000 import Keithley2000\n",
    "pymeasure/instruments/keithley/keithley2000.py": (
        "from pymeasure.instruments.instrument import Instrument\nfrom pymeasure.instruments.validators import "
        "strict_range\nfrom .buffer import KeithleyBuffer\n\n\nclass Keithley2000(KeithleyBuffer, Instrument):\n"
        "    '''Represents the Keithley 2000 multimeter.'''\n    nplc = Instrument.control(':SENS:VOLT:NPLC?', "
        "':SENS:VOLT:NPLC %g', 'NPLC', validator=strict_range, values=[0.01, 10])\n"),
    "pymeasure/instruments/keithley/buffer.py": (
        "class KeithleyBuffer:\n    '''The buffer.'''\n    def buffer_data(self):\n"
        "        return self.ask(':TRAC:DATA?')\n"),
    "pymeasure/instruments/keithley/keithley2600.py": "class Keithley2600:\n    '''The Keithley 2600 series.'''\n",
    "pymeasure/instruments/keithley/test_keithley2000.py": "def test_it(): pass\n",
    "pymeasure/instruments/lakeshore/lakeshore3xx.py": "class LakeShore3xx:\n    '''The LS 331 and the LS 340.'''\n",
    "pymeasure/instruments/thorlabs/mff10x.py": "class MFF10x:\n    '''Thorlabs MFF101 flipper.'''\n",
    "pymeasure/instruments/tdk/tdk_gen40_38.py": "class TDK_Gen40_38:\n    '''TDK-Lambda Genesys GEN40-38.'''\n",
    "docs/conf.py": "project = 'PyMeasure'\n",
}
QCODES = {   # a QCoDeS-like tree
    "src/qcodes/instrument_drivers/Keithley/Keithley_2000.py": "class Keithley2000:\n    '''Keithley 2000 DMM.'''\n",
    "src/qcodes/instrument_drivers/Keithley/Keithley_2636B.py": "class Keithley2636B:\n    '''Keithley 2636B.'''\n",
    "src/qcodes/instrument_drivers/tektronix/AWG2000.py": "class AWG2000:\n    '''A Tektronix AWG 2000.'''\n",
    "src/qcodes/instrument_drivers/stanford_research/SR830.py": "class SR830:\n    '''Stanford Research SR830.'''\n",
}


def test_drivers_are_matched_by_maker_and_model_in_the_libraries_file_names():
    score = drivercode.path_score
    keithley = "KEITHLEY INSTRUMENTS INC."
    assert score("keithley/keithley2000.py", keithley, "MODEL 2000") == 7         # the model's number, its maker
    assert score("Keithley/Keithley_2000.py", keithley, "MODEL 2000") == 7                # case and separators aside
    assert score("tektronix/AWG2000.py", keithley, "MODEL 2000") == 0             # a bare number needs the maker
    assert score("keithley/test_keithley2000.py", keithley, "MODEL 2000") == 0            # a test is not a driver
    assert score("Keithley/Keithley_2636B.py", "Keithley", "2636B") == 7
    assert score("keithley/keithley2600.py", "Keithley", "2636B") == 3                    # its series, below the model
    assert score("lakeshore/lakeshore3xx.py", "LSCI", "MODEL340") == 3   # LSCI is Lake Shore; 3xx a series
    assert score("thorlabs/mff10x.py", "Thorlabs", "KDC101") == 0        # 10x of another model's letters
    assert score("stanford_research/SR830.py", "Stanford_Research_Systems", "SR830") == 7
    assert score("srs/sr830.py", "Stanford Research Systems", "SR830") == 7               # its initials
    assert score("agilent/agilentE4980.py", "Keysight Technologies", "E4980A") == 5       # Agilent was Keysight; digits
    assert score("tdk/tdk_gen40_38.py", "TDK-Lambda", "GEN40-38") == 7                    # the whole name, separators
    assert score("oxfordinstruments/mercuryitc.py", "Oxford Instruments", "Mercury iTC") == 7
    assert score("keithley/keithley2000.py", "ACME", "WIDGET 9000") == 0

    hub = FakeGitHub()
    hub.add("pymeasure/pymeasure", PYMEASURE, SHA1, branch="master")
    hub.add("microsoft/Qcodes", QCODES, SHA2)
    gh = drivercode.GitHub(hub)
    problems = []
    found = drivercode.from_libraries(gh, keithley, "MODEL 2000", problems)
    assert [c.path for c in found] == ["pymeasure/instruments/keithley/keithley2000.py",
                                       "src/qcodes/instrument_drivers/Keithley/Keithley_2000.py"]   # then shorter
    assert [c.commit for c in found] == [SHA1, SHA2] and found[0].library == "PyMeasure"
    assert len(problems) == 3                  # the other three libraries aren't on this fake GitHub: said, not fatal
    best = drivercode.from_libraries(gh, "Keithley", "2636B", [])
    assert [c.path for c in best] == ["src/qcodes/instrument_drivers/Keithley/Keithley_2636B.py",
                                      "pymeasure/instruments/keithley/keithley2600.py"]   # its series' counts too
    assert [c.path for c in drivercode.from_libraries(gh, "LSCI", "MODEL340", [])] == [
        "pymeasure/instruments/lakeshore/lakeshore3xx.py"]          # its text names the 340
    assert drivercode.from_libraries(gh, "LSCI", "MODEL350", []) == []   # a 3xx whose text doesn't name the 350


def test_a_github_address_is_read_raw_at_a_commit():
    a = drivercode.address("https://github.com/pymeasure/pymeasure/blob/master/pymeasure/instruments/sr830.py")
    assert (a.repo, a.ref, a.path, a.kind) == ("pymeasure/pymeasure", "master", "pymeasure/instruments/sr830.py",
                                               "file")
    a = drivercode.address("https://github.com/micro-manager/mmCoreAndDevices/tree/main/DeviceAdapters/ASIStage")
    assert (a.kind, a.path) == ("folder", "DeviceAdapters/ASIStage")
    assert drivercode.address("https://github.com/thinkSRS/srsinst.rga").kind == "repo"
    assert drivercode.address("https://raw.githubusercontent.com/o/r/main/a%20b/x.py").path == "a b/x.py"
    assert drivercode.address("http://github.com/o/r/blob/main/x.py") is None              # https only
    assert drivercode.address("https://pymeasure.readthedocs.io/en/latest/api.html") is None
    assert drivercode.pinned("o/r", SHA1, "a b/x.py") == "https://raw.githubusercontent.com/o/r/%s/a%%20b/x.py" % SHA1
    assert drivercode.page("o/r", SHA1, "x.py") == "https://github.com/o/r/blob/%s/x.py" % SHA1
    assert not drivercode.on_this_computer("https://github.com/o/r") and drivercode.on_this_computer("C:/d/x.py")
    assert not drivercode.on_this_computer("pymeasure.instruments.keithley.Keithley2000")   # a dotted name
    assert drivercode.on_this_computer("widget.py") and drivercode.on_this_computer("bad\x00name.py")

    hub = FakeGitHub()
    hub.add("acme-labs/widget-driver", {"widget/driver.py": "class Widget9000: '''ACME Widget 9000.'''\n"}, SHA2)
    gh = drivercode.GitHub(hub)
    [c] = drivercode._from_item(gh, {"kind": "python-driver", "ref": "widget.driver.Widget9000",
                                     "url": "https://github.com/acme-labs/widget-driver/blob/main/widget/driver.py"},
                                "WIDGET 9000", "the collection lists it")
    assert (c.repo, c.commit, c.path, c.ref, c.license) == ("acme-labs/widget-driver", SHA2, "widget/driver.py",
                                                            "Widget9000", "MIT")   # main, pinned to its commit now
    assert "ACME Widget 9000" in gh.text(c.repo, c.commit, c.path)
    raw = [u for u in hub.asked if "raw.githubusercontent.com" in u]
    assert raw == ["https://raw.githubusercontent.com/acme-labs/widget-driver/%s/widget/driver.py" % SHA2]
    assert "https://api.github.com/repos/acme-labs/widget-driver/commits/main" in hub.asked

    hub.add("pymeasure/pymeasure", PYMEASURE, SHA1, branch="master")   # a library's file: at its list's commit
    hub.asked.clear()
    [c] = drivercode._from_item(gh, {"url": "https://github.com/pymeasure/pymeasure/blob/master/pymeasure/instruments/"
                                            "keithley/keithley2000.py"}, "MODEL 2000", "")
    assert c.commit == SHA1 and c.library == "PyMeasure" and "/commits/master" not in " ".join(hub.asked)

    hub.add("AlexShkarin/pyLabLib", {   # a dotted name on a documentation page: the module its package takes it from
        "pylablib/devices/Thorlabs/__init__.py": "from .serial import FW, MDT69xA\nfrom .kinesis import KinesisMotor\n",
        "pylablib/devices/Thorlabs/serial.py": "class FW:\n    '''Thorlabs FW102C filter wheel.'''\n",
        "pylablib/devices/Thorlabs/kinesis.py": "class KinesisMotor: pass\n"})
    [c] = drivercode._from_item(gh, {"ref": "pylablib.devices.Thorlabs.FW",
                                     "url": "https://pylablib.readthedocs.io/en/latest/devices/Thorlabs.html"},
                                "FW102C", "")
    assert (c.path, c.ref, c.library) == ("pylablib/devices/Thorlabs/serial.py", "FW", "pyLabLib")


def test_a_librarys_file_list_is_kept_a_week_then_fetched_again(tmp_path):
    hub = FakeGitHub()
    hub.add("pymeasure/pymeasure", PYMEASURE, SHA1, branch="master")
    now = [1_000_000.0]
    folder = tmp_path / "drivers"

    def tree():
        return drivercode.GitHub(hub, folder, now=lambda: now[0]).tree("pymeasure/pymeasure")   # a new card each time
    assert tree()["commit"] == SHA1 and hub.asked == [
        "https://api.github.com/repos/pymeasure/pymeasure/commits/HEAD",
        "https://api.github.com/repos/pymeasure/pymeasure/git/trees/%s?recursive=1" % SHA1]
    kept = json.loads((folder / "pymeasure__pymeasure.json").read_text(encoding="utf-8"))
    assert kept["commit"] == SHA1 and "pymeasure/instruments/keithley/keithley2000.py" in kept["files"]
    assert all(p.startswith("pymeasure/instruments/") for p in kept["files"])   # the drivers' folder only
    hub.asked.clear()
    now[0] += 6 * 24 * 3600
    assert tree()["commit"] == SHA1 and hub.asked == []                          # under a week old: not asked again
    hub.add("pymeasure/pymeasure", dict(PYMEASURE, **{"pymeasure/instruments/acme/widget9000.py": "x"}), SHA2)
    now[0] += 2 * 24 * 3600
    fresh = tree()
    assert fresh["commit"] == SHA2 and "pymeasure/instruments/acme/widget9000.py" in fresh["files"] and hub.asked
    hub.down = True                                                             # rate-limited, offline
    now[0] += 8 * 24 * 3600
    said = []
    old = drivercode.GitHub(hub, folder, log=said.append, now=lambda: now[0]).tree("pymeasure/pymeasure")
    assert old["commit"] == SHA2 and "rate limit" in said[0]                    # the copy here, said in the log
    assert "drivers" in uninstall.HARNESS                                       # Kelvin's own: uninstalling removes it


def test_only_github_is_read_and_each_file_and_card_has_a_cap():
    for bad in ("http://raw.githubusercontent.com/o/r/%s/x.py" % SHA1, "https://example.com/x.py",
                "https://raw.githubusercontent.com:8443/o/r/%s/x.py" % SHA1, "https://user@api.github.com/repos/o/r",
                "file:///etc/passwd", "https://github.com/o/r/blob/main/x.py"):   # github.com's pages: never fetched
        with pytest.raises(drivercode.DriverError, match="GitHub only"):
            drivercode.check(bad)
    assert drivercode.check("https://api.github.com/repos/o/r") == "api.github.com"
    with pytest.raises(drivercode.DriverError):   # a redirect anywhere else is refused as the request is
        drivercode._GitHubOnly().http_request(urllib.request.Request("https://evil.example/x.py"))

    hub = FakeGitHub()
    big = "x" * (drivercode.MAX_FILE + 1)
    hub.add("o/r", dict({"big.py": big}, **{"f%d.py" % i: "pass\n" for i in range(drivercode.MAX_FILES + 1)}))
    gh = drivercode.GitHub(hub)
    with pytest.raises(drivercode.DriverError, match="larger than"):
        gh.text("o/r", SHA1, "big.py")                                          # a runaway download stops at the cap
    with pytest.raises(drivercode.DriverError, match="larger than"):
        gh.text("o/r", SHA1, "big.py", size=len(big))                           # one listed as too big: never fetched
    assert sum("big.py" in u for u in hub.asked) == 1
    for i in range(drivercode.MAX_FILES - 1):
        gh.text("o/r", SHA1, "f%d.py" % i)
    with pytest.raises(drivercode.DriverError, match="fetched %d files" % drivercode.MAX_FILES):
        gh.text("o/r", SHA1, "f%d.py" % drivercode.MAX_FILES)                   # a handful a card
    with pytest.raises(drivercode.DriverError, match="GitHub only"):
        drivercode.given("https://pymeasure.readthedocs.io/en/latest/x.html", drivercode.GitHub(hub), "SR830")
    assert not any("readthedocs" in u for u in hub.asked)


WIDGET_DRIVER = {   # a driver on GitHub that the collection lists: its class is built on one of its own package
    "widget/__init__.py": "",
    "widget/driver.py": ("from .base import WidgetBase\n\n\nclass Widget9000(WidgetBase):\n    '''The ACME Widget 9000 "
                         "source.'''\n\n    def set_voltage(self, volts=5.0):\n        if not -10 <= volts <= 10:\n"
                         "            raise ValueError('out of range')\n        self.write('SOUR:VOLT %g' % volts)\n"),
    "widget/base.py": ("class WidgetBase:\n    def voltage_range(self, value):\n"
                       "        self.write(f'SOUR:VOLT:RANG {value}')\n"),
    "tests/test_driver.py": "def test_nothing(): pass\n",
}
WIDGET = {"id": "acme/widget-9000", "maker": "ACME", "model": "Widget 9000", "name": "ACME Widget 9000 source",
          "kind": "source-meter", "identify": {"idn": "^ACME,WIDGET 9000,"}, "card": None, "manuals": [],
          "software": [{"name": "ACME Control Center", "kind": "vendor-software", "url": "https://www.acme.example/sw"},
                       {"name": "widget-driver (community)", "kind": "python-driver", "ref": "widget.driver.Widget9000",
                        "url": "https://github.com/acme-labs/widget-driver/blob/main/widget/driver.py"},
                       {"name": "ACME SDK", "kind": "vendor-sdk", "url": "https://pypi.org/project/acme-sdk/"}],
          "examples": [{"title": "Widget examples (Python)", "url": "https://github.com/acme-labs/widget-examples",
                        "license": "MIT"}], "links": []}
CARD = """```yaml
card: 1
id: acme/widget-9000
model: ACME Widget 9000 source
kind: source-meter
status: community
manual: the driver
identify: { idn: "^ACME,WIDGET 9000," }
parameters:
  V:
    label: Voltage
    description: "def set_voltage(self, volts=5.0): self.write('SOUR:VOLT %g' % volts)"
    kind: drive
    unit: V
    set: "SOUR:VOLT {value}"
    limits: [-10, 10]
    safe: 0
    ramp: gate
    max_rate: 1
  range:
    label: Voltage range
    kind: measure
    unit: V
    set: "SOUR:VOLT:RANG {value}"
```
- The limits are the driver's own check, -10 to 10 V; check them against the manual.
- The driver reads nothing back and has no error query."""


class Catalog:
    """The instrument collection's copy, with one instrument that has no card."""

    def __init__(self, *entries):
        self.entries = list(entries)

    def index(self):
        return {"instruments": self.entries}

    def instrument_for(self, idn):
        return next((e for e in self.entries if re.search(e["identify"]["idn"], idn, re.I)), None)

    def cards(self):
        return []


def driver_model(prompts, answer=CARD):
    """The card model: no manual's address to suggest; a card from the driver it is given."""
    def ask(system, prompt):
        prompts.append((system, prompt))
        if system == autocard.SUGGEST_SYSTEM:
            return "", "test:model"
        assert system == carddraft.DRIVER_SYSTEM, "a manual was drafted from"
        return answer, "test:model"
    return ask


def test_without_a_manual_the_card_is_written_from_the_driver_the_collection_lists(tmp_path):
    hub = FakeGitHub()
    hub.add("acme-labs/widget-driver", WIDGET_DRIVER, SHA1, license="BSD-3-Clause")
    prompts, said = [], []
    r = autocard.make_card({"address": "GPIB0::5::INSTR", "idn": IDN}, tmp_path / "cards", driver_model(prompts), hub,
                           log=said.append, catalog=Catalog(WIDGET))
    assert r.get("card_id") == "acme/widget-9000", r
    assert r["driver"] == {"library": "acme-labs/widget-driver", "repo": "acme-labs/widget-driver", "commit": SHA1,
                           "path": "widget/driver.py", "license": "BSD-3-Clause",
                           "url": "https://github.com/acme-labs/widget-driver/blob/%s/widget/driver.py" % SHA1}
    assert r["manual"] is None and r["notes"][0].startswith("The limits are the driver's")
    system, prompt = prompts[-1]
    assert system == carddraft.DRIVER_SYSTEM and "strict_range" in system and "never a safe value" in system
    assert "SOUR:VOLT %g" in prompt and "SOUR:VOLT:RANG {value}" in prompt          # the driver and its base class
    assert "test_nothing" not in prompt and "SN12345" not in prompt                 # no tests; never the serial number
    assert "the instrument collection lists it for the ACME Widget 9000 source" in prompt and "Widget9000" in prompt
    saved = (tmp_path / "cards" / "acme_widget-9000.yaml").read_text(encoding="utf-8")
    card, problems, _ = carddraft.check(saved)
    assert problems == [] and card.status == "draft"                               # a draft, like any Kelvin writes
    assert card.manual == ("Driver widget/driver.py, github.com/acme-labs/widget-driver at commit %s, BSD-3-Clause "
                           "license (written from its code, not the maker's manual)" % SHA1)
    assert card.manual_url == ""                                                    # never the driver: no manual
    head = saved.split("\ncard: 1")[0]
    assert "not from the maker's manual" in head and r["driver"]["url"] in head and "check each against" in head
    assert "def set_voltage" not in saved and card.params["V"].set_cmd is not None  # commands and ranges, not code
    assert any("copied from the driver's code" in c for c in r["changed"])
    assert all(SHA1 in u for u in hub.asked if "raw.githubusercontent.com" in u)
    assert any("Kelvin could not find the ACME WIDGET 9000 manual online" in m for m in said)


def test_without_the_collection_kelvin_looks_in_the_open_libraries(tmp_path):
    hub = FakeGitHub()
    hub.add("pymeasure/pymeasure", PYMEASURE, SHA1, branch="master")
    prompts = []
    answer = CARD.replace("acme/widget-9000", "keithley/2000").replace("ACME Widget 9000 source", "Keithley 2000") \
        .replace('"^ACME,WIDGET 9000,"', '"^KEITHLEY INSTRUMENTS INC\\\\.,MODEL 2000,"')
    r = autocard.make_card({"idn": "KEITHLEY INSTRUMENTS INC.,MODEL 2000,1234567,A01"}, tmp_path / "cards",
                           driver_model(prompts, answer), hub)
    assert r.get("card_id") == "keithley/2000" and r["driver"]["library"] == "PyMeasure", r
    assert r["driver"]["path"] == "pymeasure/instruments/keithley/keithley2000.py" and r["driver"]["license"] == "MIT"
    prompt = prompts[-1][1]
    assert "':SENS:VOLT:NPLC %g'" in prompt and ":TRAC:DATA?" in prompt             # its own package's buffer...
    assert "FRAMEWORK" not in prompt                                                # ...never the library's framework
    assert "Instrument.control(get, set, ...)" in prompt                            # how PyMeasure's drivers send
    saved = (tmp_path / "cards" / "keithley_2000.yaml").read_text(encoding="utf-8")
    assert "PyMeasure driver pymeasure/instruments/keithley/keithley2000.py" in saved and "MIT license" in saved

    nothing = autocard.make_card({"idn": "ACME,GIZMO 1,1,1"}, tmp_path / "c2", driver_model([]), hub)["error"]
    assert nothing.startswith("Kelvin could not find the ACME GIZMO 1 manual online, nor an open driver for it on "
                              "GitHub") and "choose its PDF" in nothing


def test_draft_card_takes_the_driver_given(tmp_path):
    pytest.importorskip("mcp")
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    hub = FakeGitHub()
    hub.add("acme-labs/widget-driver", WIDGET_DRIVER, SHA1)
    prompts = []
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.cards_dir = tmp_path / "Kelvin", tmp_path / "Kelvin" / "cards"
    asked = []

    def writer(found):
        asked.append(found)
        return autocard.make_card(found, rt.cards_dir, driver_model(prompts), hub)
    rt.card_writer = writer
    said = rt.draft_card("ASRL5::INSTR", "ACME", "Widget 9000",
                         driver="https://github.com/acme-labs/widget-driver/tree/main/widget")   # a folder: the model's
    assert said.startswith("drafted card acme/widget-9000 (ACME Widget 9000 source, source-meter) from the driver "
                           "widget/driver.py (acme-labs/widget-driver@111111111111, MIT license), not from the maker's "
                           "manual"), said
    assert "check them against the maker's manual" in said and "SOUR:VOLT:RANG {value}" in prompts[-1][1]
    assert not any(s == autocard.SUGGEST_SYSTEM for s, _ in prompts)              # given a driver: no manual looked for
    assert "Kelvin already has a card for it: acme/widget-9000" in rt.draft_card(   # never drafted twice
        "ASRL5::INSTR", "ACME", "Widget 9000", driver="https://github.com/acme-labs/widget-driver")
    (rt.cards_dir / "acme_widget-9000.yaml").unlink()
    assert "manual or driver, not both" in rt.draft_card("ASRL5::INSTR", "ACME", "Widget 9000", manual="m.pdf",
                                                         driver="https://github.com/acme-labs/widget-driver")
    assert "GitHub only" in rt.draft_card("ASRL5::INSTR", "ACME", "Widget 9000", driver="https://www.acme.example/d.py")

    vendor = tmp_path / "vendor" / "acme"   # a file on this computer, with its own package
    vendor.mkdir(parents=True)
    for name in ("__init__.py", "driver.py", "base.py"):
        (vendor / name).write_text(WIDGET_DRIVER["widget/" + name], encoding="utf-8")
    said = rt.draft_card("ASRL5::INSTR", "ACME", "Widget 9000", driver=str(vendor / "driver.py"))
    assert "from the driver %s, not from the maker's manual" % (vendor / "driver.py") in said, said
    assert isinstance(asked[-1]["driver"], dict) and "SOUR:VOLT:RANG {value}" in prompts[-1][1]   # read here, base too
    (rt.cards_dir / "acme_widget-9000.yaml").unlink()
    mine = rt.home / "notes"   # in Kelvin's own folder: the file tools never read there, nor does this
    mine.mkdir(parents=True)
    (mine / "driver.py").write_text(WIDGET_DRIVER["widget/driver.py"], encoding="utf-8")
    before = len(asked)
    refused = rt.draft_card("ASRL5::INSTR", "ACME", "Widget 9000", driver=str(mine / "driver.py"))
    assert refused.startswith("no card yet: ") and "Kelvin's own folder" in refused and len(asked) == before


DLL_DRIVER = ("import ctypes\n\n\nclass MCS9000:\n    '''The ACME MCS9000 positioner, through SmarActCTL.dll.'''\n\n"
              "    def __init__(self):\n        self.lib = ctypes.WinDLL('SmarActCTL.dll')\n")


def test_a_driver_that_loads_the_makers_dll_gives_no_text_card(tmp_path):
    assert drivercode.loads([DLL_DRIVER]) == ["SmarActCTL.dll (a DLL, through ctypes)"]
    assert drivercode.loads(["self._dll_path = 'Thorlabs.MotionControl.KCube.DCServo.dll'\n"]) == [
        "Thorlabs.MotionControl.KCube.DCServo.dll"]
    assert drivercode.loads(["import clr\nclr.AddReference('Thorlabs.MotionControl.DeviceManagerCLI')\n"]) == [
        "Thorlabs.MotionControl.DeviceManagerCLI (a .NET assembly, through pythonnet)"]
    assert drivercode.loads(["import zhinst.core\n"]) == ["the maker's package zhinst"]
    assert drivercode.loads(["import ctypes\nself.dll = ctypes.cdll.LoadLibrary(path)\n"]) == ["a DLL, through ctypes"]
    assert drivercode.loads(["instr = comm_backend.new_backend(conn, backend=('auto', 'ft232'))\n"]) == [
        "the FTDI library (ft232)"]                                              # pyLabLib's Kinesis: binary, over FTDI
    assert drivercode.loads(["from pymeasure.instruments import Instrument\nimport numpy as np\nimport pyvisa\n"]) == []
    assert drivercode.loads(["import ctypes\nraw = bytes(ctypes.c_float(1.5))\n"]) == []   # packs bytes, loads none

    hub = FakeGitHub()
    hub.add("acme-labs/mcs", {"mcs/mcs9000.py": DLL_DRIVER}, SHA1)
    never = lambda system, prompt: pytest.fail("a model was asked about a DLL driver")   # noqa: E731
    r = autocard.make_card({"maker": "ACME", "model": "MCS9000",
                            "driver": "https://github.com/acme-labs/mcs/blob/main/mcs/mcs9000.py"},
                           tmp_path / "cards", never, hub)
    assert r["loads"] == ["SmarActCTL.dll (a DLL, through ctypes)"] and r["driver"]["path"] == "mcs/mcs9000.py", r
    assert ("reaches the ACME MCS9000 through the maker's library, not text commands: it loads SmarActCTL.dll"
            in r["error"])
    assert not (tmp_path / "cards").exists()

    def library(system, prompt):   # the model finds what Kelvin's reading missed
        return "Library: the maker's binary APT protocol over its FTDI chip", "test:model"
    hub.add("acme-labs/apt", {"apt/stage.py": "class Stage:\n    '''ACME MCS9000 stage.'''\n"}, SHA1)
    r = autocard.make_card({"maker": "ACME", "model": "MCS9000",
                            "driver": "https://github.com/acme-labs/apt/blob/main/apt/stage.py"}, tmp_path / "c2",
                           library, hub)
    assert r["loads"] == ["the maker's binary APT protocol over its FTDI chip"] and "library card" in r["error"]

    pytest.importorskip("mcp")
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.cards_dir = tmp_path / "Kelvin", tmp_path / "Kelvin" / "cards"
    rt.card_writer = lambda found: autocard.make_card(found, rt.cards_dir, never, hub)
    said = rt.draft_card("ASRL7::INSTR", "ACME", "MCS9000", driver="https://github.com/acme-labs/mcs/blob/main/"
                                                                    "mcs/mcs9000.py")
    assert said.startswith("no card yet: the driver mcs/mcs9000.py (acme-labs/mcs@111111111111, MIT license) reaches")
    assert "it loads SmarActCTL.dll" in said and "install_for_instrument" in said and "library_api" in said
    assert "lib://<address>?env=" in said


def test_find_card_and_the_scan_name_the_drivers_the_collection_lists(tmp_path):
    catalog = Catalog(WIDGET)
    found = look_up(Found("GPIB0::5::INSTR", IDN), catalog)
    line = found.line()
    assert "acme/widget-9000 in the instrument collection" in line and "or an open driver" in line
    assert ("drivers and SDKs: python-driver widget-driver (community): widget.driver.Widget9000, https://github.com/"
            "acme-labs/widget-driver/blob/main/widget/driver.py; vendor-sdk ACME SDK: "
            "https://pypi.org/project/acme-sdk/") in line
    assert "examples: Widget examples (Python): https://github.com/acme-labs/widget-examples" in line
    assert "ACME Control Center" not in line                                     # the maker's program: no card from it
    assert drivercode.listed({"software": [{"name": "D%d" % i, "kind": "python-driver", "url": "https://github.com/o/"
                                            "r%d" % i} for i in range(6)]})["drivers"][-1] == "2 more"

    pytest.importorskip("mcp")
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.cards_dir, rt.collection = tmp_path, tmp_path / "cards", catalog
    said = rt.find_card("ACME Widget 9000")
    lines = said.splitlines()
    assert "- acme/widget-9000: ACME Widget 9000 source (source-meter); no public manual" in lines, said
    assert any(ln.startswith("  drivers and SDKs: python-driver widget-driver (community): widget.driver.Widget9000, ")
               for ln in lines)
    assert "  examples: Widget examples (Python): https://github.com/acme-labs/widget-examples" in lines
    assert "draft_card with driver" in said and "library card" in said
