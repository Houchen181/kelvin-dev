<p align="center">
  <img src="src/labharness/web/static/kelvin.svg" width="112" alt="Kelvin: a sleeping cat beside a snowflake">
</p>

<h1 align="center">Kelvin</h1>
<p align="center"><b>The coolest cat in the lab.</b><br>An AI lab assistant for experimental physicists that plans, measures and logs, and asks before it drives your sample harder.</p>

---

Kelvin connects Claude, GPT, Gemini or a local model to your instruments through a safety gate. Measurement runs freely. Anything that pushes more power, voltage, current, field or temperature into the sample is checked against limits and interlocks, approved by a person, ramped, and watched. Like its namesake, Kelvin stays calm and never rushes.

See [VISION.md](VISION.md) for the goals and [ARCHITECTURE.md](ARCHITECTURE.md) for the design.

**Status:** The core rules, formats and a fully simulated lab are done (Phase 0). The MCP server, the approval page, the chat portal and the desktop app are done, so anyone can install Kelvin and drive the virtual lab. Real-instrument adapters, roles and locks, and monitoring come later.

## Download Kelvin

Get the latest version from **[Releases](https://github.com/Houchen181/kelvin/releases/latest)**. No Python needed.

| | |
|---|---|
| **Windows 10/11** | Run `Kelvin-Setup-<version>.exe`. It installs for your user only, so no admin rights are needed, and adds Kelvin to the Start menu. The installer isn't code-signed yet, so Windows may say "Windows protected your PC": choose **More info → Run anyway**. |
| **macOS (Apple silicon)** | Unzip `Kelvin-<version>-macOS.zip` and move `Kelvin.app` to Applications. It isn't signed yet, so the first time, right-click it and choose **Open**. |

Kelvin opens in its own window. It starts with a simulated lab and a scripted demo model; add an API key under Settings to talk to Claude, GPT or Gemini. Everything it keeps (settings, keys, chats, lab logs and runs) stays in `~/Kelvin` on your computer. Kelvin runs one copy at a time; opening it again brings up the running one in your browser.

To publish a new version: bump `src/labharness/_version.py`, then push a tag such as `v0.2.0`. GitHub builds both apps, tests them, and publishes the release.

## The portal from source

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

- **Models:** Add keys under Settings for Claude, GPT or Gemini, or point it at a local OpenAI-compatible server (Ollama, vLLM, LM Studio) for confidential work. Keys stay in `~/Kelvin/settings.json` and go only to their own provider.
- **Projects:** The sidebar groups conversations into projects, for example one per sample or experiment. Make a project with **+**, start a chat inside it with its **+**, and move a conversation with its **⋯** menu or by dragging it onto a project. Deleting a project keeps its conversations.
- **Memory:** Kelvin learns from conversations, but keeps three levels that never mix:
  - **You, in every chat:** your instructions and the preferences and conventions Kelvin kept, such as units or how careful to be (Settings → Memory).
  - **One project:** that project's instructions and notes, such as decisions, procedures, instrument quirks and results (the project's **⋯ → Project settings**). Only chats in that project see them. Deleting a project deletes its memory.
  - **The conversation itself:** a new chat never sees another chat's history.

  Kelvin keeps short, typed notes, not transcripts, and never questions or chit-chat. It merges a fact it already knows instead of saving it twice. Recalled notes come with your message, and a reply says which ones it used ("Recalled 2 notes"). A large project recalls only the notes that bear on the question. You can delete any note, or switch off **Learn from conversations**.
- **Incognito:** For a question that has nothing to do with your work, press **Incognito** on a new chat. Kelvin then uses no memory and keeps none, and the chat is never saved or listed; it is gone when you leave it. Instrument commands and runs are still logged, for safety.
- **Conversation:** Answers stream in Markdown. Each tool call is a step you can expand, and anything that drives the sample harder appears as an **Approve / Decline** card inside the conversation.
- **Runs pane:** Measurements get their own pane beside the chat, like a terminal pane, with one tab per run. It opens when a plan starts, including plans started by another agent. While the plan runs, the pane shows a progress bar, the points done, the time left and plots that grow point by point, plus a Stop button. When the run ends, Kelvin analyses the data. The conversation gets a short card with the assessment and the next step. The pane shows the key results with uncertainties, the plots with their fits (hover for values), the table and the plan. Drag the pane's edge to resize it; open earlier runs from its clock button.
- **Lab panel:** Shows live instrument values (drives away from safe are highlighted) and the activity log.
- **Safety:** Kelvin uses the same MCP tools and safety gate as external agents, so it has the same rules and no extra power. Only one conversation drives the lab at a time; Stop ends the current answer and any ramp.

