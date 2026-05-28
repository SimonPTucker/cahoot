<div align="center">

<img src="docs/assets/banner.svg" alt="Cahoot — mission control for agent fleets" width="820">

**A terminal-native operator console for multi-agent AI orchestration.**
Built to live in a long-running tmux session you SSH into from anywhere.

[Architecture](docs/ARCHITECTURE.md) · [Writing adapters](docs/ADAPTERS.md) · [Operations](docs/OPERATIONS.md) · [Roadmap](#roadmap)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

> The banner renders in yellow→deep-orange gradient when launched in a truecolor terminal.

</div>

---

## What this is

Cahoot — from "in cahoots", working closely together in coordination — is a persistent, terminal-based mission-control plane for multi-agent AI systems. You start it once on a Mac mini or server, leave it running inside a named tmux session, and `ssh box -t tmux attach -t cahoot` from any device to see:

- Which agents are connected, degraded, or offline
- A unified chat/activity timeline across the whole fleet
- Per-agent inspector with status, tasks, version, errors
- Fleet-level metrics: tasks/hour, latency, queue depth, error counts
- A simple command box for `/dm hermes …`, `/all …`, `/restart …`

It's deliberately not a web app. Web dashboards rot, need TLS, need accounts, and feel heavy. A TUI inside tmux is what an operator actually wants for daily use: instant, keyboard-driven, SSH-friendly, no ports to expose.

## Why this exists

Multi-agent systems quickly turn into a wall of terminals — one for the planner, one for the formatter, one for the researcher, and so on. The operator ends up `tmux switch-client`-ing between them, missing events, and losing track of which agent did what to whom.

Cahoot collapses that into one screen. Every agent publishes structured envelopes (chat, status, heartbeat, metrics, errors, tasks) onto a shared bus; the operator sees everything, can address any agent, and can trust that "is it actually running?" has a visible answer.

## Architecture in one breath

```
                     ┌──────────────────────┐
   tmux session ─────┤  Textual TUI shell   │  (operator sees here)
                     └──────────┬───────────┘
                                │
                          ┌─────┴──────┐
                          │  Event bus │  (asyncio queue v1; pluggable)
                          └─────┬──────┘
                ┌───────────────┼───────────────┐
                │               │               │
       ┌────────┴─────┐ ┌───────┴────┐ ┌────────┴─────┐
       │ Hermes adptr │ │ OpenClaw   │ │ Synthetic    │
       └────────┬─────┘ └───────┬────┘ └──────────────┘
                │               │
          (native session) (native session)
```

Each agent talks to its own adapter through the agent's native protocol. The adapter translates inbound and outbound traffic to a single typed `Envelope` and pushes it onto the bus. The TUI subscribes as the operator and renders everything that flows.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design rationale, [`docs/ADAPTERS.md`](docs/ADAPTERS.md) for the adapter contract, and [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the tmux/SSH/launcher patterns.

## Quick start

> Requires **Python 3.11+** and **tmux 3.0+**.

```bash
# clone + install in editable mode
git clone https://github.com/SimonPTucker/cahoot.git
cd cahoot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# run the test suite
pytest

# try the headless runtime with the bundled synthetic adapter
mkdir -p ~/.config/cahoot
cp docs/examples/cahoot.toml ~/.config/cahoot/cahoot.toml
python -m cahoot --no-ui

# follow the log in another pane
tail -F ~/.local/state/cahoot/cahoot.log
```

You should see status transitions, chat lines, heartbeats, and metric events streaming into the log. That confirms the bus, adapter lifecycle, and runtime are all wired correctly.

## Adding real agents — Hermes and OpenClaw

Both **Hermes Agent** ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)) and **OpenClaw** ([docs.openclaw.ai](https://docs.openclaw.ai)) natively expose [Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol) (JSON-RPC over stdio). Cahoot drives them as ACP clients via the canonical `agent-client-protocol` Python package — no custom protocol or shim required.

### 1. Install the agent runtimes

```bash
# Hermes — installed via uv/uvx so Cahoot can pin a specific build
curl -LsSf https://astral.sh/uv/install.sh | sh

# OpenClaw — its CLI handles its own onboarding (gateway URL, token, etc.)
brew install openclaw         # or however your distribution ships it
openclaw onboard              # one-time interactive setup
```

### 2. Install Cahoot with the ACP extra

```bash
pip install -e ".[acp]"
```

(On macOS + Python 3.13, follow up with `chflags -R nohidden .venv` — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for why.)

### 3. Edit `~/.config/cahoot/cahoot.toml`

```toml
[cahoot]
room = "ops"
log_level = "INFO"

# (Optional) lock the fleet down so only listed agents can join.
# Without this block, admission mode defaults to "open" and every
# successfully-spawned agent is admitted as soon as it ACKs the welcome.
[cahoot.admission]
mode = "strict"
allowed_ids = []   # any [[agents]] below are auto-allowlisted in strict mode

# Hermes Agent — the orchestrator.
# `version` pins the uvx --from spec so the build is reproducible.
[[agents]]
id = "hermes-main"
role = "orchestrator"
kind = "hermes"
version = "0.14.0"
cwd = "~/work/project"          # where Hermes runs its ACP session
permission_policy = "auto-allow" # or "deny" for full-stop interactive

# OpenClaw formatter #1 — routes through the OpenClaw Gateway.
# Prefer token_file over inline token so the secret stays out of the config.
[[agents]]
id = "openclaw-formatter-1"
role = "formatter"
kind = "openclaw"
token_file = "~/.openclaw/main.token"
session = "agent:formatter:main"

# OpenClaw formatter #2 — second seat on the same gateway session pool.
[[agents]]
id = "openclaw-formatter-2"
role = "formatter"
kind = "openclaw"
token_file = "~/.openclaw/main.token"
session = "agent:formatter:secondary"
```

### 4. Start Cahoot

```bash
cahoot
```

For each agent block, Cahoot will:

1. **Spawn** the agent process (`uvx --from 'hermes-agent[acp]==0.14.0' hermes-acp` or `openclaw acp --token-file … --session …`).
2. **Run the ACP `initialize` handshake** and open one long-lived session.
3. **Send the welcome prompt** — the agent must reply with the literal token `READY` to confirm it's operational.
4. **Decide admission** — admitted (default) → instructions prompt with the participation rules; quarantined → restricted to operator-only visibility.
5. **Stream the agent's `session/update` notifications** onto the bus as chat / task / metric / status envelopes for your roster, feed, and inspector.

Inside the TUI:

- `/roster` — see every agent, its lifecycle state, and its enrollment.
- `/dm hermes-main please review the release notes` — direct an agent.
- `/all heads up` — broadcast to every other agent.
- `/approve openclaw-formatter-1` — live-admit a quarantined agent without a respawn.
- `/deny openclaw-formatter-1 needs investigation` — quarantine an admitted agent.
- `/whoami` — operator context (hostname, user, tmux, SSH connection).
- `/help` for the full list, `/quit` for a clean shutdown.

The agent-facing instructions (`@mention` routing, structured task / metric / error markers, etc.) are in [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md). Cahoot also auto-sends a condensed version to each agent on admission, so they get the rules in their own context — no system-prompt edit required for agents Cahoot spawned itself.

## Daily operational pattern

On the host that runs your agents:

```bash
# one-time: create a named tmux session that auto-starts Cahoot
tmux new-session -d -s cahoot 'cahoot'
```

From any client (Mac, iPad with Blink, work laptop):

```bash
ssh agents-box -t tmux attach -t cahoot
```

`tmux attach` reconnects you to the live dashboard exactly where you left it. Detach with `Ctrl-b d` and your session keeps running. This is the standard persistent-SSH pattern that works for any long-running TUI.

Mac users: drop the `.app` bundle in `/Applications` (build instructions in [`docs/OPERATIONS.md`](docs/OPERATIONS.md)) to launch the SSH-attach in one click from Spotlight or the Dock. The icon is the bubble-letter "C" from the banner above, with the same yellow→deep-orange gradient — regenerated from `cahoot.banner.BANNER_ART` via `scripts/generate_app_icon.py`.

## Status

**Alpha — v1.0 surface complete** (94/94 tests passing, including 16 end-to-end UI journeys):

- ✅ Typed event envelope (Pydantic v2 discriminated union)
- ✅ In-memory pub/sub bus with bounded subscriber queues + wiretap
- ✅ Adapter ABC with lifecycle, heartbeats, liveness detection, reconnect-with-jitter
- ✅ Synthetic adapter (working reference implementation)
- ✅ Runtime: state dir, single-instance lock, signal handling, rotating logs
- ✅ Config loading from TOML (with admission policy section)
- ✅ **SQLite event store** with WAL, replay on UI mount
- ✅ **Hermes + OpenClaw adapters** via Agent Client Protocol (stdio JSON-RPC)
- ✅ **Agent onboarding handshake** — welcome → ACK → admit → instructions
- ✅ **Textual UI shell** — roster | feed | inspector | command box
- ✅ **Operator commands** — /dm /all /whoami /roster /approve /deny /help /quit
- ✅ **Mac `.app` launcher** — double-click attaches to the tmux session

[`CLAUDE.md`](CLAUDE.md) is the explicit build plan for the remaining phases.

## Roadmap

**v1.0** — the persistent control plane: TUI shell, two real adapters, SQLite store, Mac launcher. Focus is daily-usable mission control, not breadth.

**v1.5** — release watch widget, command palette, transcript search, configurable themes, per-room filtering.

**v2.0** — runtime adapter registration (no restart), multi-process bus (Redis/NATS), remote multi-operator support, audit log export.

Anything that pushes toward "agent OS" or "web dashboard" is out of scope. Cahoot stays a terminal control plane.

## Contributing

Pull requests welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, style, and the test/lint gates.

## License

MIT — see [`LICENSE`](LICENSE).
