"""Field report 2026-11-15: bringing up an NV rig (Windows 11). A QICK RFSoC 4x2 on the board's USB network, a Cobolt
laser, an IDQ Time Controller on ZeroMQ (TCP 5555), and a GPIB bus (an NI GPIB-USB-HS) with two SR830 lock-ins and a
DMM. What went wrong, kept from going wrong again:
- install_for_instrument refused numpy>=2.1,<2.3 as "not package names": its pattern took one version clause only;
- the board's QICK server registered its eth0 address, 192.168.2.99, which this PC can't reach (it reaches the board
  at 192.168.3.1 over usb0), and nothing said so; the card's notes named a boot service that doesn't do what they said;
- find_on_network found the Time Controller but couldn't say what it was: its port 5555 speaks ZeroMQ, not SCPI, and
  its MAC address couldn't be used to find it;
- at 18:46:34 the monitor said "lockin's reply to SLVL? arrived garbled ('Stanford_Research_Systems,SR830,s/n81118,
  ver1.07')": the lock-in's own identity, a late reply to an earlier *IDN?, read in SLVL?'s place."""
import re

import pytest

from labharness import envs
from test_envs import PYPI, FakeUv, kelvin  # noqa: F401  (kelvin: Kelvin on the virtual lab, uv and PyPI faked)


# ---------------------------------------------------------------------- item 3: a version range is a package

@pytest.mark.parametrize("spec, plain", [
    ("numpy>=2.1,<2.3", "numpy>=2.1,<2.3"),                       # the field report's
    ("numpy >= 2.1, < 2.3", "numpy>=2.1,<2.3"),                   # PEP 508's spaces, written plainly
    ("qick==0.2.436", "qick==0.2.436"),
    ("qick", "qick"),
    ("zhinst-core~=24.1", "zhinst-core~=24.1"),
    ("pkg===1.0+local.7", "pkg===1.0+local.7"),                   # arbitrary equality
    ("numpy!=2.2.*,>=2.0", "numpy!=2.2.*,>=2.0"),
    ("qick[full]>=0.2.400,<0.3", "qick[full]>=0.2.400,<0.3"),     # extras
    ("qick[ full , docs ]", "qick[full,docs]"),
    ("pyvisa (>=1.13, <2)", "pyvisa>=1.13,<2"),                   # PEP 508's parentheses
    ("zope.interface>=6", "zope.interface>=6"),
    ("torch==2.1.0+cpu", "torch==2.1.0+cpu"),
    ("pkg==1!2.0", "pkg==1!2.0"),                                 # an epoch
])
def test_a_version_specifier_set_is_a_package(spec, plain):
    assert envs.requirement(spec) == plain
    assert envs.PACKAGE.match(spec) and envs.base_name(spec) == re.split(r"[\[<>=!~]", plain)[0]


@pytest.mark.parametrize("spec, why", [
    ("-r requirements.txt", "an option"),
    ("--index-url=https://evil.example/simple", "an option"),
    ("  -e git+https://example.com/x.git", "an option"),
    ("numpy>=2.1, -r extra.txt", "an option"),
    ("https://evil.example/pkg-1.0.whl", "a URL"),
    ("pkg @ https://evil.example/pkg-1.0.whl", "a URL"),
    ("pkg@https://evil.example/pkg-1.0.whl", "a URL"),
    ("git+https://github.com/x/y", "a URL"),
    ("file:pkg", "a URL"),
    ("C:\\Users\\me\\pkg-1.0.whl", "a path"),
    ("./pkg", "a path"),
    ("../pkg", "a path"),
    ("/tmp/pkg", "a path"),
    ("~/pkg", "a path"),
    ("pkg-1.0-py3-none-any.whl", "a path"),
    ("pkg-1.0.tar.gz", "a path"),
    ("numpy\n--index-url=https://evil.example/simple", "a tab, a line break"),
    ("numpy\t>=2", "a tab, a line break"),
    ("num\u200bpy", "a tab, a line break"),                      # a zero-width space
    ("numpy\u00a0>=2", "a tab, a line break"),                    # a no-break space
    ("numpy scipy", "not a package name"),
    ("qick; rm -rf /", "after the package"),
    ("numpy; python_version<'3.12'", "after the package"),
    ("numpy>=2.1,", "not a package name"),
    ("numpy>=", "not a package name"),
    ("[full]", "not a package name"),
    ("", "empty"),
])
def test_what_isnt_a_package_from_pypi_is_still_refused_saying_why(spec, why):
    with pytest.raises(ValueError, match=why):
        envs.requirement(spec)
    assert not envs.PACKAGE.match(spec)


