---
name: lock-in-measurement
description: Setting up a lock-in measurement: excitation, frequency, time constant and settling, sensitivity, phase.
---
1. Excitation: small enough for a linear response (check: doubling it doubles the signal). For transport, drive a current through a series resistor much larger than the sample.
2. Frequency: away from the mains (50 or 60 Hz) and its harmonics, e.g. 13.7 or 17.77 Hz; low enough that cable capacitance does not shunt a high-impedance sample (a growing Y, or phase, as the frequency rises shows it).
3. Time constant τ and filter slope: settling to 99% takes about 5τ (6 dB/oct), 7τ (12), 9τ (18) or 10τ (24). Give each point at least that as settle in the plan.
4. Sensitivity: the signal at 30 to 90% of full scale, never overloaded (check the status). Raise the dynamic reserve for a noisy signal.
5. Phase: set it once on a known signal (auto-phase, or Y near 0 at the start), never in the middle of a sweep. Record X and Y, or R and θ.
6. Noise floor: measure with the excitation off, or far from any feature, before reading small signals. Averaging N points improves it as √N.
7. Derivative-like signals (dI/dV with a small modulation) read at the first harmonic; a second-derivative signal at 2f.
