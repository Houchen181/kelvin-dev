<p align="center">
  <img src="src/labharness/web/static/kelvin.svg" width="112" alt="Kelvin: a sleeping cat beside a snowflake">
</p>

<h1 align="center">Kelvin</h1>
<p align="center"><b>The coolest cat in the lab.</b><br>An AI lab assistant for experimental physicists that plans, measures and logs, and asks before it drives your sample harder.</p>

<p align="center"><a href="https://github.com/Houchen181/kelvin/releases/latest/download/Kelvin-Setup.exe"><b>⬇ Download Kelvin for Windows</b></a> (latest version, Windows 10/11)<br><a href="https://github.com/Houchen181/kelvin/releases/latest">macOS, and what's new</a></p>

---

Kelvin connects Claude, GPT, Gemini or a local model to your instruments through a safety gate. Measuring runs freely. Anything that pushes more power, voltage, current, field or temperature into the sample is checked against your limits, approved by a person, then ramped and watched.

## Get started

No Python needed.

- **Windows 10/11:** [download `Kelvin-Setup.exe`](https://github.com/Houchen181/kelvin/releases/latest/download/Kelvin-Setup.exe) and run it. No admin rights are needed. The installer isn't code-signed yet, so if Windows says it protected your PC, choose **More info → Run anyway**.
- **macOS (Apple silicon):** from the [latest release](https://github.com/Houchen181/kelvin/releases/latest), unzip `Kelvin-<version>-macOS.zip` and move `Kelvin.app` to Applications. The first time, right-click it and choose **Open**.

Kelvin starts with a simulated lab and a scripted demo model. To talk to a real model, add an API key in Settings or sign in with ChatGPT. If instruments are connected, Kelvin finds them and connects them, with their outputs locked until you review the limits. Everything it keeps stays in `~/Kelvin` on your computer.

## What it does

- **Runs measurements from a chat.** Ask in plain words. Kelvin reads the instruments, writes a measurement plan, dry-runs it, runs it, and fits the data with uncertainties.
- **Works with your instruments.** It talks to instruments over VISA, serial, or the maker's software. Cards are included for common source-meters, lock-ins, cryostats, magnet supplies, lasers and amplifiers. Each instrument gets a folder with its settings and its maker's manual.
- **Uses the model you choose.** Claude, GPT, Gemini, your ChatGPT plan, OpenRouter and other services, or a local model for confidential work. You can compare models side by side or ask one for a second opinion.
- **Remembers without leaking.** It keeps notes for you and for each project, and they never mix. Incognito chats keep nothing.
- **Reads the literature.** It searches arXiv and journals, and cites what it used.
- **Keeps your data and a record.** One folder per run, plus a record of every command and approval, in a folder you choose. It writes PDF and PowerPoint reports.
- **Has a terminal.** Type to Kelvin, or send an instrument its own commands, through the same safety gate.
- **Watches the lab.** It raises alarms when a reading leaves its range. You can approve a request or stop a run from your phone.
- **Works with other agents.** Claude Code, Codex or Gemini CLI can drive the lab over MCP under the same rules.

## Safety

Every command goes through one gate, whoever sends it: Kelvin, another agent, or you in the terminal.

| Command | What happens |
|---|---|
| Measure or configure | Runs at once, and is logged |
| Drive toward safe | Runs, ramped |
| Drive away from safe | Limits and interlocks are checked, then a person approves. The change is ramped while guard readings are watched |
| Beyond the limits, or a blocked command | Refused |

Only people can change the lab config, the approval mode or which lab Kelvin controls.

## Documentation

| Page | Covers |
|---|---|
| [Using Kelvin](docs/portal.md) | Models and services, projects, memory, incognito, literature, comparing models, the Runs and Lab panels, the terminal, phone access |
| [Instruments](docs/instruments.md) | Automatic setup, instrument folders and manuals, supported instruments, lab configs, writing cards |
| [Plans, data and reports](docs/data.md) | Measurement plans and automatic analysis, the data folder, reports |
| [Safety](docs/safety.md) | The rules in full, auto mode, watches and notifications |
| [Other AI agents](docs/agents.md) | Connecting Claude Code, Codex or Gemini CLI over MCP |
| [Development](docs/development.md) | Running from source, tests, code layout, releases |

[VISION.md](VISION.md) sets out the goals and [ARCHITECTURE.md](ARCHITECTURE.md) the design.

## Development

Needs Python 3.10 or later (the core alone runs on 3.9).

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -e ".[web,dev]"
```

```bash
.venv/Scripts/python -m pytest
```

`.venv/Scripts/kelvin --demo` opens the portal on the virtual lab with a scripted model. Read [Development](docs/development.md) before you open a pull request.
