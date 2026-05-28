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
- 51 passing tests covering envelope roundtrip, bus routing and backpressure, adapter lifecycle, reconnect, inbox overflow, banner gradient and colour detection, ACP onboarding handshake, admission policy, @mention routing, quarantine gating.

### Not yet built (see [`CLAUDE.md`](CLAUDE.md) for the explicit phases)
- SQLite persistence and replay (phase 1).
- Textual UI shell (phase 3).
- Command box with `/dm`, `/all`, `/whoami`, `/where`, `/approve` (phase 4).
- Mac `.app` launcher bundle (phase 5).
