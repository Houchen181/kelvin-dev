"""A lab's Special Measure read so it can switch to Kelvin (specialmeasure.py): a setup script's rack as Special
Measure would build it, its if-blocks decided by the flags set first (never run: no MATLAB); each instrument's
address, connection settings and start-up commands; each named channel with its limits, ramp rate and multiplier,
and the card parameter its driver's command (or its name) says it is; function channels; and its scans translated
into Kelvin plans by fixed rules. The files here are written for the tests, in Special Measure's form: none of its
code and none of a lab's files are in Kelvin."""
from pathlib import Path

import pytest

from labharness import homelab, load_plan, specialmeasure
from labharness.specialmeasure import expression, translate, zeroes_of

SETUP = r"""
% a rig's setup, as Special Measure labs write them
clear all;
GPIB_BOARD = 'ni';
BOARD_NUM = 1;
LockIn_GPIB = 9;  % the lock-in on the upper rack
Source_GPIB = 21;
DAC_COM = 'COM2';
LockIn_Use = 1;
Source_Use = 0;
DAC_Use = 1;
Gen_Use = 1;
global smdata;
load smdata_empty;
smloadinst('test');
smaddchannel('test', 'CH2', 'count');

if LockIn_Use == 1
    try
        ind = smloadinst('SR830', [], GPIB_BOARD, BOARD_NUM, LockIn_GPIB);
        smopen(ind);
        smdata.inst(ind).name = 'LockIn_Upper';
        smaddchannel('LockIn_Upper', 'X', 'X_A', [-Inf, Inf, Inf, 1]);
        smaddchannel('LockIn_Upper', 'THETA', 'THETA_A');
        smaddchannel('LockIn_Upper', 'FREQ', 'Freq_A', [0, 102000, 10, 1]);
        smaddchannel('LockIn_Upper', 'VREF', 'Vref_A', [0.004, 5, 0.5, 1]);
    catch err
        fprintf(['*ERROR* problem with the lock-in\n' err.message '\n'])
    end
end
if Source_Use == 1
    ind = smloadinst('K2400', [], GPIB_BOARD, BOARD_NUM, Source_GPIB);
    smdata.inst(ind).name = 'Source';
    smaddchannel('Source', 'V', 'Vbg', [-80 80 40 1]);
    fprintf(smdata.inst(ind).data.inst, 'sense:current:protection 1e-6');
end
if DAC_Use == 1
    ind = smloadinst('BabyDAC', [], 'serial', DAC_COM);
    set(smdata.inst(ind).data.inst, 'BaudRate', 9600);
    smdata.inst(ind).name = 'DAC';
    smaddchannel('DAC', 'CH0', 'Vtg', [-10 10 2 1]);
    smaddchannel('DAC', 'CH1', 'Vg2', [-5 5 1 0.1]);
    fprintf(smdata.inst(ind).data.inst, '*RST');
end
if Gen_Use == 1
    global Gen
    ind = smloadinst('Generator3321', [], 'None');
    Gen = gpib('ni', BOARD_NUM, 17);
    fopen(Gen);
    Initial_Gen;
    smdata.inst(ind).name = 'Gen';
    smaddchannel('Gen', 'Frequency ', 'WG_Freq');
end
ind = smloadinst('Function');
smdata.inst(ind).name = 'Gates';
smdata.inst(ind).data.dependences = {'Vtg', 'Vg2'};
smdata.inst(ind).data.formula = {@(n, E) 1.2*n - 8*E, ...
    @(n, E) 0.9*n + 7*E};
smaddchannel('Gates', 'VAR1', 'n');
smaddchannel('Gates', 'VAR2', 'E');
smgui_small
"""

DRIVER = r"""function val = smcSR830(ic, val, rate)
% a lock-in's control function, in Special Measure's form: the channels' commands by number
global smdata;
cmds = {'OUTP 1', 'OUTP 2', 'OUTP 3', 'OUTP 4', 'FREQ', 'SLVL'};
switch ic(3)
    case 1
        fprintf(smdata.inst(ic(1)).data.inst, sprintf('%s %f', cmds{ic(2)}, val));
    case 0
        val = query(smdata.inst(ic(1)).data.inst, sprintf('%s? %s', cmds{ic(2)}(1:4), cmds{ic(2)}(5:end)), '%s\n', '%f');
    otherwise
        error('SR830 driver: Operation not supported');
end
"""

