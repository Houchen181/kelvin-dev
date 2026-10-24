# Safety

Every command goes through one safety gate, whoever sends it: Kelvin, another agent, or a person in the terminal.

## The rules

Every command passes through the `SafetyGate`, both direct commands (`get`, `set`, `raw`) and the steps of a plan.

| Command | What happens |
|---|---|
| Measure / configure | Runs immediately and is logged |
| Drive toward safe | Runs, ramped at the lab's rate |
| Drive away from safe | Limits and interlocks are checked, then a human is asked (unless the project envelope, an approved plan or auto mode covers it). The change is ramped while guard readings are watched, and it goes back to safe if one trips |
| Beyond the lab's limits or the mounted device's, or a blocked command | Refused |

- Raw strings are matched against the card. A raw string that sets a drive is treated as a drive, and one that changes a source's range follows the range rule below. One that touches a drive's command family but can't be parsed is sent to a human. Blocked and reset families come from the card.
- A plan is approved once as a range (if it drives the sample) and then run. Its caution comes from the limits, the approval, ramps and guard readings, so it isn't dry-run first. A dry run on twins is there when someone asks for one (`simulate_plan`, or `dry_run` on a run or a queue), and in auto mode (below).
- Stopping a plan (the Stop button, or the agent's own abort) runs its `on_abort` steps. By default, every drive the plan touched goes back to safe, or holds where the lab config's `safe_actions` says to hold. A guard trip sends that drive back to safe.
- Approvals come from an `Approver`, a human-facing object the agent never sees.
- An instrument whose card is a draft (no one has reviewed it yet) keeps its outputs simulation-only on real hardware, and every change to its settings asks a person, since its measure and drive tags are unchecked. Reading it is free.

### Checked like an experiment, not tested like code

Language models are trained largely on programming, where every step is tested and checked again. At the bench that only costs time, so Kelvin keeps the checks that protect the sample and drops the rest:
- **Instruments are checked before use, not before every scan.** A run checks the instruments it uses: identity, error queue, readings, drives inside limits. An instrument that passed is not asked again for 10 minutes, so a series of scans or a queue doesn't stop to re-check. A plan that drives the sample always checks now, and an instrument whose run failed is checked again next time. **health_check** always checks.
- **No routine dry runs.** Not even a plan that drives the sample is dry-run on its own: what a dry run would predict, the limits, the approval and the guard readings catch on the real run, which goes back to safe if a guard trips. A dry run happens when the person or the agent asks for one, for instance to see how long a long plan or a queue takes, and in auto mode, where no person looks at the run.
- **The agent works like an experimentalist.** Its instructions tell it to:
  - run a plan directly, with no separate validate or simulate first and no test or verification scans;
  - not read a value again to confirm it, since every set and get reports what the instrument says;
  - not try variations to get past a refusal or failure, but stop and say what happened;
  - not write code or re-analyse data unless asked.

### Auto mode

A person can switch approvals from **Ask before drive changes** to **Auto for low-risk runs**, in the portal (next to the model picker) or on the approval page. No agent tool can switch it. Kelvin goes back to asking every time it restarts.

In auto mode, a drive change or a run goes ahead without asking when all of these hold:
- Every drive stays within `auto_fraction` (default 25%) of the way from its safe value to its lab limit. Kelvin judges the target's distance from safe, not the step size, so creeping up in small steps doesn't get around the rule.
- Switches (outputs, ranges) move at most one step from safe.
- For a run, its dry run finishes cleanly. In auto mode a run is dry-run first, since no person looks at it.
- The lab config doesn't exclude the instrument.

Anything else asks as usual, and the request says why auto mode didn't cover it. Each auto-approval shows in the conversation with its reasons and is written to the event log. Limits, interlocks, guard readings and Stop work the same in both modes.

Whoever sets up the lab writes the rule in the lab config:

```yaml
auto_fraction: 0.25                 # share of the way from safe to each lab limit
instruments:
  smu:
    auto: { V: [-5, 5] }            # this parameter's own auto range
  laser:
    auto: never                     # a person approves every change on this instrument
```

## Protecting the sample

A finished device (a 2D heterostructure, a nanowire, a fragile flake) is often worth more than the setup. Most are lost to the same few accidents: a gate pushed past its dielectric's breakdown, a ramp that goes on while the leakage climbs, a spike when a source changes range or switches its output under bias, and a discharge while someone rewires. Kelvin guards against each.

**Each device's own limits.** Mount the device in the Lab panel (**Sample**) and press **Protect this device…**. For each gate, give its dielectric and thickness: the limit is 0.3 V/nm for hBN and SiO₂ and 0.2 V/nm for Al₂O₃ and HfO₂, well below breakdown, unless you type your own. Add whatever else the sample can't take, such as a bias current or a laser power. While the device is mounted:
- The tighter of its limits and the lab's holds. No approval, envelope or auto mode passes it, and a refusal names the device.
- Each gate ramps no faster than its rate. It stops and goes back to 0 once its leakage reading passes its limit (1 nA unless you set another).
- The source itself is capped where its card allows. Its range is fixed just above the limit with autorange off, and its current limit (compliance) is 10 times the leakage limit, so a breakdown can't pass more. These caps are written into the instrument, so they hold even if Kelvin or the computer stops. They are limits too: nothing raises them while the device is mounted. A cap that can't be written yet waits and is written as soon as it can: when the source is back at 0, the instrument is switched on, or the running plan ends.
- A plan that names another device on the same chip (`device: D5`) is held to both devices' limits for that run.
- Mounting a device, or changing its protection, while a drive sits outside the new limits says so. That drive may then only move back within them, or to safe.

Only a person sets or changes a device's protection. Kelvin reads it (the lab status shows it), and while a protected device is mounted it can't change what is mounted.

**No switching glitches.** A source's range, or its autorange switch, changes only while its level is at safe. The gate refuses it otherwise, whether it comes from a plan, the agent or a raw command. Outputs already switch cleanly: a drive is parked at safe before its output switches on, and ramped to safe before it switches off.

**Hands on the sample.** Before you touch the sample (connect it, rewire, flip a breakout box), press **Hands on the sample** in the Lab panel or on your phone. Kelvin stops what runs and brings everything that applies something to the sample (sources, excitations, light, RF) to safe with its outputs off. Then every drive stays at safe until a person unlocks. Temperature, field and positions stay as they are, and Kelvin can still read and measure. The lock outlasts a restart of Kelvin. To unlock, choose the sample that is in the setup now and confirm it is wired as the next measurement needs and nobody is touching it; its protection holds from that moment. Any paired phone can lock; only one that can approve can unlock.

A source's card names its range and current limit on its level (see [instruments.md](instruments.md#writing-a-card)).

## Watches and notifications

Kelvin keeps an eye on the lab between runs and during them. A **watch** reads one value every few seconds and raises an alarm when the value leaves its range for longer than the hold time. Durations are in lab time, so the virtual lab at 10x behaves like the real one. The alarm clears itself when the value comes back. Kelvin also raises an alarm when an instrument stops answering (three failed reads in a row) and when a real instrument reports an error in its own error queue (checked once a minute).

Who can add a watch, and what it may do:

| Watch | Added by | On alarm |
|---|---|---|
| Lab watch | A person, in the lab config | Tells people; can stop the running plan and send named drives to their **safe** values (the config refuses any other value) |
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
