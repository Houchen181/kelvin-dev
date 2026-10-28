"""Resonator dips, flux tuning and a qubit tone's dispersive shift, seen through the PNA as a script would."""
import numpy as np
import pyvisa

rm = pyvisa.ResourceManager("@labsim")
vna = rm.open_resource("TCPIP0::A-N5222B-11204::hislip0::INSTR", timeout=20000)
vna.write("SYST:PRES;:CALC1:PAR:DEF 'S21m','S21';:CALC1:PAR:SEL 'S21m';:INIT1:CONT OFF")
vna.write("SENS1:FREQ:STAR 6.8e9;:SENS1:FREQ:STOP 7.7e9;:SENS1:SWE:POIN 4001;:SENS1:BAND 1e3;:SOUR1:POW -20")
vna.write("FORM:DATA REAL,64;:FORM:BORD SWAP")

def sweep(lo=6.8e9, hi=7.7e9, n=4001):
    vna.write("SENS1:FREQ:STAR %g;:SENS1:FREQ:STOP %g;:SENS1:SWE:POIN %d" % (lo, hi, n))
    vna.write("INIT1:IMM"); vna.query("*OPC?")
    d = np.array(vna.query_binary_values("CALC1:DATA? SDATA", datatype="d", is_big_endian=False))
    s = d[0::2] + 1j * d[1::2]
    return np.linspace(lo, hi, n), 20 * np.log10(np.abs(s))

f, m = sweep()
dips = [f[i] / 1e9 for i in range(5, len(m) - 5) if m[i] == m[i - 5:i + 6].min() and m[i] < np.median(m) - 3]
print("dips (GHz):", ["%.4f" % x for x in dips], " baseline %.1f dB" % np.median(m))
gs = rm.open_resource("USB0::0x0B21::0x0039::91T810993::INSTR")
gs.write(":SOUR:FUNC VOLT;:SOUR:RANG 1;:SOUR:LEV 0.5;:OUTP ON")
f2, m2 = sweep(7.0e9, 7.2e9, 2001)
print("with 0.5 V on the flux coil (0.5 mA): tunable dip at %.4f GHz" % (f2[np.argmin(m2)] / 1e9))
gs.write(":SOUR:LEV 0.0")
f3, m3 = sweep(6.84e9, 6.86e9, 1001)
f0 = f3[np.argmin(m3)]
print("flux back to 0: tunable dip at %.5f GHz" % (f0 / 1e9))
q = rm.open_resource("TCPIP0::192.168.1.41::inst0::INSTR")
shifts = []
for fq in np.arange(5.30e9, 5.50e9, 0.004e9):
    q.write("SOUR:FREQ %g;:SOUR:POW -25;:OUTP ON" % fq)
    f4, m4 = sweep(f0 - 4e6, f0 + 4e6, 401)
    shifts.append((f4[np.argmin(m4)] - f0, fq))
q.write("OUTP OFF")
s_min = min(shifts)
print("two-tone: dip moves %.2f MHz with the drive at %.4f GHz (others within %.2f MHz)" % (
    s_min[0] / 1e6, s_min[1] / 1e9, sorted(abs(x[0]) for x in shifts)[len(shifts) // 2] / 1e6))
print(vna.query("SYST:ERR?").strip())
