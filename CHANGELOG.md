# Changelog

All notable changes to Cahoot are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Typed event envelope (`Envelope`) with Pydantic v2 discriminated union over `ChatPayload`, `StatusPayload`, `HeartbeatPayload`, `MetricPayload`, `TaskPayload`, `ErrorPayload`, `ReleasePayload`.
- In-memory pub/sub `Bus` Protocol with `InMemoryBus` implementation, bounded subscriber queues, drop-oldest backpressure, source-aware broadcast routing.
- `AgentAdapter` ABC with full lifecycle (`OFFLINE → CONNECTING → CONNECTED ⇄ DEGRADED → DISCONNECTED`), heartbeat liveness detection, exponential backoff with full jitter on reconnect ([AWS pattern](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)), structured error envelopes.
- `SyntheticAdapter` reference implementation with configurable chatter interval and drop probability for exercising reconnect paths.
- Runtime: XDG-compliant state directory, POSIX advisory single-instance lock via `fcntl.flock`, rotating file logging (5 MB × 3), signal handlers for `SIGINT`/`SIGTERM`/`SIGHUP` that translate to clean shutdown.
- TOML configuration loader with `$CAHOOT_CONFIG` → `$XDG_CONFIG_HOME/cahoot/cahoot.toml` → `./cahoot.toml` lookup order.
- Startup splash banner (`cahoot/banner.py`) with 24-bit truecolor gradient (yellow → deep orange), `NO_COLOR` and non-TTY fallback to plain text.
- `ACPAdapter` base + `HermesAdapter` and `OpenClawAdapter` concrete subclasses speaking [Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol) over stdio via the `agent-client-protocol` Python package. Translates session/update notifications → envelopes (chat / thought / tool call / usage / status) and outbound chat → ACP `prompt` requests with `TextContentBlock`. Auto-allows tool calls by default; configurable via `permission_policy`.
- Agent onboarding handshake (`cahoot/onboarding.py`): welcome prompt → `READY` ACK with configurable timeout → admission decision → instructions prompt (or quarantine notice). Agents are forwarded `[source] …`-prefixed messages and route their own replies via `@mention` parsing (`@operator`, `@all`, `@<agent_id>`, `@<role>`).
- Admission policy (`cahoot/admission.py`) with `open` (default) and `strict` modes. Strict mode implicitly trusts every agent listed in `[[agents]]` plus any explicit `allowed_ids`; everyone else lands in `QUARANTINED` with operator-only visibility.
- `docs/AGENT_GUIDE.md` — canonical agent-facing instructions, copy-pasteable as a system prompt for agents bootstrapped outside Cahoot. Same content is auto-sent (condensed) over ACP after admission.
- SQLite event store (`cahoot/store.py`) — single-table append-only with WAL mode, JSON payloads, indices on ts / room / source. Registered as a bus wiretap subscriber so every envelope persists in lockstep with delivery. `recent()` / `by_agent()` / `replay_into()` for UI backfill on restart.
- Bus `wiretap` flag — subscribers receive every envelope regardless of target. Used by the store and prospective audit-log subscribers.
- Runtime admit / quarantine on `ACPAdapter` — public `admit()` / `quarantine()` methods drive enrollment state without respawning the agent and send a one-line runtime notice over ACP so the agent learns about the change immediately.
- Operator command parser + executor (`cahoot/commands.py`) — `/dm <agent> <text>`, `/all <text>`, plain text → broadcast, `/whoami`/`/where`, `/roster`/`/agents`/`/fleet`, `/approve <agent>`, `/deny <agent> [reason]`, `/help`, `/quit`. Parsing is pure and trivially unit-testable.
- Textual UI shell (`cahoot/ui/`) — 4-region layout (roster | feed | inspector | command box). Roster colours status dots by liveness and shows enrollment badge. Feed renders chat / status / error / task / metric envelopes with per-kind styling, suppresses heartbeats. Inspector tracks per-agent counters + last error + last task. Backfills from the event store on mount and consumes the operator queue thereafter.
- Mac `.app` launcher (`scripts/Cahoot.app/`) — minimal Info.plist + AppleScript wrapper that opens Terminal attached to the tmux session. Configurable via `CAHOOT_HOST` / `CAHOOT_SESSION` / `CAHOOT_CMD` env vars.
- 75 passing tests covering envelope roundtrip, bus routing / backpressure / wiretap, adapter lifecycle / reconnect, banner gradient, ACP onboarding handshake, admission policy, @mention routing, quarantine gating, store CRUD + replay + WAL, command parser + executor, Textual UI smoke (boot + command + envelope dispatch).

