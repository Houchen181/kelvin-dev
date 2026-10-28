"""Cool the PPMS through MultiPyVu with 10 uA DC through the film; read the 2182A as Tc passes."""
import time
import MultiPyVu as mpv
import pyvisa

rm = pyvisa.ResourceManager("@labsim")
src = rm.open_resource("GPIB0::12::INSTR")
nv = rm.open_resource("GPIB0::7::INSTR", timeout=10000)
src.write("SOUR:CURR 1e-5;:SOUR:CURR:COMP 10;:OUTP ON")
with mpv.Client(host="127.0.0.1", port=5000) as c:
    c.set_temperature(10.0, 50.0, c.temperature.approach_mode.fast_settle)
    t0 = time.time()
    while time.time() - t0 < 75:
        T, st = c.get_temperature()
        v = float(nv.query(":READ?"))
        print("t=%4.0f s  T=%7.3f K (%s)  V=%.4e V  R=%.2f ohm" % (time.time() - t0, T, st, v, v / 1e-5))
        if T < 10.2:
            break
        time.sleep(4)
    c.set_temperature(6.0, 20.0, c.temperature.approach_mode.fast_settle)
    for _ in range(12):
        time.sleep(3)
        T, st = c.get_temperature()
        v = float(nv.query(":READ?"))
        print("          T=%7.3f K (%s)  R=%.3f ohm" % (T, st, v / 1e-5))
    c.set_field(50000, 100, c.field.approach_mode.linear)
    time.sleep(8)
    F, sf = c.get_field()
    print("field %.0f Oe (%s), R=%.3f ohm" % (F, sf, float(nv.query(":READ?")) / 1e-5))
src.write("OUTP OFF")
