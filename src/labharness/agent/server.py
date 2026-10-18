"""MCP server: lets Claude, GPT, Gemini or any MCP client drive a lab through the safety gate.

    python -m labharness.agent                     # virtual lab over stdio, opens the approval page
    python -m labharness.agent --speed 10          # virtual lab in real time ×10 instead of instant
"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path
from typing import List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .._version import __version__
from ..clock import ScaledClock, SimClock
from ..labconfig import load_envelope
from ..virtual_lab import build_virtual_lab, demo_envelope
from .approval import ApprovalServer, WebApprover
from .runtime import LabRuntime

INSTRUCTIONS = """\
You are connected to a physics lab through Kelvin.
- Measurement and configuration (reading values, time constants, averaging, spans, moving a delay stage...) run immediately.
- Changes that drive the sample harder (more power, voltage, current, field, temperature, outputs on) wait for a human to approve them on the Kelvin approval page, unless the project envelope covers them. Your call blocks until they answer or the request times out. You cannot approve requests yourself; do not try.
- A person may switch on auto mode. Then drive changes and runs that stay well inside the lab's limits (and pass
  a clean dry run) go ahead without asking; lab_status says when it is on. Only a person can switch it.
- Values beyond the lab's limits, firmware/calibration commands and interlock violations are refused. Do not try to work around a refusal; tell the user.
- For sweeps, write a plan and call simulate_plan, then run_plan: one approval, dry run first, everything logged.
  run_plan analyses the data itself; report its assessment and key numbers (with uncertainties) rather than raw rows.
- Units are the ones describe_instrument shows. Call describe_instrument before using an instrument for the first time.
- If something looks wrong, stop and tell the user. Moving a drive back toward safe runs without asking.
- Before a long or unattended run, offer to watch the readings that matter (watch); alarms reach the user even when
  they are away. lab_status lists active alarms; mention any you see.
