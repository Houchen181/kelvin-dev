# Other AI agents (MCP)

Kelvin's lab tools are an MCP server, so Claude Code, Codex, Gemini CLI or another MCP client can drive the lab under the same rules as Kelvin itself.

## Running the server

```bash
.venv/Scripts/python -m pip install -e ".[agent]"
```

```bash
.venv/Scripts/kelvin-mcp
```

By default the server starts the virtual lab in instant simulated time. Use `--speed 10` for 10× real time. Event logs and run folders go to `~/Kelvin/virtual-lab`, or to `~/Kelvin/agents` once people have accounts in the app, so they land in no one's own folder; `--data-dir` chooses another. The server also opens the **approval page** in your browser; its URL, which includes an access key, is printed on the server's stderr.

On the approval page you can:
- **Approve or decline** what the agent asks for. An unanswered request is declined after 120 s (`--approval-timeout`). No MCP tool can answer approvals. Nobody signs in here, so in a lab with roles ([safety.md](safety.md#lab-roles)) each yes takes the lab owner's PIN, and auto mode stays off.
- **Watch** every instrument (drives away from safe are highlighted) and the activity log.
- **Press Stop.** This declines open requests and stops the agent's current ramp or plan, and the queue. Drives stay where they are; a stopped plan brings the drives it touched to safe (or holds them where the lab config says hold), then runs its own `on_abort` steps. It works even while an instrument holds Kelvin up: a sweep the instrument is still taking, a settle or a wait ends within a second, and in the portal the chat's step ends at once. Calls the agent made that were still waiting for the lab are not run: they answer that a person pressed Stop.

**Cancelling a call** (the client's `notifications/cancelled`, or the client disconnecting) ends it, so nothing happens for a caller that is gone: what it asked a person for is declined, the ramp or run it started stops as at Stop, and a call still waiting for the lab is never run. The record says `call_cancelled`.

**Calls at once** (several clients, the portal's chats, or one client's calls side by side): changes to the lab (a set, a raw command, an action, a run, starting the queue, adding an instrument) go one at a time, as the gate's approvals are the lab's. A change that finds the lab held waits up to 30 s, or answers at once, saying what holds it, when a longer run, the queue or an approval card does. Each change holds only the instruments it uses (a run: those its plan names, with their guards' and interlocks' instruments). Everything else goes on beside it: lookups, the queue's edits, watches, `stop_run`, and readings (`lab_status`, `get_value`, `health_check`, `show_sequence`, a camera's frame). A reading never sends a command to an instrument a change holds: it gives that instrument's latest values, with how old each is and whether the run measured or set it, and reads the others. Two readings of one instrument take turns. **`stop_run`** is the agent's Stop, for when the person asks to stop: it does what the Stop button does, whoever started the work.

Client setup. Use the full path to the venv's `kelvin-mcp` (or its Python with `-m labharness.agent`):

| Client | Where |
|---|---|
| Claude Code | copy `.mcp.example.json` to `.mcp.json` and set the path, or `claude mcp add kelvin -- <venv>/Scripts/kelvin-mcp` |
| Claude Desktop | `claude_desktop_config.json` → `"mcpServers": {"kelvin": {"command": "<venv>/Scripts/kelvin-mcp"}}` |
| Codex CLI | `~/.codex/config.toml` → `[mcp_servers.kelvin]` with `command` |
| Gemini CLI | `~/.gemini/settings.json` → `"mcpServers": {"kelvin": {"command": "<venv>/Scripts/kelvin-mcp"}}` |
| ChatGPT | Needs a public HTTPS MCP endpoint. The server only listens on 127.0.0.1 (`--transport streamable-http`), and putting a lab on the internet is out of scope for now |

A drive change blocks its tool call until a person answers. Set the client's tool timeout above the approval timeout: `MCP_TOOL_TIMEOUT` (ms) for Claude Code, `tool_timeout_sec` for Codex, `timeout` (ms) for Gemini CLI.

Tools:
- **The lab:** `lab_status`, `describe_instrument` (each limit with where it comes from: the card, the lab config or a mounted device), `instrument_info`, `lab_config` (the lab config as written, or one instrument's entry, and where the file differs from what Kelvin uses), `search_manual`, `show_sequence` and `save_sequence` (pulse sequences, drawn and checked; see [instruments.md](instruments.md#spin-experiments-nv-centers)), `get_value`, `set_value`, `send_raw`, `undo_last`, `health_check`, `scan_instruments`, `ask_to_switch_on`, `reconnect_instrument`, `do_action`, `watch`, `watches`, `unwatch`, `wait`, `stop_run`.
- **Plans and the queue:** `validate_plan`, `simulate_plan`, `run_plan`, `run_recipe` (a measurement in stages, each checked on its data before the next; [data.md](data.md#recipes)), `queue_show`, `queue_add`, `queue_edit`, `queue_change`, `queue_start`, `queue_pause`, `queue_save`, `queue_load`.
- **Data:** `find_runs` and `query_runs` (every run in the data folder: filters, or read-only SQL over the catalog), `describe_run` (a run's card), `get_run_data` (with `where`), `compute` (arithmetic on a run's columns), `get_trace`, `look_at` (a run's plots, a 2-D scan's map or a camera frame as a picture, with a caption of its numbers; a stack's frame by its number), `analyze_run`, `fit_run` (Kelvin's models or an expression, with weights, a window and held parameters; kept in run.h5), `plot_runs` (overlays, derived quantities in their units, log axes, heat maps; PNG, SVG, PDF), `export_run` (CSV, Excel, NumPy), `run_python` (Python on runs' data in a sandbox; see data.md for what it stops and what it doesn't), `list_runs`, `recent_events`, `run_provenance` (a run's plan, conditions at the start and the end, measurement chain, and each approval with who gave it), `activity` (who did what between two times, in wall-clock time), `measurement_chain` (a device's excitation, peak or rms, series resistor and gains; stamped into its runs), `make_report` (`kind`: runs, scan, device or project; `pages`: the length asked for), `import_data` (another program's tables, Excel, BioLogic .mpt/.mpr, Gamry .dta, Arbin exports, LightField .spe, NumPy arrays and TIFF frames, as a run the tools above read; several on the same x values in one run, to compare or subtract; a data file attached in the portal is imported as it is attached). `describe_run` and `lab_status` say where the files are.
- **New instruments:** `card_guide`, `check_card`, `read_examples` (a maker's examples, headers and manual PDFs, in a file or folder the person names), `try_card`, `save_card` (also this lab's version of one of Kelvin's cards, under its id, as a draft); in the Kelvin app also `draft_card` (a card from the maker's manual online) and `add_instrument` (adds a connected instrument to the lab, marked as added by Kelvin: every output change on it asks a person until they review its limits).
- **The lab's memory:** `shared_memory` (what people and Kelvin keep about the lab, each note with who wrote it and when: all of it with the measurement chains on record, an instrument's notes, or those that bear on some words) and `remember_shared` (keeps a lesson, wiring, a reference source, an excitation or another lasting fact, tied to the instruments it concerns; `replaces` corrects a note by its id). See [The lab's memory](#the-labs-memory).
- **Experiments:** `experiment_guide`: the instrument collection's experiments, with their plans' instrument names changed to the lab's ([instruments.md](instruments.md#the-instrument-collection)).
- **What an instrument is, and what it needs:** `inspect_serial` (an instrument on a serial port: its speed, framing and identity), `inspect_address` (what a local network address answers: `*IDN?` where it speaks SCPI, a web page's title, its LXI identification, a board's Jupyter or Pyro server), `connected_devices` (USB devices, also without a driver; serial ports; this computer's networks), `installed_software`, `find_maker_files`; `environments`, `install_for_instrument` (a maker's Python package or files, in one of Kelvin's own environments, once a person says yes: a download onto their computer is theirs to approve), `library_api` (a maker's package's classes and functions, read without calling anything) and `remove_environment`; `run_on_board` (code or one of the board's notebooks, in its Jupyter, once a person approves the code: QICK's servers on a PYNQ board).
- **In the portal's chats** (not over MCP, which offers every tool): the rarer tools above (setting up instruments, cards, installs, queue editing, watches, boards, `query_runs`) are loaded when a chat needs them, with `find_tools` or by calling one by name; `read_skill_file` reads a skill's files; in a chat with a folder of its own, `list_folder`, `read_file` and `write_file` (a text file saved there; Python code in a folder an instrument's library loads from shows in the chat whole); several lookups that touch no instrument in one step run side by side.
- **Kelvin itself:** `kelvin_help` searches Kelvin's own documentation and the map of its portal: where a person approves, changes limits, reviews an instrument or a card, protects a device.
- **Literature and the web:** `search_papers`, `read_paper`; `read_web_page` (a page or a PDF on the web, with its links) and `save_manual` (keeps an instrument's manual, for its card and its folder). In the Kelvin app also `web_search`, through the model service's own web search.

Text from outside the lab (web pages, papers, manuals, instruments' replies) comes back marked as `<outside_text>`; in auto mode, drive changes ask a person for 30 minutes after the agent read text from the web.

## The lab's memory

What people and Kelvin learned about the lab is kept for every chat in the portal, everyone who signs in on the computer and every agent over MCP: how it is wired (which channel reads the mixing chamber), where references come from (the lock-in takes the chopper's), what excites the sample and whether it is stated rms or peak, and lessons learned at the bench ("switch each OBIS's emission on before its power"). It is kept in Kelvin's own folder: `~/Kelvin/lab-memory.json` for the lab in `lab.yaml`, `~/Kelvin/virtual-lab-memory.json` for the virtual lab, which the MCP server runs, so its agents share the virtual lab's notes with the portal's chats on it.

- **In the server's instructions:** a short brief of it, as it was when the server started (each stdio session starts its own server): lessons first, then wiring, references, excitations and other facts, within about 1200 characters, and the measurement chain on record. `shared_memory` has the rest, and the latest.
- **When an instrument is used:** `describe_instrument` ends with the notes on that instrument; `set_value`, `send_raw`, `do_action` and `run_plan` add them the first time the server uses it, and again once a note's text changes. Approval cards show the lessons on the instruments they name, for the person who answers.
- **Keeping a note:** `remember_shared` keeps one as an AI agent's, with the time; a person sees, changes and deletes every note in the portal's Lab panel (**Lab memory**), and each change goes in the lab's record (`activity`, kind `lab`). The same fact is kept once. After the agent has read text from the web in the last few minutes, a person approves the note first: a note reaches every chat's prompt.
- **What a note is not:** information, never a rule Kelvin enforces. It changes no limit, interlock or approval; the safety gate decides as before.

**Security limit:** the access key keeps the model from approving its own requests through its tools. An agent that also has a shell on the same PC could still reach the page. For real hardware, run Kelvin on a machine where the agent has no shell.
