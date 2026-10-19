# Safety

Every command goes through one safety gate, whoever sends it: Kelvin, another agent, or a person in the terminal.

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
- An instrument whose card is a draft (no one has reviewed it yet) keeps its outputs simulation-only on real hardware, and every change to its settings asks a person, since its measure and drive tags are unchecked. Reading it is free.

### Auto mode

A person can switch approvals from **Ask before drive changes** to **Auto for low-risk runs**, in the portal (next to the model picker) or on the approval page. No agent tool can switch it. Kelvin goes back to asking every time it restarts.

In auto mode, a drive change or a run goes ahead without asking when all of these hold:
- Every drive stays within `auto_fraction` (default 25%) of the way from its safe value to its lab limit. Kelvin judges the target's distance from safe, not the step size, so creeping up in small steps doesn't get around the rule.
- Switches (outputs, ranges) move at most one step from safe.
- For a run, the dry run finishes cleanly.
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
