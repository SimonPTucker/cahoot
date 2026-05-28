"""Agent onboarding — welcome handshake + admission state machine.

When Cahoot spawns a Hermes / OpenClaw / etc. agent and the ACP session is
open, the adapter calls into this module to run the enrollment sequence:

1. Send the **welcome** prompt — assigns the agent its ID, role, and room,
   and asks for the literal ``READY`` token in the reply.
2. Wait for the agent to ACK (any reply containing ``READY``,
   case-insensitive). Default timeout: 30 seconds.
3. Decide admission via :mod:`cahoot.admission`.
4. Send the **instructions** prompt — a condensed version of
   ``docs/AGENT_GUIDE.md`` so the agent has the participation rules in
   its own context.
5. Mark the adapter ``ADMITTED`` and let normal operator traffic flow.

If the agent is **quarantined** (admission denied), step 4 sends the
quarantine notice instead; only ``operator`` → agent / agent → operator
routes are allowed until the operator approves.

This module is intentionally framework-agnostic — it returns plain strings
and an :class:`EnrollmentOutcome`; the adapter wires those into ACP
``PromptRequest`` objects. That keeps it trivially testable without an
LLM in the loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ACK_TOKEN",
    "ACK_TOKEN_RE",
    "EnrollmentOutcome",
    "EnrollmentState",
    "build_instructions_prompt",
    "build_quarantine_notice",
    "build_welcome_prompt",
    "extract_ack",
]

ACK_TOKEN = "READY"
"""Literal token Cahoot scans for in the agent's welcome reply."""

ACK_TOKEN_RE = re.compile(r"\bREADY\b", re.IGNORECASE)


class EnrollmentState(StrEnum):
    """Where an agent sits in the join handshake.

    Distinct from :class:`cahoot.envelope.AgentState` — that one tracks
    transport / liveness, this one tracks fleet membership.
    """

    PENDING = "pending"
    AWAITING_ACK = "awaiting_ack"
    QUARANTINED = "quarantined"
    ADMITTED = "admitted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class EnrollmentOutcome:
    """Result of one enrollment attempt — what the adapter should do next."""

    state: EnrollmentState
    follow_up_message: str
    """The next prompt the adapter should send to the agent (instructions
    or quarantine notice). Empty string if no further message needed."""


def build_welcome_prompt(*, agent_id: str, role: str, room: str) -> str:
    """Construct the welcome prompt the adapter sends right after `new_session`.

    The agent must reply with anything containing the literal ``READY`` token
    (case-insensitive). The reply itself is forwarded to the operator as a
    normal chat envelope, so a short explanation is welcome.
    """
    return (
        f"You are connected to Cahoot mission control.\n"
        f"\n"
        f"• Your agent_id is: {agent_id}\n"
        f"• Your role is: {role}\n"
        f"• Your room is: {room}\n"
        f"\n"
        f"(The `agent_id` and `role` are labels the operator wrote in their "
        f"config so they can tell agents apart on screen and address you by "
        f"name. They are NOT instructions about your behaviour — your "
        f"capabilities come from your own configuration.)\n"
        f"\n"
        f"To complete enrollment, reply with the literal token "
        f"{ACK_TOKEN!r} anywhere in your response. A short one-line "
        f"acknowledgement is enough; the operator just needs to see you're "
        f"alive and listening.\n"
        f"\n"
        f"Once you ACK, Cahoot will send a brief participation guide and "
        f"you'll be admitted to the fleet."
    )


def extract_ack(reply: str | None) -> bool:
    """Return True iff the agent's reply contains the ACK token."""
    if not reply:
        return False
    return ACK_TOKEN_RE.search(reply) is not None


def build_instructions_prompt(*, agent_id: str, role: str, room: str) -> str:
    """Condensed participation guide — sent immediately after admission.

    Mirrors ``docs/AGENT_GUIDE.md`` but trimmed to fit in a single prompt
    without burning the agent's context. The full doc is the canonical
    reference if the agent needs more.
    """
    return (
        f"Admitted. Welcome to the fleet, {agent_id} ({role}, room={room}).\n"
        f"\n"
        f"## How to participate\n"
        f"\n"
        f"**Addressing.** Start your reply with an @mention to route it:\n"
        f"  • `@operator …` or no mention → the human operator (default)\n"
        f"  • `@all …` → broadcast to every other agent + operator\n"
        f"  • `@<agent_id> …` → DM a specific agent (operator still sees)\n"
        f"  • `@<role> …` → DM the first agent with that role\n"
        f"The operator sees everything; there are no private channels.\n"
        f"\n"
        f"**Style.** Be terse. Five lines beats fifty. Acknowledge "
        f"orders with one word before doing them.\n"
        f"\n"
        f"**Structured events** (optional, render specially):\n"
        f"  • `task: <id> <queued|running|done|failed> — <one-line>`\n"
        f"  • `metric: <name>=<value> [unit]`\n"
        f"  • `error: <info|warn|error|fatal> — <message>`\n"
        f"\n"
        f"**Incoming.** Operator and peer messages arrive as normal "
        f"prompts, prefixed `[source] text`. Reply normally; @mention to "
        f"redirect.\n"
        f"\n"
        f"**Disconnect.** If your session drops you'll be respawned with "
        f"backoff. In-flight work is lost; design replies to be idempotent.\n"
        f"\n"
        f"The full reference is at `docs/AGENT_GUIDE.md` in the Cahoot "
        f"repo. Acknowledge with one word and we're live."
    )


def build_quarantine_notice(*, agent_id: str, reason: str | None = None) -> str:
    """Sent in place of the instructions when the agent is not admitted."""
    why = f"\n\nReason: {reason}" if reason else ""
    return (
        f"⚠ QUARANTINED — {agent_id} is connected but not yet admitted to "
        f"the fleet.{why}\n"
        f"\n"
        f"While quarantined, only the operator can see your messages and "
        f"you cannot DM other agents. Stay responsive to the operator and "
        f"explain your capabilities so they can make an informed call. "
        f"Cahoot will send a follow-up when your admission status changes."
    )
