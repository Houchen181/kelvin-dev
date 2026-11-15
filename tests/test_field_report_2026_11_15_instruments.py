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


LONG = ["qick" + " " * 50000 + "x", "qick[" + " " * 50000 + "x", "qick[full" + " " * 50000 + "x",
        "qick (>=1" + " " * 50000 + "x", "numpy>=2.1" + " ," * 25000 + "x", "q" * 50000]


@pytest.mark.parametrize("spec", LONG, ids=["spaces", "extras", "an extra", "parentheses", "commas", "a name"])
def test_a_long_package_is_refused_at_once_and_never_freezes_kelvin(spec):
    """Review of 2026-11-15: the pattern had two runs of spaces side by side, and re tried every split of them:
    "qick", 16,000 spaces and "x" took 10 s, 50,000 spaces 110 s, with re holding the GIL, so the monitor, Stop and
    the portal froze with it. The pattern is linear now, and nothing over 200 characters is matched at all."""
    import time
    t0 = time.perf_counter()
    envs.PACKAGE.match(spec)                           # the pattern alone, on all of it (a long name is one)
    took = time.perf_counter() - t0
    assert took < 0.5, "the pattern took %.1f s" % took
    t0 = time.perf_counter()
    with pytest.raises(ValueError, match="characters long: no package's name and versions are longer than 200"):
        envs.requirement(spec)
    assert time.perf_counter() - t0 < 0.05


def test_a_cards_requires_and_an_install_with_a_long_package_are_refused_at_once(kelvin):  # noqa: F811
    import time
    from labharness.cards import CardError, parse_card
    card = {"card": 1, "id": "a/b", "model": "B", "vendor": "A", "kind": "dmm", "requires": ["qick" + " " * 12000 + "x"],
            "parameters": {"V": {"kind": "measure", "unit": "V", "get": "V?"}}}
    t0 = time.perf_counter()
    with pytest.raises(CardError, match="the maker's Python packages"):
        parse_card(card)                                # took 4.6 s
    assert time.perf_counter() - t0 < 0.5
    t0 = time.perf_counter()
    said = kelvin.install_for_instrument("drivers", ["qick" + " " * 50000 + "x"])
    assert said.startswith("not package names: ") and "characters long" in said
    assert time.perf_counter() - t0 < 0.5 and kelvin.uv.calls == []
    assert kelvin.install_for_instrument("drivers", ["q%d" % i for i in range(13)]) == "at most 12 packages at a time"


@pytest.mark.parametrize("spec", ["pkg-1.0.tbz", "pkg-1.0.txz", "pkg-1.0.tlz", "pkg-1.0.tar.lz", "pkg-1.0.tar.lzma",
                                  "pkg-1.0.tar.zst", "pkg-1.0.tzst", "pkg-1.0-py3-none-any.WHL", "numpy.whl==1.0",
                                  "pkg.tar.gz[full]", "pkg-1.0.7z"])
def test_every_archive_pip_or_uv_takes_for_a_file_is_refused(spec):
    """Review of 2026-11-15: .tbz, .txz, .tlz, .tar.lz, .tar.lzma and .tar.zst passed as names, which pip and uv take
    for files here: any name with an archive's or a wheel's suffix is a path, whatever follows."""
    with pytest.raises(ValueError, match="a path or a file"):
        envs.requirement(spec)
    assert not envs.PACKAGE.match(spec)


@pytest.mark.parametrize("spec", ["backports.tarfile", "zope.interface>=6", "jaraco.zipp", "lazr.restfulclient"])
def test_a_name_that_only_holds_an_archives_letters_is_a_package(spec):
    assert envs.requirement(spec) == spec


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
    assert field_pc.tried == [("192.168.2.99", 41234)] * 2   # two short knocks (2 s, then 5 s), nothing sent


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
    # review of 2026-11-15: no name server on every network of the board's (-n 0.0.0.0, pickle, as root, at every
    # boot): it listens on the USB network's address, where start_server registers too (its ns_host's)
    assert words[at:at + 5] == ["pyro4-ns", "-n", "192.168.3.1", "-p", "8888"] and "0.0.0.0" not in unit
    assert "PYRO_SERIALIZERS_ACCEPTED=pickle" in words and words[-2:] == ["wait", "-n"]
    code = words[words.index("python3") + 2]
    call = ast.parse(code).body[1].value
    assert call.func.id == "start_server" and {k.arg: k.value.value for k in call.keywords} == {
        "ns_host": "192.168.3.1", "ns_port": 8888, "proxy_name": "myqick"}
    said = " ".join(boot.split())
    assert "The Pyro server has no password: anyone who reaches it can run code on the board, as root" in said
    assert "keeps it listening across reboots" in said and "Keep the board on a private network" in said


def test_a_server_this_computer_may_not_reach_is_left_to_pyro_and_its_error_says_why(field_pc):
    """Review of 2026-11-15: one 2 s connection decided, and a lost packet would have stopped a board that works. Only
    a private address on none of this computer's networks, beside a name server on one, which takes no connection
    twice, fails at once; anywhere else Pyro tries, and its error says what Kelvin found."""
    from labharness import builtin_registry
    from labharness.adapters import AdapterError
    from labharness.rfsoc import Qick
    card = builtin_registry().get("qick/rfsoc")
    for address, registered in (("qick://192.168.3.1:8888/myqick", ("8.8.4.4", 41234)),   # public: a router
                                ("qick://10.20.0.9:8888/myqick", ("10.30.0.5", 41234))):  # a name server routed too
        field_pc.tried.clear()
        module, _ = board_at(registered)
        board = Qick("rfsoc", card, address, module)
        assert board.registered == registered and field_pc.tried == [registered]   # one knock, then Pyro's turn
        assert "which this computer may not reach: %s takes no connection" % registered[0] in board.doubt

        def refused(**kw):
            raise pyro_error("CommunicationError", "cannot connect to %s: timed out" % (registered,))
        module.make_proxy = refused
        with pytest.raises(AdapterError, match=r"could not reach the QICK server 'myqick' \(cannot connect .*\): the "
                                               r"board's name server .* which this computer may not reach"):
            Qick("rfsoc", card, address, module)


