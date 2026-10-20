# Kelvin — Architecture v0.4

Draft 2026-10-17. Spec: [VISION.md](VISION.md).
**Audience: every experimental physicist, in any lab, with any instruments and any control software.** This is not a tool for one lab.

## What "serve all experimental physicists" forces

| Reality | Design answer |
|---|---|
| Every lab runs a different control stack (LabVIEW, MATLAB, Python, EPICS/Tango, vendor software) | Wrap existing code and drivers through adapters; never require a rewrite |
| Many fields: transport, optics/lasers, RF, cryogenics, vacuum, AMO, scanning probe, beamlines | A generic core plus **domain packs** (plan templates, safety rules, reviewer rubrics) |
| Instrument PCs are often old, shared, offline Windows machines | A small local **lab runtime** that works offline and exposes everything through one interface |
| Instruments are shared and expensive; labs have PIs, students and guests | Roles, instrument locks and reservations, a full audit trail |
| Data is unpublished and sometimes export-controlled | Local-first and self-hostable; local models; bring-your-own API keys |
| The people trusting it did not write it | Open-source safety layer and card format; simulation first; trust earned step by step |

## Core principles

1. **The model commands the instruments; a deterministic safety gate guards the dangerous moves.** The model can set, query and send raw commands directly, like a person at the front panel. Measurement and configuration run freely. Only *drive* changes away from safe (more power, higher gate voltage, more field or current…) are checked, ramped and confirmed. The gate is ordinary code, so the model cannot argue its way past it.
2. **The lab runtime does not depend on the chat or the cloud.** Safety limits, logging and monitoring keep running without them.
3. **Notebook ≠ memory.** The notebook records everything automatically (#7). Memory keeps a small, curated set of facts with a scope (#14).
4. **Model-agnostic.** Tools are exposed over MCP and a router picks the model, so Claude, GPT, Gemini or a local model can drive any lab.
5. **Concise by contract.** Every reply follows a fixed shape, with details collapsed (#9).
6. **Open core.** The lab runtime, card format and safety gate are open source, so a lab can inspect what runs on its instrument PC.

## Components

```
 CLIENTS        Desktop app · Web/mobile · any MCP client (Claude, ChatGPT, Gemini…)
                       │
 HARNESS CORE   Advisor (plan, literature) · Operator (run) · Reviewer (critique)
 (local or      Memory manager (global | project | conversation) · Report generator
 self-hosted)   Literature service · Model router · Notifications · Approval UI
                       │  MCP tools (every command passes the safety gate)
        ┌──────────────┼──────────────┐
 LAB RUNTIME    (one per setup / instrument PC, works offline)
   Safety gate → Run engine → Instrument server → Adapters → hardware
   Simulator (twins) · Data store + event log · Monitor daemon
                       │
 SHARED REGISTRY  Instrument cards + twins + domain packs (community + lab-private)
```

One harness core can connect to many lab runtimes (several setups or rooms). Each runtime is its own trust boundary.

| Component | Job |
|---|---|
| **Adapters** | Talk to anything: VISA/SCPI, serial, TCP, vendor DLLs/SDKs, Python frameworks (QCoDeS, PyMeasure, Bluesky), EPICS/Tango, LabVIEW and MATLAB bridges |
| **Instrument card** | One universal file per instrument model: how to identify it (`*IDN?` pattern, USB ID), parameters with units, each tagged *measure* or *drive* (with safe direction and guard readings), safe state, non-invasive health checks, interlock dependencies, quirks, manual references, twin. Status: community / lab-verified / draft |
| **Limits, three levels** | Vendor hard limits (in the card) ⊇ lab limits (set by the instrument owner) ⊇ project envelope (set per experiment). The agent never sets limits |
| **Procedures** | A lab's existing scripts (VIs, .m files, Python) registered with declared inputs and limits, so the agent can call them as safe tools. This is the main adoption path |
| **Simulator** | Each card ships a twin; plans dry-run there first and return the predicted trajectory, duration and data shape. A fully simulated **virtual lab** lets anyone try the system with no hardware |
| **Safety gate** | Sits in front of every command, direct or planned. Lets measurement through; for drive changes, enforces limits and ramp rates, checks interlocks, roles and locks, watches guard readings, and asks a human when needed. Confirmations come only from a human action in the approval UI |
| **Run engine** | Runs approved plans; preflight health checks; abort sends the drives the plan touched back to safe (or holds them where the lab says so) |
| **Data store** | Append-only event log. Each run stores a full instrument snapshot, the plan, the code version, the operator, and the sample/device/session IDs |
| **Monitor daemon** | Rule-based alarms (threshold, rate of change, stale data) independent of the LLM; the LLM adds a diagnosis *after* the alert is sent |
| **Domain packs** | Per field: plan templates (e.g. gate sweep, field sweep, pump–probe delay scan, frequency sweep), safety rules (laser interlocks, magnet quench, high voltage, vacuum), reviewer rubrics |

**Two ways to run an experiment**, both through the same gate:
- **Direct control:** the model sends single commands (`set`, `get`, raw strings) and reacts to each result. This is for setup, alignment, troubleshooting and exploration.
- **Plans:** the model writes a sequence (sweeps, overnight runs) that the runtime executes without an LLM turn per point. This is faster (#1) and keeps running if the connection drops.

**Caution only where it matters (#3).** The instrument card tags every parameter as *measure* or *drive*. A drive is anything that changes what reaches the sample or a sensitive part: power, gate or bias voltage, current, field, temperature, high voltage, output on/off. Each drive has a safe direction, usually toward zero or off.

| Command | Examples | What the gate does |
|---|---|---|
| Measure / configure | Reading values; time constant, sensitivity, averaging, IF bandwidth, span, points, exposure, triggers | Runs immediately and is logged |
| Drive toward safe | Lowering power, ramping gate voltage toward 0, output off | Runs, ramped at the safe rate |
| Drive away from safe | Raising RF or laser power; raising gate or bias voltage, current, field, temperature or HV; output on; a reset that would make a drive jump | Checks limits → one-line preview (current → target) → asks you unless a signed envelope covers it → ramps while watching guard readings (leakage, compliance, temperature) and stops if one trips |
| Beyond limits | Above lab or vendor limits; firmware or calibration changes | Refused; only the instrument owner can unlock |

Raw commands are matched by command header, so a raw string that hits a drive is treated as a drive. A plan's drives are approved once as a range (e.g. V_gate 0 → 10 V at 0.5 V/s), not point by point. The lab sets limits, safe directions and guard readings per instrument, and the model cannot change them.

**Plan format.** Plans use the same shape in every field:

```yaml
plan: gate_sweep
requires: [smu, lockin]
preconditions: { smu.leakage_A: "< 1e-9" }
setup:   { smu.compliance_A: 1e-8, lockin.tc_s: 0.3 }
sweep:   { smu.V: { from: 0, to: 10, step: 0.05, rate_V_per_s: 0.5 } }
measure: [lockin.X, lockin.Y, smu.I]
on_abort: { smu.V: { ramp_to: 0, rate_V_per_s: 1 } }
```

**MCP tools:** `instr.get`, `instr.set`, `instr.raw`, `instr.undo`, `lab.status`, `lab.describe`, `lab.health_check`, `plan.validate`, `plan.simulate`, `plan.submit` (waits for human approval), `run.status`, `run.abort`, `data.query`, `data.plot`, `notebook.append`, `procedure.call`.

**Onboarding a new lab (no code):** install the runtime → it scans VISA, serial and USB → matches instruments to cards in the registry → for an unmatched instrument, the agent drafts a card and adapter from its manual (status: draft; its drives are simulation-only until the owner reviews it) → the owner sets lab limits and safe states → health check → ready.

**Trust (#3)** applies only to drive changes away from safe, and is set per instrument: *simulation only* (new or draft cards) → *ask each time* (default) → *run inside an envelope* signed by the PI or instrument owner (e.g. |V_gate| ≤ 5 V, P_RF ≤ −10 dBm). Logs compare predicted and real behavior, so trust grows from a track record.

**Deployment modes (#2):** *Local* (lab PC + local model; nothing leaves) · *Self-hosted* (institution server + approved API with zero data retention) · *Hosted* (for non-sensitive work). Each project has a policy that decides which models see what, and everything sent out is logged.

**Limits of the design:** Software limits add to hardware interlocks; they never replace them. Simulation checks the procedure (limits, order, timing, data shape), not the sample's physics.

## Feature map

| # | Feature | Where it lives |
|---|---|---|
| 1 | Fast instrument access | Direct commands over persistent connections; long loops run as plans in the run engine (not as LLM turns); status read from the monitor cache |
| 2 | Confidential | Deployment modes, per-project model policy, raw data stays in the runtime, alias map for sample names, outbound log |
| 3 | Caution + trust | Measure/drive tags per parameter; measurement runs freely; drive changes away from safe are limit-checked, previewed, confirmed and ramped while guard readings are watched; per-instrument trust; a stop button; open-source gate |
| 4 | Custom instruments | Adapters + card format + procedures; the agent drafts cards from manuals; a draft's drives stay simulation-only until the owner reviews it |
| 5 | Literature-based plans | Advisor + literature service + domain-pack templates output a plan with rationale and expected signal |
| 6 | Know and test instruments | Cards (correct operating conditions); preflight health checks block runs on failure |
| 7 | Track everything | Event log + per-run snapshot + sample/device/session registry |
| 8 | PDF/PPT logs | Templates (daily log, run summary, advisor update, group-meeting deck); every figure cites a run ID |
| 9 | Concise answers | Reply = answer (≤ 3 lines) + status/next action + collapsed details; units and run IDs always |
| 10 | Mobile / remote | Web/mobile client over VPN or Tailscale; phone can view, approve and abort; never expose the runtime directly to the internet |
| 11 | Better literature review | arXiv / Semantic Scholar / OpenAlex + Zotero + full-text index; every citation checked against its DOI |
| 12 | Virtual reviewer | Separate agent on a different model; rubric from the domain pack (controls, systematics, reproducibility, alternative explanations, overclaiming); ranked issues |
| 13 | Monitor sensitive instruments | Monitor daemon with rules from cards and domain packs, independent of the LLM |
| 14 | Scoped memory | Isolation enforced at the storage level; typed writes (decision, procedure, quirk, result) confirmed by the user; Q&A never stored; confidential projects never promote to global |
| 15 | Notifications | ntfy / Slack / Teams / email; info / warn / critical; critical repeats until acknowledged |
| 16 | Multi-model | Model router: parallel ask, side-by-side comparison, cross-model review |

## Build order

| Phase | Build | Done when |
|---|---|---|
| 0 | Card and plan schemas, safety gate (measure/drive classes, ramping, guard readings), run engine, simulator, event log, plus a **virtual lab** of common instruments (source-meter, lock-in, temperature controller, magnet supply, VNA, laser + shutter, motor stage) | An agent runs the virtual lab by direct commands and by plans; measurement runs freely; drive increases are confirmed or refused; everything is logged. **Built 2026-10-17** (Python ≥ 3.9 + PyYAML; 41 tests); the agent-side MCP wrapper comes in Phase 2. **Experiment workflow added 2026-10-17:** nested, log, there-and-back, repeat and time loops with per-axis settle and stabilize; live progress events; automatic analysis (curve classification, fits with uncertainties, trends along outer loops, quality flags, a short assessment and next step) |
| 1 | Adapters (VISA/SCPI, serial, Python frameworks, LabVIEW, MATLAB), auto-discovery onboarding, procedures, cards for the ~20 most common instruments | A new lab gets from install to a health-checked setup without writing code |
| 2 | MCP server usable from any MCP client; approval UI; roles and locks; monitor + notifications; concise contract | 3–5 pilot labs in different fields run supervised experiments. **MCP server, approval page (with a stop button and browser notifications) and concise tool answers built 2026-10-17**; roles, locks and the monitor remain |
| 3 | Literature service, reviewer, report generator, scoped memory, multi-model router, first domain packs | Pilots use it end to end: plan → run → report → review. **Scoped memory started 2026-10-17** in the portal: global instructions; per-project instructions and typed notes (decision, procedure, quirk, result) that the agent saves and the user can see and delete; a chat sees only its own project's memory, and deleting a project deletes it |
| 4 | Own desktop/web/mobile app, self-hosted server, community registry, local-model path | Public release. **Early portal built 2026-10-17**: a chat web app (Claude/GPT/Gemini/local), inline approvals, run plots, lab panel; one lab per portal, local only. **Desktop app built 2026-10-17**: native window, Windows installer and macOS app, built and self-tested by GitHub Actions on each version tag; unsigned for now. A hosted website comes later, as remote/phone access to the desktop app, since instruments are always reached through a local install. **Kelvin hub started 2026-10-20** ([docs/hub.md](docs/hub.md)): online accounts across labs (about 5,000 people), with consent, labs without an owner, Kelvins connected through the browser, and their shared projects, chats and runs, on one SQLite file. Wired into the app the same day: Settings → Kelvin hub connects, and Kelvin shares in the background |

## Decisions needed

1. **License:** open-core (recommended, for trust on instrument PCs) vs. closed.
2. **Default deployment:** local-first runtime + self-hostable core (recommended) vs. cloud-first.
3. **First pilot fields:** breadth (3 fields with the most common instruments) vs. depth (one field done very well).
4. **Front end for pilots:** existing MCP clients + a small approval/monitor web UI (recommended, fastest) vs. own app from day one.
