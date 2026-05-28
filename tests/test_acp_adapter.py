"""Tests for the ACP adapter base — verify envelope translation in isolation.

We don't spawn the real ``uvx hermes-acp`` / ``openclaw acp`` subprocesses
here. Instead we monkey-patch :func:`acp.spawn_agent_process` to yield a
fake (connection, process) pair, drive the adapter through the same lifecycle
the real agent would (initialize → new_session → session_update notifications),
and assert the right envelopes land on the operator subscriber.
"""

from __future__ import annotations

import asyncio
from typing import Any

import acp
import acp.schema as schema
import pytest

from cahoot.adapter import AdapterConfig
from cahoot.adapters._acp_base import ACPAdapter
from cahoot.bus import InMemoryBus
from cahoot.envelope import AgentState, ChatPayload, TaskPayload

pytestmark = pytest.mark.asyncio


class _FakeProcess:
    """Mimics :class:`asyncio.subprocess.Process` just enough."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self._exit = asyncio.Event()
        self.pid = 99999

    async def wait(self) -> int:
        await self._exit.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self.returncode = 0
        self._exit.set()

    def kill(self) -> None:
        self.terminate()


class _FakeConnection:
    """Minimal stand-in for ``acp.ClientSideConnection``."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.prompts: list[acp.PromptRequest] = []
        self.initialized = False
        self.session_id = "sess-1"

    async def initialize(self, req: acp.InitializeRequest) -> acp.InitializeResponse:
        self.initialized = True
        return acp.InitializeResponse(
            agent_capabilities=schema.AgentCapabilities(),
            protocol_version=acp.PROTOCOL_VERSION,
        )

    async def new_session(self, req: acp.NewSessionRequest) -> acp.NewSessionResponse:
        return acp.NewSessionResponse(session_id=self.session_id)

    async def prompt(self, req: acp.PromptRequest) -> acp.PromptResponse:
        self.prompts.append(req)
        return acp.PromptResponse(stop_reason="end_turn")


class _FakeSpawnCM:
    """Async-context-manager mock for :func:`acp.spawn_agent_process`."""

    def __init__(self, client: Any) -> None:
        self.connection = _FakeConnection(client)
        self.process = _FakeProcess()

    async def __aenter__(self) -> tuple[_FakeConnection, _FakeProcess]:
        return self.connection, self.process

    async def __aexit__(self, *exc: Any) -> None:
        self.process.terminate()


@pytest.fixture
def patched_spawn(monkeypatch: pytest.MonkeyPatch):
    """Replace :func:`acp.spawn_agent_process` with our context-manager mock."""
    held: dict[str, _FakeSpawnCM] = {}

    def fake_spawn(client, command, *args, env=None, cwd=None, **_):
        cm = _FakeSpawnCM(client)
        held["cm"] = cm
        return cm

    monkeypatch.setattr(acp, "spawn_agent_process", fake_spawn)
    return held


class _TestACPAdapter(ACPAdapter):
    LAUNCH_COMMAND = "true"  # never actually invoked thanks to patched_spawn
    LAUNCH_ARGS = ()


async def _wait_state(op: asyncio.Queue, state: AgentState, budget_s: float = 2.0):
    deadline = asyncio.get_event_loop().time() + budget_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        assert remaining > 0, f"timeout waiting for {state}"
        env = await asyncio.wait_for(op.get(), timeout=remaining)
        if env.kind == "status" and env.payload.state is state:
            return env


async def _wait_chat(op: asyncio.Queue, predicate, budget_s: float = 2.0):
    deadline = asyncio.get_event_loop().time() + budget_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        assert remaining > 0, "timeout waiting for chat"
        env = await asyncio.wait_for(op.get(), timeout=remaining)
        if env.kind == "chat" and predicate(env):
            return env


async def test_acp_adapter_reaches_connected_and_sends_prompt(patched_spawn):
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    adapter = _TestACPAdapter(
        "hermes-test",
        "orchestrator",
        bus,
        AdapterConfig(heartbeat_interval_s=5.0),
    )
    task = asyncio.create_task(adapter.run())
    try:
        await _wait_state(op, AgentState.CONNECTED)

        # Operator DMs the agent: bus → adapter inbox → _write → prompt.
        from cahoot.envelope import chat

        await bus.publish(chat("operator", "hermes-test", "review the release notes"))

        # Wait for the prompt to land in the fake connection.
        cm = patched_spawn["cm"]
        for _ in range(50):
            if cm.connection.prompts:
                break
            await asyncio.sleep(0.02)
        assert cm.connection.prompts, "prompt never reached fake connection"
        prompt = cm.connection.prompts[0]
        assert prompt.session_id == "sess-1"
        # Prompt content blocks: list of TextContentBlock-like objects.
        assert len(prompt.prompt) == 1
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_session_update_chunks_become_chat_envelopes(patched_spawn):
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    adapter = _TestACPAdapter("hermes-test", "orchestrator", bus)
    task = asyncio.create_task(adapter.run())
    try:
        await _wait_state(op, AgentState.CONNECTED)
        cm = patched_spawn["cm"]

        # Simulate the agent streaming a reply.
        chunk = schema.AgentMessageChunk(
            session_update="agent_message_chunk",
            content=acp.text_block("hello operator"),
        )
        notif = acp.SessionNotification(session_id="sess-1", update=chunk)
        await cm.connection.client.session_update(notif)

        env = await _wait_chat(op, lambda e: "hello operator" in e.payload.text)
        assert env.source == "hermes-test"
        assert env.target == "operator"
        assert isinstance(env.payload, ChatPayload)
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_tool_call_update_becomes_task_envelope(patched_spawn):
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    adapter = _TestACPAdapter("hermes-test", "orchestrator", bus)
    task = asyncio.create_task(adapter.run())
    try:
        await _wait_state(op, AgentState.CONNECTED)
        cm = patched_spawn["cm"]

        tc_update = acp.update_tool_call(
            "t-1",
            title="read file",
            status="in_progress",
        )
        notif = acp.SessionNotification(session_id="sess-1", update=tc_update)
        await cm.connection.client.session_update(notif)

        deadline = asyncio.get_event_loop().time() + 2.0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            assert remaining > 0
            env = await asyncio.wait_for(op.get(), timeout=remaining)
            if env.kind == "task":
                assert isinstance(env.payload, TaskPayload)
                assert env.payload.task_id == "t-1"
                assert env.payload.state == "running"
                break
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)
