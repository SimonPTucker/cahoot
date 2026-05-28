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
from cahoot.admission import AdmissionPolicy
from cahoot.bus import InMemoryBus
from cahoot.envelope import AgentState, ChatPayload, TaskPayload, chat
from cahoot.onboarding import EnrollmentState

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
    """Minimal stand-in for ``acp.ClientSideConnection``.

    To make adapter tests run end-to-end, the fake auto-replies to the
    Cahoot welcome prompt with a streamed ACK so the onboarding handshake
    completes. Set ``ack_response`` before ``run()`` to change what the
    fake "agent" streams back.
    """

    def __init__(self, client: Any, ack_response: str = "READY captain") -> None:
        self.client = client
        self.prompts: list[acp.PromptRequest] = []
        self.initialized = False
        self.session_id = "sess-1"
        self.ack_response = ack_response

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
        # Simulate the agent streaming a reply via session_update.
        # For the welcome prompt, stream the ACK so onboarding completes.
        if str(req.message_id or "").startswith("cahoot-welcome-") and self.ack_response:
            chunk = schema.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=acp.text_block(self.ack_response),
            )
            notif = acp.SessionNotification(session_id=self.session_id, update=chunk)
            await self.client.session_update(notif)
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
        # By the time CONNECTED is published, onboarding has sent welcome
        # + instructions.
        cm = patched_spawn["cm"]
        assert len(cm.connection.prompts) == 2
        assert str(cm.connection.prompts[0].message_id).startswith("cahoot-welcome-")
        assert str(cm.connection.prompts[1].message_id).startswith("cahoot-instructions-")
        assert adapter._enrollment is EnrollmentState.ADMITTED

        # Operator DMs the agent: bus → adapter inbox → _write → prompt #3.
        await bus.publish(chat("operator", "hermes-test", "review the release notes"))

        for _ in range(50):
            if len(cm.connection.prompts) >= 3:
                break
            await asyncio.sleep(0.02)
        assert len(cm.connection.prompts) >= 3
        op_prompt = cm.connection.prompts[2]
        assert op_prompt.session_id == "sess-1"
        # Operator's source is prefixed so the agent knows who to reply to.
        block = op_prompt.prompt[0]
        text = getattr(block.root if hasattr(block, "root") else block, "text", "")
        assert text.startswith("[operator]")
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_onboarding_quarantines_unlisted_agent_in_strict_mode(patched_spawn):
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    policy = AdmissionPolicy(mode="strict", allowed_ids=frozenset({"different-agent"}))
    adapter = _TestACPAdapter("rogue-1", "test", bus, admission_policy=policy)
    task = asyncio.create_task(adapter.run())
    try:
        await _wait_state(op, AgentState.CONNECTED)
        cm = patched_spawn["cm"]
        # Second prompt should be the quarantine notice, not instructions.
        assert len(cm.connection.prompts) == 2
        second = cm.connection.prompts[1]
        text = getattr(
            second.prompt[0].root if hasattr(second.prompt[0], "root") else second.prompt[0],
            "text",
            "",
        )
        assert "QUARANTINED" in text
        assert adapter._enrollment is EnrollmentState.QUARANTINED
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_no_ack_within_timeout_triggers_reconnect(patched_spawn, monkeypatch):
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    # Suppress the auto-ACK so the welcome prompt times out.
    adapter = _TestACPAdapter(
        "hermes-test",
        "orchestrator",
        bus,
        AdapterConfig(reconnect_initial_s=0.01, reconnect_max_s=0.05),
        ack_timeout_s=0.2,
    )
    # Patch the FakeConnection to NOT auto-ACK by reaching into the spawn fixture.
    original_init = _FakeConnection.__init__

    def init_no_ack(self, client, ack_response=""):
        original_init(self, client, ack_response=ack_response)

    monkeypatch.setattr(_FakeConnection, "__init__", init_no_ack)

    task = asyncio.create_task(adapter.run())
    try:
        # ACK timeout raises from _open → base class emits an ErrorPayload
        # describing the failed open and retries. We want to see that error.
        deadline = asyncio.get_event_loop().time() + 3.0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            assert remaining > 0, "never saw ack-timeout error envelope"
            env = await asyncio.wait_for(op.get(), timeout=remaining)
            if env.kind == "error" and "ACK" in env.payload.message.upper():
                break
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_at_mention_routes_target(patched_spawn):
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    bus.subscribe("hermes-test")  # the adapter's own inbox
    other = bus.subscribe("openclaw-1")  # a peer
    adapter = _TestACPAdapter("hermes-test", "orchestrator", bus)
    task = asyncio.create_task(adapter.run())
    try:
        await _wait_state(op, AgentState.CONNECTED)
        cm = patched_spawn["cm"]
        # Simulate the agent producing a chat chunk addressed @openclaw-1.
        chunk = schema.AgentMessageChunk(
            session_update="agent_message_chunk",
            content=acp.text_block("@openclaw-1 please format this report"),
        )
        await cm.connection.client.session_update(
            acp.SessionNotification(session_id="sess-1", update=chunk)
        )
        # Operator sees the chat (everyone does) AND openclaw-1 receives it.
        env_other = await asyncio.wait_for(other.get(), timeout=1.0)
        assert env_other.target == "openclaw-1"
        assert "@openclaw-1" in env_other.payload.text
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
