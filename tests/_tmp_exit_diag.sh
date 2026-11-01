#!/usr/bin/env bash
# TEMPORARY (reverted before merge): which daemon threads crash Python 3.13 as it exits on Linux, with the fix.
set -u
work=$(mktemp -d)
python - "$work" <<'EOF'
import sys
from pathlib import Path
from labharness import rundata
root = Path(sys.argv[1])
for k in range(3):
    d = root / str(k) / "20261024-101500-gate_sweep"
    d.mkdir(parents=True)
    r = rundata.Recorder(d)
    r.write("start", {"plan": "plan: gate_sweep\n", "meta": {"run_id": d.name, "plan": "gate_sweep"}})
    r.write("row", values={"i": 0, "t": 0.0, "smu.V": 0.5})
    r.close()
    rundata._let_go(r._lock)
    rundata.finish(d)
EOF
runs="$work/0/20261024-101500-gate_sweep $work/1/20261024-101500-gate_sweep $work/2/20261024-101500-gate_sweep"

cat > "$work/busy.py" <<'EOF'
import sys, threading, time
if sys.argv[1] == "h5py":
    import h5py, numpy  # noqa: F401
def work():
    n = 0
    while True:
        n += 1
        try:
            raise OSError("x %d" % n)
        except Exception:
            pass
threading.Thread(target=work, daemon=True).start()
time.sleep(1.0)
EOF

cat > "$work/loop.py" <<'EOF'
import sys, threading, time
from labharness import rundata
pause = float(sys.argv[1])
def work():
    n = 0
    while True:
        for folder in sys.argv[2:]:
            n += 1
            try:
                rundata.meta(folder)
                rundata.set_analysis(folder, {'assessment': 'pass %d' % n, 'series': []})
            except Exception:
                if pause:
                    time.sleep(pause)
threading.Thread(target=work, daemon=True).start()
time.sleep(1.0)
EOF

tally() {   # name, runs, command...
  local name=$1 n=$2; shift 2
  local bad=0 shown=0
  for i in $(seq 1 "$n"); do
    rc=0; timeout 60 "$@" > "$work/out.txt" 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
      bad=$((bad + 1))
      if [ "$shown" -lt 2 ]; then
        shown=$((shown + 1)); echo "--- $name run $i: exit $rc"; tail -40 "$work/out.txt"
      fi
    fi
  done
  echo "=== $name: $bad of $n exits not clean"
}

tally "pure Python busy daemon" 30 python -X faulthandler "$work/busy.py" none
tally "busy daemon, h5py imported" 30 python -X faulthandler "$work/busy.py" h5py
tally "run.h5 loop, fixed, spinning" 30 python -X faulthandler "$work/loop.py" 0 $runs
tally "run.h5 loop, fixed, 5 ms after a refusal" 30 python -X faulthandler "$work/loop.py" 0.005 $runs
tally "flush_reports at exit, fixed" 8 python -X faulthandler tests/_tmp_flush_at_exit.py tests
