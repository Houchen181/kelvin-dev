from datetime import datetime

from labharness import rundata


def test_a_run_says_when_it_began_in_seconds():
    """For a phone in another time zone: the time in the run's name (this computer's clock) as seconds since 1970."""
    assert rundata.started_at("20261030-140116-field-sweep") == datetime(2026, 10, 30, 14, 1, 16).timestamp()
    assert rundata.started_at("notes") is None
