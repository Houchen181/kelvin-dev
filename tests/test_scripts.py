"""A lab's own scripts, read (never run) for how it works: instruments and their addresses, connection settings,
the commands each takes with the values used, loops, waits and saves; the card that fits each one, its commands'
parameters on it, and limits from the values the scripts use."""
import json

import pytest

from labharness import builtin_registry
from labharness.discovery import instrument_block, set_limits
from labharness.scripts import normalize, survey
from labharness.yamlio import load_yaml

IV = '''import time
import numpy as np
import pyvisa

KEITHLEY = "GPIB0::24::INSTR"   # Keithley 2400 SourceMeter on the sample
V_MAX = 1.0

def measure(smu):
    return float(smu.query(":MEAS:CURR?"))

rm = pyvisa.ResourceManager()
smu = rm.open_resource(KEITHLEY)
smu.read_termination = "\\n"
smu.timeout = 5000
smu.write(":SENS:CURR:PROT 1e-6")
smu.write(":OUTP ON")
data = []
for v in np.linspace(-V_MAX, V_MAX, 21):
    smu.write(f":SOUR:VOLT {v}")
    time.sleep(0.1)
    data.append((v, measure(smu)))
smu.write(":OUTP OFF")
np.savetxt("iv.csv", data)
'''
LOCKIN = '''from qcodes.instrument_drivers.stanford_research.SR830 import SR830
lockin = SR830("lockin", "GPIB0::8::INSTR")
lockin.amplitude(0.1)
x = lockin.X()
'''
SERIAL = '''import serial
import socket
tc = serial.Serial("COM5", 57600, timeout=1)   # Lake Shore 336
tc.write(b"SETP 1,4.2\\r\\n")
s = socket.socket()
s.connect(("192.168.1.9", 5025))
s.sendall(b"OUTP ON\\n")
'''
PNA = '''% Keysight N5227B PNA: S21 at a few powers
visaResource = "TCPIP0::192.168.1.20::hislip0::INSTR";
vna = visadev(visaResource);
vna.Timeout = 30;
writeline(vna, "SENS1:FREQ:STAR 3.99e9");
writeline(vna, "SENS1:SWE:POIN 401");
for p = -30:5:-10
    writeline(vna, sprintf("SOUR1:POW1 %g", p));  % the power
    writeread(vna, "SENS1:SWE:MODE SING;*OPC?");
    data = writeread(vna, "CALC1:MEAS1:DATA:FDATA?");
    pause(1);
end
writematrix(data, "s21.csv");
'''
HP = '''% HP 8719ES network analyzer
na = gpib('ni', 0, 16);
fopen(na);
fprintf(na, 'STAR 3.99E9');
fprintf(na, 'POIN 201');
fprintf(na, 'S21');
fprintf(na, 'LOGM');
fprintf(na, 'OPC?;SING');
fprintf(na, 'OUTPFORM');
x = fscanf(na);
'''
SOURCE = '''src = serialport("COM3", 9600);
configureTerminator(src, "CR/LF");
for v = linspace(0, 2, 5)
    writeline(src, ['VOLT ', num2str(v)]);
end
'''
NOTEBOOK = {"cells": [
    {"cell_type": "markdown", "source": ["# gate sweep"]},
    {"cell_type": "code", "source": ["%matplotlib inline\n", "import pyvisa\n",
                                     "gate = pyvisa.ResourceManager().open_resource('GPIB0::25::INSTR')  # Keithley 2400\n"]},
    {"cell_type": "code", "source": ["for vg in range(0, 30, 5):\n", "    gate.write('SOUR:VOLT %g' % vg)\n"]}],
    "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


@pytest.fixture
def scripts(tmp_path):
    d = tmp_path / "scripts"
    (d / "python_libs" / "pkg").mkdir(parents=True)
    (d / "python_libs" / "pkg" / "vendored.py").write_text('x = rm.open_resource("GPIB0::99::INSTR")\nx.write("*RST")\n')
    (d / "iv_sweep.py").write_text(IV, encoding="utf-8")
    (d / "lockin_qcodes.py").write_text(LOCKIN, encoding="utf-8")
    (d / "temperature.py").write_text(SERIAL, encoding="utf-8")
    (d / "vna_power.m").write_text(PNA, encoding="utf-8")
    (d / "hp8719_s21.m").write_text(HP, encoding="utf-8")
    (d / "source.m").write_text(SOURCE, encoding="utf-8")
    (d / "gate.ipynb").write_text(json.dumps(NOTEBOOK), encoding="utf-8")
    (d / "plot_only.py").write_text("import matplotlib.pyplot as plt\nplt.plot([1, 2])\n", encoding="utf-8")
    (d / "old.py").write_text("print 'python 2'\n", encoding="utf-8")
    return d


def by_address(found):
    return {i.address: i for i in found.instruments}


def test_the_survey_finds_each_instrument_and_how_it_is_driven(scripts):
    found = survey(scripts, builtin_registry())
    inst = by_address(found)
    assert "GPIB0::99::INSTR" not in inst                     # a library folder isn't the lab's own scripts
    smu = inst["GPIB0::24::INSTR"]
    assert smu.names == ["smu"] and smu.library == "pyvisa" and "2400" in smu.hints
    assert smu.settings == {"read_termination": "\n", "timeout_ms": 5000}
    texts = [c.text for c in smu.commands]
    assert ":SOUR:VOLT {v}" in texts and ":MEAS:CURR?" in texts   # also what a function sends through its argument
    sweep = next(c for c in smu.commands if c.text == ":SOUR:VOLT {v}")
    assert sweep.values["v"][0] == -1 and sweep.values["v"][-1] == 1 and len(sweep.values["v"]) == 21
    assert smu.card["id"] == "keithley/2400" and smu.card["limits"]["V"] == (-1.0, 1.0)
    assert {m["command"]: m["param"] for m in smu.card["mapped"]}[":SOUR:VOLT {v}"] == "V"

    lockin = inst["GPIB0::8::INSTR"]
    assert lockin.library == "driver SR830" and lockin.card["id"] == "srs/sr830" and "X()" in lockin.calls
    tc = inst["serial://COM5"]
    assert tc.settings["baud_rate"] == 57600 and tc.card["id"] == "lakeshore/336"
    assert tc.card["limits"]["setpoint"] == (0.0, 4.2)
    assert [c.text for c in inst["TCPIP0::192.168.1.9::5025::SOCKET"].commands] == ["OUTP ON"]

    vna = inst["TCPIP0::192.168.1.20::hislip0::INSTR"]
    assert vna.card["id"] == "keysight/pna" and "N5227B" in vna.card["why"]   # named where its address is set
    assert vna.settings == {"timeout_ms": 30000} and vna.card["limits"]["power"] == (-90.0, -10.0)
    na = inst["GPIB0::16::INSTR"]
    assert na.card["id"] == "hp/8720" and "6 of 6 commands" in na.card["why"]
    parts = {m["command"]: (m["param"], m["part"]) for m in na.card["mapped"]}
    assert parts["OUTPFORM"] == ("S21", False) and parts["LOGM"] == ("S21", True) and parts["STAR 3.99E9"][0] == "f_start"
    src = inst["serial://COM3"]
    assert src.settings == {"baud_rate": 9600, "read_termination": "\r\n", "write_termination": "\r\n"}
    assert src.commands[0].text == "VOLT {v}" and src.commands[0].values["v"] == [0.0, 0.5, 1.0, 1.5, 2.0]
    gate = inst["GPIB0::25::INSTR"]
    assert gate.commands[0].text == "SOUR:VOLT {vg}" and gate.card["limits"]["V"] == (0.0, 25.0)


def test_the_report_outlines_each_script_and_says_what_it_could_not_read(scripts):
    found = survey(scripts, builtin_registry())
    text = found.text()
    assert "only read, never run" in text and "1 other file hold" in text.replace("files hold", "file hold")
    assert "old.py" in text and "does not parse as Python 3" in text
    iv = next(s for s in found.scripts if s.path == "iv_sweep.py")
    assert "for v in np.linspace(-V_MAX, V_MAX, 21) (-1 … 1 (21 values)):" in iv.outline
    assert "  smu ← :SOUR:VOLT {v}   [sets smu.V]" in iv.outline and "  wait 0.1 s" in iv.outline
    assert "save iv.csv" in iv.outline
    pna = next(s for s in found.scripts if s.path == "vna_power.m")
    assert "  vna ? CALC1:MEAS1:DATA:FDATA?   [reads vna.S21]" in pna.outline and "save s21.csv" in pna.outline
    view = found.view()
    assert {i["name"] for i in view["instruments"]} >= {"smu", "lockin", "tc", "vna", "na", "gate", "src"}


def test_reading_never_runs_a_script(tmp_path):
    keep = tmp_path / "keep.txt"
    keep.write_text("still here")
    (tmp_path / "danger.py").write_text("import os\nos.remove(%r)\nraise SystemExit(1)\n" % str(keep))
    (tmp_path / "danger.m").write_text("delete('%s');\nexit;\n" % keep)
    survey(tmp_path, builtin_registry())
    assert keep.read_text() == "still here"


def test_a_maker_alone_names_no_card(tmp_path):
    (tmp_path / "x.py").write_text('# attocube cryostat\nc = rm.open_resource("GPIB0::3::INSTR")\nc.write("FOO 1")\n')
    found = survey(tmp_path, builtin_registry())
    assert found.instruments[0].card is None   # neither a model named nor its commands on a card


def test_addresses_as_kelvin_writes_them():
    assert normalize("GPIB::24") == "GPIB0::24::INSTR" and normalize("COM3") == "serial://COM3"
    assert normalize("TCPIP::10.0.0.5::INSTR") == "TCPIP0::10.0.0.5::inst0::INSTR"
    assert normalize("TCPIP0::10.0.0.5::5025::SOCKET") == "TCPIP0::10.0.0.5::5025::SOCKET"


LAB = '''# my lab
lab: Bench
instruments:
  smu:
    card: keithley/2400        # Keithley 2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-210, 210], I: [-1.05, 1.05] }   # vendor limits
    added_by: kelvin   # each output change asks you
  lockin: { card: srs/sr830, address: "GPIB0::8::INSTR" }
  tc:
    card: lakeshore/336
    address: "serial://COM5"
    limits:
      setpoint: [0, 300]
    connection: { baud_rate: 57600 }
'''


def test_one_instruments_limits_change_and_the_rest_of_the_config_stays():
    text = set_limits(LAB, "smu", {"V": [-1, 1]}, reviewed=True)
    text = set_limits(text, "lockin", {"amplitude": [0.004, 1]})
    text = set_limits(set_limits(text, "tc", {"setpoint": [0, 10]}), "tc", {"setpoint": None})
    assert text.startswith("# my lab\n") and "# Keithley 2400" in text and "added_by" not in text
    data = load_yaml(text)["instruments"]
    assert data["smu"]["limits"] == {"V": [-1, 1], "I": [-1.05, 1.05]}
    assert data["lockin"]["limits"] == {"amplitude": [0.004, 1.0]} and data["lockin"]["card"] == "srs/sr830"
    assert "limits" not in data["tc"] and data["tc"]["connection"] == {"baud_rate": 57600}
    with pytest.raises(KeyError):
        set_limits(LAB, "nope", {"V": [0, 1]})


def test_an_instrument_from_the_scripts_keeps_their_limits_and_settings():
    block = "\n".join(instrument_block({"name": "smu", "card": "keithley/2400", "address": "GPIB0::24::INSTR",
                                        "limits": {"V": [-1, 1]}, "connection": {"timeout_ms": 5000}},
                                       builtin_registry()))
    assert "V: [-1, 1]" in block and "from your scripts" in block and 'connection: { timeout_ms: 5000 }' in block
    entry = load_yaml("instruments:\n" + block)["instruments"]["smu"]
    assert entry["limits"]["V"] == [-1, 1] and "compliance_I" in entry["limits"]   # other drives: the card's


def test_matlab_loops_in_their_other_forms(tmp_path):
    (tmp_path / "loops.m").write_text(
        's = serialport("COM3", 9600);\nfor (v = linspace(0, 2, 5))\n    writeline(s, sprintf("VOLT %g", v));\nend\n'
        'format = 3;\nfor k = 1:3, writeline(s, sprintf("CURR %g", k)); end\n'
        'for(w=1:2)\n    writeline(s, sprintf("POW %g", w));\nend\n', encoding="utf-8")
    (src,) = survey(tmp_path).instruments
    assert {c.text: c.values for c in src.commands} == {"VOLT {v}": {"v": [0.0, 0.5, 1.0, 1.5, 2.0]},
                                                        "CURR {k}": {"k": [1.0, 2.0, 3.0]}, "POW {w}": {"w": [1.0, 2.0]}}