SWITCHED = r"""function val = smcK2400(ic, val, rate)
global smdata;
switch ic(2)
    case 1
        switch ic(3)
            case 0
                val = query(smdata.inst(ic(1)).data.inst, ':read?', '%s\n', '%g');
            case 1
                fprintf(smdata.inst(ic(1)).data.inst, sprintf(':source:volt %g', val));
            otherwise
                error('K2400 driver: Operation not supported');
        end
    case {2, 3}
        val = query(smdata.inst(ic(1)).data.inst, ':sense:current:protection?', '%s\n', '%g');
end
"""


@pytest.fixture
def rig(tmp_path):
    (tmp_path / "setups").mkdir()
    (tmp_path / "setups" / "smsetup_rig.m").write_text(SETUP, encoding="utf-8")
    (tmp_path / "channels").mkdir()
    (tmp_path / "channels" / "smcSR830.m").write_text(DRIVER, encoding="utf-8")
    (tmp_path / "channels" / "smcK2400.m").write_text(SWITCHED, encoding="utf-8")
    (tmp_path / "data").mkdir()
    for n in ("001_gate.mat", "002_gate.mat", "notes.txt"):
        (tmp_path / "data" / n).write_bytes(b"MATLAB 5.0 MAT-file" if n.endswith(".mat") else b"x")
    return tmp_path


def test_the_rack_is_read_as_its_flags_decide_never_run(rig):
    found = specialmeasure.survey(rig, homelab.builtin_registry(), read_mat=False)
    s = found.setup
    assert s.path.replace("\\", "/") == "setups/smsetup_rig.m" and not s.problem
    byname = {ri.name: ri for ri in s.instruments}
    assert set(byname) == {"test", "LockIn_Upper", "Source", "DAC", "Gen", "Gates"}
    lock, src, dac, gen = byname["LockIn_Upper"], byname["Source"], byname["DAC"], byname["Gen"]
    assert lock.used and not src.used and dac.used and gen.used   # Source_Use = 0: listed, not proposed
    assert lock.inst.address == "GPIB1::9::INSTR" and src.inst.address == "GPIB1::21::INSTR"
    assert dac.inst.address == "serial://COM2" and dac.inst.settings == {"baud_rate": 9600}
    assert [c.text for c in dac.inst.commands] == ["*RST"]
    assert gen.inst.address == "GPIB1::17::INSTR" and gen.helpers == ["Initial_Gen"]   # the block opens it itself
    freq = next(c for c in lock.channels if c.name == "Freq_A")
    assert (freq.low, freq.high, freq.rate, freq.factor) == (0, 102000, 10, 1)
    g2 = next(c for c in dac.channels if c.name == "Vg2")
    assert (g2.low, g2.high, g2.rate, g2.factor) == (-5, 5, 1, 0.1)
    assert byname["Gates"].dependences == ["Vtg", "Vg2"] and byname["Gates"].formulas[1] == "@(n, E) 0.9*n + 7*E"
    assert found.runs == {"data": 2} and set(found.drivers) == {"smcsr830", "smck2400"}
    assert [ri.name for ri in specialmeasure._hardware(s)][-1] == "Source"   # the ones it uses first


def test_each_channel_is_its_cards_parameter_by_its_drivers_command_else_by_its_name(rig):
    found = specialmeasure.survey(rig, homelab.builtin_registry(), read_mat=False)
    byname = {ri.name: ri for ri in found.setup.instruments}
    lock, src = byname["LockIn_Upper"], byname["Source"]
    assert lock.inst.card["id"] == "srs/sr830" and src.inst.card["id"] == "keithley/2400"   # K2400 is a 2400
    params = {c.name: c.param for c in lock.channels}
    assert params == {"X_A": "X", "THETA_A": "theta", "Freq_A": "frequency", "Vref_A": "amplitude"}
    assert specialmeasure.driver_commands(DRIVER)[5] == ["FREQ"]
    assert specialmeasure.driver_commands(SWITCHED) == {1: [":read?", ":source:volt %g"],
                                                         2: [":sense:current:protection?"],
                                                         3: [":sense:current:protection?"]}
    text = found.text()
    assert "X_A = LockIn_Upper.X → X" in text and "the script doesn't load it" in text and "MATLAB" not in text[:0]


def test_names_come_from_the_racks_channels_but_never_take_an_instruments_or_kelvins_own():
    found = specialmeasure.Setup("s.m", 0.0)
    ri = specialmeasure.RackInstrument("SR830", "LockIn", specialmeasure.Instrument("", [], "", []))
    ri.channels = [specialmeasure.Channel("X_A", "X", param="X"), specialmeasure.Channel("t", "T", param="T"),
                   specialmeasure.Channel("lockin", "Y", param="Y")]
    found.instruments = [ri]
    assert specialmeasure.names_of(found, {"LockIn": "lockin"}) == {"X_A": "lockin.X"}


