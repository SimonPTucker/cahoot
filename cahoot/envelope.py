"""Typed event envelope for the Cahoot bus.

Everything that flows between adapters and the operator UI is wrapped in an
:class:`Envelope`. The envelope carries provenance (``source``, ``target``,
``room``, ``ts``, ``id``) and exactly one strongly-typed payload from a
discriminated union: chat | status | heartbeat | metric | task | error |
release.

The discriminated union is enforced by Pydantic v2 — adapters cannot
publish a malformed event without it failing at construction time, and
subscribers can rely on ``isinstance(env.payload, ChatPayload)`` /
``match env.payload:`` working.

Envelopes (and every payload) are **frozen** and reject **extra** fields.
See ``docs/ARCHITECTURE.md`` for the rationale.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AgentState",
    "ChatPayload",
    "Envelope",
    "ErrorPayload",
    "HeartbeatPayload",
    "MetricPayload",
    "Payload",
    "ReleasePayload",
    "Severity",
    "StatusPayload",
    "TaskPayload",
    "chat",
    "error",
    "heartbeat",
    "metric",
    "status",
]


class AgentState(StrEnum):
    """Lifecycle states an adapter publishes via :class:`StatusPayload`."""

    OFFLINE = "offline"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


class Severity(StrEnum):
    """Severity for :class:`ErrorPayload`."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"


_FROZEN = ConfigDict(frozen=True, extra="forbid")


class _PayloadBase(BaseModel):
    model_config = _FROZEN


class ChatPayload(_PayloadBase):
    kind: Literal["chat"] = "chat"
    text: str
    # Optional rendering hint for the UI (markdown | plain). Default plain.
    format: Literal["plain", "markdown"] = "plain"


class StatusPayload(_PayloadBase):
    kind: Literal["status"] = "status"
    state: AgentState
    detail: str | None = None


class HeartbeatPayload(_PayloadBase):
    kind: Literal["heartbeat"] = "heartbeat"
    # Optional measured latency for the round-trip to the agent.
    latency_ms: float | None = None


class MetricPayload(_PayloadBase):
    kind: Literal["metric"] = "metric"
    name: str
    value: float
    unit: str | None = None


class TaskPayload(_PayloadBase):
    kind: Literal["task"] = "task"
    task_id: str
    state: Literal["queued", "running", "done", "failed"]
    detail: str | None = None


class ErrorPayload(_PayloadBase):
    kind: Literal["error"] = "error"
    severity: Severity = Severity.ERROR
    message: str
    # Free-form context, e.g. exception class name, retry count, etc.
    context: dict[str, str] = Field(default_factory=dict)


class ReleasePayload(_PayloadBase):
    kind: Literal["release"] = "release"
    component: str
    version: str
    notes: str | None = None


Payload = Annotated[
    Union[  # noqa: UP007 — discriminated unions are clearer with Union[...]
        ChatPayload,
        StatusPayload,
        HeartbeatPayload,
        MetricPayload,
        TaskPayload,
        ErrorPayload,
        ReleasePayload,
    ],
    Field(discriminator="kind"),
]


def _new_id() -> str:
    """Short opaque event id. UUID4 hex without dashes."""
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Envelope(BaseModel):
    """A single event flowing on the bus.

    Identity, provenance, and one typed payload. Immutable once constructed.
    """

    model_config = _FROZEN

    id: str = Field(default_factory=_new_id)
    ts: datetime = Field(default_factory=_now)
    source: str
    target: str = "operator"
    room: str = "ops"
    payload: Payload
    in_reply_to: str | None = None

    @property
    def kind(self) -> str:
        """The discriminator value of the inner payload."""
        return self.payload.kind


# ---------------------------------------------------------------------------
# Convenience factories — keep adapter code terse.
# ---------------------------------------------------------------------------


def chat(
    source: str,
    target: str,
    text: str,
    *,
    room: str = "ops",
    in_reply_to: str | None = None,
    format: Literal["plain", "markdown"] = "plain",
) -> Envelope:
    return Envelope(
        source=source,
        target=target,
        room=room,
        payload=ChatPayload(text=text, format=format),
        in_reply_to=in_reply_to,
    )


def status(
    source: str,
    state: AgentState,
    *,
    detail: str | None = None,
    target: str = "operator",
    room: str = "ops",
) -> Envelope:
    return Envelope(
        source=source,
        target=target,
        room=room,
        payload=StatusPayload(state=state, detail=detail),
    )


def heartbeat(
    source: str,
    *,
    latency_ms: float | None = None,
    target: str = "operator",
    room: str = "ops",
) -> Envelope:
    return Envelope(
        source=source,
        target=target,
        room=room,
        payload=HeartbeatPayload(latency_ms=latency_ms),
    )


def metric(
    source: str,
    name: str,
    value: float,
    *,
    unit: str | None = None,
    target: str = "operator",
    room: str = "ops",
) -> Envelope:
    return Envelope(
        source=source,
        target=target,
        room=room,
        payload=MetricPayload(name=name, value=value, unit=unit),
    )


def error(
    source: str,
    message: str,
    *,
    severity: Severity = Severity.ERROR,
    context: dict[str, str] | None = None,
    target: str = "operator",
    room: str = "ops",
) -> Envelope:
    return Envelope(
        source=source,
        target=target,
        room=room,
        payload=ErrorPayload(
            severity=severity,
            message=message,
            context=context or {},
        ),
    )
