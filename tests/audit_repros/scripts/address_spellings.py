"""Repro: spellings of a lab instrument's own address that Kelvin does not match to it, and what a person is then
asked.

docs/safety.md: "Every spelling of a lab instrument's address counts as it ..., so none opens a session of the
script's own." scriptrun.canonical (scriptrun.py:519-539, on scripts.normalize, scripts.py:214-234, and
adapters.resource_name, adapters.py:430-443) misses the spellings below. same_device (scriptrun.py:568-584) does
not catch them either: it skips an instrument whose way in equals the script's (`have == want`, line 579), taking
it that lab_name matched it already, and _way() reads nothing from a serial port's other spellings.
So ScriptRun._open (scriptrun.py:1814-1834) asks "Let sweep.py open <address>, which is not in this lab: Kelvin
can't check what it sends there", and a yes lets pyvisa or pyserial open the script's own session to the lab's
instrument, past the gate (on a COM port Kelvin itself holds, the script's open then fails instead).

  ASRLCOM7::INSTR      pyvisa-py's name for COM7 on Windows (its list_resources gives exactly this)
  \\\\.\\COM7            the Win32 device path, what pyserial needed for COM10 and up before 3.0
  COM7:                DOS style
  TCPIP0::h::inst0     VISA's own grammar makes the trailing ::INSTR optional
  USB...::RAW          the same USB interface, opened as a raw resource
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import make_lab
from labharness import scriptrun

LAB = """
lab: Address lab
instruments:
  laser:
    card: virtual/laser
    address: serial://COM7
  vna:
    card: virtual/lock-in
    address: TCPIP0::10.0.0.5::inst0::INSTR
  dmm:
    card: virtual/source-meter
    address: USB0::0x0957::0x1796::MY123::INSTR
"""


def opener(lab):
    asked = []

    def ask(request):
        asked.append(request)
        return True   # the person says yes
    me = types.SimpleNamespace(lab=lab, rehearsal=False, allowed=set(), prep=types.SimpleNamespace(name="sweep.py"),
                               opened={}, passed=[], _ask=ask)
    return me, asked


def main():
    lab = make_lab(LAB)
    for name, ic in lab.config.instruments.items():
        print("the lab's %-5s is at %s" % (name, ic.address))
    print()
    tries = [("laser", "COM7", "serial"), ("laser", "ASRL7::INSTR", "visa"),            # matched: the baseline
             ("laser", "ASRLCOM7::INSTR", "visa"), ("laser", "\\\\.\\COM7", "serial"), ("laser", "COM7:", "serial"),
             ("vna", "TCPIP0::10.0.0.5::INSTR", "visa"),                                # matched: the baseline
             ("vna", "TCPIP0::10.0.0.5::inst0", "visa"),
             ("dmm", "USB0::0x0957::0x1796::MY123::RAW", "visa")]
    for name, address, kind in tries:
        me, asked = opener(lab)
        answer = scriptrun.ScriptRun._open(me, address, kind)
        if answer.get("instrument"):
            print("%-34r -> Kelvin's stand-in for %s (through the gate)" % (address, answer["instrument"]))
        else:
            print("%-34r -> NOT matched to %s; the script opens it itself after this card:" % (address, name))
            for q in asked:
                print("        %s | %s" % (q.title, " ".join(q.items)))


if __name__ == "__main__":
    main()