def test_kelvin_installs_a_version_range_as_one_argument_to_uv(kelvin, monkeypatch):  # noqa: F811
    """The field report: numpy>=2.1,<2.3 was refused as "not package names". It is shown, approved and given to uv as
    written, one argument of uv's list (no shell reads it), PyPI asked about numpy only."""
    monkeypatch.setitem(PYPI, "numpy", {"info": {"name": "numpy", "version": "2.5.4", "summary": "arrays"}})
    said = kelvin.install_for_instrument("drivers", ["numpy >= 2.1, < 2.3", "qick[full]==0.2.436"],
                                         why="the RFSoC 4x2")
    assert said.startswith("installed in environment drivers"), said
    install = next(c for c in kelvin.uv.calls if c[1:3] == ["pip", "install"])
    assert install[-2:] == ["numpy>=2.1,<2.3", "qick[full]==0.2.436"]
    assert all(isinstance(a, str) for a in install)
    rec = envs.info("drivers", kelvin.home)
    assert set(rec["packages"]) == {"numpy", "qick"}


def test_what_isnt_a_package_reaches_neither_the_approval_nor_uv(kelvin):  # noqa: F811
    for bad, why in (("--index-url=https://evil.example/simple", "an option"),
                     ("pkg @ https://evil.example/pkg.whl", "a URL"),
                     ("C:/Users/me/pkg.whl", "a path"),
                     ("numpy\n-r evil.txt", "a line break")):
        said = kelvin.install_for_instrument("drivers", ["qick", bad])
        assert said.startswith("not package names: ") and why in said, said
        assert "numpy>=2.1,<2.3" in said   # what it takes instead
    assert kelvin.approver.requests == [] and kelvin.uv.calls == []


def test_a_clash_with_a_version_range_says_uvs_whole_reason(kelvin, monkeypatch):  # noqa: F811
    """0.3.53 kept uv's whole reason for a clash (envs._said): it still does for a range."""
    pin = (envs.app_pins() or ["numpy==2.5.4"])[0]
    monkeypatch.setattr(envs, "app_pins", lambda: [pin])
    monkeypatch.setitem(PYPI, "numpy", {"info": {"name": "numpy", "version": "2.5.4", "summary": "arrays"}})
    assert kelvin.install_for_instrument("drivers", ["Pyro4"]).startswith("installed")
    uv = kelvin.uv
    clash = ("Using Python 3.13.15 environment at: C:\\Users\\someone\\Kelvin\\env\\drivers\n"
             "  \u00d7 No solution found when resolving dependencies:\n"
             "  \u2570\u2500\u25b6 Because you require numpy>=2.1,<2.3 and %s, we can conclude that your requirements "
             "are unsatisfiable.\n" % pin)

    def refuses(cmd, env):
        if cmd[1:3] == ["pip", "install"] and "numpy>=2.1,<2.3" in cmd:
            return 1, clash
        return uv(cmd, env)
    monkeypatch.setattr(envs, "_run", refuses)
    said = kelvin.install_for_instrument("drivers", ["numpy>=2.1,<2.3"])
    assert said == ("could not install: uv could not install numpy>=2.1,<2.3: No solution found when resolving "
                    "dependencies: Because you require numpy>=2.1,<2.3 and %s, we can conclude that your requirements "
                    "are unsatisfiable. (%s is Kelvin's own. Kelvin's built-in drivers run with it whatever this "
                    "environment has, so the environment keeps it.)" % (pin, pin))


