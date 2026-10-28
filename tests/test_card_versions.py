"""Each lab can have its own version of one of Kelvin's cards (another firmware, a wider range, a different command):
saved under the same id, it replaces Kelvin's card in that lab only, and going back is one click."""
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from labharness.labconfig import ConfigError, load_lab_config  # noqa: E402
from labharness.virtual_lab import builtin_registry, lab_registry  # noqa: E402
from test_web import client_for, parts  # noqa: E402,F401

LAB = """
lab: Cryostat
instruments:
  attodry: { card: attocube/attodry, address: "lib://COM4?python=py", limits: { T_set: [1.65, 310] } }
"""


def kelvins_attodry() -> str:
    return Path(builtin_registry().get("attocube/attodry").source).read_text(encoding="utf-8")


def test_a_lab_version_of_kelvins_card_allows_the_instruments_own_range(parts, tmp_path):
    runtime, settings, store = parts
    with pytest.raises(ConfigError, match=r"exceeds vendor limits \[1\.65, 300\]; for a wider range, make this lab's "
                                          r"version of the card attocube/attodry"):
        load_lab_config(LAB, lab_registry(tmp_path))                          # 310 K is beyond Kelvin's card
    ours = kelvins_attodry().replace("limits: [1.65, 300]", "limits: [1.65, 320]")
    client, headers = client_for(parts, {})
    with client:
        refused = client.post("/api/cards", json={"yaml": ours}, headers=headers)
        assert refused.status_code == 400 and "Make your version" in refused.json()["error"]   # only on purpose
        saved = client.post("/api/cards", json={"yaml": ours, "version": True}, headers=headers).json()
        assert saved["id"] == "attocube/attodry" and saved["status"] == "draft"
        mine = [c for c in client.get("/api/cards", headers=headers).json()["cards"] if c["id"] == "attocube/attodry"]
        assert mine and mine[0]["kelvins"]
        assert runtime.lab.registry.get("attocube/attodry").params["T_set"].limits == (1.65, 320.0)
        cfg = load_lab_config(LAB, lab_registry(tmp_path))                     # what the next start reads
        assert cfg.instruments["attodry"].limits["T_set"] == (1.65, 310.0)
        gone = client.post("/api/cards/delete", json={"id": "attocube/attodry"}, headers=headers).json()
        assert gone == {"ok": True, "reconnect": []}                            # Back to Kelvin's card
        assert runtime.lab.registry.get("attocube/attodry").params["T_set"].limits == (1.65, 300.0)
    with pytest.raises(ConfigError):
        load_lab_config(LAB, lab_registry(tmp_path))


def test_kelvin_can_draft_the_labs_version_of_its_card(parts, tmp_path):
    runtime, settings, store = parts
    client_for(parts, {})                                                       # sets the cards folder
    ours = kelvins_attodry().replace("limits: [1.65, 300]", "limits: [1.65, 320]")
    out = runtime.save_card(ours)
    assert out.startswith("saved draft card attocube/attodry") and "this lab's version of Kelvin's card" in out
    assert lab_registry(tmp_path).get("attocube/attodry").status == "draft"     # a person reviews it before outputs
    reviewed = ours.replace("status: draft", "status: lab-verified")
    assert runtime.save_card(reviewed).startswith("not saved: a card Kelvin writes must have status: draft")
