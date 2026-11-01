"""A run's automatic reports (flush_reports, in its _report_timer thread) being rewritten as Python exits."""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])          # the tests' folder (simlab)
from labharness import ScriptedApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from simlab import build_virtual_lab  # noqa: E402

PLAN = """plan: gate_sweep
sweep: { smu.V: {from: 0, to: 1, step: 0.25} }
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""

rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 20), Path(tempfile.mkdtemp()) / "data", None)
rt.report_delay = 0.2
for _ in range(3):
    rt.run(PLAN)


def flushing():
    for t in threading.enumerate():
        f = sys._current_frames().get(t.ident)
        while f is not None:
            if f.f_code.co_name in ("read_h5", "columns", "card"):
                return True
            f = f.f_back
    return False


deadline = time.monotonic() + 30
while time.monotonic() < deadline and not flushing():
    time.sleep(0.001)
print("main: exiting while the reports are rewritten" if flushing() else "main: never saw it", flush=True)