def test_a_name_server_that_doesnt_answer_a_lookup_is_waited_for_seconds_not_pyros_minute(field_pc, monkeypatch):
    """Review of 2026-11-15: after a call that had already failed, the name server's lookup could wait Pyro's
    COMMTIMEOUT (a minute and more) and then say the server wasn't registered. It is waited for LOOKUP_S, and a
    name server that didn't answer is said so; one that answered without the server, that."""
    import time
    from labharness import rfsoc
    from labharness.drivers import MODULES
    from labharness.homelab import build_lab
    from labharness.instrument import InstrumentLost
    monkeypatch.setattr(rfsoc, "LOOKUP_S", 0.3)
    module, soc = board_at(("192.168.3.1", 41234))
    MODULES["qick"] = module
    inst = build_lab(QICK_LAB).instruments["rfsoc"]
    soc.fail = pyro_error()

    def slow(ns_host, ns_port, name):
        time.sleep(5)
        return ("192.168.3.1", 41234)
    module.lookup = slow
    t0 = time.monotonic()
    with pytest.raises(InstrumentLost, match=r"the board's name server at 192\.168\.3\.1:8888 takes a connection but "
                                             r"didn't answer within 0\.3 s: the board may be busy or restarting"):
        inst.write("output", 0)
    assert time.monotonic() - t0 < 2.0

    def refusing(ns_host, ns_port, name):
        raise pyro_error("CommunicationError", "connection closed")
    module.lookup = refusing
    with pytest.raises(InstrumentLost, match=r"takes a connection but didn't answer \(connection closed\)"):
        inst.write("output", 0)

    def unknown(ns_host, ns_port, name):
        raise pyro_error("NamingError", "unknown name: myqick")
    module.lookup = unknown
    with pytest.raises(InstrumentLost, match=r"answers, but has no QICK server 'myqick' now"):
        inst.write("output", 0)


# ---------------------------------------------------------------------- item 9: a ZeroMQ instrument, and MACs

class Rep:
    """A ZeroMQ REP socket's side of ZMTP 3.1, written out by hand on a port of this computer, in libzmq's order: its
    signature as a connection opens, its major version once the peer's signature is in, the rest of its greeting once
    the peer's version is, READY with its Socket-Type, then a reply to each request. `how` makes it misbehave as a peer
    might. It keeps every request's frames, and every connection is served on a thread of its own (a sweep's knock
    is one too)."""
    IDN = b"ID Quantique,ID900,SN01234,v7.0.4"

    def __init__(self, how="", socket_type=b"REP", mechanism=b"NULL", major=3):
        import socket
        import threading
        self.how, self.socket_type, self.mechanism, self.major = how, socket_type, mechanism, major
        self.requests, self.readies, self.free = [], [], threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.free.set()
        self.listener.close()

    def _accept(self):
        import threading
        while not self.free.is_set():
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(10)
        try:
            if self._talk(conn) is False:   # it closes the connection there and then
                return
            conn.settimeout(3)
            while conn.recv(4096):   # until the peer closes: its last reads never meet a reset
                pass
        except OSError:
            pass
        finally:
            conn.close()

    @staticmethod
    def frame(flags, body):
        import struct
        if flags & 0x02:
            return bytes([flags]) + struct.pack(">Q", len(body)) + body
        return bytes([flags, len(body)]) + body

    def command(self, name, data=b""):
        return self.frame(0x04, bytes([len(name)]) + name + data)

    def _talk(self, conn):
        import struct
        import time

        def need(n):
            data = b""
            while len(data) < n:
                chunk = conn.recv(n - len(data))
                if not chunk:
                    raise OSError("closed")
                data += chunk
            return data

        def read_frame():
            flags = need(1)[0]
            size = struct.unpack(">Q", need(8))[0] if flags & 0x02 else need(1)[0]
            return flags, need(size)
        ready = self.command(b"READY", bytes([11]) + b"Socket-Type" + struct.pack(">I", len(self.socket_type))
                             + self.socket_type)
        rest = b"\x01" + self.mechanism.ljust(20, b"\x00") + b"\x00" + bytes(31)
        if self.how == "eager":   # its whole greeting and READY at once, as a peer may
            conn.sendall(b"\xff" + bytes(7) + b"\x01\x7f" + bytes([self.major]) + rest + ready)
            need(64)
        else:
            conn.sendall(b"\xff" + bytes(7) + b"\x01\x7f")
            if self.how == "closes":
                return False
            need(10)
            conn.sendall(bytes([self.major]))
            need(1)
            if self.major < 3:
                self.free.wait(5)
                return
            conn.sendall(rest)
            need(53)
            if self.how == "error":
                conn.sendall(self.command(b"ERROR", bytes([20]) + b"Invalid socket type!"))
                return
            conn.sendall(ready)
        flags, body = read_frame()
        self.readies.append(body)
        if self.how == "silent":
            self.free.wait(10)
            return
        parts = []
        while True:
            flags, body = read_frame()
            parts.append(body)
            if not flags & 0x01:
                break
        self.requests.append(parts)
        delimiter = self.frame(0x01, b"")
        if self.how == "long":
            conn.sendall(delimiter + self.frame(0x02, self.IDN))
        elif self.how == "ping":
            conn.sendall(self.command(b"PING", b"\x00\x64ctx") + delimiter + self.frame(0x00, self.IDN))
        elif self.how == "split":
            for byte in delimiter + self.frame(0x00, self.IDN):
                conn.sendall(bytes([byte]))
                time.sleep(0.005)
        elif self.how == "parts":
            conn.sendall(delimiter + self.frame(0x01, self.IDN[:18]) + self.frame(0x00, self.IDN[18:]))
        elif self.how == "huge":
            conn.sendall(delimiter + b"\x02" + struct.pack(">Q", 1 << 40))
            self.free.wait(10)
        elif self.how == "flood":
            for _ in range(100):
                conn.sendall(self.frame(0x01, b"x"))
            self.free.wait(10)
        elif self.how == "bad":
            conn.sendall(b"\xf0\x05hello")
        else:
            conn.sendall(delimiter + self.frame(0x00, self.IDN))


