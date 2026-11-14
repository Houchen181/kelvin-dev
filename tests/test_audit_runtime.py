"""The 0.3.52 audit's findings on the runtime's tools (P1, R1-R10, R16-R18), kept fixed: each test is the audit's
repro (audit-repros/portal, runtime-a, runtime-b) cut to what it showed."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from labharness.agent.approval import Approver
from labharness.agent.runtime import LabRuntime
from simlab import build_virtual_lab

WEB_CARD = """
card: 1
id: acme/femto
model: ACME Femto laser (REST)
kind: laser
status: draft
identify: { query: "GET /v1/About#model", idn: "^FEMTO" }
parameters:
  power:
    label: Output power
    kind: measure
    unit: W
    get: "GET /v1/Basic/ActualOutputPower"
"""


class Recorder(Approver):
    """Every request that reaches a person, and every one Kelvin approved itself; says `answer` to the first."""
    def __init__(self, answer=False):
        self.asked, self.informed, self.answer = [], [], answer

    def approve(self, request):
        self.asked.append(request.title)
        return self.answer

    def inform(self, request, reasons):
        self.informed.append(request.title)


@pytest.fixture
def web_server():
    """A web server on this computer that keeps what it was sent: the path and the Authorization header."""
    seen = []

    class Catch(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            body = json.dumps({"model": "FEMTO-2"} if "About" in self.path else 1.5).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Catch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def runtime(tmp_path):
    made = []

    def make(approver):
        rt = LabRuntime(build_virtual_lab(seed=1), approver, tmp_path / "data")
        made.append(rt)
        return rt
    yield make
    for rt in made:
        rt.close_instruments(5)


def test_p1_an_address_that_names_an_environment_variable_is_a_persons_to_write(runtime, web_server, monkeypatch):
    """try_card sent any environment variable's value (an API key) to any web address the model named, as a bearer
    token, approved by Kelvin itself."""
    port, seen = web_server
    monkeypatch.setenv("KELVIN_AUDIT_SECRET", "sk-audit-0000-not-a-real-key")
    approver = Recorder(answer=True)
    rt = runtime(approver)
    address = "http://127.0.0.1:%d/?auth_env=KELVIN_AUDIT_SECRET" % port
    said = rt.try_card(WEB_CARD, address)
    assert said.startswith("not tried") and "KELVIN_AUDIT_SECRET" in said and "only a person" in said, said
    for spelling in (address.replace("auth_env", "AUTH_ENV"), "http://127.0.0.1:%d/?x=1&token_env=PATH" % port):
        assert rt.try_card(WEB_CARD, spelling).startswith("not tried")
    rt.home = rt.data_dir.parent   # the app's: it keeps a lab config
    said = rt.add_instrument(address, card="acme/femto")
    assert said.startswith("not added") and "KELVIN_AUDIT_SECRET" in said, said
    assert seen == [] and approver.asked == [] and approver.informed == []


def test_p1_a_web_address_the_scan_did_not_find_asks_a_person(runtime, web_server):
    port, seen = web_server
    approver = Recorder(answer=False)
    rt = runtime(approver)
    said = rt.try_card(WEB_CARD, "http://127.0.0.1:%d/" % port)
    assert approver.asked == ["Try a card on http://127.0.0.1:%d/?" % port] and approver.informed == [], said
    assert seen == [], seen
    rt.gate.auto = True   # a person's "approve what's safe" isn't a yes to reach the network
    rt.try_card(WEB_CARD, "http://127.0.0.1:%d/" % port)
    assert len(approver.asked) == 2 and approver.informed == [] and seen == []
