# CLAUDE.md — build guidance for Claude Code

This file is the contract between the project author and Claude Code. **Read it before any non-trivial change, and read the linked design docs before starting a phase.**

If you find yourself wanting to deviate from anything in this file, stop and propose the change first — don't silently re-architect.

---

## 1. Project in one paragraph

Cahoot is a persistent, terminal-native mission-control TUI for multi-agent AI orchestration. It runs inside a long-lived tmux session on a Mac mini or server. Each agent (Hermes, OpenClaw, future specialists) has a native adapter that translates the agent's protocol to a typed `Envelope` on a shared asyncio bus. The Textual UI subscribes to the bus as the operator and renders everything that flows. Adding a new agent is a config edit, not a rewrite.

## 2. Where you are in the build

**Built and tested (do not change without discussion):**

| Layer | File | Status |
|---|---|---|
| Event envelope | `cahoot/envelope.py` | ✅ Pydantic v2 discriminated union |
| Bus | `cahoot/bus.py` | ✅ In-memory pub/sub with bounded queues |
| Adapter ABC | `cahoot/adapter.py` | ✅ Lifecycle, heartbeats, reconnect |
| Synthetic adapter | `cahoot/adapters/synthetic.py` | ✅ Working reference |
| Runtime | `cahoot/runtime.py` | ✅ Signals, lockfile, XDG paths, logging |
| Config | `cahoot/config.py` | ✅ TOML loader |
| Splash banner | `cahoot/banner.py` | ✅ Truecolor gradient render |
| Entry point | `cahoot/__main__.py` | ✅ Headless monitor; UI hook is a TODO |
| Tests | `tests/` | ✅ 27/27 passing |

**To build, in order (see §6 for detail):**

1. SQLite persistence + replay
2. Concrete `HermesAdapter` and `OpenClawAdapter`
3. Textual UI shell (4-region layout)
4. Command box + routing (`/dm`, `/all`, `/whoami`, `/where`)
5. Inspector drawer + sparkline widgets
6. Mac `.app` launcher
7. CI workflow

## 3. Architectural invariants — DO NOT VIOLATE

These were chosen deliberately. Deviating from any will require redesigning multiple layers.

1. **Envelopes are immutable values.** `Envelope` and every payload class use `model_config = ConfigDict(frozen=True, extra="forbid")`. If you find yourself wanting to mutate a published envelope, you want a new envelope.

2. **Adapters never own the bus.** The bus is injected; adapters depend on the `Bus` Protocol in `cahoot/bus.py`, never on `InMemoryBus`. When we swap in a SQLite-backed bus in phase 1, no adapter code changes.

3. **All inbound traffic goes through `_publish_from_agent`.** Adapter subclasses must call `self._publish_from_agent(env)` (not `self.bus.publish(env)`) from `_read_loop`. This updates the liveness timestamp so DEGRADED detection works.

4. **Errors are envelopes, not exceptions.** When the adapter encounters a transport problem, it emits an `ErrorPayload` and continues (or reconnects). Exceptions only propagate out of the adapter on programmer error — never on routine network failures.

5. **One process, one tmux session, one operator.** Single-instance is enforced by `runtime.single_instance_lock()`. Do not work around it. Multiple operators is a v2 feature.

6. **No silent state mutation outside the bus.** If an agent's state changes, an envelope is published. The UI reads state by subscribing — it never asks an adapter "what state are you in?" via a side channel.

7. **Async-only.** No `time.sleep`, no `threading`, no blocking IO on the event loop. Use `asyncio.sleep`, `asyncio.Queue`, `aiosqlite`. If you must call a blocking library, wrap it in `asyncio.to_thread`.

8. **No new runtime dependencies without justification.** The current set is intentionally small: `pydantic`, `textual`, `rich`, `structlog`, `aiosqlite`. Adding anything else needs a sentence of why in the PR description.

9. **No web dashboard. No browser. No HTTP server.** This is a terminal application. If you find yourself adding `fastapi` or `flask`, stop.

10. **Tests pass before you push.** `pytest` is green right now. Keep it green.

## 4. Conventions

### Python style

- Python 3.11+ required. Use `match`, `except*`, modern type syntax (`X | None`, `list[T]`).
- Type hints on every public function. Strict mypy compliance — see `pyproject.toml`.
- `from __future__ import annotations` at the top of every module.
- Module docstrings explain *why* the module exists, not what it does.
- Public surface is declared via `__all__`.

### Async patterns

- One event loop, one process. Don't create nested loops.
- `asyncio.TaskGroup` is fine for short-lived structured concurrency; the adapter's long-lived loops use plain `create_task` + explicit `cancel` (see `adapter.py` for the pattern).
- Wrap any potentially-failing operation in a `try`/`except` that emits an `ErrorPayload` on the bus. **Silent failures are a bug.**
- Always handle `asyncio.CancelledError` by re-raising. Never swallow it.

