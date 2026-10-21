# Plans, data and reports

How measurements are written, run and analysed, where the results are kept, and how they become reports.

## Plans

A plan is a short YAML file. The agent writes it and runs it. Kelvin then checks the instruments it uses, dry-runs it on the twins if it drives the sample, asks once for its drive ranges, runs it and records every point.

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

## The queue

Line up scans to run one after another, overnight or while you are away, as in Special Measure's queue. Open **Queue** in the Runs pane, or ask Kelvin ("queue a gate sweep at 2, 5 and 10 K"). An item is a plan, or a step between scans:

```yaml
step: warm to 10 K
set: { tc.T_set: 10 }
stabilize: { read: tc.T, within: 0.02, for: 120, timeout: 7200 }   # wait until it settles
wait: 300                                                          # then wait 5 minutes more
```

- **Building it.** Add an item at the end or at a position, with a note that is kept with its run. Reorder with ↑ ↓ (or select an item and press `u` / `d`), edit, or remove it (Delete). **Add to the queue** under a finished run's plan queues that scan again. You can change the queue while it runs; only the running item is fixed.
- **One approval.** **Start** checks every waiting item and dry-runs them all in order on the simulated lab, so a step that cools the sample counts for the scans after it. You then approve once, for every drive range the queue uses. Each scan is still dry-run just before it runs, and anything outside what you approved asks again.
- **Running.** Items run in the background, one at a time, each scan in its own run folder that records its place in the queue. Chat keeps working; a tool that needs the instruments says the queue is busy instead of waiting.
- **Stopping.** **Pause** stops after the item that is running. **Stop** ends that item as well. A failed, declined or stopped item pauses the queue, unless you choose to go on to the next item. The queue never starts by itself, even after Kelvin restarts.
- **Keeping queues.** **Save as…** keeps the waiting items under a name (`queues/<name>.yaml` in the data folder), and **Load** adds them again. The queue itself is kept in `queue.json`.

## Your data