@pytest.fixture
def rep_server():
    made = []

    def make(*a, **k):
        made.append(Rep(*a, **k))
        return made[-1]
    yield make
    for r in made:
        r.close()


@pytest.mark.parametrize("how, reply", [
    ("", "ID Quantique,ID900,SN01234,v7.0.4"),
    ("eager", "ID Quantique,ID900,SN01234,v7.0.4"),
    ("long", "ID Quantique,ID900,SN01234,v7.0.4"),
    ("ping", "ID Quantique,ID900,SN01234,v7.0.4"),
    ("split", "ID Quantique,ID900,SN01234,v7.0.4"),
    ("parts", "ID Quantique,ID900 | ,SN01234,v7.0.4"),   # a reply of two frames: each shown
])
def test_kelvin_asks_a_zeromq_instrument_who_it_is_as_a_req_socket_does(rep_server, how, reply):
    """The field report's Time Controller answered port 5555 with ZeroMQ's greeting, and *IDN? sent as SCPI's text
    never came back. Kelvin speaks ZMTP 3.0 itself (no pyzmq): greetings, READY as a REQ socket, an empty delimiter
    and *IDN?, its reply read however it comes: at once, in a long frame, after a PING, a byte at a time, in parts."""
    import time
    from labharness import probe
    rep = rep_server(how)
    t0 = time.monotonic()
    found = probe.probe_port("127.0.0.1", rep.port, "idn")
    assert time.monotonic() - t0 < 2.0
    assert found["zmq"] == {"version": "3.1", "socket": "REP", "reply": reply}
    assert "idn" not in found and "banner" not in found
    assert rep.requests == [[b"", b"*IDN?"]]           # one question, as a REQ asks it; nothing else
    assert b"\x05READY\x0bSocket-Type\x00\x00\x00\x03REQ" in rep.readies[0]


@pytest.mark.parametrize("how, kw, said", [
    ("silent", {}, "it sent nothing more within 1.5 s"),
    ("closes", {}, "it closed the connection"),
    ("huge", {}, "it sent a frame of 1099511627776 bytes, not an answer to who it is"),
    ("flood", {}, "its reply went on past 16 frames"),
    ("bad", {}, "it sent a frame ZMTP doesn't have (flags 0xf0)"),
    ("error", {}, "it refused the connection: Invalid socket type!"),
    ("", {"socket_type": b"PUB"}, "its socket is PUB, which takes no questions: Kelvin asked it nothing"),
    ("", {"mechanism": b"CURVE"}, "it wants ZeroMQ's CURVE security, which Kelvin's look doesn't speak"),
    ("", {"major": 1}, "it speaks an older ZeroMQ (ZMTP revision 1, before 3.0), which Kelvin's look doesn't"),
])
def test_a_zeromq_peer_that_misbehaves_never_holds_the_look(rep_server, how, kw, said):
    """Short timeouts in all (the port's 0.8 s and 0.7 s more), a frame's size read before its body, and nothing asked
    of a socket that takes no requests or a peer that wants security: the look always ends, saying why."""
    import time
    from labharness import probe
    rep = rep_server(how, **kw)
    t0 = time.monotonic()
    found = probe.probe_port("127.0.0.1", rep.port, "idn")
    took = time.monotonic() - t0
    assert took < 2.5, "the look waited %.1f s" % took
    assert found["zmq"]["error"] == said
    if kw.get("socket_type") or kw.get("mechanism") or kw.get("major") or how in ("error", "closes"):
        assert rep.requests == []                      # nothing asked
    line = probe.what_answers(dict(found, hint="SCPI over a socket"))
    assert line.startswith("port %d: a ZeroMQ instrument" % rep.port)
    assert line.endswith("Kelvin has no card or connection for ZeroMQ instruments yet") and "SCPI" not in line


