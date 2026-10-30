"""Keeping work apart (VISION point 2), from the overnight test of 2026-10-30: Kelvin's AI never reads Kelvin's own
folder or a file that holds sign-ins; a confidential project's runs are seen only from inside it, by the models its
rules allow; reports and records leave it out elsewhere; phones through the Kelvin hub get lab-wide records without
its names and data; and a run is filed with the project and rules of the chat that started it."""
import pytest

from labharness import ScriptedApprover, folderfiles
from labharness.agent.runtime import LabRuntime
from simlab import build_virtual_lab


@pytest.fixture
def rt(tmp_path):
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 50), tmp_path / "data")


# ------------------------------------------------------------------ Kelvin's own folder and sign-ins (t10)

def test_read_examples_never_reads_kelvins_own_folder_or_a_file_with_sign_ins(rt, tmp_path):
    home = tmp_path / "Kelvin"
    (home / "users" / "ada" / "chats").mkdir(parents=True)
    (home / "settings.json").write_text('{"access_key": "portal-key-123"}', encoding="utf-8")
    (home / "lab.yaml").write_text("name: Bench\ninstruments: {}\n", encoding="utf-8")
    (home / "users" / "ada" / "chats" / "notes.md").write_text("Ada's chat", encoding="utf-8")
    rt.home = home
    maker = tmp_path / "Maker SDK"
    (maker / "examples").mkdir(parents=True)
    (maker / "examples" / "demo.py").write_text("sdk.connect('COM3')", encoding="utf-8")
    (maker / "README.txt").write_text("Call connect first.", encoding="utf-8")
    (maker / ".env").write_text("OPENAI_API_KEY=sk-env-456", encoding="utf-8")
    (maker / "credentials.json").write_text('{"password": "pw-789"}', encoding="utf-8")
    (maker / ".aws").mkdir()
    (maker / ".aws" / "config.txt").write_text("aws_secret_access_key=aws-000", encoding="utf-8")

    for named in (home / "settings.json", home / "lab.yaml", home, home / "users"):
        out = rt.read_examples(str(named))
        assert out.startswith("not read") and "Kelvin's own folder" in out, named
        assert "portal-key-123" not in out and "Bench" not in out
    for named in (maker / ".env", maker / "credentials.json", maker / ".aws" / "config.txt"):
        out = rt.read_examples(str(named))
        assert out.startswith("not read") and "sign-ins, keys or tokens" in out, named
    # a folder that holds Kelvin's own folder, or files with sign-ins, is read without them
    text = rt.read_examples(str(tmp_path))
    assert "sdk.connect('COM3')" in text and "Call connect first." in text
    for kept in ("portal-key-123", "Bench", "Ada's chat", "sk-env-456", "pw-789", "aws-000"):
        assert kept not in text, kept
    # the lab's scripts are read the same way
    (home / "env").mkdir()
    (home / "env" / "probe.py").write_text("import pyvisa\nrm = pyvisa.ResourceManager()\n", encoding="utf-8")
    assert "Kelvin's own folder" in rt.survey_scripts(str(home / "env"))
    assert "probe.py" not in rt.survey_scripts(str(tmp_path))


def test_a_chats_file_tools_never_read_or_save_a_file_with_sign_ins(tmp_path):
    folder = tmp_path / "Sample A"
    (folder / ".ssh").mkdir(parents=True)
    (folder / ".env").write_text("TOKEN=abc", encoding="utf-8")
    (folder / "secret project").mkdir()   # a folder of the person's own may be named so
    (folder / "secret project" / "iv.csv").write_text("V,I\n0,0\n", encoding="utf-8")
    for path in (".env", ".ssh/id_ed25519", "api_token.txt"):
        with pytest.raises(folderfiles.FolderError, match="sign-ins, keys or tokens"):
            folderfiles.inside(folder, path)
    with pytest.raises(folderfiles.FolderError, match="sign-ins, keys or tokens"):
        folderfiles.save(folder, ".ssh/authorized_keys", "ssh-ed25519 AAAA")
    text, _ = folderfiles.read(folder, "secret project/iv.csv")
    assert "0,0" in text
