"""The Claude Code bridge ends everything it started (review of 0.3.45, section 8).

An npm install's `claude` is a launcher (claude.CMD) running node. Stop and the time limit killed the launcher alone,
and node finished its answer, or went on working the screen. `claude auth status` waited for its output's pipe,
which a child of the launcher kept open, so the Settings page's look at the bridge hung for as long as the child
lived. The fakes here are such launchers: a Python program that starts a child holding its output."""
import asyncio
import json
import sys
import textwrap
import time

import pytest

from labharness import claudecode

psutil = pytest.importorskip("psutil")

LAUNCHER = textwrap.dedent("""
    import json, subprocess, sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=sys.stdout)
    print(json.dumps({"type": "child", "pid": child.pid}), flush=True)
    child.wait()
""")

SIGNED_IN = textwrap.dedent("""
    import json, subprocess, sys
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), flush=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=sys.stdout)
    with open(sys.argv[1], "w") as f:
        f.write(str(child.pid))
""")

NEVER_ANSWERS = textwrap.dedent("""
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=sys.stdout)
    with open(sys.argv[1], "w") as f:
        f.write(str(child.pid))
    time.sleep(60)
""")


def gone(pid, within=10.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            if not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


def end(pid):
    try:
        psutil.Process(pid).kill()
    except psutil.Error:
        pass


def test_stop_ends_the_launcher_and_what_it_started(tmp_path):
    script = tmp_path / "claude.py"
    script.write_text(LAUNCHER, encoding="utf-8")
    seen, stop = {}, {"now": False}

    async def run():
        with pytest.raises(claudecode.StopRequested):
            async for event in claudecode.stream([sys.executable, str(script)], [], "", tmp_path,
                                                 should_stop=lambda: stop["now"], timeout=60):
                if event.get("type") == "child":
                    seen["pid"] = event["pid"]
                    stop["now"] = True   # the person presses Stop while node answers
    try:
        asyncio.run(run())
        assert gone(seen["pid"]), "the launcher's child still runs after Stop"
    finally:
        if "pid" in seen:
            end(seen["pid"])


def test_auth_status_never_waits_for_a_pipe_a_child_holds(tmp_path):
    script, pid_file = tmp_path / "claude.py", tmp_path / "child.pid"
    script.write_text(SIGNED_IN, encoding="utf-8")
    t0 = time.monotonic()
    try:
        state = claudecode.status([sys.executable, str(script), str(pid_file)], timeout=10)
        assert time.monotonic() - t0 < 8   # the launcher ended at once; its child holds the pipe for 30 s
        assert state == {"signed_in": True, "method": "claude.ai"}
    finally:
        if pid_file.exists():
            end(int(pid_file.read_text()))


def test_auth_status_that_never_answers_is_ended_with_its_children(tmp_path):
    script, pid_file = tmp_path / "claude.py", tmp_path / "child.pid"
    script.write_text(NEVER_ANSWERS, encoding="utf-8")
    t0 = time.monotonic()
    try:
        state = claudecode.status([sys.executable, str(script), str(pid_file)], timeout=2)
        assert time.monotonic() - t0 < 8
        assert state["signed_in"] is False and "didn't answer within 2 s" in state["error"]
        assert pid_file.exists() and gone(int(pid_file.read_text()))
    finally:
        if pid_file.exists():
            end(int(pid_file.read_text()))


def test_the_launcher_alone_never_answers_for_the_whole_tree():
    """kill_tree on a program that already ended does nothing, and raises nothing."""
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    claudecode.kill_tree(p)
    assert json.dumps({"ok": True})   # nothing raised