def test_what_a_zeromq_instrument_said_and_which_macs_are_macs():
    from labharness import probe
    said = probe.what_answers({"port": 5555, "hint": "SCPI over a socket: TCPIP0::10.0.0.7::5555::SOCKET", "zmq": {
        "version": "3.1", "socket": "REP", "reply": "ID Quantique,ID900,SN01234,v7.0.4"}})
    assert said == ("port 5555: a ZeroMQ (REQ) instrument; it says: ID Quantique,ID900,SN01234,v7.0.4. Kelvin has no "
                    "card or connection for ZeroMQ instruments yet")
    for written in ("70-B3-D5-65-78-CF", "70:b3:d5:65:78:cf", "70b3.d565.78cf", "70B3D56578CF", " 70-b3-d5-65-78-cf "):
        assert probe.mac_of(written) == "70:b3:d5:65:78:cf"
    for other in ("192.168.1.0/24", "192.168.1.*", "70-B3-D5-65-78", "70-B3-D5-65-78-CG", ""):
        assert probe.mac_of(other) is None
    assert not probe.zmtp_signature(b"\xff\xfd\x18\xff\xfd\x20\xff\xfd\x23\xff")   # a Telnet server's IAC DO ...


FIELD_ARP = """
Interface: 192.168.1.10 --- 0x9
  Internet Address      Physical Address      Type
  192.168.1.1           00-1a-2b-3c-4d-5e     dynamic
  192.168.1.78          70-b3-d5-65-78-cf     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
"""


class Page:
    """The Time Controller's web page on port 8080, as a connection to it answers."""

    def __init__(self):
        self.pending = b""

    def settimeout(self, seconds):
        pass

    def sendall(self, data):
        if data.startswith(b"GET "):
            self.pending = (b"HTTP/1.0 200 OK\r\nServer: lighttpd\r\nContent-Type: text/html\r\n\r\n"
                            b"<html><head><title>Time Controller</title></head></html>")

    def recv(self, n):
        import socket
        if not self.pending:
            raise socket.timeout()
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def close(self):
        pass


@pytest.fixture
def lab_network(monkeypatch, rep_server):
    """The field report's lab network, 192.168.1.0/24, this computer at .10: the IDQ Time Controller at .78 (ZeroMQ
    on 5555, its web page on 8080; MAC 70-B3-D5-65-78-CF), a router at .1 that answers no port instruments use. This
    computer's neighbour table (arp -a) has both once the sweep has knocked."""
    import socket
    pytest.importorskip("mcp")
    from labharness import probe, survey
    rep = rep_server()
    asked = []

    def connect(ip, port, seconds):
        if (ip, port) == ("192.168.1.78", 5555):
            return socket.create_connection(("127.0.0.1", rep.port), timeout=seconds)
        if (ip, port) == ("192.168.1.78", 8080):
            return Page()
        raise ConnectionRefusedError()
    monkeypatch.setattr(probe, "_connect", connect)
    monkeypatch.setattr(probe, "WIN", True)
    monkeypatch.setattr(probe, "network_adapters", lambda: [
        {"name": "Ethernet", "address": "192.168.1.10", "network": "192.168.1.0/24", "link_local": False}])
    monkeypatch.setattr(probe, "_run_text", lambda command: asked.append(command) or FIELD_ARP)
    monkeypatch.setattr(probe, "_udp", lambda: pytest.fail("a GigE Vision broadcast with no link-local network"))
    monkeypatch.setattr(survey, "network_adverts", lambda *a, **k: ([], []))
    return rep, asked


def test_find_on_network_says_what_the_time_controller_is_and_each_hosts_mac(lab_network, tmp_path):
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    from simlab import build_virtual_lab
    rep, asked = lab_network
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    said = rt.find_on_network()
    lines = said.splitlines()
    assert lines[0].endswith("and for instruments that announce themselves: 1 address answered")
    assert ("- 192.168.1.78: port 5555: a ZeroMQ (REQ) instrument; it says: ID Quantique,ID900,SN01234,v7.0.4. Kelvin "
            "has no card or connection for ZeroMQ instruments yet; MAC 70:b3:d5:65:78:cf") in lines
    assert rep.requests == [[b"", b"*IDN?"]]           # the sweep's knock asked nothing; the look asked once
    assert asked == [["arp", "-a", "-N", "192.168.1.10"]]   # read once, after the sweep
    assert not any("192.168.1.78" in a for a in rt.last_found)   # no address Kelvin could connect to it through

    inspected = rt.inspect_address("192.168.1.78")
    assert inspected.splitlines()[0] == ("192.168.1.78 (MAC 70:b3:d5:65:78:cf): 2 of the ports instruments use "
                                         "answer")
    assert "- port 5555: a ZeroMQ (REQ) instrument; it says: ID Quantique,ID900" in inspected
    assert "- port 8080: web page 'Time Controller' (lighttpd)" in inspected


def test_find_on_network_finds_a_device_by_its_mac(lab_network, tmp_path):
    """The field report: the Time Controller's configured address, 169.254.220.105, answered nothing, and its MAC
    couldn't be used to find it. Given its MAC (as network or as mac), the sweep fills this computer's neighbour table,
    which says where it is; that address is looked at on every port."""
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    from simlab import build_virtual_lab
    rep, asked = lab_network
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    said = rt.find_on_network("70-B3-D5-65-78-CF")
    lines = said.splitlines()
    assert lines[1] == ("MAC 70:b3:d5:65:78:cf is 192.168.1.78 (on Ethernet), in this computer's neighbour table after "
                        "the sweep: looked at on every port below")
    idq = next(ln for ln in lines if ln.startswith("- 192.168.1.78:"))
    assert "port 5555: a ZeroMQ (REQ) instrument; it says: ID Quantique,ID900" in idq
    assert "port 8080: web page 'Time Controller'" in idq and idq.endswith("MAC 70:b3:d5:65:78:cf")
    assert "looked for MAC 70:b3:d5:65:78:cf on the network (192.168.1.0/24)" in rt.events(5, "network")

    said = rt.find_on_network(mac="70:b3:d5:00:00:01")
    assert said.splitlines()[1].startswith("No address on 192.168.1.0/24 has MAC 70:b3:d5:00:00:01 in this computer's "
                                           "neighbour table after the sweep: the device is off, on a network this "
                                           "computer has no address on")
    assert rt.find_on_network(mac="70-B3-D5").startswith("'70-B3-D5' is not a MAC address")


