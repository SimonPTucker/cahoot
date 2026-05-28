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
- 27 passing tests covering envelope roundtrip, bus routing and backpressure, adapter lifecycle, reconnect, inbox overflow, banner gradient and colour detection.

### Not yet built (see [`CLAUDE.md`](CLAUDE.md) for the explicit phases)
- SQLite persistence and replay (phase 1).
- Concrete `HermesAdapter` and `OpenClawAdapter` (phase 2).
- Textual UI shell (phase 3).
- Command box with `/dm`, `/all`, `/whoami`, `/where` (phase 4).
- Mac `.app` launcher bundle (phase 5).
- GitHub Actions CI matrix (phase 6).
