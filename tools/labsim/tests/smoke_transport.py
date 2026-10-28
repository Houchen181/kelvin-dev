"""A quick look at the transport bench through PyVISA, the way a lab script would see it."""
import time
import pyvisa
from pyvisa.constants import Parity

rm = pyvisa.ResourceManager("@labsim")
print(rm.visalib, rm.list_resources())
for r in rm.list_resources():
    if r.startswith("ASRL"):
        continue
    inst = rm.open_resource(r, timeout=1500)
    try:
        print("%-42s %s" % (r, inst.query("*IDN?").strip()))
    except Exception as e:
        print("%-42s ERROR %s" % (r, e))
    inst.close()

print("-- Lake Shore 336 on COM3, wrong framing first (8N1)")
tc = rm.open_resource("ASRL3::INSTR", timeout=800)
try:
    print(tc.query("*IDN?"))
except Exception as e:
    print("  as expected:", type(e).__name__, str(e)[:60])
tc.close()
tc = rm.open_resource("ASRL3::INSTR", baud_rate=57600, data_bits=7, parity=Parity.odd,
                      read_termination="\r\n", write_termination="\r\n", timeout=2000)
print("  ", tc.query("*IDN?"), tc.query("KRDG? A"), tc.query("KRDG? B"), tc.query("HTR? 1"))

print("-- AMI 430 over its socket")
m = rm.open_resource("TCPIP0::192.168.10.30::7180::SOCKET", read_termination="\r\n", write_termination="\r\n")
print("  greeting:", repr(m.read()), repr(m.read()))
print("  ", m.query("*IDN?"), m.query("FIELD:UNITS?"), m.query("STATE?"), m.query("FIELD:MAGnet?"))

print("-- gate: 2400 at 5 V")
k = rm.open_resource("GPIB0::24::INSTR")
try:
    k.query(":READ?")
except Exception as e:
    print("  READ? with the output off:", type(e).__name__, "->", k.query(":SYST:ERR?").strip())
k.write(":SOUR:VOLT 5;:SENS:CURR:PROT 1e-6;:OUTP ON")
print("  READ?", k.query(":READ?").strip())
print("  undefined:", end=" ")
k.write(":SOUR:VOLTS 3")
print(k.query(":SYST:ERR?").strip())

print("-- 6221 sine 100 nA at 17.77 Hz, phase marker on; SR830s external ref")
s = rm.open_resource("GPIB0::12::INSTR")
s.write("SOUR:WAVE:FUNC SIN;:SOUR:WAVE:AMPL 1e-7;:SOUR:WAVE:FREQ 17.77;:SOUR:WAVE:PMAR:STAT ON;:SOUR:WAVE:ARM;:SOUR:WAVE:INIT")
print("  6221 errors:", s.query("SYST:ERR?").strip())
li = rm.open_resource("GPIB0::8::INSTR")
li.write("FMOD 0;SENS 17;OFLT 7")
time.sleep(1.0)
print("  li_xx FREQ?", li.query("FREQ?").strip(), "X,Y:", li.query("SNAP? 1,2").strip(), "LIAS?", li.query("LIAS?").strip())
li2 = rm.open_resource("GPIB0::9::INSTR")
li2.write("FMOD 0;SENS 14;OFLT 7")
time.sleep(0.6)
print("  li_xy X,Y:", li2.query("SNAP? 1,2").strip())
nv = rm.open_resource("GPIB0::7::INSTR", timeout=5000)
print("  2182A READ?", nv.query(":READ?").strip())
dmm = rm.open_resource("GPIB0::22::INSTR", timeout=5000)
print("  34401A MEAS:RES?", dmm.query("MEAS:RES?").strip())
print("-- query interrupted: write a query, then another command before reading")
li.write("OUTP? 1"); li.write("SLVL?"); print("  read:", li.read().strip())
