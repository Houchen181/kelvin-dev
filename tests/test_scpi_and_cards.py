import pytest

from labharness import CardError, builtin_registry, parse_card
from labharness.scpi import Template, is_query, split_compound


def test_template_short_long_and_optional_nodes():
    t = Template("SOURce:VOLTage[:LEVel][:IMMediate][:AMPLitude] {value}")
    assert t.match("SOUR:VOLT 5") == "5"
    assert t.match(":source:voltage:level -2.5") == "-2.5"
    assert t.match("SOUR:VOLT:LEV:IMM:AMPL 1e-3") == "1e-3"
    assert t.match("SOUR:VOLT:RANG 20") is None
    assert t.match("SOUR:CURR 1") is None
    assert t.render("5") == "SOURce:VOLTage 5"


def test_leading_optional_node():
    t = Template("[:SENSe]:FREQuency:STARt {value}")
    assert t.match("FREQ:STAR 1e9") == "1e9"
    assert t.match("SENS:FREQ:STAR 1e9") == "1e9"


def test_prefix_template_matches_a_command_family():
    t = Template("CALibration", prefix=True)
    assert t.match("CAL:ZERO") == ""
    assert t.match("*CAL?") is None
    assert t.match("CALCulate:DATA?") is None


def test_split_compound_follows_scpi_paths():
    assert split_compound("SOUR:VOLT 1;CURR 2;:OUTP ON;*OPC?") == [
        "SOUR:VOLT 1", "SOUR:CURR 2", "OUTP ON", "*OPC?"]
    assert is_query("CALC:DATA? FDATA") and not is_query("SOUR:VOLT 5")


def test_builtin_cards_load():
    ids = {c.id for c in builtin_registry()}
    assert {"virtual/source-meter", "virtual/lock-in", "virtual/temperature-controller", "virtual/magnet-supply",
            "virtual/vna", "virtual/laser", "virtual/motion-stage"} <= ids


def test_card_errors_are_specific():
    bad = {
        "card": 1, "id": "x/bad", "model": "Bad", "kind": "source-meter",
        "parameters": {
            "V": {"kind": "drive", "set": "SOUR:VOLT {value}"},           # no safe, no limits
            "P": {"kind": "drive", "set": "POW {value}", "limits": [0, 1], "safe": 0, "ramp": "gate"},  # no max_rate
            "Q": {"kind": "measure", "get": "Q? {value}"},                # get with a value
        },
    }
    with pytest.raises(CardError) as e:
        parse_card(bad)
    msg = str(e.value)
    assert "parameters.V.safe" in msg and "parameters.V.limits" in msg
    assert "parameters.P.max_rate" in msg and "parameters.Q.get" in msg


def test_a_header_does_not_split_a_word():
    from labharness.scpi import Template
    v = Template(":SOURce:VOLTage[:LEVel][:IMMediate][:AMPLitude] {value}")
    assert v.match(":SOURce:VOLTage:ILIMit 1e-09") is None       # not "VOLT" + "age:ILIMit ..."
    assert v.match(":SOUR:VOLT 1.5") == "1.5" and v.match(":SOURce:VOLTage:LEVel 2") == "2"
    assert Template("SLVL {value}").match("SLVL5") == "5"           # no space is still the same setting


def test_a_value_that_runs_into_its_mnemonic():
    soup = Template("SOUP{value}")                                   # HP's source power: SOUPON, SOUPOFF
    assert soup.match("SOUPON") == "ON" and soup.match("soupoff") == "off" and soup.render("ON") == "SOUPON"
    family = soup.header_prefix(["ON", "OFF"])                       # a raw SOUPON or SOUP1 touches the switch
    assert family.match("SOUPON") == "" and family.match("SOUP1") == "" and family.match("SOUP?") == ""
    assert family.match("SOUPX") is None
    xv = Template("XV{value}")                                       # Thorlabs' piezo controller: XV10
    assert xv.match("XV10") == "10" and xv.header_prefix().match("XV10") == "" and xv.header_prefix().match("XVMAX") is None
