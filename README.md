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

To publish a new version: bump `src/labharness/_version.py`, then push a tag such as `v0.3.0`. GitHub builds both apps, tests them, and publishes the release.

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
- **Confidential projects:** In a project's **Project settings → Who may see this project**, choose which kinds of model may read it (Claude, GPT, Gemini, local models), and whether literature search may be used. For confidential work, allow only local models and switch off literature search.
  - **Enforcement:** Kelvin refuses any other model for chats, comparisons and second opinions, and leaves out the plan reviewer if it isn't allowed. The composer shows **Confidential**.
  - **The rules stay with the chat:** a chat that ran under them keeps them, even if it is moved out of the project or the project is deleted, so its history never reaches a model you excluded.
- **Incognito:** For a question that has nothing to do with your work, press **Incognito** on a new chat. Kelvin then uses no memory and keeps none, and the chat is never saved or listed; it is gone when you leave it. Instrument commands and runs are still logged, for safety.
- **Literature:** Kelvin searches arXiv and journal papers (through [OpenAlex](https://openalex.org)) and reads them, so it can plan an experiment from what is known and check a result against published work. Try "what parameters did people use to drive magnons with SAWs in CrSBr? propose a plan".
  - **Search:** results come with authors, venue, citation counts and abstracts.
  - **Reading:** arXiv papers with an HTML version are read in full, section by section, with equations as LaTeX; other papers give their abstract.
  - **Citations:** Kelvin cites what it used as [Author Year](link) and never invents a reference.
  - **Reading list:** in a project, ask Kelvin to keep a paper. It goes on the project's reading list (Project settings), Kelvin sees that list in the project's conversations, and reports list it as references.
  - **Privacy:** only the search words go to arXiv and OpenAlex, never your data or chats. Switch literature search off in Settings for confidential work. A free OpenAlex API key (Settings) raises the daily search allowance.
- **Several models at once:** Press **Compare** next to the model picker and tick one or two more models. Each message then goes to all of them, and the answers stream side by side. While comparing, the models can read the lab (status, values, runs, dry runs) but not change it, so three models never drive the same instruments. Press **Continue with this answer** under the one you prefer; the conversation goes on from that answer and with that model.
- **Second opinions:** A different model checks Kelvin's work.
  - **Plans:** pick a reviewer in Settings → **Second opinion on plans**. It then reads every plan before it runs: the request, the lab's state, Kelvin's own checks, and the plan. It answers go, caution or stop with its reasons, shown under the plan's step. If it says stop, Kelvin asks you before running the plan.
  - **Answers:** press **Second opinion** under the latest answer and choose a model. It says whether it agrees, partly agrees or disagrees, and why, and Kelvin sees that opinion with your next message.
  - **Limits:** the reviewer never gets tools, so it can't touch the lab. Its advice doesn't replace the safety gate's limits or approvals. A model from another company gives a more independent view.
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

**Set up your instruments in the app:** open the Lab panel and press **Set up…**. In the setup:
1. **Scan** finds the connected instruments. You can also add one by address, such as `GPIB0::24::INSTR` or `serial://COM3`, and pick its card.
2. Review the drafted lab config, mainly the limits.
3. **Save and connect** saves it as `~/Kelvin/lab.yaml` and health-checks every instrument. It checks that each one is the model its card says, so a changed address can't send commands to the wrong instrument. It also reads every reading once and checks that drives sit inside your limits.

Kelvin opens on your lab from then on. You can switch back to the virtual lab at any time. If your lab can't be connected at start-up, Kelvin opens the virtual lab and says why. Kelvin only switches labs while it is idle, and approvals go back to asking.

From a terminal:

```bash
kelvin-scan --write lab.yaml
```

The scan only asks each instrument who it is (`*IDN?`, or the card's own identity query); it never sets anything. Serial ports are tried with each known card's framing; for example, the Lake Shore 336 needs 57600 baud, 7 data bits and odd parity. The command matches each instrument to a card and drafts a lab config for you to review. The agent can run the same scan (`scan_instruments`), but only a person adds an instrument to the lab.

**Cards included** (written from the manuals; marked `community` until a lab verifies them on hardware):

| Kind | Models |
|---|---|
| Source-meters and sources | Keithley 2400-series, Keithley 2450/2460/2461/2470, Keysight B2900, Yokogawa GS200/GS210 |
| Lock-ins | SRS SR830/SR810, SRS SR860/SR865, Zurich Instruments MFLI (through LabOne) |
| Temperature controllers (cryostats) | Lake Shore 335/336, Lake Shore 350, Oxford Instruments Mercury iTC |
| Cryostat systems | Quantum Design PPMS, DynaCool, VersaLab, MPMS3 (through MultiVu) |
| Magnet supplies | Lake Shore 625, AMI 430 |
| Lasers and optics | Coherent OBIS, Thorlabs ITC4000 laser/TEC controller, Thorlabs PM100/PM400 power meter |
| Amplifiers | SRS SR560 voltage preamp, SRS SR570 current preamp (listen-only: Kelvin shows what it last set) |
| Meters and generators | Keysight/Agilent 344xxA DMMs, Keithley 2182A nanovoltmeter, Keysight 33500B/33600A generators |

Real hardware gets the same gate as the virtual lab, and a few protections of its own:
- **Measuring never powers anything up.** On some source-meters a reading command switches the output on, so those readings are only taken while the output is already on (`read_when`).
- **No speed-up on real time.** A lab with any real instrument runs on real time, so ramps keep their rates.
- **Risky commands stay locked.** Commands that change what readings mean, or that bypass the instrument's own protection, are blocked in the card: sensor curves, coil constants, persistent-switch heaters, current limits, and DMM resistance modes that push current into a device.
- **Connecting only configures.** A card's `on_connect` commands are checked never to touch a drive.

**Instruments behind vendor software.** Some instruments are not driven with text commands but through their maker's library. Kelvin has drivers for two of them; the address says which:
- `zhinst://dev1234` for a Zurich Instruments lock-in, through LabOne's Python API (`pip install zhinst-core`). Add `?host=...&port=8004&interface=1GbE` if the data server runs elsewhere. A scan finds these devices through LabOne.
- `qd://<computer>:5000` for a Quantum Design system, through MultiPyVu (`pip install MultiPyVu`, with its server running next to MultiVu). MultiVu ramps temperature and field itself; Kelvin sets the rate and setpoint, then waits for the reading.

Their cards use the same command templates (`SET <path> {value}`, `GET <path>`), so limits, ramps, approvals and the simulated twins work as for any instrument. The vendor libraries are not part of the desktop app; install Kelvin from source to use them (`pip install "kelvin-lab[desktop,zhinst]"` or `[desktop,qd]`). These cards and drivers follow the vendors' documentation and haven't been verified on hardware yet, so tighten the limits and watch the first runs.

**Names that differ between installations.** A card can declare variables, such as a Mercury iTC's sensor UID or a lock-in's demodulator. Set them per instrument with `vars`. The draft lab config lists them with their defaults so you can check them, and values may only contain letters, digits and `. _ + -`, so nothing can be slipped into a command.

A lab can mix real and simulated instruments, and override a card's connection settings per instrument:

```yaml
lab: Cryostat 2
instruments:
  smu:    { card: keithley/2400, address: "GPIB0::24::INSTR", limits: { V: [-5, 5] }, rates: { V: 0.2 },
            guards: { V: [{ read: I, max_abs: 1.0e-9 }] } }
  lockin: { card: srs/sr830, address: "serial://COM3", connection: { read_termination: "\r" } }
  tc:     { card: lakeshore/336, address: "ASRL5::INSTR" }
  laser:  { card: coherent/obis, address: "serial://COM7", auto: never }
  itc:    { card: oxford/mercury-itc, address: "TCPIP0::10.0.0.9::7020::SOCKET", vars: { sensor: DB6.T1 } }
  li:     { card: zhinst/mfli, address: "zhinst://dev5678", limits: { amplitude: [0, 0.5] } }
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

## Watches and notifications

Kelvin keeps an eye on the lab between runs and during them. A **watch** reads one value every few seconds and raises an alarm when the value leaves its range for longer than the hold time. Durations are in lab time, so the virtual lab at 10x behaves like the real one. The alarm clears itself when the value comes back. Kelvin also raises an alarm when an instrument stops answering (three failed reads in a row) and when a real instrument reports an error in its own error queue (checked once a minute).

Who can add a watch, and what it may do:

| Watch | Added by | On alarm |
|---|---|---|
| Lab watch | The lab owner, in the lab config | Tells people; can stop the running plan and send named drives to their **safe** values (the config refuses any other value) |
| Your watch | You, in the Lab panel (**Watches → Add…**) | Tells people |
| Kelvin's watch | The agent (`watch`, `watches`, `unwatch`), e.g. "tell me if the sample warms above 4 K while this runs" | Tells people |

Lab watches can't be removed from the portal or by the agent; they change only in the lab config.

```yaml
watch:
  - read: tc.T_sample
    above: 20
    for: 10                          # seconds it must stay above before the alarm
    then: { laser.emission: off }    # only ever toward safe; also stops the running plan
    note: The sample must stay cold; the laser is the usual culprit.
```

**How Kelvin tells you.** Alarms show as a banner above the chat until you press **Got it**, and the window title starts with (!). In Settings → Notifications:
- **Allow on this computer** turns on system notifications in your browser: alarms always, and approvals waiting and finished runs while Kelvin is in the background.
- **Also send to** takes a webhook: an [ntfy](https://ntfy.sh) topic (push to your phone with the ntfy app), or a Slack, Teams or Discord incoming webhook. Tick which notices go there (alarms and finished runs by default), then press **Send a test**. The address is kept like an API key and never shown again. Nothing leaves the computer unless you set one; when you do, the notices carry lab details, so use a private topic or channel.

## Phone and remote access

Check on a run from the coffee room, or answer an approval from home. Phone access is **off** until you switch it on in Settings → **Phone access**. When it's on, Kelvin also listens on the network (port 8771 by default). Your desktop's access key never works there; only paired phones do.

1. Switch on **Phone access**. Kelvin shows its addresses: on this network, and on Tailscale if you use it.
2. Name the phone, choose what it may do, and press **Pair a phone**.
   - **Watch:** see the lab, runs and alarms, stop a run, and decline requests.
   - **Control:** also chat with Kelvin and approve requests.
3. On the phone, scan the QR code, or open the address and type the 6-digit code. The code works once, for 10 minutes, and repeated wrong codes lock pairing for a while.

The phone then opens Kelvin's phone page: alarms with **Got it**, requests with **Approve** (control) or **Decline**, the run in progress with its progress and **Stop**, the instruments, and recent notices. A control phone can also open the full portal.

Settings, instruments, the lab config, the approval mode and pairing work only on the Kelvin computer. Remove a phone in Settings, or with **Forget this phone** on the phone itself.

**Away from the lab:** install [Tailscale](https://tailscale.com) (a free, encrypted VPN) on the lab computer and on the phone, and pair the phone using the Tailscale address. On a plain Wi-Fi network traffic isn't encrypted, so on a shared network prefer Tailscale. Don't forward Kelvin's port to the internet. For push notifications to the phone, add an ntfy topic under Notifications.

## Reports

Kelvin writes up runs as a **PDF** or as **PowerPoint slides**, to look back on, review with your advisor, or bring to group meeting. A report has:
- an opening summary in your words (or Kelvin's, when you ask it in a chat);
- each run's plots, with the fitted curves dashed;
- the results with uncertainties, and Kelvin's assessment and suggested next step;
- what was swept and set up, and every instrument's state at the start;
- drives the run left away from safe, the plan itself, and the approvals and alarms during the run;
- a lab log of the period;
- for a project, the project's notes (decisions, procedures, instrument quirks, results).

Kelvin builds the report from what it already keeps (run folders and the event log), so it never touches an instrument and can run while a plan runs.

- **In a chat:** "make a report of today's runs for my advisor". Kelvin's `make_report` tool covers the runs started in this conversation, or in all of the project's conversations, unless you name runs, a day, or the last N. The reply shows the files with **Open** and **Download**.
- **In the Runs pane:** **Report…** picks this run, the project's or chat's runs, today's runs, or the last five.

Reports are saved in the `reports` folder next to the run folders (`~/Kelvin/virtual-lab/reports/` in the desktop app). Slides are 16:9; the plots are native shapes and the results native tables, so you can edit them in PowerPoint, Keynote or Google Slides. Neither format needs extra software to write. Characters that the standard PDF fonts can't show are spelled out (Ω becomes "Ohm").

## Layout

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

## Cards for new instruments

For an instrument Kelvin doesn't know, open **Lab → Set up → Make a card for it from its manual…**.
- **Drafting:** paste the manual's remote-programming chapter (copy it from your PDF viewer, or load a text file). Pick a model and press **Draft the card**. The model writes the card from that text only, and Kelvin checks it against the card format, asking the model to fix any problems once.
- **Checking:** read the draft against the manual, edit it, press **Check**, then **Save card**. It is kept in `~/Kelvin/cards/` and appears in the card list for your lab config.
- **In a chat:** "here is the command reference of my magnet supply, make a card". Kelvin uses `card_guide`, `check_card` and `save_card`.
- **Drafts are safe to try:** a new card is a **draft**. On real hardware its readings work, so you can try it, but its outputs stay simulation-only.
- **Reviewing:** when you have checked the commands, limits and safe values, press **Mark reviewed**. Only a person can do that, and Kelvin never overwrites a card a person has reviewed.

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
