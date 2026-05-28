"""Textual UI smoke tests — run the app headlessly via ``App.run_test``."""

from __future__ import annotations

import asyncio

import pytest

from cahoot.bus import InMemoryBus
from cahoot.envelope import chat
from cahoot.ui import ConnApp

pytestmark = pytest.mark.asyncio


async def test_app_boots_and_renders_empty_state() -> None:
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Roster widget is mounted and reports the empty-state.
        assert app._roster is not None
        # Feed and inspector exist too.
        assert app._feed is not None
        assert app._inspector is not None


async def test_help_command_renders_feedback_into_feed() -> None:
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Type "/help<enter>" via the command box.
        for ch in "/help":
            await pilot.press(_key(ch))
        await pilot.press("enter")
        await pilot.pause()
        # Wait for the feedback envelope to land back on the operator queue.
        # The UI consumer drains it, so we look at the feed's written lines.
        feed = app._feed
        # RichLog stores lines; just confirm it has at least one line containing /dm.
        rendered = "\n".join(str(line) for line in feed.lines)
        assert "/dm" in rendered or "/help" in rendered


async def test_envelope_dispatch_updates_inspector() -> None:
    bus = InMemoryBus()
    app = ConnApp(bus, {}, store=None, room="ops")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Publish a chat from a synthetic agent — the consumer should ingest it.
        await bus.publish(chat("synth-1", "operator", "tick 1"))
        # Give the consumer a tick.
        for _ in range(20):
            if "synth-1" in app._infos:
                break
            await asyncio.sleep(0.02)
        assert "synth-1" in app._infos
        assert app._infos["synth-1"].chats >= 1


def _key(ch: str) -> str:
    """Translate ASCII char to a Textual key name where the literal differs."""
    mapping = {"/": "slash", " ": "space"}
    return mapping.get(ch, ch)
