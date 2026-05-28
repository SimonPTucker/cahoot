"""Single-use, time-limited join tokens for network onboarding.

When the operator wants a new agent to join the fleet from another box on
the LAN, they type ``/invite <agent_id> [role]`` in the TUI. Cahoot mints
a short, copy-pasteable token (e.g. ``CH7-9X42-8K3M``), records it in
this in-memory registry, and prints a one-shot ``cahoot-join`` command
the user pastes onto the remote box.

Tokens are:

* **Single-use** — consumed on first valid connect.
* **Time-limited** — default 30 minutes (configurable).
* **Bound to an agent_id + role** — the inbound side cannot impersonate
  a different agent than the operator invited.
* **In-memory only** — restarting Cahoot invalidates outstanding invites.

The token format is ``CH<digit>-<4 base32>-<4 base32>``. ``CH`` is a
recognisable prefix; the digit is a version tag so we can change the
format later without breaking parsers.

Rationale for an explicit short token over a JWT or HMAC: this is a LAN
admission gate, not an identity claim. A 60-bit secret short enough to
copy by hand without errors is the right shape.
"""

from __future__ import annotations

import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "TOKEN_PREFIX",
    "TOKEN_VERSION",
    "Invite",
    "InviteRegistry",
    "InviteResult",
]

TOKEN_PREFIX = "CH"
TOKEN_VERSION = "7"

# Base32 alphabet minus 0/O/I/1 to make hand-typing painless.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

DEFAULT_TTL_S = 30 * 60  # 30 minutes


@dataclass(frozen=True)
class Invite:
    """One outstanding join invitation."""

    token: str
    agent_id: str
    role: str
    created_at: float
    expires_at: float
    issued_by: str = "operator"

    def expired_at(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at


InviteOutcome = Literal["ok", "unknown_token", "expired", "wrong_agent_id"]


@dataclass(frozen=True)
class InviteResult:
    outcome: InviteOutcome
    invite: Invite | None = None
    reason: str | None = None


@dataclass
class InviteRegistry:
    """In-memory store of outstanding invites.

    Not thread-safe; intended to be driven from the single Cahoot event
    loop alongside the bus.
    """

    ttl_s: float = DEFAULT_TTL_S
    _by_token: dict[str, Invite] = field(default_factory=dict)

    # -- minting -------------------------------------------------------

    def mint(
        self,
        *,
        agent_id: str,
        role: str,
        ttl_s: float | None = None,
        issued_by: str = "operator",
    ) -> Invite:
        """Generate a fresh single-use token bound to ``agent_id`` + ``role``."""
        token = _generate_token()
        now = time.time()
        invite = Invite(
            token=token,
            agent_id=agent_id,
            role=role,
            created_at=now,
            expires_at=now + (ttl_s if ttl_s is not None else self.ttl_s),
            issued_by=issued_by,
        )
        self._by_token[token] = invite
        return invite

    # -- redemption ----------------------------------------------------

    def redeem(
        self,
        *,
        token: str,
        claimed_agent_id: str,
        now: float | None = None,
    ) -> InviteResult:
        """Validate ``token`` and consume it on success.

        ``claimed_agent_id`` is what the inbound connection says its ID is;
        we require it to match the bound ID so an intercepted token can't
        be used to register a different agent.
        """
        invite = self._by_token.get(token)
        if invite is None:
            return InviteResult(outcome="unknown_token", reason="no such token")
        if invite.expired_at(now):
            # Expired tokens still get evicted so the dict doesn't grow
            # without bound.
            self._by_token.pop(token, None)
            return InviteResult(
                outcome="expired",
                invite=invite,
                reason=f"token expired at {invite.expires_at}",
            )
        if invite.agent_id != claimed_agent_id:
            return InviteResult(
                outcome="wrong_agent_id",
                invite=invite,
                reason=(
                    f"token was issued for {invite.agent_id!r}, "
                    f"connection claimed {claimed_agent_id!r}"
                ),
            )
        # Success — consume.
        self._by_token.pop(token, None)
        return InviteResult(outcome="ok", invite=invite)

    # -- introspection ------------------------------------------------

    def outstanding(self) -> list[Invite]:
        return list(self._by_token.values())

    def revoke(self, token: str) -> bool:
        """Drop ``token`` if present. Returns True if it was outstanding."""
        return self._by_token.pop(token, None) is not None

    def prune_expired(self, now: float | None = None) -> int:
        """Evict expired tokens. Returns the count removed."""
        now = now or time.time()
        before = len(self._by_token)
        self._by_token = {t: inv for t, inv in self._by_token.items() if inv.expires_at > now}
        return before - len(self._by_token)


def _generate_token() -> str:
    """Mint a hand-typeable token: ``CH7-XXXX-YYYY``."""

    def chunk() -> str:
        return "".join(secrets.choice(_ALPHABET) for _ in range(4))

    return f"{TOKEN_PREFIX}{TOKEN_VERSION}-{chunk()}-{chunk()}"


def looks_like_token(s: str) -> bool:
    """Lightweight client-side sanity check; not a security boundary."""
    if not s.startswith(f"{TOKEN_PREFIX}{TOKEN_VERSION}-"):
        return False
    parts = s.split("-")
    if len(parts) != 3:
        return False
    # First chunk is the short prefix like "CH7"; the two random chunks
    # are each 4 chars from our alphabet.
    if len(parts[1]) != 4 or len(parts[2]) != 4:
        return False
    valid = set(string.ascii_uppercase + string.digits)
    return all(c in valid for c in parts[1] + parts[2])
