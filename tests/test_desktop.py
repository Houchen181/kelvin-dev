"""The desktop launcher, without a window (the release builds run the same self-test on the packaged app)."""
import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

from labharness.desktop import main  # noqa: E402


def test_self_test_starts_the_portal_and_cleans_up(tmp_path):
    assert main(["--self-test", "--home", str(tmp_path)]) == 0
    assert (tmp_path / "settings.json").exists()        # access key created on first start
    assert not (tmp_path / "app.json").exists()         # the single-instance lock is removed on exit
    assert "self-test: ok" in (tmp_path / "kelvin.log").read_text(encoding="utf-8")
