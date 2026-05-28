"""Pub/sub bus for the operator and adapters.

The bus is the only path between components. Everyone — adapters, the UI,
the persistence store — depends on the :class:`Bus` Protocol, not on the
concrete :class:`InMemoryBus`. That lets us swap in a SQLite-backed or
distributed implementation later without touching adapter code.

Routing rules (deliberately simple, see ``docs/ARCHITECTURE.md``):

* The operator subscriber (id ``"operator"``) always receives every event.
* ``target == "all"`` is broadcast to every subscriber *except* the source.
* ``target == <agent_id>`` is delivered only to that subscriber (and to
  ``"operator"``).

Backpressure is **drop-oldest**: each subscriber owns a bounded
:class:`asyncio.Queue`; when it fills, the oldest envelope is discarded
and the new one is enqueued. Drops are counted on :attr:`InMemoryBus.dropped`
so an operator can see when the system is under load.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from .envelope import Envelope

__all__ = [
    "OPERATOR",
    "Bus",
    "InMemoryBus",
]

log = logging.getLogger(__name__)

OPERATOR = "operator"
"""Reserved subscriber id for the human operator (the TUI)."""

DEFAULT_QUEUE_MAXSIZE = 1024


@runtime_checkable
class Bus(Protocol):
    """Minimum surface a Cahoot bus must implement.

    Adapters depend on this Protocol, not on :class:`InMemoryBus`.
    """

    async def publish(self, envelope: Envelope) -> None: ...

    def subscribe(
        self,
        subscriber_id: str,
        *,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        wiretap: bool = False,
    ) -> asyncio.Queue[Envelope]: ...

    def unsubscribe(self, subscriber_id: str) -> None: ...


class InMemoryBus:
    """Single-process pub/sub bus backed by :class:`asyncio.Queue` per subscriber.

    Not thread-safe; intended to be driven from one asyncio event loop.
    """

    def __init__(self) -> None:
        self._subs: dict[str, asyncio.Queue[Envelope]] = {}
        self._wiretaps: set[str] = set()
        self.dropped: int = 0
        """Cumulative count of envelopes dropped to relieve backpressure."""

    # -- subscription management ------------------------------------------------

    def subscribe(
        self,
        subscriber_id: str,
        *,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        wiretap: bool = False,
    ) -> asyncio.Queue[Envelope]:
        """Register ``subscriber_id`` and return its inbox queue.

        Re-subscribing with the same id replaces the queue (the old one is
        orphaned). The maxsize is per-subscriber.

        Set ``wiretap=True`` to receive every envelope on the bus regardless
        of target, useful for persistence stores and audit logs. Wiretaps
        do not count as broadcast targets — sending ``target="all"`` still
        excludes them from the recipient set, they just get a copy anyway.
        """
        q: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=maxsize)
        self._subs[subscriber_id] = q
        if wiretap:
            self._wiretaps.add(subscriber_id)
        else:
            self._wiretaps.discard(subscriber_id)
        return q

    def unsubscribe(self, subscriber_id: str) -> None:
        self._subs.pop(subscriber_id, None)
        self._wiretaps.discard(subscriber_id)

    def subscriber_ids(self) -> list[str]:
        return list(self._subs)

    # -- routing -----------------------------------------------------------------

    async def publish(self, envelope: Envelope) -> None:
        """Deliver ``envelope`` to all matching subscribers.

        The operator subscriber always receives a copy. Then:

        * ``target == "all"`` → every subscriber except the source.
        * any other target  → just that subscriber (operator already covered).

        Delivery is non-blocking; a full queue triggers drop-oldest and
        increments :attr:`dropped`.
        """
        recipients: set[str] = set()

        # Operator sees everything (unless they're the source of a self-loop;
        # we still deliver because an echo is useful for the UI).
        if OPERATOR in self._subs:
            recipients.add(OPERATOR)

        if envelope.target == "all":
            for sub in self._subs:
                if sub != envelope.source and sub not in self._wiretaps:
                    recipients.add(sub)
        else:
            if envelope.target in self._subs:
                recipients.add(envelope.target)

        # Wiretaps see every envelope regardless of target.
        recipients.update(self._wiretaps)

        for sub_id in recipients:
            self._deliver(sub_id, envelope)

    def _deliver(self, sub_id: str, envelope: Envelope) -> None:
        q = self._subs.get(sub_id)
        if q is None:
            return
        try:
            q.put_nowait(envelope)
        except asyncio.QueueFull:
            # Drop the oldest item; if that race-loses (consumer already
            # drained it) just skip the drop and try again.
            try:
                q.get_nowait()
                self.dropped += 1
                log.debug(
                    "bus drop-oldest for subscriber %r (total dropped=%d)",
                    sub_id,
                    self.dropped,
                )
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                # Genuinely cannot deliver; count and move on.
                self.dropped += 1
                log.warning("bus failed to deliver to %r even after eviction", sub_id)
