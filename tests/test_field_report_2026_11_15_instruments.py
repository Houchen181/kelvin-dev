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