### Logging

- Use `logging.getLogger(__name__)`, never `print`.
- Logs go to a file (`~/.local/state/cahoot/cahoot.log`). Textual owns the TTY; stray `print` corrupts rendering.
- Use structured records: `log.info("adapter %s reconnect in %.2fs (attempt %d)", agent_id, delay, attempt)`.

### Tests

- `pytest-asyncio` in auto mode. Mark async tests with the file-level `pytestmark = pytest.mark.asyncio`.
- Test names describe behaviour: `test_drop_oldest_on_full_subscriber`, not `test_bus_2`.
- Use `asyncio.wait_for(..., timeout=...)` on every queue read so a hung test fails in seconds, not minutes.

## 5. Commands

```bash
# install dev environment
pip install -e ".[dev]"

# run tests
pytest                       # all
pytest tests/test_adapter.py # one file
pytest -k reconnect          # by keyword
pytest --cov=cahoot            # with coverage

# lint and type-check
ruff check .
ruff format .
mypy cahoot

# run the app
python -m cahoot               # uses default config lookup
python -m cahoot --no-ui       # headless (current default until UI ships)
python -m cahoot -c path/to/cahoot.toml

# follow the log
tail -F ~/.local/state/cahoot/cahoot.log
```

## 6. Build phases — explicit task lists

Each phase has acceptance criteria. Don't move on until the previous phase meets them.

### Phase 1 — SQLite persistence and replay

**Goal:** every envelope is persisted; the UI can replay history on attach.

**Read first:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §"Persistence"

**Tasks:**

1. Add `cahoot/store.py` with an `EventStore` class:
   - `async def append(env: Envelope) -> None`
   - `async def recent(limit: int = 200, room: str | None = None) -> list[Envelope]`
   - `async def by_agent(agent_id: str, limit: int = 100) -> list[Envelope]`
   - Use `aiosqlite` with WAL mode enabled (`PRAGMA journal_mode=WAL`).
   - Schema: single `events` table with columns `(id TEXT PK, ts TEXT, source TEXT, target TEXT, room TEXT, kind TEXT, payload JSON)`.
   - Index on `(ts)`, `(source)`, `(room, ts)`.
2. Wire `EventStore.append` into the bus as a fan-out subscriber so persistence is automatic.
3. Add a `replay_into(bus)` helper so the UI can backfill recent events on connect.

**Acceptance:**
- `pytest tests/test_store.py` covers append, recent (filtered by room), by_agent, and replay roundtrip.
- Running `python -m cahoot --no-ui` for a minute then restarting should preserve the log of events in `~/.local/state/cahoot/cahoot.db`.
- WAL mode confirmed via `sqlite3 ~/.local/state/cahoot/cahoot.db 'PRAGMA journal_mode;'`.

### Phase 2 — concrete adapters

**Goal:** Hermes and OpenClaw are first-class citizens, not synthetic.

**Read first:** [`docs/ADAPTERS.md`](docs/ADAPTERS.md), then the agent's own protocol docs.

**Tasks:**

1. `cahoot/adapters/hermes.py` with `HermesAdapter(AgentAdapter)`:
   - Wrap the existing Hermes runtime connection.
   - Translate Hermes events → `ChatPayload`, `TaskPayload`, `MetricPayload`.
   - Translate inbound `ChatPayload` → Hermes command/message format.
2. `cahoot/adapters/openclaw.py` with `OpenClawAdapter(AgentAdapter)`: same shape.
3. Register both in `cahoot/adapters/__init__.py:REGISTRY`.
4. Add integration tests that mock the underlying transport and assert envelope shape.

**Acceptance:**
- Both adapters connect, send a chat, receive a chat, emit at least one task event, and reconnect cleanly when the underlying transport drops.
- An operator running `python -m cahoot --no-ui` with both configured sees envelope traffic in the log within 5 seconds of start.

**DON'T:** invent a new protocol for the agents. Wrap what they already speak.

### Phase 3 — Textual UI shell

**Goal:** the 4-region dashboard from the design doc, rendering real bus traffic.

