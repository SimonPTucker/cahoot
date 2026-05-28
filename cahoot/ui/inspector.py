"""Per-agent inspector drawer.

When the operator selects (or DMs / mentions) an agent, this widget shows:

* canonical agent_id and role,
* current :class:`AgentState` + enrollment,
* last known error message if any,
* last task and its state,
* simple counters for chat / task / metric envelopes since process start.

The inspector renders whatever :class:`AgentInfo` it's holding; the parent
:class:`ConnApp` swaps the info object when the focus changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from ..envelope import AgentState, Envelope, Severity

__all__ = ["AgentInfo", "InspectorWidget"]


@dataclass
class AgentInfo:
    agent_id: str
    role: str = "?"
    state: AgentState = AgentState.OFFLINE
    enrollment: str = "pending"
    last_error: str | None = None
    last_error_severity: str | None = None
    last_task: str | None = None
    last_task_state: str | None = None
    chats: int = 0
    tasks: int = 0
    metrics: int = 0
    extras: dict[str, str] = field(default_factory=dict)

    def ingest(self, env: Envelope) -> None:
        if env.kind == "status":
            state = getattr(env.payload, "state", None)
            if isinstance(state, AgentState):
                self.state = state
        elif env.kind == "error":
            self.last_error = getattr(env.payload, "message", None)
            sev = getattr(env.payload, "severity", Severity.ERROR)
            self.last_error_severity = str(sev)
        elif env.kind == "task":
            self.last_task = getattr(env.payload, "task_id", None)
            self.last_task_state = getattr(env.payload, "state", None)
            self.tasks += 1
        elif env.kind == "chat":
            self.chats += 1
            text = getattr(env.payload, "text", "")
            if text.startswith("[enrollment] ") or text.startswith("[admission] "):
                self.enrollment = text.rsplit(":", 1)[-1].strip().split(" ", 1)[0]
        elif env.kind == "metric":
            self.metrics += 1


class InspectorWidget(Static):
    """Right column. ``focus_on(agent_id)`` swaps the rendered AgentInfo."""

    info: reactive[AgentInfo | None] = reactive(None, layout=False, repaint=False)

    def on_mount(self) -> None:
        self.border_title = "inspector"
        self.set_interval(1.0, self.refresh)

    def focus_on(self, info: AgentInfo) -> None:
        self.info = info
        self.refresh()

    def ingest(self, env: Envelope) -> None:
        """Forward an envelope into the currently-focused AgentInfo."""
        if self.info is None or env.source != self.info.agent_id:
            return
        self.info.ingest(env)
        self.refresh()

    def render(self) -> Any:
        if self.info is None:
            return Text("(no agent selected)", style="dim")
        i = self.info
        out = Text()
        out.append(f"{i.agent_id}\n", style="bold cyan")
        out.append(f"  role        {i.role}\n", style="white")
        out.append("  state       ", style="dim")
        out.append(f"{i.state.value}\n", style=_state_color(i.state))
        out.append("  enrollment  ", style="dim")
        out.append(f"{i.enrollment}\n", style="green" if i.enrollment == "admitted" else "yellow")
        out.append(f"  chats       {i.chats}\n", style="dim")
        out.append(f"  tasks       {i.tasks}\n", style="dim")
        out.append(f"  metrics     {i.metrics}\n", style="dim")
        if i.last_task:
            out.append(f"\nlast task   {i.last_task} ({i.last_task_state})\n", style="yellow")
        if i.last_error:
            out.append("\nlast error  ", style="red bold")
            out.append(f"[{i.last_error_severity}] {i.last_error}\n", style="red")
        return out


def _state_color(state: AgentState) -> str:
    if state is AgentState.CONNECTED:
        return "green"
    if state is AgentState.DEGRADED:
        return "orange1"
    if state is AgentState.DISCONNECTED:
        return "red"
    if state is AgentState.CONNECTING:
        return "yellow"
    return "grey50"
