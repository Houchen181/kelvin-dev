"""Kelvin starts whatever a Kelvin before it left behind (review of 0.3.45, 7.4): app.json names the Kelvin running, and
one that crashed leaves it. A stale one that named a port another program now answered in its own protocol, or that
held null, ended every start at once with no window, until someone deleted the file."""
import json
import os
import socket
import threading

import pytest

from labharness import desktop


def _serve(reply: bytes) -> int:
    """A program on a port of its own that answers every connection with reply, then hangs up."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                c.settimeout(2.0)
                c.recv(4096)
                c.sendall(reply)
            except OSError:
                pass
            finally:
                c.close()
    threading.Thread(target=loop, daemon=True).start()
    return srv.getsockname()[1]


@pytest.mark.parametrize("reply", [b"220 ready\r\n",                                   # its own protocol
                                   b"HTTP/2 200\r\n\r\n",                              # a status line http.client refuses
                                   b"",                                                # hangs up at once
                                   b"HTTP/1.0 200 OK\r\n\r\n<html>another page</html>"])
def test_a_port_another_program_answers_now_is_no_kelvin(tmp_path, reply):
    lock = tmp_path / "app.json"
    lock.write_text(json.dumps({"port": _serve(reply), "pid": os.getpid()}), encoding="utf-8")
    assert desktop._running_copy(lock, wait=2.0) is None
    assert desktop._one_kelvin(None, lock, "key") is True   # this start goes on: it is the one Kelvin


@pytest.mark.parametrize("text", ["", "null", "[]", '"x"', "{", '{"port": null}', '{"port": "x"}', '{"port": 0}',
                                  '{"port": 70000}', '{"pid": 1}', '{"port": [1]}'])
def test_an_app_json_cut_short_or_garbled_is_no_kelvin(tmp_path, text):
    lock = tmp_path / "app.json"
    lock.write_text(text, encoding="utf-8")
    assert desktop._running_copy(lock) is None
    assert desktop._one_kelvin(None, lock, "key") is True


def test_a_kelvin_that_answers_is_still_found(tmp_path):
    lock = tmp_path / "app.json"
    port = _serve(b"HTTP/1.0 200 OK\r\n\r\n<!doctype html><head><title>Kelvin</title></head>")
    lock.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    assert desktop._running_copy(lock, wait=2.0) == port
    lock.write_text(json.dumps({"port": port}), encoding="utf-8")   # written by a Kelvin from before the process id
    assert desktop._running_copy(lock, wait=2.0) == port


def test_the_portal_of_a_kelvin_that_has_gone_is_not_trusted(tmp_path, monkeypatch):
    """The Kelvin that wrote app.json has gone: whatever answers on its port is not asked, even a page that looks like
    Kelvin's."""
    lock = tmp_path / "app.json"
    port = _serve(b"HTTP/1.0 200 OK\r\n\r\n<title>Kelvin</title>")
    lock.write_text(json.dumps({"port": port, "pid": 4242}), encoding="utf-8")
    monkeypatch.setattr(desktop, "_alive", lambda pid: pid != 4242)
    assert desktop._running_copy(lock, wait=2.0) is None


def test_removing_data_is_not_stopped_by_a_garbled_app_json(tmp_path, monkeypatch, capsys):
    from labharness import uninstall
    (tmp_path / "app.json").write_text("null", encoding="utf-8")
    monkeypatch.setattr(uninstall, "remove_data", lambda home: {"failed": []})
    assert desktop.main(["--remove-data", "--home", str(tmp_path)]) == 0
