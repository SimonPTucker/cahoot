"""Tests for the invite token registry."""

from __future__ import annotations

import time

from cahoot.invites import (
    TOKEN_PREFIX,
    TOKEN_VERSION,
    InviteRegistry,
    looks_like_token,
)


class TestTokenShape:
    def test_minted_token_has_expected_prefix_and_format(self) -> None:
        reg = InviteRegistry()
        inv = reg.mint(agent_id="hermes-main", role="planner")
        assert inv.token.startswith(f"{TOKEN_PREFIX}{TOKEN_VERSION}-")
        parts = inv.token.split("-")
        assert len(parts) == 3
        assert len(parts[1]) == 4 and len(parts[2]) == 4
        assert looks_like_token(inv.token)

    def test_tokens_are_unique_across_mints(self) -> None:
        reg = InviteRegistry()
        seen = {reg.mint(agent_id=f"a{i}", role="r").token for i in range(20)}
        assert len(seen) == 20


class TestRedemption:
    def test_valid_token_redeems_once_then_is_consumed(self) -> None:
        reg = InviteRegistry()
        inv = reg.mint(agent_id="hermes-main", role="planner")
        out = reg.redeem(token=inv.token, claimed_agent_id="hermes-main")
        assert out.outcome == "ok"
        # Second redeem fails — single-use.
        again = reg.redeem(token=inv.token, claimed_agent_id="hermes-main")
        assert again.outcome == "unknown_token"

    def test_wrong_agent_id_rejected(self) -> None:
        reg = InviteRegistry()
        inv = reg.mint(agent_id="hermes-main", role="planner")
        out = reg.redeem(token=inv.token, claimed_agent_id="someone-else")
        assert out.outcome == "wrong_agent_id"
        # The invite stays available so the legitimate connect can still
        # land — only the wrong claim is rejected.
        assert inv.token in {i.token for i in reg.outstanding()}

    def test_unknown_token_rejected(self) -> None:
        reg = InviteRegistry()
        out = reg.redeem(token="CH7-AAAA-BBBB", claimed_agent_id="x")
        assert out.outcome == "unknown_token"

    def test_expired_token_rejected_and_evicted(self) -> None:
        reg = InviteRegistry(ttl_s=0.01)
        inv = reg.mint(agent_id="hermes-main", role="planner")
        time.sleep(0.05)
        out = reg.redeem(token=inv.token, claimed_agent_id="hermes-main")
        assert out.outcome == "expired"
        assert inv.token not in {i.token for i in reg.outstanding()}


class TestRegistryHousekeeping:
    def test_outstanding_lists_minted_tokens(self) -> None:
        reg = InviteRegistry()
        a = reg.mint(agent_id="a", role="r")
        b = reg.mint(agent_id="b", role="r")
        tokens = {i.token for i in reg.outstanding()}
        assert {a.token, b.token} <= tokens

    def test_revoke_drops_a_token(self) -> None:
        reg = InviteRegistry()
        inv = reg.mint(agent_id="a", role="r")
        assert reg.revoke(inv.token) is True
        # Second revoke is a no-op.
        assert reg.revoke(inv.token) is False

    def test_prune_expired_drops_only_expired(self) -> None:
        reg = InviteRegistry()
        fresh = reg.mint(agent_id="a", role="r", ttl_s=60)
        stale = reg.mint(agent_id="b", role="r", ttl_s=0.01)
        time.sleep(0.05)
        pruned = reg.prune_expired()
        assert pruned == 1
        assert stale.token not in {i.token for i in reg.outstanding()}
        assert fresh.token in {i.token for i in reg.outstanding()}
