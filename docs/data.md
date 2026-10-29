# Plans, data and reports

How measurements are written, run and analysed, where the results are kept, and how they become reports.

## Plans

A plan is a short YAML file. The agent writes it and runs it. Kelvin then checks the instruments it uses, asks once for its drive ranges, runs it and records every point. A dry run on the twins comes only when asked for.

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

Sweep values can be `{from, to, step}`, `{from, to, points}`, `{from, to, points, log: true}` or `{values: [...]}`. A sweep axis can also set:
- its own `settle`: one number, one per value (`settle: [5, 5, 600]`, mirrored on the way back), or only for the steps that need longer (`settle: {50: 600}`), so a staircase's last step can hold longer than the others;
- a `stabilize` condition: wait until a reading changes by less than `within` over `for` seconds, up to `timeout` (then it measures anyway and says so). With `near: 0.05` the reading must also be that close to its set point (the one its instrument reads back, else the axis's value), so a steady reading short of it, or a dead sensor, doesn't pass. With `every: 10` a point is recorded every 10 s while it waits, marked `stabilizing: 1` (the settled point has 0; the automatic analysis takes only those). The run keeps when each one settled (`stabilized`);
- `until:` or `while:` (see [Ending early](#ending-early)).

After a run, Kelvin classifies each measured curve and fits it: a peak or dip (Lorentzian or Gaussian: center, FWHM, height, Q for frequency axes), a straight line (slope, and resistance for I–V), an exponential relaxation (τ), a steady value or no signal. Every number has a one-standard-error uncertainty. It also tracks how the fitted feature changes along an outer loop, and it flags clipping, under-resolved or cut-off features, hysteresis, unsettled readings and stopped runs. The result is saved as `analysis.json` in the run folder; `analyze_run` re-fits a run with a chosen model.

### Measuring while it ramps

A set point the instrument ramps to by itself (a cryostat's temperature, a magnet's field, a stage's position) waits until its reading gets there: in `setup:`, at each value of a stepped sweep, and in `set_value`. Nothing is measured on the way, so a cool-down from 300 K holds the run for half an hour or more. To measure on the way, sweep it with `ramp:`:

```yaml
plan: RT_cooldown
setup: { isrc.I: 1.0e-6, isrc.output: on }
sweep:
  ppms.T_set:
    ramp: [{to: 10, rate: 10}, {to: 2, rate: 1, hold: 600}]   # rates in K/min, the unit of the card's T_rate
    every: 10                                                  # a point every 10 s while it ramps
measure: [nvm.V]
after: { isrc.output: off }
```

- **Each leg** sets its rate (on the card's rate parameter, in its unit; `describe_instrument` shows it) and its target without waiting. It then measures every `every` seconds until the reading (the card's read-back, or `read:`) is within the card's tolerance (or `within:`), goes on for `hold:` seconds more, and moves to the next leg. A bare number is a leg at the rate the instrument has.
- **The ramp's column is the reading it follows,** so R is recorded and analysed against the temperature reached, not against the set point. The legs are kept with the run (`legs`: when each began, got there and ended).
- **It is the innermost loop.** Stepped sweeps can go outside it (an R(T) at several fields); `time:` can't go with it.
- **A ramp that stops getting closer** for `stall:` seconds (600 unless the plan says; `0` never) stops the run as Stop does: its outputs go to safe and its points are kept. The [set-point watchdog](safety.md#watches-and-notifications) also raises an alarm after 5 minutes.
- **The approval** covers the targets and, where the rate is itself a drive (a magnet supply's), the rates. A drive Kelvin ramps itself, a step at a time (a source's voltage), is swept with `{from, to, step}` instead.

### Ending early

`until:` ends a loop once its condition holds, and `while:` once it no longer does. Both are checked after each point, on that point's readings (anything else is read). A condition compares readings with numbers, also their size (`abs(...)`), joined by `and` or `or`: `"abs(smu.I) > 1e-6"`, `"nvm.V < 1e-6 or ppms.T < 1.9"`.
- **On a sweep axis,** its remaining values are skipped, and the loops outside it go on.
- **On a ramp,** the ramp ends. Its set point stays where it was sent; `after:` decides where it goes next.
- **On `time:`,** the series ends: `time: {every: 10, for: 7200, until: "tc.T_sample < 2.1"}`.
- **At the top of the plan,** the whole sweep ends, `after:` runs and the run is done, saying what ended it.

A condition whose reading isn't a number (NaN, nothing) ends its loop too, since nobody can tell whether it holds.

### A reading that fails for a moment

With `retry: {times: 3, wait: 10}` (or `retry: 3`), a point whose reading fails in a way that can pass (no answer in time, a garbled reply, a link that dropped) is taken again, `wait` seconds apart.
- **Before the point counts,** the setup's levels are read back. An instrument that restarted (its output back off after a power blip) stops the run rather than be measured dark.
- **The gap is marked:** every point has `gap_s`, the time lost to a fault just before it (0 for none), and the run lists the gaps (`gaps`) and says how much time was lost.
- **Never retried:** a sensor fault, a stop or an instrument switched off. Without `retry:`, a failed reading stops the run.


## Recipes

A recipe is a measurement in stages that checks its own results, kept so that it can be run again (`run_recipe`; in the portal by its name). Each stage is a plan, a check on its run's data, values kept for the later stages, and how often to try again with changed values:

```yaml
recipe: find-resonance
description: Coarse, then fine ODMR around the dip.
values: {span: 2e8}
stages:
  - name: coarse
    plan: |
      plan: odmr_coarse
      sweep: {mw.frequency: {from: {{2.87e9 - span / 2}}, to: {{2.87e9 + span / 2}}, points: 101}}
      measure: [counter.rate]
    pass: min(counter.rate) < 0.95 * median(counter.rate)     # a dip at least 5% deep
    keep: {f0: "at_min(counter.rate, mw.frequency)"}           # its frequency, for the next stage
    retries: 1
    retry: {span: span * 2}                                     # try a wider range once
  - name: fine
    plan: |
      plan: odmr_fine
      sweep: {mw.frequency: {from: {{f0 - 1e7}}, to: {{f0 + 1e7}}, points: 81}}
      measure: [counter.rate]
```

- **Numbers that change** are `{{...}}` in a stage's plan: a value, or arithmetic on values. `run_recipe` can give the values others (`values: {span: 4e8}`).
- **Checks and kept values** are expressions over the run's columns, as `compute` takes them; `at_min(y, x)` and `at_max(y, x)` give x where y is smallest or largest. Put an expression with a comma in quotes.
- **Each stage runs through the safety gate** like any plan. A stage whose check still fails after its retries ends the recipe, and so does a run that stops. The answer lists each stage's run and whether it passed, with the values kept.
- **Kept** in the portal per person or per project, shown in the chat when Kelvin keeps it (save_recipe; Settings → Memory and Project settings list them).

## The queue

Line up scans to run one after another, overnight or while you are away, as in Special Measure's queue. Open **Queue** in the Runs pane, or ask Kelvin ("queue a gate sweep at 2, 5 and 10 K"). An item is a plan, or a step between scans:

```yaml
step: warm to 10 K
set: { tc.T_set: 10 }
stabilize: { read: tc.T, within: 0.02, for: 120, timeout: 7200 }   # wait until it settles
wait: 300                                                          # then wait 5 minutes more
```

- **Building it.** Add an item at the end or at a position, with a note that is kept with its run. Reorder with ↑ ↓ (or select an item and press `u` / `d`), edit, or remove it (Delete). **Add to the queue** under a finished run's plan queues that scan again. You can change the queue while it runs; only the running item is fixed.
- **One approval.** **Start** checks every waiting item, then asks you once for every drive range the queue uses. Anything outside what you approved asks again. Asked to (`dry_run`), Kelvin only rehearses the whole queue in order on the simulated lab, so a step that cools the sample counts for the scans after it, and says how long it takes and what would fail; the queue doesn't start.
- **Running.** Items run in the background, one at a time, each scan in its own run folder that records its place in the queue. Chat keeps working; a tool that needs the instruments says the queue is busy instead of waiting.
- **Stopping.** **Pause** stops after the item that is running. **Stop** (any of them, also the runs pane's **Stop run**) ends that item as well, and the queue pauses there. A failed or declined item pauses the queue too, unless you choose to go on to the next item. The queue never starts by itself, even after Kelvin restarts.
- **Keeping queues.** **Save as…** keeps the waiting items under a name (`queues/<name>.yaml` in the data folder), and **Load** adds them again. The queue itself is kept in `queue.json`; beside it, `watches.json` keeps your watches and Kelvin's, and `alarms.json` the latest alarms, so both outlast a restart ([safety.md](safety.md#watches-and-notifications)).

## Your data

Every run is saved as it is measured, with nothing to click, in the folder of the chat that started it (see [Each chat's folder](#each-chats-folder)). It gets its own folder there, named by date, time and plan, such as `20261018-015328-gate_sweep/`, which holds:
- `run.h5`: the run's data, in one HDF5 file. It holds every point, trace and camera frame, every instrument setting at the start and at the end, the plan as it ran, the automatic fits, and a short card saying what the run is.
- `report.pdf` and `report.pptx`: the scan report, written as soon as the run ends (see [Reports](#reports)).

**Nothing measured is lost when a run is cut short.**
- **While a plan runs,** each point goes into the run's record, `record.bin`, the moment it is measured. It is written at once with a checksum, and forced to disk at least once a second.
- **When the run ends,** done or stopped, Kelvin writes `run.h5` from the record, checks it, and removes the record.
- **If Kelvin or the computer stops in the middle of a run** (a crash, a power cut), the record keeps every point up to then. At most the point being measured is lost, or after a power cut the last second or so. A point cut off halfway is recognised and left out, so the rest always reads.
- **The next time Kelvin starts,** it keeps such a run as `run.h5` with the status **interrupted** and the reason. A run that another Kelvin is still measuring is left alone.

**Opening `run.h5`.** Any HDF5 reader opens it:
- **Python:** `h5py.File("run.h5")["points/smu.V"][()]`
- **MATLAB:** `h5read("run.h5", "/points/smu.V")`
- **Others:** Igor Pro (HDF5 Browser), Origin (Import → HDF5), HDFView, Julia or R.

```
run.h5
  (attributes)    the run: run_id, plan, status, started, finished, points, project, device...; meta_json has all of it
  plan            the plan as it ran (YAML text)
  points/<key>    one column per scalar: i, t, each swept axis and each reading; attributes unit, role, quantity
  traces/<key>    one row per point (NaN-padded); <key>.x holds each point's x values, <key>.axis its start and stop
  frames/<key>    camera frames, points × rows × columns, compressed
  conditions/     start and end: every instrument setting (JSON text)
  analysis        the automatic fits and assessment (JSON text)
  card            the run in brief: what Kelvin's AI reads first (JSON text)
```

**Every run in one catalog.** `catalog.sqlite` in the data folder lists every run:
- what it swept and measured, with units, ranges and the physical quantity (voltage, temperature, magnetic field...);
- the conditions it was measured in;
- its fitted results.

Kelvin keeps it up to date by itself. It lets the AI answer questions across all your runs, such as "every field sweep on D3 below 2 K last month", without opening each one. Any SQLite tool opens it too. It is only an index: the runs stay the record, and if `catalog.sqlite` is deleted, Kelvin builds it again.

**Asking about your data in a chat.** Kelvin's AI finds runs (`find_runs`, or `query_runs` for anything its filters can't ask), reads a run's card (`describe_run`), and reads or computes numbers (`get_run_data`, `compute`):
- **Selecting points:** both reading tools take a condition, `where: smu.V > 1 and tc.T < 5`.
- **`compute` works without writing code:** `lockin.X / smu.I` (a resistance), `deriv(lockin.X, smu.V)`, `mean(smu.I)`. It only does arithmetic on the run's columns.

**Runs saved by Kelvin 0.3.19 or earlier** (`data.jsonl`, `meta.json`, `plan.yaml`, `data.csv`...). At its first start after the update, Kelvin writes each one as `run.h5` and moves its old files into `legacy/` in the run's folder. Nothing is deleted.

Next to the run folders sit `events.jsonl`, your record of every command, reading, approval, refusal and alarm with who did it (for an approval or a Stop: the person's account, where they answered and when), and `reports/`. Each person has their own (see [Signing in](portal.md#signing-in)); the lab's instrument log, `~/Kelvin/instrument-log.jsonl`, has everyone's, each with their name.

**When you uninstall Kelvin:** the Windows uninstaller deletes Kelvin's own data and keeps your measurements.
- **Deleted:** settings and API keys, the ChatGPT sign-ins (revoked with OpenAI first), the accounts, chats, projects and memory, the lab config, your instrument cards, the instrument folders with their manuals, the manuals Kelvin kept, what it installed for instruments (its environments in `~/Kelvin/env`, and uv and their Pythons in `~/Kelvin/tools`), the log and the window's cache. Everyone's.
- **Kept:** every run folder, `events.jsonl` and `reports/`, in `~/Kelvin/virtual-lab`, in each person's `~/Kelvin/users/<name>/data` and in any data folder someone chose; and the lab's instrument log. A note in `~/Kelvin`, "About this folder.txt", says what is there. Installing Kelvin again shows those runs again.
- **Only Kelvin's own items are deleted, by name.** Any other file you put in `~/Kelvin` stays.
- **Kelvin must be closed:** the uninstaller waits until it is.
- **On a Mac or from source:** run `/Applications/Kelvin.app/Contents/MacOS/Kelvin --remove-data` (from source: `python -m labharness.desktop --remove-data`) for the same clean-up, then delete the app.

**Choosing the data folder:** these live in your own folder, `~/Kelvin/users/<you>/data` (for the first account, `~/Kelvin/virtual-lab`, where they were before accounts), until you choose another folder under **Settings → Data folder**. A lab server or a synced drive works:
- **Use this folder** switches at once. Kelvin checks first that it can write there. In the Kelvin app, **Choose…** opens the computer's folder picker.
- **Move what is saved now** takes the existing runs, reports and record along (runs in chats' own folders stay there, and Kelvin keeps finding them). Nothing there is overwritten: a run whose name is taken stays where it was, and Kelvin says so.
- **If the folder can't be written to when you sign in** (a network drive that is offline, say), Kelvin saves to your own folder meanwhile and tells you in Settings.
- **What stays in `~/Kelvin`:** chats, settings, API keys and instrument folders.
- **From a terminal:** `kelvin --data-dir <folder>` overrides the setting, for one person without sign-in.

### Data from other programs

Spectra and tables that Kelvin didn't measure can become runs, so Kelvin's tools work on them: its card, its numbers, `compute`, its plots, reports and the automatic fits. In a chat: "compare the two PL spectra in C:\Data\PL" or "import these files".
- **What it reads:** tables (CSV, TSV, text, with or without a header row; `Wavelength (nm)` gives the column its unit) and LightField's .spe files (each frame's rows summed per column, frames averaged, the wavelengths from the file's calibration). LightField's long CSV export is summed the same way.
- **Several files at once** go side by side into one run when they share their x values, so `compute` can normalise or subtract them: `f_004_sample.counts - f_003_silicon.counts`.
- **Only numbers are read;** names become column names (starting with a letter). The run is kept like any other (in the chat's folder, if it has one), marked as imported, with the files it came from.

### Each chat's folder

Each chat saves its runs in a folder you choose, so you always know where its data are.
- **Where it shows:** the folder button under the message box, at the left, as in code editors: it reads **Data folder**, or the name of the folder you chose. Click it for the whole path, **Recent folders**, **Choose folder…**, **Open folder** and **Use the data folder**.
- **Choosing:** in the Kelvin app, **New chat** opens the computer's folder picker; **Choose folder…** opens it for a chat you are in, and a recent folder is one click. In a browser, you type the folder's full path. Kelvin checks that it can write there, and makes the folder if it is new. A folder change applies from the next message.
- **A new chat** starts in the folder you last chose (in a project, the one its chats last chose). Settings → **Data & reports** → **Ask for a folder when I start a new chat** turns the picker off for new chats.
- **Without a folder of its own** (and from other AI apps through MCP), a chat's runs go to the data folder's `runs/`.
- **Queued runs** go to the folder of the chat that queued them.
- **Kelvin reads the files in it:** PDFs, text and CSV files, and pictures in the chat's folder and its subfolders, never outside it (see [Files in the chat's folder](portal.md#in-the-portal)).
- **Kelvin finds every run wherever it is:** the catalog, reports and Kelvin's tools look in the data folder and in every chat's folder (`folders.json` in the data folder lists them). When a run ends, Kelvin says its whole path. Your other files in a chat's folder are left alone.
- **A folder that can't be written when a run starts** (a drive that is away, say): the run is saved in the data folder instead, and Kelvin says so.
- **Only a person** changes a chat's folder.

## Reports

Kelvin keeps three kinds of report up to date by itself, following **project → device (sample) → scan**. Each is a PDF and a PowerPoint deck with the same numbered sections every time, so you always find things in the same place. They are built from what Kelvin already keeps (run folders, the lab record, the project's notes and reading list), with no model call, so they never touch an instrument and can be written while a plan runs.

| Report | Covers | Sections |
|---|---|---|
| **Scan report** | one run | One page: Result (Kelvin's assessment, plots with fits, fitted values with uncertainties, any issue) · Conditions (what was swept, set and measured; the plan's instruments' other settings as found; drives elsewhere away from safe; what was left changed) · Approvals and alarms. A last line names the file with the rest: `run.h5`. |
| **Device report** | one sample, all its scans | 1 The device (material, geometry, contacts) · 2 Mount history · 3 Results by measurement · 4 Trends across repeated scans · 5 Ranges explored · 6 Problems · 7 Scan index |
| **Master report** | the whole project | 1 Summary · 2 Goals and decisions (project notes) · 3 Devices compared · 4 Main results and figures · 5 Timeline · 6 Open questions and issues · 7 References · 8 Index |

A section with nothing to show yet still appears, with a line saying so.

**Where they go:**

```
<data folder>/
  runs/<run_id>/report.pdf, report.pptx    each scan's report, next to its data (run.h5)
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

**Settings → Reports** chooses PDF, PowerPoint or both (the default), or **Only when I ask**, and which of the three kinds Kelvin keeps up to date. The data (`run.h5`) is always written. A report that can't be written never fails the run; the lab record notes it.

Slides are 16:9; the plots are native shapes and the results native tables, so you can edit them in PowerPoint, Keynote or Google Slides. Neither format needs extra software to write. Characters that the standard PDF fonts can't show are spelled out (Ω becomes "Ohm").
