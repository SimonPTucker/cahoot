"""Tests for the SQLite event store."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cahoot.bus import InMemoryBus
from cahoot.envelope import AgentState, chat, status
from cahoot.store import open_event_store

pytestmark = pytest.mark.asyncio


async def test_append_and_recent_roundtrip(tmp_path: Path) -> None:
    store = await open_event_store(tmp_path / "cahoot.db")
    try:
        await store.append(chat("a", "operator", "first"))
        await store.append(chat("a", "operator", "second"))
        out = await store.recent(limit=10)
        texts = [e.payload.text for e in out]  # type: ignore[union-attr]
        assert texts == ["first", "second"]
        assert await store.count() == 2
    finally:
        await store.close()


async def test_recent_filters_by_room(tmp_path: Path) -> None:
    store = await open_event_store(tmp_path / "c.db")
    try:
        await store.append(chat("a", "operator", "ops one", room="ops"))
        await store.append(chat("a", "operator", "scratch one", room="scratch"))
        ops = await store.recent(limit=10, room="ops")
        assert len(ops) == 1
        assert ops[0].room == "ops"
    finally:
        await store.close()


async def test_by_agent_filters(tmp_path: Path) -> None:
    store = await open_event_store(tmp_path / "c.db")
    try:
        await store.append(chat("a", "operator", "from a"))
        await store.append(chat("b", "operator", "from b"))
        await store.append(status("a", AgentState.CONNECTED))
        out = await store.by_agent("a", limit=10)
        assert {e.source for e in out} == {"a"}
        assert len(out) == 2
    finally:
        await store.close()


async def test_wal_mode_enabled(tmp_path: Path) -> None:
    store = await open_event_store(tmp_path / "c.db")
    try:
        mode = await store.journal_mode()
        assert mode.lower() == "wal"
    finally:
        await store.close()


async def test_bus_subscription_drains_into_store(tmp_path: Path) -> None:
    bus = InMemoryBus()
    bus.subscribe("operator")  # so publishes don't get dropped
    store = await open_event_store(tmp_path / "c.db")
    drain = await store.subscribe_to(bus, subscriber_id="_store")
    try:
        for i in range(3):
            await bus.publish(chat("a", "operator", str(i)))
        # Give the drain task a tick to consume the queue.
        for _ in range(50):
            if await store.count() == 3:
                break
            await asyncio.sleep(0.02)
        assert await store.count() == 3
    finally:
        drain.cancel()
        from contextlib import suppress

        with suppress(asyncio.CancelledError):
            await drain
        await store.close()


async def test_replay_into_delivers_to_named_subscriber(tmp_path: Path) -> None:
    store = await open_event_store(tmp_path / "c.db")
    try:
        await store.append(chat("a", "operator", "historical"))
        bus = InMemoryBus()
        op = bus.subscribe("operator")
        n = await store.replay_into(bus, subscriber="operator", limit=10)
        assert n == 1
        env = await asyncio.wait_for(op.get(), timeout=1.0)
        assert env.payload.text == "historical"  # type: ignore[union-attr]
    finally:
        await store.close()
