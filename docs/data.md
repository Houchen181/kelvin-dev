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

What the run needs first goes in its `setup`, so the run's one card covers it: an excitation's amplitude, frequency and output, a laser's emission and power. One of the card's actions runs once there, in order (`isrc.wave_start: run`), or in `after` (`isrc.wave_stop: run`); `on_abort` takes only actions that don't drive the sample. The card shows every drive the run changes. It gives the rate each moves at, as the instrument reports it or as the plan sets it, and how long the run takes. It also shows what the run leaves on at the end ([safety.md](safety.md#the-rules)).

After a run, Kelvin classifies each measured curve and fits it: a peak or dip (Lorentzian or Gaussian: center, FWHM, height, Q for frequency axes), two resolved lines (each center and width, and how far apart), a straight line (slope, and resistance for I–V), a parabola (where it turns, its curvature, and how much it changes across the sweep), an exponential relaxation (τ), an oscillation, a steady value or no signal. Every number has a one-standard-error uncertainty. It also tracks how the fitted feature changes along an outer loop, and it flags clipping, under-resolved or cut-off features, hysteresis, unsettled readings and stopped runs. The result is saved with the run (`analysis` in `run.h5`); `analyze_run` re-fits a run with a chosen model.

The automatic analysis says only what the data support:
- **A read-back is checked, not fitted.** A reading that is the swept set point's own read-back (`magnet.B_actual` while sweeping `magnet.B`), or the swept axis itself, is compared with what was set: the largest difference, and an issue when it lagged or never moved ("the sweep did not reach it"). It is never reported as "linear in" its own set point.
- **A sweep over two axes of 8 or more values each is a map.** It is looked at as an image, not row by row: spots must stand out from the map's own point-to-point noise by more than noise alone reaches anywhere in a map of that many points (a look-elsewhere threshold), and the brightest is fitted with a 2-D Gaussian (center, FWHM along each axis, height). A "spot" wider than the map along an axis is the map's trend, not a spot: the map is then told along each axis, averaged over the other ("along smu.V it has a peak at 1.50 V; along magnet.B it rises as magnet.B²").
- **A peak or dip must be real.** It must span more than one point, lie inside the sweep and be narrower than it, and stand out by more than the noise reaches somewhere along the sweep; a single point that stands out is flagged as a glitch, or a feature finer than the step. A line just short of that is "a possible dip at ...: average longer to tell", never "does not depend". Where one line leaves more than noise behind, two are tried: two lines need a valley between them (an ODMR doublet is two dips, a flat-topped line stays one).
- **A bend is a bend.** A curve that only curves (a magnetoresistance) is a parabola, told as the trend: "rises as magnet.B² from 14.5 µV at magnet.B = 0: +125% at 2.5 T", never a dip with an FWHM wider than the sweep.
- **An oscillation needs turning points inside the sweep:** three, a quarter period from either end (a peak that fills the window is no cycle of a cosine). A decay needs a time the points pin down. On an axis that isn't time, neither is told in time's words ("falls by 1/e over 20 MHz", not "τ = 20 MHz").
- **A resonance recorded in dB is fitted on linear power** (10^(dB/10)), and says so: its FWHM is a linewidth and Q the loaded Q. (A Lorentzian fitted to dB values gives the width of the dB curve, and a Q about 2.4 times too high.) A fit whose power goes below zero is no line.
- **One model for every row of a run.** The traces of a power, temperature or field sweep (up to 12 of them, spread over the run) and the sweeps at each value of an outer loop are fitted with the shape most of them show, so a change in Q or width is the sample's, not the fits'. The trend follows the feature from the first row that shows it to the last, and the most it changes between them, and says where it leaves the window ("the peak center shifts by -2.70 MHz from 0 T to 1.5 T; no peak inside the sweep at 2 T": re-centre the sweep at each field). "Nothing changes" is said only when the rows agree within their errors.
- **What the run measured leads.** A pulse generator's protocol (`rabi`, `ramsey`, `pulsed_odmr`, `cw`, `t1`, `hahn_echo`) says what its sweep should show (an oscillation, lines, a decay), and the next step follows it: only a Rabi's half period is a π time. A sequence written out is analysed without that. A reading that is the ratio of two others of the same instrument (a counter's contrast, signal / reference) leads over its parts, whose own quirks come last. A guard reading (the gate leakage a ramp watches) leads only when nothing else was measured. A reading the device's [measurement chain](#your-data) names is told as the sample's resistance, in ohms.
- **A first point far outside the rest is left out.** A reading of 0 at the run's first point while the rest are far from 0 (a counter read before its first acquisition), or a first point beyond all the others, is said ("counter may not have settled there") and left out of that instrument's fits.
- **No data, no signal: it says so.** A run with no points, a reading with no numbers, or readings with no signal above the noise is never "no problems found".
- **It is quick and bounded.** NumPy fits; a long trace is first looked at decimated (each stretch's lowest and highest point kept, so a narrow resonance stays in), then the model it shows is fitted to every point. A run's analysis stops after 10 s and says what it left out.

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
- **A ramp that stops getting closer** for `stall:` seconds (600 unless the plan says; `0` never) stops the run as Stop does: its outputs go to safe and its points are kept. The reason names the switch the set point goes through when it is below its highest setting (*tc.heater_range is only low: a higher range may be needed*) rather than calling the instrument stuck. The [set-point watchdog](safety.md#watches-and-notifications) also raises an alarm after 5 minutes.
- **The approval** covers the targets and, where the rate is itself a drive (a magnet supply's), the rates; on a draft card, the rates set as a setting (a PPMS's `T_rate`) too. A drive Kelvin ramps itself, a step at a time (a source's voltage), is swept with `{from, to, step}` instead.
- **Its card counts each leg** as the distance over its rate (the leg's own, else the rate the plan or the instrument has set) plus its hold, from where the reading is, in every pass of the loops outside it, and the ramp's points as about that time over `every`. A sample that lags its set point near each target takes a little longer.

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

### Hardware-timed techniques

Some measurements are timed by the instrument itself, into its buffer: cyclic voltammetry on a potentiostat, an impedance sweep, a scope's burst. An instrument's card lists them as techniques (`describe_instrument` shows each with its parameters; [instruments.md](instruments.md#writing-a-card) says how a card declares one). A plan runs one at every point, after its triggers and before its readings, and keeps the buffer as traces:

```yaml
plan: cv_scan_rates
setup: { pstat.compliance_I: 1.0e-3, pstat.nplc: 0.01, pstat.output: on }
technique: { pstat.cv: { start: -0.2, vertex: 0.6, step: 0.002, cycles: 2 } }
sweep: { pstat.cv.rate: { values: [0.01, 0.02, 0.05, 0.1, 0.2] } }   # one of its parameters, stepped
after: { pstat.output: off }
```

- **What is kept:** each quantity it fetches, as a trace against its `x` (`pstat.cv.I`, with `pstat.cv.I.x` the potential of each point), one row per point of the plan.
- **What is checked first:** every value its sweep gives it, against its own limits, and each level against the limits of the drive it is a level of (here `pstat.V`, with the lab's and a mounted device's limits), each rate against that drive's rate limit; and that the drive has no guards, which nothing could watch inside it.
- **One card:** its levels, rate, cycles and duration are on the run's card (or the queue's), with the instrument's own current limit, since Kelvin can't ramp or watch guards inside it. A person approves it; auto mode never does. The run's duration counts it.
- **Stop** ends it at once: the instrument gets the technique's abort commands, and the drives the plan touched go back to safe.
- Put the output, and settings such as a fast integration (`nplc`), in the plan's `setup:`. A technique never sets them itself.

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
- **Checks and kept values** are expressions over the run's columns, as `compute` takes them; `at_min(y, x)` and `at_max(y, x)` give x where y is smallest or largest. `smooth(y, n)` averages n points around each, and near the sweep's ends only the points there are, so `at_min(smooth(counter.rate, 3), mw.frequency)` finds the dip, not the sweep's edge. Put an expression with a comma in quotes.
- **Each stage runs through the safety gate** like any plan. A stage whose check still fails after its retries ends the recipe, and so does a run that stops. The answer lists each stage's run and whether it passed, with the values kept.
- **Kept** in the portal per person or per project, shown in the chat when Kelvin keeps it (save_recipe; Settings → Memory and Project settings list them).

## Your own scripts

Ask Kelvin to run one of your lab's scripts ("run sweep.py with VMAX at 2") and it runs it as it is, in your lab's own Python, with every instrument command through the safety gate ([Safety](safety.md#your-labs-own-scripts)). A script's run is a run like a plan's:
- **Its readings are its rows.** Each reading the script takes from a lab instrument (a query the card knows) goes into the run, with the settings the script had made by then; a new setting starts a new row. So a sweep's readings plot, analyse and report like a plan's (`get_run_data`, `analyze_run`, the run's report), even when the script saves its data its own way.
- **Its folder** keeps `script.py` (the script exactly as it ran, with any value changed for that run), `output.txt` (what it printed), `commands.jsonl` (every command it sent, with the gate's answer), `run.h5` (its readings), the figures it left open (matplotlib) and the run's reports. Kelvin also lists the files the script wrote beside itself.
- **Values for one run:** the script's settings are its module constants with a plain value (`VMAX = 5   # top of the sweep, V`) and its `main()`'s defaults. `check_script` lists them, and `params` changes them in the copy that runs, never in your file; `args` gives its command-line arguments.
- **Which Python:** the one set in **Settings → Instruments → Your lab's scripts**, else the first that has everything the script imports: an environment beside your scripts (`.venv`, `venv` or `env`), this computer's Python, then Kelvin's own (which has numpy, pyvisa and pyserial). Python 3.6 or later. A notebook runs its code cells in order, without its `%` and `!` lines. A MATLAB script doesn't run, but can become a recipe.

## The queue

Line up scans to run one after another, overnight or while you are away, as in Special Measure's queue. Open **Queue** in the Runs pane, or ask Kelvin ("queue a gate sweep at 2, 5 and 10 K"). An item is a plan, or a step between scans:

```yaml
step: warm to 10 K
set: { tc.T_set: 10 }
stabilize: { read: tc.T, within: 0.02, for: 120, timeout: 7200 }   # wait until it settles
wait: 300                                                          # then wait 5 minutes more
```

- **Building it.** Add an item at the end or at a position, with a note that is kept with its run. Reorder with ↑ ↓ (or select an item and press `u` / `d`), edit, or remove it (Delete). **Add to the queue** under a finished run's plan queues that scan again. You can change the queue while it runs; only the running item is fixed.
- **One approval.** **Start** checks every waiting item, then asks you once for every drive range the queue uses. Anything outside what you approved asks again. Asked to (`dry_run`), Kelvin only rehearses the whole queue in order on simulated copies of the lab's instruments, so a step that cools the sample counts for the scans after it, and says how long it takes and what would fail; the queue doesn't start. The rehearsal's instruments take as long as this lab's runs measured (each reading, each ramp step), so it says what the queue's card says ([safety.md](safety.md#the-rules)). The real start's card says when that rehearsal said something would fail.
  - **Over the whole night.** The card covers every value an item sets away from safe, wherever the drive is when the queue starts, and what a plan's `on_abort` sets. An item that switches an output off at its end no longer makes the next one's switching it on a new question at 3 a.m.
  - **Interlocks in queue order.** When an item before it works on an interlock's condition (a cool-down step before a field sweep), that interlock is checked when its drive moves, not at the start, and the card says so. If it still doesn't hold then, the queue pauses there. A queue whose own set points would break an interlock (a warm-up to 15 K before a field sweep, under `T < 10 K`) doesn't start, and says which item ([safety.md](safety.md#the-rules)).
  - **Nobody answering.** A request nobody answers at the computer goes to your phone (see [safety.md](safety.md#watches-and-notifications)) and waits for you there before it is declined.
- **Running.** Items run in the background, one at a time, each scan in its own run folder that records its place in the queue. Chats keep working: readings give the latest values of the instruments the scan uses and read the others, lookups and queue edits go on, and a change to the lab says the queue is busy instead of waiting. A scan's item is filed as it was queued (its chat's project and folder), whichever chat is busy meanwhile. An item of unshared work (a confidential, unpublished or model-limited project's, or an incognito chat's) goes unnamed for other chats wherever the queue is told (its list, the rehearsal and the check before it starts, what holds the lab), and so does what Kelvin is doing for such work.
- **Stopping.** **Pause** stops after the item that is running. **Stop** (any of them, also the runs pane's **Stop run** and the phone's) ends that item as well, a step's wait or settle between scans too, and the queue pauses there; a Stop between two items keeps the next one from starting. A failed or declined item pauses the queue too, unless you choose to go on to the next item (the card then says so). A guard trip (a gate's leakage) pauses it whatever you chose: a gate that broke down is never swept again in the night. The pause says why in plain words, and names the alarm that stopped it (an instrument that stopped answering, an output found off). The queue never starts by itself, even after Kelvin restarts.
- **The morning's account.** When the queue ends, Kelvin writes what happened from the record alone: when it ran and how it ended, what was done, what stopped part-way and the points it kept, what stopped with none, what didn't run, and every alarm raised meanwhile (a guard's trip too), cleared or still on, with how often. It goes out with the queue's notice (to your phone, email or webhook), and the queue shows it until it runs again (`queue_show`, kept across a restart).
- **Keeping queues.** **Save as…** keeps the waiting items under a name (`queues/<name>.yaml` in the data folder), and **Load** adds them again. The queue itself is kept in `queue.json`; beside it, `watches.json` keeps your watches and Kelvin's, and `alarms.json` the latest alarms, so both outlast a restart ([safety.md](safety.md#watches-and-notifications)).

## Your data

Every run is saved as it is measured, with nothing to click, in the folder of the chat that started it (see [Each chat's folder](#each-chats-folder)). It gets its own folder there, named by date, time and plan, such as `20261018-015328-gate_sweep/` (a plan's name longer than 40 characters is cut, with a short code of the whole, so a folder fits in Windows' 260-character paths), which holds:
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
  fits/<name>     each fit kept with fit_run: model, parameters, errors, chi-squared and how it was asked for
                  as attributes; its covariance, and x, y, fit and residuals at every point it used
```

**Every run in one catalog.** `catalog.sqlite` in the data folder lists every run:
- what it swept and measured, with units, ranges and the physical quantity (voltage, temperature, magnetic field...);
- the conditions it was measured in;
- its fitted results.

Kelvin keeps it up to date by itself. It lets the AI answer questions across all your runs, such as "every field sweep on D3 below 2 K last month", without opening each one. Any SQLite tool opens it too. It is only an index: the runs stay the record, and if `catalog.sqlite` is deleted, Kelvin builds it again.

**Asking about your data in a chat.** Kelvin's AI finds runs (`find_runs`, or `query_runs` for anything its filters can't ask), reads a run's card (`describe_run`) and where it came from (`run_provenance`), and reads or computes numbers (`get_run_data`, `compute`):
- **Selecting points:** both reading tools take a condition, `where: smu.V > 1 and tc.T < 5`.
- **`compute` works without writing code:** `lockin.X / smu.I` (a resistance), `deriv(lockin.X, smu.V)`, `mean(smu.I)`. It only does arithmetic on the run's columns.
- **Unshared work stays inside it.** The runs of a confidential, unpublished or model-limited project, of an incognito chat, or of a chat that keeps such a project's marks, are seen only from inside: by the chat that ran them and the chats of their project, with a model the project's rules allow (for confidential work, never one through the Kelvin hub). Other chats, a second opinion or a Compare lane with another model, and agents over MCP don't see them in any of these tools (`list_runs`, `find_runs`, `query_runs`, `describe_run`, `get_run_data`, `compute`, `run_provenance`, `activity`, `recent_events`, plots, fits and reports), nor their part of the lab record (a person's answer to one of its cards too); they are told how many runs they don't see. Each run keeps the chat that started it and the rules it ran under, so lifting a project's rules later doesn't open its earlier runs to a model they excluded.

**Where a run came from, and who did what.** Kelvin's AI reads these rather than guessing:
- **`run_provenance`:** the plan as it ran; who started it and when (this computer's clock, never a rehearsal's); every setting at the start and the end, with what the plan left changed or couldn't read; the measurement chain; each approval it needed and who gave it (their account, and portal or phone); and the stops, trips and alarms while it ran.
- **`activity`:** the lab record between two times (a date, a time, today, or "2h" ago), each line with who: a signed-in person and where they answered, Kelvin's AI, a queue or a plan. It reads the lab's instrument log, everyone's, where there is one.

**A device's measurement chain** says what sits between the instruments and the sample, so a run says it whatever changes later:
```yaml
excitation: {source: iac.amplitude, form: peak}   # a 6221's AC amplitude is its peak; a lock-in's sine out is rms
readings: {li_xx.X: {gain: 100, name: R_xx}}       # a preamplifier's gain before the instrument
notes: 10 MOhm bias resistor on the sine out       # or series_resistor: 10e6 under excitation
```
- **Kept** in the data folder (`chains.json`), per device, or for the setup itself (`setup`), which a device without one of its own uses. Kelvin records it when a person says how the sample is wired (`measurement_chain`), and never assumes peak or rms.
- **Stamped into every run** of the device (its record in `run.h5`), with the excitation worked out from the run's own setting: 50 nA peak is 35.4 nA rms.
- **Its own work's.** The chain of a device of confidential (or other unshared) work is read in that project's chats only: other chats' briefs and `shared_memory` leave it out.
- **Reports plot the named readings as the sample's resistance** (V / gain / I_rms, in ohms), saying how in the caption.

### Fits, figures, files and Python

Kelvin's AI analyses with its own tools rather than handing you a script to run. None of them waits for the lab, so they work while a run is going.

- **Fits (`fit_run`).** Kelvin's models, or your own expression in `x` (`a*exp(-x/tau) + c`: every other name is a parameter, and the run's columns may appear too), fitted with weights (`sigma`: a column, an expression or one number), a window (`where`, `x_range`), starting values, bounds and parameters held fixed (`fix`). The answer gives each parameter with its error, chi-squared and its reduced value, R², warnings (no convergence, a parameter at its bound, parameters the data can't tell apart, residuals that run in long stretches, a line narrower than two steps, an oscillation faster than the points follow, a center outside them), and a picture of the fit with its residuals. It says when a fit found nothing: compared with a flat line (an F test, counting the places a peak or a frequency could have been), one that noise alone would match more often than 1 time in 1000 is "not significant". A fit that did not converge, whose parameters the data can't tell apart, that fits worse than a flat line, or that leaves what it is for undetermined (a width, a time, a center: "fr = 4 ± 77642376 GHz") starts with FAILED. The fit is kept in the run's `run.h5` under `fits/<name>`, with its covariance, its residuals and who kept it and when, and the run's reports show it ([kept and accepted fits](#kept-and-accepted-fits)); fitting again under the same name replaces it. A failed fit is kept only with `keep: true`, marked failed in run.h5, the report and `plot_runs`. Kelvin's models:
  - peaks and decays: `linear`, `lorentzian`, `gaussian`, `double_lorentzian`, `exponential`, `stretched_exponential`, `biexponential`, `damped_cosine` (Rabi and Ramsey; its envelope's power `n` is held at 1 unless you free it; 2 is Gaussian);
  - `exp_irf` and `biexp_irf`: decays starting at `t0`, convolved with a Gaussian instrument response of FWHM `irf` (pump-probe, time-resolved photoluminescence);
  - `resonator`: a notch-type resonator's S21 with the cable delay, after Probst et al., Rev. Sci. Instrum. 86, 024706 (2015): `fr`, the loaded `Ql`, `|Qc|`, the mismatch angle `phi`, and from them `Qi`. Give the phase (`phase`, in degrees or radians) with the magnitude (dB or linear) for the full circle fit; with the magnitude alone, the delay drops out;
  - `graphene`: R against gate voltage, with the contact resistance, mobility, residual (puddle) density and the Dirac point; give the gate's capacitance per area `C_g` and the channel's length/width `aspect` with `fix`;
  - `hall`: the Hall resistance against field: the sheet density n = 1/(e·|R_H|), electrons or holes, and with `fix: {rho_xx: ...}` the Hall mobility. Only from an R_H that stands out from 0 by 3σ, with y a resistance (Ω, kΩ...) and x a field (T, mT, G): volts, or an R_H consistent with 0, give no density and say why;
  - `gl_hc2` and `whh_hc2`: the upper critical field against temperature (Ginzburg-Landau's parabola; Werthamer-Helfand-Hohenberg, dirty limit), with Bc2(0) and the coherence length. Numbers gathered from several runs can be fitted directly (`x_values`, `y_values`).
- **Figures (`plot_runs`).** Curves from one run or several, overlaid: any column or expression (`lockin.X / 1e-6`) in the unit you ask for (`µV`, or an expression's own unit, `Ω`), one curve per value of an outer axis (`group`), error bars, a kept fit drawn dashed, log axes and ranges; a value over two swept axes as a heat map. A repeated sweep is drawn without the stroke back to its start. The figure comes back into the chat with its numbers in words, and is saved in the data folder's `figures/` as PNG, and as SVG and PDF when asked.
- **Files (`export_run`, and the Runs pane's CSV, Excel and NumPy).** Written by Kelvin's code, never copied through the chat, every digit kept: `data.csv` (one row per point, units in the header) and a CSV for each trace (one line per sample), an Excel workbook (`data.xlsx`: the points, each trace and the run on sheets; Kelvin needs no Excel to write it) or a NumPy file (`data.npz`: every column, trace and camera frame as arrays; `numpy.load` reads it). They go in the run's folder. Every run gets `data.csv` and its traces' CSVs when it ends; the **CSV**, **Excel** and **NumPy** buttons on a run's tab in the Runs pane give any of the three (`/api/runs/<id>/export?format=csv|xlsx|npz`).
- **Python (`run_python`).** For what these can't do, Kelvin runs Python on the runs it names, in a process of its own: NumPy and h5py, and SciPy, lmfit and Matplotlib when they are installed in Kelvin's Python (the Kelvin app brings none of the three; the answer says what is there). In the code, `runs[<id>]` (or `run`) gives a run's columns and traces, units, meta and kept fits, and `plot(...)` draws a figure without Matplotlib. What it prints and the pictures it saves come back; its files stay in its own folder, the data folder's `python/<date-time>/`.
  - **What the sandbox stops.** It writes only in its own folder: a run's files, Kelvin's settings and everything else are refused, and h5py opens a run's `run.h5` read-only. It reads only its folder, the runs it was given and Python's own files. It has no network, and cannot start programs, load native libraries (ctypes), touch the registry or import instrument packages (pyvisa, serial, usb...), so it never reaches an instrument or the lab. It runs without Kelvin's API keys or sign-ins. It stops at its time limit (60 s unless asked, at most 10 min), at Stop, and when the AI's call is cancelled. On Windows it gets at most 4 GB of memory and cannot start another process.
  - **What it doesn't stop.** These guards work inside Python, in the same process as the code. Python's documentation says such audit hooks are not a sandbox: code written on purpose to get around them can, and the process runs as you, with your files' permissions. They keep honest code, and code an AI was talked into by text it read, away from the instruments, the lab's records and Kelvin's settings; they are no wall against a determined attacker. Within its time limit it may keep a processor busy (at low priority).

**Runs saved by Kelvin 0.3.19 or earlier** (`data.jsonl`, `meta.json`, `plan.yaml`, `data.csv`...). At its first start after the update, Kelvin writes each one as `run.h5` and moves its old files into `legacy/` in the run's folder. Nothing is deleted.

Next to the run folders sit `events.jsonl`, your record of every command, reading, approval, refusal and alarm with who did it (for an approval or a Stop: the person's account, where they answered and when), and `reports/`. Each person has their own (see [Signing in](portal.md#signing-in)); the lab's instrument log, `~/Kelvin/instrument-log.jsonl`, has everyone's, each with their name.

**When you uninstall Kelvin:** the Windows uninstaller deletes Kelvin's own data and keeps your measurements.
- **Deleted:** settings and API keys, the ChatGPT sign-ins (revoked with OpenAI first), the accounts, chats, projects and memory, the lab owner's PIN and the lab's roles, the lab config and its history, your instrument cards, the instrument folders with their manuals, the manuals Kelvin kept, what it installed for instruments (its environments in `~/Kelvin/env`, and uv and their Pythons in `~/Kelvin/tools`), the log and the window's cache. Everyone's.
- **Kept:** every run folder, `events.jsonl` and `reports/`, in `~/Kelvin/virtual-lab` (where an older Kelvin kept them), in each lab's `~/Kelvin/labs/<lab>`, in each person's `~/Kelvin/users/<name>/data` and in any data folder someone chose; and the lab's instrument log. A note in `~/Kelvin`, "About this folder.txt", says what is there. Installing Kelvin again shows those runs again.
- **Only Kelvin's own items are deleted, by name.** Any other file you put in `~/Kelvin` stays.
- **Kelvin must be closed:** the uninstaller waits until it is.
- **On a Mac or from source:** run `/Applications/Kelvin.app/Contents/MacOS/Kelvin --remove-data` (from source: `python -m labharness.desktop --remove-data`) for the same clean-up, then delete the app.

**Choosing the data folder:** these live in your own folder, `~/Kelvin/users/<you>/data` (for the first account, `~/Kelvin/virtual-lab`, where they were before accounts), until you choose another folder under **Settings → Data folder**. A lab server or a synced drive works:
- **Without accounts** (`--no-accounts`), each lab keeps its own folder, `~/Kelvin/labs/<the lab's name>`. Switching labs switches folders, and nothing moves: the other folder's runs are still found by their ids, and its reports by their names. A lab that measured into `virtual-lab` with an older Kelvin keeps using it. `~/Kelvin/lab-folders.json` records each lab's folder, by its config file, so a renamed lab keeps its own. To move one by hand: close Kelvin, move the folder, and change its line in `lab-folders.json` (or choose the new place under Settings → Data folder, with **Move what is saved now**).
- **Use this folder** switches at once. Kelvin checks first that it can write there. In the Kelvin app, **Choose…** opens the computer's folder picker.
- **Move what is saved now** takes the existing runs, reports and record along (runs in chats' own folders stay there, and Kelvin keeps finding them). Nothing there is overwritten: a run whose name is taken stays where it was, and Kelvin says so.
- **If the folder can't be written to when you sign in** (a network drive that is offline, say), Kelvin saves to your own folder meanwhile and tells you in Settings.
- **What stays in `~/Kelvin`:** chats, settings, API keys and instrument folders.
- **From a terminal:** `kelvin --data-dir <folder>` overrides the setting, for one person without sign-in.

### Kept and accepted fits

A run's fits after it was measured are of two kinds, and its reports show both.
- **Kept** (`fit_run`, or **Keep fit** in the Analysis pane): each fit is kept with the run in its `run.h5` (`fits/<name>`; `keep: false` only looks; a failed fit only with `keep: true`, marked failed), with the model and how it was asked for (window, weights, start and held values, a trace's row; from the pane also the steps that made its data, its notes, such as a resonance in dB fitted on linear power, and Kelvin's version), its values with their errors, the covariance and residuals, and who kept it and when: the agent, for the person signed in when people sign in, or the person who kept it in the pane. A run keeps as many as are made; fitting again under a name replaces that one. The run's reports draw each (its points with the model dashed) beside its values, newest first, saying who kept it and when; past four, the rest are listed by name. The run's scan report is written again when one is kept.
- **Accepted** (`analyze_run` with `accept`, once the person agrees the fit is right): the re-analysis becomes the run's own result for that column, in place of the automatic one. The run's plots and results table show it, marked "accepted by ... on ..." with its window and reduced chi-squared, and the device and master reports follow it across runs (trends, key results).

A kept fit leaves the run's result as it was. To make a fit the result, re-analyse the run with the model you chose and accept that.

### Data from other programs

Data that Kelvin didn't measure can become runs, so Kelvin's tools work on them: its card, its numbers, `compute`, `look_at`, `plot_runs`, `fit_run`, `run_python` and reports. In a chat: "compare the two PL spectra in C:\Data\PL", "import these files", or attach the file with the paperclip.

| What | Read as |
|---|---|
| **Tables:** CSV, TSV, text | The row just above the numbers names the columns, after any lines of settings an instrument writes first. `Wavelength (nm)`, `I [nA]`, `time/s` or a row of units under the names, such as `(sec)`, give the units. Dates and times become seconds from the first, and the note says when that was. A decimal comma is read. Text columns are left out, and rows whose x isn't a number are skipped; the notes say so. A long table of a 2-D scan (`gate, field, R` with the gate held while the field runs) becomes a 2-D run, which `look_at` shows as a map. |
| **Excel** (.xlsx) | The first sheet with numbers, read as a table. Kelvin needs no Excel. An old .xls: save it as .xlsx or CSV first. |
| **Arbin** CSV or Excel export (MITS Pro, MITS 7 and 8) | On the test time. An Excel export's Channel sheets are read one after another. Arbin's own .res database isn't read: export the test as Excel or CSV. |
| **BioLogic** EC-Lab .mpt (text) | A voltammogram on its potential (Ewe), an impedance spectrum on its frequency, anything else on time. `-Im(Z)` is `minus_im_z`, `|Z|` `abs_z`, `<I>` `i_mean`. The note gives the technique, the start, and the electrode's area and active mass. |
| **BioLogic** EC-Lab .mpr (binary) | Only when Kelvin is sure of its layout. BioLogic doesn't publish it, so Kelvin reads the column types the open-source reader galvani lists. It checks that the points fill the file exactly and that time runs forward. Otherwise it refuses, with a word to export .mpt from EC-Lab, rather than read numbers that may be shifted. |
| **Gamry** .dta | A voltammogram's cycles one after another (column `curve`), on its potential; an impedance spectrum on its frequency; anything else on time. The open-circuit stretch before a measurement is left out, and said. |
| **LightField** .spe and its long CSV | Each frame's rows summed per column, frames averaged, the wavelengths from the file's calibration. |
| **NumPy** .npy, .npz | 1-D: a column. 2-D and narrow: a table, with x its first column when that runs one way. 2-D and wide: a camera frame. 3-D: a stack of frames. A record array: its named fields. A .npz: its 1-D arrays of one length side by side. Arrays of Python objects are never loaded. |
| **TIFF** (.tif, .tiff, BigTIFF too) | A camera frame, or a stack of them: a point per frame, kept as a camera's frames are. A stack also gets each frame's mean and maximum. `look_at` shows the last frame, or any by its number (`frame: 3`, from 0); `run_python` reads them all from run.h5's `/frames/`. Read: 8 to 64-bit gray or RGB, in strips or tiles, uncompressed, LZW, Deflate or PackBits. JPEG-compressed TIFFs are refused, so save them another way. |

- **Several files at once** go side by side into one run when they share their x values, so `compute` can normalise or subtract them: `f_004_sample.counts - f_003_silicon.counts`.
- **Attached in a chat:** a data file you attach becomes a run as you attach it, filed with the chat's runs (in its folder, if it has one), even while a run holds the lab: an import touches no instrument. The chip under your message says how many points or frames it has, and the AI is told the run's id and columns. A file Kelvin can't read is refused at once, saying why. Attachments go up to 40 MB; for a bigger file, give Kelvin its full path.
- **Fits:** the automatic fits run on tables and spectra. They don't run on electrochemistry, a cycler's record or frames: they are made for Kelvin's own sweeps, and on such data they mislead (a voltammogram's set point "linear in its potential"). Use `fit_run` with the model you choose.
- **Only numbers are read.** The AI gets names only as column names (letters, digits and _) and units only when they are a unit's few characters. A maker's settings reach it only in Kelvin's own words: the technique, the start, an area. Comments, sample notes and text columns stay in the file. The run is kept like any other, marked as imported, with the files it came from.

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
- **Unshared work stays apart:** a chat's scan never goes under the mounted sample of another project when either is unshared work (confidential, unpublished or model-limited), nor when an incognito chat starts it: it goes to its own chat's project (an incognito chat's to none), without the sample, and the chat is told (for a queued scan, on its line in the queue). Work outside every chat (the Queue pane's, an agent's over MCP) goes under the mounted sample, and keeps its project's marks: a confidential sample's scan is that project's unshared work.

**The master report's opening.** By default it is a summary of the data: how many scans, on which devices, and the latest result. Write your own in **Project settings → Master report**, or press **Write the opening with Kelvin**. Kelvin writes it from the project's scans and notes, if the project's rules allow your model. Kelvin uses that opening until you change it, then the summary of the data follows.

**Reports of runs you choose.** For a different selection or your own words:
- **In a chat:** "make a report of today's runs for my advisor". Kelvin's `make_report` covers the runs started in this conversation, or in all of the project's conversations, unless you name runs, a day, or the last N. Another project's or chat's unshared work is left out, and the answer says how many runs. It also rewrites a master or device report on request (`kind` project or device).
- **The length you ask for:** "one page" (`pages: 1`) gives one page: the report leaves out detail, least needed first (the plans, the conditions tables and the lab log, then all but each run's result and first plot, then the runs as a table), and says it was shortened; each run's own report keeps the rest. A deck keeps the summary and each run's result slide.
- **Fits:** a run's report shows its accepted fit as its result, and the other fits kept with it, each with who kept or accepted it and when ([kept and accepted fits](#kept-and-accepted-fits)).
- **In the Runs pane:** **Report…** opens the library of every report above, and below it makes a report of this run, the project's or chat's runs, today's runs, or the last five. Today's runs and the last five are those the chat you are in may see: another project's or chat's unshared work is left out, and the dialog says how many runs. These go to `reports/custom/`.

**Settings → Reports** chooses PDF, PowerPoint or both (the default), or **Only when I ask**, and which of the three kinds Kelvin keeps up to date. The data (`run.h5`) is always written. A report that can't be written never fails the run; the lab record notes it.

Slides are 16:9; the plots are native shapes and the results native tables, so you can edit them in PowerPoint, Keynote or Google Slides. Neither format needs extra software to write. Characters that the standard PDF fonts can't show are spelled out (Ω becomes "Ohm").

### A record for your lab notebook (ELN)

A run's or a day's record as Markdown, with its plots as PNG files beside it, to paste or upload into an electronic lab notebook (eLabFTW, LabArchives, Benchling, OneNote, Obsidian...). Like the reports, it is written by Kelvin's code from what Kelvin keeps, never retyped by a model, so every number is the run's own.
- **What a run's record holds:** when it ran, its duration, points and status, who started it and the run folder; what it set, swept and measured; each instrument's values at the start, and what was away from safe; **each instrument's calibration in force when it started** (and whether it was out of date then, see [Calibration](portal.md#calibration)); the plots with their numbers in words, the results with their fits, the assessment, its issues and the next step; the approvals and alarms with who answered and when; what it left changed; and the plan.
- **A day's record** has that day's runs (the latest 40) and the day's lab record: approvals and declines, runs, stops, alarms, calibrations recorded and changes to the lab, as `events.jsonl` keeps them. Every command is still in `events.jsonl`.
- **Where to get it:** on a run's tab in the Runs pane, **Lab notebook** beside the data's **CSV**, **Excel** and **NumPy**; for a day, **Report…** → **For a lab notebook (ELN)**, a date, **Download the day's record**. Each downloads a .zip (the Markdown and its `figures/`). In a chat: "put today's runs in my lab notebook" (`export_eln`: runs by id, a day, or the last N).
- **A day's record follows the chat it is made from:** another project's or chat's unshared work is left out, its runs and its part of the lab record, and the record says how many runs it left out. Download a confidential project's day from one of its chats.
- **A copy stays** in the data folder's `eln/<name>/` (`<name>.md` and `figures/`), and making one is in the lab record.