SCAN = {"name": "gate map", "comments": ["T=4K", "Vbias=100uV"], "saveloop": [2, 1],
        "loops": [{"npoints": 5, "rng": [0, 1], "setchan": ["Vtg", "Vg2"], "setchanranges": [[-1, 1], [0.5, -0.5]],
                   "getchan": ["X_A", "time"], "waittime": 0.2, "startwait": 1, "ramptime": []},
                  {"npoints": 3, "rng": [0, 1], "setchan": ["Freq_A"], "setchanranges": [[100, 300]],
                   "getchan": ["Vref_A"], "waittime": 0, "startwait": 0}],
        "consts": [{"setchan": "Vref_A", "val": 0.1, "set": 1}, {"setchan": "Freq_A", "val": 7, "set": 0}],
        "escapefn": {"fn": "sf%0@(x)smset({'Vtg','Vg2'},[0,0])", "args": []}}
KEYS = {"X_A": ("lockin.X", 1.0), "Vref_A": ("lockin.amplitude", 1.0), "Freq_A": ("lockin.frequency", 1.0),
        "Vtg": ("dac.CH0", 1.0), "Vg2": ("dac.CH1", 0.1)}


def test_a_scan_becomes_a_plan_its_loops_outer_first_and_its_channels_together():
    t = translate(SCAN, KEYS)
    assert not t["gaps"], t["gaps"]
    plan = t["plan"]
    assert plan["description"] == "From Special Measure"   # its comments stay in the file
    assert plan["setup"] == {"lockin.amplitude": 0.1}   # set: 1 sets it; set: 0 is recorded (a run's snapshot)
    assert list(plan["sweep"]) == ["lockin.frequency", "dac.CH0"]
    outer, inner = plan["sweep"]["lockin.frequency"], plan["sweep"]["dac.CH0"]
    assert outer == {"from": 100, "to": 300, "points": 3, "measure": ["lockin.amplitude"]}
    assert inner == {"from": -1, "to": 1, "points": 5, "with": {"dac.CH1": {"from": 0.05, "to": -0.05}},
                     "settle": 0.2, "first_settle": 1.2}   # the multiplier: Kelvin's value is sm's × 0.1
    assert plan["measure"] == ["lockin.X"]   # sm's time channel: every Kelvin point has t
    assert plan["after"] == plan["on_abort"] == {"dac.CH0": 0, "dac.CH1": 0}
    p = load_plan(t["text"])
    assert p.points == 15 and p.axes[1].with_[0].values == pytest.approx([0.05, 0.025, 0, -0.025, -0.05])


def test_function_channels_become_variables_and_what_their_formulas_set():
    functions = {"n": {"variables": ["n", "E"], "sets": {"Vtg": "@(n, E) 1.2*n - 8*E", "Vg2": "@(n, E) 0.9*n + 7*E"}},
                 "E": {"variables": ["n", "E"], "sets": {"Vtg": "@(n, E) 1.2*n - 8*E", "Vg2": "@(n, E) 0.9*n + 7*E"}}}
    scan = {"name": "nE", "loops": [{"npoints": 3, "rng": [0, 1], "setchan": ["n"], "setchanranges": [[0, 2]],
                                     "getchan": ["X_A"]},
                                    {"npoints": 2, "rng": [0, 1], "setchan": ["E"], "setchanranges": [[-0.1, 0.1]]}]}
    t = translate(scan, KEYS, functions=functions)
    assert t["plan"]["sweep"] == {"E": {"from": -0.1, "to": 0.1, "points": 2},
                                  "n": {"from": 0, "to": 2, "points": 3}}
    assert t["plan"]["sets"] == {"dac.CH0": "1.2*n - 8*E", "dac.CH1": "(0.9*n + 7*E) * 0.1"}
    assert load_plan(t["text"]).sets[1].values[0] == pytest.approx(-0.07)
    one = dict(scan, loops=scan["loops"][:1])   # E as it was before the scan: unknown, unless a constant says
    assert translate(one, KEYS, functions=functions)["plan"] is None
    t = translate(dict(one, consts=[{"setchan": "E", "val": 0.05, "set": 1}]), KEYS, functions=functions)
    assert t["plan"]["sets"]["dac.CH0"] == "1.2*n - 8*(0.05)" and "setup" not in t["plan"]