# ---------------------------------------------------------------------- item 11: a late reply read as another's

class LateLockin:
    """The field report's SR830 on a slow GPIB bus (an NI GPIB-USB-HS that had lost its connection that day): a reply
    to a question in `slow` comes after Kelvin stopped waiting for it, and after the device clear that followed. It is
    in the lock-in's output queue `slow[question]` writes later, in front of the next question's own reply."""
    IDN = "Stanford_Research_Systems,SR830,s/n81118,ver1.07"

    def __init__(self):
        self.timeout, self.read_termination, self.write_termination = 3000, None, None
        self.out, self.later, self.sent, self.cleared = [], [], [], 0
        self.slow, self.drop = {}, set()
        self.answers = {"*IDN?": self.IDN, "SLVL?": "0.004", "FREQ?": "1000.0", "OUTP? 3": "1.2E-06", "*OPC?": "1"}

    def write(self, cmd):
        self.sent.append(cmd)
        later = [(n - 1, r) for n, r in self.later]
        self.out += [r for n, r in later if n <= 0]   # what came too late arrives now, in front
        self.later = [(n, r) for n, r in later if n > 0]
        if cmd in self.drop:   # its reply never comes (an instrument that discards a reply behind an unread one)
            self.drop.discard(cmd)
            return
        if "?" in cmd:
            reply = self.answers.get(cmd, "0")
            if callable(reply):
                reply = reply()
            if cmd in self.slow:
                self.later.append((self.slow.pop(cmd), reply))
            else:
                self.out.append(reply)

    def read(self):
        if not self.out:
            raise TimeoutError("VI_ERROR_TMO")
        return self.out.pop(0)

    def query(self, cmd, delay=None):
        self.write(cmd)
        return self.read()

    def clear(self):   # a device clear: what is queued now goes, not what comes later
        self.cleared += 1
        self.out = []

    def close(self):
        pass


class OneLockin:
    """A VISA resource manager with one instrument on it, the late lock-in, at GPIB0::8 (a GPIB-USB-HS's board)."""

    def __init__(self, res):
        self.res = res

    def open_resource(self, name, **kw):
        return self.res

    def list_resources(self, query="?*::INSTR"):
        return ("GPIB0::8::INSTR",)


def lockin(res, card="srs/sr830"):
    from labharness import builtin_registry
    from labharness.adapters import VisaBackend
    return VisaBackend("lockin", builtin_registry().get(card), "GPIB0::8::INSTR", rm=OneLockin(res))


def test_a_late_identity_is_never_read_as_the_next_questions_reply():
    """The monitor asked the lock-in who it is (a second at most, the bus slow) and its reply came late. Before the
    next command Kelvin cleared it and asked who it is: the late identity came first, was taken for the check's own,
    and the check's own reply then answered SLVL? ('Stanford_Research_Systems,SR830,...', said to be garbled)."""
    res = LateLockin()
    li = lockin(res)
    assert li.query("SLVL?") == "0.004"
    res.slow["*IDN?"] = 1
    with pytest.raises(TimeoutError):
        li.query("*IDN?")
    assert li.owed
    assert li.query("SLVL?") == "0.004"
    assert li.query("FREQ?") == "1000.0" and not li.owed
    assert li.said_as == LateLockin.IDN


def test_a_check_whose_own_identity_is_late_on_a_slow_bus_is_a_slow_answer_and_then_never_one_behind():
    """_back_in_step's own *IDN? timed out on the slow bus, after it had read off SLVL?'s late reply: it said the
    lock-in "answered '0.004' to *IDN?, not who it is ... check it, then reconnect it", and at the next command it took
    its own late identity for the new check's, one behind from then on (FREQ? got the identity)."""
    from labharness.adapters import timed_out
    res = LateLockin()
    li = lockin(res)
    res.slow["SLVL?"] = 1
    with pytest.raises(TimeoutError):
        li.query("SLVL?")
    res.slow["*IDN?"] = 1
    with pytest.raises(Exception) as e:
        li.query("FREQ?")
    assert timed_out(e.value) and "out of step" not in str(e.value)   # slow, not another instrument
    assert li.owed
    assert li.query("FREQ?") == "1000.0"
    assert li.query("SLVL?") == "0.004"


def test_a_connection_made_anew_on_a_slow_bus_is_never_one_behind():
    """The field report's lock-ins lost their connection (VI_ERROR_CONN_LOST) and were connected again on a slow bus:
    the first try's identity came late, the second try took it for its own, and its own then answered SLVL?."""
    res = LateLockin()
    li = lockin(res)
    li.broken = "VI_ERROR_CONN_LOST"
    res.slow["*IDN?"] = 1
    with pytest.raises(TimeoutError):
        li.reopen()
    assert li.broken
    assert li.reopen() == LateLockin.IDN and not li.broken
    assert li.query("SLVL?") == "0.004"


