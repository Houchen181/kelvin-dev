"""The lab's record never fails for its file (review of 0.3.45: lines kept back while it can't be written) and is
chained (the ELN/LIMS work: each line keeps the hash of the one before). Together: lines kept back are chained only as
they are written, so the chain holds across the gap; a file kept open while a run measures that fails is let go of, so
nothing left half-written in it goes out later, and letting it go never fails the run's end."""
import pytest

from labharness import eventlog


class Gone:
    """A record file kept open whose drive went away: writes fail; closing it may fail too."""

    def __init__(self, f, close_fails=False):
        self.f, self.close_fails = f, close_fails

    def fileno(self):
        return self.f.fileno()

    def write(self, data):
        raise OSError(53, "The network path was not found")

    def flush(self):
        pass

    def close(self):
        self.f.close()
        if self.close_fails:
            raise OSError(53, "The network path was not found")


def types(path):
    return [e["type"] for e in eventlog.EventLog(path).events()]


@pytest.fixture(autouse=True)
def at_once(monkeypatch):
    monkeypatch.setattr(eventlog, "RETRY_S", 0.0)


def test_lines_kept_back_while_its_folder_is_away_are_chained_once_they_are_written(tmp_path):
    here = tmp_path / "data"
    log = eventlog.EventLog(here / "events.jsonl")
    log.write("one")
    here.rename(tmp_path / "away")                     # the data folder's network share went away
    log.write("two")
    log.write("three")
    assert "could not be written" in log.trouble
    assert [e["type"] for e in log.events()] == ["two", "three"]   # kept meanwhile, and read back
    (tmp_path / "away").rename(here)                   # it is back
    log.write("four")
    assert log.trouble is None
    assert types(here / "events.jsonl") == ["one", "two", "three", "four"]
    v = eventlog.verify_chain(here / "events.jsonl")
    assert v["lines"] == v["chained"] == 4 and v["breaks"] == []


def test_a_held_record_that_fails_while_a_run_measures_is_let_go_of_and_its_lines_go_once_they_can(
        tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "RETRY_S", 3600.0)   # tried again only when its time comes
    path, shared = tmp_path / "events.jsonl", tmp_path / "instrument-log.jsonl"
    log = eventlog.EventLog(path, shared=shared)
    log.write("one")
    with log.holding():
        log._held, log._held_shared = Gone(log._held), Gone(log._held_shared)
        log.write("two")                               # both fail: the lab's own record keeps it back
        assert log._held is None and log._held_shared is None and "could not be written" in log.trouble
        log.write("three")                             # kept back too, until its time comes
        assert "could not be written" in log.trouble
        log._retry_at = 0.0                            # it comes: the file is opened for the lines now
        log.write("four")
        assert log.trouble is None
    assert types(path) == ["one", "two", "three", "four"]
    assert types(shared) == ["one", "three", "four"]   # the lab's log is a copy: its line for two is lost
    for p in (path, shared):
        v = eventlog.verify_chain(p)
        assert v["lines"] == v["chained"] and v["breaks"] == [], p.name


def test_letting_go_of_a_held_record_never_fails_the_end_of_a_run(tmp_path):
    log = eventlog.EventLog(tmp_path / "events.jsonl", shared=tmp_path / "instrument-log.jsonl")
    with log.holding():
        log.write("one")
        log._held = Gone(log._held, close_fails=True)
        log._held_shared = Gone(log._held_shared, close_fails=True)
    assert log._held is None and log._held_shared is None
    log.write("two")
    assert types(tmp_path / "events.jsonl") == ["one", "two"]