## Connect another AI agent (MCP)

```bash
.venv/Scripts/python -m pip install -e ".[agent]"
```

```bash
.venv/Scripts/kelvin-mcp
```

By default the server starts the virtual lab in instant simulated time. Use `--speed 10` for 10× real time. Event logs and run folders go to `~/Kelvin/virtual-lab`. The server also opens the **approval page** in your browser; its URL, which includes an access key, is printed on the server's stderr.

On the approval page you can:
- **Approve or decline** what the agent asks for. An unanswered request is declined after 120 s (`--approval-timeout`). No MCP tool can answer approvals.
- **Watch** every instrument (drives away from safe are highlighted) and the activity log.
- **Press Stop.** This declines open requests and stops the agent's current ramp or plan. Drives stay where they are; a stopped plan runs its own `on_abort` steps.

Client setup. Use the full path to the venv's `kelvin-mcp` (or its Python with `-m labharness.agent`):

| Client | Where |
|---|---|
| Claude Code | copy `.mcp.example.json` to `.mcp.json` and set the path, or `claude mcp add kelvin -- <venv>/Scripts/kelvin-mcp` |
| Claude Desktop | `claude_desktop_config.json` → `"mcpServers": {"kelvin": {"command": "<venv>/Scripts/kelvin-mcp"}}` |
| Codex CLI | `~/.codex/config.toml` → `[mcp_servers.kelvin]` with `command` |
| Gemini CLI | `~/.gemini/settings.json` → `"mcpServers": {"kelvin": {"command": "<venv>/Scripts/kelvin-mcp"}}` |
| ChatGPT | Needs a public HTTPS MCP endpoint. The server only listens on 127.0.0.1 (`--transport streamable-http`), and putting a lab on the internet is out of scope for now |

A drive change blocks its tool call until a person answers. Set the client's tool timeout above the approval timeout: `MCP_TOOL_TIMEOUT` (ms) for Claude Code, `tool_timeout_sec` for Codex, `timeout` (ms) for Gemini CLI.

Tools: `lab_status`, `describe_instrument`, `get_value`, `set_value`, `send_raw`, `undo_last`, `health_check`, `validate_plan`, `simulate_plan`, `run_plan`, `analyze_run`, `list_runs`, `get_run_data`, `get_trace`, `recent_events`, `wait`.

**Security limit:** the access key keeps the model from approving its own requests through its tools. An agent that also has a shell on the same PC could still reach the page. For real hardware, run Kelvin on a machine where the agent has no shell.

## Plans: measurement loops

A plan is a short YAML file. The agent writes it, Kelvin dry-runs it on the twins, asks once for its drive ranges, runs it and records every point.

```yaml
plan: field_gate_map
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep:                                     # several keys = nested loops, outer first
  magnet.B: { values: [0, 0.5, 1], stabilize: { read: tc.T_sample, within: 0.01, for: 30 } }
  smu.V:    { from: 0, to: 4, step: 0.1, back: true }   # there and back (hysteresis)
repeat: 2                                  # repeat everything (outermost loop)
time: { every: 5, for: 60 }                # a time series at each point (innermost loop)
settle: 0.5
measure: [lockin.X, smu.I]
after: { smu.V: 0, magnet.B: 0 }
```

Sweep values can be `{from, to, step}`, `{from, to, points}`, `{from, to, points, log: true}` or `{values: [...]}`. A sweep axis can also set its own `settle` and a `stabilize` condition (wait until a reading changes by less than `within` over `for` seconds, up to `timeout`).

After a run, Kelvin classifies each measured curve and fits it: a peak or dip (Lorentzian or Gaussian: center, FWHM, height, Q for frequency axes), a straight line (slope, and resistance for I–V), an exponential relaxation (τ), a steady value or no signal. Every number has a one-standard-error uncertainty. It also tracks how the fitted feature changes along an outer loop, and it flags clipping, under-resolved or cut-off features, hysteresis, unsettled readings and stopped runs. The result is saved as `analysis.json` in the run folder; `analyze_run` re-fits a run with a chosen model.

## Real instruments

Kelvin talks to instruments over VISA: GPIB, USB-TMC, LAN and RS-232 / USB-serial. It uses the VISA library you already have (NI-VISA or Keysight IO Libraries), or a pure-Python one if there is none. Install the extra with `pip install "kelvin-lab[hardware]"`; the desktop app includes it.

