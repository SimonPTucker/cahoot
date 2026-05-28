"""``cahoot-join`` — bridge a local agent to a remote Cahoot over WebSocket.

Run on the box where your Hermes / OpenClaw / synthetic agent will live.
Spawns the agent locally (same ACP stdio plumbing Cahoot uses today),
opens a WebSocket to the operator's Cahoot instance, and shuttles
envelopes in both directions.

Typical invocation (paste from ``/invite`` in the Cahoot TUI)::

    cahoot-join \
        --server ws://my-mac-mini.local:9876 \
        --token CH7-XXXX-YYYY \
        --as hermes-main --role planner \
        -- uvx --from 'hermes-agent[acp]==0.14.0' hermes-acp

What it does on the wire:

1. Connects to ``--server``.
2. Sends the ``hello`` frame with the token, claimed agent_id, role,
   and protocol version.
3. On ``welcome`` (rejects on ``rejected``), spawns the agent given on
   the command line via the matching adapter from
   :data:`cahoot.adapters.REGISTRY`. The adapter publishes onto an
   in-process :class:`RemoteBridgeBus` that translates every
   ``publish`` into a ``{"type": "envelope", "data": …}`` frame on
   the WebSocket, and feeds inbound envelope frames into the matching
   subscriber queue.
4. ``admit`` / ``quarantine`` control frames from Cahoot are forwarded
   to the local adapter's ``admit()`` / ``quarantine()`` methods so
   the operator's UI commands still work end-to-end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

from .adapter import AdapterConfig
from .adapters import REGISTRY
from .bus import DEFAULT_QUEUE_MAXSIZE, Bus
from .envelope import Envelope
from .listener import PROTOCOL_VERSION
from .runtime import setup_logging

__all__ = ["RemoteBridgeBus", "main"]

log = logging.getLogger("cahoot-join")


# ---------------------------------------------------------------------------
# Bridge bus — a Bus implementation backed by a websocket
# ---------------------------------------------------------------------------


class RemoteBridgeBus:
    """A :class:`cahoot.bus.Bus` that lives on the agent's box.

    Every ``publish(envelope)`` is serialised and sent over the
    websocket to Cahoot. Subscribers (the local AgentAdapter) get a
    queue that the bridge fills as envelopes arrive from Cahoot.
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._subs: dict[str, asyncio.Queue[Envelope]] = {}
        self._wiretaps: set[str] = set()
        self.dropped: int = 0

    # -- Bus protocol -------------------------------------------------

    def subscribe(
        self,
        subscriber_id: str,
        *,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        wiretap: bool = False,
    ) -> asyncio.Queue[Envelope]:
        q: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=maxsize)
        self._subs[subscriber_id] = q
        if wiretap:
            self._wiretaps.add(subscriber_id)
        return q

    def unsubscribe(self, subscriber_id: str) -> None:
        self._subs.pop(subscriber_id, None)
        self._wiretaps.discard(subscriber_id)

    async def publish(self, envelope: Envelope) -> None:
        """Forward outbound envelope to Cahoot."""
        try:
            frame = {
                "type": "envelope",
                "data": json.loads(envelope.model_dump_json()),
            }
            await self._ws.send(json.dumps(frame))
        except Exception as exc:
            log.warning("bridge: failed to send envelope %s: %r", envelope.id, exc)

    # -- inbound from the remote --------------------------------------

    def deliver(self, envelope: Envelope) -> None:
        """Hand one inbound envelope (already deserialised) to subscribers."""
        recipients: set[str] = set()
        # The bridge only has one "real" subscriber: the agent itself.
        # Match against target like the real bus does.
        if envelope.target in self._subs:
            recipients.add(envelope.target)
        if envelope.target == "all":
            for sub in self._subs:
                if sub != envelope.source and sub not in self._wiretaps:
                    recipients.add(sub)
        recipients.update(self._wiretaps)
        for sub_id in recipients:
            q = self._subs.get(sub_id)
            if q is None:
                continue
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                self.dropped += 1
                with suppress(asyncio.QueueFull):
                    q.put_nowait(envelope)


# Make the protocol attestation explicit even though it's structural.
_ = Bus


# ---------------------------------------------------------------------------
# Bridge loop
# ---------------------------------------------------------------------------


async def _hello(ws: Any, *, token: str, agent_id: str, role: str) -> dict[str, Any]:
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "version": PROTOCOL_VERSION,
                "id": agent_id,
                "role": role,
                "token": token,
                "client_version": __import__("cahoot").__version__,
            }
        )
    )
    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
    parsed = json.loads(raw if isinstance(raw, str) else raw.decode())
    response: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
    if response.get("type") != "welcome" or not response.get("ok"):
        reason = response.get("reason") or "no reason given"
        raise RuntimeError(f"cahoot rejected our hello: {reason}")
    return response