def test_what_cant_be_translated_is_said_and_nothing_is_guessed():
    bad = dict(SCAN, loops=[dict(SCAN["loops"][0], trafofn=[{"fn": "@(x, y) x(1)^2"}])])
    t = translate(bad, KEYS)
    assert t["plan"] is None and "trafofn" in t["gaps"][0]
    t = translate(dict(SCAN, loops=[dict(SCAN["loops"][0], ramptime=[-0.1])]), KEYS)
    assert t["plan"] is None and "ramps its channels on the instrument" in t["gaps"][0]
    t = translate(dict(SCAN, loops=[dict(SCAN["loops"][0], getchan=["B"])]), KEYS)
    assert t["plan"] is None and "B is not channels of the rack read" in t["gaps"][0] or "B is not" in t["gaps"][0]
    t = translate(dict(SCAN, escapefn={"fn": "@(x) myCleanup()"}), KEYS)
    assert t["plan"] is not None and "escape function" in t["gaps"][0] and "after" not in t["plan"]
    count = {"loops": [{"npoints": 10, "setchan": ["count"], "getchan": ["X_A"], "waittime": 0.5},
                       {"npoints": 3, "setchan": ["Vtg"], "setchanranges": [[0, 1]]}]}
    t = translate(count, KEYS)   # sm's counter innermost: a time series
    assert t["plan"]["time"] == {"every": 0.5, "points": 10} and t["plan"]["measure"] == ["lockin.X"]
    t = translate({"loops": [{"npoints": 3, "setchan": ["Vtg"], "setchanranges": [[0, 1]], "getchan": ["X_A"]},
                             {"npoints": 4, "setchan": ["count"]}]}, KEYS)
    assert t["plan"]["repeat"] == 4   # outermost: repeat


def test_formulas_and_escape_functions_are_read_as_text():
    assert expression("@(n,E) 1.18.*n - 8.4*E + 2.4", ["n", "E"]) == "1.18*n - 8.4*E + 2.4"
    assert expression("@(x) mapto(x, -16, 0, 0, -16)", ["hysVbg"]) == \
        "where((hysVbg) < 0.5, (-16) + ((0) - (-16)) * 2 * (hysVbg), (0) + ((-16) - (0)) * (2 * (hysVbg) - 1))"
    assert expression("@(x) x^2 + offset", ["v"]) is None   # a variable of the setup's workspace: not guessed
    assert expression("@(x) myfun(x)", ["v"]) is None
    assert zeroes_of("sf%0@(x)smset(['Vtg';'Vbg'],[0,0])") == {"Vtg": 0.0, "Vbg": 0.0}
    assert zeroes_of("@(x)smset('Vbg',0)") == {"Vbg": 0.0}
    assert zeroes_of("@(x) disp('done')") is None


def test_a_channel_set_through_its_set_point_is_swept_as_the_set_point_and_read_as_itself():
    keys = dict(KEYS, T=("optc.T", 1.0, "optc.T_set"))
    scan = {"name": "cooldown", "loops": [{"npoints": 3, "setchan": ["T"], "setchanranges": [[10, 2]],
                                           "getchan": ["T", "X_A"]}],
            "consts": [{"setchan": "T", "val": 10, "set": 1}]}
    plan = translate(scan, keys)["plan"]
    assert list(plan["sweep"]) == ["optc.T_set"] and plan["measure"] == ["optc.T", "lockin.X"]
    assert plan["setup"] == {"optc.T_set": 10}


def test_constants_on_a_function_no_loop_sweeps_set_what_its_formulas_give():
    fn = {"variables": ["n", "E"], "sets": {"Vtg": "@(n, E) 1.2*n - 8*E", "Vg2": "@(n, E) 0.9*n + 7*E"},
          "function": "Gates"}
    functions = {"n": fn, "E": fn}
    scan = {"name": "freq", "loops": [{"npoints": 3, "setchan": ["Freq_A"], "setchanranges": [[100, 300]],
                                       "getchan": ["X_A"]}],
            "consts": [{"setchan": "n", "val": 1, "set": 1}, {"setchan": "E", "val": 0.05, "set": 1}]}
    t = translate(scan, KEYS, functions=functions)
    assert t["plan"]["setup"] == {"dac.CH0": 0.8, "dac.CH1": 0.125} and not t["gaps"]   # Vg2's multiplier 0.1
    t = translate(dict(scan, consts=scan["consts"][:1]), KEYS, functions=functions)   # E as it was before
    assert t["plan"] is None and "E, which keeps the value it had before the scan" in t["gaps"][0]


