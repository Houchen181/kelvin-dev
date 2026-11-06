"""Kelvin's helper for Teledyne Princeton Instruments cameras: PICam (Picam.dll) runs in this process, never in Kelvin's
own, so a PICam that hangs or crashes takes only this process with it, and Kelvin can end it (labharness/princeton.py).

Kelvin starts it with its own Python (the app runs it with --bridge) and sends its import path on the first line, then
JSON requests, one per line, each answered on a line of its own (princeton.serve). Anything PICam prints goes to
stderr, so it can't get in the way of the replies."""
import json
import os
import sys


def main():
    out = os.fdopen(os.dup(1), "wb")   # the replies go on the real stdout
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    inp = os.fdopen(0, "rb")
    first = json.loads(inp.readline().decode("utf-8") or "{}")
    sys.path[:] = list(dict.fromkeys([p for p in first.get("path") or [] if p] + sys.path))
    from labharness import princeton
    princeton.serve(inp, out)


if __name__ == "__main__":
    main()
