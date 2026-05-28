"""Adapter for an inbound agent that connected over WebSocket.

When a remote ``cahoot-join`` bridge connects to the listener, presents a
valid invite token, and successfully completes the hello/welcome
handshake, the listener constructs a :class:`RemoteAdapter` and runs it
exactly like any other adapter. The roster, feed, inspector, and slash
commands all see the agent identically.

Wire protocol (newline-delimited JSON over the WebSocket):

* ``hello`` (bridge → Cahoot, before adapter is constructed; see
  ``cahoot/listener.py``): ``{"type": "hello", "version": 1, "id": …,
  "role": …, "token": …}``
* ``welcome`` (Cahoot → bridge, before adapter): ``{"type": "welcome",
  "ok": true, "agent_id": …}``
* ``envelope`` (bidirectional): ``{"type": "envelope", "data": <Envelope
  JSON>}``
* ``admit`` / ``quarantine`` (Cahoot → bridge): control frames the
  remote bridge applies to its local ACPAdapter so /approve and /deny
  from the UI keep working end-to-end.
* ``ping`` / ``pong`` (bidirectional, optional): liveness probe.

This module is import-safe without the ``websockets`` extra; it only
imports it inside the constructor signature via :class:`typing.Any`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Any, ClassVar, Literal

from ..adapter import AdapterConfig, AgentAdapter
from ..bus import Bus
from ..envelope import (
    AgentState,
    Envelope,
    ErrorPayload,
    Severity,
)
from ..onboarding import EnrollmentState

__all__ = ["RemoteAdapter"]

log = logging.getLogger(__name__)


class RemoteAdapter(AgentAdapter):
    """Wraps a WebSocket connection from a ``cahoot-join`` bridge.

    Unlike :class:`ACPAdapter`, this adapter does **not** spawn a
    subprocess — the connection is already established by the time the
    listener hands it over. ``_open`` is therefore a no-op confirmation;
    ``_read_loop`` drives the bidirectional pump.

    The :class:`AgentAdapter` heartbeat / DEGRADED machinery still works:
    every inbound envelope updates ``_last_inbound_at`` via
    :meth:`_publish_from_agent`, so a silent bridge gets demoted as
    expected.
    """

    LAUNCH_COMMAND: ClassVar[str] = ""

    def __init__(
        self,
        agent_id: str,
        role: str,
        bus: Bus,
        config: AdapterConfig | None = None,
        *,
        websocket: Any,
        remote_address: str = "",
        room: str = "ops",
        **_: Any,
    ) -> None:
        super().__init__(agent_id, role, bus, config)
        self._ws = websocket
        self._remote_address = remote_address
        self._room = room
        # Enrollment state for /approve / /deny semantics. Mirrors the
        # ACPAdapter behaviour so the UI surface is uniform across kinds.
        self._enrollment: EnrollmentState = EnrollmentState.ADMITTED
        self._open_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public surface for /approve and /deny — same names as ACPAdapter
    # so cahoot.commands.execute can dispatch without knowing the kind.
    # ------------------------------------------------------------------

    @property
    def enrollment(self) -> EnrollmentState:
        return self._enrollment

    async def admit(self, *, by: str = "operator") -> bool:
        if self._enrollment is EnrollmentState.ADMITTED:
            return False
        self._enrollment = EnrollmentState.ADMITTED
        await self._send_control("admit", {"by": by})
        log.info("remote adapter %s admitted by %s", self.agent_id, by)
        return True

    async def quarantine(self, *, by: str = "operator", reason: str | None = None) -> bool:
        if self._enrollment is EnrollmentState.QUARANTINED:
            return False
        self._enrollment = EnrollmentState.QUARANTINED
        await self._send_control("quarantine", {"by": by, "reason": reason})
        log.info(
            "remote adapter %s quarantined by %s: %s",
            self.agent_id,
            by,
            reason,
        )
        return True

    # ------------------------------------------------------------------
    # AgentAdapter contract
    # ------------------------------------------------------------------

    async def _open(self) -> None:
        # The websocket is already connected and the hello/welcome
        # already happened in the listener. Nothing to spin up here, but
        # tell the bridge we're alive at the adapter layer so the bridge
        # can mirror our state if needed.
        self._open_event.set()
        await self._send_control(
            "ready",
            {"agent_id": self.agent_id, "enrollment": str(self._enrollment)},
        )
        # Bump the liveness clock so heartbeat isn't immediately DEGRADED.
        self._last_inbound_at = time.monotonic()

    async def _close(self) -> None:
        self._open_event.clear()
        ws = self._ws
        self._ws = None
        if ws is not None:
            with suppress(Exception):
                await ws.close()

    async def _read_loop(self) -> None:
        """Pump envelopes from the WebSocket onto the bus.

        Inbound bridges are **one-shot**: the invite token was single-use,
        so once the bridge disconnects there's nothing to reconnect to.
        We flip ``self._stop`` so the base class's reconnect loop exits
        cleanly instead of busy-looping on a dead socket.
        """
        if self._ws is None:
            raise ConnectionResetError("remote adapter has no websocket")
        try:
            async for raw in self._ws:
                await self._handle_frame(raw)
        except ConnectionResetError:
            self._stop.set()
            raise
        except Exception as exc:
            self._stop.set()
            raise ConnectionResetError(
                f"remote adapter {self.agent_id} ws read failed: {exc!r}"
            ) from exc
        # Clean WS close → bridge said bye. Same outcome: don't reconnect.
        self._stop.set()

    async def _write(self, envelope: Envelope) -> None:
        """Forward one outbound envelope to the remote bridge."""
        if self._ws is None:
            raise RuntimeError("remote adapter not connected")
        # Quarantined remotes only receive operator messages.
        if self._enrollment is not EnrollmentState.ADMITTED and envelope.source != "operator":
            log.info(
                "remote adapter %s (quarantined) dropping inbound from %s",
                self.agent_id,
                envelope.source,
            )
            return
        frame = {
            "type": "envelope",
            "data": json.loads(envelope.model_dump_json()),
        }
        await self._ws.send(json.dumps(frame))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _handle_frame(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw if isinstance(raw, str) else raw.decode())
        except Exception as exc:
            log.warning(
                "remote adapter %s: dropping unparseable frame: %r",
                self.agent_id,
                exc,
            )
            return
        kind = frame.get("type")
        if kind == "envelope":
            await self._handle_envelope_frame(frame.get("data") or {})
        elif kind == "ping":
            with suppress(Exception):
                if self._ws is not None:
                    await self._ws.send(json.dumps({"type": "pong"}))
        elif kind == "pong":
            self._last_inbound_at = time.monotonic()
        elif kind == "bye":
            raise ConnectionResetError(f"remote bridge for {self.agent_id} said bye")
        else:
            log.debug(
                "remote adapter %s: unknown frame type %r",
                self.agent_id,
                kind,
            )

    async def _handle_envelope_frame(self, data: dict[str, Any]) -> None:
        try:
            env = Envelope.model_validate(data)
        except Exception as exc:
            log.warning(
                "remote adapter %s: malformed envelope dropped: %r",
                self.agent_id,
                exc,
            )
            await self._publish(
                Envelope(
                    source=self.agent_id,
                    target="operator",
                    payload=ErrorPayload(
                        severity=Severity.WARN,
                        message=f"remote envelope rejected: {exc!r}",
                    ),
                )
            )
            return
        # The bridge MAY rewrite the source so it always reflects the
        # bound agent_id; clamp it server-side to prevent spoofing.
        if env.source != self.agent_id:
            env = env.model_copy(update={"source": self.agent_id})
        await self._publish_from_agent(env)

    async def _send_control(
        self,
        kind: Literal["admit", "quarantine", "ready"],
        extras: dict[str, Any] | None = None,
    ) -> None:
        if self._ws is None:
            return
        frame: dict[str, Any] = {"type": kind}
        if extras:
            frame.update(extras)
        with suppress(Exception):
            await self._ws.send(json.dumps(frame))

    # Convenience for the listener — once /quit fires we want the
    # adapter to surface DISCONNECTED then exit cleanly.
    async def stop_with_state(self, state: AgentState = AgentState.OFFLINE) -> None:
        await self._set_state(state)
        await self.stop()
