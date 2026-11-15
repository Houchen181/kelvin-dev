"""The 0.3.52 audit's findings on lab scripts and run_python's sandbox (SC1-SC8), kept fixed. Each test runs the
audit's own repro (tests/audit_repros/scripts, copied from audit-repros/scripts) and reads what it says happened."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REPROS = HERE / "audit_repros" / "scripts"


def repro(name):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join([str(HERE.parent / "src"), str(HERE)]))
    r = subprocess.run([sys.executable, str(REPROS / (name + ".py"))], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600, env=env, cwd=str(HERE.parent))
    return r.stdout + r.stderr


def test_sc1_a_binary_blocks_command_is_one_command():
    out = repro("binary_block")
    assert "holds more than one command" in out, out[-3000:]
    assert "V = 0.0 V (limit 10), output = 0" in out.split("--- binary.py ---")[1], out[-3000:]


def test_sc2_a_cards_try_sends_a_reading_only_when_every_part_is_a_query():
    out = repro("cardtry_chain")
    assert "range: NOT TRIED" in out and "the instrument after the try: V = 0.0 V, output = 0" in out, out[-3000:]


def test_sc3_a_scripts_asyncio_connection_or_another_name_never_passes_the_gate():
    out = repro("socket_paths")
    parts = {part.split(" ---", 1)[0]: part for part in out.split("--- ")[1:]}
    assert {"through_kelvin.py", "with_asyncio.py", "other_name.py"} <= set(parts), out[-3000:]
    for part in parts.values():
        assert "lines that reached the instrument's own port, past Kelvin: []" in part, part[:3000]
    asyncio_part = parts["with_asyncio.py"]
    if os.name == "nt":   # the proactor connects below the socket's own connect (ConnectEx): refused outright
        assert "asyncio's own connection would go past it" in asyncio_part, asyncio_part
    else:                 # the selector loop (Linux, macOS) connects and sends with the socket's methods: gated
        assert "commands the gate saw: ['SLVL 5.0']" in asyncio_part, asyncio_part
    assert "It opens lockin2 (TCPIP0::localhost" in out   # another name of the lab's own address


def test_sc4_other_spellings_of_a_lab_instruments_address_go_through_the_gate():
    out = repro("address_spellings")
    assert "NOT matched" not in out, out[-3000:]
    assert out.count("(through the gate)") == 8, out[-3000:]


@pytest.mark.skipif(not os.name == "nt", reason="the escapes are Windows' (O_TEMPORARY, _overlapped)")
def test_sc5_run_pythons_known_escapes_are_refused():
    pytest.importorskip("numpy")
    pytest.importorskip("h5py")                     # the repro keeps and reads runs' run.h5
    out = repro("sandbox_claims")
    assert "lab.yaml now reads: 'limits: { V: [-10, 10] }" in out, out[-3000:]
    assert "the other run's points/v: [np.float64(1.0), np.float64(2.0), np.float64(3.0)]" in out, out[-3000:]
    assert "commands-copy.jsonl still exists: True" in out
    head = out.split("--- 5.")[0]
    assert head.count("REFUSED") >= 14, head


@pytest.mark.skipif(not os.name == "nt", reason="Windows' integrity levels")
def test_sc5_the_child_runs_at_low_integrity_and_writes_only_its_own_folder(tmp_path):
    """The operating system's restriction under the audit hook: once lowered, the process writes in a folder labelled
    low and nowhere else, whatever code (native too) does it."""
    from labharness.sandbox import _label_low
    low, high = tmp_path / "low", tmp_path / "high"
    low.mkdir()
    high.mkdir()
    assert _label_low(low) is None
    runner = HERE.parent / "src" / "labharness" / "data" / "sandbox"
    code = ("import sys; sys.path.insert(0, %r); import kelvin_sandbox as k; why = k._low_integrity(); "
            "print('lowered', why); open(%r, 'w').write('ok'); print('low folder written')\n"
            "try:\n    open(%r, 'w').write('no')\n    print('other folder written')\n"
            "except PermissionError:\n    print('other folder refused')\n"
            % (str(runner), str(low / "a.txt"), str(high / "b.txt")))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert "lowered None" in r.stdout and "low folder written" in r.stdout, r.stdout + r.stderr
    assert "other folder refused" in r.stdout, r.stdout + r.stderr


def test_sc6_the_text_the_person_approved_is_the_text_that_runs():
    out = repro("file_changed_after_approval")
    assert "this is the text Kelvin read and the person approved; VMAX = 1.0" in out, out[-3000:]
    assert "AFTER KELVIN READ IT" not in out.split("what the script's Python printed")[1], out[-3000:]


def test_sc7_a_cards_own_chained_setting_from_a_script_goes_whole():
    out = repro("chain_setter_split")
    script = out.split("=== the same text from a lab script ===")[1]
    assert "Send raw command?" not in script and "target now: 1" in script, script


def test_sc8_pyvisas_get_instrument_goes_through_kelvin():
    out = repro("pyvisa_alias")
    assert "what went past Kelvin (past.txt): nothing" in out, out[-3000:]
