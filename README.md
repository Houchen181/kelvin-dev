<p align="center">
  <img src="src/labharness/web/static/kelvin.svg" width="112" alt="Kelvin: a sleeping cat beside a snowflake">
</p>

<h1 align="center">Kelvin</h1>
<p align="center"><b>The coolest cat in the lab.</b><br>An AI lab assistant for experimental physicists that plans, measures and logs, and asks before it drives your sample harder.</p>

<p align="center"><a href="https://github.com/Houchen181/kelvin/releases/download/private-latest/Kelvin-Setup-private.exe"><b>⬇ Download Kelvin 0.3.23 for Windows</b></a> (the newest private build, Windows 10/11)<br><a href="https://github.com/Houchen181/kelvin/releases/latest">macOS</a> (the latest public release) · <a href="https://github.com/Houchen181/kelvin/releases">what's new</a></p>

---

You ask in plain words. Kelvin turns the request into a measurement plan for your instruments, asks you before anything drives the sample harder, runs it, and fits the result. It works like an experimentalist, not a programmer: no staged checks, test runs or code unless you ask (it can still rehearse a plan on simulated copies of the instruments when you want). Every command and approval is logged.

```text
You     Sweep the gate from 0 to 5 V and record the lock-in signal.
Kelvin  Plan gate_sweep: smu.V from 0 to 5 V in 0.1 V steps; read lockin.X and smu.I; back to 0 V after.
        Approve?  51 points.  smu.output off → on,  smu.V 0 → 5 V (your limit ±10 V), ramped.
You     Approve
Kelvin  Done. lockin.X has a peak at 2.41 ± 0.02 V, FWHM 0.38 ± 0.05 V.
        Plot, data and fit in runs/20261018-153012-gate_sweep/
```

It works with Claude, GPT, Gemini, your ChatGPT plan or a local model, and keeps everything on your computer.

## Instruments

Kelvin talks to instruments over GPIB, USB, LAN, RS-232, web APIs and the makers' own libraries. On start it finds what is connected and sets it up, outputs locked until you review the limits.

| Kind | Included |
|---|---|
| Sources and meters | Keithley 2400 series, 2450–2470, 2182A; Keysight B2900, 344xxA; Yokogawa GS200 |
| Lock-ins and preamps | SRS SR830, SR860, SR560, SR570; Zurich Instruments MFLI |
| Cryostats and magnets | Quantum Design PPMS, DynaCool, MPMS3; attocube attoDRY; Lake Shore 335, 336, 350, 625; Oxford Mercury iTC; AMI 430 |
| Lasers and optics | Coherent OBIS; M Squared SolsTiS; Light Conversion PHAROS; TOPTICA DLC pro; Thorlabs ITC4000, PM100 |
| Microwave, RF and pulses | SRS SG384, DG645; R&S SMB100A; Keysight N5183B, 33500B; Rigol DG1000Z; Windfreak SynthHD; MOGLabs XRF; Novatech 409B; Swabian Pulse Streamer and Time Tagger; SpinCore PulseBlaster; RFSoC boards with QICK |

Cards follow the makers' manuals and most are not yet checked on hardware. The attoDRY, DLC pro and SynthHD cards are drafts: their outputs stay simulated until someone checks them.

**Anything else:** Kelvin writes the instrument's card from its manual or from the maker's example scripts, then tries it on the instrument with you, reading only, until the readings match the front panel.

For spin experiments it builds and checks the pulse sequences itself: CW and pulsed ODMR, Rabi, Ramsey, Hahn echo and T1.

## Safety

Every command goes through one gate, whether it comes from Kelvin, another AI agent, or you in the terminal.

| Command | What happens |
|---|---|
| Measure or configure | Runs, and is logged |
| Drive toward safe | Runs, ramped |
| Drive away from safe | Checked against your limits, approved by a person, ramped and watched |
| Beyond the limits, or blocked | Refused |

Only people can change the lab config, the approval mode or which lab Kelvin controls.

## Get started

- **Windows 10/11:** run [`Kelvin-Setup-private.exe`](https://github.com/Houchen181/kelvin/releases/download/private-latest/Kelvin-Setup-private.exe), the newest private build; no admin rights needed. It isn't code-signed yet: if Windows warns you, choose **More info → Run anyway**.
- **macOS (Apple silicon):** move `Kelvin.app` from the [latest public release](https://github.com/Houchen181/kelvin/releases/latest)'s zip to Applications (there is no private macOS build). The first time, right-click it and choose **Open**.

Kelvin opens on a simulated lab with a demo model, so you can try it right away. Add an API key in Settings, or sign in with ChatGPT, to use a real model. It keeps itself up to date, replacing the installed copy, so there is only ever one Kelvin on a computer.

Also included: sign-in for everyone in the lab, each with their own chats, keys, data and record, and a shared instrument log; a queue that runs scans overnight after one approval; reports that keep themselves up to date for every scan, every sample and every project (PDF and PowerPoint), with CSV data; project memory and incognito chats, literature search with citations, a terminal, alarms and approvals from your phone, and MCP access for Claude Code, Codex or Gemini CLI.

## Documentation

- [Using Kelvin](docs/portal.md): models, projects, memory, the terminal, phone access
- [Instruments](docs/instruments.md): setup, supported instruments, writing and trying cards
- [Plans, data and reports](docs/data.md): plans, the queue, analysis, the data folder, reports, uninstalling
- [Safety](docs/safety.md): the rules in full, auto mode, watches
- [Other AI agents](docs/agents.md): Claude Code, Codex or Gemini CLI over MCP
- [Development](docs/development.md): running from source, tests, releases
- [Vision](VISION.md) and [architecture](ARCHITECTURE.md): the goals and the design

To run from source (Python 3.10 or later):

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[web,dev]" && .venv/Scripts/kelvin --demo
```