def test_a_uv_that_a_shell_would_run_is_never_used_on_windows(tmp_path, monkeypatch):
    """A version range's < and > are plain characters to a program started from a list, but cmd.exe reads them as
    redirections: a uv.cmd or uv.bat on PATH would run through it. On Windows only a uv.exe is used."""
    import shutil
    monkeypatch.setattr(envs, "WIN", True)
    monkeypatch.setattr(shutil, "which", lambda name: "C:\\tools\\shims\\uv.CMD")
    assert envs.uv_path(tmp_path) is None
    monkeypatch.setattr(shutil, "which", lambda name: "C:\\Users\\me\\.cargo\\bin\\uv.exe")
    assert str(envs.uv_path(tmp_path)).endswith("uv.exe")


# ---------------------------------------------------------------------- item 5: a QICK board on its USB network

FIELD_PC = [{"name": "Ethernet 3", "address": "192.168.3.100", "network": "192.168.3.0/24", "link_local": False},
            {"name": "Ethernet", "address": "192.168.1.10", "network": "192.168.1.0/24", "link_local": False}]
QICK_LAB = """lab: NV
instruments:
  rfsoc: { card: qick/rfsoc, address: "qick://192.168.3.1:8888/myqick" }
"""


@pytest.fixture
def field_pc(monkeypatch):
    """This computer as the field report's: the RFSoC 4x2 on its USB network (192.168.3.0/24, the board at
    192.168.3.1), the lab's network (192.168.1.0/24), and a lab network behind a router (10.20.x.x). Anything else
    (192.168.2.99, the board's eth0) takes no connection. The connections tried are kept; set `down` to have the
    board's name server refuse them too (the board restarting)."""
    import types
    from labharness import probe
    from labharness.drivers import MODULES
    state = types.SimpleNamespace(tried=[], down=False)

    def connect(ip, port, seconds):
        state.tried.append((ip, port))
        if (ip == "192.168.3.1" and not state.down) or ip.startswith("10.20."):
            return types.SimpleNamespace(close=lambda: None)
        raise OSError("timed out")
    monkeypatch.setattr(probe, "network_adapters", lambda: FIELD_PC)
    monkeypatch.setattr(probe, "_connect", connect)
    yield state
    MODULES.clear()


def pyro_error(kind="TimeoutError", text="receiving: timeout"):
    """One of Pyro4's errors of the connection, as Pyro4.errors raises it (Pyro4 isn't installed here)."""
    return type(kind, (Exception,), {"__module__": "Pyro4.errors"})(text)


def board_at(registered, uri=None):
    """QICK as Kelvin drives a board through Pyro4 (test_web_instruments' fake), the board's name server giving
    `registered` for myqick; the proxy's own URI is `uri` (where the name server said at the connect)."""
    import types
    from test_web_instruments import SOCCFG, FakeSoc, fake_qick

    class Soc(FakeSoc):
        fail = None

        def reset_gens(self):
            if self.fail is not None:
                raise self.fail
            super().reset_gens()
    soc = Soc()
    host, port = uri or registered
    soc._pyroUri = types.SimpleNamespace(host=host, port=port)
    module = fake_qick(soc, dict(SOCCFG, board="RFSoC4x2"))
    module.lookup = lambda ns_host, ns_port, name: registered
    return module, soc


