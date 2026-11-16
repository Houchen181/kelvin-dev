"""Reports and slides show rounded numbers (reports/numbers.py); the run keeps every digit (the user, 2026-11-15)."""
import pytest

from labharness.reports.chart import scale_of
from labharness.reports.numbers import prose, result, shown
from test_reports import pdf_strings


@pytest.mark.parametrize("value, err, unit, want", [
    (1.500058, 0.000043, "V", "1.500 V"),                 # the ± is below the last digit shown: it goes
    (1.60035, 0.00024, "V", "1.600 V"),
    (43.6067e-6, 0.0032e-6, "V", "43.61 µV"),
    (4.8423e-6, 0.0031e-6, "V", "4.842 ± 0.003 µV"),      # one figure of uncertainty, the value to the same place
    (1.010e-12, 0.024e-12, "A/V", "1.01 ± 0.02 pA/V"),    # a prefix on a compound unit, never e-12
    (990e9, 23e9, "Ω", "990 ± 20 GΩ"),
    (14.50182e-6, 0.00062e-6, "V", "14.50 µV"),
    (0.15, 0.014, "V", "150 ± 14 mV"),                    # two figures when the uncertainty starts with 1
    (0.0004, 0.004, "V", "0 ± 4 mV"),                     # the prefix that suits the larger of the two
    (524.9, None, "", "525"),                             # no uncertainty: three figures
    (3.965e-9, None, "V", "3.97 nV"),
    (12345.6, None, "", "12346"),                         # whole numbers stay whole
    (1.2e-7, 3e-9, "", "(1.20 ± 0.03)e-7"),               # no prefix for this unit: a power of ten
    (None, None, "V", "-"),
    (0.996e-12, 0.027e-12, "A/V", "1.00 ± 0.03 pA/V"),    # rounding up into the next prefix: not 1000 ± 30 fA/V
    (1004e9, 27e9, "Ω", "1.00 ± 0.03 TΩ"),
    (999.96e-6, None, "V", "1.00 mV"),
])
def test_values_are_shown_rounded(value, err, unit, want):
    assert shown(value, err, unit) == want


def test_a_line_comparing_values_keeps_one_unit():
    scale = (1e-12, "pA/V")
    assert shown(9.96e-13, 2.7e-14, "A/V", scale) == "1.00 ± 0.03 pA/V"     # not 1000 ± 30 fA/V
    assert shown(-1.4e-14, 3.6e-14, "A/V", scale) == "-0.01 ± 0.04 pA/V"


def test_sentences_round_only_their_uncertain_numbers():
    assert prose("lockin.X has a peak at smu.V = 1.500058 ± 0.000043 V (FWHM 1.60035 ± 0.00024 V; lorentzian fit, "
                 "R² 1.000).") == "lockin.X has a peak at smu.V = 1.500 V (FWHM 1.600 V; lorentzian fit, R² 1.000)."
    assert prose("smu.I is linear in smu.V: slope (1.010 ± 0.024)e-12 A/V (R² 0.9987)") == \
        "smu.I is linear in smu.V: slope 1.01 ± 0.02 pA/V (R² 0.9987)"
    kept = "flat at 14.50182 ± 0.00062 µV (fluctuations 3.965 nV rms); lock-in at 17.78 Hz, 41 of 41 points"
    assert prose(kept) == "flat at 14.50 µV (fluctuations 3.965 nV rms); lock-in at 17.78 Hz, 41 of 41 points"


def test_sentences_show_noise_and_prefixes_as_the_tables_do():
    """The noise a sentence wrote to four figures is shown from its every digit, as the table shows it (never 3.97
    from the 3.965 written); a value written with a prefix gets the one a table gives it."""
    noise = {"name": "noise (rms)", "num": 3.96495e-9, "err": None, "unit": "V"}
    kept = "lockin.X does not depend on stage.position: 14.50 µV (fluctuations 3.965 nV rms)"
    assert prose(kept, [noise]) == "lockin.X does not depend on stage.position: 14.50 µV (fluctuations 3.96 nV rms)"
    assert prose("laser.P_detected shows no signal above the noise (1.263 µW rms)",
                 [{"name": "noise (rms)", "num": 1.26295e-6, "unit": "W"}]) == \
        "laser.P_detected shows no signal above the noise (1.26 µW rms)"
    assert prose("smu.I is linear in smu.V: R = 1000 ± 30 GΩ") == "smu.I is linear in smu.V: R = 1.00 ± 0.03 TΩ"
    assert prose("width 12.5 ± 0.43 mm") == "width 12.5 ± 0.4 mm"     # millimetres: no prefix to take off


def test_results_use_their_numbers_and_older_runs_their_text():
    assert result({"name": "center", "value": "1.500058 ± 0.000043 V", "num": 1.500058, "err": 4.3e-5,
                   "unit": "V"}) == "1.500 V"
    assert result({"name": "width", "value": "12.5 ± 0.43 mV"}) == "12.5 ± 0.4 mV"   # kept only as text


def test_compound_units_get_a_prefix_on_report_axes():
    assert scale_of([1.0e-12, 1.015e-12], "A/V") == (1e-12, "pA/V")
    assert scale_of([2.6e-10], "V/mm") == (1e-12, "pV/mm")


def test_scan_and_device_reports_show_few_digits(tmp_path):
    from test_structured_reports import D3, SWEEP, runtime
    rt, _ = runtime(tmp_path)
    rt.mount("p1", D3)
    rt.run(SWEEP)
    rt.run(SWEEP)
    folder = rt._run_dir(rt.runs_index(1)[0]["run_id"])
    text = " ".join(pdf_strings(folder / "report.pdf"))
    assert "has a peak at smu.V = 1.500 V" in text and "± 0.0000" not in text
    rt.flush_reports()
    device = " ".join(pdf_strings(rt.reports_dir / "Graphene" / "devices" / "D3.pdf"))
    assert "a change of" in device and "(2 scans)" in device and "± 0.0000" not in device
    assert "0.000000000001" not in device   # the slope's axis is in pA/V
