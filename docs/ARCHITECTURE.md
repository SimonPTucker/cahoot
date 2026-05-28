# Architecture

This document explains *why* Cahoot is shaped the way it is. If you're picking up the codebase, read this before reading the modules — it'll save you an afternoon of "why didn't they just …".

## Design goals (in priority order)

1. **Daily-usable mission control over a tmux session.** Every other goal serves this. If a feature makes the daily SSH-and-attach pattern worse, it doesn't ship.
2. **Trust through visibility.** An operator should never have to ask "is it actually running?". State is on screen, errors are envelopes, silence is suspicious and shown as DEGRADED.
3. **Adding an agent is configuration, not engineering.** New adapter → register in `REGISTRY` → add a `[[agents]]` block. Anything more is a design smell.
4. **Small, legible, async Python.** Boring code beats clever code. The whole foundational layer fits in five files.
5. **No premature distribution.** Single process, single loop, single SQLite file. Multi-process resilience is v2 — and only if v1 is actually getting used.

## The mental model

```
                  ┌──────────────────────┐
   operator   ────│   Textual TUI shell  │
   (one human)    └──────────┬───────────┘
                             │  (subscribes as "operator")
                       ┌─────┴──────┐
                       │ Event bus  │
                       └─────┬──────┘
              ┌──────────────┼──────────────┐
              │              │              │
       ┌──────┴─────┐ ┌──────┴────┐ ┌──────┴─────┐
       │  Hermes    │ │ OpenClaw  │ │ Synthetic  │
       │  adapter   │ │ adapter   │ │ adapter    │
       └──────┬─────┘ └──────┬────┘ └────────────┘
              │              │
       (native session) (native session)
```

One operator. One bus. N adapters. Each adapter owns its agent's native protocol and translates both ways. The bus is the *only* place messages between components flow.

## Why these layers and not others

### The envelope

Every message is wrapped in a typed `Envelope` with a discriminated-union payload (`ChatPayload | StatusPayload | HeartbeatPayload | MetricPayload | TaskPayload | ErrorPayload | ReleasePayload`). See [`cahoot/envelope.py`](../cahoot/envelope.py).

**Why discriminated union and not a loose dict?** Loose dicts are how mission-control systems quietly accumulate inconsistencies. By the time you notice that half the heartbeats are missing the `latency_ms` field, you've got six months of bad data and three places that depend on the missing-field shape. Pydantic v2 discriminated unions catch this at parse time, with zero runtime overhead in the hot path ([Pydantic docs](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions)).

**Why frozen?** Envelopes are values that flow on a bus. Mutating one after publish is undefined behaviour — different subscribers see different states depending on race ordering. Frozen makes the invariant enforceable.

**Why `extra="forbid"`?** A typo in a payload field shouldn't silently succeed. Strict mode means a broken adapter fails loudly at parse time, not hours later when the UI fails to render the field.

### The bus

`Bus` is a `Protocol` (see [`cahoot/bus.py`](../cahoot/bus.py)). Adapters and the UI depend on the Protocol, not on `InMemoryBus`. This means:

- v1 ships with `InMemoryBus` (one process, asyncio queue).
- v1.5 will add a SQLite-backed bus that persists and replays.
- v2 will optionally swap in Redis or NATS for cross-process resilience.

Adapter code doesn't change for any of those swaps.

**Routing rules** are deliberately simple:

- The operator always sees everything (the UI subscribes as `"operator"`).
- `target="all"` → broadcast to every agent *except* the source.
- `target=<agent_id>` → deliver to that agent's inbox only.

