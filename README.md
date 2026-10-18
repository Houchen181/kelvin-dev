<p align="center">
  <img src="src/labharness/web/static/kelvin.svg" width="112" alt="Kelvin: a sleeping cat beside a snowflake">
</p>

<h1 align="center">Kelvin</h1>
<p align="center"><b>The coolest cat in the lab.</b><br>An AI lab assistant for experimental physicists that plans, measures and logs, and asks before it drives your sample harder.</p>

---

Kelvin connects Claude, GPT, Gemini or a local model to your instruments through a safety gate. Measurement runs freely. Anything that pushes more power, voltage, current, field or temperature into the sample is checked against limits and interlocks, approved by a person, ramped, and watched. Like its namesake, Kelvin stays calm and never rushes.

See [VISION.md](VISION.md) for the goals and [ARCHITECTURE.md](ARCHITECTURE.md) for the design.

**Status:** The core rules, formats and a fully simulated lab are done (Phase 0). The MCP server, the approval page and the chat portal are done, so any AI agent can drive the virtual lab. Real-instrument adapters, roles and locks, and monitoring come later.

## The portal: chat with Kelvin in your browser

This is a chat site like the Claude or ChatGPT website, wired to the lab. It needs Python ≥ 3.10:

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -e ".[web]"
```

```bash
.venv/Scripts/kelvin
```

The last command opens `http://127.0.0.1:8770` in your browser. Bookmark that link: the access key after `#` stays the same between restarts. To try the portal without an API key, add `--demo`; this gives you a scripted model that is not AI.

- **Models:** Add keys under Settings for Claude, GPT or Gemini, or point it at a local OpenAI-compatible server (Ollama, vLLM, LM Studio) for confidential work. Keys stay in `~/Kelvin/settings.json` and go only to their own provider.
- **Conversation:** Answers stream in Markdown. Each tool call is a step you can expand, and anything that drives the sample harder appears as an **Approve / Decline** card inside the conversation. Runs are plotted under the answer (hover for values, "Show table" for numbers).
- **Lab panel:** Shows live instrument values (drives away from safe are highlighted) and the activity log. **All drives to safe** is always in the top bar.
- **Safety:** Kelvin uses the same MCP tools and safety gate as external agents, so it has the same rules and no extra power. Only one conversation drives the lab at a time; Stop ends the current answer and any ramp.

## Connect another AI agent (MCP)

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
- **Press "All drives to safe."** This declines open requests, stops the current ramp or plan, and sends every drive to safe.

Client setup. Use the full path to the venv's `kelvin-mcp` (or its Python with `-m labharness.agent`):

| Client | Where |
|---|---|
| Claude Code | copy `.mcp.example.json` to `.mcp.json` and set the path, or `claude mcp add kelvin -- <venv>/Scripts/kelvin-mcp` |
| Claude Desktop | `claude_desktop_config.json` → `"mcpServers": {"kelvin": {"command": "<venv>/Scripts/kelvin-mcp"}}` |
| Codex CLI | `~/.codex/config.toml` → `[mcp_servers.kelvin]` with `command` |
| Gemini CLI | `~/.gemini/settings.json` → `"mcpServers": {"kelvin": {"command": "<venv>/Scripts/kelvin-mcp"}}` |
| ChatGPT | Needs a public HTTPS MCP endpoint. The server only listens on 127.0.0.1 (`--transport streamable-http`), and putting a lab on the internet is out of scope for now |

A drive change blocks its tool call until a person answers. Set the client's tool timeout above the approval timeout: `MCP_TOOL_TIMEOUT` (ms) for Claude Code, `tool_timeout_sec` for Codex, `timeout` (ms) for Gemini CLI.

Tools: `lab_status`, `describe_instrument`, `get_value`, `set_value`, `send_raw`, `undo_last`, `safe_state`, `health_check`, `validate_plan`, `simulate_plan`, `run_plan`, `list_runs`, `get_run_data`, `get_trace`, `recent_events`, `wait`.

**Security limit:** the access key keeps the model from approving its own requests through its tools. An agent that also has a shell on the same PC could still reach the page. For real hardware, run Kelvin on a machine where the agent has no shell.

## Try the core without an agent

Requires Python ≥ 3.9 and PyYAML. No hardware is needed.

```bash
python examples/demo.py
```

```bash
python -m pytest
```

