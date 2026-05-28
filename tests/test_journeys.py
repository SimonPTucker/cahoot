"""End-to-end user-journey tests for the Textual UI.

Drives every operator action through the real :class:`ConnApp` via Textual's
``run_test`` pilot, so we exercise the actual UI / bus / commands / store
pipeline rather than testing them in isolation. Every journey emits a
single ``✅`` / ``❌`` line on stdout when run with ``-s`` so the user can
read the live-test transcript at a glance.

Run interactively::

    pytest tests/test_journeys.py -s -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cahoot.bus import InMemoryBus
from cahoot.envelope import AgentState, Envelope, chat, status
from cahoot.onboarding import EnrollmentState
from cahoot.store import open_event_store
from cahoot.ui import ConnApp

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes — synthetic adapter is fine for most paths, but /approve and /deny
# need something with admit() / quarantine().
# ---------------------------------------------------------------------------


class _ApprovableFakeAdapter:
    """Minimal adapter stand-in supporting the full admission surface."""

    def __init__(self, agent_id: str, role: str) -> None:
        self.agent_id = agent_id
        self.role = role
        self.state = AgentState.CONNECTED
        self._enrollment = EnrollmentState.QUARANTINED
        self.admit_count = 0
        self.quarantine_count = 0
        self.last_admit_by: str | None = None
        self.last_quarantine_reason: str | None = None

    @property
    def enrollment(self) -> EnrollmentState:
        return self._enrollment

    async def admit(self, *, by: str = "operator") -> bool:
        self.last_admit_by = by
        self.admit_count += 1
        if self._enrollment is EnrollmentState.ADMITTED:
            return False
        self._enrollment = EnrollmentState.ADMITTED
        return True

    async def quarantine(self, *, by: str = "operator", reason: str | None = None) -> bool:
        self.last_quarantine_reason = reason
        self.quarantine_count += 1
        if self._enrollment is EnrollmentState.QUARANTINED:
            return False
        self._enrollment = EnrollmentState.QUARANTINED
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_KEY_MAP = {
    "/": "slash",
    " ": "space",
    "-": "minus",
}


async def _type(pilot: Any, text: str) -> None:
    """Press the keys corresponding to ``text`` then Enter."""
    for ch in text:
        await pilot.press(_KEY_MAP.get(ch, ch))
    await pilot.press("enter")
    # Give the operator-queue consumer + the announce() round-trip a chance
    # to settle into the feed and the inspector.
    await pilot.pause()
    await asyncio.sleep(0.02)


def _feed_text(app: ConnApp) -> str:
    """Plain-text snapshot of the feed widget for substring assertions."""
    return "\n".join(str(line) for line in app._feed.lines)  # type: ignore[attr-defined]


def _tick(msg: str) -> None:
    print(f"  ✅ {msg}")


# ---------------------------------------------------------------------------
# Journey 1 — boot + empty-state rendering
# ---------------------------------------------------------------------------


async def test_journey_boot_with_no_agents_shows_empty_state() -> None:
    print("\nJOURNEY: boot, no agents configured")
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._roster is not None  # type: ignore[attr-defined]
        _tick("ConnApp mounts cleanly with zero agents")
        assert app._inspector.info is None  # type: ignore[attr-defined]
        _tick("inspector reports no agent selected")


# ---------------------------------------------------------------------------
# Journey 2 — boot with agents pre-loaded
# ---------------------------------------------------------------------------


async def test_journey_boot_with_preconfigured_agents() -> None:
    print("\nJOURNEY: boot with three pre-configured agents")
    bus = InMemoryBus()
    adapters = {
        "hermes-main": _ApprovableFakeAdapter("hermes-main", "orchestrator"),
        "openclaw-formatter-1": _ApprovableFakeAdapter("openclaw-formatter-1", "formatter"),
        "openclaw-formatter-2": _ApprovableFakeAdapter("openclaw-formatter-2", "formatter"),
    }
    app = ConnApp(bus, adapters, store=None, room="ops")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        # Inspector focuses on the first agent automatically.
        assert app._inspector.info is not None  # type: ignore[attr-defined]
        assert app._inspector.info.agent_id in adapters  # type: ignore[union-attr,attr-defined]
        _tick("inspector auto-focuses the first agent")
        # Internal AgentInfo dict has rows for every adapter.
        assert set(app._infos) == set(adapters)  # type: ignore[attr-defined]
        _tick("internal roster tracks every configured adapter")


# ---------------------------------------------------------------------------
# Journey 3 — /help renders the command list
# ---------------------------------------------------------------------------


async def test_journey_help_shows_command_list() -> None:
    print("\nJOURNEY: /help displays the command list")
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/help")
        feed = _feed_text(app)
        for verb in ("/dm", "/all", "/whoami", "/roster", "/approve", "/deny", "/quit"):
            assert verb in feed, f"feed missing {verb}\n{feed}"
        _tick("every documented slash command appears in the help text")


# ---------------------------------------------------------------------------
# Journey 4 — /whoami shows session context
# ---------------------------------------------------------------------------


async def test_journey_whoami_shows_session_context() -> None:
    print("\nJOURNEY: /whoami shows session context")
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/whoami")
        feed = _feed_text(app)
        for key in ("hostname", "user", "pid", "python"):
            assert key in feed
        _tick("/whoami surfaces hostname, user, pid, python version")


# ---------------------------------------------------------------------------
# Journey 5 — /roster lists configured agents
# ---------------------------------------------------------------------------


async def test_journey_roster_lists_configured_agents() -> None:
    print("\nJOURNEY: /roster lists the fleet")
    bus = InMemoryBus()
    adapters = {"hermes-main": _ApprovableFakeAdapter("hermes-main", "orchestrator")}
    app = ConnApp(bus, adapters, store=None, room="ops")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/roster")
        feed = _feed_text(app)
        assert "hermes-main" in feed
        assert "orchestrator" in feed
        _tick("/roster prints agent_id + role")


# ---------------------------------------------------------------------------
# Journey 6 — /dm routes to a single agent
# ---------------------------------------------------------------------------


async def test_journey_dm_routes_to_named_agent() -> None:
    print("\nJOURNEY: /dm routes to a specific agent")
    bus = InMemoryBus()
    inbox = bus.subscribe("hermes-main")
    adapters = {"hermes-main": _ApprovableFakeAdapter("hermes-main", "orchestrator")}
    app = ConnApp(bus, adapters, store=None, room="ops")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/dm hermes-main please review")
        env = await asyncio.wait_for(inbox.get(), timeout=1.0)
        assert env.target == "hermes-main"
        assert env.payload.text == "please review"  # type: ignore[union-attr]
        _tick("DM landed in the target agent's queue with correct payload")
        assert "→ hermes-main" in _feed_text(app)
        _tick("feed shows the operator's outbound DM")


# ---------------------------------------------------------------------------
# Journey 7 — /dm to unknown agent reports clearly
# ---------------------------------------------------------------------------


async def test_journey_dm_unknown_agent_reports() -> None:
    print("\nJOURNEY: /dm <unknown> reports an error")
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/dm ghost hello")
        assert "no such agent" in _feed_text(app)
        _tick("unknown-agent DM surfaces a clear error in the feed")


# ---------------------------------------------------------------------------
# Journey 8 — /all and plain text both broadcast
# ---------------------------------------------------------------------------


async def test_journey_broadcast_explicit_and_implicit() -> None:
    print("\nJOURNEY: /all and plain text both broadcast")
    bus = InMemoryBus()
    inbox = bus.subscribe("hermes-main")
    adapters = {"hermes-main": _ApprovableFakeAdapter("hermes-main", "orchestrator")}
    app = ConnApp(bus, adapters, store=None, room="ops")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/all heads up")
        await _type(pilot, "lunch in ten")
        envs: list[Envelope] = []
        for _ in range(2):
            envs.append(await asyncio.wait_for(inbox.get(), timeout=1.0))
        texts = sorted(e.payload.text for e in envs)  # type: ignore[union-attr]
        targets = {e.target for e in envs}
        assert texts == ["heads up", "lunch in ten"]
        assert targets == {"all"}
        _tick("both /all and plain text deliver to peer agents with target='all'")


# ---------------------------------------------------------------------------
# Journey 9 — /approve flips a quarantined agent → admitted
# ---------------------------------------------------------------------------


async def test_journey_approve_flips_quarantined_agent() -> None:
    print("\nJOURNEY: /approve admits a quarantined agent at runtime")
    bus = InMemoryBus()
    a = _ApprovableFakeAdapter("hermes-main", "orchestrator")
    app = ConnApp(bus, {"hermes-main": a}, store=None, room="ops")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert a.enrollment is EnrollmentState.QUARANTINED
        await _type(pilot, "/approve hermes-main")
        assert a.admit_count == 1
        assert a.last_admit_by == "operator"
        assert a.enrollment is EnrollmentState.ADMITTED
        _tick("admit() invoked exactly once, enrollment flipped to ADMITTED")
        assert "admitted" in _feed_text(app)
        _tick("feed acknowledges the admission")


# ---------------------------------------------------------------------------
# Journey 10 — /deny flips an admitted agent → quarantined with reason
# ---------------------------------------------------------------------------


async def test_journey_deny_quarantines_with_reason() -> None:
    print("\nJOURNEY: /deny quarantines an admitted agent with reason")
    bus = InMemoryBus()
    a = _ApprovableFakeAdapter("hermes-main", "orchestrator")
    a._enrollment = EnrollmentState.ADMITTED  # start admitted
    app = ConnApp(bus, {"hermes-main": a}, store=None, room="ops")  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/deny hermes-main maintenance window")
        assert a.quarantine_count == 1
        assert a.last_quarantine_reason == "maintenance window"
        assert a.enrollment is EnrollmentState.QUARANTINED
        _tick("quarantine() invoked with the operator's reason")
        feed = _feed_text(app)
        assert "quarantined" in feed
        assert "maintenance window" in feed
        _tick("feed surfaces the quarantine + reason")


# ---------------------------------------------------------------------------
# Journey 11 — unknown command is friendly
# ---------------------------------------------------------------------------


async def test_journey_unknown_command_friendly_error() -> None:
    print("\nJOURNEY: unknown command shows a friendly error")
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/wibble")
        assert "unknown command" in _feed_text(app).lower()
        _tick("/wibble doesn't crash; feed says 'unknown command'")


# ---------------------------------------------------------------------------
# Journey 12 — /quit signals shutdown
# ---------------------------------------------------------------------------


async def test_journey_quit_sets_stop_event() -> None:
    print("\nJOURNEY: /quit requests shutdown via the stop event")
    bus = InMemoryBus()
    stop = asyncio.Event()
    app = ConnApp(bus, {}, store=None, room="ops", stop_event=stop)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "/quit")
        await pilot.pause()
        assert stop.is_set()
        _tick("/quit flips the shared stop event so __main__ tears down adapters")


# ---------------------------------------------------------------------------
# Journey 13 — live envelopes update roster + inspector
# ---------------------------------------------------------------------------


async def test_journey_envelopes_update_roster_and_inspector() -> None:
    print("\nJOURNEY: live envelopes drive roster + inspector")
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Simulate a synthetic agent coming online + chatting.
        await bus.publish(status("synth-1", AgentState.CONNECTING))
        await bus.publish(status("synth-1", AgentState.CONNECTED))
        await bus.publish(chat("synth-1", "operator", "tick 1"))
        await bus.publish(chat("synth-1", "operator", "tick 2"))
        # Settle.
        for _ in range(30):
            if "synth-1" in app._infos and app._infos["synth-1"].chats >= 2:  # type: ignore[attr-defined]
                break
            await asyncio.sleep(0.02)
        info = app._infos["synth-1"]  # type: ignore[attr-defined]
        assert info.chats >= 2
        assert info.state is AgentState.CONNECTED
        _tick("inspector tracks per-agent counters and current state")
        feed = _feed_text(app)
        assert "tick 1" in feed and "tick 2" in feed
        _tick("feed shows every chat envelope chronologically")


# ---------------------------------------------------------------------------
# Journey 14 — Ctrl+L clears the feed
# ---------------------------------------------------------------------------


async def test_journey_ctrl_l_clears_feed() -> None:
    print("\nJOURNEY: Ctrl+L clears the feed")
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type(pilot, "noise to clear")
        # Confirm something is there.
        assert _feed_text(app)
        await pilot.press("ctrl+l")
        await pilot.pause()
        # The RichLog should have been cleared.
        assert not app._feed.lines  # type: ignore[attr-defined]
        _tick("Ctrl+L empties the feed")


# ---------------------------------------------------------------------------
# Journey 15 — empty input is a no-op
# ---------------------------------------------------------------------------


async def test_journey_empty_input_is_noop() -> None:
    print("\nJOURNEY: empty input does not crash and is a no-op")
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Just enter without typing.
        await pilot.press("enter")
        await pilot.pause()
        # Feed is still empty.
        assert not app._feed.lines  # type: ignore[attr-defined]
        _tick("blank Enter is silently ignored")


# ---------------------------------------------------------------------------
# Journey 16 — store wiretap persists everything, replay restores feed
# ---------------------------------------------------------------------------


async def test_journey_store_persists_and_replays(tmp_path: Path) -> None:
    print("\nJOURNEY: store wiretap persists every envelope; replay rebuilds feed")
    db = tmp_path / "cahoot.db"
    bus = InMemoryBus()
    store = await open_event_store(db)
    drain = await store.subscribe_to(bus, subscriber_id="_store")
    try:
        # Generate some traffic.
        for i in range(5):
            await bus.publish(chat("synth-1", "operator", f"persisted {i}"))
        # Settle the wiretap drain.
        for _ in range(30):
            if await store.count() >= 5:
                break
            await asyncio.sleep(0.02)
        assert await store.count() == 5
        _tick(f"store persisted {await store.count()} envelopes via wiretap")
        assert (await store.journal_mode()).lower() == "wal"
        _tick("WAL mode confirmed by PRAGMA journal_mode")

        # Boot a fresh app pointed at the SAME store and check the feed
        # backfills.
        fresh_bus = InMemoryBus()
        app = ConnApp(fresh_bus, {}, store=store, room="ops")
        async with app.run_test() as pilot:
            await pilot.pause()
            feed = _feed_text(app)
            for i in range(5):
                assert f"persisted {i}" in feed, f"missing 'persisted {i}'\n{feed}"
            _tick("fresh UI replays the last 200 envelopes from SQLite on mount")
    finally:
        drain.cancel()
        from contextlib import suppress

        with suppress(asyncio.CancelledError):
            await drain
        await store.close()