def test_an_identity_that_comes_later_still_is_read_past_and_nothing_is_asked_twice():
    """A late identity that arrives after Kelvin put the lock-in back in step (later than its check) is read where
    SLVL?'s reply should be: it is its own identity, not a reading, so it is out of step, not garbled. SLVL? was sent
    and answered, its reply right behind: it is read past the late one, never asked again."""
    res = LateLockin()
    li = lockin(res)
    res.slow["*IDN?"] = 2
    with pytest.raises(TimeoutError):
        li.query("*IDN?")
    assert li.query("SLVL?") == "0.004"
    assert res.sent == ["*IDN?", "*IDN?", "SLVL?"]      # read past it: SLVL? once
    assert not li.owed
    res.slow["*IDN?"] = 3                               # and for *OPC?'s wait, read on past it
    with pytest.raises(TimeoutError):
        li.query("*IDN?")
    assert li.query("FREQ?") == "1000.0"
    assert li.query_waiting("*OPC?", 5, lambda: False) == "1"
    res.slow["*IDN?"] = 2                               # the error queue's quick question after a timeout: its own
    with pytest.raises(TimeoutError):
        li.query("*IDN?")
    assert li.ask_after_timeout("ERRS?") == "0" and not li.owed


def test_a_card_without_an_identity_pattern_knows_its_identity_by_what_it_said():
    """'What it said at connect': an instrument whose card has no identity pattern is known by the identity it gave
    when asked who it is, and that, read where another reply should be, is out of step too."""
    from labharness.adapters import VisaBackend
    from labharness.cards import parse_card
    card = parse_card({"card": 1, "id": "acme/widget", "model": "Widget", "vendor": "ACME", "kind": "dmm",
                       "parameters": {"V": {"kind": "measure", "unit": "V", "get": "V?"}}})
    res = LateLockin()
    res.answers.update({"*IDN?": "ACME,WIDGET 9000,1,1.0", "V?": "1.5"})
    w = VisaBackend("widget", card, "GPIB0::8::INSTR", rm=OneLockin(res))
    assert w.query("*IDN?") == "ACME,WIDGET 9000,1,1.0" and w.said_as == "ACME,WIDGET 9000,1,1.0"
    res.out.append("ACME,WIDGET 9000,1,1.0")            # a late one, already there: read past
    assert w.query("V?") == "1.5" and res.sent.count("V?") == 1
    res.answers["V?"] = "1.0,2.0,3.0,4.0"               # numbers are a reading, never taken for an identity
    assert w.query("V?") == "1.0,2.0,3.0,4.0"


LOCKIN_LAB = """lab: NV
instruments:
  lockin:
    card: srs/sr830
    address: GPIB0::8::INSTR
"""


@pytest.fixture
def late_lab(tmp_path):
    from labharness.adapters import BUSES, set_resource_manager
    from test_field_test_2026_11_06 import runtime
    res = LateLockin()
    set_resource_manager(OneLockin(res))
    try:
        yield runtime(tmp_path, LOCKIN_LAB), res
    finally:
        set_resource_manager(None)
        BUSES.hung.clear()


def test_the_field_reports_garbled_slvl_alarm_never_comes(late_lab):
    """18:46:34: "lockin's reply to SLVL? arrived garbled ('Stanford_Research_Systems,SR830,s/n81118,ver1.07')": the
    lock-in's own identity, the late reply to the monitor's *IDN?, read where SLVL?'s should be."""
    from labharness.instrument import InstrumentLost
    rt, res = late_lab
    lab, monitor = rt.lab, rt.monitor
    inst = lab.instruments["lockin"]
    assert lab.read("lockin", "amplitude") == pytest.approx(0.004)
    res.slow["*IDN?"] = 1                               # the monitor's question whether it is there: late
    with pytest.raises(InstrumentLost):
        monitor._probe(inst)()
    assert lab.read("lockin", "amplitude") == pytest.approx(0.004)
    assert inst.health.garbles == 0
    monitor._check_health()
    assert not [a for a in monitor.alarms if a.kind == "comms"]


def test_a_late_reading_in_place_of_the_monitors_identity_question_is_out_of_step_not_garbled(late_lab):
    """The other way round: SLVL?'s late reply read where the monitor's *IDN? should be answered ('0.004') was said
    to be garbled. A reading where its identity should be is a late reply too: back in step, and asked once more."""
    from labharness.instrument import InstrumentLost
    rt, res = late_lab
    inst = rt.lab.instruments["lockin"]
    assert rt.lab.read("lockin", "amplitude") == pytest.approx(0.004)
    res.slow["SLVL?"] = 2
    with pytest.raises(InstrumentLost):
        rt.lab.read("lockin", "amplitude")
    rt.monitor._probe(inst)()
    assert inst.health.garbles == 0


def test_an_alarm_that_still_goes_up_says_a_late_reply_was_read_not_cables_or_noise(late_lab):
    from labharness.instrument import Garbled
    rt, res = late_lab
    lab, monitor = rt.lab, rt.monitor
    inst = lab.instruments["lockin"]
    assert lab.read("lockin", "amplitude") == pytest.approx(0.004)
    res.answers["SLVL?"] = LateLockin.IDN               # out of step again, after Kelvin put it back in step
    with pytest.raises(Garbled) as e:
        lab.read("lockin", "amplitude")
    said = str(e.value)
    assert said.startswith("lockin's reply to SLVL? was its own identity ('Stanford_Research_Systems,SR830,s/n81118,"
                           "ver1.07') on GPIB (address 8), again after Kelvin cleared it and asked who it is: a late "
                           "reply to an earlier question, read in this one's place")
    assert "not a loose cable or noise" in said and "a bad adapter or electrical noise" not in said
    monitor._check_health()
    [comms] = [a for a in monitor.alarms if a.kind == "comms"]
    assert comms.text == said
    assert inst.garble_text("SLVL?", LateLockin.IDN).startswith("lockin's reply to SLVL? was its own identity")
    assert "loose cable" in inst.garble_text("SLVL?", "0.0\x0704")   # noise is still noise