Run `python examples/demo.py --ask` to answer the approval requests yourself. If a pytest plugin installed elsewhere breaks collection, set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

## The rules

Every command passes through the `SafetyGate`, both direct commands (`get`, `set`, `raw`) and the steps of a plan.

| Command | What happens |
|---|---|
| Measure / configure | Runs immediately and is logged |
| Drive toward safe | Runs, ramped at the lab's rate |
| Drive away from safe | Limits and interlocks are checked, then a human is asked (unless the project envelope or an approved plan covers it). The change is ramped while guard readings are watched, and it goes back to safe if one trips |
| Beyond limits, or a blocked command | Refused |

- Raw strings are matched against the card. A raw string that sets a drive is treated as a drive. One that touches a drive's command family but can't be parsed is sent to a human. Blocked and reset families come from the card.
- A plan is dry-run on twins first, approved once as a range, and then run.
- `safe_state()` is the kill switch. It sends every drive to safe in card order, or holds it where the lab config says to hold.
- Approvals come from an `Approver`, a human-facing object the agent never sees.

## Layout

The Python package is still called `labharness` (`import labharness`); the product, commands and repo are Kelvin.

| Path | What it is |
|---|---|
| `src/labharness/cards.py` | Instrument card schema: parameters tagged measure/drive, safe values, limits, ramps, health checks |
| `src/labharness/scpi.py` | SCPI command templates (short/long forms, optional nodes, compound commands) |
| `src/labharness/labconfig.py` | Lab config (lab limits, rates, guards, interlocks, trust) and project envelopes |
| `src/labharness/gate.py` | The safety gate |
| `src/labharness/plans.py`, `engine.py` | Plan format; validate → preflight → dry run → approve → run → log |
| `src/labharness/eventlog.py` | Append-only event log; one folder per run (plan, snapshots, data, meta) |
| `src/labharness/sim/` | Twins that answer the card's own commands, plus a toy sample |
| `src/labharness/agent/` | MCP server (`server.py`), plain-text tool answers (`runtime.py`), approval page (`approval.py`, `ui.html`) |
| `src/labharness/web/` | The portal: web API (`app.py`), chat loop (`agent.py`), one module per model provider (`providers/`), chats and settings, browser UI and the logo (`static/`) |
| `src/labharness/data/` | Cards for the virtual lab, its lab config and a demo envelope |

## Working together

Set up a checkout (Python ≥ 3.10 for everything; the core alone runs on 3.9):

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -e ".[web,dev]"
```

```bash
.venv/Scripts/python -m pytest
```

- Work on a branch and open a pull request into `main`. GitHub Actions runs the tests on Python 3.9 (core) and 3.13 (everything) for every push and PR.
- [VISION.md](VISION.md) is the spec. [ARCHITECTURE.md](ARCHITECTURE.md) records the design decisions and the build order; update it when a decision changes.
- Never commit API keys, lab data, or a machine-specific `.mcp.json`. `.gitignore` already excludes them; keys live in `~/Kelvin/settings.json`.
- Changes to the safety gate (`gate.py`), the card format (`cards.py`) or the approval path need a test that shows the rule holds.

## Brand

| | |
|---|---|
| Name | Kelvin, "the coolest cat in the lab" |
| Mark | a sleeping cat beside a snowflake: [`kelvin.svg`](src/labharness/web/static/kelvin.svg) |
| Colors | navy `#1b3a5c`, ice `#2f6fb0`, frost `#e8f2fc`, snowflake `#8cc4f2` |
| Charts | keep the validated data palette in `app.css` (`--series-1`, `--s1`…`--s8`); brand colors are for chrome, not data |

## Writing a card

```yaml
card: 1
id: acme/sm-2000
model: ACME SM-2000 Source-Meter
kind: source-meter
status: draft                 # draft cards keep their drives simulation-only on real hardware
identify: { idn: "^ACME,SM-2000" }
error_query: "SYSTem:ERRor?"
parameters:
  V:
    kind: drive
    unit: V
    set: "SOURce:VOLTage[:LEVel] {value}"
    get: "SOURce:VOLTage[:LEVel]?"
    limits: [-200, 200]       # vendor limits; the lab sets tighter ones
    safe: 0
    ramp: gate
    max_rate: 10              # V/s
  I:
    kind: measure
    unit: A
    get: "MEASure:CURRent?"
raw:
  blocked: ["CALibration", "SYSTem:FIRMware"]
  reset: ["*RST"]
```