Every run is saved as it is measured, with nothing to click. It gets its own folder, named by date, time and plan, such as `runs/20261018-015328-gate_sweep/`. Each folder holds:
- `plan.yaml`: what was asked for.
- `snapshot_start.json` and `snapshot_end.json`: every instrument setting at the start and at the end.
- `data.jsonl`: every point, traces included, written point by point.
- `data.csv`: the same points as a table (every axis and scalar reading), for Origin, MATLAB, Python or Excel. Each trace reading gets its own `<reading>.csv` with `i, x, value`, where `i` is the point's row in `data.csv`.
- `meta.json`: status, points, duration and warnings.
- `analysis.json`: the automatic fit and assessment.
- `report.pdf` and `report.pptx`: the scan report, written as soon as the run ends (see [Reports](#reports)).
- In `meta.json`, the run's **project** and **device** (the sample), which the device and master reports use.

Next to the run folders sit `events.jsonl`, your record of every command, reading, approval, refusal and alarm with who did it, and `reports/`. Each person has their own (see [Signing in](portal.md#signing-in)); the lab's instrument log, `~/Kelvin/instrument-log.jsonl`, has everyone's, each with their name.

**When you uninstall Kelvin:** the Windows uninstaller deletes Kelvin's own data and keeps your measurements.
- **Deleted:** settings and API keys, the ChatGPT sign-ins (revoked with OpenAI first), the accounts, chats, projects and memory, the lab config, your instrument cards, the instrument folders with their manuals, the log and the window's cache. Everyone's.
- **Kept:** every run folder, `events.jsonl` and `reports/`, in `~/Kelvin/virtual-lab`, in each person's `~/Kelvin/users/<name>/data` and in any data folder someone chose; and the lab's instrument log. A note in `~/Kelvin`, "About this folder.txt", says what is there. Installing Kelvin again shows those runs again.
- **Only Kelvin's own items are deleted, by name.** Any other file you put in `~/Kelvin` stays.
- **Kelvin must be closed:** the uninstaller waits until it is.
- **On a Mac or from source:** run `/Applications/Kelvin.app/Contents/MacOS/Kelvin --remove-data` (from source: `python -m labharness.desktop --remove-data`) for the same clean-up, then delete the app.

**Choosing the data folder:** these live in your own folder, `~/Kelvin/users/<you>/data` (for the first account, `~/Kelvin/virtual-lab`, where they were before accounts), until you choose another folder under **Settings → Data folder**. A lab server or a synced drive works:
- **Use this folder** switches at once. Kelvin checks first that it can write there.
- **Move what is saved now** takes the existing runs, reports and record along. Nothing there is overwritten: a run whose name is taken stays where it was, and Kelvin says so.
- **If the folder can't be written to when you sign in** (a network drive that is offline, say), Kelvin saves to your own folder meanwhile and tells you in Settings.
- **What stays in `~/Kelvin`:** chats, settings, API keys and instrument folders.
- **From a terminal:** `kelvin --data-dir <folder>` overrides the setting, for one person without sign-in.

## Reports

Kelvin keeps three kinds of report up to date by itself, following **project → device (sample) → scan**. Each is a PDF and a PowerPoint deck with the same numbered sections every time, so you always find things in the same place. They are built from what Kelvin already keeps (run folders, the lab record, the project's notes and reading list), with no model call, so they never touch an instrument and can be written while a plan runs.

| Report | Covers | Sections |
|---|---|---|
| **Scan report** | one run | 1 Result (assessment, fitted values with uncertainties, next step) · 2 Plots with fits · 3 Method · 4 Conditions at the start · 5 Approvals and alarms · 6 Appendix (plan and files) |
| **Device report** | one sample, all its scans | 1 The device (material, geometry, contacts) · 2 Mount history · 3 Results by measurement · 4 Trends across repeated scans · 5 Ranges explored · 6 Problems · 7 Scan index |
| **Master report** | the whole project | 1 Summary · 2 Goals and decisions (project notes) · 3 Devices compared · 4 Main results and figures · 5 Timeline · 6 Open questions and issues · 7 References · 8 Index |

A section with nothing to show yet still appears, with a line saying so.

**Where they go:**

```
<data folder>/
  runs/<run_id>/report.pdf, report.pptx    each scan's report, next to its data (data.csv, ...)
  reports/
    <project>/
      <project>_master.pdf / .pptx          the master report, always the latest
      devices/<device>.pdf / .pptx          one per sample, always the latest
      history/2026-10-19_master.pdf         a dated copy for each day it changed
      index.csv                             every scan: date, device, measurement, status, key result, folder
    queues/                                 a report of each queue's runs
    custom/                                 reports you ask for
```

Work outside every project goes to a **General** project.

**When they are written:**
- **Scan report:** as soon as the run finishes or is stopped.
- **Device and master reports:** a little after each scan of that device or project (about 20 s, so a queue of quick scans rewrites them once). **Update now** in **Reports** or in **Project settings** rewrites them at once.

**Devices.** A device is a sample you measure, such as Hall bar D3 or an NV diamond.
- **The list:** each project has one, in **Project settings → Devices** (name, material, description). Retire a device you no longer measure.
- **Mounting:** in the Lab panel, **Sample** shows which device is in the setup now. Choose another when you change samples (the change goes into the lab record), or use **New device…**. Kelvin can do the same when you tell it in a chat ("I just mounted D3").
- **Which device a scan belongs to:** every scan records its device and project. With nothing mounted, a scan belongs to the project of the chat that started it. A plan can name another device on the same chip with `device: D4`.

**The master report's opening.** By default it is a summary of the data: how many scans, on which devices, and the latest result. Write your own in **Project settings → Master report**, or press **Write the opening with Kelvin**. Kelvin writes it from the project's scans and notes, if the project's rules allow your model. Kelvin uses that opening until you change it, then the summary of the data follows.

**Reports of runs you choose.** For a different selection or your own words:
- **In a chat:** "make a report of today's runs for my advisor". Kelvin's `make_report` covers the runs started in this conversation, or in all of the project's conversations, unless you name runs, a day, or the last N. It also rewrites a master or device report on request (`kind` project or device).
- **In the Runs pane:** **Report…** opens the library of every report above, and below it makes a report of this run, the project's or chat's runs, today's runs, or the last five. These go to `reports/custom/`.

**Settings → Reports** chooses PDF, PowerPoint or both (the default), or **Only when I ask**, and which of the three kinds Kelvin keeps up to date. The CSV data is always written. A report that can't be written never fails the run; the lab record notes it.

Slides are 16:9; the plots are native shapes and the results native tables, so you can edit them in PowerPoint, Keynote or Google Slides. Neither format needs extra software to write. Characters that the standard PDF fonts can't show are spelled out (Ω becomes "Ohm").