def test_its_identity_for_a_reading_through_any_connection_is_said_out_of_step_and_set_back_in_step(late_lab):
    """Through a connection without VisaBackend's own check of what came where, its identity for a reading still
    isn't called garbled, and the connection is put back in step before its next command where it can be."""
    from labharness.instrument import Garbled
    rt, res = late_lab
    inst = rt.lab.instruments["lockin"]
    assert rt.lab.read("lockin", "amplitude") == pytest.approx(0.004)

    class Plain:
        lock, owed = None, False

        def query(self, command):
            return LateLockin.IDN
    inst.backend = plain = Plain()
    with pytest.raises(Garbled, match=r"^lockin's reply to SLVL\? was its own identity .*not a loose cable or noise\. "
                                      r"Kelvin puts it back in step"):
        inst.read("amplitude")
    assert plain.owed


def acme(res, idn, query="*IDN?", parameters=None):
    """An instrument of a lab's own card (the review's: identity pattern idn) on GPIB, through VISA."""
    from labharness.adapters import VisaBackend
    from labharness.cards import parse_card
    card = parse_card({"card": 1, "id": "lab/dmm", "model": "DMM", "vendor": "ACME", "kind": "dmm",
                       "identify": {"idn": idn, "query": query},
                       "parameters": parameters or {"V": {"kind": "measure", "unit": "V", "get": "READ?"}}})
    return VisaBackend("dmm", card, "GPIB0::8::INSTR", rm=OneLockin(res))


def test_a_question_that_may_change_something_is_never_asked_twice_its_reply_read_past_the_late_one():
    """Review of 2026-11-15: asked once more after a late identity, a relative move went twice
    (['MOVE:REL 1;*OPC?', '*IDN?', 'MOVE:REL 1;*OPC?']) and the error the first SYST:ERR? took was lost (-113 read, not
    -222). The question was sent and done, so its reply is right behind the late one: read past it."""
    res = LateLockin()
    stage = acme(res, "^ACME,STAGE")
    res.answers.update({"*IDN?": "ACME,STAGE 1,1,1.0", "MOVE:REL 1;*OPC?": "1"})
    res.out.append("ACME,STAGE 1,1,1.0")                # late, already there
    assert stage.query("MOVE:REL 1;*OPC?") == "1"
    assert res.sent == ["MOVE:REL 1;*OPC?"] and not stage.owed
    errors = ['-222,"Data out of range"', '-113,"Undefined header"']
    res.answers["SYST:ERR?"] = lambda: errors.pop(0) if errors else '0,"No error"'
    res.out.append("ACME,STAGE 1,1,1.0")
    assert stage.query("SYST:ERR?") == '-222,"Data out of range"'
    assert stage.query("SYST:ERR?") == '-113,"Undefined header"'


def test_a_question_whose_reply_didnt_follow_the_late_one_is_put_back_in_step_and_not_asked_again():
    """A buffer's read (DATA:REM? takes its readings away) whose own reply didn't follow the late identity (an
    instrument that discards it): back in step, and not sent again, which would take more readings away. Said so."""
    from labharness.adapters import OutOfStep
    res = LateLockin()
    dmm = acme(res, "^ACME,DMM")
    res.answers.update({"*IDN?": "ACME,DMM 9,1,1.0", "DATA:REM?": "1.0,2.0,3.0"})
    res.out.append("ACME,DMM 9,1,1.0")
    res.drop.add("DATA:REM?")
    with pytest.raises(OutOfStep, match=r"didn't ask DATA:REM\? again, as that may change what the instrument does") \
            as e:
        dmm.query("DATA:REM?")
    assert res.sent == ["DATA:REM?", "*IDN?"] and not e.value.asked_again   # back in step, once: who it is
    assert not dmm.owed and dmm.query("DATA:REM?") == "1.0,2.0,3.0"


def test_a_plain_reading_whose_reply_didnt_follow_is_asked_once_more_and_a_raw_query_is_not(late_lab):
    """Only a card's plain reading (its own get: no acquisition, no trace) is asked once more, which only reads again.
    A raw query a person approved once is never sent twice: its alarm says it wasn't, and why."""
    from labharness.instrument import Garbled
    rt, res = late_lab
    inst = rt.lab.instruments["lockin"]
    assert inst.query_raw("*IDN?") == LateLockin.IDN    # what it says as itself: known (said_as)
    res.out.append(LateLockin.IDN)
    res.drop.add("SLVL?")
    assert rt.lab.read("lockin", "amplitude") == pytest.approx(0.004)
    assert res.sent[-3:] == ["SLVL?", "*IDN?", "SLVL?"] and inst.health.garbles == 0
    res.out.append(LateLockin.IDN)
    res.drop.add("FREQ?")
    with pytest.raises(Garbled) as e:
        inst.query_raw("FREQ?")
    assert res.sent[-2:] == ["FREQ?", "*IDN?"]          # not sent again
    assert "and that question's own reply didn't follow it" in str(e.value)
    assert "didn't send FREQ? again, as that may change what it does: check whether it was done" in str(e.value)


