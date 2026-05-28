"""Tests for the onboarding message templates and ACK scanning."""

from __future__ import annotations

import pytest

from cahoot.onboarding import (
    ACK_TOKEN,
    EnrollmentState,
    build_instructions_prompt,
    build_quarantine_notice,
    build_welcome_prompt,
    extract_ack,
)


class TestWelcomePrompt:
    def test_carries_agent_id_role_room_and_ack_token(self) -> None:
        msg = build_welcome_prompt(agent_id="hermes-main", role="orchestrator", room="ops")
        assert "hermes-main" in msg
        assert "orchestrator" in msg
        assert "ops" in msg
        assert ACK_TOKEN in msg


class TestExtractAck:
    @pytest.mark.parametrize(
        "reply",
        [
            "READY",
            "ready when you are",
            "Acknowledged. READY.",
            "I'm READY, captain.",
        ],
    )
    def test_matches_token_case_insensitive(self, reply: str) -> None:
        assert extract_ack(reply) is True

    @pytest.mark.parametrize(
        "reply",
        [None, "", "alreadydone", "preparedness"],
    )
    def test_rejects_substrings_and_empty(self, reply: str | None) -> None:
        # `\bREADY\b` correctly rejects substrings inside other words.
        # Hyphenated forms like "load-ready-state" do match, which we treat as
        # acceptable — natural-language phrases like "I'm ready" should ack.
        assert extract_ack(reply) is False


class TestInstructionsAndQuarantine:
    def test_instructions_mention_addressing_and_markers(self) -> None:
        out = build_instructions_prompt(
            agent_id="openclaw-formatter-1",
            role="formatter",
            room="ops",
        )
        # Addressing modes documented.
        for token in ("@operator", "@all", "@<agent_id>"):
            assert token in out
        # Structured event markers documented.
        for marker in ("task:", "metric:", "error:"):
            assert marker in out

    def test_quarantine_includes_reason_when_given(self) -> None:
        out = build_quarantine_notice(
            agent_id="rogue-1",
            reason="not in admission allowlist",
        )
        assert "QUARANTINED" in out
        assert "rogue-1" in out
        assert "not in admission allowlist" in out

    def test_quarantine_works_without_reason(self) -> None:
        out = build_quarantine_notice(agent_id="rogue-1")
        assert "QUARANTINED" in out
        assert "rogue-1" in out


class TestEnrollmentState:
    def test_state_string_values_stable(self) -> None:
        # Persisted to the operator log; don't rename casually.
        assert EnrollmentState.PENDING == "pending"
        assert EnrollmentState.AWAITING_ACK == "awaiting_ack"
        assert EnrollmentState.QUARANTINED == "quarantined"
        assert EnrollmentState.ADMITTED == "admitted"
        assert EnrollmentState.REJECTED == "rejected"
