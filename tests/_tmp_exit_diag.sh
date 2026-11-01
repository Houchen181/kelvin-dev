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

cat > "$work/jsonspin.py" <<'EOF'
import json, sys, threading, time
big = {"k%d" % i: [float(j) for j in range(50)] for i in range(200)}
def work():
    if sys.argv[1] == "h5":   # this thread reads run.h5 a few times first, then only encodes JSON
        from labharness import rundata
        for folder in sys.argv[2:]:
            rundata.meta(folder)
    while True:
        json.dumps(big)
threading.Thread(target=work, daemon=True).start()
time.sleep(1.0)
EOF

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
  local bad=0 shown=0
  for i in $(seq 1 "$n"); do
    rc=0; timeout 60 "$@" > "$work/out.txt" 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
      bad=$((bad + 1))
      if [ "$shown" -lt 2 ]; then
        shown=$((shown + 1)); echo "--- $name run $i: exit $rc"; tail -12 "$work/out.txt"
      fi
    fi
  done
  echo "=== $name: $bad of $n exits not clean"
}

tally "A json spin, no h5py" 60 python -X faulthandler "$work/jsonspin.py" none
tally "D json spin after run.h5 reads in that thread" 60 python -X faulthandler "$work/jsonspin.py" h5 $runs
tally "B run.h5 loop, fixed, raising" 60 python -X faulthandler "$work/loop.py" raise $runs
tally "C run.h5 loop, fixed, parked once refused" 60 python -X faulthandler "$work/loop.py" park $runs
