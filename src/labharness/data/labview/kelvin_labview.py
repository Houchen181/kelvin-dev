"""Kelvin's reader of a lab's LabVIEW files, started by labharness/viworker.py as a process of its own: it reads VIs
and VI libraries as bytes (never runs one) and says each one's summary.

stdin: one line of JSON, the job: path (Kelvin's import path), memory_mb, folder (where sub-VIs may be read from),
own (Kelvin's own folders, never read), files ([kind, path], kind "vi" or "llb").
stdout: a line of JSON for each summary, as it is made (viworker.serve); anything else printed goes to stderr."""
import json
import os
import sys


def main():
    out = os.fdopen(os.dup(1), "wb")   # the summaries go on the real stdout; a stray print can't get in their way
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    job = json.loads(os.fdopen(0, "rb").readline().decode("utf-8"))
    sys.path[:] = list(dict.fromkeys([p for p in job.get("path") or [] if p] + sys.path))
    from labharness import viworker
    viworker.serve(job, out)


if __name__ == "__main__":
    main()
