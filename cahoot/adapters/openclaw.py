"""Adapter for OpenClaw (``openclaw acp`` subcommand).

OpenClaw exposes itself as an ACP server over stdio via the ``openclaw acp``
subcommand. It then forwards the work to its own Gateway over websocket,
so the operator-visible target is the *session*, not the model. Per the
docs, sessions are addressed via ``--session agent:<name>:<profile>``.

Config example::

    [[agents]]
    id = "openclaw-formatter-1"
    role = "formatter"
    kind = "openclaw"
    # Pin the gateway session
    gateway_url = "wss://gateway.openclaw.example"
    token_file = "~/.openclaw/main.token"
    session = "agent:formatter:main"

    [[agents]]
    id = "openclaw-formatter-2"
    role = "formatter"
    kind = "openclaw"
    token_file = "~/.openclaw/main.token"
    session = "agent:formatter:secondary"

Authentication: prefer ``token_file`` over an inline ``token`` so secrets
don't end up in the config or process listing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ._acp_base import ACPAdapter

__all__ = ["OpenClawAdapter"]


class OpenClawAdapter(ACPAdapter):
    """ACP adapter that spawns ``openclaw acp`` and routes via the Gateway."""

    LAUNCH_COMMAND: ClassVar[str] = "openclaw"
    LAUNCH_ARGS: ClassVar[tuple[str, ...]] = ("acp",)

    def __init__(
        self,
        *args: Any,
        gateway_url: str | None = None,
        token: str | None = None,
        token_file: str | Path | None = None,
        session: str | None = None,
        session_label: str | None = None,
        reset_session: bool = False,
        profile: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Only rebuild launch args if the caller didn't pass a custom override.
        if self._launch_args != OpenClawAdapter.LAUNCH_ARGS:
            return
        args_list: list[str] = []
        if profile:
            args_list.extend(["--profile", profile])
        args_list.append("acp")
        if gateway_url:
            args_list.extend(["--url", gateway_url])
        if token_file:
            args_list.extend(["--token-file", str(Path(token_file).expanduser())])
        elif token:
            args_list.extend(["--token", token])
        if session:
            args_list.extend(["--session", session])
        if session_label:
            args_list.extend(["--session-label", session_label])
        if reset_session:
            args_list.append("--reset-session")
        self._launch_args = tuple(args_list)
