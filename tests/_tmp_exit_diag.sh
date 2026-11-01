#!/usr/bin/env bash
# TEMPORARY (reverted before merge): where Python 3.13 crashes as it exits on Linux after run.h5 work at exit.
set -u
which gdb || (sudo apt-get update -qq && sudo apt-get install -y -qq gdb > /dev/null)
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
                if "closing" in str(e):
                    sys.__stderr__.write("[loop] refused: %s\n" % e)
                    if park:
                        threading.Event().wait()
            except Exception as e:
                sys.__stderr__.write("[loop] %s: %s\n" % (type(e).__name__, e))
threading.Thread(target=work, daemon=True).start()
time.sleep(1.0)
EOF

for mode in park raise; do
  found=0
  for i in $(seq 1 120); do
    timeout 120 gdb -q -batch -ex "set pagination off" -ex "handle SIGPIPE nostop noprint" -ex run \
      -ex "echo \n=== CRASH backtraces\n" -ex "thread apply all bt 40" \
      --args python "$work/loop.py" "$mode" $runs > "$work/gdb.txt" 2>&1
    if grep -q "SIGSEGV\|SIGBUS\|SIGABRT" "$work/gdb.txt"; then
      echo "##### $mode: crashed on try $i"
      grep -v "^\[New Thread\|^\[Thread .* exited\|^\[Inferior\|libthread_db\|Using host" "$work/gdb.txt" | tail -150
      found=1
      break
    fi
  done
  [ "$found" = 0 ] && echo "##### $mode: no crash in 120 tries under gdb"
done
true
