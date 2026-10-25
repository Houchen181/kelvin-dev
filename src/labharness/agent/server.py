"""MCP server: lets Claude, GPT, Gemini or any MCP client drive a lab through the safety gate.

    python -m labharness.agent                     # virtual lab over stdio, opens the approval page
    python -m labharness.agent --speed 10          # virtual lab in real time ×10 instead of instant
"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .. import carddraft, expr
from .._version import __version__
from ..catalog import MAX_ROWS as QUERY_ROWS, SCHEMA_TEXT
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
- An instrument a person switched off in the Lab panel (lab_status says so) is not part of this experiment: Kelvin
  sends it nothing, and commands and plans that use it are refused. Don't plan with it; if it is needed,
  ask_to_switch_on asks the person, saying why. Only they switch it on.
- For sweeps, write a plan and call run_plan directly. It checks the plan, health-checks the instruments it uses,
  asks once for its drive ranges, runs, logs and analyses the data. Report its assessment and key numbers (with
  uncertainties) rather than raw rows. A dry run (simulate_plan, or run_plan with dry_run: true) is not a routine
  step, even for a plan that drives: only when the person asks, or to estimate how long a long plan takes.
- Units are the ones describe_instrument shows. Call describe_instrument before using an instrument for the first time.
- A camera's frame is an image: measure cam.frame to keep images (each run keeps them in its run.h5), and its mean,
  max or sum for numbers to fit. A spectrum is recorded with its wavelengths.
- Every run is kept as one file (run.h5) and listed in a catalog of all runs. For questions about earlier data:
  find_runs (query_runs for what its filters can't ask), describe_run for a run's card, then get_run_data or compute
  for its numbers. Ask compute for a number or a derived column rather than reading every row.
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
- A mounted device may carry protection a person set (shown in the lab status): its limits, a leakage stop on each
  gate and caps in the source itself. While a person has their hands on the sample, every drive stays at safe until
  they unlock it. Plan within these; when one refuses something, say which and leave it to the person.
- To line up scans for later (overnight, or one after another), use the queue: queue_add each plan, and steps
  between them (step: name, set: {...}, stabilize: {read, within, for, timeout}, wait: seconds), then queue_start.
  A person approves the whole queue once; it runs in the background and pauses on a failure or Stop. Say what is
  queued and roughly how long it takes; queue_show follows it.
- When the person names an instrument connected to this computer, or wants to use one the lab doesn't have yet,
  set it up yourself: scan_instruments to find it; if a card matched, add_instrument; if none did, draft_card (Kelvin
  finds the maker's manual online and writes a draft card), try_card it (it only reads) and check that its readings
  make sense, then add_instrument. Then tell the person in a line or two what you added, with a reading or two they
  can compare with the front panel, and what the card left out (draft_card's notes). Add only what they asked for.
  When draft_card can't find the manual, or you need the maker's documentation, software or a driver, look it up
  yourself: web_search, read_web_page (a page's text and its links, PDFs first), save_manual (keeps the PDF, with the
  instrument's address), then draft_card with manual. Never use the screen to search or to download.
- An instrument the scan doesn't find or doesn't identify: find out what it is yourself. inspect_address for one on
  the network (what its ports answer), connected_devices for one plugged in (USB devices, also without a driver;
  serial ports; this computer's networks), installed_software and find_maker_files for the maker's software, SDK,
  examples and manual on this computer. Then how its maker talks to it (the manual, the maker's examples, the web)
  decides the card: text commands give an ordinary card; a maker's Python package or DLL gives a library card.
- What an instrument needs on this computer, Kelvin installs, never the app: install_for_instrument installs the
  maker's Python package (at the version the instrument needs) or the maker's files once the person approves, into
  the drivers environment for Kelvin's built-in drivers (a card's requires, or a driver saying a package is missing),
  or into an environment of its own for a library card (lib://<address>?env=<it>; Python and bitness as the library
  needs). With no example scripts, library_api shows the package's classes and functions. Don't ask the person to
  pip install. A board that runs its software in Jupyter (QICK on a PYNQ board): run_on_board starts its servers
  from the board's own notebooks and reads its versions, once the person approves the code. Ask them only where you can't go on: a setting on the instrument
  itself (remote control, its address, a list of allowed computers), the maker's installer (a USB driver, a runtime
  that needs administrator rights: say which and where it is), a manual or specifications document nobody publishes
  (ask for its PDF, then draft_card with manual or specs), or an address nothing answers on.
- When draft_card can't write it (no public manual, or an instrument driven through the maker's library), or outside
  the Kelvin app, write the card yourself: the manual's command reference or the maker's example scripts (pasted, or
  read_examples on a file or folder, found with find_maker_files or named by the person), card_guide, the card from
  that text only, check_card until it is valid, try_card, save_card.
- An instrument Kelvin added, or one with a draft card, is not reviewed yet (lab_status says so): it reads and
  configures at once, and every output change on it asks the person, auto mode included, until they review its
  limits. Its limits and that mark are the person's to change, never yours.
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
  - Don't write code, scripts or tests, or re-analyse data in code, unless the person asks: use run_plan's analysis,
    analyze and compute.
  - Plan the whole measurement, run it, report. A coarse scan before a fine one is good practice when it saves time;
    the physics decides how many measurements there are, not a habit of testing.
- Reply to the user briefly: the result first, then the next step."""

