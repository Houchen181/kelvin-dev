#!/usr/bin/env bash
# TEMPORARY (reverted before merge): what crashes Python 3.13 as it exits on Linux after run.h5 work, with the fix.
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
python -c "import h5py; print('h5py', h5py.__version__, 'HDF5', h5py.version.hdf5_version, 'threadsafe', h5py.get_config().__dict__.get('_threadsafe', '?'))" || true
python -c "import h5py; c = h5py.get_config(); print([a for a in dir(c) if not a.startswith('__')])" || true

cat > "$work/e.py" <<'EOF'
import atexit, sys, threading, time
from labharness import rundata
mode, folders = sys.argv[1], sys.argv[2:]
def burst():
    for n in range(40):
        for folder in folders:
            rundata.meta(folder)
            rundata.set_analysis(folder, {'assessment': 'pass %d' % n, 'series': []})
if mode == "main":            # H: all of it in the main thread, no other thread
    burst()
else:
    def work():
        burst()
        if mode == "park":    # E: the thread stays, idle, until Python exits
            threading.Event().wait()
    t = threading.Thread(target=work, daemon=True)
    t.start()
    if mode == "ended":       # I: the thread has ended before Python exits
        t.join()
    if mode == "nounreg":     # F: as E, but h5py's own exit is left out
        import h5py
        atexit.unregister(h5py._unregister_converters)
    time.sleep(3.0)
EOF

tally() {   # name, runs, command...
  local name=$1 n=$2; shift 2
  local bad=0 shown=0
  for i in $(seq 1 "$n"); do
    rc=0; timeout 60 "$@" > "$work/out.txt" 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
      bad=$((bad + 1))
      if [ "$shown" -lt 1 ]; then
        shown=$((shown + 1)); echo "--- $name run $i: exit $rc"; tail -12 "$work/out.txt"
      fi
    fi
  done
  echo "=== $name: $bad of $n exits not clean"
}

tally "H main thread only" 40 python -X faulthandler "$work/e.py" main $runs
tally "I worker ended before exit" 40 python -X faulthandler "$work/e.py" ended $runs
tally "E worker idle (parked) at exit" 40 python -X faulthandler "$work/e.py" park $runs
tally "F worker parked, h5py's unregister left out" 40 python -X faulthandler "$work/e.py" nounreg $runs
