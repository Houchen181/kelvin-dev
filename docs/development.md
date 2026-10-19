# Development

Running Kelvin from a checkout, testing it, how the code is laid out, and how a release is made.

## Run the portal from source

The same chat site runs from a checkout, which is handy for development. It needs Python ≥ 3.10:

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -e ".[web]"
```

```bash
.venv/Scripts/kelvin
```

The last command opens `http://127.0.0.1:8770` in your browser. Bookmark that link: the access key after `#` stays the same between restarts. To try the portal without an API key, add `--demo`; this gives you a scripted model that is not AI (try *sweep the gate* or *map gate against field*). The virtual lab runs at 10× real time so you can watch runs; `--speed 0` makes it instant. Add `--lab my_lab.yaml` to load a different simulated lab (for example many instruments at once); the format is `src/labharness/data/virtual_lab.yaml`.

## Try the core without an agent

Requires Python ≥ 3.9 and PyYAML. No hardware is needed.

```bash
python examples/demo.py
```

```bash
python -m pytest
```

Run `python examples/demo.py --ask` to answer the approval requests yourself. If a pytest plugin installed elsewhere breaks collection, set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

## Working together

Set up a checkout (Python ≥ 3.10 for everything; the core alone runs on 3.9):

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -e ".[web,dev]"
```

```bash
.venv/Scripts/python -m pytest
```

- Work on a branch and open a pull request into `main`. GitHub Actions runs the tests on Python 3.9 (core) and 3.13 (everything) for every push and PR.
- [VISION.md](../VISION.md) is the spec. [ARCHITECTURE.md](../ARCHITECTURE.md) records the design decisions and the build order; update it when a decision changes.
- Never commit API keys, lab data, or a machine-specific `.mcp.json`. `.gitignore` already excludes them; keys live in `~/Kelvin/settings.json`.
- Changes to the safety gate (`gate.py`), the card format (`cards.py`) or the approval path need a test that shows the rule holds.

## Releasing

To publish a new version: bump the version in `src/labharness/_version.py` and `pyproject.toml`, then push a tag such as `v0.3.4`. GitHub builds both apps, tests them, and publishes the release.

## Code layout

The Python package is still called `labharness` (`import labharness`); the product, commands and repo are Kelvin.

| Path | What it is |
|---|---|
| `src/labharness/cards.py` | Instrument card schema: parameters tagged measure/drive, safe values, limits, ramps, health checks |
| `src/labharness/scpi.py` | SCPI command templates (short/long forms, optional nodes, compound commands) |
| `src/labharness/labconfig.py` | Lab config (lab limits, rates, guards, interlocks, trust) and project envelopes |
| `src/labharness/gate.py` | The safety gate |
| `src/labharness/plans.py`, `engine.py` | Plan format; validate → preflight → dry run → approve → run → log |
| `src/labharness/reports/` | Reports: gathering runs and the log (`collect.py`), shared plot layout (`chart.py`), the PDF and PowerPoint writers |
| `src/labharness/web/remote.py`, `qr.py`, `static/mobile.*` | Phone access: pairing, device roles, the network listener, QR codes, the phone page |
| `src/labharness/monitor.py`, `notify.py` | Watches and alarms on a background thread; in-app notices and the optional webhook |
| `src/labharness/adapters.py`, `discovery.py` | VISA/serial connections for real instruments; the read-only scan that matches instruments to cards |
| `src/labharness/eventlog.py` | Append-only event log; one folder per run (plan, snapshots, data, meta) |
| `src/labharness/sim/` | Twins that answer the card's own commands, plus a toy sample |
| `src/labharness/agent/` | MCP server (`server.py`), plain-text tool answers (`runtime.py`), approval page (`approval.py`, `ui.html`) |
| `src/labharness/web/` | The portal: web API (`app.py`), chat loop (`agent.py`), one module per model provider (`providers/`), chats and settings, browser UI and the logo (`static/`) |
| `src/labharness/data/` | Cards for the virtual lab, its lab config and a demo envelope |
| `src/labharness/desktop.py` | The desktop app: the portal in a native window, one copy at a time, `--self-test` |
| `packaging/` | PyInstaller recipe (`kelvin.spec`), Windows installer (`kelvin.iss`), app icons (`make_icons.py`, `icons/`) |

## Brand

| | |
|---|---|
| Name | Kelvin, "the coolest cat in the lab" |
| Mark | a sleeping cat beside a snowflake: [`kelvin.svg`](../src/labharness/web/static/kelvin.svg) |
| Colors | navy `#1b3a5c`, ice `#2f6fb0`, frost `#e8f2fc`, snowflake `#8cc4f2` |
| Charts | keep the validated data palette in `app.css` (`--series-1`, `--s1`…`--s8`); brand colors are for chrome, not data |
