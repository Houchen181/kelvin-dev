"""Each instrument's calibration, as a person records it: its due date and certificate, kept with the instrument's
folder; health_check and the approval cards warn when one is out of date (F3 #20: no calibration tracking)."""
import datetime
import threading

import pytest
from labsim_bench import labsim_bench  # noqa: F401  (the fixture)

from labharness import build_virtual_lab, profiles

TODAY = datetime.date(2026, 10, 29)


def real(lab, name, serial=""):
    """A virtual instrument Kelvin takes for real (as on labsim): its calibration counts."""
    lab.instruments[name].backend.simulated = False
    return lab.instruments[name]


def test_a_calibration_is_recorded_with_its_dates_and_certificate_and_every_one_is_kept(tmp_path):
    assert profiles.calibration(tmp_path, "dmm") == {}
    assert profiles.calibration_state({}, TODAY)["state"] == "none"
    with pytest.raises(ValueError, match="due date must be a date"):
        profiles.record_calibration(tmp_path, "dmm", "next year", today=TODAY)
    with pytest.raises(ValueError, match="before it was calibrated"):
        profiles.record_calibration(tmp_path, "dmm", "2026-01-01", "2026-03-01", today=TODAY)
    with pytest.raises(ValueError, match="in the future"):
        profiles.record_calibration(tmp_path, "dmm", "2027-12-01", "2026-12-01", today=TODAY)
    profiles.record_calibration(tmp_path, "dmm", "2026-09-30", "2025-09-30", "C-1234", "Ada", today=TODAY)
    profiles.record_calibration(tmp_path, "dmm", "2027-10-01", "2026-10-01", "C-2001", "Ada", provider="Keysight",
                                today=TODAY)
    assert len(profiles.calibrations(tmp_path, "dmm")) == 2   # the earlier record stays, for runs before
    last = profiles.calibration(tmp_path, "dmm")
    assert last["certificate"] == "C-2001" and last["by"] == "Ada" and last["provider"] == "Keysight"
    st = profiles.calibration_state(last, TODAY)
    assert st["state"] == "ok" and st["words"] == "calibrated 2026-10-01, due 2027-10-01 (certificate C-2001)"
    assert profiles.calibration_state({"due": "2026-11-10"}, TODAY)["words"] == "due 2026-11-10, in 12 days"
    st = profiles.calibration_state({"due": "2026-09-30", "certificate": "C-1"}, TODAY)
    assert st["state"] == "overdue" and st["words"] == "out of date since 2026-09-30 (certificate C-1)"


def test_a_certificate_is_kept_in_the_instruments_folder(tmp_path):
    kept = profiles.save_certificate(tmp_path, "dmm", "Cal cert 2026.pdf", b"%PDF-1.4 certificate")
    assert kept.endswith("-Cal_cert_2026.pdf")
    assert profiles.certificate_path(tmp_path, "dmm", kept).read_bytes() == b"%PDF-1.4 certificate"
    with pytest.raises(ValueError, match="isn't a PDF"):
        profiles.save_certificate(tmp_path, "dmm", "fake.pdf", b"MZ\x90\x00")
    with pytest.raises(ValueError, match="PDF, PNG or JPEG"):
        profiles.save_certificate(tmp_path, "dmm", "run.exe", b"MZ")
    for bad in ("../instrument.json", "..", "", "x/y.pdf"):
        with pytest.raises(KeyError):
            profiles.certificate_path(tmp_path, "dmm", bad)


def test_a_calibration_for_another_serial_number_is_said(tmp_path):
    folder = profiles.folder(tmp_path, "dmm")
    folder.mkdir(parents=True)
    (folder / "instrument.json").write_text('{"parsed": {"serial": "MY12345"}}', encoding="utf-8")
    record = profiles.record_calibration(tmp_path, "dmm", "2027-01-01", today=TODAY)
    assert record["serial"] == "MY12345"   # the instrument's own, from its identity
    (folder / "instrument.json").write_text('{"parsed": {"serial": "MY99999"}}', encoding="utf-8")   # swapped
    notes = profiles.calibration_notes(tmp_path, ["dmm"], TODAY)
    assert notes == ["dmm: the calibration on record is for serial MY12345, but it reports serial MY99999"]
    assert profiles.card_lines(tmp_path, ["dmm"], TODAY) == [
        "Calibration on record is for serial MY12345; dmm reports serial MY99999"]


