"""Shared base for adapters whose agent speaks ACP over stdio.

ACP (`Agent Client Protocol`_) is the JSON-RPC-over-stdio protocol defined
by Zed Industries and natively supported by both Hermes Agent
(`NousResearch/hermes-agent`) and OpenClaw (`openclaw acp`). Cahoot is the
*client*; the agent is spawned as a subprocess and we drive it via the
official `agent-client-protocol` Python package.

This base handles:

* spawning the agent process (via :func:`acp.spawn_agent_process`),
* the protocol-version handshake (``initialize``),
* opening a single long-lived session per adapter,
* translating inbound ACP notifications → Cahoot envelopes,
* translating outbound Cahoot ``chat`` envelopes → ACP ``prompt`` requests,
* surfacing process / connection exits to the base ``AgentAdapter`` so the
  exponential-backoff-with-jitter reconnect machinery handles flaky
  agents the same way it handles any other transport.

The `acp` package is an **optional** dependency — install with
``pip install -e ".[acp]"`` or ``[dev]``. Importing this module without the
package raises a clear :class:`ACPDependencyError` at the first use, not at
import time, so users with only the SyntheticAdapter installed are
unaffected.

.. _`Agent Client Protocol`: https://github.com/zed-industries/agent-client-protocol
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from .. import __version__ as _CAHOOT_VERSION
from ..adapter import AdapterConfig, AgentAdapter
from ..admission import AdmissionPolicy
from ..admission import decide as admission_decide
from ..bus import Bus
from ..envelope import (
    ChatPayload,
    Envelope,
    ErrorPayload,
    MetricPayload,
    Severity,
    TaskPayload,
)
from ..onboarding import (
    EnrollmentState,
    build_instructions_prompt,
    build_quarantine_notice,
    build_welcome_prompt,
    extract_ack,
)

if TYPE_CHECKING:
    import acp as _acp_t  # noqa: F401 — only for type hints

__all__ = ["ACPAdapter", "ACPDependencyError"]

log = logging.getLogger(__name__)


class ACPDependencyError(RuntimeError):
    """Raised when the ``agent-client-protocol`` package is not installed."""


def _require_acp() -> Any:
    """Lazy import of :mod:`acp`. Raises a clear error if missing."""
    try:
        import acp
    except ImportError as exc:  # pragma: no cover — exercised by users without [acp] extra
        raise ACPDependencyError(
            "the `agent-client-protocol` package is required for ACP-based "
            'adapters. Install with: `pip install -e ".[acp]"`'
        ) from exc
    return acp


# Map ACP tool-call status strings → Cahoot TaskPayload state literal.
# ACP statuses: 'pending', 'in_progress', 'completed', 'failed'.
_TOOL_STATE_MAP: dict[str, Literal["queued", "running", "done", "failed"]] = {
    "pending": "queued",
    "in_progress": "running",
    "completed": "done",
    "failed": "failed",
}

PermissionPolicy = Literal["auto-allow", "deny"]


class ACPAdapter(AgentAdapter):
    """Generic Cahoot adapter for any ACP-speaking agent.

    Concrete adapters (Hermes, OpenClaw) override the class variables
    ``LAUNCH_COMMAND`` and ``LAUNCH_ARGS`` to point at their entry point,
    and may override :meth:`_default_env` to populate per-agent env vars.
    """

    LAUNCH_COMMAND: ClassVar[str] = ""
    LAUNCH_ARGS: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        agent_id: str,
        role: str,
        bus: Bus,
        config: AdapterConfig | None = None,
        *,
        launch_command: str | None = None,
        launch_args: tuple[str, ...] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        permission_policy: PermissionPolicy = "auto-allow",
        client_name: str = "cahoot",
        client_version: str | None = None,
        room: str = "ops",
        admission_policy: AdmissionPolicy | None = None,
        ack_timeout_s: float = 30.0,
        **_: Any,
    ) -> None:
        super().__init__(agent_id, role, bus, config)
        self._launch_command = launch_command or self.LAUNCH_COMMAND
        self._launch_args = tuple(launch_args) if launch_args is not None else self.LAUNCH_ARGS
        self._launch_env = self._merge_env(env)
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._permission_policy: PermissionPolicy = permission_policy
        self._client_name = client_name
        self._client_version = client_version or _CAHOOT_VERSION
        self._room = room
        self._admission_policy = admission_policy or AdmissionPolicy()
        self._ack_timeout_s = ack_timeout_s

        # Set by _open, cleared by _close.
        self._connection: Any | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._cm: Any | None = None

        # Onboarding state. Drives whether agent → operator routing is
        # the only route allowed (QUARANTINED) or full bus access (ADMITTED).
        self._enrollment: EnrollmentState = EnrollmentState.PENDING
        # Accumulates the agent's reply text during the welcome window so we
        # can scan for the ACK token.
        self._ack_buffer: list[str] = []
        self._ack_event = asyncio.Event()

    # ------------------------------------------------------------------
    # AgentAdapter contract
    # ------------------------------------------------------------------

    async def _open(self) -> None:
        acp = _require_acp()
        if not self._launch_command:
            raise ValueError(f"{type(self).__name__} requires LAUNCH_COMMAND or launch_command=")

        client = self._build_client(acp)

        self._cm = acp.spawn_agent_process(
            client,
            self._launch_command,
            *self._launch_args,
            env=self._launch_env,
            cwd=str(self._cwd),
        )
        # __aenter__ returns (ClientSideConnection, asyncio.subprocess.Process).
        self._connection, self._process = await self._cm.__aenter__()

        # Initialize handshake — protocol version + client metadata.
        init_req = acp.InitializeRequest(
            protocol_version=acp.PROTOCOL_VERSION,
            client_capabilities=None,
            client_info=acp.schema.Implementation(
                name=self._client_name,
                title="Cahoot mission control",
                version=self._client_version,
            ),
        )
        await self._connection.initialize(init_req)

        # Open a session so the operator can DM the agent.
        new_session_req = acp.NewSessionRequest(
            cwd=str(self._cwd),
            mcp_servers=[],
            additional_directories=None,
        )
        new_resp = await self._connection.new_session(new_session_req)
        self._session_id = new_resp.session_id
        log.info(
            "acp adapter %s connected; session=%s pid=%s",
            self.agent_id,
            self._session_id,
            getattr(self._process, "pid", "?"),
        )

        # Onboarding handshake — must complete before AgentAdapter declares
        # us CONNECTED. If it fails we raise; the base class catches and
        # reconnects with backoff.
        await self._run_onboarding()

    async def _run_onboarding(self) -> None:
        """Welcome + ACK wait + admission decision + instructions."""
        acp = _require_acp()
        assert self._connection is not None and self._session_id is not None

        # Reset enrollment state for this attempt.
        self._enrollment = EnrollmentState.AWAITING_ACK
        self._ack_buffer.clear()
        self._ack_event.clear()

        welcome_text = build_welcome_prompt(
            agent_id=self.agent_id,
            role=self.role,
            room=self._room,
        )
        await self._connection.prompt(
            acp.PromptRequest(
                session_id=self._session_id,
                prompt=[acp.text_block(welcome_text)],
                message_id=f"cahoot-welcome-{self.agent_id}",
            )
        )

        # Wait for an ACK token in the streamed reply.
        try:
            await asyncio.wait_for(self._ack_event.wait(), timeout=self._ack_timeout_s)
        except TimeoutError as exc:
            self._enrollment = EnrollmentState.REJECTED
            raise ConnectionResetError(
                f"agent {self.agent_id} did not ACK within {self._ack_timeout_s}s"
            ) from exc

        # Admission verdict.
        decision = admission_decide(self._admission_policy, self.agent_id)
        if decision.admitted:
            self._enrollment = EnrollmentState.ADMITTED
            follow_up = build_instructions_prompt(
                agent_id=self.agent_id,
                role=self.role,
                room=self._room,
            )
        else:
            self._enrollment = EnrollmentState.QUARANTINED
            follow_up = build_quarantine_notice(
                agent_id=self.agent_id,
                reason=decision.reason,
            )
            log.warning("acp adapter %s quarantined: %s", self.agent_id, decision.reason)

        await self._connection.prompt(
            acp.PromptRequest(
                session_id=self._session_id,
                prompt=[acp.text_block(follow_up)],
                message_id=f"cahoot-instructions-{self.agent_id}",
            )
        )

        # Expose enrollment state as a property so the UI / command box can
        # render it. See :meth:`enrollment` below.

        # Surface the enrollment outcome to the operator feed as a status
        # detail so the operator can see who's in vs quarantined.
        await self._publish(
            Envelope(
                source=self.agent_id,
                target="operator",
                payload=ChatPayload(text=f"[enrollment] {self.agent_id}: {self._enrollment}"),
            )
        )

    # ------------------------------------------------------------------
    # Runtime admission control — used by the /approve and /deny commands
    # ------------------------------------------------------------------

    @property
    def enrollment(self) -> EnrollmentState:
        """Public read-only view of the agent's enrollment state."""
        return self._enrollment

    async def admit(self, *, by: str = "operator") -> bool:
        """Move the agent into ``ADMITTED`` and notify it.

        Returns True if the state changed (was not already admitted).
        Idempotent and safe to call while disconnected — in that case it
        just records the new desired state so the next reconnect skips
        the quarantine path.
        """
        if self._enrollment is EnrollmentState.ADMITTED:
            return False
        self._enrollment = EnrollmentState.ADMITTED
        log.info("acp adapter %s admitted by %s", self.agent_id, by)
        await self._send_runtime_notice(
            f"✅ Admitted by {by}. You are now part of the fleet. "
            f"Standard @mention routing applies; see the participation "
            f"guide already sent."
        )
        await self._publish(
            Envelope(
                source=self.agent_id,
                target="operator",
                payload=ChatPayload(text=f"[admission] {self.agent_id}: admitted by {by}"),
            )
        )
        return True

    async def quarantine(self, *, by: str = "operator", reason: str | None = None) -> bool:
        """Move the agent into ``QUARANTINED`` and notify it.

        While quarantined, the adapter clamps outbound routing to
        ``operator`` and drops inbound from non-operator sources.
        """
        if self._enrollment is EnrollmentState.QUARANTINED:
            return False
        self._enrollment = EnrollmentState.QUARANTINED
        log.info("acp adapter %s quarantined by %s: %s", self.agent_id, by, reason)
        notice = build_quarantine_notice(agent_id=self.agent_id, reason=reason)
        await self._send_runtime_notice(notice)
        await self._publish(
            Envelope(
                source=self.agent_id,
                target="operator",
                payload=ChatPayload(
                    text=(
                        f"[admission] {self.agent_id}: quarantined by {by}"
                        f"{' — ' + reason if reason else ''}"
                    )
                ),
            )
        )
        return True

    async def _send_runtime_notice(self, text: str) -> None:
        """Send a short out-of-band prompt to the agent (admission changes)."""
        if self._connection is None or self._session_id is None:
            log.debug(
                "acp adapter %s: skipping runtime notice (not connected)",
                self.agent_id,
            )
            return
        acp = _require_acp()
        try:
            await self._connection.prompt(
                acp.PromptRequest(
                    session_id=self._session_id,
                    prompt=[acp.text_block(text)],
                    message_id=f"cahoot-runtime-{self.agent_id}",
                )
            )
        except Exception as exc:
            log.warning("acp adapter %s: runtime notice failed: %r", self.agent_id, exc)

    async def _close(self) -> None:
        # Drop refs first so concurrent writers don't race on a half-closed conn.
        self._connection = None
        self._session_id = None
        proc = self._process
        self._process = None
        cm = self._cm
        self._cm = None
        if cm is not None:
            with suppress(Exception):
                await cm.__aexit__(None, None, None)
        if proc is not None and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()

    async def _read_loop(self) -> None:
        """Wait for the agent process to exit.

        Inbound traffic is delivered by the ACP library invoking our
        :class:`acp.Client` callbacks (see :meth:`_build_client`); those
        callbacks call :meth:`_publish_from_agent`. The read loop here just
        watches for the subprocess to die or the connection to drop, which
        promotes us back into the reconnect path.
        """
        if self._process is None:
            raise RuntimeError("ACPAdapter._open did not initialise _process")
        rc = await self._process.wait()
        if rc != 0:
            raise ConnectionResetError(f"acp agent {self.agent_id} exited rc={rc}")

    async def _write(self, envelope: Envelope) -> None:
        """Translate outbound chat envelopes to an ACP ``prompt`` request."""
        if envelope.kind != "chat":
            log.debug(
                "acp adapter %s: ignoring outbound kind=%s",
                self.agent_id,
                envelope.kind,
            )
            return
        if self._connection is None or self._session_id is None:
            raise RuntimeError("acp adapter not connected")

        # Quarantined agents only receive operator messages — never peer
        # broadcast. Drop anything else with an operator-visible note.
        if self._enrollment is not EnrollmentState.ADMITTED and envelope.source != "operator":
            log.info(
                "acp adapter %s (not admitted) dropping inbound from %s",
                self.agent_id,
                envelope.source,
            )
            return

        acp = _require_acp()
        payload = envelope.payload
        assert isinstance(payload, ChatPayload)  # type-narrowing for mypy

        # Tell the agent who sent it so it can address its reply.
        prefixed = f"[{envelope.source}] {payload.text}"
        prompt_req = acp.PromptRequest(
            session_id=self._session_id,
            prompt=[acp.text_block(prefixed)],
            message_id=envelope.id,
        )
        await self._connection.prompt(prompt_req)

    # ------------------------------------------------------------------
    # Inbound translation
    # ------------------------------------------------------------------

    def _build_client(self, acp: Any) -> Any:
        """Construct an ``acp.Client`` that publishes onto our bus."""
        adapter = self

        class _CahootACPClient(acp.Client):  # type: ignore[misc]
            async def session_update(self, params: Any) -> None:
                await adapter._handle_session_update(params)

            async def request_permission(self, params: Any) -> Any:
                return await adapter._handle_permission(params)

            async def read_text_file(self, params: Any) -> Any:
                raise acp.RequestError.method_not_found(method="fs/read_text_file")

            async def write_text_file(self, params: Any) -> Any:
                raise acp.RequestError.method_not_found(method="fs/write_text_file")

            async def create_terminal(self, params: Any) -> Any:
                raise acp.RequestError.method_not_found(method="terminal/create")

            async def kill_terminal(self, params: Any) -> Any:
                raise acp.RequestError.method_not_found(method="terminal/kill")

            async def release_terminal(self, params: Any) -> Any:
                raise acp.RequestError.method_not_found(method="terminal/release")

            async def terminal_output(self, params: Any) -> Any:
                raise acp.RequestError.method_not_found(method="terminal/output")

            async def wait_for_terminal_exit(self, params: Any) -> Any:
                raise acp.RequestError.method_not_found(method="terminal/wait")

        return _CahootACPClient()

    async def _handle_session_update(self, params: Any) -> None:
        """Translate one ACP ``session/update`` notification to envelopes."""
        update = params.update
        # ACP wraps the update in a root model — peel to the inner type.
        inner = getattr(update, "root", update)
        kind = type(inner).__name__

        try:
            if kind == "AgentMessageChunk":
                await self._on_text_chunk(inner, "operator", from_thought=False)
            elif kind == "AgentThoughtChunk":
                await self._on_text_chunk(inner, "operator", from_thought=True)
            elif kind == "UserMessageChunk":
                # Echo of our own prompt content as confirmation; no-op for now.
                pass
            elif kind in {"ToolCallStart", "ToolCallProgress"}:
                await self._on_tool_call(inner)
            elif kind == "UsageUpdate":
                await self._on_usage(inner)
            elif kind == "SessionInfoUpdate":
                await self._on_session_info(inner)
            elif kind == "CurrentModeUpdate":
                log.debug(
                    "acp adapter %s: mode -> %s",
                    self.agent_id,
                    getattr(inner, "current_mode_id", "?"),
                )
            elif kind == "AvailableCommandsUpdate":
                log.debug(
                    "acp adapter %s: %d commands available",
                    self.agent_id,
                    len(getattr(inner, "available_commands", []) or []),
                )
            else:
                log.debug("acp adapter %s: unhandled update %s", self.agent_id, kind)
        except Exception as exc:
            log.warning(
                "acp adapter %s: translation failed for %s: %r",
                self.agent_id,
                kind,
                exc,
            )
            await self._publish(
                Envelope(
                    source=self.agent_id,
                    target="operator",
                    payload=ErrorPayload(
                        severity=Severity.WARN,
                        message=f"acp translate failed for {kind}: {exc!r}",
                    ),
                )
            )

    async def _handle_permission(self, params: Any) -> Any:
        """Decide whether the agent may run a requested tool call."""
        acp = _require_acp()
        tool = getattr(params, "tool_call", None)
        title = getattr(tool, "title", "(unknown tool)") if tool else "(unknown tool)"
        if self._permission_policy == "auto-allow":
            # Pick the first 'allow_*' option offered by the agent.
            options = getattr(params, "options", None) or []
            allowed = next(
                (
                    opt
                    for opt in options
                    if getattr(opt, "kind", "") in {"allow_once", "allow_always"}
                ),
                None,
            )
            option_id = getattr(allowed, "option_id", None) if allowed is not None else None
            if option_id is None:
                # Fall back to allow_always as the spec-canonical id.
                option_id = "allow_always"
            log.info(
                "acp adapter %s: auto-allowing tool call %s (option=%s)",
                self.agent_id,
                title,
                option_id,
            )
            return acp.RequestPermissionResponse(
                outcome=acp.schema.AllowedOutcome(option_id=option_id)
            )
        # deny
        log.info("acp adapter %s: denying tool call %s", self.agent_id, title)
        return acp.RequestPermissionResponse(outcome=acp.schema.DeniedOutcome())

    # ------------------------------------------------------------------
    # Per-update handlers
    # ------------------------------------------------------------------

    async def _on_text_chunk(self, update: Any, target: str, *, from_thought: bool) -> None:
        text = _extract_text(getattr(update, "content", None))
        if not text:
            return

        # During onboarding, accumulate the agent's reply and scan for the
        # ACK token. The text is still surfaced to the operator so they can
        # see the greeting verbatim.
        if self._enrollment is EnrollmentState.AWAITING_ACK and not from_thought:
            self._ack_buffer.append(text)
            if extract_ack("".join(self._ack_buffer)):
                self._ack_event.set()

        prefix = "💭 " if from_thought else ""
        resolved_target = self._route_target(text, default=target)
        await self._publish_from_agent(
            Envelope(
                source=self.agent_id,
                target=resolved_target,
                room=self._room,
                payload=ChatPayload(text=f"{prefix}{text}"),
            )
        )

    def _route_target(self, text: str, *, default: str) -> str:
        """Resolve @mention prefix to a Cahoot bus target.

        Quarantined agents are clamped to ``operator`` regardless of what
        they typed — the operator still sees a note that a non-operator
        target was requested.
        """
        target = _parse_mention(text) or default
        if self._enrollment is not EnrollmentState.ADMITTED and target != "operator":
            # Surface the attempt for the operator's benefit.
            log.info(
                "acp adapter %s (quarantined) suppressed target=%r → operator",
                self.agent_id,
                target,
            )
            return "operator"
        return target

    async def _on_tool_call(self, update: Any) -> None:
        status = getattr(update, "status", None) or "pending"
        state = _TOOL_STATE_MAP.get(str(status), "running")
        tool_call_id = getattr(update, "tool_call_id", "") or "unknown"
        title = getattr(update, "title", None)
        await self._publish_from_agent(
            Envelope(
                source=self.agent_id,
                target="operator",
                payload=TaskPayload(
                    task_id=str(tool_call_id),
                    state=state,
                    detail=title,
                ),
            )
        )

    async def _on_usage(self, update: Any) -> None:
        used = getattr(update, "used", None)
        if used is None:
            return
        # ACP's UsageUpdate is currently shaped as { used: int, size: int, cost: ... }.
        await self._publish_from_agent(
            Envelope(
                source=self.agent_id,
                target="operator",
                payload=MetricPayload(
                    name="tokens_used",
                    value=float(used),
                    unit="tokens",
                ),
            )
        )

    async def _on_session_info(self, update: Any) -> None:
        title = getattr(update, "title", None)
        if title:
            log.debug("acp adapter %s: session titled '%s'", self.agent_id, title)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _merge_env(self, override: dict[str, str] | None) -> dict[str, str]:
        env = dict(os.environ)
        if override:
            env.update(override)
        return env

    def _default_env(self) -> dict[str, str]:  # for subclasses to override
        return {}


_MENTION_RE = re.compile(r"^\s*@([A-Za-z0-9._:-]+)\b")


def _parse_mention(text: str) -> str | None:
    """Return the ``@<token>`` at the start of ``text``, or ``None``."""
    m = _MENTION_RE.match(text)
    return m.group(1) if m else None


def _extract_text(content: Any) -> str:
    """Pull plain text out of an ACP content block or list of blocks."""
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(_extract_text(c) for c in content)
    # Pydantic root-models expose the variant at `.root`.
    inner = getattr(content, "root", content)
    text = getattr(inner, "text", None)
    if isinstance(text, str):
        return text
    return ""
