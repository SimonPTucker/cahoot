"""Tests for the runtime: single-instance lock, session probe."""

from __future__ import annotations

from pathlib import Path

import pytest

from cahoot.runtime import AlreadyRunning, session_context, single_instance_lock


def test_single_instance_lock_blocks_second_caller(tmp_path: Path) -> None:
    lock = tmp_path / "cahoot.lock"
    with single_instance_lock(lock), pytest.raises(AlreadyRunning), single_instance_lock(lock):
        pass


def test_session_context_keys_present() -> None:
    ctx = session_context()
    for key in ("hostname", "user", "tmux", "ssh_connection", "pid", "python"):
        assert key in ctx
        assert isinstance(ctx[key], str)
    assert ctx["pid"].isdigit()
