"""Tests for the typed event envelope."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cahoot.envelope import (
    AgentState,
    ChatPayload,
    Envelope,
    chat,
    status,
)


class TestEnvelope:
    def test_chat_factory_roundtrip_via_discriminator(self) -> None:
        env = chat("operator", "hermes", "hello")
        dumped = env.model_dump_json()
        rebuilt = Envelope.model_validate_json(dumped)
        assert isinstance(rebuilt.payload, ChatPayload)
        assert rebuilt.payload.text == "hello"
        assert rebuilt.kind == "chat"
        assert rebuilt.source == "operator"
        assert rebuilt.target == "hermes"

    def test_envelope_is_frozen(self) -> None:
        env = chat("a", "b", "hi")
        with pytest.raises(ValidationError):
            env.source = "c"  # type: ignore[misc]

    def test_payload_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ChatPayload(text="ok", nonsense="boom")  # type: ignore[call-arg]

    def test_status_factory_carries_enum(self) -> None:
        env = status("hermes", AgentState.CONNECTED, detail="online")
        assert env.kind == "status"
        assert env.payload.state is AgentState.CONNECTED  # type: ignore[union-attr]
        assert env.payload.detail == "online"  # type: ignore[union-attr]