def test_a_qick_server_registered_where_this_computer_cant_reach_is_said_at_once_with_the_fix(field_pc):
    """The field report: the board's server registered 192.168.2.99, its eth0, in the name server this computer reaches
    at 192.168.3.1 over usb0. make_proxy's first call waited out a connection to it and said only that it failed; it
    is never made now, and the error names the address, why it can't be reached, and how to start the server."""
    from labharness import builtin_registry
    from labharness.adapters import AdapterError
    from labharness.rfsoc import Qick
    module, _ = board_at(("192.168.2.99", 41234))
    module.make_proxy = lambda **kw: pytest.fail("make_proxy waited out its connection to 192.168.2.99")
    with pytest.raises(AdapterError) as e:
        Qick("rfsoc", builtin_registry().get("qick/rfsoc"), "qick://192.168.3.1:8888/myqick", module)
    said = str(e.value)
    assert said.startswith("rfsoc: the board's name server (192.168.3.1:8888) has its QICK server 'myqick' at "
                           "192.168.2.99:41234, which this computer can't reach: 192.168.2.99 takes no connection from "
                           "this computer, and is on none of its networks (192.168.3.0/24 on Ethernet 3, "
                           "192.168.1.0/24 on Ethernet).")
    assert "registers the address of one network interface, iface, eth0 unless told" in said
    assert "this computer reaches the board at 192.168.3.1, its USB network (usb0)" in said
    assert "start_server(ns_host='localhost', ns_port=8888, proxy_name='myqick', iface='usb0')" in said
    assert "or with ns_host='192.168.3.1' (qick.pyro_cli: -n 192.168.3.1" in said
    assert "run_on_board" in said and "at every boot" in said
    assert field_pc.tried == [("192.168.2.99", 41234)]   # one short knock, nothing sent


@pytest.mark.parametrize("registered, knocks", [
    (("192.168.3.1", 41234), []),                      # the name server's own address: usb0, as it should be
    (("192.168.1.77", 41234), []),                     # on one of this computer's networks
    (("10.20.0.5", 4000), [("10.20.0.5", 4000)]),      # on none, but it takes a connection (a router between)
])
def test_a_qick_server_this_computer_reaches_connects_as_before(field_pc, registered, knocks):
    from labharness import builtin_registry
    from labharness.rfsoc import Qick
    module, _ = board_at(registered)
    board = Qick("rfsoc", builtin_registry().get("qick/rfsoc"), "qick://192.168.3.1:8888/myqick", module)
    assert board.registered == registered and field_pc.tried == knocks
    assert board.identity().startswith("QICK,RFSoC4x2,192.168.3.1:8888/myqick")


def test_a_qick_server_registered_at_the_boards_loopback_is_said(field_pc):
    """start_server with no address on its iface registers 127.0.0.1: the board's own, not this computer's, unless
    the name server is on this computer too."""
    from labharness import builtin_registry
    from labharness.adapters import AdapterError
    from labharness.rfsoc import Qick
    card = builtin_registry().get("qick/rfsoc")
    with pytest.raises(AdapterError, match=r"at 127\.0\.0\.1:41234, which this computer can't reach: 127\.0\.0\.1 is "
                                           r"the board's own loopback address"):
        Qick("rfsoc", card, "qick://192.168.3.1:8888/myqick", board_at(("127.0.0.1", 41234))[0])
    assert Qick("rfsoc", card, "qick://127.0.0.1:8888/myqick", board_at(("127.0.0.1", 41234))[0]).registered


