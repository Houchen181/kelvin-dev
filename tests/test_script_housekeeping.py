"""Smaller things about running a lab's scripts (review of 0.3.45, section 8): a camera's frame recorded at once, not
in time quadratic in its size; input() said for what it is; output.txt kept to a size; a pool of processes never
refused by the stand-in's listener."""
import socket
import time

from labharness import scriptrun
from test_script_runs import folder, lab, output, run, write  # noqa: F401 (the script runs' fixtures and helpers)


class Quiet:
    """A socket that is never read here: _Conn only keeps it."""


def test_lines_come_whole_however_the_bytes_arrive():
    conn = scriptrun._Conn(Quiet())
    assert list(conn.lines(b'{"op": "a"}\n{"op"')) == [b'{"op": "a"}']
    assert list(conn.lines(b': "b"')) == []
    assert list(conn.lines(b'}\n\n{"op": "c"}\n')) == [b'{"op": "b"}', b"", b'{"op": "c"}']
    assert conn.buf == bytearray() and conn.looked == 0
    assert all(isinstance(line, bytes) for line in conn.lines(b"x\n"))


def test_a_frame_of_tens_of_megabytes_is_read_in_one_pass():
    """record() of a camera's frame: one line of 48 MB, here in 64 KB pieces (the old reading copied what it had and
    looked for the line's end from the start at each piece: 19 s for 72 MB)."""
    conn = scriptrun._Conn(Quiet())
    piece = b"7" * 65536
    t0 = time.perf_counter()
    for _ in range(768):
        assert list(conn.lines(piece)) == []
    got = list(conn.lines(b"\n"))
    took = time.perf_counter() - t0
    assert len(got) == 1 and len(got[0]) == 768 * 65536
    assert took < 5, took
    assert scriptrun.RECV_BYTES == 1 << 20


ASKS = """
try:
    v = input("Voltage? ")
except EOFError as e:
    print("caught %s: %s" % (type(e).__name__, e))
"""


def test_input_in_a_script_says_nobody_can_answer_and_how_to_give_it_values(lab, folder, tmp_path):
    """stdin is nothing: input() ended the script with a bare EOFError. It says why now, and the script that catches
    EOFError still does."""
    gate, request, report = run(lab, folder, tmp_path, write(folder, "ask.py", ASKS))
    said = output(report)
    assert "caught NoInput" in said, said
    assert "nobody can type" in said and "VMAX = 5" in said and "params" in said, said
    assert report.status == "done"


LOUD = """
for i in range(2000):
    print("line %04d " % i + "x" * 40)
"""


def test_output_txt_keeps_what_fits_and_says_how_much_more_there_was(lab, folder, tmp_path, monkeypatch):
    monkeypatch.setattr(scriptrun, "MAX_OUTPUT", 4096)
    gate, request, report = run(lab, folder, tmp_path, write(folder, "loud.py", LOUD))
    said = output(report)
    assert len(said.encode("utf-8")) < 4096 + 200
    assert said.startswith("line 0000") and "line 1999" not in said
    assert "[Kelvin kept the first 4 kB of what the script printed, and left out" in said
    assert report.status == "done"   # the script ran to its end: nothing waited on a full pipe


def test_the_stand_ins_listener_takes_a_pool_of_processes_connecting_at_once(lab, folder, tmp_path, monkeypatch):
    backlogs = []
    own = socket.socket.listen

    def listen(self, *args):
        backlogs.append(args[0] if args else None)
        return own(self, *args)
    monkeypatch.setattr(socket.socket, "listen", listen)
    run(lab, folder, tmp_path, write(folder, "quiet.py", "print('ok')\n"))
    assert backlogs and backlogs[0] == scriptrun.LISTEN_BACKLOG >= 64
