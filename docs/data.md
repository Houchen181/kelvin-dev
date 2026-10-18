# Plans, data and reports

How measurements are written, run and analysed, where the results are kept, and how they become reports.

## Plans

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

## Your data

Every run gets its own folder, named by date, time and plan, such as `runs/20261018-015328-gate_sweep/`. Each folder holds:
- `plan.yaml`: what was asked for.
- `snapshot_start.json` and `snapshot_end.json`: every instrument setting at the start and at the end.
- `data.jsonl`: every point, traces included.
- `meta.json`: status, points, duration and warnings.
- `analysis.json`: the automatic fit and assessment.

Next to the run folders sit `events.jsonl`, the lab record of every command, reading, approval, refusal and alarm with who did it, and `reports/`.

**When you uninstall Kelvin:** the Windows uninstaller deletes Kelvin's own data and keeps your measurements.
- **Deleted:** settings and API keys, the ChatGPT sign-in (revoked with OpenAI first), chats, projects and memory, the lab config, your instrument cards, the instrument folders with their manuals, the log and the window's cache.
- **Kept:** every run folder, `events.jsonl` and `reports/`, in `~/Kelvin/virtual-lab` and in any data folder you chose. A note in `~/Kelvin`, "About this folder.txt", says what is there. Installing Kelvin again shows those runs again.
- **Only Kelvin's own items are deleted, by name.** Any other file you put in `~/Kelvin` stays.
- **Kelvin must be closed:** the uninstaller waits until it is.
- **On a Mac or from source:** run `/Applications/Kelvin.app/Contents/MacOS/Kelvin --remove-data` (from source: `python -m labharness.desktop --remove-data`) for the same clean-up, then delete the app.

**Choosing the data folder:** these live in `~/Kelvin/virtual-lab` until you choose another folder under **Settings → Data folder**. A lab server or a synced drive works:
- **Use this folder** switches at once. Kelvin checks first that it can write there.
- **Move what is saved now** takes the existing runs, reports and record along. Nothing there is overwritten: a run whose name is taken stays where it was, and Kelvin says so.
- **If the folder can't be written to at start-up** (a network drive that is offline, say), Kelvin saves to `~/Kelvin/virtual-lab` meanwhile and tells you in Settings.
- **What stays in `~/Kelvin`:** chats, settings, API keys and instrument folders.
- **From a terminal:** `kelvin --data-dir <folder>` overrides the setting.

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

Reports are saved in the `reports` folder of your data folder, next to the run folders. Slides are 16:9; the plots are native shapes and the results native tables, so you can edit them in PowerPoint, Keynote or Google Slides. Neither format needs extra software to write. Characters that the standard PDF fonts can't show are spelled out (Ω becomes "Ohm").