**Read first:** [Textual app docs](https://textual.textualize.io/guide/app/), [Textual reactivity](https://textual.textualize.io/guide/reactivity/).

**Tasks:**

1. `cahoot/ui/app.py` — `ConnApp(App)` with a fixed 4-region layout (roster | feed | inspector | command box).
2. `cahoot/ui/roster.py` — agent list widget; subscribes to bus, renders status dot + role + heartbeat age ("2s ago").
3. `cahoot/ui/feed.py` — scrollable chat/activity timeline. Filter by room. Backfill from `EventStore.recent`.
4. `cahoot/ui/inspector.py` — selected-agent detail: status, version, last error, task, sparklines.
5. `cahoot/ui/command.py` — single input box at the bottom. Parses `/dm <agent>`, `/all`, `/whoami`, `/where`, plain text (broadcast).
6. Wire `__main__.py`: when `--no-ui` is false, run `await ConnApp(bus, store).run_async()` instead of the headless monitor.

**Visual rules** (don't invent new ones):
- Dark background, green=healthy, amber=degraded, red=blocked, cyan=selected, grey=structure.
- Heartbeat age as human language ("2s ago"), never raw timestamps.
- Sparklines via Textual's built-in `Sparkline` widget, not custom drawing.
- One scrollable feed, one command input — no nested panes.

**Acceptance:**
- Launching `python -m cahoot` opens the dashboard inside the current terminal.
- Detaching tmux and reattaching restores the dashboard exactly.
- Backfilled events appear within 500ms of UI start.

### Phase 4 — command box features

**Goal:** the operator can drive the fleet from the keyboard.

**Tasks:**

1. `/dm <agent_id> <text>` — direct message.
2. `/all <text>` — broadcast.
3. `/whoami`, `/where` — print `runtime.session_context()` into the feed (read-only).
4. `Ctrl+K` opens a command palette (Textual's built-in `CommandPalette`) listing agents and quick actions.
5. Unknown command → echo "unknown command: …" into the feed, never crash.

**Acceptance:**
- All four commands work end-to-end and emit appropriate envelopes.
- `/dm` round-trips through SyntheticAdapter and the reply renders in the feed.

### Phase 5 — Mac launcher

**Goal:** double-clickable Mac launch that opens Terminal into the SSH-attach.

**Read first:** [`docs/OPERATIONS.md`](docs/OPERATIONS.md) §"Mac launcher".

**Tasks:**

1. `scripts/Cahoot.app/Contents/Info.plist` with the right bundle identifier.
2. `scripts/Cahoot.app/Contents/MacOS/run` shell script that runs `osascript` to open Terminal with the SSH + tmux-attach command.
3. README snippet for installing into `/Applications`.

**Acceptance:**
- Dragging the `.app` to `/Applications`, double-clicking, opens Terminal attached to the live session.
- Quitting Terminal does not kill the Cahoot process (tmux owns it).

### Phase 6 — CI

**Tasks:**

1. `.github/workflows/ci.yml`: matrix on Python 3.11 + 3.12, Ubuntu + macOS.
2. Steps: install, `ruff check`, `mypy cahoot`, `pytest --cov`.
3. Upload coverage as a workflow artifact.

**Acceptance:**
- Green on PRs.
- Failing lint, types, or tests block merge.

## 7. Files to read before doing anything substantive

In order of priority when picking up a task:

1. **`CLAUDE.md`** — this file. Always.
2. **`docs/ARCHITECTURE.md`** — the why behind the layout.
3. **`docs/ADAPTERS.md`** — only if writing or modifying an adapter.
4. **`docs/OPERATIONS.md`** — only if touching launcher, tmux, signals, paths.
5. **The module's own docstring** — every file has one explaining its purpose.

## 8. Common pitfalls (learned the hard way)

- **Don't create `asyncio.Queue()` outside an async context expecting it to bind to a specific loop.** It's fine in 3.10+ to construct at import time but be deliberate.
- **`except* Exception as eg:` requires Python 3.11.** That's our floor anyway.
- **`Pydantic v2` discriminated unions need `Field(discriminator="kind")`** and a `Literal["..."]` type on the discriminator field. The pattern is shown in `envelope.py`; copy it.
- **Textual reactive attributes re-render the whole widget by default.** For sparklines/feeds with high update rates, use `Reactive(value, layout=False, repaint=False)` and call `refresh()` explicitly. See https://textual.textualize.io/guide/reactivity/.
- **`tmux kill-session` sends SIGHUP, not SIGTERM.** `runtime.install_signal_handlers` traps both; if you bypass it, you'll leak adapter connections.
- **State directory creation must be idempotent.** `runtime.state_dir()` uses `mkdir(parents=True, exist_ok=True)`. Don't add a check that breaks on the second run.
- **`pip install -e .` then `pytest` from the repo root** — running tests with the wrong CWD will pick up the wrong `cahoot/` package.

## 9. What "done" looks like for a PR

Before opening:

```bash
ruff format .
ruff check .
mypy cahoot
pytest --cov=cahoot
```

All four must be clean. Coverage shouldn't drop below 80% on touched modules. PR description includes: what changed, why, and (if architectural) which invariant in §3 you considered and why this doesn't break it.

## 10. Out of scope (don't build these, even if asked)

- Web dashboard, REST API, GraphQL — Cahoot is a TUI.
- Plugins via dynamic discovery (entry points). Use the explicit `REGISTRY` dict.
- Multi-tenancy / multi-operator concurrency — v2 work.
- Auth, RBAC, audit signing — v2+ work.
- Cross-host bus (Redis/NATS) — only when v1 is genuinely usable daily.
- Custom rich-text editor for the command box — single-line input only.

If a request lands that maps to any of these, push back and reference this section.
