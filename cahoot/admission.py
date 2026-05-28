"""Admission policy — decides whether a connecting agent joins the fleet.

Two modes, chosen via ``[cahoot.admission].mode`` in ``cahoot.toml``:

* ``open`` *(default)* — every agent Cahoot successfully spawns is admitted
  as soon as it ACKs the welcome prompt. Because Cahoot only spawns agents
  declared in its config, the config itself acts as the implicit allowlist.

* ``strict`` — only agent IDs listed in ``allowed_ids`` (or, for v1 without
  the command box, also any agent listed in ``[[agents]]``) are admitted.
  Anyone else lands in ``QUARANTINED``: operator-only visibility, no DMs
  to peers, no broadcast.

Future v1.5 will add an interactive ``/approve <agent_id>`` command via the
TUI's command box; until then approval is config-driven.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "AdmissionDecision",
    "AdmissionMode",
    "AdmissionPolicy",
]


AdmissionMode = Literal["open", "strict"]


@dataclass(frozen=True)
class AdmissionPolicy:
    """How Cahoot decides who's in the fleet."""

    mode: AdmissionMode = "open"
    allowed_ids: frozenset[str] = field(default_factory=frozenset)
    """Explicit allowlist for ``strict`` mode. Empty in ``open`` mode."""


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str | None
    """Operator-facing one-liner; surfaced to the agent if quarantined."""


def decide(policy: AdmissionPolicy, agent_id: str) -> AdmissionDecision:
    """Apply the policy to a freshly-ACKed agent."""
    if policy.mode == "open":
        return AdmissionDecision(admitted=True, reason=None)
    if agent_id in policy.allowed_ids:
        return AdmissionDecision(admitted=True, reason=None)
    return AdmissionDecision(
        admitted=False,
        reason=(
            f"agent_id {agent_id!r} not in admission allowlist "
            f"(strict mode); add it to [cahoot.admission].allowed_ids to admit"
        ),
    )
