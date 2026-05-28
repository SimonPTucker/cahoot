"""Adapter for the Hermes Agent (NousResearch/hermes-agent).

Hermes ships an ACP server entry point — the project lists itself in the
official ACP registry under
``uvx --from 'hermes-agent[acp]==<version>' hermes-acp``. We spawn that
subprocess and speak Agent Client Protocol (JSON-RPC over stdio) to it.

Auth: Hermes reads OAuth tokens / API keys from ``~/.hermes/.env`` and its
own config; Cahoot inherits the parent process environment so anything you
configured for the standalone ``hermes`` CLI Just Works. To override per
adapter, pass ``env = { ... }`` in the ``[[agents]]`` block.

Config example::

    [[agents]]
    id = "hermes-main"
    role = "orchestrator"
    kind = "hermes"
    version = "0.14.0"
    cwd = "~/work/project"
    # Optional: pin uv / uvx invocation
    # launch_command = "uvx"
    # launch_args = ["--from", "hermes-agent[acp]==0.14.0", "hermes-acp"]
"""

from __future__ import annotations

from typing import Any, ClassVar

from ._acp_base import ACPAdapter

__all__ = ["HermesAdapter"]


class HermesAdapter(ACPAdapter):
    """ACP adapter wired up to launch ``hermes-acp`` via ``uvx``."""

    LAUNCH_COMMAND: ClassVar[str] = "uvx"
    LAUNCH_ARGS: ClassVar[tuple[str, ...]] = (
        "--from",
        "hermes-agent[acp]",
        "hermes-acp",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # If the user pinned a version via [[agents]].version, rebuild the uvx
        # launch args to pin Hermes to that exact build. Only fires when the
        # user did not pass an explicit launch_args= override.
        if self.config.version and self._launch_args == HermesAdapter.LAUNCH_ARGS:
            self._launch_args = (
                "--from",
                f"hermes-agent[acp]=={self.config.version}",
                "hermes-acp",
            )