**Backpressure via drop-oldest, not blocking.** A slow subscriber must not block the bus. In control-plane systems, fresh state beats stale backlog: if the UI falls a second behind, we want it to catch up to *now*, not to grind through the missed second. This is the same principle the LMAX Disruptor talks about for bounded ring buffers (https://lmax-exchange.github.io/disruptor/disruptor.html) — drop-oldest is a degraded mode you instrument and alert on, not something you avoid by unbounded growth.

The bus tracks `dropped` count so an operator can see when the system is under pressure.

### The adapter

`AgentAdapter` (in [`cahoot/adapter.py`](../cahoot/adapter.py)) is the integration point and the most important class in the codebase. Its job is to be the boring, reliable boilerplate around an agent's transport so that adapter authors focus on translation, not on lifecycle.

**State machine:**

```
   OFFLINE ──┐
             ▼
         CONNECTING
             │ (on _open success)
             ▼
         CONNECTED ──────────► DEGRADED
             │ (silence)          │  (heartbeat recovered)
             │                    │
             │ (transport error)  │
             ▼                    │
        DISCONNECTED ◄────────────┘
             │ (backoff with jitter)
             └──► CONNECTING (retry)

   any state ──► OFFLINE (on stop())
```

**Why heartbeats and liveness are separate concerns.** A TCP socket can be open while the agent is hung — connection state lies. So the adapter tracks `_last_inbound_at` (updated only by `_publish_from_agent`) and demotes `CONNECTED → DEGRADED` when silence exceeds `heartbeat_timeout_s`. Recovery happens on the next inbound message.

**Why exponential backoff with full jitter on reconnect.** Without jitter, simultaneous client disconnects (e.g. a network blip across the whole fleet) all retry in lockstep and stampede the server. Full jitter randomises within `[0, computed_backoff]` and is the standard recommendation from AWS Architecture: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/.

**Why `_publish_from_agent` is a separate method from `_publish`.** The base class has internal events (status transitions, its own heartbeats, error reports). Only the *agent*-originated messages should reset the liveness clock. Splitting the methods makes the invariant impossible to break by accident.

**Why not `asyncio.TaskGroup`.** TaskGroup is excellent for structured concurrency where children complete naturally. The adapter's loops are intentionally infinite, so TaskGroup's "wait for all" semantics fight us at shutdown. Plain `create_task` + explicit `cancel` gives deterministic ordering: stop event fires → all three loops cancel → close transport → emit OFFLINE → return.

### The runtime

[`cahoot/runtime.py`](../cahoot/runtime.py) handles the operational concerns that exist *because* Cahoot lives in a tmux session:

- **Signal handling** — `SIGINT`/`SIGTERM`/`SIGHUP` all translate to a clean stop. `SIGWINCH` (resize) is left alone so Textual can handle it.
- **Single-instance lock** — `fcntl.flock` on `~/.local/state/cahoot/cahoot.lock` (`man 2 flock`). Two processes trying to drive the same agents would be chaos.
- **Logging to a rotating file** — stdout is owned by Textual. Logs go to `~/.local/state/cahoot/cahoot.log` (5MB × 3 backups).
- **XDG state paths** — per the [freedesktop base directory spec](https://specifications.freedesktop.org/basedir-spec/latest/). Predictable for SSH-and-grep workflows.
- **Session context probe** — `runtime.session_context()` returns hostname, user, `$TMUX`, `$SSH_CONNECTION`. The `/whoami` and `/where` commands read this.

### Persistence (planned — phase 1)

A single SQLite file at `~/.local/state/cahoot/cahoot.db` with one table:

```sql
CREATE TABLE events (
    id      TEXT PRIMARY KEY,
    ts      TEXT NOT NULL,
    source  TEXT NOT NULL,
    target  TEXT NOT NULL,
    room    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload JSON NOT NULL
);
CREATE INDEX idx_events_ts        ON events(ts);
CREATE INDEX idx_events_room_ts   ON events(room, ts);
CREATE INDEX idx_events_source    ON events(source);
```

**Why SQLite, not Postgres or Redis.** One file, one process, no daemon, zero setup, durable across reboots. WAL mode keeps writes fast and reads non-blocking ([SQLite WAL docs](https://www.sqlite.org/wal.html)). When and if we outgrow it, the `EventStore` interface is what we'll re-implement.

**Why JSON for the payload.** SQLite has decent JSON support (`json_extract`, `json_each`) and the alternative is one table per envelope kind, which loses the unified-feed model.

## Data flow at runtime

A complete trace of one operator message:

1. Operator types `/dm hermes review the release notes` into the Textual command box.
2. UI parses the slash command, builds `chat("operator", "hermes", "review the release notes")`.
3. UI calls `await bus.publish(env)`.
4. Bus routes the envelope:
   - Pushes to the `"operator"` subscriber queue (UI sees its own message echo).
   - Pushes to the `"hermes"` subscriber queue.
5. `HermesAdapter._dispatch_loop` pulls the envelope, calls `_write(env)`.
6. `HermesAdapter._write` translates `env.payload.text` to Hermes' native command format and sends it.
7. Hermes processes the message; its native reply event fires.
8. `HermesAdapter._read_loop` catches it, builds `chat("hermes-main", "operator", "ack: reviewing")`, calls `self._publish_from_agent(reply)`.
9. Bus fans the reply out to the operator queue.
10. UI feed widget renders the reply, indented under the original message via `in_reply_to`.

End-to-end latency target: < 50ms for steps 1–4 and 8–10. Step 5–7 is the agent's own.

## Persistence and replay (phase 1 detail)

When the operator detaches and reattaches tmux, the Textual app is the same process — no state lost. But when the *process* restarts (machine reboot, deliberate restart), we want to:

1. Cold-start with an empty in-memory bus.
2. Backfill the UI feed with the last N events from SQLite (so the operator has context).
3. Resume new event ingestion.

The `EventStore.replay_into(bus)` method on phase 1 will do step 2 by re-publishing recent events to the operator subscriber only (not to agents, since they don't want to re-process old DMs).

## What's deliberately not here

- **No web layer.** Web dashboards rot, need TLS, need accounts, need ports open. A TUI inside tmux is what an operator actually wants for daily use.
- **No plugin discovery via entry points.** Explicit `REGISTRY` in `cahoot/adapters/__init__.py`. You can read the whole list of supported agents at a glance.
- **No async generators returning envelopes from adapters.** They look elegant; they make lifecycle and error handling subtly worse. Use callbacks (`_publish_from_agent`) — same effect, cleaner shutdown.
- **No JSON-RPC, no MCP, no protocol negotiation in the adapter base.** The adapter wraps whatever native protocol the agent already speaks. If you want JSON-RPC, write a `JsonRpcAdapter` subclass.
- **No retry on outbound writes.** If `_write` fails, an `ErrorPayload` is emitted and the envelope is dropped. The operator decides what to do. Retrying without intent is how you get duplicate "publish" commands.

## Trade-offs we accepted

- **Drop-oldest on full subscriber queue means some events disappear under load.** Trade-off accepted; the alternative is unbounded memory growth or a stuck bus. We expose the `dropped` count.
- **No transactional guarantees across the bus and the store.** If we crash between publish-to-bus and store-append, the in-flight event isn't persisted. For mission-control this is acceptable; if you need stronger guarantees, the store needs to subscribe to the bus as a regular subscriber and the UI must replay from store on attach.
- **Single operator, single process.** This is a feature, not a limit. The complexity of multi-operator coordination isn't earned until v2.

## Further reading

- [Pydantic v2 discriminated unions](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions)
- [AWS: Exponential backoff and jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)
- [Textual reactivity](https://textual.textualize.io/guide/reactivity/)
- [SQLite WAL mode](https://www.sqlite.org/wal.html)
- [PEP 654 — Exception Groups](https://peps.python.org/pep-0654/)
- [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/)
- [LMAX Disruptor — bounded queue rationale](https://lmax-exchange.github.io/disruptor/disruptor.html)
