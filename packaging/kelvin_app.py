"""Entry point PyInstaller freezes into Kelvin.exe / Kelvin.app."""
import sys

if len(sys.argv) > 2 and sys.argv[1] == "--bridge":   # the helper for a maker's library (labharness/library.py)
    import runpy
    sys.argv = sys.argv[2:]
    runpy.run_path(sys.argv[0], run_name="__main__")
    sys.exit(0)

if len(sys.argv) > 1 and sys.argv[1] == "--screen-mcp":   # Claude Code bridge (private build only): screen tools
    from labharness.screen import main as screen_main
    sys.exit(screen_main())

from labharness.desktop import main  # noqa: E402

sys.exit(main())
