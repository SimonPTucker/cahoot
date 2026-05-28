"""4-region operator dashboard.

Layout::

    ┌──────────┬───────────────────────────────────────┬──────────────┐
    │ roster   │            feed                       │  inspector   │
    │          │                                       │              │
    │          │                                       │              │
    ├──────────┴───────────────────────────────────────┴──────────────┤
    │ /command                                                       │
    └────────────────────────────────────────────────────────────────┘

ConnApp owns the bus subscription, the adapter dict, and the optional
event store. It dispatches envelopes to each widget and routes command
submissions through :mod:`cahoot.commands`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical

from .. import commands as cmds
from ..bus import Bus
from ..envelope import Envelope
from .command import CommandBox, CommandSubmitted
from .feed import FeedWidget
from .inspector import AgentInfo, InspectorWidget
from .roster import RosterWidget

if TYPE_CHECKING:
    from ..adapter import AgentAdapter
    from ..store import EventStore

__all__ = ["ConnApp"]

log = logging.getLogger(__name__)


class ConnApp(App[int]):
    """Operator dashboard, exits with rc=0 on /quit."""

    CSS = """
    Screen {
        layout: vertical;
        background: #0c0f13;
    }
    #top {
        height: 1fr;
        layout: horizontal;
    }
    RosterWidget {
        width: 36;
        border: round #2a3140;
        padding: 0 1;
        color: #d6deeb;
    }
    FeedWidget {
        width: 1fr;
        border: round #2a3140;
        padding: 0 1;
    }
    InspectorWidget {
        width: 42;
        border: round #2a3140;
        padding: 0 1;
        color: #d6deeb;
    }
    CommandBox {
        height: 3;
        border: round #2a3140;
        background: #14181f;
    }
    """

    BINDINGS: ClassVar[list[Any]] = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_feed", "Clear feed"),
    ]

    def __init__(
        self,
        bus: Bus,
        adapters: dict[str, AgentAdapter],
        *,
        store: EventStore | None = None,
        room: str = "ops",
        stop_event: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self._bus = bus
        self._adapters = adapters
        self._store = store
        self._room = room
        self._stop = stop_event or asyncio.Event()
        self._roster = RosterWidget()
        self._feed = FeedWidget()
        self._inspector = InspectorWidget()
        self._command = CommandBox()
        self._infos: dict[str, AgentInfo] = {}
        self._consumer_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Composition + lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="top"):
                yield self._roster
                yield self._feed
                yield self._inspector
            yield self._command

    async def on_mount(self) -> None:
        # Seed AgentInfos from the adapters that were spawned at startup,
        # so the inspector has rows to flip between before any traffic.
        for aid, adapter in self._adapters.items():
            self._infos[aid] = AgentInfo(agent_id=aid, role=adapter.role)
        if self._adapters:
            first = next(iter(self._adapters))
            self._inspector.focus_on(self._infos[first])

        # Backfill from store (most-recent envelopes go through the same
        # ingest path as live traffic).
        if self._store is not None:
            try:
                history = await self._store.recent(limit=200, room=self._room)
                for env in history:
                    self._dispatch(env)
            except Exception:
                log.exception("backfill failed; continuing with empty feed")

        # Start the operator queue consumer.
        op_queue = self._bus.subscribe("operator")
        self._consumer_task = asyncio.create_task(self._consume(op_queue), name="ui-consumer")

    async def on_unmount(self) -> None:
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()

    # ------------------------------------------------------------------
    # Bus consumer
    # ------------------------------------------------------------------

    async def _consume(self, q: asyncio.Queue[Envelope]) -> None:
        while True:
            env = await q.get()
            self._dispatch(env)

    def _dispatch(self, env: Envelope) -> None:
        # Track per-agent info for the inspector.
        if env.source not in {"operator", "_store", "system"}:
            info = self._infos.setdefault(env.source, AgentInfo(agent_id=env.source))
            info.ingest(env)
        # Widget fan-out.
        self._roster.ingest(env)
        self._feed.ingest(env)
        self._inspector.ingest(env)

    # ------------------------------------------------------------------
    # Command box
    # ------------------------------------------------------------------

    async def on_command_submitted(self, event: CommandSubmitted) -> None:
        parsed = cmds.parse(event.text)
        result = await cmds.execute(
            parsed,
            bus=self._bus,
            adapters=self._adapters,
            room=self._room,
        )
        # Surface the feedback into the feed as an operator chat so it
        # persists alongside agent traffic.
        await cmds.announce(self._bus, result, room=self._room)
        if result.quit_requested:
            self._stop.set()
            self.exit(0)

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def action_clear_feed(self) -> None:
        self._feed.clear()
