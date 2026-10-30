<p align="center">
  <img src="src/labharness/web/static/kelvin.svg" width="112" alt="Kelvin: a sleeping cat beside a snowflake">
</p>

<h1 align="center">Kelvin</h1>
<p align="center"><b>The coolest cat in the lab.</b><br>An AI lab assistant for experimental physicists that plans, measures and logs, and asks before it drives your sample harder.</p>

<p align="center"><a href="https://github.com/Houchen181/kelvin-dev/releases/download/private-latest/Kelvin-Setup-private.exe"><b>⬇ Download Kelvin 0.3.32 for Windows</b></a> (the newest private build, Windows 10/11)<br><a href="https://github.com/Houchen181/Kelvin/releases/latest">macOS</a> (the latest public release) · <a href="https://github.com/Houchen181/kelvin-dev/releases">what's new</a></p>

---

Ask in plain words. Kelvin turns the request into a measurement plan for your instruments, asks you before anything drives the sample harder, runs it, and fits the result. It works like an experimentalist: no test runs or code unless you ask. Every command and approval is logged.

```text
You     Sweep the gate from 0 to 5 V and record the lock-in signal.
Kelvin  Plan gate_sweep: smu.V from 0 to 5 V in 0.1 V steps; read lockin.X and smu.I; back to 0 V after.
        Approve?  51 points, ~1 min.  smu.output off → on,  smu.V 0 → 5 V at 0.5 V/s (your limit ±10 V).
You     Approve
Kelvin  Done. lockin.X has a peak at 2.41 ± 0.02 V, FWHM 0.38 ± 0.05 V.
```

## What it does

- **Instruments:** finds what is connected (GPIB, USB, LAN, RS-232, makers' libraries) and sets it up; for one it doesn't know, writes a card from the manual. [Supported instruments](docs/instruments.md).
- **Measurements:** plans, an overnight queue, your lab's own scripts, pulse sequences; each run's data saved as it is measured.
- **Reports:** PDF and PowerPoint for every scan, sample and project, kept up to date.
- **Models:** Claude, GPT, Gemini, your ChatGPT plan or a local model, side by side if you like.
- **Memory:** yours, each project's, and the lab's, shared by every chat.
- **Away from the lab:** alarms and approvals on your phone; other AI agents over MCP.

## Safety

Every command goes through one gate, from Kelvin, another AI agent or you.

| Command | What happens |
|---|---|
| Measure or configure | Runs, and is logged |
| Drive toward safe | Runs, ramped |
| Drive away from safe | Checked against your limits, approved by a person, ramped and watched |
| Beyond the limits, or blocked | Refused |

Only people change the limits, the lab config and the approval mode. **Hands on the sample** holds everything at safe while you rewire.

## Get started

- **Windows 10/11:** run [`Kelvin-Setup-private.exe`](https://github.com/Houchen181/kelvin-dev/releases/download/private-latest/Kelvin-Setup-private.exe); no admin rights needed. If Windows warns you, choose **More info → Run anyway**.
- **macOS (Apple silicon):** move `Kelvin.app` from the [latest public release](https://github.com/Houchen181/Kelvin/releases/latest) to Applications; the first time, right-click it and choose **Open**.

Then **Lab → Set up…** finds your instruments, and your Kelvin account gives Kelvin its models through the Kelvin hub (or use your ChatGPT plan, or a model server on your own computer). Kelvin keeps itself up to date.

## Documentation

[Using Kelvin](docs/portal.md) · [Instruments](docs/instruments.md) · [Plans, data and reports](docs/data.md) · [Safety](docs/safety.md) · [Other AI agents](docs/agents.md) · [Development](docs/development.md) · [iPhone](docs/ios.md) · [Vision](VISION.md)

From source (Python 3.10 or later):

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[web,dev]" && .venv/Scripts/kelvin
```
