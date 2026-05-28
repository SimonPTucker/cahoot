"""Agent roster widget.

Renders one row per connected agent showing:

* a status dot (●) coloured by :class:`AgentState`,
* the agent_id and role,
* the heartbeat age in human language ("2s ago"),
* the enrollment state badge if the adapter exposes it.

Subscribes to envelopes through a callback installed by :class:`ConnApp`;
purely reactive — no calls back into adapters.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from ..envelope import AgentState, Envelope

__all__ = ["RosterRow", "RosterWidget"]


_STATE_COLOR = {
    AgentState.OFFLINE: "grey50",
    AgentState.CONNECTING: "yellow",
    AgentState.CONNECTED: "green",
    AgentState.DEGRADED: "orange1",
    AgentState.DISCONNECTED: "red",
}


@dataclass
class RosterRow:
    agent_id: str
    role: str = "?"
    state: AgentState = AgentState.OFFLINE
    enrollment: str = "pending"
    last_seen: float = field(default_factory=time.monotonic)


class RosterWidget(Static):
    """Left column. Recomputes its rich-text body whenever rows change."""

    rows: reactive[dict[str, RosterRow]] = reactive(dict, layout=False, repaint=False)

    def on_mount(self) -> None:
        self.border_title = "agents"
        # Periodic repaint so the "2s ago" countdown updates.
        self.set_interval(1.0, self.refresh)

    def ingest(self, env: Envelope) -> None:
        """Update roster state from one envelope."""
        if env.source in {"operator", "_store", "system"}:
            return
        row = self.rows.get(env.source) or RosterRow(agent_id=env.source)
        row.last_seen = time.monotonic()
        if env.kind == "status":
            state = getattr(env.payload, "state", None)
            if isinstance(state, AgentState):
                row.state = state
        if env.kind == "chat":
            # Detect the [enrollment] preamble we publish on admission changes.
            text = getattr(env.payload, "text", "")
            if text.startswith("[enrollment] ") or text.startswith("[admission] "):
                # Last token is the new state ("admitted", "quarantined", …)
                row.enrollment = text.rsplit(":", 1)[-1].strip().split(" ", 1)[0]
        self.rows[env.source] = row
        # ``rows`` is a Reactive with repaint=False; nudge an explicit refresh.
        self.mutate_reactive(RosterWidget.rows)
        self.refresh()

    def render(self) -> Any:
        if not self.rows:
            return Text("(no agents connected)", style="dim")
        out = Text()
        now = time.monotonic()
        for row in sorted(self.rows.values(), key=lambda r: r.agent_id):
            colour = _STATE_COLOR.get(row.state, "white")
            out.append("● ", style=colour)
            out.append(f"{row.agent_id:<22}", style="bold")
            out.append(f" {row.role:<12}", style="dim")
            out.append(f" {_age(now - row.last_seen):<6}", style="dim")
            badge_style = "green" if row.enrollment == "admitted" else "yellow"
            out.append(f" {row.enrollment}", style=badge_style)
            out.append("\n")
        return out


def _age(seconds: float) -> str:
    """Human-friendly heartbeat age."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds / 3600)}h"
