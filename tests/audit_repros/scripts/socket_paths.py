"""Repro: two ways a script's TCP connection to a lab instrument's own address goes straight to the instrument,
past Kelvin's stand-in and the gate, with nobody asked while it runs.

1. asyncio. On Windows asyncio's default loop (proactor) connects with _overlapped.ConnectEx and sends with
   WSASend, never through socket.socket.connect / send, which are what the stand-in patches
   (data/scriptshim/kelvin_shim.py:591-665). asyncio.open_connection(host, port) to the lab instrument's exact
   address is a raw connection. The read-ahead (scripts.py:568, 601) doesn't know the call, so the approval card
   says nothing of it either.
2. Another name for the same host. scriptrun.py:1824-1826 lets a socket through, asking no one, when its address
   is no lab instrument's as text (canonical / same_device compare the host's spelling: scriptrun.py:519-584).
   The lab lists the instrument as 127.0.0.1 here and the script's sock.connect says localhost; in a lab it is the
   instrument's DNS name in one place and its IP address in the other. The card calls it "not in this lab".
   (socket.create_connection with a name is caught when the lab has the IP address: its own loop calls the patched
   connect with each resolved address. sock.connect((name, port)) is not, nor an IP address when the lab has the name.)

The "instrument" is a TCP server on this computer's loopback that keeps what it is sent; in Kelvin's lab the same
address is a simulated lock-in. No hardware, nothing off this computer.
"""
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import fakes, make_lab, output_of, run_script, write_script

LAB = """
lab: Socket lab
instruments:
  lockin2:
    card: virtual/lock-in
    address: TCPIP0::127.0.0.1::%d::SOCKET
"""

THROUGH_KELVIN = '''
import socket
s = socket.create_connection(("127.0.0.1", %(port)d), timeout=5)
s.sendall(b"*IDN?\\n")
print("reply:", s.recv(200))
s.close()
'''

ASYNCIO = '''
import asyncio

async def main():
    reader, writer = await asyncio.open_connection("127.0.0.1", %(port)d)
    writer.write(b"SLVL 5.0\\n*IDN?\\n")
    await writer.drain()
    print("reply:", await asyncio.wait_for(reader.readline(), 5))
    writer.close()

asyncio.run(main())
'''

OTHER_NAME = '''
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
s.connect(("localhost", %(port)d))
s.sendall(b"SLVL 5.0\\n*IDN?\\n")
print("reply:", s.recv(200))
s.close()
'''


class Port:
    """What listens at the instrument's address: it keeps every line it is sent, and answers *IDN?."""

    def __init__(self):
        self.got = []
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.talk, args=(conn,), daemon=True).start()

    def talk(self, conn):
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.got.append(line.decode("latin-1"))
                    if line.strip().upper() == b"*IDN?":
                        conn.sendall(b"THE REAL INSTRUMENT'S OWN PORT\n")
        except OSError:
            pass


def run(port, name, text):
    lab = make_lab(LAB % port.port)
    port.got.clear()
    with tempfile.TemporaryDirectory(prefix="audit-socket-") as tmp:
        fakes(tmp)
        folder = Path(tmp) / "scripts"
        folder.mkdir()
        write_script(folder, name, text % {"port": port.port})
        gate, approver, prep, report = run_script(lab, folder, Path(tmp) / "runs", name, answers=[True])
        print("--- %s ---" % name)
        print("approval card:")
        for line in approver.requests[0].items[1:-1]:
            print("   ", line.replace(str(port.port), "<port>"))
        print("asked while it ran:", [q.title for q in approver.requests[1:]])
        print("run:", report.status, "|", report.message, "| warnings:", report.warnings)
        for line in output_of(report).splitlines()[-3:]:
            print("    out:", line)
        entries = [json.loads(ln) for ln in (Path(report.path) / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
        print("    commands the gate saw:", [e.get("command") for e in entries])
        print("    lines that reached the instrument's own port, past Kelvin:", port.got)
        print()


if __name__ == "__main__":
    port = Port()
    run(port, "through_kelvin.py", THROUGH_KELVIN)
    run(port, "with_asyncio.py", ASYNCIO)
    run(port, "other_name.py", OTHER_NAME)
