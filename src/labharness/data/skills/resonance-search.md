---
name: resonance-search
description: Finding a resonance, a line or a peak (ODMR, a cavity, ESR or NMR, an optical line): coarse then fine, fit, and check it is real.
---
1. Coarse sweep over the whole expected range with a step at most half the expected linewidth; a coarser step can miss it.
2. look_at the plot, then locate the feature with compute (at_min or at_max of the signal against the swept value).
3. Fine sweep over 3 to 5 linewidths around it, with at least 10 points across the linewidth; fit (lorentzian or gaussian) and read the center, width and depth with their uncertainties.
4. Check it is real: repeat it; sweep the other way (drift, hysteresis); lower the drive power (power broadening and saturation change the width and depth); a control with the drive off.
5. Report the center ± its uncertainty, the width, the contrast, and the conditions (power, temperature, field).
6. When this will be done again, keep it as a recipe: coarse stage, keep the center, fine stage around it.
