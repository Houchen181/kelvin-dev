"""Kelvin reads the lab config it uses (issue #12: it refused a value, citing a limit, but couldn't read the lab config
to say what the limit was), and says whose limit a refused value hit: the instrument's card, the lab config, or a
mounted device's protection."""
import re

import pytest

mcp = pytest.importorskip("mcp")
anyio = pytest.importorskip("anyio")

from mcp import Client  # noqa: E402

from labharness import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.discovery import instrument_text, set_limits  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402

LAB = """lab: Bench
instruments:
  smu:
    card: virtual/source-meter
    limits: { V: [-5, 5] }   # our sample takes 5 V at most
  lockin: { card: virtual/lock-in }
"""


def runtime(tmp_path, text=LAB):
    (tmp_path / "lab.yaml").write_text(text, encoding="utf-8")
    return LabRuntime(build_virtual_lab(config=tmp_path / "lab.yaml"), ScriptedApprover([]), tmp_path / "data")


def call(rt, name, args):
    async def main():
        async with Client(build_server(rt)) as client:
            result = await client.call_tool(name, args)
            return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    return anyio.run(main)


def test_kelvin_reads_the_lab_config_as_written(tmp_path):
    rt = runtime(tmp_path)
    whole = call(rt, "lab_config", {})
    assert str(tmp_path / "lab.yaml") in whole
    assert "limits: { V: [-5, 5] }   # our sample takes 5 V at most" in whole   # its comments too
    assert "lockin: { card: virtual/lock-in }" in whole
    assert "differs" not in whole and "keeps its card's range" in whole
    smu = call(rt, "lab_config", {"instrument": "smu"})
    assert "smu's entry" in smu and "limits: { V: [-5, 5] }" in smu and "lockin" not in smu.split("```")[1]
    assert call(rt, "lab_config", {"instrument": "lockin"}).count("lockin: { card: virtual/lock-in }") == 1
    assert "has no instrument 'magnet' (it has: smu, lockin)" in call(rt, "lab_config", {"instrument": "magnet"})


def test_it_says_where_the_file_differs_from_what_kelvin_uses(tmp_path):
    rt = runtime(tmp_path)
    path = tmp_path / "lab.yaml"
    path.write_text(LAB.replace("[-5, 5]", "[-8, 8]").replace("  lockin:", "  lockin2:"), encoding="utf-8")
    out = rt.lab_config()
    assert "smu's limits: V [-8, 8] in the file, V [-5, 5] in use" in out
    assert "lockin is in the lab Kelvin loaded but no longer in the file" in out
    assert "lockin2 is in the file but not in the lab Kelvin loaded" in out
    assert "Save and connect" in out
    path.write_text(LAB.replace("[-5, 5]", "[-500, 500]"), encoding="utf-8")   # beyond the card: not loadable
    assert "The file has errors now (" in rt.lab_config() and "exceeds" in rt.lab_config()
    path.write_text(LAB.replace("[-5, 5] }", "[-5, 5]"), encoding="utf-8")       # not even YAML
    assert "keeps using what it loaded before" in rt.lab_config()


def test_a_lab_from_no_file_says_so(tmp_path):
    rt = LabRuntime(build_virtual_lab(config={"lab": "Inline", "instruments": {"smu": {"card": "virtual/source-meter"}}}),
                    ScriptedApprover([]), tmp_path / "data")
    assert "Inline was not loaded from a lab config file" in rt.lab_config()


def test_a_refusal_says_whose_limit_it_hit(tmp_path):
    rt = runtime(tmp_path)
    by_lab = rt.gate.set("smu", "V", 6)
    assert by_lab.action == "refused"
    assert "smu.V 6 V is outside the lab config's limits [-5, 5] V (a person changes them with Limits…" in by_lab.message
    by_card = rt.gate.set("lockin", "tc", 1e6)
    assert by_card.action == "refused" and "is outside its card's limits [1e-05, 3e+04] s" in by_card.message
    assert "the instrument's own range, from its card virtual/lock-in; the lab config sets none tighter" in by_card.message
    assert re.search(r"V  drive  V  \[-5, 5\] lab config", rt.gate.describe("smu"))
    assert re.search(r"tc  measure  s  \[1e-05, 3e\+04\] card", rt.gate.describe("lockin"))


def test_the_panel_survives_an_instrument_renamed_in_the_lab_config(tmp_path):
    rt = runtime(tmp_path)
    rt._status = {"gate": {"V": 0.0}, "smu": {"V": 0.0, "nonsense": 1}}   # read before the lab was loaded again
    rt._status_at = 1e18   # no fresh reading in between
    assert list(rt.ui_state()["status"]) == ["smu"]


def test_an_instruments_entry_is_found_as_written():
    text = LAB + "interlocks:\n  - { drive: smu.V, require: lockin.X < 1 }\n"
    assert instrument_text(text, "smu") == ("  smu:\n    card: virtual/source-meter\n"
                                            "    limits: { V: [-5, 5] }   # our sample takes 5 V at most")
    assert instrument_text(text, "lockin") == "  lockin: { card: virtual/lock-in }"
    with pytest.raises(KeyError):
        instrument_text(text, "magnet")
    with pytest.raises(KeyError):
        instrument_text("lab: Empty\n", "smu")
    changed = set_limits(text, "smu", {"V": [-2, 2]})   # the limits editor still finds its block
    assert "limits: { V: [-2, 2] }" in instrument_text(changed, "smu") and changed.endswith("lockin.X < 1 }\n")
