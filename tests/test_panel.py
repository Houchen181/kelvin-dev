"""What the lab panel shows: readable labels, readings vs outputs vs settings, and each instrument's state."""
from labharness import ScriptedApprover
from simlab import build_virtual_lab, demo_envelope
from labharness.agent.runtime import LabRuntime


def panel(runtime):
    return {i["name"]: i for i in runtime.ui_state()["instruments"]}


def test_instruments_are_grouped_and_labelled(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path, demo_envelope(lab))
    smu = panel(rt)["smu"]
    assert smu["model"] == "Virtual Source-Meter" and smu["kind"] == "source-meter"
    assert smu["headline"] == "I" and smu["readings"][0]["label"] == "Current"
    assert [d["label"] for d in smu["drives"]] == ["Voltage", "Compliance", "Output"]
    assert [s["label"] for s in smu["settings"]] == ["Source range", "Source autorange", "Integration (NPLC)"]
    # it powers up with 100 µA compliance (above safe) but the output is off: armed, not active
    assert smu["state"] == "safe" and smu["active"] == 0
    compliance = next(d for d in smu["drives"] if d["name"] == "compliance_I")
    assert compliance["armed"] and not compliance["away"] and compliance["switch"] == "Output"
    assert compliance["value"] == "100 µA" and compliance["safe"] == "1 nA"
    assert all(i["state"] == "safe" for i in panel(rt).values())   # a fresh lab drives nothing
    laser = panel(rt)["laser"]
    assert next(d for d in laser["drives"] if d["name"] == "shutter")["value"] == "closed"


def test_switching_an_output_on_makes_the_instrument_active(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, ScriptedApprover([True, True]), tmp_path, demo_envelope(lab))
    rt.set("smu", "output", True)
    rt.set("smu", "V", 2)
    smu = panel(rt)["smu"]
    assert smu["state"] == "active" and smu["active"] == 3   # voltage, compliance and the output itself
    assert next(d for d in smu["drives"] if d["name"] == "V")["away"]


def test_unresponsive_instrument_shows_as_error(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path, demo_envelope(lab))
    lab.instruments["lockin"].backend.fault = "offline"
    rt._status_at = 0  # force a refresh
    lockin = panel(rt)["lockin"]
    assert lockin["state"] == "error"
    assert all(r["value"] == "no response" for r in lockin["readings"])


def test_the_lab_panel_never_scrolls_and_its_controls_say_their_state():
    """The page's Lab panel (fit D): it never scrolls as a whole, each open part scrolls inside (a region); the folds
    are kept by their part's id; Add… and Settings (n) are disclosures; the one switch; Mine | Everyone a radio group;
    the panel only says Locked (the one Unlock… is in the notice row); Team chat's messages are its region."""
    import pytest
    pytest.importorskip("starlette")
    from labharness.web.app import STATIC
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for part in ("instruments", "watches", "memory", "activity"):
        assert 'data-part="%s"' % part in page and 'id="lab-part-%s" class="lab-part-body" data-region="lab-part"' % part in page
    assert '<div id="lab-pending" data-region="lab-part">' in page and 'data-region="team"' in page
    assert 'id="hands-done-panel"' not in page and "<span>Locked</span>" in page
    assert 'id="watch-add-toggle" class="btn small disclose" type="button" aria-expanded="false" aria-controls="watch-form"' in page
    assert 'id="labmem-add" class="btn small disclose" type="button" aria-expanded="false" aria-controls="labmem-form"' in page
    assert 'id="activity-who" class="seg" role="radiogroup"' in page and 'id="inst-seg" class="seg" role="radiogroup"' in page
    start = js.index("function initLabFolds(")
    folds = js[start:js.index("\n  }\n", start)]
    assert '"lh.labFold." + id' in folds and 'store("lh.fold." + name)' in folds and "body.inert = folded" in folds
    assert 'class: "switch inst-switch"' in js and '"aria-busy"' in js and '"Not switched: "' in js
    assert '"Settings (" + inst.settings.length + ")"' in js and '"Show settings"' not in js
    assert 'b.setAttribute("aria-checked", String(on))' in js and 'store("lh.labEveryone"' in js
    lab = css[css.index("/* ===== fit D"):css.index("/* ===== end fit D")]
    assert "grid-auto-rows: minmax(var(--lab-row), auto); align-content: start; overflow: clip;" in lab
    assert "@container lab (min-width: 900px)" in lab and "/* v3A */" in lab and "/* /v3A */" in lab


def test_a_waiting_request_rings_the_lab_and_each_switch_sits_in_as_its_icon_does():
    """A request no conversation shows brings the Lab panel to the screen as every opener that is not its own button
    does (Kelvin.paneShow): there already, its head rings, and it never closes. Each instrument's switch has its track
    as far in from the list's right edge as the icon is from its left: with a mouse (a 40 px button) and on touch (44)."""
    import re
    import pytest
    pytest.importorskip("starlette")
    from labharness.web.app import STATIC
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    start = js.index("async function pollState(")
    poll = js[start:js.index("\n  }\n", start)]
    assert 'if (fresh) Kelvin.paneShow("labpanel");' in poll and "toggleLab(true)" not in poll and "fullShow(" not in poll
    bare = lambda s: re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    section = lambda name: bare(css[css.index("/* ===== fit %s:" % name):css.index("/* ===== end fit %s " % name)])
    a, d = section("A"), section("D")
    track = int(re.search(r"\.switch::before \{[^}]*?\bwidth: (\d+)px", a).group(1))
    mouse = int(re.search(r"(?:^|\n)\.switch \{[^}]*?\bwidth: (\d+)px", a).group(1))
    touch = int(re.search(r"\.switch \{ width: (\d+)px;", a[a.index("@media (pointer: coarse)"):]).group(1))
    icon = int(re.search(r"(?:^|\n)\.inst-row \{[^}]*?\bpadding: 0 (\d+)px;", bare(css)).group(1))
    inset = re.compile(r"#labpanel \.inst-head > \.inst-switch \{ margin-right: (\d+)px; \}")
    coarse = d.index("@media (pointer: coarse)")
    by_mouse, by_touch = inset.search(d[:coarse]), inset.search(d[coarse:])
    assert int(by_mouse.group(1)) + (mouse - track) / 2 == icon == int(by_touch.group(1)) + (touch - track) / 2
