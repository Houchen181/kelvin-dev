"""Scratch probe (not a finding by itself): how scriptrun.canonical / lab_name / same_device / outside treat
spellings of one lab instrument's address."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import make_lab
from labharness import scriptrun

LAB = """
lab: Address lab
instruments:
  smu:
    card: virtual/source-meter
    address: GPIB0::24::INSTR
  dmm:
    card: virtual/source-meter
    address: USB0::0x0957::0x1796::MY123::INSTR
  vna:
    card: virtual/lock-in
    address: TCPIP0::10.0.0.5::inst0::INSTR
  laser:
    card: virtual/laser
    address: serial://COM7
  mag:
    card: virtual/source-meter
    address: ASRL4::INSTR
  sock:
    card: virtual/lock-in
    address: TCPIP0::scope-1.lab::5025::SOCKET
"""
lab = make_lab(LAB)
for name, ic in lab.config.instruments.items():
    print("lab %-6s %-40s canonical %s   way %s" % (name, ic.address, scriptrun.canonical(ic.address), scriptrun._way(ic.address)))
print()
tries = [
    "GPIB::24", "gpib0::24::instr", "GPIB0::24::0::INSTR", "GPIB0::24::INTFC", "GPIB0::24 ", "GPIB0::024::INSTR",
    "USB0::0x0957::0x1796::MY123::0::INSTR", "USB0::0x0957::0x1796::MY123::RAW", "USB::0x0957::0x1796::MY123",
    "usb0::2391::6038::my123::instr",
    "TCPIP0::10.0.0.5::INSTR", "TCPIP::10.0.0.5", "TCPIP0::10.0.0.5::inst0", "TCPIP0::10.0.0.5::INST0::INSTR",
    "TCPIP0::10.0.0.5::gpib0,5::INSTR", "TCPIP0::010.000.000.005::inst0::INSTR", "TCPIP0::10.0.0.5::hislip0::INSTR",
    "TCPIP0::10.0.0.5::5025::SOCKET", "TCPIP0::10.0.0.5::inst1::INSTR",
    "COM7", "com7", "COM7:", r"\\.\COM7", "ASRL7::INSTR", "ASRL7", "asrl7::instr", "ASRLCOM7::INSTR", "serial://COM7",
    "serial://com7", "COM07", "ASRL07::INSTR",
    "COM4", "ASRL4::INSTR", "ASRL4", "ASRLCOM4::INSTR", r"\\.\COM4", "serial://COM4",
    "TCPIP0::scope-1.lab::5025::SOCKET", "TCPIP0::SCOPE-1.LAB::5025::SOCKET", "TCPIP0::scope-1::5025::SOCKET",
    "TCPIP0::scope-1.lab.::5025::SOCKET", "TCPIP0::192.168.0.9::5025::SOCKET",
]
for a in tries:
    try:
        print("%-42s canonical %-40s lab_name %-6s same_device %-6s" % (
            a, scriptrun.canonical(a), scriptrun.lab_name(lab, a), scriptrun.same_device(lab, a)))
    except Exception as e:
        print("%-42s ERROR %s: %s" % (a, type(e).__name__, e))
