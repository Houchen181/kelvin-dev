"""Home the delay line, turn the lasers on, scan the delay, read the lock-in; ask the Pfeiffer gauge (ENQ)."""
import time
import pyvisa

rm = pyvisa.ResourceManager("@labsim")
st = rm.open_resource("ASRL6::INSTR", baud_rate=57600, read_termination="\r\n", write_termination="\r\n", timeout=2000)
print("stage state:", st.query("1TS").strip())
st.write("1PA10")
print("move before homing ->", st.query("1TE").strip(), "(H: not allowed while not referenced)")
st.write("1OR")
while not st.query("1TS").strip().endswith(("32", "33")):
    time.sleep(0.5)
print("homed:", st.query("1TS").strip(), st.query("1TP").strip())
for port, p in (("ASRL8::INSTR", "0.02"), ("ASRL9::INSTR", "0.005")):
    las = rm.open_resource(port, baud_rate=115200, read_termination="\r\n", write_termination="\r\n")
    print(port, las.query("*IDN?").strip(), las.read().strip(), end=" | ")
    las.write("SOUR:POW:LEV:IMM:AMPL " + p); print(las.read().strip(), end=" ")
    las.write("SOUR:AM:STAT ON"); print(las.read().strip())
li = rm.open_resource("GPIB0::8::INSTR")
print("overload at SENS 12 (20 uV): LIAS?", li.query("LIAS?").strip()); li.write("FMOD 0;SENS 22;OFLT 7;OFSL 1")
for mm in (12.0, 12.33, 12.345, 12.36, 12.45, 12.9, 14.0):
    st.write("1PA%.4f" % mm)
    while not st.query("1TS").strip().endswith("33"):
        time.sleep(0.05)
    time.sleep(0.4)
    print("delay %.3f mm  ->  X = %+.3e V   (ref %s Hz)" % (mm, float(li.query("OUTP? 1")), li.query("FREQ?").strip()))
g = rm.open_resource("ASRL3::INSTR", read_termination="\r\n", write_termination="\r\n", timeout=2000)
g.write("PR1")
ack = g.read()
g.write_raw(b"\x05")
print("gauge: ack %r  reading %s" % (ack, g.read().strip()))
g.write("XYZ")
print("gauge, unknown mnemonic -> %r" % g.read())