def test_a_functions_variables_follow_its_channel_numbers():
    ri = specialmeasure.RackInstrument("Function", "F", specialmeasure.Instrument("", [], "", []))
    ri.channels = [specialmeasure.Channel("E", "VAR2"), specialmeasure.Channel("n", "VAR1")]
    assert specialmeasure.function_variables(ri) == ["n", "E"]   # as sm's Function gives its formulas their values
    ri.channels = [specialmeasure.Channel("a", "VAR10"), specialmeasure.Channel("b", "VAR2"),
                   specialmeasure.Channel("c", "VAR1")]
    v = specialmeasure.function_variables(ri)
    assert v[:3] == ["c", "b", "VAR3"] and v[9] == "a" and len(v) == 10   # VAR10 after VAR2
    ri.order = ["VAR1", "VAR2", "VAR10"]   # its sminst file's order
    assert specialmeasure.function_variables(ri) == ["c", "b", "a"]


def test_what_would_translate_differently_is_said_or_refused():
    from labharness.yamlio import dump_yaml
    keys = dict(KEYS, I=("smu.I", 1e-7))
    t = translate({"loops": [{"npoints": 3, "setchan": ["Vtg"], "setchanranges": [[0, 1]], "getchan": ["I"]}]}, keys)
    assert t["plan"] is not None and "I is recorded as the instrument reads it" in t["gaps"][0]
    two = {"loops": [{"npoints": 3, "setchan": ["Vtg"], "setchanranges": [[0, 1]]},
                     {"npoints": 2, "setchan": ["Vtg"], "setchanranges": [[0, 1]]}]}
    assert "two loops step Vtg" in translate(two, KEYS)["gaps"][0]
    for npoints in (float("nan"), float("inf"), 2.5):
        bad = {"loops": [{"npoints": npoints, "setchan": ["Vtg"], "setchanranges": [[0, 1]]}]}
        assert "isn't a number of points" in translate(bad, KEYS)["gaps"][0]
    own = {"loops": [{"npoints": 3, "setchan": ["Vtg"], "setchanranges": [[0, 1]], "getchan": ["X_A"]},
                     {"npoints": 2, "setchan": ["Freq_A"], "setchanranges": [[100, 200]],
                      "getchan": ["Freq_A", "Vref_A"]}]}
    plan = translate(own, KEYS)["plan"]
    assert plan["sweep"]["lockin.frequency"]["measure"] == ["lockin.amplitude"]   # its own setting: its column
    assert load_plan(dump_yaml(plan)).points == 6
    rep = {"loops": [{"npoints": 3, "setchan": ["Vtg"], "setchanranges": [[0, 1]]},
                     {"npoints": 4, "setchan": ["count"], "getchan": ["X_A"]}]}
    t = translate(rep, KEYS)
    assert t["plan"]["repeat"] == 4 and "once per repeat" in t["gaps"][0]
    assert zeroes_of("@(x) smset({'A','B'}, -[1 2])") is None and zeroes_of("@(x) smset({'A','B'}, [-(1) 2])") is None
    assert zeroes_of("@(x) smset({'A','B'}, [-1 2.5])") == {"A": -1.0, "B": 2.5}
    assert expression("@(a,b) a^b^2", ["x", "y"]) is None   # MATLAB groups it (a^b)^2, Python a^(b^2)
    assert expression("@(a,b) (a^2)^3 + b^2", ["x", "y"]) == "(x**2)**3 + y**2"


def test_statements_on_one_line_are_read_as_matlab_runs_them(tmp_path):
    (tmp_path / "smsetup.m").write_text("""
Use = 1;
Off = 0;
if Off == 1, smloadinst('SP2150', [], 'serial', 'COM5'), end
if Off == 1
    if Use == 0
        smloadinst('K2400', [], 'ni', 0, 24);
    else if Use == 1
        smloadinst('K2400', [], 'ni', 0, 25);
        end
    end
    smloadinst('SR830', [], 'ni', 0, 8);
end
smloadinst('BabyDAC', [], 'serial', 'COM2');
smaddchannel('BabyDAC', 'CH0', 'Vg');
""", encoding="utf-8")
    found = specialmeasure.survey(tmp_path, homelab.builtin_registry(), read_mat=False)
    used = {ri.model: ri.used for ri in found.setup.instruments}
    assert used == {"SP2150": False, "K2400": False, "SR830": False, "BabyDAC": True}, used
    driver = """function val = smcX(ic, val, rate)
switch ic(2)
    case 1
        if val > 1, fprintf(inst, 'SOUR1:VOLT %f', val); end
    case 2
        fprintf(inst, 'FREQ %f', val);
    case 3
        fprintf(inst, '*RST');
end
"""
    assert specialmeasure.driver_commands(driver) == {1: ["SOUR1:VOLT %f"], 2: ["FREQ %f"], 3: ["*RST"]}
