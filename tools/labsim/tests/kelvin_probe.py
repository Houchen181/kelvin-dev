"""Connect a Kelvin lab (Kelvin's own adapters and cards) to a bench and read every parameter once:
   LABSIM_HOME=... KELVIN_VISA_BACKEND=@labsim PYTHONPATH=<labsim>/shims;<labsim>;<kelvin src>;<pyvisa> python kelvin_probe.py lab.yaml"""
import sys
import time
from labharness.homelab import build_lab

t0 = time.time()
lab = build_lab(sys.argv[1], partial=True)
print("connected in %.1f s; set aside: %s" % (time.time() - t0, getattr(lab, "set_aside", None) or getattr(lab, "unavailable", None)))
for h in lab.health_check():
    print("health:", h)
for name, inst in lab.instruments.items():
    card = getattr(inst, "card", None)
    params = list(card.params) if card is not None else []
    for p in params:
        t = time.time()
        try:
            v = lab.read(name, p)
            print("  %-8s %-14s = %-28r (%.2f s)" % (name, p, v, time.time() - t))
        except Exception as e:
            print("  %-8s %-14s ! %s: %s (%.2f s)" % (name, p, type(e).__name__, str(e)[:150], time.time() - t))
