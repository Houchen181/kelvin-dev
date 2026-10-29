---
name: cool-down-watch
description: Cooling a cryostat or fridge, or waiting for any temperature to settle: what to check before, what to watch, how to wait.
---
1. Before: every source at its safe value and outputs off, magnet at zero field, heaters off unless the procedure needs them; the sample's wiring and vacuum as the person set them.
2. Watch the readings that warn early (sample and stage temperatures, pressure, heater output) with watch, so an alarm reaches the person even while no chat is open.
3. Wait with wake_me (until: the temperature below the target, for a hold of some minutes), never with a long wait inside a turn.
4. To measure on the way (R(T) while it cools), don't put the set point in a plan's setup: it waits there until the temperature arrives and measures nothing. Sweep it with ramp: instead, e.g. {ramp: [{to: 2, rate: 2}], every: 10}: points all the way down, and the run stops if the cryostat stops getting closer.
5. The sample lags its thermometer: once the setpoint is reached, wait until the reading is stable (a plan's stabilize, with near: so a steady reading short of the set point doesn't count, or a hold in wake_me) before measuring.
6. At base, check the device survived the cool-down before driving it: two-probe resistances of the contacts against the room-temperature values, and gate leakage on a small sweep.
7. Warming up again: sources and fields to safe first; let the person decide when the sample may be exposed to air.