async def _read_loop(ws: Any, bus: RemoteBridgeBus, adapter: Any) -> None:
    """Read frames from Cahoot and route them to the bus / adapter."""
    async for raw in ws:
        try:
            frame = json.loads(raw if isinstance(raw, str) else raw.decode())
        except Exception as exc:
            log.warning("bridge: unparseable inbound frame: %r", exc)
            continue
        kind = frame.get("type")
        if kind == "envelope":
            try:
                env = Envelope.model_validate(frame.get("data") or {})
            except Exception as exc:
                log.warning("bridge: malformed inbound envelope: %r", exc)
                continue
            bus.deliver(env)
        elif kind == "admit":
            admit = getattr(adapter, "admit", None)
            if callable(admit):
                await admit(by=frame.get("by", "operator"))
        elif kind == "quarantine":
            quarantine = getattr(adapter, "quarantine", None)
            if callable(quarantine):
                await quarantine(
                    by=frame.get("by", "operator"),
                    reason=frame.get("reason"),
                )
        elif kind == "ping":
            with suppress(Exception):
                await ws.send(json.dumps({"type": "pong"}))
        elif kind in {"pong", "ready", "rejected"}:
            pass
        elif kind == "bye":
            log.info("bridge: cahoot said bye")
            return
        else:
            log.debug("bridge: unknown frame type %r", kind)


async def _bridge(
    *,
    server: str,
    token: str,
    agent_id: str,
    role: str,
    kind: str,
    agent_argv: list[str],
    cwd: str | None,
) -> int:
    websockets = _require_websockets()
    factory = REGISTRY.get(kind)
    if factory is None:
        raise RuntimeError(f"unknown adapter kind {kind!r}; known: {sorted(REGISTRY)}")

    async with websockets.connect(server) as ws:
        welcome = await _hello(ws, token=token, agent_id=agent_id, role=role)
        log.info(
            "bridge: connected, admitted as %r in room %r",
            welcome.get("agent_id"),
            welcome.get("room"),
        )

        bus = RemoteBridgeBus(ws)
        kwargs: dict[str, Any] = {}
        if agent_argv:
            # For ACP kinds, pass through the launch command + args so
            # the bridge can launch the user-supplied binary instead of
            # the adapter default (lets the operator override uvx
            # version pins etc.).
            kwargs["launch_command"] = agent_argv[0]
            if len(agent_argv) > 1:
                kwargs["launch_args"] = tuple(agent_argv[1:])
        if cwd:
            kwargs["cwd"] = cwd

        adapter = factory(
            agent_id=agent_id,
            role=role,
            bus=bus,
            config=AdapterConfig(),
            **kwargs,
        )

        adapter_task = asyncio.create_task(adapter.run(), name="bridge.adapter")
        read_task = asyncio.create_task(_read_loop(ws, bus, adapter), name="bridge.read")
        done, pending = await asyncio.wait(
            {adapter_task, read_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
            with suppress(BaseException):
                await t
        # Surface the first exception (if any).
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                log.error("bridge: terminating due to %r", exc)
                return 2
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _require_websockets() -> Any:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError(
            "cahoot-join requires the `websockets` package. "
            'Install with: `pip install -e ".[network]"`'
        ) from exc
    return websockets


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cahoot-join",
        description=(
            "Bridge a local agent to a remote Cahoot. Run on the box where "
            "the agent lives; Cahoot lives somewhere else on the LAN."
        ),
    )
    p.add_argument(
        "--server",
        required=True,
        help="Cahoot WebSocket URL, e.g. ws://my-mac-mini.local:9876",
    )
    p.add_argument(
        "--token",
        required=True,
        help="One-shot invite token from /invite in the Cahoot TUI.",
    )
    p.add_argument(
        "--as",
        dest="agent_id",
        required=True,
        help="The agent_id this seat will claim. Must match the /invite.",
    )
    p.add_argument(
        "--role",
        default="agent",
        help="Sticky-note label shown in the roster (default: 'agent').",
    )
    p.add_argument(
        "--kind",
        choices=sorted(REGISTRY),
        default="hermes",
        help="Which Cahoot adapter to drive locally (default: hermes).",
    )
    p.add_argument(
        "--cwd",
        default=None,
        help="Working directory the local agent should run in.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG|INFO|WARNING|ERROR).",
    )
    p.add_argument(
        "agent_argv",
        nargs=argparse.REMAINDER,
        help=(
            "After `--`, the command Cahoot-join should run as the local "
            "agent (e.g. `uvx --from 'hermes-agent[acp]==0.14.0' hermes-acp`). "
            "If omitted, the adapter's default LAUNCH_COMMAND is used."
        ),
    )
    return p


def main() -> int:
    args = _build_argparser().parse_args()
    setup_logging(level=getattr(logging, args.log_level.upper(), logging.INFO))
    agent_argv = list(args.agent_argv or [])
    if agent_argv and agent_argv[0] == "--":
        agent_argv = agent_argv[1:]
    try:
        return asyncio.run(
            _bridge(
                server=args.server,
                token=args.token,
                agent_id=args.agent_id,
                role=args.role,
                kind=args.kind,
                agent_argv=agent_argv,
                cwd=str(Path(args.cwd).expanduser()) if args.cwd else None,
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
