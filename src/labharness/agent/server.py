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

from .. import carddraft
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
- For sweeps, write a plan and call run_plan directly. It checks the plan, health-checks the instruments it uses,
  asks once for its drive ranges, runs, logs and analyses the data. Report its assessment and key numbers (with
  uncertainties) rather than raw rows. A dry run (simulate_plan, or run_plan with dry_run: true) is not a routine
  step, even for a plan that drives: only when the person asks, or to estimate how long a long plan takes.
- Units are the ones describe_instrument shows. Call describe_instrument before using an instrument for the first time.
- Before an involved scan on a real instrument (buffers, triggering, sweep or list modes, fast acquisition, ranges,
  filters, settling), read its manual: instrument_info, then search_manual for the commands and their limits. Work
  from what the manual says, cite the page, and say so if it does not cover something.
- If something looks wrong, stop and tell the user. Moving a drive back toward safe runs without asking.
- Before a long or unattended run, offer to watch the readings that matter (watch); alarms reach the user even when
  they are away. lab_status lists active alarms; mention any you see.
- To plan an experiment, first look at what is known: search_papers, read the most relevant one or two, then
  propose parameters with reasons and cite them as [Author Year](url). Never invent a reference or a number.
- Spin experiments (NV centres; atoms or ions with an AOM and a microwave or RF drive) go in this order: CW ODMR
  (sweep the MW frequency, read the counter's rate) to find the resonance; Rabi (pulse generator protocol rabi,
  sweep its tau, read the counter's signal, reference and contrast) for the pi time; then pulsed ODMR with that
  t_pi, Ramsey (T2*), Hahn echo (T2) and T1. Before running a sequence, show it with show_sequence and say its
  period and duty cycles. Start with low MW and laser power and raise them within the limits. Playing a sequence
  (the generator's running) pulses light and microwaves onto the sample: it is a drive.
- Every scan is filed under the device (sample) mounted in the lab and its project. When the person says which
  sample is in, call mount_device; a plan can name another device on the same chip with device: <name>. Reports
  keep themselves up to date: each scan's report, each device's report and the project's master report.
- To line up scans for later (overnight, or one after another), use the queue: queue_add each plan, and steps
  between them (step: name, set: {...}, stabilize: {read, within, for, timeout}, wait: seconds), then queue_start.
  A person approves the whole queue once; it runs in the background and pauses on a failure or Stop. Say what is
  queued and roughly how long it takes; queue_show follows it.
- For an instrument Kelvin has no card for: get its manual's command reference or the maker's example scripts
  (pasted, or read_examples on a file or folder the person names), read card_guide, and write the card from that
  text only. check_card until it is valid; try_card on the instrument (it only reads) and compare the readings with
  what the person sees; fix and try again; then save_card. A person adds it to the lab and reviews it.
- Work like an experimentalist at the bench, not like a programmer testing code:
  - One plan answers one question. Don't stage checks around it: no validate_plan or simulate_plan before run_plan
    (use them only when the person asks what a plan would do or how long it takes), no test or verification scans,
    no running the same plan again to make sure.
  - set_value and get_value report the value read back, and a run reports its data and analysis: don't read values
    again to confirm them.
  - Check an instrument before you first use it, not before every step; health_check again only after an error or
    an alarm.
  - When a command or a run fails or is refused, don't try variations to get past it. Say what happened and what you
    suggest, and let the person decide.
  - Don't write code, scripts or tests, or re-analyse data in code, unless the person asks: use run_plan's analysis
    and analyze.
  - Plan the whole measurement, run it, report. A coarse scan before a fine one is good practice when it saves time;
    the physics decides how many measurements there are, not a habit of testing.
- Reply to the user briefly: the result first, then the next step."""

READ = ToolAnnotations(readOnlyHint=True)
WEB = ToolAnnotations(readOnlyHint=True, openWorldHint=True)   # reads the public literature, never the lab
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
    def instrument_info(instrument: str) -> str:
        """A real instrument's folder: its identity (maker, model, serial number, firmware), address and connection
        settings, every setting it had when Kelvin set it up, and whether its manual is there to search."""
        return rt.instrument_info(instrument)

    @server.tool(annotations=READ, structured_output=False)
    def search_manual(instrument: str, query: str, max_passages: int = 4) -> str:
        """Search the instrument's own manual (the maker's PDF in its folder) and get the best passages with page
        numbers. Use the manual's terms, e.g. "trigger buffer", "sweep list mode", "time constant settling"."""
        return rt.search_manual(instrument, query, max_passages)

    @server.tool(annotations=READ, structured_output=False)
    def show_sequence(protocol: str, generator: str = "", t_pi: Optional[float] = None, tau: Optional[float] = None,
                      t_laser: Optional[float] = None, t_gate: Optional[float] = None,
                      t_wait: Optional[float] = None) -> str:
        """The timing a pulse generator would play, checked, without playing it: per-channel diagram (laser/AOM, MW
        switch, counter gate), period, MW and laser duty cycles, and problems. protocol: cw, pulsed_odmr, rabi,
        ramsey, hahn_echo or t1. Times in seconds; any left out come from the named generator's settings."""
        return rt.pulse_sequence(protocol, {"t_pi": t_pi, "tau": tau, "t_laser": t_laser, "t_gate": t_gate,
                                            "t_wait": t_wait}, generator)

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
        Only reads and notifies; watches written in the lab config may also act. Returns the watch id."""
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
        """Stop a watch you or the user added (watches from the lab config stay)."""
        try:
            rt.remove_watch(watch_id, source="agent")
        except (KeyError, PermissionError) as e:
            return "not removed: %s" % (e.args[0] if e.args else e)
        return "no longer watching (%s)" % watch_id

    @server.tool(annotations=READ, structured_output=False)
    def card_guide() -> str:
        """How to write a card for an instrument Kelvin does not know: the format, the rules, and a full example.
        Read it before writing a card from a manual."""
        return carddraft.guide()

    @server.tool(annotations=READ, structured_output=False)
    def check_card(card: str) -> str:
        """Check a card (YAML text) without saving it: what is wrong, or what it offers and what to improve."""
        return rt.check_card(card)

    @server.tool(annotations=READ, structured_output=False)
    def read_examples(path: str) -> str:
        """Read an instrument maker's example scripts, SDK header files and READMEs (Python, MATLAB, C/C#, notebooks,
        text) from a file or folder on this computer that the person named, to write a card from how the maker's
        own scripts talk to the instrument."""
        return rt.read_examples(path)

    @server.tool(annotations=READ, structured_output=False)
    def try_card(card: str, instrument: str) -> str:
        """Try a card (YAML text) before saving it: on an instrument in the lab (its name) or an address the last
        scan found, ask who it is and read every reading. It sets nothing and skips reads that could start something.
        Compare the values with what the person sees, fix the card, and try again."""
        return rt.try_card(card, instrument)

    @server.tool(annotations=ACT, structured_output=False)
    def save_card(card: str) -> str:
        """Keep a new instrument card (status draft) with the user's cards. Only from the manual text the user gave;
        never invent commands. A person adds it to the lab and reviews it; until then its drives stay
        simulation-only on real hardware."""
        return rt.save_card(card)

    @server.tool(annotations=WEB, structured_output=False)
    def search_papers(query: str, limit: int = 8, since_year: Optional[int] = None) -> str:
        """Search the literature: arXiv preprints and journal papers (OpenAlex). Each result has authors, year,
        venue, citations, a short abstract and an id for read_paper. Use it to plan an experiment from what is known
        (typical parameters, signal sizes, pitfalls) and to check a result against published work. The query goes to
        public services: describe the physics, never confidential sample or project details."""
        return rt.search_papers(query, limit, since_year)

    @server.tool(annotations=WEB, structured_output=False)
    def read_paper(paper: str, start: int = 0, max_chars: int = 12000) -> str:
        """Read a paper by its id from search_papers (arXiv:2401.01234, or doi:10.1103/...): title, authors, and for
        arXiv papers with an HTML version the full text by section with equations as LaTeX (otherwise the abstract).
        Long papers come in parts: pass start to read on."""
        return rt.read_paper(paper, start, max_chars)

    @server.tool(annotations=ACT, structured_output=False)
    def make_report(runs: Optional[List[str]] = None, day: str = "", last: int = 0, title: str = "",
                    summary: str = "", format: str = "pdf", kind: str = "runs", device: str = "") -> str:
        """Reports. Kelvin already keeps three up to date by itself: each run's scan report (in its folder), each
        device's report and each project's master report. kind project: rewrite the project's master report now
        (summary, if given, becomes its opening, in your own words); device: rewrite that sample's report; scan: a
        run's own report again. kind runs (default): a separate report of the runs chosen by id, by day (YYYY-MM-DD,
        today, yesterday) or the last N (default today's), with summary as its opening, for sharing a selection.
        Only facts from the runs and the conversation; no speculation."""
        return rt.make_report(runs, day, last, title, summary, format, kind=kind, device=device)

    @server.tool(annotations=READ, structured_output=False)
    def scan_instruments() -> str:
        """Find instruments connected to this computer (GPIB, USB, LAN, serial) and match them to cards. It only
        asks each instrument who it is. Adding one to the lab is the user's decision."""
        return rt.scan()

    @server.tool(annotations=READ, structured_output=False)
    def health_check(instruments: Optional[List[str]] = None) -> str:
        """Identity, error queue, sanity readings and drives inside limits, for all or some instruments. run_plan
        already checks the instruments a plan uses; call this when one misbehaves, after an alarm, or when asked."""
        return rt.health(instruments)

    @server.tool(annotations=READ, structured_output=False)
    def validate_plan(plan: str) -> str:
        """Check a plan (YAML text) without running it and say what it would need approval for: only when the person
        asks (run_plan checks plans itself). Format:
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
        """Dry-run a plan (YAML text) on a simulated copy of the lab, when the person asks what it would do or how
        long it takes (run_plan dry-runs plans that drive the sample by itself). The real lab is not touched."""
        return rt.simulate(plan)

    @server.tool(annotations=ACT, structured_output=False)
    def run_plan(plan: str, dry_run: bool = False) -> str:
        """Run a plan (YAML text): validate, health check (of instruments not checked in the last 10 minutes), one
        human approval for its drive ranges, run, log, then analyse automatically. Call it directly, without
        validate_plan or simulate_plan first. dry_run: true runs it on the simulated lab first (only when the person
        asks, or to show how long it takes). Returns the assessment, key results and the run id."""
        return rt.run(plan, dry_run=bool(dry_run))

    @server.tool(annotations=READ, structured_output=False)
    def queue_show() -> str:
        """The scan queue: each item (a plan, or a step that sets values and waits), its status and run, and whether
        the queue is running, paused or idle."""
        return rt.queue_show()

    @server.tool(annotations=ACT, structured_output=False)
    def queue_add(item: str, position: Optional[int] = None, note: str = "") -> str:
        """Add to the scan queue (it does not start it): a plan (YAML, as for run_plan) or a step between scans, e.g.
        "step: cool to 2 K\nset: {tc.T_set: 2}\nstabilize: {read: tc.T, within: 0.02, for: 120, timeout: 7200}\n
        wait: 600". position: 1 = first; default last. note: a line kept with the run."""
        return rt.queue_add(item, position, note)

    @server.tool(annotations=ACT, structured_output=False)
    def queue_edit(item: str, text: str) -> str:
        """Replace a waiting or finished queue item (by its number or id) with new plan or step text."""
        return rt.queue_edit(item, text)

    @server.tool(annotations=ACT, structured_output=False)
    def queue_change(action: str, item: Optional[str] = None, position: Optional[int] = None) -> str:
        """Rearrange the queue. action: move (item to position), remove (item), again (put a finished item back in
        line) or clear (remove every finished item). Items are named by their number in queue_show, or their id."""
        return rt.queue_change(action, item, position)

    @server.tool(annotations=ACT, structured_output=False)
    def queue_start(dry_run: bool = False) -> str:
        """Start the queue: check every waiting item, ask a person once for every drive range they use, then run
        them one after another in the background. dry_run: true first runs the whole queue in order on the simulated
        lab (only when the person asks, or to say how long it takes)."""
        return rt.queue_start(dry_run=bool(dry_run))

    @server.tool(annotations=ACT, structured_output=False)
    def queue_pause() -> str:
        """Pause the queue after the item that is running (the Stop button ends that item too)."""
        return rt.queue_pause()

    @server.tool(annotations=ACT, structured_output=False)
    def queue_save(name: str) -> str:
        """Keep the waiting items as a named queue, to load again later (queue_load)."""
        return rt.queue_save(name)

    @server.tool(annotations=ACT, structured_output=False)
    def queue_load(name: str) -> str:
        """Add a saved queue's items to the end of the queue."""
        return rt.queue_load(name)

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
    ap.add_argument("--data-dir", help="event log and run folders (default: ~/Kelvin/virtual-lab; once people have "
                                       "accounts in the app, ~/Kelvin/agents, so no one's own folder)")
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
    home = Path.home() / "Kelvin"
    data_dir = args.data_dir or home / ("agents" if (home / "accounts.json").exists() else "virtual-lab")
    runtime = LabRuntime(lab, approver, data_dir, envelope)
    runtime.cards_dir = Path.home() / "Kelvin" / "cards"
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
