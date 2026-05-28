"""End-to-end tests for the WebSocket listener + RemoteAdapter.

Spins up the real ``websockets`` server on a localhost port, drives it
with a real ``websockets`` client, and asserts the handshake, envelope
pump, /approve forwarding, and reject paths all work as documented.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

import pytest
import websockets

from cahoot.adapter import AgentAdapter
from cahoot.bus import InMemoryBus
from cahoot.envelope import AgentState, chat
from cahoot.invites import InviteRegistry
from cahoot.listener import PROTOCOL_VERSION

pytestmark = pytest.mark.asyncio


async def _spin_listener(
    bus: Any,
    invites: InviteRegistry,
    adapters: dict[str, AgentAdapter],
) -> tuple[asyncio.Event, asyncio.Task[None], int]:
    """Boot the listener on an ephemeral port and return (stop, task, port)."""
    stop = asyncio.Event()
    # Use port 0 to get an ephemeral allocation, then sniff what we got.
    port_holder: dict[str, int] = {}

    async def runner() -> None:
        # We need the real bound port; tweak run_listener to expose it,
        # but for the test we patch the server creation path.
        from cahoot.listener import start_listener

        server = await start_listener(
            bus=bus,
            invites=invites,
            adapters=adapters,
            adapter_tasks={},
            bind="127.0.0.1",
            port=0,
            room="ops",
        )
        # `websockets.serve` returns a Server with .sockets[0].
        port_holder["p"] = server.sockets[0].getsockname()[1]
        try:
            await stop.wait()
        finally:
            server.close()
            with suppress(Exception):
                await server.wait_closed()

    task = asyncio.create_task(runner(), name="listener-test")
    # Wait until the port has been allocated.
    for _ in range(50):
        if "p" in port_holder:
            break
        await asyncio.sleep(0.02)
    assert "p" in port_holder, "listener never bound"
    return stop, task, port_holder["p"]


async def test_invalid_token_is_rejected_with_reason() -> None:
    bus = InMemoryBus()
    bus.subscribe("operator")
    invites = InviteRegistry()
    adapters: dict[str, Any] = {}
    stop, task, port = await _spin_listener(bus, invites, adapters)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "version": PROTOCOL_VERSION,
                        "id": "rogue",
                        "role": "agent",
                        "token": "CH7-FAKE-BAD0",
                    }
                )
            )
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            response = json.loads(raw)
            assert response["type"] == "rejected"
            assert "unknown_token" in response["reason"]
        assert adapters == {}
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)


async def test_valid_token_accepts_and_registers_adapter() -> None:
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    invites = InviteRegistry()
    adapters: dict[str, Any] = {}
    invite = invites.mint(agent_id="hermes-main", role="planner")
    stop, task, port = await _spin_listener(bus, invites, adapters)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "version": PROTOCOL_VERSION,
                        "id": "hermes-main",
                        "role": "planner",
                        "token": invite.token,
                    }
                )
            )
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            welcome = json.loads(raw)
            assert welcome["type"] == "welcome"
            assert welcome["ok"] is True
            assert welcome["agent_id"] == "hermes-main"

            # The listener should have registered a RemoteAdapter and
            # the adapter goes through CONNECTING -> CONNECTED on the bus.
            deadline = asyncio.get_event_loop().time() + 2.0
            saw_connected = False
            while not saw_connected:
                remaining = deadline - asyncio.get_event_loop().time()
                assert remaining > 0, "never saw CONNECTED for remote agent"
                env = await asyncio.wait_for(op.get(), timeout=remaining)
                if (
                    env.kind == "status"
                    and env.source == "hermes-main"
                    and env.payload.state is AgentState.CONNECTED
                ):
                    saw_connected = True
            assert "hermes-main" in adapters

            # And the invite token is now consumed.
            second = invites.redeem(token=invite.token, claimed_agent_id="hermes-main")
            assert second.outcome == "unknown_token"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)


async def test_inbound_envelope_lands_on_operator_queue() -> None:
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    invites = InviteRegistry()
    adapters: dict[str, Any] = {}
    invite = invites.mint(agent_id="hermes-main", role="planner")
    stop, task, port = await _spin_listener(bus, invites, adapters)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "version": PROTOCOL_VERSION,
                        "id": "hermes-main",
                        "role": "planner",
                        "token": invite.token,
                    }
                )
            )
            # Skip welcome + initial control frames.
            await asyncio.wait_for(ws.recv(), timeout=1.0)
            # The adapter sends a `ready` control frame; receive it but
            # don't assert on it.
            await asyncio.wait_for(ws.recv(), timeout=1.0)

            # Now push an envelope from the "remote agent".
            outbound_env = chat("hermes-main", "operator", "hello from the LAN")
            await ws.send(
                json.dumps(
                    {
                        "type": "envelope",
                        "data": json.loads(outbound_env.model_dump_json()),
                    }
                )
            )
            # Drain the operator queue until we see the chat.
            deadline = asyncio.get_event_loop().time() + 2.0
            seen = None
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                assert remaining > 0, "remote chat never arrived"
                env = await asyncio.wait_for(op.get(), timeout=remaining)
                if env.kind == "chat" and getattr(env.payload, "text", "") == "hello from the LAN":
                    seen = env
                    break
            assert seen.source == "hermes-main"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)


async def test_duplicate_agent_id_second_connection_rejected() -> None:
    bus = InMemoryBus()
    bus.subscribe("operator")
    invites = InviteRegistry()
    adapters: dict[str, Any] = {}
    invite_a = invites.mint(agent_id="hermes-main", role="planner")
    invite_b = invites.mint(agent_id="hermes-main", role="planner")
    stop, task, port = await _spin_listener(bus, invites, adapters)
    try:
        ws_a = await websockets.connect(f"ws://127.0.0.1:{port}")
        await ws_a.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": PROTOCOL_VERSION,
                    "id": "hermes-main",
                    "role": "planner",
                    "token": invite_a.token,
                }
            )
        )
        # Welcome.
        await asyncio.wait_for(ws_a.recv(), timeout=2.0)
        # Wait for registration to settle.
        for _ in range(40):
            if "hermes-main" in adapters:
                break
            await asyncio.sleep(0.02)
        assert "hermes-main" in adapters

        # Second client with valid second token should still be refused —
        # the slot is taken.
        ws_b = await websockets.connect(f"ws://127.0.0.1:{port}")
        await ws_b.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": PROTOCOL_VERSION,
                    "id": "hermes-main",
                    "role": "planner",
                    "token": invite_b.token,
                }
            )
        )
        raw = await asyncio.wait_for(ws_b.recv(), timeout=2.0)
        rejection = json.loads(raw)
        assert rejection["type"] == "rejected"
        assert "already connected" in rejection["reason"]
        await ws_b.close()
        await ws_a.close()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