### Added (Phase B — network onboarding)
- `cahoot/invites.py` — single-use, time-limited join tokens (`CH7-XXXX-YYYY`). In-memory registry with `mint` / `redeem` / `revoke` / `prune_expired`. Tokens bound to a specific `agent_id` + role; intercepted tokens can't be used to register a different agent.
- `cahoot/listener.py` — WebSocket server (default `0.0.0.0:9876`) that accepts inbound connections from `cahoot-join`. Hello → validate token → welcome → spawn `RemoteAdapter`. Duplicate `agent_id` connections refused with a clear reason.
- `cahoot/adapters/remote.py` — `RemoteAdapter` wraps an accepted WebSocket connection; implements the same `AgentAdapter` surface as other kinds (`admit()` / `quarantine()` etc.) so `/approve` and `/deny` work end-to-end across the network. One-shot lifecycle: when the bridge disconnects the adapter exits cleanly without trying to reconnect.
- `cahoot/cahoot_join.py` — new `cahoot-join` CLI entry point. Runs on the agent's box; spawns the local agent via the existing adapter registry, opens a WebSocket to Cahoot, and bridges in both directions via an in-process `RemoteBridgeBus`. `admit` / `quarantine` control frames from Cahoot are forwarded to the local adapter so runtime admission control works across the LAN.
- New operator commands: `/invite <agent_id> [role]` mints a token and prints a copy-pasteable `cahoot-join` block in the feed. `/invites` (alias `/tokens`) lists outstanding invitations.
- `[cahoot.listener]` config section: `enabled`, `bind`, `port`, `invite_ttl_s`. Off by default.
- Optional `[network]` extra pulling in `websockets>=12.0` for the server + bridge.
- 13 new tests covering token lifecycle (mint, redeem, expiry, revocation, wrong-agent-id rejection) and end-to-end listener handshakes (invalid token, valid token, inbound envelope translation, duplicate connection refusal). Total: 107 passing.

### Added (Phase B.1 — mDNS / Bonjour discovery)
- `cahoot/discovery.py` — async wrappers around the `zeroconf` package for advertising and browsing `_cahoot._tcp.local.`. Service name = short hostname; TXT record carries protocol version, room, full hostname, and proto (`ws`).
- Listener now optionally advertises on startup. `[cahoot.listener].advertise` (default `true`) controls it; turn off if you're running multiple Cahoot instances and don't want them visible.
- `cahoot-join` now supports auto-discovery: omit `--server` (or pass `--server auto`) and the bridge browses for 2.5s and connects to whatever it finds. `--server-name <name>` picks one when several are visible; `--list` enumerates them and exits.
- 4 new tests cover advertise + browse roundtrip on loopback (single instance, two instances simultaneously, empty-LAN baseline, service-type constant). Total: 111 passing.
- `zeroconf>=0.131` added to the `[network]` extra (pure-Python, no system deps).
- New `docs/ONBOARDING.md` — comprehensive end-to-end guide to network agent onboarding: architecture diagram, five-step flow, full wire-frame catalogue, invite token semantics, discovery details, admission modes, disconnection semantics, security model, troubleshooting playbook, cheat sheet.

### Added (Phase B.2 — local runtime auto-detection)
- `cahoot/local_detect.py` — `RuntimeProbe` dataclass + `detect_hermes()` / `detect_openclaw()` / `detect_synthetic()` + `pick_default()`. Hermes is "available" if `uvx` is on PATH (Hermes itself is fetched on demand); OpenClaw is "available" if its CLI is on PATH, and if `~/.openclaw/main.token` exists, it's auto-suggested as `token_file`.
- `cahoot-join --detect` prints the report and exits. Useful before pasting an invite.
- `cahoot-join` without `--kind` auto-picks the single available real runtime. Both installed → demands `--kind`. Neither → prints install hints and exits non-zero. Synthetic is intentionally never auto-picked.
- `/invite` output simplified: the canonical block is now two arguments (`--token`, `--as`/`--role`) and a one-liner aside about how to add `--kind` / `--server` if either auto-discovery is unavailable.
- 15 new tests cover every probe + every pick-default branch + the format-report tips. Total: 126 passing.
