"""Field report 2026-11-15, the lab's scripts: an NV pulsed-measurement rig brought up on Windows 11 (a QICK board, a
Cobolt 06-MLD 520 nm laser on COM11, an IDQ Time Controller, and the lab's own package nvpulse, run as python -m
nvpulse from a conda environment).

Item 12: calculate took one expression; a list is answered too, each on its own line.
Item 1 (checked, not changed): a package's __main__.py given by its path runs as python -m runs it, and a relative
import that fails says how to run it: test_module_scripts.py covers both."""
import pytest

from labharness.gate import ScriptedApprover


# ------------------------------------------------------------------ item 12: calculate, a list

def test_calculate_answers_each_expression_of_a_list(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    said = rt.calculate(["2*muB*0.35/h", "h*c/(1.55e-6*qe)", "Tc + 1", "sqrt(16)"]).splitlines()
    assert len(said) == 4, said
    assert said[0].startswith("2*muB*0.35/h = ") and float(said[0].split("= ")[1]) == pytest.approx(9.7974e9, rel=1e-4)
    assert said[1] == "h*c/(1.55e-6*qe) = 0.7998980544"
    assert said[2].startswith("Tc + 1: not calculated: 'Tc' is not a known constant"), said[2]
    assert said[3] == "sqrt(16) = 4"
    assert rt.calculate("sqrt(16)") == "sqrt(16) = 4"                    # one, as before
    assert rt.calculate("Tc + 1").startswith("not calculated: 'Tc' is not a known constant")
    assert rt.calculate("2+2\n\n3*3") == "2+2 = 4\n3*3 = 9"               # one per line
    assert rt.calculate([]).startswith("nothing to calculate")


def test_calculate_takes_a_list_over_mcp(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import build_server
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")

    async def main():
        async with Client(build_server(rt)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            out = []
            for expression in (["1+1", "2*3"], "1+1"):
                r = await client.call_tool("calculate", {"expression": expression})
                out.append("\n".join(c.text for c in r.content if getattr(c, "type", "") == "text"))
            return tools["calculate"], out
    tool, (several, one) = anyio.run(main)
    assert several == "1+1 = 2\n2*3 = 6" and one == "1+1 = 2"
    assert "a list" in tool.description
    kinds = {alt.get("type") for alt in tool.input_schema["properties"]["expression"].get("anyOf", [])}
    assert kinds == {"string", "array"}, tool.input_schema
