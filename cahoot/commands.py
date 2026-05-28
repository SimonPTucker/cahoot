"""Operator command parser and dispatcher.

The Textual UI's bottom-of-screen input field hands every line to
:func:`parse`. The result is either a :class:`Command` value object (one
of the slash commands) or a :class:`Broadcast` (plain text → ``@all``).

Execution lives separately in :func:`execute`, which knows about the bus,
event store, and adapter registry. This split keeps parsing trivially
testable and lets the UI render help / usage errors without needing to
plumb live state.

Slash commands supported (v1):

* ``/dm <agent_id> <text>`` — direct message a specific agent.
* ``/all <text>`` — explicit broadcast (same as no slash).
* ``/whoami`` — print operator context (hostname, user, tmux, SSH).
* ``/where`` — alias of ``/whoami``.
* ``/approve <agent_id>`` — admit a quarantined agent.
* ``/deny <agent_id> [reason]`` — quarantine an admitted agent.
* ``/roster`` — list connected agents + enrollment + state.
* ``/help`` — show this list.
* ``/quit`` — clean shutdown.

Any line that does not start with ``/`` is a broadcast (``target="all"``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .adapter import AgentAdapter
from .bus import Bus
from .envelope import chat
from .onboarding import EnrollmentState
from .runtime import session_context

__all__ = [
    "Approve",
    "Broadcast",
    "Command",
    "CommandResult",
    "Deny",
    "DirectMessage",
    "Help",
    "ParsedInput",
    "Quit",
    "Roster",
    "Whoami",
    "execute",
    "parse",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Broadcast:
    text: str


@dataclass(frozen=True)
class DirectMessage:
    agent_id: str
    text: str


@dataclass(frozen=True)
class Whoami:
    pass


@dataclass(frozen=True)
class Approve:
    agent_id: str


@dataclass(frozen=True)
class Deny:
    agent_id: str
    reason: str | None = None


@dataclass(frozen=True)
class Roster:
    pass


@dataclass(frozen=True)
class Help:
    pass


@dataclass(frozen=True)
class Quit:
    pass


@dataclass(frozen=True)
class ParseError:
    message: str


Command = Broadcast | DirectMessage | Whoami | Approve | Deny | Roster | Help | Quit
ParsedInput = Command | ParseError


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse(raw: str) -> ParsedInput:
    """Parse one input line.

    Empty / whitespace-only input is reported as a ParseError so the UI can
    no-op rather than emit an empty chat.
    """
    text = raw.strip()
    if not text:
        return ParseError("empty input")

    if not text.startswith("/"):
        return Broadcast(text=text)

    # Slash command — split off the verb.
    head, *rest = text.split(maxsplit=1)
    verb = head[1:].lower()
    arg = rest[0] if rest else ""

    if verb in {"whoami", "where"}:
        return Whoami()
    if verb in {"roster", "agents", "fleet"}:
        return Roster()
    if verb in {"help", "?"}:
        return Help()
    if verb in {"quit", "exit", "q"}:
        return Quit()
    if verb == "all":
        if not arg:
            return ParseError("/all needs text")
        return Broadcast(text=arg)
    if verb == "dm":
        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            return ParseError("/dm <agent_id> <text>")
        return DirectMessage(agent_id=parts[0], text=parts[1])
    if verb == "approve":
        if not arg:
            return ParseError("/approve <agent_id>")
        return Approve(agent_id=arg.strip())
    if verb == "deny":
        parts = arg.split(maxsplit=1)
        if not parts:
            return ParseError("/deny <agent_id> [reason]")
        reason = parts[1] if len(parts) > 1 else None
        return Deny(agent_id=parts[0], reason=reason)

    return ParseError(f"unknown command: /{verb} (try /help)")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """What ``execute`` returns — drives the feed and (for /quit) shutdown."""

    feedback: str
    """One-line text to render in the feed (operator-visible)."""

    quit_requested: bool = False
    """Tell the runtime to stop after rendering feedback."""


_HELP_TEXT = (
    "commands:\n"
    "  /dm <agent_id> <text>   direct message an agent\n"
    "  /all <text>             broadcast (also: any plain text)\n"
    "  /whoami | /where        operator context\n"
    "  /roster | /fleet        list agents + enrollment state\n"
    "  /approve <agent_id>     admit a quarantined agent\n"
    "  /deny <agent_id> [why]  quarantine an admitted agent\n"
    "  /help                   this help\n"
    "  /quit                   clean shutdown"
)


async def execute(
    parsed: ParsedInput,
    *,
    bus: Bus,
    adapters: dict[str, AgentAdapter],
    operator_id: str = "operator",
    room: str = "ops",
) -> CommandResult:
    """Apply ``parsed`` to the live bus / adapters.

    Returns a :class:`CommandResult` describing what to render in the feed
    and whether to shut down.
    """
    if isinstance(parsed, ParseError):
        return CommandResult(feedback=f"⚠ {parsed.message}")

    if isinstance(parsed, Help):
        return CommandResult(feedback=_HELP_TEXT)

    if isinstance(parsed, Quit):
        return CommandResult(feedback="shutdown requested", quit_requested=True)

    if isinstance(parsed, Whoami):
        ctx = session_context()
        lines = [f"  {k:<14} {v}" for k, v in ctx.items()]
        return CommandResult(feedback="session:\n" + "\n".join(lines))

    if isinstance(parsed, Roster):
        if not adapters:
            return CommandResult(feedback="roster: (none)")
        rows = []
        for aid, a in adapters.items():
            enroll = getattr(a, "enrollment", None)
            enroll_str = enroll.value if isinstance(enroll, EnrollmentState) else "n/a"
            rows.append(f"  {aid:<24} {a.role:<14} {a.state.value:<12} {enroll_str}")
        return CommandResult(feedback="roster:\n" + "\n".join(rows))

    if isinstance(parsed, Broadcast):
        await bus.publish(chat(operator_id, "all", parsed.text, room=room))
        return CommandResult(feedback=f"→ all: {parsed.text}")

    if isinstance(parsed, DirectMessage):
        if parsed.agent_id not in adapters:
            return CommandResult(feedback=f"⚠ no such agent: {parsed.agent_id}")
        await bus.publish(chat(operator_id, parsed.agent_id, parsed.text, room=room))
        return CommandResult(feedback=f"→ {parsed.agent_id}: {parsed.text}")

    if isinstance(parsed, Approve):
        adapter = adapters.get(parsed.agent_id)
        if adapter is None:
            return CommandResult(feedback=f"⚠ no such agent: {parsed.agent_id}")
        admit = getattr(adapter, "admit", None)
        if admit is None:
            return CommandResult(feedback=f"⚠ {parsed.agent_id} does not support admit/deny")
        changed = await admit(by=operator_id)
        if changed:
            return CommandResult(feedback=f"✅ admitted {parsed.agent_id}")
        return CommandResult(feedback=f"= {parsed.agent_id} already admitted")

    if isinstance(parsed, Deny):
        adapter = adapters.get(parsed.agent_id)
        if adapter is None:
            return CommandResult(feedback=f"⚠ no such agent: {parsed.agent_id}")
        quarantine = getattr(adapter, "quarantine", None)
        if quarantine is None:
            return CommandResult(feedback=f"⚠ {parsed.agent_id} does not support admit/deny")
        changed = await quarantine(by=operator_id, reason=parsed.reason)
        if changed:
            tail = f" — {parsed.reason}" if parsed.reason else ""
            return CommandResult(feedback=f"⛔ quarantined {parsed.agent_id}{tail}")
        return CommandResult(feedback=f"= {parsed.agent_id} already quarantined")

    # Exhaustive — mypy verifies all variants are covered above.
    return CommandResult(feedback=f"⚠ unhandled command: {parsed!r}")  # type: ignore[unreachable]


# ---------------------------------------------------------------------------
# Helper used by the UI to surface operator-side feedback as a real envelope
# so it shows up in the feed and the store alongside everything else.
# ---------------------------------------------------------------------------


async def announce(
    bus: Bus,
    result: CommandResult,
    *,
    operator_id: str = "operator",
    room: str = "ops",
) -> None:
    """Push the command feedback onto the bus as an operator-sourced chat.

    The feed widget renders chat envelopes already, so the operator's own
    `/whoami` output, `/roster` results, and DM echoes all appear in the
    same scrollback as agent traffic.
    """
    await bus.publish(chat(operator_id, "operator", result.feedback, room=room))
