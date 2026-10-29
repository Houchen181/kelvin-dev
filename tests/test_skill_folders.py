"""Skills packed as Claude's are: a folder with SKILL.md and the files that go with it. Added from a .zip, given back as
one, switched off without deleting, read with their files, and started by the person with /name."""
import asyncio
import io
import zipfile

import pytest

pytest.importorskip("mcp")

from simlab import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.skills import Know  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted, client_for, parts  # noqa: E402,F401

SKILL = """---
name: cool-down
description: >-
  Cooling the cryostat from room temperature to base: when to pump, the order of the stages, and the checks
  between them.
license: CC-BY-4.0
metadata:
  author: a lab
---
# Cool-down

1. Pump the sample space to below 1e-4 mbar (see references/pressures.csv).
2. Start the compressor; check the first stage falls below 60 K within 3 h.
"""


def claude_zip(extra=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cool-down/SKILL.md", SKILL)
        z.writestr("cool-down/references/pressures.csv", "stage,mbar\npump,1e-4\n")
        z.writestr("cool-down/scripts/check.py", "print('check')\n")
        z.writestr("__MACOSX/cool-down/._SKILL.md", "x")
        z.writestr("cool-down/.DS_Store", "x")
        for name, data in extra:
            z.writestr(name, data)
    return buf.getvalue()


def test_a_skill_made_for_claude_comes_in_and_goes_back_out(tmp_path):
    know = Know(ChatStore(tmp_path / "chats"))
    item = know.import_skill("global", claude_zip([("cool-down/../../evil.txt", "no")]), "cool-down.zip")
    assert item.name == "cool-down" and item.description.startswith("Cooling the cryostat from room temperature")
    assert item.files == ["references/pressures.csv", "scripts/check.py"]          # nothing hidden, nothing outside
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "chats" / "evil.txt").exists()
    with pytest.raises(FileExistsError):
        know.import_skill("global", claude_zip(), "cool-down.zip")
    assert know.import_skill("global", claude_zip(), "x.zip", replace=True).name == "cool-down"
    name, data = know.export("global", "cool-down")
    back = zipfile.ZipFile(io.BytesIO(data))
    assert name == "cool-down.zip" and sorted(back.namelist()) == [
        "cool-down/SKILL.md", "cool-down/references/pressures.csv", "cool-down/scripts/check.py"]
    assert back.read("cool-down/SKILL.md").decode() == SKILL                        # as it came
    unnamed = SKILL.replace("name: cool-down", "name: ''").encode()
    assert know.import_skill("global", unnamed, "pump-down.md").name == "pump-down"   # then its file's name
    for bad, why in [(b"PK\x03\x04junk", "could not open"), (unnamed, "needs a name"),
                     (SKILL.replace("description: >-", "x: >-").encode(), "needs a description")]:
        with pytest.raises(ValueError, match=why):
            know.import_skill("global", bad, "SKILL.md")


def test_old_skills_still_read_new_ones_are_folders_and_any_can_be_switched_off(tmp_path):
    know = Know(ChatStore(tmp_path / "chats"))
    old = tmp_path / "chats" / "skills" / "global" / "find-dirac-point.md"
    old.parent.mkdir(parents=True)
    old.write_text("---\nname: find-dirac-point\ndescription: Locating the Dirac point: a coarse gate sweep, then "
                   "fine.\n---\n1. Sweep the gate from -5 to 5 V.\n", encoding="utf-8")
    found = know.find("skill", "find-dirac-point")
    assert found.description == "Locating the Dirac point: a coarse gate sweep, then fine." and found.folder is None
    name, data = know.export("global", "find-dirac-point")                          # given out as Claude's are
    head = zipfile.ZipFile(io.BytesIO(data)).read("find-dirac-point/SKILL.md").decode()
    assert head.startswith("---\nname: find-dirac-point\ndescription: ") and "1. Sweep the gate" in head
    kept = know.save_skill("find-dirac-point", "Locating the Dirac point.", "1. Sweep the gate from -6 to 6 V.",
                           "global")
    assert kept.folder.name == "find-dirac-point" and not old.exists()               # saved again: as a folder
    assert "1. Sweep the gate from -6" in (kept.folder / "SKILL.md").read_text(encoding="utf-8")
    know.switch("skill", "kelvin", "lock-in-measurement", False)
    know.switch("skill", "global", "find-dirac-point", False)
    assert "lock-in-measurement" not in know.index() and "find-dirac-point" not in know.index()
    assert know.find("skill", "find-dirac-point") is None                          # nor used, while off
    assert [i.enabled for i in know.items() if i.name == "find-dirac-point"] == [False]   # but kept
    know.switch("skill", "global", "find-dirac-point", True)
    assert "find-dirac-point" in know.index()


def test_kelvin_reads_a_skills_files_and_the_person_starts_one_with_slash(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    provider = Scripted([[{"type": "tool_call", "id": "u1", "name": "use_skill", "input": {"name": "cool-down"}}],
                         [{"type": "tool_call", "id": "f1", "name": "read_skill_file",
                           "input": {"skill": "cool-down", "path": "references/pressures.csv"}}],
                         [{"type": "text", "text": "Pumping first."}]])
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    agent.know.import_skill("global", claude_zip(), "cool-down.zip")
    chat = store.create("scripted:m1")

    async def emit(e):
        pass

    async def go():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")
            await agent.run_turn(chat["id"], "/cool-down from 300 K", "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    assert provider.prompts[0][0].startswith('<skill name="cool-down">\n# Cool-down') and \
        provider.prompts[0][0].endswith("</skill>\n\n/cool-down from 300 K")
    assert "Files with this skill (read_skill_file reads them): references/pressures.csv, scripts/check.py" in \
        provider.prompts[0][0]
    assert {"use_skill", "read_skill_file"} <= set(provider.tool_names[0])
    results = [r for m in store.load(chat["id"])["messages"] if m["role"] == "tool" for r in m["results"]]
    assert "Files with this skill" in results[0]["content"] and "stage,mbar" in results[1]["content"]


def test_the_portal_adds_switches_gives_and_deletes_skills(parts):
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        added = client.post("/api/skills/import?scope=global", content=claude_zip(),
                            headers=dict(headers, **{"X-File-Name": "cool-down.zip"}))
        assert added.status_code == 200 and added.json()["files"] == ["references/pressures.csv", "scripts/check.py"]
        again = client.post("/api/skills/import?scope=global", content=claude_zip(), headers=headers)
        assert again.status_code == 409 and "already" in again.json()["error"]
        menu = client.get("/api/know/menu", headers=headers).json()["items"]
        assert {"kind": "skill", "name": "cool-down", "description": added.json()["description"]} in menu
        off = client.post("/api/know/skill/global/cool-down", json={"enabled": False}, headers=headers)
        assert off.json() == {"ok": True}
        assert "cool-down" not in [i["name"] for i in client.get("/api/know/menu", headers=headers).json()["items"]]
        listed = client.get("/api/know?scope=global", headers=headers).json()["items"]
        assert [i["enabled"] for i in listed if i["name"] == "cool-down"] == [False]
        got = client.get("/api/skills/export/global/cool-down", headers=headers)
        assert got.headers["content-type"] == "application/zip" and "cool-down.zip" in got.headers["content-disposition"]
        assert client.get("/api/skills/export/kelvin/lock-in-measurement", headers=headers).status_code == 200
        assert client.delete("/api/know/skill/global/cool-down", headers=headers).json() == {"ok": True}
        assert client.get("/api/skills/export/global/cool-down", headers=headers).status_code == 404
