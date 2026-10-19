# Other AI agents (MCP)

Kelvin's lab tools are an MCP server, so Claude Code, Codex, Gemini CLI or another MCP client can drive the lab under the same rules as Kelvin itself.

## Running the server

```bash
.venv/Scripts/python -m pip install -e ".[agent]"
```

```bash
.venv/Scripts/kelvin-mcp
```

By default the server starts the virtual lab in instant simulated time. Use `--speed 10` for 10× real time. Event logs and run folders go to `~/Kelvin/virtual-lab`. The server also opens the **approval page** in your browser; its URL, which includes an access key, is printed on the server's stderr.

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
- **The lab:** `lab_status`, `describe_instrument`, `instrument_info`, `search_manual`, `show_sequence`, `get_value`, `set_value`, `send_raw`, `undo_last`, `health_check`, `scan_instruments`, `watch`, `watches`, `unwatch`, `wait`.
- **Plans and the queue:** `validate_plan`, `simulate_plan`, `run_plan`, `queue_show`, `queue_add`, `queue_edit`, `queue_change`, `queue_start`, `queue_pause`, `queue_save`, `queue_load`.
- **Data:** `analyze_run`, `list_runs`, `get_run_data`, `get_trace`, `recent_events`, `make_report`.
- **New instruments:** `card_guide`, `check_card`, `read_examples`, `try_card`, `save_card`.
- **Literature:** `search_papers`, `read_paper`.

**Security limit:** the access key keeps the model from approving its own requests through its tools. An agent that also has a shell on the same PC could still reach the page. For real hardware, run Kelvin on a machine where the agent has no shell.