**Find what is connected:**

```bash
kelvin-scan --write lab.yaml
```

The scan only asks each instrument who it is (`*IDN?`, or the card's own identity query); it never sets anything. Serial ports are tried with each known card's framing; for example, the Lake Shore 336 needs 57600 baud, 7 data bits and odd parity. The command matches each instrument to a card and drafts a lab config for you to review. The agent can run the same scan (`scan_instruments`), but only a person adds an instrument to the lab.

**Cards included** (written from the manuals; marked `community` until a lab verifies them on hardware):

| Kind | Models |
|---|---|
| Source-meters and sources | Keithley 2400-series, Keithley 2450/2460/2461/2470, Keysight B2900, Yokogawa GS200/GS210 |
| Lock-ins | SRS SR830/SR810, SRS SR860/SR865 |
| Temperature controllers (cryostats) | Lake Shore 335/336, Lake Shore 350 |
| Magnet supplies | Lake Shore 625, AMI 430 |
| Lasers and optics | Coherent OBIS, Thorlabs ITC4000 laser/TEC controller, Thorlabs PM100/PM400 power meter |
| Amplifiers | SRS SR560 voltage preamp, SRS SR570 current preamp (listen-only: Kelvin shows what it last set) |
| Meters and generators | Keysight/Agilent 344xxA DMMs, Keithley 2182A nanovoltmeter, Keysight 33500B/33600A generators |

Real hardware gets the same gate as the virtual lab, and a few protections of its own:
- **Measuring never powers anything up.** On some source-meters a reading command switches the output on, so those readings are only taken while the output is already on (`read_when`).
- **No speed-up on real time.** A lab with any real instrument runs on real time, so ramps keep their rates.
- **Risky commands stay locked.** Commands that change what readings mean, or that bypass the instrument's own protection, are blocked in the card: sensor curves, coil constants, persistent-switch heaters, current limits, and DMM resistance modes that push current into a device.
- **Connecting only configures.** A card's `on_connect` commands are checked never to touch a drive.

A lab can mix real and simulated instruments, and override a card's connection settings per instrument:

```yaml
lab: Cryostat 2
instruments:
  smu:    { card: keithley/2400, address: "GPIB0::24::INSTR", limits: { V: [-5, 5] }, rates: { V: 0.2 },
            guards: { V: [{ read: I, max_abs: 1.0e-9 }] } }
  lockin: { card: srs/sr830, address: "serial://COM3", connection: { read_termination: "\r" } }
  tc:     { card: lakeshore/336, address: "ASRL5::INSTR" }
  laser:  { card: coherent/obis, address: "serial://COM7", auto: never }
```

## Try the core without an agent

Requires Python ≥ 3.9 and PyYAML. No hardware is needed.

```bash
python examples/demo.py
```

```bash
python -m pytest
```

Run `python examples/demo.py --ask` to answer the approval requests yourself. If a pytest plugin installed elsewhere breaks collection, set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

## The rules

Every command passes through the `SafetyGate`, both direct commands (`get`, `set`, `raw`) and the steps of a plan.

| Command | What happens |
|---|---|
| Measure / configure | Runs immediately and is logged |
| Drive toward safe | Runs, ramped at the lab's rate |
| Drive away from safe | Limits and interlocks are checked, then a human is asked (unless the project envelope, an approved plan or auto mode covers it). The change is ramped while guard readings are watched, and it goes back to safe if one trips |
| Beyond limits, or a blocked command | Refused |

- Raw strings are matched against the card. A raw string that sets a drive is treated as a drive. One that touches a drive's command family but can't be parsed is sent to a human. Blocked and reset families come from the card.
- A plan is dry-run on twins first, approved once as a range, and then run.
- Stopping a plan (the Stop button, or the agent's own abort) runs its `on_abort` steps. By default, every drive the plan touched goes back to safe, or holds where the lab config's `safe_actions` says to hold. A guard trip sends that drive back to safe.
- Approvals come from an `Approver`, a human-facing object the agent never sees.

### Auto mode

A person can switch approvals from **Ask before drive changes** to **Auto for low-risk runs**, in the portal (next to the model picker) or on the approval page. No agent tool can switch it. Kelvin goes back to asking every time it restarts.

In auto mode, a drive change or a run goes ahead without asking when all of these hold:
- Every drive stays within `auto_fraction` (default 25%) of the way from its safe value to its lab limit. Kelvin judges the target's distance from safe, not the step size, so creeping up in small steps doesn't get around the rule.
- Switches (outputs, ranges) move at most one step from safe.
- For a run, the dry run finishes cleanly.
- The lab config doesn't exclude the instrument.

Anything else asks as usual, and the request says why auto mode didn't cover it. Each auto-approval shows in the conversation with its reasons and is written to the event log. Limits, interlocks, guard readings and Stop work the same in both modes.

The lab owner sets the rule in the lab config:

```yaml
auto_fraction: 0.25                 # share of the way from safe to each lab limit
instruments:
  smu:
    auto: { V: [-5, 5] }            # this parameter's own auto range
  laser:
    auto: never                     # a person approves every change on this instrument
```

## Layout

The Python package is still called `labharness` (`import labharness`); the product, commands and repo are Kelvin.

| Path | What it is |
|---|---|
| `src/labharness/cards.py` | Instrument card schema: parameters tagged measure/drive, safe values, limits, ramps, health checks |
| `src/labharness/scpi.py` | SCPI command templates (short/long forms, optional nodes, compound commands) |
| `src/labharness/labconfig.py` | Lab config (lab limits, rates, guards, interlocks, trust) and project envelopes |
| `src/labharness/gate.py` | The safety gate |
| `src/labharness/plans.py`, `engine.py` | Plan format; validate → preflight → dry run → approve → run → log |
| `src/labharness/eventlog.py` | Append-only event log; one folder per run (plan, snapshots, data, meta) |
| `src/labharness/sim/` | Twins that answer the card's own commands, plus a toy sample |
| `src/labharness/agent/` | MCP server (`server.py`), plain-text tool answers (`runtime.py`), approval page (`approval.py`, `ui.html`) |
| `src/labharness/web/` | The portal: web API (`app.py`), chat loop (`agent.py`), one module per model provider (`providers/`), chats and settings, browser UI and the logo (`static/`) |
| `src/labharness/data/` | Cards for the virtual lab, its lab config and a demo envelope |
| `src/labharness/desktop.py` | The desktop app: the portal in a native window, one copy at a time, `--self-test` |
| `packaging/` | PyInstaller recipe (`kelvin.spec`), Windows installer (`kelvin.iss`), app icons (`make_icons.py`, `icons/`) |

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
- [VISION.md](VISION.md) is the spec. [ARCHITECTURE.md](ARCHITECTURE.md) records the design decisions and the build order; update it when a decision changes.
- Never commit API keys, lab data, or a machine-specific `.mcp.json`. `.gitignore` already excludes them; keys live in `~/Kelvin/settings.json`.
- Changes to the safety gate (`gate.py`), the card format (`cards.py`) or the approval path need a test that shows the rule holds.

## Brand

| | |
|---|---|
| Name | Kelvin, "the coolest cat in the lab" |
| Mark | a sleeping cat beside a snowflake: [`kelvin.svg`](src/labharness/web/static/kelvin.svg) |
| Colors | navy `#1b3a5c`, ice `#2f6fb0`, frost `#e8f2fc`, snowflake `#8cc4f2` |
| Charts | keep the validated data palette in `app.css` (`--series-1`, `--s1`…`--s8`); brand colors are for chrome, not data |

## Writing a card

```yaml
card: 1
id: acme/sm-2000
model: ACME SM-2000 Source-Meter
kind: source-meter
status: draft                 # draft cards keep their drives simulation-only on real hardware
identify: { idn: "^ACME,SM-2000" }
error_query: "SYSTem:ERRor?"
parameters:
  V:
    label: Voltage            # what the lab panel shows
    kind: drive
    unit: V
    set: "SOURce:VOLTage[:LEVel] {value}"
    get: "SOURce:VOLTage[:LEVel]?"
    limits: [-200, 200]       # vendor limits; the lab sets tighter ones
    safe: 0
    ramp: gate
    max_rate: 10              # V/s
    applies_when: output      # only reaches the sample while the output is on ("armed" otherwise)
  output:
    label: Output
    kind: drive
    type: bool
    set: "OUTPut {value}"
    get: "OUTPut?"
    safe: off
    enables: [V]
  I:
    label: Current
    kind: measure
    unit: A
    get: "MEASure:CURRent?"
display: { headline: I }      # the reading shown large in the lab panel
raw:
  blocked: ["CALibration", "SYSTem:FIRMware"]
  reset: ["*RST"]
```
