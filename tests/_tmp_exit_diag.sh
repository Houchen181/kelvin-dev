#!/usr/bin/env bash
# TEMPORARY (reverted before merge): Python's exit on Linux with run.h5 work in a daemon thread, with the fix
# holding h5py's lock through h5py's own exit.
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

cat > "$work/loop.py" <<'EOF'
import sys, threading, time
from labharness import rundata
park = sys.argv[1] == "park"
def work():
    n = 0
    while True:
        for folder in sys.argv[2:]:
            n += 1
            try:
                rundata.meta(folder)
                rundata.set_analysis(folder, {'assessment': 'pass %d' % n, 'series': []})
            except OSError as e:
                if park and "closing" in str(e):
                    threading.Event().wait()
            except Exception:
                pass
threading.Thread(target=work, daemon=True).start()
time.sleep(1.0)
EOF

tally() {   # name, runs, command...
  local name=$1 n=$2; shift 2
  local bad=0 shown=0 conv=0
  for i in $(seq 1 "$n"); do
    rc=0; timeout 90 "$@" > "$work/out.txt" 2>&1 || rc=$?
    grep -q "Failed to find converter" "$work/out.txt" && conv=$((conv + 1))
    if [ "$rc" -ne 0 ]; then
      bad=$((bad + 1))
      if [ "$shown" -lt 2 ]; then
        shown=$((shown + 1)); echo "--- $name run $i: exit $rc"; tail -15 "$work/out.txt"
      fi
    fi
  done
  echo "=== $name: $bad of $n exits not clean, $conv with converter messages"
}

tally "run.h5 loop, raising once refused" 100 python -X faulthandler "$work/loop.py" raise $runs
tally "run.h5 loop, parked once refused" 100 python -X faulthandler "$work/loop.py" park $runs
tally "flush_reports at exit" 15 python -X faulthandler tests/_tmp_flush_at_exit.py tests
for i in 1 2 3 4 5; do rc=0; python -m pytest -q -p no:cacheprovider tests/test_rundata.py -k "exits" > "$work/out.txt" 2>&1 || rc=$?; echo "regression tests $i: pytest exit $rc, $(tail -1 "$work/out.txt")"; done
true
