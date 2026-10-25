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
- **Approve or decline** what the agent asks for. An unanswered request is declined after 120 s (`--approval-timeout`). No MCP tool can answer approvals.
- **Watch** every instrument (drives away from safe are highlighted) and the activity log.
- **Press Stop.** This declines open requests and stops the agent's current ramp or plan. Drives stay where they are; a stopped plan runs its own `on_abort` steps.

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
- **The lab:** `lab_status`, `describe_instrument`, `instrument_info`, `search_manual`, `show_sequence`, `get_value`, `set_value`, `send_raw`, `undo_last`, `health_check`, `scan_instruments`, `ask_to_switch_on`, `reconnect_instrument`, `do_action`, `watch`, `watches`, `unwatch`, `wait`.
- **Plans and the queue:** `validate_plan`, `simulate_plan`, `run_plan`, `run_recipe` (a measurement in stages, each checked on its data before the next; [data.md](data.md#recipes)), `queue_show`, `queue_add`, `queue_edit`, `queue_change`, `queue_start`, `queue_pause`, `queue_save`, `queue_load`.
- **Data:** `find_runs` and `query_runs` (every run in the data folder: filters, or read-only SQL over the catalog), `describe_run` (a run's card), `get_run_data` (with `where`), `compute` (arithmetic on a run's columns), `get_trace`, `look_at` (a run's plots, a 2-D scan's map or a camera frame as a picture, with a caption of its numbers), `analyze_run`, `list_runs`, `recent_events`, `make_report` (`kind`: runs, scan, device or project).
- **New instruments:** `card_guide`, `check_card`, `read_examples`, `try_card`, `save_card` (also this lab's version of one of Kelvin's cards, under its id, as a draft); in the Kelvin app also `draft_card` (a card from the maker's manual online) and `add_instrument` (adds a connected instrument to the lab, marked as added by Kelvin: every output change on it asks a person until they review its limits).
- **What an instrument is, and what it needs:** `inspect_serial` (an instrument on a serial port: its speed, framing and identity), `inspect_address` (what a local network address answers: `*IDN?` where it speaks SCPI, a web page's title, its LXI identification, a board's Jupyter or Pyro server), `connected_devices` (USB devices, also without a driver; serial ports; this computer's networks), `installed_software`, `find_maker_files`; `environments`, `install_for_instrument` (a maker's Python package or files, in one of Kelvin's own environments, once a person approves), `library_api` (a maker's package's classes and functions, read without calling anything) and `remove_environment`; `run_on_board` (code or one of the board's notebooks, in its Jupyter, once a person approves the code: QICK's servers on a PYNQ board).
- **Kelvin itself:** `kelvin_help` searches Kelvin's own documentation and the map of its portal: where a person approves, changes limits, reviews an instrument or a card, protects a device.
- **Literature and the web:** `search_papers`, `read_paper`; `read_web_page` (a page or a PDF on the web, with its links) and `save_manual` (keeps an instrument's manual, for its card and its folder). In the Kelvin app also `web_search`, through the model service's own web search.

Text from outside the lab (web pages, papers, manuals, instruments' replies) comes back marked as `<outside_text>`; in auto mode, drive changes ask a person for 30 minutes after the agent read text from the web.

**Security limit:** the access key keeps the model from approving its own requests through its tools. An agent that also has a shell on the same PC could still reach the page. For real hardware, run Kelvin on a machine where the agent has no shell.
