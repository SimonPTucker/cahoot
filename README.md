<div align="center">

<img src="docs/assets/banner.svg" alt="Cahoot — mission control for agent fleets" width="640">

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

The Textual UI is the next build phase — see [`CLAUDE.md`](CLAUDE.md) for the explicit task list.

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

Mac users: drop the `.app` bundle in `/Applications` (build instructions in [`docs/OPERATIONS.md`](docs/OPERATIONS.md)) to launch the SSH-attach in one click from Spotlight or the Dock.

## Status

**Alpha.** Foundational layer is built and tested (16/16 passing):

- ✅ Typed event envelope (Pydantic v2 discriminated union)
- ✅ In-memory pub/sub bus with bounded subscriber queues
- ✅ Adapter ABC with lifecycle, heartbeats, liveness detection, reconnect-with-jitter
- ✅ Synthetic adapter (working reference implementation)
- ✅ Runtime: state dir, single-instance lock, signal handling, rotating logs
- ✅ Config loading from TOML
- ⏳ Textual UI (next)
- ⏳ SQLite persistence and replay
- ⏳ Concrete Hermes and OpenClaw adapters
- ⏳ Mac `.app` launcher bundle

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
