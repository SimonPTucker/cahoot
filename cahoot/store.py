"""SQLite-backed event store with replay.

Every envelope that flows on the bus is persisted here. On UI start we
replay the last N events into the operator feed so the dashboard has
context after a process restart.

Design notes — see ``docs/ARCHITECTURE.md`` §"Persistence":

* One file: ``$XDG_STATE_HOME/cahoot/cahoot.db`` (default
  ``~/.local/state/cahoot/cahoot.db``).
* WAL mode (``PRAGMA journal_mode=WAL``) — concurrent reads, fast writes,
  durable across restarts. See https://www.sqlite.org/wal.html.
* Single ``events`` table; the payload is stored as a JSON blob so we
  retain the unified-feed model rather than fragmenting by kind.
* ``aiosqlite`` for non-blocking I/O on the event loop.

The store is intentionally **append-only**. We never edit an envelope
after persistence — the bus invariant (envelopes are values) extends to
disk.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from .bus import Bus
from .envelope import Envelope

if TYPE_CHECKING:
    from .runtime import db_path  # noqa: F401 — re-exported via runtime

__all__ = ["EventStore", "open_event_store"]

log = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      TEXT PRIMARY KEY,
    ts      TEXT NOT NULL,
    source  TEXT NOT NULL,
    target  TEXT NOT NULL,
    room    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts        ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_room_ts   ON events(room, ts);
CREATE INDEX IF NOT EXISTS idx_events_source    ON events(source);
"""


class EventStore:
    """Async SQLite event store.

    Construct via :func:`open_event_store` so we apply ``PRAGMA`` settings
    on the live connection once. The store owns one long-lived connection;
    SQLite serialises writes internally, which is the right shape for our
    single-process bus.
    """

    def __init__(self, conn: aiosqlite.Connection, path: Path) -> None:
        self._conn = conn
        self.path = path

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def append(self, env: Envelope) -> None:
        """Persist one envelope. Idempotent on ``id`` (``INSERT OR IGNORE``)."""
        await self._conn.execute(
            "INSERT OR IGNORE INTO events (id, ts, source, target, room, kind, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                env.id,
                env.ts.isoformat(),
                env.source,
                env.target,
                env.room,
                env.kind,
                env.model_dump_json(),
            ),
        )
        await self._conn.commit()

    async def recent(
        self,
        limit: int = 200,
        *,
        room: str | None = None,
    ) -> list[Envelope]:
        """Return up to ``limit`` most-recent events, oldest first."""
        if room is None:
            sql = "SELECT payload FROM events ORDER BY ts DESC LIMIT ?"
            params: tuple[Any, ...] = (limit,)
        else:
            sql = "SELECT payload FROM events WHERE room = ? ORDER BY ts DESC LIMIT ?"
            params = (room, limit)
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        # Reverse so the caller sees oldest-first (UI feeds want chronological).
        return [Envelope.model_validate_json(r[0]) for r in reversed(list(rows))]

    async def by_agent(self, agent_id: str, *, limit: int = 100) -> list[Envelope]:
        """Most-recent events emitted by ``agent_id``, oldest first."""
        async with self._conn.execute(
            "SELECT payload FROM events WHERE source = ? ORDER BY ts DESC LIMIT ?",
            (agent_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [Envelope.model_validate_json(r[0]) for r in reversed(list(rows))]

    async def count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM events") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def journal_mode(self) -> str:
        """Read back the current journal_mode (used in tests / smoke)."""
        async with self._conn.execute("PRAGMA journal_mode;") as cur:
            row = await cur.fetchone()
        return str(row[0]) if row else ""

    # ------------------------------------------------------------------
    # Bus integration
    # ------------------------------------------------------------------

    async def replay_into(
        self,
        bus: Bus,
        *,
        subscriber: str = "operator",
        limit: int = 200,
        room: str | None = None,
    ) -> int:
        """Re-publish recent events to one subscriber for UI backfill.

        Returns the number of envelopes published. We deliver only to the
        named ``subscriber`` (not via :meth:`Bus.publish` broadcast) so
        agents don't re-process old DMs.

        Implementation note: bus subscribers receive copies via
        :meth:`Bus.publish`; we can't target a single subscriber that way
        without breaking the published API. So this helper is best-effort:
        if the bus implementation exposes ``_deliver`` (the in-memory bus
        does), we use it; otherwise we publish normally and accept the
        broader fan-out.
        """
        events = await self.recent(limit=limit, room=room)
        deliver = getattr(bus, "_deliver", None)
        if callable(deliver):
            for env in events:
                deliver(subscriber, env)
        else:
            for env in events:
                await bus.publish(env)
        return len(events)

    async def subscribe_to(self, bus: Bus, *, subscriber_id: str = "_store") -> asyncio.Task[None]:
        """Spawn a background task that drains the bus into the store.

        The store registers itself as a regular bus subscriber so every
        envelope goes through both routing and persistence in lockstep.
        Returns the task so the caller can cancel it on shutdown.
        """
        q = bus.subscribe(subscriber_id, wiretap=True)

        async def _drain() -> None:
            while True:
                env = await q.get()
                try:
                    await self.append(env)
                except Exception:
                    log.exception("store append failed for envelope %s", env.id)

        return asyncio.create_task(_drain(), name="event-store-drain")

    async def close(self) -> None:
        with suppress(Exception):
            await self._conn.close()


async def open_event_store(path: Path | str) -> EventStore:
    """Open or create the SQLite event store at ``path``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(target)
    # WAL gives us concurrent reads while we're appending writes — exactly
    # what the operator-feed-plus-inspector pattern needs.
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.executescript(_SCHEMA)
    await conn.commit()
    log.info("event store opened at %s (wal)", target)
    return EventStore(conn, target)
