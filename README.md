<div align="center">

<img src="docs/assets/banner.svg" alt="Cahoot — mission control for agent fleets" width="820">

**One screen to watch and steer all your AI agents — running on your Mac, reachable from any device over SSH.**
A terminal-native operator console for multi-agent AI orchestration, built to live in a long-running `tmux` session on an always-on Apple Silicon Mac.

[How it works](#how-it-works) · [Writing adapters](docs/ADAPTERS.md) · [Operations](docs/OPERATIONS.md) · [Roadmap](#roadmap)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

</div>

![Cahoot live in tmux — roster on the left, unified feed in the centre, per-agent inspector on the right, operator command box at the bottom.](docs/assets/screenshot.png)

---

## Is this for you?

**Use Cahoot if** you run two or more AI agents at once and want them on a single screen; you're comfortable in a terminal with `tmux` and `ssh`; you have (or want) an always-on Apple Silicon Mac as the host — a Mac mini is the canonical case.

**Not for you (yet) if** you want a browser dashboard with logins; you only run a single agent; you need a hosted multi-tenant service. Cahoot is also **alpha** — solid foundations and a green test suite, but the surface keeps changing.

## What this is

**The pain.** Multi-agent setups quickly become a wall of terminals — one for the planner, one for the formatter, one for the researcher, and so on. The operator ends up `tmux switch-client`-ing between them, missing events, losing track of which agent did what to whom.

**The payoff.** Cahoot collapses that into one screen. You start it once on a dedicated Mac (typically a Mac mini sat on a shelf), leave it running inside a named `tmux` session, and `ssh box -t tmux attach -t cahoot` from any device — laptop, iPad, work machine — to see:

- Which agents are connected, degraded, or offline
- A unified chat / activity timeline across the whole fleet
- A per-agent inspector with status, tasks, version, last error
- Fleet-level counters: tasks running, tokens used, errors emitted
- A simple command box: `/dm hermes-main please review`, `/all heads up`, `/approve openclaw-1`, …

Coordination stays on your own machine. Cahoot does **not** route your fleet through a third-party chat service — there is no Telegram, Slack or Discord bridge, no bot tokens to manage, no message data leaving your box, and nothing that breaks when someone else's API has an outage. The operator-to-agent channel is local, in the terminal, over an SSH connection you already trust.

## Why this exists

Two problems showed up the moment a "personal AI fleet" became a realistic thing for one person to run:

1. **The wall of terminals.** Each agent gets its own window or tab; the operator becomes a switchboard. Important events scroll past unnoticed, and "is that agent even still running?" doesn't have an obvious answer.
2. **The chat-bridge tax.** The usual workaround is to pipe everything into a Slack, Discord or Telegram channel and reply from there. That works until you remember it routes your work through someone else's account, depends on someone else's uptime, leaks message data off your machine, and turns "start an agent" into "manage another bot token".

Cahoot's answer is to put both problems on one screen on your own box. Every agent publishes structured events onto a shared local bus; the operator sees everything and can address any agent with a slash command. No browser, no port, no third-party API, no bot account — just a terminal you SSH into.

A TUI in `tmux` is what an operator actually wants for daily use: instant, keyboard-driven, SSH-friendly, persistent across reboots, nothing to expose. Web dashboards rot, need TLS, need accounts. They're the wrong tool for this job.

## Words you'll see

A short glossary, because the rest of the document is denser if these mean different things to you than they do here.

| Term | In Cahoot |
|---|---|
| **agent** | An external AI process Cahoot supervises — e.g. one Hermes instance, one OpenClaw seat. |
| **fleet** | All the agents Cahoot has connected at once. |
| **operator** | You, the human at the keyboard. Always sees every event. |
| **adapter** | A small piece of code per *kind* of agent (Hermes / OpenClaw / synthetic) that knows how to spawn it, talk to it, and translate its protocol to Cahoot's events. |
| **envelope** | One typed, immutable event flowing on the bus — a chat line, status change, heartbeat, metric, task update, or error. |
| **bus** | The in-process pub/sub channel every envelope goes through. The operator and every adapter subscribe to it. |
| **room** | A label for grouping envelopes (default `ops`). Useful when you want separate streams for separate projects. |
| **TUI** | Terminal user interface — Cahoot's screen, drawn by the [Textual](https://textual.textualize.io/) library. |
| **admission** / **quarantine** | Whether a connected agent is fully part of the fleet (`admitted`) or restricted to the operator only (`quarantined`). |
| **enrollment** | The handshake — welcome prompt → agent ACKs with `READY` → admission decision → instructions prompt — that an ACP agent goes through on connect. |

## Quick start (no real agents needed)

The fastest way to see Cahoot working is with the bundled **synthetic adapter** — a fake agent that ticks every few seconds and exercises every code path the real adapters use. No Hermes install, no OpenClaw account, no API key.

> Tested on Apple Silicon (M-series) macOS with Python 3.11 / 3.12 / 3.13. CI also runs the test suite on Ubuntu. Other Unix-likes will probably work; Windows is untested. You need **Python 3.11+** ([downloads](https://www.python.org/downloads/)) and **tmux 3.0+** (`brew install tmux`).

```bash
# clone + install in editable mode
git clone https://github.com/SimonPTucker/cahoot.git
cd cahoot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# (macOS + Python 3.13 only) the editable install ends up "hidden"; un-hide
# it so the cahoot CLI can find itself — see CONTRIBUTING.md for why.
chflags -R nohidden .venv 2>/dev/null || true

# run the full test suite — should report 94 passed
pytest

# drop in the bundled example config and launch the UI
mkdir -p ~/.config/cahoot
cp docs/examples/cahoot.toml ~/.config/cahoot/cahoot.toml
cahoot
```

In about two seconds you should see the four-region dashboard: a roster on the left with the synthetic agent ticking, a feed in the middle filling with chat lines and metrics, an inspector on the right tracking the agent's counters, and a command box at the bottom. Try `/help`, then `/whoami`, then `/quit`.

If you see all that, every layer — bus, store, adapter lifecycle, UI — is wired correctly. The next section swaps the synthetic agent for real ones.

## Connecting agents from elsewhere on the LAN

You can run agents on the same Mac as Cahoot — the next section walks through that — but most real fleets have the agent processes spread across several machines on the same LAN. The Mac mini sits in the cupboard running Cahoot; the agents run on a workstation, a beefier GPU box, an old laptop. Cahoot has an **inbound onboarding mode** for that case.

The flow has three steps:

**Step 1 — turn on the listener.** Add this to your `~/.config/cahoot/cahoot.toml` and restart Cahoot:

```toml
[cahoot.listener]
enabled = true     # listen for inbound cahoot-join connections
bind    = "0.0.0.0"  # accept from any interface on the LAN
port    = 9876
invite_ttl_s = 1800  # tokens expire after 30 minutes
```

Cahoot logs `listener: announcing ws://<your-host>:9876 for invites` on startup.

**Step 2 — mint an invite from the TUI.** Type:

```
/invite hermes-main planner
```

Cahoot prints a copy-pasteable block right in the feed:

```
invite for hermes-main (role: planner)
  token expires in 30 minutes; single-use
  paste this on the box where the agent will live:

    cahoot-join \
      --server ws://my-mac-mini.local:9876 \
      --token CH7-9X42-8K3M \
      --as hermes-main --role planner \
      --kind hermes \
      -- uvx --from 'hermes-agent[acp]' hermes-acp
```

`/invites` lists everything outstanding; tokens are single-use and time-bounded.

**Step 3 — run that command on the agent's box.** On the workstation (or whichever machine the agent will actually live on), install Cahoot with the network extra and paste the command:

```bash
pip install -e ".[acp,network]"      # acp for hermes/openclaw, network for the bridge
cahoot-join \
  --server ws://my-mac-mini.local:9876 \
  --token CH7-9X42-8K3M \
  --as hermes-main --role planner \
  --kind hermes \
  -- uvx --from 'hermes-agent[acp]' hermes-acp
```

`cahoot-join` spawns the agent locally on that machine, opens a WebSocket to Cahoot, validates the token, and bridges the two. From Cahoot's point of view the agent appears in the roster, goes through the standard welcome → `READY` → admission → instructions handshake, and accepts `/dm`, `/approve`, `/deny` exactly like a locally-spawned one. `Ctrl-C` on the agent's box (or `tmux kill-session`, or unplugging the network cable) cleanly disconnects it; the slot in Cahoot frees up and the operator sees a status drop.

The wire format is a tiny JSON-over-WebSocket protocol with one frame per envelope, documented in [`cahoot/adapters/remote.py`](cahoot/adapters/remote.py). There's **no TLS in v1** — the trust boundary is "your LAN", and the token plus single-use semantics gate authentication. v1.5 will add `wss://` + a self-signed cert and an operator-driven approval queue.

If you don't yet have a real agent, you can still test the whole inbound path with the synthetic adapter:

```bash
cahoot-join \
  --server ws://my-mac-mini.local:9876 \
  --token CH7-9X42-8K3M \
  --as remote-synth-1 --role tester \
  --kind synthetic
```

The next section is the alternative path — letting Cahoot spawn the agent locally on its own machine. Use that when you only have one box and don't want a separate `cahoot-join` process.

## Adding real agents — Hermes and OpenClaw

Both **Hermes Agent** ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)) and **OpenClaw** ([docs.openclaw.ai](https://docs.openclaw.ai)) natively expose [Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol) — JSON-RPC over stdio, the same protocol an IDE like Zed uses to drive them. Cahoot speaks the canonical client side via the official `agent-client-protocol` Python package, so there's no custom protocol or shim to maintain.

A glossary footnote, since one acronym and two tools may be new:

- **`uv` / `uvx`** is the Python package launcher from [Astral](https://docs.astral.sh/uv/) that Hermes recommends — it pins exact versions of Hermes's runtime without polluting your environment.
- **`brew`** is [Homebrew](https://brew.sh/), the standard macOS package manager.
- **ACP** is the [Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol) — a JSON-RPC dialect for an editor (or in our case Cahoot) to drive an AI agent over stdio.

### What goes in each `[[agents]]` block

Before the recipe, the field semantics — because three of these fields look like reserved keywords but are actually labels you choose:

| Field | What it is | You pick? |
|---|---|---|
| `id` | A short unique label for this agent. It's what appears in the roster and what you type after `/dm`. Convention: kebab-case with a hint if you'll run multiples (`hermes-main`, `openclaw-1`). | **Yes — anything unique.** |
| `role` | A **sticky note you put on the agent so you can tell which is which.** It lives entirely in Cahoot's world — it is *not* sent into the agent's configuration, model, system prompt, tools or capabilities. Cahoot uses it for two things: display in the roster widget, and `@<role>` mention routing. The agent's actual behaviour comes from its own setup (Hermes profile / OpenClaw session). | **Yes — anything.** |
| `kind` | Which adapter Cahoot should spawn. **Reserved**: must be one of `synthetic`, `hermes`, `openclaw`. Adding a new kind is a one-line registry edit — see [`docs/ADAPTERS.md`](docs/ADAPTERS.md). | No — must match a registered kind. |
| `version` | (Optional, Hermes-only) Pins the `uvx` build of Hermes so the spawned binary is reproducible. | Up to you (defaults to latest on PyPI). |
| `cwd` | Working directory the agent will run in. | Yes. |
| `permission_policy` | (Optional, ACP adapters) `auto-allow` (default) admits every tool call the agent asks to run; `deny` blocks them all. v1.5 will add an interactive prompt. | One of `auto-allow` \| `deny`. |
| Anything else | Forwarded to the adapter constructor as keyword arguments. Hermes has no extras; OpenClaw accepts `token`, `token_file`, `session`, `session_label`, `gateway_url`, `reset_session`, `profile`. | Yes — these are OpenClaw's own CLI flags (`openclaw acp --help`). |

> **The point:** `kind` is reserved. Everything else — including `role` — is a label you make up to help yourself read the screen. Two `role = "writer"` seats are not a Cahoot setting that makes them write; what they actually do is determined by their own Hermes / OpenClaw configuration.

### 1. Install the agent runtimes

```bash
# Hermes — installed via uv/uvx so Cahoot can pin a specific build
curl -LsSf https://astral.sh/uv/install.sh | sh

# OpenClaw — its CLI handles its own onboarding (gateway URL, token, etc.)
brew install openclaw         # or whichever distribution channel you use
openclaw onboard              # one-time interactive setup
```

### 2. Install Cahoot with the ACP extra

```bash
pip install -e ".[acp]"
```

(On macOS + Python 3.13, follow up with `chflags -R nohidden .venv` — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for why.)

### 3. Edit `~/.config/cahoot/cahoot.toml`

Below is a realistic three-agent fleet — **one Hermes + two OpenClaw seats**. Comments flag which fields are sticky-note labels you chose vs reserved keywords.

```toml
[cahoot]
room = "ops"
log_level = "INFO"

# Optional: gate who can join the fleet. Without this block, admission
# defaults to "open" and every agent Cahoot spawns is admitted as soon
# as it replies READY to the welcome prompt.
[cahoot.admission]
mode = "strict"     # "open" (default) or "strict"
allowed_ids = []    # extra IDs to allow on top of the [[agents]] list

# ─── Agent 1: Hermes ──────────────────────────────────────────────────
[[agents]]
id   = "hermes-main"   # sticky-note label, must be unique
role = "planner"       # sticky-note label, anything readable
kind = "hermes"        # RESERVED — must be the literal string "hermes"
version = "0.14.0"     # pins uvx --from hermes-agent[acp]==0.14.0
cwd  = "~/work/project"
permission_policy = "auto-allow"   # auto-allow | deny

# ─── Agents 2 + 3: two OpenClaw seats ─────────────────────────────────
# What makes seat #2 different from seat #1 is the `session` they each
# point at (writer:main vs writer:secondary), NOT the role label.
[[agents]]
id   = "openclaw-1"
role = "writer"
kind = "openclaw"      # RESERVED
token_file = "~/.openclaw/main.token"     # path to your real token file
session    = "agent:writer:main"          # your OpenClaw Gateway session ID

[[agents]]
id   = "openclaw-2"
role = "writer"
kind = "openclaw"
token_file = "~/.openclaw/main.token"
session    = "agent:writer:secondary"
```

**About OpenClaw's `session` value.** OpenClaw uses a structured session string (`agent:<name>:<profile>`) to address a specific seat inside its Gateway — see `openclaw acp --help`. The names `writer:main` and `writer:secondary` are placeholders; substitute the session IDs configured inside your own OpenClaw.

### 4. Start Cahoot

```bash
cahoot
```

For each `[[agents]]` block, Cahoot will:

1. **Spawn** the agent process (`uvx --from 'hermes-agent[acp]==0.14.0' hermes-acp` or `openclaw acp --token-file … --session …`).
2. **Run the ACP initialise handshake** and open one long-lived session.
3. **Send the welcome prompt.** The agent must reply with the literal token `READY` to confirm it's operational.
4. **Decide admission.** Admitted → instructions prompt with the participation rules. Quarantined → operator-only visibility until `/approve`.
5. **Stream the agent's notifications** onto the bus as chat / task / metric / status envelopes that the roster, feed and inspector all render live.

Inside the TUI you have:

- `/roster` — every agent, its lifecycle state, and its enrollment.
- `/dm hermes-main please review the release notes` — message one agent.
- `/all heads up` — broadcast to every other agent.
- `/approve openclaw-1` — live-admit a quarantined agent without a respawn.
- `/deny openclaw-1 needs investigation` — quarantine an admitted agent (the agent gets a notice).
- `/whoami` — operator context: hostname, user, tmux socket, SSH connection.
- `/help` for the full list; `/quit` for a clean shutdown.

Agents Cahoot spawned itself get a condensed participation guide automatically over ACP after admission — no system-prompt editing required. The canonical, copy-pasteable version is in [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md) if you bootstrap an agent outside Cahoot.

## How it works

```
                     ┌──────────────────────┐
   tmux session ─────┤  Textual TUI shell   │  (operator sees here)
                     └──────────┬───────────┘
                                │
                          ┌─────┴──────┐
                          │  Event bus │  (in-process asyncio, pluggable)
                          └─────┬──────┘
                ┌───────────────┼───────────────┐
                │               │               │
       ┌────────┴─────┐ ┌───────┴────┐ ┌────────┴─────┐
       │   Hermes     │ │  OpenClaw  │ │  Synthetic   │
       │   adapter    │ │  adapter   │ │  adapter     │
       └────────┬─────┘ └───────┬────┘ └──────────────┘
                │               │
        (native ACP stdio) (native ACP stdio)
```

Each agent talks to its own adapter through the agent's native protocol. The adapter translates inbound and outbound traffic to a single typed `Envelope` and pushes it onto the bus. The TUI subscribes as the operator and renders everything that flows. A SQLite event store wiretaps the bus, so every envelope is persisted; on restart the feed backfills from there.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design rationale, [`docs/ADAPTERS.md`](docs/ADAPTERS.md) for the adapter contract, and [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the `tmux` / SSH / launcher patterns.

## Daily operational pattern

A named `tmux` session keeps Cahoot running on the host even after you disconnect, so reattaching over SSH drops you back into the live screen exactly where you left it. The standard pattern:

```bash
# On the host that runs your agents — one-time setup.
tmux new-session -d -s cahoot 'cahoot'

# From any client (Mac, iPad with Blink, work laptop):
ssh agents-box -t tmux attach -t cahoot
```

Detach with `Ctrl-b d`; your session keeps running. Reattach later with the same `ssh … attach` command. Everything you missed while disconnected is in the feed (and persisted to SQLite).

Mac users: drop the `.app` bundle from `scripts/Cahoot.app` into `/Applications`, and double-clicking it from Finder, Spotlight or the Dock opens Terminal directly into the live session. Set `CAHOOT_HOST=agents-box` in your environment if Cahoot runs on a different machine than the one you're double-clicking from. Full launcher details, including code-signing notes, are in [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Status

**Alpha — v1.0 surface complete.** 94/94 tests passing locally and in CI (Ubuntu + macOS × Python 3.11 + 3.12), including 16 end-to-end UI journey tests that drive the actual `ConnApp` through Textual's `run_test` pilot.

- ✅ Typed event envelope (Pydantic v2 discriminated union)
- ✅ In-process pub/sub bus with bounded subscriber queues + wiretap
- ✅ Adapter lifecycle: heartbeats, liveness detection, reconnect with full-jitter backoff
- ✅ Runtime: XDG state dir, single-instance lock, signal handling, rotating logs
- ✅ Config loading from TOML (with admission policy)
- ✅ SQLite event store with WAL, replay on UI mount
- ✅ Hermes + OpenClaw adapters via Agent Client Protocol
- ✅ Agent onboarding handshake — welcome → ACK → admit → instructions
- ✅ Textual UI shell — roster | feed | inspector | command box
- ✅ Operator commands — `/dm` `/all` `/whoami` `/roster` `/approve` `/deny` `/help` `/quit`
- ✅ Mac `.app` launcher

The platform target is Apple Silicon macOS — the only target where the `.app` is supported and where the project is run day-to-day. CI verifies the test suite on Ubuntu as well, and other Unix-likes will probably work, but they aren't a supported target today. Windows is untested.

[`CLAUDE.md`](CLAUDE.md) is the build plan for the remaining phases.

## Roadmap

**v1.0** — the persistent control plane: TUI shell, two real adapters, SQLite store, Mac launcher. Focus is daily-usable mission control, not breadth.

**v1.5** — release watch widget, command palette, transcript search, configurable themes, per-room filtering.

**v2.0** — runtime adapter registration without restart, multi-process bus (Redis / NATS), remote multi-operator support, audit log export.

Out of scope, deliberately: web dashboard or REST/GraphQL API, hosted multi-tenant version, "agent OS" framing. Cahoot stays a terminal control plane on your own box.

## Contributing

Pull requests welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, the four-gate test/lint protocol, and the macOS + Python 3.13 install gotcha.

## License

MIT — see [`LICENSE`](LICENSE).
