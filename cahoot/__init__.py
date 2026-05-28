"""Cahoot — mission control for agent fleets."""

from __future__ import annotations

__version__ = "0.1.0"

from .adapter import AdapterConfig, AgentAdapter
from .bus import Bus, InMemoryBus
from .envelope import (
    AgentState,
    ChatPayload,
    Envelope,
    ErrorPayload,
    HeartbeatPayload,
    MetricPayload,
    ReleasePayload,
    Severity,
    StatusPayload,
    TaskPayload,
    chat,
    error,
    heartbeat,
    metric,
    status,
)

__all__ = [
    "AdapterConfig",
    "AgentAdapter",
    "AgentState",
    "Bus",
    "ChatPayload",
    "Envelope",
    "ErrorPayload",
    "HeartbeatPayload",
    "InMemoryBus",
    "MetricPayload",
    "ReleasePayload",
    "Severity",
    "StatusPayload",
    "TaskPayload",
    "__version__",
    "chat",
    "error",
    "heartbeat",
    "metric",
    "status",
]
