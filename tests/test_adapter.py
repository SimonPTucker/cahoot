"""Tests for the adapter base class lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from cahoot.adapter import AdapterConfig
from cahoot.adapters.synthetic import SyntheticAdapter
from cahoot.bus import InMemoryBus
from cahoot.envelope import AgentState, chat

pytestmark = pytest.mark.asyncio


async def _wait_for_state(op: asyncio.Queue, state: AgentState, budget_s: float = 2.0):
    """Drain operator queue until we see the given status state."""
    deadline = asyncio.get_event_loop().time() + budget_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"never saw state {state}")
        env = await asyncio.wait_for(op.get(), timeout=remaining)
        if env.kind == "status" and env.payload.state is state:
            return env


async def test_adapter_reaches_connected() -> None:
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    adapter = SyntheticAdapter("synth-1", "test", bus, chatter_interval_s=0.05)
    task = asyncio.create_task(adapter.run())
    try:
        await _wait_for_state(op, AgentState.CONNECTED)
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_dm_echoed_via_write_loop() -> None:
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    adapter = SyntheticAdapter("synth-1", "test", bus, chatter_interval_s=10.0)
    task = asyncio.create_task(adapter.run())
    try:
        await _wait_for_state(op, AgentState.CONNECTED)
        await bus.publish(chat("operator", "synth-1", "hello"))
        # Drain until we see the echoed uppercase reply.
        deadline = asyncio.get_event_loop().time() + 2.0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            assert remaining > 0, "no echoed reply"
            env = await asyncio.wait_for(op.get(), timeout=remaining)
            if env.kind == "chat" and getattr(env.payload, "text", "") == "HELLO":
                assert env.source == "synth-1"
                break
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_adapter_reconnects_on_transport_error() -> None:
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    adapter = SyntheticAdapter(
        "synth-flaky",
        "test",
        bus,
        AdapterConfig(reconnect_initial_s=0.01, reconnect_max_s=0.05),
        chatter_interval_s=0.02,
        drop_probability=1.0,  # every tick raises
        seed=1,
    )
    task = asyncio.create_task(adapter.run())
    try:
        # We expect to see CONNECTED twice — once before the drop, once after.
        await _wait_for_state(op, AgentState.CONNECTED)
        await _wait_for_state(op, AgentState.DISCONNECTED)
        await _wait_for_state(op, AgentState.CONNECTED)
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_stop_drives_clean_shutdown_to_offline() -> None:
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    adapter = SyntheticAdapter("synth-1", "test", bus, chatter_interval_s=0.05)
    task = asyncio.create_task(adapter.run())
    await _wait_for_state(op, AgentState.CONNECTED)
    await adapter.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert adapter.state is AgentState.OFFLINE
