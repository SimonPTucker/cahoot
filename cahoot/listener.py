"""WebSocket listener that accepts inbound ``cahoot-join`` connections.

Bound by default to ``0.0.0.0:9876`` (LAN-wide). One :class:`RemoteAdapter`
is spawned per accepted connection, after the invite token is validated.

Wire protocol (see :mod:`cahoot.adapters.remote`):

1. Client opens a WebSocket to ``ws://<host>:<port>``.
2. Client sends ``{"type": "hello", "version": 1, "id": "...",
   "role": "...", "token": "CH7-XXXX-YYYY"}`` as the very first frame.
3. Server validates the token against the :class:`InviteRegistry`. On
   success it replies ``{"type": "welcome", "ok": true, "agent_id":
   "..."}`` and the connection becomes a :class:`RemoteAdapter`. On
   failure it replies ``{"type": "rejected", "reason": "..."}`` and
   closes.

The listener has **no TLS** in v1; the trust boundary is "your LAN". v1.5
will add ``wss://`` + a self-signed cert and a `--tls` flag.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from .adapter import AdapterConfig
from .adapters.remote import RemoteAdapter
from .bus import Bus
from .invites import InviteRegistry

if TYPE_CHECKING:
    pass

__all__ = [
    "DEFAULT_BIND",
    "DEFAULT_PORT",
    "PROTOCOL_VERSION",
    "ListenerError",
    "run_listener",
    "start_listener",
]

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 9876


class ListenerError(RuntimeError):
    """Raised when the optional ``websockets`` dependency isn't installed."""


def _require_websockets() -> Any:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover — exercised by users without [network]
        raise ListenerError(
            "the `websockets` package is required for the network listener. "
            'Install with: `pip install -e ".[network]"`'
        ) from exc
    return websockets


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


async def _handle_connection(
    websocket: Any,
    *,
    bus: Bus,
    invites: InviteRegistry,
    adapters: dict[str, Any],
    adapter_tasks: dict[str, asyncio.Task[None]],
    room: str,
) -> None:
    """One inbound connection. Hello → validate → spawn adapter → run."""
    peer = getattr(websocket, "remote_address", ("?", 0))
    peer_str = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
    log.info("listener: accepted ws connection from %s", peer_str)

    # 1) Read the hello frame with a short deadline.
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
    except (TimeoutError, Exception) as exc:
        log.warning("listener: hello not received from %s: %r", peer_str, exc)
        with suppress(Exception):
            await websocket.close(code=1008, reason="no hello")
        return

    try:
        hello = json.loads(raw if isinstance(raw, str) else raw.decode())
    except Exception as exc:
        await _reject(websocket, f"unparseable hello: {exc!r}")
        return

    if hello.get("type") != "hello":
        await _reject(websocket, f"first frame must be 'hello', got {hello.get('type')!r}")
        return

    version = hello.get("version")
    if version != PROTOCOL_VERSION:
        await _reject(
            websocket,
            f"protocol version mismatch (cahoot speaks v{PROTOCOL_VERSION}, "
            f"client sent v{version!r})",
        )
        return

    token = str(hello.get("token", "")).strip()
    agent_id = str(hello.get("id", "")).strip()
    role = str(hello.get("role", "")).strip() or "agent"

    if not token or not agent_id:
        await _reject(websocket, "hello missing required field 'token' or 'id'")
        return

    # 2) Validate against the invite registry.
    result = invites.redeem(token=token, claimed_agent_id=agent_id)
    if result.outcome != "ok":
        await _reject(
            websocket,
            f"invite rejected ({result.outcome}): {result.reason}",
        )
        log.warning(
            "listener: rejecting %s for %s — %s (%s)",
            peer_str,
            agent_id,
            result.outcome,
            result.reason,
        )
        return

    # 3) Refuse duplicate connections for the same agent_id.
    if agent_id in adapters:
        await _reject(
            websocket,
            f"agent_id {agent_id!r} already connected; quarantine or disconnect "
            f"the existing session first",
        )
        return

    # 4) Welcome the bridge, hand control to a RemoteAdapter.
    await websocket.send(
        json.dumps(
            {
                "type": "welcome",
                "ok": True,
                "agent_id": agent_id,
                "room": room,
            }
        )
    )
    log.info(
        "listener: %s admitted as %r (role %r) from %s",
        token[:12],
        agent_id,
        role,
        peer_str,
    )

    adapter = RemoteAdapter(
        agent_id=agent_id,
        role=role,
        bus=bus,
        config=AdapterConfig(version=hello.get("client_version")),
        websocket=websocket,
        remote_address=peer_str,
        room=room,
    )
    adapters[agent_id] = adapter
    task = asyncio.create_task(adapter.run(), name=f"remote.{agent_id}")
    adapter_tasks[agent_id] = task
    try:
        await task
    finally:
        adapters.pop(agent_id, None)
        adapter_tasks.pop(agent_id, None)
        log.info("listener: %r disconnected", agent_id)


async def _reject(websocket: Any, reason: str) -> None:
    with suppress(Exception):
        await websocket.send(json.dumps({"type": "rejected", "reason": reason}))
    with suppress(Exception):
        await websocket.close(code=1008, reason=reason[:120])


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def start_listener(
    *,
    bus: Bus,
    invites: InviteRegistry,
    adapters: dict[str, Any],
    adapter_tasks: dict[str, asyncio.Task[None]],
    bind: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    room: str = "ops",
) -> Any:
    """Bind a WebSocket listener; returns the server handle."""
    websockets = _require_websockets()

    async def handler(ws: Any) -> None:
        try:
            await _handle_connection(
                ws,
                bus=bus,
                invites=invites,
                adapters=adapters,
                adapter_tasks=adapter_tasks,
                room=room,
            )
        except Exception:
            log.exception("listener: unhandled error in connection handler")

    server = await websockets.serve(handler, bind, port)
    log.info("listener: ws server bound to %s:%d", bind, port)
    return server


async def run_listener(
    *,
    bus: Bus,
    invites: InviteRegistry,
    adapters: dict[str, Any],
    adapter_tasks: dict[str, asyncio.Task[None]],
    stop: asyncio.Event,
    bind: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    room: str = "ops",
    advertise: bool = True,
) -> None:
    """Convenience: run the listener until ``stop`` is set.

    If ``advertise`` is True (default), also announce the service over
    mDNS / Bonjour as ``_cahoot._tcp.local.`` so ``cahoot-join`` can
    auto-discover this instance.
    """
    server = await start_listener(
        bus=bus,
        invites=invites,
        adapters=adapters,
        adapter_tasks=adapter_tasks,
        bind=bind,
        port=port,
        room=room,
    )

    advertiser_cm: Any = None
    if advertise:
        try:
            from .discovery import advertise as _adv

            advertiser_cm = _adv(port=port, room=room)
            await advertiser_cm.__aenter__()
        except Exception as exc:
            log.warning(
                "listener: mDNS advertise unavailable, continuing without it: %r",
                exc,
            )
            advertiser_cm = None

    try:
        await stop.wait()
    finally:
        if advertiser_cm is not None:
            with suppress(Exception):
                await advertiser_cm.__aexit__(None, None, None)
        server.close()
        with suppress(Exception):
            await server.wait_closed()
        log.info("listener: ws server stopped")
