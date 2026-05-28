"""Tests for the command parser and executor."""

from __future__ import annotations

import asyncio

from cahoot.bus import InMemoryBus
from cahoot.commands import (
    Approve,
    Broadcast,
    Deny,
    DirectMessage,
    Help,
    Quit,
    Roster,
    Whoami,
    execute,
    parse,
)
from cahoot.commands import ParseError as PE


class TestParse:
    def test_blank_is_parse_error(self) -> None:
        assert isinstance(parse("   "), PE)

    def test_plain_text_is_broadcast(self) -> None:
        out = parse("hello fleet")
        assert isinstance(out, Broadcast) and out.text == "hello fleet"

    def test_slash_all(self) -> None:
        out = parse("/all heads up")
        assert isinstance(out, Broadcast) and out.text == "heads up"

    def test_slash_all_without_text_errors(self) -> None:
        assert isinstance(parse("/all"), PE)

    def test_slash_dm(self) -> None:
        out = parse("/dm hermes-main please review")
        assert isinstance(out, DirectMessage)
        assert out.agent_id == "hermes-main"
        assert out.text == "please review"

    def test_slash_dm_missing_text(self) -> None:
        assert isinstance(parse("/dm hermes-main"), PE)

    def test_whoami_and_where_alias(self) -> None:
        assert isinstance(parse("/whoami"), Whoami)
        assert isinstance(parse("/where"), Whoami)

    def test_roster_and_aliases(self) -> None:
        assert isinstance(parse("/roster"), Roster)
        assert isinstance(parse("/agents"), Roster)
        assert isinstance(parse("/fleet"), Roster)

    def test_approve_and_deny(self) -> None:
        a = parse("/approve hermes-main")
        d = parse("/deny rogue-1 hostile")
        assert isinstance(a, Approve) and a.agent_id == "hermes-main"
        assert isinstance(d, Deny)
        assert d.agent_id == "rogue-1" and d.reason == "hostile"

    def test_help_and_quit(self) -> None:
        assert isinstance(parse("/help"), Help)
        assert isinstance(parse("/?"), Help)
        assert isinstance(parse("/quit"), Quit)
        assert isinstance(parse("/q"), Quit)

    def test_unknown_command(self) -> None:
        out = parse("/blarghle x")
        assert isinstance(out, PE)
        assert "blarghle" in out.message


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Adapter stand-in with the surface execute() touches."""

    def __init__(self, agent_id: str, role: str) -> None:
        self.agent_id = agent_id
        self.role = role
        self._enrollment_value = "pending"
        from cahoot.envelope import AgentState

        self.state = AgentState.CONNECTED
        self.admit_calls = 0
        self.quarantine_calls = 0
        self.admit_changed = True
        self.quarantine_changed = True

    @property
    def enrollment(self) -> str:
        return self._enrollment_value

    async def admit(self, *, by: str = "operator") -> bool:
        self.admit_calls += 1
        return self.admit_changed

    async def quarantine(self, *, by: str = "operator", reason: str | None = None) -> bool:
        self.quarantine_calls += 1
        return self.quarantine_changed


async def test_execute_broadcast_publishes_envelope() -> None:
    bus = InMemoryBus()
    bus.subscribe("operator")
    a = bus.subscribe("hermes")
    await execute(Broadcast("hello"), bus=bus, adapters={"hermes": _FakeAdapter("hermes", "x")})  # type: ignore[dict-item]
    env = await asyncio.wait_for(a.get(), timeout=1.0)
    assert env.payload.text == "hello"
    assert env.target == "all"


async def test_execute_dm_routes_to_named_agent() -> None:
    bus = InMemoryBus()
    bus.subscribe("operator")
    h = bus.subscribe("hermes-main")
    adapters = {"hermes-main": _FakeAdapter("hermes-main", "orchestrator")}
    res = await execute(DirectMessage("hermes-main", "review"), bus=bus, adapters=adapters)  # type: ignore[arg-type]
    env = await asyncio.wait_for(h.get(), timeout=1.0)
    assert env.payload.text == "review"
    assert env.target == "hermes-main"
    assert "review" in res.feedback


async def test_execute_dm_unknown_agent_reports() -> None:
    bus = InMemoryBus()
    bus.subscribe("operator")
    res = await execute(DirectMessage("ghost", "x"), bus=bus, adapters={})
    assert "no such agent" in res.feedback


async def test_execute_approve_calls_admit() -> None:
    bus = InMemoryBus()
    bus.subscribe("operator")
    a = _FakeAdapter("hermes-main", "orchestrator")
    res = await execute(Approve("hermes-main"), bus=bus, adapters={"hermes-main": a})  # type: ignore[dict-item]
    assert a.admit_calls == 1
    assert "admitted" in res.feedback


async def test_execute_deny_calls_quarantine() -> None:
    bus = InMemoryBus()
    bus.subscribe("operator")
    a = _FakeAdapter("hermes-main", "orchestrator")
    res = await execute(
        Deny("hermes-main", reason="testing"),
        bus=bus,
        adapters={"hermes-main": a},  # type: ignore[dict-item]
    )
    assert a.quarantine_calls == 1
    assert "quarantined" in res.feedback


async def test_execute_quit_sets_flag() -> None:
    bus = InMemoryBus()
    res = await execute(Quit(), bus=bus, adapters={})
    assert res.quit_requested is True


async def test_execute_roster_lists_agents() -> None:
    bus = InMemoryBus()
    adapters = {"hermes": _FakeAdapter("hermes", "orchestrator")}
    res = await execute(Roster(), bus=bus, adapters=adapters)  # type: ignore[arg-type]
    assert "hermes" in res.feedback
    assert "orchestrator" in res.feedback