READ = ToolAnnotations(readOnlyHint=True)
WEB = ToolAnnotations(readOnlyHint=True, openWorldHint=True)   # reads the public literature, never the lab
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
FETCH = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


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
        """Try a card on the instrument before using it: card is its YAML text, or the id of a card Kelvin has (one
        draft_card wrote); instrument is its name in the lab or its address (one the last scan did not find asks the
        person first). Asks who it is and reads every reading; it sets nothing and skips reads that could start
        something. Compare the values with what the person sees, fix the card, and try again."""
        return rt.try_card(card, instrument)

    @server.tool(annotations=ACT, structured_output=False)
    def save_card(card: str) -> str:
        """Keep a new instrument card (status draft) with the user's cards. Only from the manual text the user gave;
        never invent commands. Then try_card and add_instrument; until a person reviews it, every setting and output
        change on it asks them."""
        return rt.save_card(card)

    @server.tool(annotations=FETCH, structured_output=False)
    def draft_card(address: str, maker: str = "", model: str = "", manual: str = "", specs: str = "") -> str:
        """Write a card for a connected instrument Kelvin has none for: Kelvin finds the maker's manual online, writes
        the card from its command reference, checks it and keeps it as a draft. Maker and model come from the
        instrument's identity reply (scan_instruments first); for one that gave none, pass what the person said it is.
        manual: when Kelvin can't find it online, the manual the person gives (a file on this computer or its web
        address). specs: likewise, the specifications document an output's ranges are in, when the manual leaves them
        there and Kelvin can't find it. Only the maker and model leave the computer. Takes a minute or two; sends the
        instrument nothing."""
        return rt.draft_card(address, maker, model, manual, specs)

    @server.tool(annotations=WEB, structured_output=False)
    def read_web_page(url: str, start: int = 0, max_chars: int = 12000) -> str:
        """Read a web page or a document on the web (a PDF) as text: a maker's product or download page, a manual,
        documentation. A page comes with its links, documents first, to follow. Reads only; keeps nothing. Long
        texts come in parts (start)."""
        return rt.read_web_page(url, start, max_chars)

    @server.tool(annotations=FETCH, structured_output=False)
    def save_manual(source: str, maker: str = "", model: str = "", address: str = "", instrument: str = "") -> str:
        """Keep an instrument's manual: its web address (a PDF or a page) or a file on this computer. Kelvin checks
        it names the model and keeps it in Kelvin's manuals folder. With address (an instrument not in the lab yet),
        add_instrument puts it in the instrument's folder; then draft_card with manual=<the kept file>. With
        instrument (one in the lab), it becomes that instrument's manual for search_manual."""
        return rt.save_manual(source, maker, model, address, instrument)

    @server.tool(annotations=ACT, structured_output=False)
    def add_instrument(address: str, card: str = "", name: str = "", connection: Optional[Dict[str, Any]] = None,
                       vars: Optional[Dict[str, str]] = None) -> str:
        """Add an instrument connected to this computer to the lab, once the person has asked for it. address: from
        scan_instruments; card: the one that matched (default), or the id draft_card or save_card gave; name: short,
        e.g. smu or lockin2 (default: from its kind). connection: serial or line-ending settings from the manual or
        the person (baud_rate, read_termination...); vars: the card's per-installation names (sensor, channel). Kelvin
        connects it and checks who it is; the other instruments stay as they are. It reads and configures at once;
        every output change on it asks the person until they review its limits. An address the scan did not find
        asks the person first."""
        return rt.add_instrument(address, card, name, connection, vars)

    @server.tool(annotations=ACT, structured_output=False)
    def ask_to_switch_on(instrument: str, why: str) -> str:
        """Ask the person to switch on an instrument they switched off in the Lab panel, saying why you need it.
        It is switched on only if they say yes."""
        return rt.ask_to_switch_on(instrument, why)

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
        asks each instrument who it is. add_instrument adds one the person asked for."""
        return rt.scan()

    @server.tool(annotations=READ, structured_output=False)
    def inspect_address(address: str) -> str:
        """What an address on the lab's network answers, for an instrument the scan didn't find or didn't
        identify: the ports instruments use, what each says (its *IDN? reply where it speaks SCPI, a web page's
        title, a greeting, a Jupyter or Pyro server on a board), its LXI identification, the matching card and the
        address to use. address: an IP address or host name, optionally :port. Local networks only; only looks."""
        return rt.inspect_address(address)

    @server.tool(annotations=READ, structured_output=False)
    def connected_devices() -> str:
        """Everything plugged into this computer, also what no driver claims: USB devices with the name each
        reports, its maker and driver (or that it has none), serial ports, and the networks this computer is on (a
        board on USB networking makes one). For an instrument scan_instruments doesn't find."""
        return rt.connected_devices()

    @server.tool(annotations=READ, structured_output=False)
    def installed_software(query: str = "") -> str:
        """Programs and drivers installed on this computer, with version and folder: those matching query (a maker
        or program, e.g. attocube, LightField, NI-DAQmx), or with none the instrument makers' software, drivers and
        Pythons. Tells whether the maker's software or driver an instrument needs is here."""
        return rt.installed_software(query)

    @server.tool(annotations=READ, structured_output=False)
    def find_maker_files(query: str) -> str:
        """Find a maker's files on this computer by name, in the folders makers install to (Program Files,
        ProgramData, Public Documents; /Applications, /Library, /opt): its example scripts, its SDK's Python
        package, its DLL and header, its manual. Then read_examples, or install_for_instrument with files."""
        return rt.find_maker_files(query)

    @server.tool(annotations=READ, structured_output=False)
    def environments() -> str:
        """The instrument environments Kelvin installed on this computer: each one's Python, the makers' packages
        and files in it, and which instruments it is for."""
        return rt.environments()

    @server.tool(annotations=FETCH, structured_output=False)
    def install_for_instrument(env: str, packages: Optional[List[str]] = None, python: str = "", bits: int = 64,
                               files: str = "", why: str = "") -> str:
        """Install what an instrument needs on this computer, once the person approves (they see what, which
        version and from where): its maker's Python packages from PyPI (pip names, with ==version where it must
        match the instrument's own, e.g. qick==0.2.436), and/or the maker's files (files: a folder or file here, or
        a web address: an SDK's Python package is installed, a DLL is kept). env: drivers for Kelvin's built-in
        drivers (a card's requires, or what a driver says is missing); for a library card, an environment of its own
        (e.g. attodry), with python (3.8...) and bits (32 for a 32-bit DLL) as its library needs, then
        lib://<address>?env=<env>. Never ask the person to pip install. A maker's installer (a USB driver, a runtime)
        is theirs to run: say which and from where."""
        return rt.install_for_instrument(env, packages, python, bits, files, why)

    @server.tool(annotations=ACT, structured_output=False)
    def run_on_board(address: str, code: str = "", notebook: str = "", password: str = "", wait: float = 30) -> str:
        """Run Python on a lab board through its Jupyter server (a PYNQ board for QICK, a Red Pitaya), once the
        person approves the code: code, or notebook (the path of one of the board's own notebooks, run cell by
        cell). address: the board (port 9090 unless given). password: the board's Jupyter password (PYNQ's default
        is xilinx). A cell still running after wait seconds (a server) is left running on the board. For QICK: the
        notebooks qick/pyro4/00_nameserver.ipynb, then 01_server.ipynb, in two calls; the board's QICK version with
        code "import qick; print(qick.__version__)"."""
        return rt.run_on_board(address, code, notebook, password, wait)

    @server.tool(annotations=READ, structured_output=False)
    def library_api(module: str, env: str = "") -> str:
        """A maker's Python package as its programmer sees it: its classes (methods, properties) and functions with
        their arguments and the first line of their documentation, from one of Kelvin's environments (env; default
        drivers). For writing a library card when the maker gives no example scripts. Nothing in it is called."""
        return rt.library_api(module, env)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True,
                                             openWorldHint=False), structured_output=False)
    def remove_environment(env: str) -> str:
        """Remove one of Kelvin's instrument environments (after the person approves), e.g. one an instrument no
        longer needs; instruments using it stop connecting until it is installed again."""
        return rt.remove_environment(env)

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
        """Most recent runs with status and point counts (the one running now too)."""
        return rt.list_runs(limit)

    @server.tool(annotations=READ, structured_output=False)
    def find_runs(text: str = "", device: str = "", project: str = "", plan: str = "", status: str = "",
                  since: str = "", until: str = "", quantity: str = "", limit: int = 20) -> str:
        """Search every run in the data folder, newest first. Each comes with what it swept and measured (units,
        ranges), the conditions held and its assessment. Filters are optional and combine: text (anywhere in the
        run's card), device, project, plan (names, in part), status (done, aborted, interrupted), since and until
        (2026-10-24, or 2026-10-24T15:00; inclusive), quantity (temperature, magnetic_field, voltage, current,
        frequency, power... or a column such as smu.V). For questions these can't answer, query_runs."""
        return rt.find_runs(text, device, project, plan, status, since, until, quantity, limit)

    @server.tool(annotations=READ, structured_output=False, description=(
        "Answer questions across runs with one read-only SQL query (SQLite SELECT) over the catalog of every run in "
        "the data folder; up to %d rows come back as CSV. Tables:\n%s" % (QUERY_ROWS, SCHEMA_TEXT)))
    def query_runs(sql: str) -> str:
        return rt.query_runs(sql)

    @server.tool(annotations=READ, structured_output=False)
    def describe_run(run_id: str) -> str:
        """One run in brief, before reading its numbers: what it swept and measured (units, ranges, quantities), the
        conditions held, the fitted results with uncertainties, the assessment and issues, the plan as it ran and
        its files."""
        return rt.describe_run(run_id)

    @server.tool(annotations=READ, structured_output=False)
    def get_run_data(run_id: str, columns: Optional[List[str]] = None, max_rows: int = 50, where: str = "") -> str:
        """A run's data as CSV, evenly thinned to max_rows (trace columns show as trace[n]; get_trace reads one).
        where: only the points meeting a condition, e.g. "smu.V > 1 and tc.T < 5"."""
        return rt.run_data(run_id, columns, max_rows, where)

    @server.tool(annotations=READ, structured_output=False, description=(
        "Compute from a run's columns without writing code: one number (mean(smu.I)), or a value at each point "
        "shown against the swept axes with its minimum and maximum (lockin.X / smu.I, deriv(lockin.X, smu.V)). "
        "Columns by their names (instrument.parameter, i, t) in the run's own units; arithmetic, comparisons, "
        "and/or/not, a if cond else b, and the %s. where: only the points meeting a condition, applied first." %
        expr.HELP))
    def compute(run_id: str, expression: str, where: str = "", max_rows: int = 50) -> str:
        return rt.compute(run_id, expression, where, max_rows)

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
    from .. import envs
    envs.cleanup()
    envs.attach(envs.DRIVERS)   # makers' packages Kelvin installed for its built-in drivers (envs.py)
    runtime.monitor.start()
    runtime.tidy_later()   # runs a crash ended, and runs from before run.h5, kept as run.h5 and catalogued
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