- Reply to the user briefly: the result first, then the next step."""

READ = ToolAnnotations(readOnlyHint=True)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def build_server(rt: LabRuntime) -> MCPServer:
    server = MCPServer("kelvin", instructions=INSTRUCTIONS, version=__version__)

    @server.tool(annotations=READ, structured_output=False)
    def lab_status() -> str:
        """Every instrument's current values. * marks drives away from their safe value."""
        return rt.lab_status()

    @server.tool(annotations=READ, structured_output=False)
    def describe_instrument(instrument: str) -> str:
        """Parameters of one instrument: measure or drive, unit, limits, safe value, ramp rate, guards."""
        return rt.describe(instrument)

    @server.tool(annotations=READ, structured_output=False)
    def get_value(instrument: str, parameter: str) -> str:
        """Read one parameter (a trace is summarized; record traces with a plan to keep them)."""
        return rt.get(instrument, parameter)

    @server.tool(annotations=ACT, structured_output=False)
    def set_value(instrument: str, parameter: str, value: float | bool | str) -> str:
        """Set one parameter. Measurement settings apply at once. Drive increases wait for human
        approval (unless inside the project envelope), then ramp at a safe rate while guards are watched."""
        return rt.set(instrument, parameter, value)

    @server.tool(annotations=ACT, structured_output=False)
    def send_raw(instrument: str, command: str) -> str:
        """Send a raw instrument command (e.g. SCPI). Same rules as set_value: queries and configuration
        run; anything touching a drive goes through the gate; blocked commands are refused."""
        return rt.raw(instrument, command)

    @server.tool(annotations=ACT, structured_output=False)
    def undo_last() -> str:
        """Undo the last successful set_value (through the same safety rules)."""
        return rt.undo()

    @server.tool(annotations=ACT, structured_output=False)
    def watch(reading: str, above: Optional[float] = None, below: Optional[float] = None, for_seconds: float = 0,
              note: str = "") -> str:
        """Watch a reading (e.g. tc.T_A) and alert the user when it goes above/below a value for `for_seconds`.
        Only reads and notifies; the lab owner's watches may also act. Returns the watch id."""
        try:
            v = rt.add_watch(reading, above, below, for_seconds, note, source="agent")
        except (KeyError, ValueError) as e:
            return "not watched: %s" % (e.args[0] if e.args else e)
        return "watching %s (id %s)" % (v["rule"], v["id"])

    @server.tool(annotations=READ, structured_output=False)
    def watches() -> str:
        """What is being watched, the readings now, and any active alarms."""
        return rt.watches_text()

    @server.tool(annotations=ACT, structured_output=False)
    def unwatch(watch_id: str) -> str:
        """Stop a watch you or the user added (the lab owner's watches stay)."""
        try:
            rt.remove_watch(watch_id, source="agent")
        except (KeyError, PermissionError) as e:
            return "not removed: %s" % (e.args[0] if e.args else e)
        return "no longer watching (%s)" % watch_id

    @server.tool(annotations=READ, structured_output=False)
    def scan_instruments() -> str:
        """Find instruments connected to this computer (GPIB, USB, LAN, serial) and match them to cards. It only
        asks each instrument who it is. Adding one to the lab is the user's decision."""
        return rt.scan()

    @server.tool(annotations=READ, structured_output=False)
    def health_check(instruments: Optional[List[str]] = None) -> str:
        """Identity, error queue, sanity readings and drives inside limits, for all or some instruments."""
        return rt.health(instruments)

    @server.tool(annotations=READ, structured_output=False)
    def validate_plan(plan: str) -> str:
        """Check a plan (YAML text) and say what it would need approval for. Format:
        plan: name / preconditions: ["tc.T_sample < 2.5"] / setup: {instr.param: value} /
        sweep: {instr.param: {from, to, step} | {from, to, points} | {from, to, points, log: true} | {values: [...]},
                plus optional back: true (there and back), settle: s,
                stabilize: {read: instr.param, within: change, for: s, timeout: s}}
               (several keys = nested loops, outer first) /
        repeat: n / time: {every: s, for: s} (innermost time series) / settle: s / measure: [instr.param] /
        after: {instr.param: value} / on_abort: {...}"""
        return rt.validate(plan)

    @server.tool(annotations=READ, structured_output=False)
    def simulate_plan(plan: str) -> str:
        """Dry-run a plan (YAML text) on a simulated copy of the lab. The real lab is not touched."""
        return rt.simulate(plan)

    @server.tool(annotations=ACT, structured_output=False)
    def run_plan(plan: str) -> str:
        """Run a plan (YAML text): validate, health check, dry run, one human approval for its drive
        ranges, run, log, then analyse automatically. Returns the assessment, key results and the run id."""
        return rt.run(plan)

    @server.tool(annotations=READ, structured_output=False)
    def analyze_run(run_id: str, column: Optional[str] = None, model: Optional[str] = None) -> str:
        """Re-analyse a recorded run. column: one measured instr.param; model: linear, lorentzian,
        gaussian or exponential to force that fit (default: chosen automatically)."""
        return rt.analyze(run_id, column, model)

    @server.tool(annotations=READ, structured_output=False)
    def list_runs(limit: int = 10) -> str:
        """Most recent runs with status and point counts."""
        return rt.list_runs(limit)

    @server.tool(annotations=READ, structured_output=False)
    def get_run_data(run_id: str, columns: Optional[List[str]] = None, max_rows: int = 50) -> str:
        """A run's data as CSV (evenly thinned to max_rows; trace columns show as trace[n])."""
        return rt.run_data(run_id, columns, max_rows)

    @server.tool(annotations=READ, structured_output=False)
    def get_trace(run_id: str, row: int, column: str, max_points: int = 200) -> str:
        """One recorded trace from a run as x,y CSV (thinned to max_points)."""
        return rt.trace(run_id, row, column, max_points)

    @server.tool(annotations=READ, structured_output=False)
    def recent_events(n: int = 20) -> str:
        """The last n entries of the lab's event log (commands, approvals, trips, runs)."""
        return rt.events(n)

    @server.tool(annotations=ACT, structured_output=False)
    def wait(seconds: float) -> str:
        """Let time pass (e.g. for thermalization), up to 3600 s."""
        return rt.wait(seconds)

    return server


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m labharness.agent", description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=str(Path.home() / "Kelvin" / "virtual-lab"),
                    help="event log and run folders (default: ~/Kelvin/virtual-lab)")
    ap.add_argument("--envelope", help="project envelope YAML (default: the demo envelope)")
    ap.add_argument("--no-envelope", action="store_true", help="ask for every drive increase")
    ap.add_argument("--speed", type=float, default=0, help="0 = instant simulated time (default); N = N× real time")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--ui-port", type=int, default=8765, help="approval page port (a free one is used if busy)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the approval page")
    ap.add_argument("--ui-url-file", help="also write the approval page URL to this file (kiosks, tests)")
    ap.add_argument("--approval-timeout", type=float, default=120, help="seconds before an unanswered request is declined")
    ap.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    ap.add_argument("--mcp-port", type=int, default=8766, help="port for --transport streamable-http (127.0.0.1 only)")
    args = ap.parse_args(argv)

    lab = build_virtual_lab(clock=SimClock() if args.speed == 0 else ScaledClock(args.speed), seed=args.seed)
    envelope = None
    if not args.no_envelope:
        envelope = load_envelope(args.envelope, lab.config, lab.registry) if args.envelope else demo_envelope(lab)
    approver = WebApprover(timeout=args.approval_timeout)
    runtime = LabRuntime(lab, approver, args.data_dir, envelope)
    runtime.monitor.start()
    ui = ApprovalServer(runtime, port=args.ui_port).start()

    print("Kelvin: approval page %s" % ui.url, file=sys.stderr, flush=True)
    print("Kelvin: data in %s" % runtime.data_dir, file=sys.stderr, flush=True)
    if args.ui_url_file:
        Path(args.ui_url_file).write_text(ui.url, encoding="utf-8")
    if not args.no_browser:
        webbrowser.open(ui.url)

    server = build_server(runtime)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host="127.0.0.1", port=args.mcp_port)


if __name__ == "__main__":
    main()
