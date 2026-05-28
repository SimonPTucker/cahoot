"""Synthetic adapter — a self-contained reference implementation.

Why it exists:

* Lets the UI and runtime be exercised without any real agent attached.
* Demonstrates the four-method subclass contract from ``docs/ADAPTERS.md``.
* Provides a deterministic fault injection point (``drop_probability``) so
  the reconnect / backoff machinery in the base class can be tested.

It generates periodic chatter (``chatter_interval_s``), echoes any inbound
DMs back uppercased, and — when ``drop_probability > 0`` — raises from its
read loop on each iteration with that probability to force a reconnect.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from ..adapter import AdapterConfig, AgentAdapter
from ..bus import Bus
from ..envelope import ChatPayload, Envelope, MetricPayload

__all__ = ["SyntheticAdapter"]

log = logging.getLogger(__name__)


class SyntheticAdapter(AgentAdapter):
    """A fake agent that chatters on a timer and echoes DMs.

    Construction:

    ```python
    SyntheticAdapter("synth-1", "test", bus, chatter_interval_s=0.5)
    ```

    Or via config — any keyword in the ``[[agents]]`` block beyond ``id``,
    ``role``, ``kind``, ``version`` lands here.
    """

    def __init__(
        self,
        agent_id: str,
        role: str,
        bus: Bus,
        config: AdapterConfig | None = None,
        *,
        chatter_interval_s: float = 3.0,
        drop_probability: float = 0.0,
        seed: int | None = None,
        **_: Any,
    ) -> None:
        super().__init__(agent_id, role, bus, config)
        self._chatter_interval_s = chatter_interval_s
        self._drop_probability = drop_probability
        self._rng = random.Random(seed)
        self._open_event = asyncio.Event()
        self._tick = 0

    async def _open(self) -> None:
        # No real transport — the "connection" is just the event flag.
        self._open_event.set()

    async def _close(self) -> None:
        self._open_event.clear()

    async def _read_loop(self) -> None:
        """Emit a chat envelope every ``chatter_interval_s`` seconds."""
        while self._open_event.is_set():
            await asyncio.sleep(self._chatter_interval_s)
            if self._drop_probability > 0 and self._rng.random() < self._drop_probability:
                raise ConnectionResetError("synthetic: injected drop")
            self._tick += 1
            await self._publish_from_agent(
                Envelope(
                    source=self.agent_id,
                    target="operator",
                    payload=ChatPayload(text=f"tick {self._tick}"),
                )
            )
            # Sprinkle a metric every few ticks so the inspector has data.
            if self._tick % 5 == 0:
                await self._publish_from_agent(
                    Envelope(
                        source=self.agent_id,
                        target="operator",
                        payload=MetricPayload(
                            name="ticks",
                            value=float(self._tick),
                            unit="count",
                        ),
                    )
                )

    async def _write(self, envelope: Envelope) -> None:
        """Echo inbound chat back, uppercased."""
        text = getattr(envelope.payload, "text", None)
        if not isinstance(text, str):
            return
        await self._publish_from_agent(
            Envelope(
                source=self.agent_id,
                target=envelope.source,
                payload=ChatPayload(text=text.upper()),
                in_reply_to=envelope.id,
            )
        )
