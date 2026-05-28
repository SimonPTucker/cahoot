"""mDNS / Bonjour discovery — advertise → browse roundtrip on loopback.

These tests exercise the real :mod:`zeroconf` stack rather than mocking
it, so we get end-to-end confidence in the discovery flow at the cost of
a couple of seconds per test. Service names include a unique suffix
per-test so concurrent test runs don't collide.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from cahoot.discovery import (
    SERVICE_TYPE,
    advertise,
    browse,
    current_short_hostname,
)

pytestmark = pytest.mark.asyncio


def _unique_name() -> str:
    """Per-test name suffix; pid + nanosecond noise keeps tests independent."""
    return f"test-{os.getpid()}-{os.urandom(2).hex()}"


async def test_advertise_then_browse_discovers_the_instance() -> None:
    name = _unique_name()
    async with advertise(port=12345, room="ops", name=name):
        # Give the system a moment to publish.
        await asyncio.sleep(0.3)
        found = await browse(timeout_s=2.0)
    matching = [i for i in found if i.name == name]
    assert matching, f"didn't find {name!r} among {[i.name for i in found]}"
    inst = matching[0]
    assert inst.port == 12345
    assert inst.room == "ops"
    assert inst.proto == "ws"
    assert inst.version == "1"
    assert inst.url.startswith("ws://") and inst.url.endswith(":12345")


async def test_browse_returns_empty_when_no_instances() -> None:
    # Nothing advertised by us; assume the host doesn't already run a Cahoot.
    found = await browse(timeout_s=1.0)
    # Filter out anything else on the network claiming the type.
    ours_only = [
        i for i in found if i.name.startswith("test-") or i.name == current_short_hostname()
    ]
    # We didn't advertise in this test, so our test-prefixed instances
    # should be zero.
    assert all(not i.name.startswith("test-") for i in ours_only)


async def test_service_type_constant_is_well_formed() -> None:
    assert SERVICE_TYPE.startswith("_cahoot._tcp.")
    assert SERVICE_TYPE.endswith(".local.")


async def test_two_advertisements_both_discoverable() -> None:
    name_a = _unique_name()
    name_b = _unique_name()
    async with (
        advertise(port=33301, room="ops", name=name_a),
        advertise(port=33302, room="ops", name=name_b),
    ):
        await asyncio.sleep(0.3)
        found = await browse(timeout_s=2.0)
    names = {i.name for i in found}
    assert name_a in names and name_b in names
    by_name = {i.name: i for i in found}
    assert by_name[name_a].port == 33301
    assert by_name[name_b].port == 33302
