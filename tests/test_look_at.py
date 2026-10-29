"""look_at: a run's plots, a 2-D scan's map and camera frames as pictures for models that see, drawn by Kelvin
(pictures.py), each with a caption of its numbers."""
import base64
import re

import pytest

pytest.importorskip("numpy")
pytest.importorskip("mcp")

import anyio  # noqa: E402

from mcp import Client  # noqa: E402

from labharness import ScriptedApprover, frames, pictures  # noqa: E402
from simlab import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.drivers import MODULES  # noqa: E402
from labharness.homelab import build_lab  # noqa: E402
from test_more_instruments import CAM_LAB, FakePylon  # noqa: E402


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(frames, "FOLDER", tmp_path / "frames")
    yield
    MODULES.clear()


def picture(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return pictures.png_size(data)


def test_a_runs_plots_and_a_maps_colors(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 200), tmp_path / "data")
    assert "done" in rt.run("plan: gate\nsweep: {smu.V: {from: 0, to: 1, points: 21}}\nmeasure: [lockin.X, lockin.Y]")
    data, caption = rt.look("last")
    assert picture(data) == (960, 860) and caption.startswith("Run ") and "gate, done, 21 of 21 points" in caption
    assert "Plot 1 (top): lockin.X" in caption and "Plot 2 (bottom): lockin.Y" in caption and "vs smu.V (V)" in caption
    data, caption = rt.look("last", column="lockin.Y")
    assert picture(data) == (960, 560) and "Plot: lockin.Y" in caption and "lockin.X" not in caption
    assert rt.look("last", column="tc.T")[0] is None
    assert "measured lockin.X, lockin.Y, not tc.T" in rt.look("last", column="tc.T")[1]
    assert rt.look("")[0] is None and rt.look("no-such-run")[1].startswith("nothing to show: no run")

    plan = "plan: fan\nsweep: {smu.V: {from: 0, to: 1, points: 11}, laser.power: {from: 0, to: 0.001, points: 5}}\n" \
           "measure: [counter.rate]"
    fan = rt.run(plan)   # by its ID: two instant runs can start in the same second
    assert "done" in fan
    data, caption = rt.look(re.search(r"run (\S+) done", fan).group(1))
    assert picture(data) == (960, 640) and "Map: counter.rate as color over laser.power (horizontal" in caption
    assert "and smu.V (vertical, increasing upward" in caption and "5 x 11 points" in caption


def test_camera_frames_from_a_run_and_read_now(tmp_path):
    MODULES["pylon"] = FakePylon()
    rt = LabRuntime(build_lab(CAM_LAB), ScriptedApprover([True] * 4), tmp_path / "data", None)
    assert "done" in rt.run("plan: images\nsweep: {cam.exposure: {values: [0.001, 0.002]}}\n"
                            "measure: [cam.frame, cam.mean]")
    run_id = rt.runs_index(1)[0]["run_id"]
    data, caption = rt.look(run_id, column="cam.frame")                # packed into run.h5 once the run ended
    assert picture(data) == (4, 3) and "the last frame of cam.frame (point 2 of 2)" in caption
    assert "Frame: 4 x 3 pixels" in caption and "brightest pixel at" in caption
    data, caption = rt.look(run_id, frame="run.h5:/frames/cam.frame[0]")
    assert picture(data) == (4, 3)
    data, caption = rt.look(camera="cam.frame")                        # a new frame, saved in the frames folder
    assert picture(data) == (4, 3) and caption.startswith("cam.frame, read now. Frame:")
    assert rt.look(camera="cam.exposure")[1].startswith("cam.exposure is not a camera frame")
    stray = tmp_path / "elsewhere.npy"
    stray.write_bytes(b"")
    assert rt.look(frame=str(stray))[0] is None and "Kelvin's own frames" in rt.look(frame=str(stray))[1]
    assert rt.look(run_id, frame="../../elsewhere.npy")[1].startswith("nothing to show: not a frame of this run")


def test_the_tool_sends_the_picture_and_its_caption(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 2), tmp_path / "data")
    assert "done" in rt.run("plan: gate\nsweep: {smu.V: {from: 0, to: 1, points: 11}}\nmeasure: [lockin.X]")

    async def main():
        async with Client(build_server(rt)) as client:
            return (await client.call_tool("look_at", {"run_id": "last"})).content
    content = anyio.run(main)
    assert [c.type for c in content] == ["image", "text"] and content[0].mime_type == "image/png"
    assert picture(base64.b64decode(content[0].data)) == (960, 560) and "Plot: lockin.X" in content[1].text


def test_the_font_and_the_png_writer():
    np = pytest.importorskip("numpy")
    c = pictures.Canvas(60, 20)
    c.text(2, 14, "1.5 µV", 14, "000000")
    shown = np.frombuffer(__import__("zlib").decompress(c.png()[41:-12]), np.uint8)
    assert shown.min() < 60                                             # dark text drawn on white
    assert pictures.measure("1.5 µV", 14) == 6 * 6 * 2
    assert pictures.png(np.zeros((2, 3), np.uint8))[:8] == b"\x89PNG\r\n\x1a\n"
    assert pictures.grid_of([{"a": 1, "b": 1, "z": 2}, {"a": 1, "b": "x", "z": 3}], "a", "b", "z") is None