def test_a_board_that_stops_answering_says_why_and_what_to_do(field_pc):
    """A reboot ends the name server and the server, which ran in Jupyter kernels: the person started them again by
    hand three times. A call that gets no answer now says why when Kelvin can tell, in the alarm too."""
    from labharness.homelab import build_lab
    from labharness.drivers import MODULES
    from labharness.instrument import InstrumentLost
    module, soc = board_at(("192.168.3.1", 41234))
    MODULES["qick"] = module
    lab = build_lab(QICK_LAB)
    inst = lab.instruments["rfsoc"]
    soc.fail = pyro_error()

    field_pc.down = True                                # the board restarting: its name server doesn't answer either
    with pytest.raises(InstrumentLost) as e:
        inst.write("output", 0)
    said = str(e.value)
    assert said.startswith("rfsoc (RFSoC board running QICK) doesn't answer through the board's QICK server "
                           "(192.168.3.1:8888/myqick): the board's name server at 192.168.3.1:8888 doesn't answer "
                           "either: the board is off or restarting, or its name server and QICK server were stopped "
                           "(run in Jupyter kernels, a reboot ends both).")
    assert "at every boot (the card's notes: qick/rfsoc); then connect rfsoc again [receiving: timeout]" in said
    assert inst.health.lost_text == said

    field_pc.down = False                               # back, its server started again the notebook's way: eth0
    module.lookup = lambda ns_host, ns_port, name: ("192.168.2.99", 40000)
    with pytest.raises(InstrumentLost, match=r"its QICK server was started again and registered 192\.168\.2\.99:40000, "
                                             r"which this computer can't reach: .*iface='usb0'"):
        inst.write("output", 0)

    module.lookup = lambda ns_host, ns_port, name: ("192.168.3.1", 40000)   # started again, the right way
    with pytest.raises(InstrumentLost, match=r"has it at 192\.168\.3\.1:40000 now, not 192\.168\.3\.1:41234; connect "
                                             r"rfsoc again"):
        inst.write("output", 0)

    module.lookup = lambda ns_host, ns_port, name: ("192.168.3.1", 41234)   # where it was: a slow board, Pyro's words
    with pytest.raises(InstrumentLost) as e:
        inst.write("output", 0)
    assert "is the board on, with its Pyro4 name server and QICK server running" in str(e.value)
    assert str(e.value).endswith("[receiving: timeout]")

    soc.fail = RuntimeError("tProc: no such channel")   # the board's own error: as it was, never taken for a lost link
    with pytest.raises(RuntimeError, match="no such channel"):
        inst.write("output", 0)


def test_the_qick_cards_notes_start_the_server_at_boot_as_qicks_own_files_do_not():
    """The card said qick/pyro4/qick_pyro.service starts both at boot. As qick's repository has it, that service runs
    pyro_service.py, whose name server listens on localhost:8000 only, with the proxy named rfsoc: so the notes now
    have a service of Kelvin's, checked here as systemd and bash read it (and once on Ubuntu's own systemd)."""
    import ast
    import shlex
    from labharness import builtin_registry
    notes = builtin_registry().get("qick/rfsoc").notes
    text = "\n".join(notes)
    assert "qick_pyro.service starts both at boot" not in text
    assert "iface='usb0'" in text and "192.168.2.99" in text and "qick.pyro_cli takes no iface" in text
    assert "localhost, port 8000" in text and "proxy named rfsoc" in text   # why qick's own isn't it, as it comes
    boot = next(n for n in notes if n.startswith("At every boot"))
    script = boot[boot.index("cat > /etc/systemd/system/qick-pyro.service <<'EOF'\n"):]
    unit = script.split("<<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
    assert script.rstrip().endswith("systemctl daemon-reload && systemctl enable --now qick-pyro && "
                                    "systemctl --no-pager status qick-pyro")
    assert "%" not in unit and "$" not in unit         # systemd's specifiers and variables: none to expand
    keys = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line and not line.startswith("["))
    assert keys["Restart"] == "always" and keys["WantedBy"] == "multi-user.target"
    argv = shlex.split(keys["ExecStart"])              # systemd's quoting, as POSIX's for these characters
    assert argv[:2] == ["/bin/bash", "-c"] and len(argv) == 3
    words = shlex.split(argv[2].replace(";", " ; ").replace("&", " & "))
    at = words.index("pyro4-ns")
    assert words[at:at + 5] == ["pyro4-ns", "-n", "0.0.0.0", "-p", "8888"]
    assert "PYRO_SERIALIZERS_ACCEPTED=pickle" in words and words[-2:] == ["wait", "-n"]
    code = words[words.index("python3") + 2]
    call = ast.parse(code).body[1].value
    assert call.func.id == "start_server" and {k.arg: k.value.value for k in call.keywords} == {
        "ns_host": "localhost", "ns_port": 8888, "proxy_name": "myqick", "iface": "usb0"}