def test_health_check_warns_of_a_calibration_out_of_date_or_due_soon_and_still_passes(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", None)
    rt.home = tmp_path / "home"
    real(lab, "smu")
    real(lab, "lockin")
    profiles.record_calibration(rt.home, "smu", "2020-01-01", "2019-01-01", "C-77")
    soon = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
    profiles.record_calibration(rt.home, "lockin", soon)
    profiles.record_calibration(rt.home, "magnet", "2020-01-01")   # simulated: a simulator has no calibration
    out = rt.health(["smu", "lockin", "magnet"])
    assert "smu ok" in out and "magnet ok" in out   # a warning, never a failure: the run still goes
    assert "smu calibration out of date since 2020-01-01 (certificate C-77): its readings may have drifted" in out
    assert "lockin calibration due %s, in 10 days" % soon in out
    assert "magnet calibration" not in out
    profiles.write_profile(rt.home, "smu", lab.instruments["smu"].card, "GPIB0::24::INSTR")   # as at connecting
    assert "calibration: out of date since 2020-01-01 (certificate C-77)" in rt.instrument_info("smu")


def test_an_approval_card_says_when_an_instrument_it_names_is_out_of_calibration(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", None)
    rt.home = tmp_path / "home"
    real(lab, "smu")
    profiles.record_calibration(rt.home, "smu", "2020-01-01", "2019-01-01", "C-77")
    profiles.record_calibration(rt.home, "magnet", "2020-01-01")   # not on this card: not said
    t = threading.Thread(target=lambda: rt.gate.set("smu", "V", 3), daemon=True)
    t.start()
    entry = rt.approver.wait_for_pending(5)
    assert entry is not None
    assert entry["items"][-1] == "Calibration out of date: smu was due 2020-01-01 (certificate C-77)"
    assert not any("magnet" in i for i in entry["items"])
    rt.approver.answer(entry["id"], False)
    t.join(5)
    asked = [e for e in rt.gate.log.events() if e["type"] == "ask"][-1]
    assert "Calibration out of date: smu was due 2020-01-01" in asked["request"]   # in the record, with the card
    profiles.record_calibration(rt.home, "smu", "2099-01-01", datetime.date.today().isoformat())   # recalibrated
    t = threading.Thread(target=lambda: rt.gate.set("smu", "V", 3), daemon=True)
    t.start()
    entry = rt.approver.wait_for_pending(5)
    assert not any("Calibration" in i for i in entry["items"])
    rt.approver.answer(entry["id"], False)
    t.join(5)


TRANSPORT = """lab: labsim transport
instruments:
  gate:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
"""


def test_on_labsim_the_2400s_calibration_is_its_own_and_its_cards_say_when_it_is_out_of_date(labsim_bench, tmp_path):
    """On instruments Kelvin takes for real: the record carries the serial number the 2400 gives, health_check passes
    with the warning, and a drive change's card says it."""
    pytest.importorskip("mcp")
    import threading as th
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.virtual_lab import build_lab
    labsim_bench("transport")
    (tmp_path / "lab.yaml").write_text(TRANSPORT, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml", partial=True)
    try:
        assert lab.unavailable == {} and not lab.instruments["gate"].simulated
        rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", None)
        rt.home = tmp_path / "home"
        profiles.prepare(rt.home, lab, th.Lock(), manuals=False)   # who it is, as when Kelvin connects
        serial = (profiles.info(rt.home, "gate").get("parsed") or {}).get("serial")
        assert serial
        record = profiles.record_calibration(rt.home, "gate", "2020-06-30", "2019-06-30", "K-2400-1")
        assert record["serial"] == serial
        out = rt.health(["gate"])
        assert "gate ok" in out and "gate calibration out of date since 2020-06-30 (certificate K-2400-1)" in out
        t = th.Thread(target=lambda: rt.gate.set("gate", "V", 1.0), daemon=True)
        t.start()
        entry = rt.approver.wait_for_pending(10)
        assert entry and entry["where"] == "REAL HARDWARE"
        assert "Calibration out of date: gate was due 2020-06-30 (certificate K-2400-1)" in entry["items"]
        rt.approver.answer(entry["id"], False)
        t.join(10)
    finally:
        lab.close()


def test_a_person_records_a_calibration_in_the_portal(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", None)
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], providers={}))
    headers = {"X-Kelvin-Token": settings.access_key}
    with client:
        url = "/api/instruments/smu/calibration"
        assert client.get(url, headers=headers).status_code == 404   # a simulated instrument has no calibration
        real(lab, "smu")
        assert client.get(url, headers=headers).json()["state"]["state"] == "none"
        r = client.put(url + "/certificate", content=b"%PDF-1.7 cert",
                       headers=dict(headers, **{"X-File-Name": "c.pdf"}))
        assert r.status_code == 200
        kept = r.json()["file"]
        bad = client.put(url + "/certificate", content=b"MZ", headers=dict(headers, **{"X-File-Name": "c.exe"}))
        assert bad.status_code == 400
        r = client.post(url, json={"due": "2020-02-02", "calibrated": "2019-02-02", "certificate": "C-9",
                                   "file": kept}, headers=headers)
        assert r.status_code == 200 and r.json()["state"]["state"] == "overdue"
        assert r.json()["record"]["by"] == "human (portal)"
        assert client.post(url, json={"due": "soon"}, headers=headers).status_code == 400
        got = client.get(url + "/certificate", params={"file": kept}, headers=headers)
        assert got.status_code == 200 and got.content == b"%PDF-1.7 cert"
        assert client.get(url + "/certificate", params={"file": "../settings.json"}, headers=headers).status_code == 404
        logged = [e for e in rt.gate.log.events() if e["type"] == "calibration"]
        assert logged and logged[-1]["instrument"] == "smu" and logged[-1]["due"] == "2020-02-02"