@pytest.mark.parametrize("idn, command, reply", [
    ("2700", "READ?", "+1.27001E+00VDC,+12.345SECS,+5RDNG#"),    # the pattern inside a reading with units
    (".*", "FUNC?", '"VOLT:DC"'),                                  # a pattern that fits anything
    ("DMM", "SYST:VERS?", "1999.0"),                               # numbers
])
def test_a_loose_identity_pattern_never_throws_a_genuine_reply_away(idn, command, reply):
    """Review of 2026-11-15: a lab card's pattern found anywhere in a reply (idn: "2700" in '+1.27001E+00VDC,...') made
    the genuine reading a late identity: thrown away and asked again; with idn ".*" every reply with letters was asked
    twice, then said out of step. Only an identity at the reply's start, in an identity's form, counts where what it
    says as itself isn't known yet; where it is, only that, exactly."""
    res = LateLockin()
    res.answers.update({"*IDN?": "ACME,DMM 9,1,1.0", command: reply})
    dmm = acme(res, idn)
    assert dmm.query(command) == reply and res.sent == [command]
    dmm.query("*IDN?")
    res.answers[command] = "ACME,DMM 9,0,9.9"           # identity-like, but not what it says as itself
    assert dmm.query(command) == "ACME,DMM 9,0,9.9" and res.sent.count(command) == 2


def test_a_reading_that_reads_as_itself_is_kept_and_what_it_said_as_itself_is_known_whatever_a_parse_finds(late_lab):
    """Where what it says as itself isn't known, a reply that reads as the reading asked for is never taken for a late
    identity (the review's rule). Where it is known, that exact text is no reading, whatever a card's parse would find
    in it (a serial number read as a value)."""
    from labharness.adapters import an_identity
    rt, res = late_lab
    inst = rt.lab.instruments["lockin"]
    backend = inst.backend
    assert not backend.said_as
    assert not an_identity("+1.27001E+00VDC,+12.345SECS,+5RDNG#", "2700")   # not at the start
    assert an_identity("Stanford_Research_Systems,SR830,s/n81118,ver1.07", "Stanford_Research_Systems,SR8[13]0")
    assert not an_identity('"VOLT:DC 10,0.001"', "VOLT")               # not at the start (a quote first)
    assert not an_identity("SR830", "SR8[13]0")                        # no identity's comma
    assert an_identity("ACME,DMM 9,1,1.0", "", said="ACME,DMM 9,1,1.0") and not an_identity("1.0", ".*")
    from labharness.adapters import reading
    with reading(lambda text: True):                                   # it reads as the reading asked for
        assert not backend._in_its_place("OUTP? 3", LateLockin.IDN)    # (what it says as itself not known yet)
    inst.query_raw("*IDN?")
    assert backend.said_as == LateLockin.IDN
    with reading(lambda text: True):                                   # known: that exact text is no reading,
        assert backend._in_its_place("OUTP? 3", LateLockin.IDN)        # whatever a card's parse would find in it
    assert not backend._in_its_place("OUTP? 3", "Stanford_Research_Systems,SR830,s/n00001,ver1.07")


class Streams:
    """A gauge controller that sends its readings by itself, as Pfeiffer's do from power-up, and no <ACK> yet."""
    timeout, read_termination, write_termination = 3000, None, None

    def __init__(self):
        self.sent = []

    def write(self, cmd):
        self.sent.append(cmd)

    def write_raw(self, data):
        self.sent.append(data)

    def read(self):
        return "0,1.0000E-03"

    def read_raw(self):
        raise TimeoutError("VI_ERROR_TMO")

    def close(self):
        pass


def test_an_acknowledge_that_may_still_come_is_never_read_as_the_next_reply():
    """Two more ways a reply could stay queued without Kelvin knowing: an <ACK> not come after 20 lines, and a reply
    read where the ack should be. Each puts the instrument back in step before its next command."""
    from labharness import builtin_registry
    from labharness.adapters import AdapterError, VisaBackend
    from test_hardware import Bus, Device
    reg = builtin_registry()
    gauge = VisaBackend("gauge", reg.get("pfeiffer/tpg36x"), "ASRL5::INSTR", rm=OneLockin(Streams()))
    with pytest.raises(AdapterError, match="did not acknowledge"):
        gauge.query("PR1")
    assert gauge.owed
    obis = Device("coherent/obis", ack="OK", name="obis")
    laser = VisaBackend("laser", reg.get("coherent/obis"), "serial://COM7", Bus({"ASRL7::INSTR": obis}))
    obis.ack = "ERR-400"
    with pytest.raises(AdapterError, match="instead of 'OK'"):
        laser.write("SOURce:AM:STATe OFF")
    assert laser.owed


def test_a_binary_block_kelvin_couldnt_read_is_dropped_before_the_next_command():
    """A block whose header or length didn't read (ValueError) left the rest of it queued, for the next command."""
    from labharness.adapters import AdapterError
    res = LateLockin()

    def query_binary_values(command, datatype, is_big_endian, container):
        raise ValueError("Could not find valid header")
    res.query_binary_values = query_binary_values
    li = lockin(res)
    before = res.cleared
    with pytest.raises(AdapterError, match="did not answer with a block"):
        li.query_binary("CURV?", "int16")
    assert res.cleared == before + 1
