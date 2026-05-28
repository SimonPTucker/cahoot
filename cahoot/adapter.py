"""Adapter base class — the contract every agent integration follows.

An :class:`AgentAdapter` wraps one agent runtime. Subclasses implement four
methods (``_open``, ``_close``, ``_read_loop``, ``_write``); the base class
handles the boring, important stuff:

* connect / reconnect with exponential backoff and full jitter
* heartbeat emission and DEGRADED detection on silence
* clean shutdown (cancellation-safe, transport-closing)
* publishing status / error envelopes onto the bus

See ``docs/ADAPTERS.md`` for the subclass contract, and
``docs/ARCHITECTURE.md`` §"The adapter" for the rationale.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass, field

from .bus import Bus
from .envelope import (
    AgentState,
    Envelope,
    ErrorPayload,
    HeartbeatPayload,
    Severity,
    StatusPayload,
)

__all__ = [
    "AdapterConfig",
    "AgentAdapter",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdapterConfig:
    """Tunable knobs for the adapter lifecycle.

    Defaults are tuned for a moderately chatty agent over a stable transport.
    See ``docs/ADAPTERS.md`` §"Tuning the lifecycle".
    """

    heartbeat_interval_s: float = 5.0
    heartbeat_timeout_s: float = 15.0
    reconnect_initial_s: float = 1.0
    reconnect_max_s: float = 30.0
    reconnect_jitter: float = 0.2
    inbox_maxsize: int = 256
    version: str | None = None
    # Free-form per-adapter metadata, surfaced to the inspector.
    metadata: dict[str, str] = field(default_factory=dict)


class AgentAdapter(ABC):
    """Base class for all adapters.

    Subclasses must implement :meth:`_open`, :meth:`_close`, :meth:`_read_loop`,
    and :meth:`_write`. **They must call :meth:`_publish_from_agent` (not
    ``self.bus.publish``) for every envelope received from the agent** — the
    base class uses the timestamp of that call to drive DEGRADED detection.
    """

    def __init__(
        self,
        agent_id: str,
        role: str,
        bus: Bus,
        config: AdapterConfig | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.role = role
        self.bus = bus
        self.config = config or AdapterConfig()

        self._state: AgentState = AgentState.OFFLINE
        # Inbox is the bus subscription queue for envelopes addressed to us.
        # Subscribing here (rather than at connect time) means messages
        # published while we are CONNECTING / DEGRADED queue up correctly.
        self._inbox: asyncio.Queue[Envelope] = bus.subscribe(
            agent_id, maxsize=self.config.inbox_maxsize
        )
        self._stop = asyncio.Event()
        self._last_inbound_at: float = 0.0

        self._read_task: asyncio.Task[None] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    # ----- properties ----------------------------------------------------------

    @property
    def state(self) -> AgentState:
        return self._state

    # ----- subclass surface ----------------------------------------------------

    @abstractmethod
    async def _open(self) -> None:
        """Open the underlying transport. Raise on failure."""

    @abstractmethod
    async def _close(self) -> None:
        """Tear down the transport. Must be idempotent."""

    @abstractmethod
    async def _read_loop(self) -> None:
        """Consume from the agent. Call :meth:`_publish_from_agent` per message.

        Return cleanly on remote close; raise on transport error.
        """

    @abstractmethod
    async def _write(self, envelope: Envelope) -> None:
        """Translate one outbound envelope to the agent's native format."""

    # ----- public lifecycle ----------------------------------------------------

    async def run(self) -> None:
        """Top-level coroutine: connect, run loops, reconnect on failure, exit on stop."""
        attempt = 0
        try:
            while not self._stop.is_set():
                try:
                    await self._set_state(AgentState.CONNECTING)
                    await self._open()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    attempt += 1
                    await self._publish(
                        Envelope(
                            source=self.agent_id,
                            target="operator",
                            payload=ErrorPayload(
                                severity=Severity.ERROR,
                                message=f"open failed: {exc!r}",
                                context={"attempt": str(attempt)},
                            ),
                        )
                    )
                    with suppress(Exception):
                        await self._close()
                    delay = self._backoff(attempt)
                    log.info(
                        "adapter %s reconnect in %.2fs (attempt %d): %s",
                        self.agent_id,
                        delay,
                        attempt,
                        exc,
                    )
                    if await self._sleep_or_stop(delay):
                        break
                    continue

                # Connected.
                attempt = 0
                self._last_inbound_at = time.monotonic()
                await self._set_state(AgentState.CONNECTED)

                self._read_task = asyncio.create_task(
                    self._wrap_read(), name=f"{self.agent_id}.read"
                )
                self._dispatch_task = asyncio.create_task(
                    self._dispatch_loop(), name=f"{self.agent_id}.dispatch"
                )
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(), name=f"{self.agent_id}.hb"
                )

                # Wait for either the read loop to exit (clean or error) or stop.
                stop_wait = asyncio.create_task(self._stop.wait())
                try:
                    _done, _pending = await asyncio.wait(
                        {self._read_task, stop_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    stop_wait.cancel()
                    with suppress(asyncio.CancelledError):
                        await stop_wait

                # Capture the read task's exit reason BEFORE cancelling aux
                # tasks (which clears the references).
                read_task = self._read_task
                read_exc: BaseException | None = None
                if read_task is not None and read_task.done():
                    with suppress(asyncio.CancelledError):
                        read_exc = read_task.exception()

                # Cancel auxiliaries and close transport.
                await self._cancel_aux()
                with suppress(Exception):
                    await self._close()

                if self._stop.is_set():
                    break

                if read_exc is not None:
                    attempt = 1
                    await self._publish(
                        Envelope(
                            source=self.agent_id,
                            target="operator",
                            payload=ErrorPayload(
                                severity=Severity.WARN,
                                message=f"transport error: {read_exc!r}",
                            ),
                        )
                    )

                await self._set_state(AgentState.DISCONNECTED)
                delay = self._backoff(max(attempt, 1))
                if await self._sleep_or_stop(delay):
                    break
        finally:
            await self._cancel_aux()
            with suppress(Exception):
                await self._close()
            await self._set_state(AgentState.OFFLINE)
            # Release the bus subscription on exit so a fresh run() can
            # re-subscribe cleanly (also avoids leaking queues in tests).
            self.bus.unsubscribe(self.agent_id)

    async def stop(self) -> None:
        """Signal the adapter to exit. Idempotent."""
        self._stop.set()

    # ----- helpers used by subclasses -----------------------------------------

    async def _publish_from_agent(self, envelope: Envelope) -> None:
        """Publish an envelope **originating from the agent**.

        Updates the liveness clock so DEGRADED detection works. Always use
        this from ``_read_loop`` — never call ``self.bus.publish`` directly.
        """
        self._last_inbound_at = time.monotonic()
        if self._state is AgentState.DEGRADED:
            await self._set_state(AgentState.CONNECTED, detail="recovered from silence")
        await self._publish(envelope)

    # ----- internal -----------------------------------------------------------

    async def _publish(self, envelope: Envelope) -> None:
        await self.bus.publish(envelope)

    async def _set_state(self, state: AgentState, *, detail: str | None = None) -> None:
        if self._state is state:
            return
        log.debug(
            "adapter %s state %s -> %s%s",
            self.agent_id,
            self._state.value,
            state.value,
            f" ({detail})" if detail else "",
        )
        self._state = state
        await self._publish(
            Envelope(
                source=self.agent_id,
                target="operator",
                payload=StatusPayload(state=state, detail=detail),
            )
        )

    async def _wrap_read(self) -> None:
        """Run the subclass read loop; surface its exit to ``run``."""
        await self._read_loop()

    async def _dispatch_loop(self) -> None:
        """Pop envelopes from the inbox and hand them to ``_write``."""
        while not self._stop.is_set():
            try:
                env = await asyncio.wait_for(self._inbox.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self._write(env)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "adapter %s write failed: %r (envelope=%s)",
                    self.agent_id,
                    exc,
                    env.id,
                )
                await self._publish(
                    Envelope(
                        source=self.agent_id,
                        target="operator",
                        payload=ErrorPayload(
                            severity=Severity.WARN,
                            message=f"write failed: {exc!r}",
                            context={"envelope_id": env.id},
                        ),
                    )
                )

    async def _heartbeat_loop(self) -> None:
        """Emit periodic heartbeats and demote to DEGRADED on silence."""
        interval = self.config.heartbeat_interval_s
        timeout = self.config.heartbeat_timeout_s
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            silence = time.monotonic() - self._last_inbound_at
            if silence > timeout and self._state is AgentState.CONNECTED:
                await self._set_state(
                    AgentState.DEGRADED,
                    detail=f"no inbound for {silence:.1f}s",
                )
            await self._publish(
                Envelope(
                    source=self.agent_id,
                    target="operator",
                    payload=HeartbeatPayload(),
                )
            )

    async def _cancel_aux(self) -> None:
        for task_name in ("_read_task", "_dispatch_task", "_heartbeat_task"):
            task: asyncio.Task[None] | None = getattr(self, task_name)
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            setattr(self, task_name, None)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter.

        See AWS Architecture Blog:
        https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
        """
        base = min(
            self.config.reconnect_max_s,
            self.config.reconnect_initial_s * (2 ** max(0, attempt - 1)),
        )
        # Full jitter: random within [0, base].
        return random.uniform(0.0, base)

    async def _sleep_or_stop(self, delay: float) -> bool:
        """Sleep for ``delay`` or return ``True`` if stop was signalled."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True
