"""Entry point PyInstaller freezes into Kelvin.exe / Kelvin.app."""
import sys

if len(sys.argv) > 2 and sys.argv[1] == "--bridge":   # the helper for a maker's library (labharness/library.py)
    import runpy
    sys.argv = sys.argv[2:]
    runpy.run_path(sys.argv[0], run_name="__main__")
    sys.exit(0)

from labharness.desktop import main  # noqa: E402

sys.exit(main())
