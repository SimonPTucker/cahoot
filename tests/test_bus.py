"""Tests for the in-memory bus and routing rules."""

from __future__ import annotations

import asyncio

import pytest

from cahoot.bus import InMemoryBus
from cahoot.envelope import chat

pytestmark = pytest.mark.asyncio


async def _get(q: asyncio.Queue, deadline: float = 1.0):
    return await asyncio.wait_for(q.get(), timeout=deadline)


async def test_subscriber_receives_targeted_message() -> None:
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    hermes = bus.subscribe("hermes")
    await bus.publish(chat("operator", "hermes", "ping"))
    # Operator sees everything; hermes also gets it.
    assert (await _get(op)).payload.text == "ping"
    assert (await _get(hermes)).payload.text == "ping"


async def test_broadcast_excludes_source() -> None:
    bus = InMemoryBus()
    bus.subscribe("operator")
    a = bus.subscribe("agent-a")
    b = bus.subscribe("agent-b")
    await bus.publish(chat("agent-a", "all", "hello fleet"))
    # b gets it, a (source) does not.
    assert (await _get(b)).payload.text == "hello fleet"
    with pytest.raises(asyncio.TimeoutError):
        await _get(a, deadline=0.1)


async def test_drop_oldest_on_full_subscriber() -> None:
    bus = InMemoryBus()
    op = bus.subscribe("operator", maxsize=2)
    for i in range(4):
        await bus.publish(chat("agent", "operator", str(i)))
    # Queue capped at 2, so we should see the two newest.
    seen = [await _get(op) for _ in range(2)]
    texts = [e.payload.text for e in seen]
    assert texts == ["2", "3"]
    assert bus.dropped >= 2


async def test_dropped_counter_increments() -> None:
    bus = InMemoryBus()
    bus.subscribe("operator", maxsize=1)
    for i in range(5):
        await bus.publish(chat("agent", "operator", str(i)))
    assert bus.dropped == 4


async def test_unsubscribe_stops_delivery() -> None:
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    bus.unsubscribe("operator")
    await bus.publish(chat("agent", "operator", "should not arrive"))
    with pytest.raises(asyncio.TimeoutError):
        await _get(op, deadline=0.1)
    assert "operator" not in bus.subscriber_ids()
