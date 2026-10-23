"""One version everywhere: a version bump that forgets a place fails here, before anything is tagged."""
import re
from pathlib import Path

from labharness._version import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_the_version_is_the_same_everywhere():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'(?m)^version = "([^"]+)"', pyproject).group(1) == __version__
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    button = re.search(r"Download Kelvin (\S+) for Windows", readme)
    assert button, "the README's download button should name the version it gives"
    assert button.group(1) == __version__, "README.md: the download button says %s, Kelvin is %s" % (
        button.group(1), __version__)
