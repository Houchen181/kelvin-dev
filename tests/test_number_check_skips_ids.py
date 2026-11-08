"""The notebook's number check reads the records an entry cites, and each run has a uuid now (the ELN/LIMS work). A
uuid like 2123e834-… read as the number 2123e834, too large for a float: writing an entry that cited that run failed
(about one run in ten). One like 3abcdef0-… read as 3, so an entry's "3 V" passed the check on a uuid. Ids of hex
digits (uuids, seals) are not numbers; a number no float holds is not checked."""
from labharness import numbercheck

UUIDS = ["2123e834-a54e-429f-beb3-8d8e89517e06", "3abcdef0-1111-4222-8333-944445555666",
         "1E30a000-0000-4000-8000-000000000000"]
SEAL = "1234e567" + "ab" * 28                               # a SHA-256 that starts like a number


def test_a_records_ids_are_never_read_as_numbers():
    record = {"uuid": UUIDS, "sha256": SEAL, "text": "kept as %s, sealed %s" % (UUIDS[0], SEAL)}
    assert numbercheck.values_in(record) == []
    assert numbercheck.check("Swept to 3 V.", [record])["missing"] == ["3 V"]   # no uuid stands in for it


def test_a_number_no_float_holds_is_left_out_not_failed():
    assert numbercheck.numbers_in("1e999 K and 2 x 10^400 Hz", counts=False) == []
    r = numbercheck.check("It read 1e999 K at 1.5 K.", [{"T": 1.5}])
    assert r == {"checked": 1, "found": ["1.5 K"], "missing": []}
