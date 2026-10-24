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
- Never commit API keys, lab data, or a machine-specific `.mcp.json`. `.gitignore` already excludes them; keys live in `~/Kelvin/users/<name>/settings.json`.
- Changes to the safety gate (`gate.py`), the card format (`cards.py`) or the approval path need a test that shows the rule holds.

## Releasing

To publish a new version: bump the version in `src/labharness/_version.py`, `pyproject.toml` and the README's download button (`tests/test_version.py` fails if one is left behind), then push a tag such as `v0.3.19`. GitHub builds both apps, tests them, and publishes the release. The tag must match the version.

Installed Kelvins find the release by themselves (`updater.py`): twice a day they look at this repository's releases, download the installer for their channel, check it against the SHA-256 digest GitHub reports for the file, and install it when Kelvin closes (or at once with **Restart to update**). While the repository is private, they reach it through the GitHub CLI (`gh`) signed in on that computer; anonymous checks start working once it is public. On macOS Kelvin only says a new version is out and links to it.

## Claude Code bridge (temporary, developer only)

On the developer's own computer, Kelvin can use the Claude Code there: chats answered on that person's Claude plan (models **Claude Opus / Sonnet / Haiku (Claude Code)**), and `operate_computer`, where Claude works the screen, mouse and keyboard through Kelvin's screen tools for programs without an API. It is to be removed.

- **Only here:** Anthropic's terms don't let third-party apps route requests through people's Claude plans or handle their Claude credentials. So the bridge exists only when Kelvin runs from source, or in a private build with `src/labharness/_private_bridge.py` (gitignored). Published releases never have it. Kelvin only starts the unmodified `claude` program and never reads its credentials.
- **Needs:** the Claude Code command line, signed in (`claude`, then `/login`). Kelvin finds it on PATH or in `~/.local/bin`; otherwise give its path in Settings → **Claude Code on this computer**, which also shows whether it is signed in and switches the bridge off per person.
- **Chats:** each turn is one `claude -p` run with Claude Code's own tools off, Kelvin's system prompt, and Kelvin's tools described as `<tool_call name="...">{json}</tool_call>`. Kelvin runs the calls in its own loop, through the safety gate and approvals like any model's.
- **Operating the computer:** `operate_computer` runs `claude -p` with only Kelvin's screen tools (`python -m labharness.screen`, or `Kelvin.exe --screen-mcp`). Each step shows under the tool's step in the chat and goes to the record (`computer` events, by Claude Code). There is no extra approval, but while it runs Kelvin's approvals, settings, lab, queue and terminal are locked (HTTP 423), so a click can never stand in for a person's yes. Stop ends it. Mouse and keyboard work on Windows; on macOS they need pyobjc's Quartz.
- **The private build:** push a tag `private-vX.Y.Z` (the version in `_version.py`). GitHub builds the Windows installer with `_private_bridge.py` (gitignored, never committed) as a pre-release, `Kelvin-Setup-X.Y.Z-private.exe`, never marked latest. It has the same app id and folder as the published installer, so it replaces the installed Kelvin like any update; a private Kelvin then updates itself from later `private-v` pre-releases, a published one from `v` releases. The release check fails if the bridge is in a published build or missing from a private one. The README's Windows download points to the rolling pre-release `private-latest`, which each private tag refreshes with its installer as `Kelvin-Setup-private.exe` (GitHub's `releases/latest` can't point at a pre-release); installed Kelvins ignore that tag and update from the numbered ones. To try it locally instead: create `src/labharness/_private_bridge.py`, run `pyinstaller packaging/kelvin.spec --noconfirm --distpath dist/private --workpath build/private`, delete the file again; `Kelvin.exe --self-test` says whether the bridge is in. Don't keep such a copy next to the installed Kelvin.
- **Removing it:** delete `claudecode.py`, `screen.py`, `web/providers/claudecode_provider.py`, `tests/test_claude_code.py` and `tests/fake_claude.py`, and the code marked "Claude Code bridge" in `web/app.py`, `web/agent.py`, `web/settings.py`, `agent/runtime.py`, `desktop.py`, `packaging/kelvin_app.py` and the Settings section in `index.html`/`app.js`.

## Code layout

The Python package is still called `labharness` (`import labharness`); the product, commands and repo are Kelvin.

| Path | What it is |
|---|---|
| `src/labharness/cards.py` | Instrument card schema: parameters tagged measure/drive, safe values, limits, ramps, health checks |
| `src/labharness/scpi.py` | SCPI command templates (short/long forms, optional nodes, compound commands) |
| `src/labharness/labconfig.py` | Lab config (lab limits, rates, guards, interlocks, trust) and project envelopes |
| `src/labharness/gate.py` | The safety gate |
| `src/labharness/plans.py`, `engine.py` | Plan format; validate → preflight (reused for 10 min for measuring plans) → approve → run → log; a dry run only when asked or in auto mode |
| `src/labharness/reports/` | Reports: gathering runs and the log (`collect.py`), shared plot layout (`chart.py`), the PDF and PowerPoint writers |
| `src/labharness/web/remote.py`, `qr.py`, `static/mobile.*` | Phone access: pairing, device roles, the network listener, QR codes, the phone page |
| `src/labharness/monitor.py`, `notify.py` | Watches and alarms on a background thread; in-app notices and the optional webhook |
| `src/labharness/adapters.py`, `discovery.py` | VISA/serial connections for real instruments; the read-only scan that matches instruments to cards |
| `src/labharness/eventlog.py` | Append-only event log; one folder per run (plan, snapshots, data, meta) |
| `src/labharness/sim/` | Twins that answer the card's own commands, plus a toy sample |
| `src/labharness/agent/` | MCP server (`server.py`), plain-text tool answers (`runtime.py`), approval page (`approval.py`, `ui.html`) |
| `src/labharness/web/` | The portal: web API (`app.py`), chat loop (`agent.py`), short answers (`brevity.py`), one module per model provider (`providers/`), chats and settings, browser UI and the logo (`static/`) |
| `src/labharness/data/` | Cards for the virtual lab, its lab config and a demo envelope |
| `src/labharness/hub/` | The Kelvin hub: online accounts across labs and the work Kelvins share (`db.py`), its web server and API (`server.py`, `kelvin-hub`), and a Kelvin's side of it (`client.py`; in the portal, `web/hubsync.py`: Settings → Account → Kelvin hub, and the background sync). See [hub.md](hub.md) |
| `src/labharness/desktop.py` | The desktop app: the portal in a native window, one copy at a time, `--self-test` |
| `packaging/` | PyInstaller recipe (`kelvin.spec`), Windows installer (`kelvin.iss`), app icons (`make_icons.py`, `icons/`, and the iPhone app's) |
| `ios/` | Kelvin for iPhone: SwiftUI app, the KelvinKit package, XcodeGen spec. See [ios.md](ios.md) |

## Brand

| | |
|---|---|
| Name | Kelvin, "the coolest cat in the lab" |
| Mark | a sleeping cat beside a snowflake: [`kelvin.svg`](../src/labharness/web/static/kelvin.svg) |
| Colors | navy `#1b3a5c`, ice `#2f6fb0`, frost `#e8f2fc`, snowflake `#8cc4f2` |
| Charts | keep the validated data palette in `app.css` (`--series-1`, `--s1`…`--s8`); brand colors are for chrome, not data |
