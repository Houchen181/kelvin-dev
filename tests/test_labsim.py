"""labsim, the simulated lab Kelvin is tested against (tools/labsim): instruments that behave as their manuals say,
and a Kelvin lab connected to a running bench through the reusable fixture in labsim_bench.py."""
import pytest

from labsim_bench import advance, labsim_offline  # noqa: F401


def test_a_ppms_settles_on_its_set_point(labsim_offline):
    """F2 l03/l09: the PPMS stopped short of every set point (7.2 K for 10 K, below the film's 8.6 K Tc, and 273 K
    for 300 K) because the cryostat's own cooling pulled against it, so it never said Stable and waits ran 4 h."""
    bench = labsim_offline("ppms")
    ppms = bench.instruments["ppms"]

    def settle(kelvin, rate, seconds):
        ppms.sdk_call("set_temperature", [kelvin, rate, "fast_settle"], {}, 0.0)
        advance(bench, seconds)
        (t, status), _ = ppms.sdk_call("get_temperature", [], {}, 0.0)
        return t, status
    t, status = settle(10.0, 50.0, 600)   # from 300 K
    assert abs(t - 10.0) < 0.02 and status == "Stable"
    (r, _), _ = ppms.sdk_call("get_resistance", [1], {}, 0.0)
    assert r > 400   # the film is normal above Tc
    t, status = settle(2.0, 10.0, 240)
    assert abs(t - 2.0) < 0.02 and status == "Stable"
    (r, _), _ = ppms.sdk_call("get_resistance", [1], {}, 0.0)
    assert r < 1   # and superconducting below it
    t, status = settle(300.0, 50.0, 900)
    assert abs(t - 300.0) < 0.05 and status == "Stable"
    ppms.power(False)   # switched off, the PPMS holds nothing: the cryostat drifts on its own again
    assert bench.world.cryo.owner is None
    ppms.power(True)
    assert bench.world.cryo.owner == "ppms"


def test_a_ramp_follows_the_rate_it_was_given(labsim_offline):
    bench = labsim_offline("ppms")
    ppms = bench.instruments["ppms"]
    ppms.sdk_call("set_temperature", [200.0, 10.0, "fast_settle"], {}, 0.0)
    advance(bench, 300)   # 10 K/min for 5 min: 50 K down, a little behind
    (t, status), _ = ppms.sdk_call("get_temperature", [], {}, 0.0)
    assert 250.0 < t < 252.5 and status == "Tracking"


def test_smc100_says_where_a_move_is_going(labsim_offline):
    """F2 l06: Kelvin's SMC100 card reads the target with TH (the manual's "get set-point position"); labsim
    answered nothing, so the read timed out."""
    bench = labsim_offline("pumpprobe")
    stage = bench.instruments["delay"]
    assert stage.handle("1TH") == "1TH0.000000"
    stage.handle("1OR")   # home it first: it refuses moves until referenced
    advance(bench, 1)
    stage.handle("1PA20")
    advance(bench, 2)     # 2.5 mm/s: on its way
    assert stage.handle("1TH") == "1TH20.000000" and float(stage.handle("1TP")[3:]) < 20
    advance(bench, 8)
    assert stage.handle("1TP") == "1TP20.000000" and stage.handle("1TS").endswith("33")
    stage.handle("1PA5")
    advance(bench, 1)
    stage.handle("1ST")   # stopped on the way: the set point is where it stopped
    assert stage.handle("1TH") == stage.handle("1TP").replace("TP", "TH")


def test_sr830_keeps_its_reference_trigger(labsim_offline):
    """RSLP sets how an external reference triggers (0 sine, 1 TTL rising, 2 TTL falling); labsim forgot it."""
    from labsim.instrument import CommandError
    lockin = labsim_offline("transport").instruments["li_xx"]
    assert lockin.handle("RSLP?") == "0"
    lockin.handle("RSLP 1")
    assert lockin.handle("RSLP?") == "1"
    lockin.handle("RSLP 2;FMOD 0")
    assert lockin.handle("RSLP?;FMOD?") == "2;0"
    with pytest.raises(CommandError):
        lockin.handle("RSLP 3")
    assert lockin.handle("RSLP?") == "2"
    lockin.handle("*RST")
    assert lockin.handle("RSLP?") == "0"

