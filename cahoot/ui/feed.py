"""Scrollable chat/activity timeline.

Every envelope routed to the operator becomes one or more lines in the
feed. The widget formats per kind:

* ``chat`` — ``timestamp source → target: text``.
* ``status`` — dim, with the state colour.
* ``error`` — red, with severity prefix.
* ``task`` — yellow, with state and detail.
* ``metric`` — dim.
* ``heartbeat`` — suppressed (too noisy for the feed; the roster shows
  liveness already).

The widget is a thin wrapper around :class:`textual.widgets.RichLog`,
which already handles scrollback and overflow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.text import Text
from textual.widgets import RichLog

from ..envelope import Envelope, Severity

__all__ = ["FeedWidget"]


class FeedWidget(RichLog):
    """Center column. Append envelopes; autoscroll keeps the newest visible."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(highlight=False, markup=False, wrap=True, **kw)
        self.can_focus = False  # let command box keep focus by default

    def on_mount(self) -> None:
        self.border_title = "feed"

    def ingest(self, env: Envelope) -> None:
        """Render one envelope into the log if it's user-visible."""
        if env.kind == "heartbeat":
            return  # roster shows liveness; feed stays uncluttered

        ts = _short_ts(env.ts)
        line = Text()
        line.append(f"{ts} ", style="dim")

        if env.kind == "chat":
            line.append(env.source, style="bold cyan")
            line.append(" → ", style="dim")
            line.append(env.target, style="cyan")
            line.append(": ", style="dim")
            line.append(getattr(env.payload, "text", ""), style="white")
        elif env.kind == "status":
            state = getattr(env.payload, "state", None)
            detail = getattr(env.payload, "detail", None)
            line.append(env.source, style="bold")
            line.append(" status ", style="dim")
            line.append(str(state), style=_state_style(state))
            if detail:
                line.append(f"  ({detail})", style="dim")
        elif env.kind == "error":
            severity = getattr(env.payload, "severity", Severity.ERROR)
            message = getattr(env.payload, "message", "")
            line.append(f"⚠ [{severity}] ", style="red bold")
            line.append(env.source, style="red")
            line.append(f": {message}", style="red")
        elif env.kind == "task":
            tid = getattr(env.payload, "task_id", "?")
            tstate = getattr(env.payload, "state", "?")
            detail = getattr(env.payload, "detail", "")
            line.append(f"task {tid} ", style="yellow")
            line.append(f"{tstate}", style="bold yellow")
            line.append(f"  {env.source}", style="dim")
            if detail:
                line.append(f"  {detail}", style="white")
        elif env.kind == "metric":
            name = getattr(env.payload, "name", "?")
            value = getattr(env.payload, "value", 0.0)
            unit = getattr(env.payload, "unit", "") or ""
            line.append(f"metric {env.source} {name}={value}{unit}", style="dim")
        else:
            line.append(f"{env.kind} {env.source} → {env.target}", style="white")

        self.write(line)


def _short_ts(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def _state_style(state: Any) -> str:
    s = str(state).lower()
    if "connected" in s and "dis" not in s:
        return "green"
    if "degraded" in s:
        return "orange1"
    if "disconnected" in s:
        return "red"
    if "connecting" in s:
        return "yellow"
    return "grey50"
