"""TOML configuration loader.

Lookup order (first hit wins):

1. ``$CAHOOT_CONFIG`` — explicit override, intended for ad-hoc / dev use.
2. ``$XDG_CONFIG_HOME/cahoot/cahoot.toml`` (default ``~/.config/cahoot/cahoot.toml``).
3. ``./cahoot.toml`` in the current working directory, useful when running
   straight out of a checkout.

The file is parsed with the stdlib :mod:`tomllib` (Python 3.11+, read-only),
so no third-party TOML dependency is needed.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .admission import AdmissionMode, AdmissionPolicy
from .runtime import config_path as default_config_path

__all__ = [
    "AgentSpec",
    "CahootConfig",
    "ConfigError",
    "ListenerConfig",
    "find_config",
    "load_config",
]


@dataclass(frozen=True)
class ListenerConfig:
    """Optional WebSocket listener for inbound `cahoot-join` connections."""

    enabled: bool = False
    bind: str = "0.0.0.0"
    port: int = 9876
    invite_ttl_s: int = 30 * 60


class ConfigError(ValueError):
    """Raised when the config file is missing fields or malformed."""


@dataclass(frozen=True)
class AgentSpec:
    """One ``[[agents]]`` block."""

    id: str
    role: str
    kind: str
    version: str | None = None
    # Anything beyond the four fields above is forwarded to the adapter
    # constructor as keyword arguments.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CahootConfig:
    """Parsed contents of ``cahoot.toml``."""

    room: str = "ops"
    log_level: str = "INFO"
    agents: tuple[AgentSpec, ...] = ()
    admission: AdmissionPolicy = field(default_factory=AdmissionPolicy)
    listener: ListenerConfig = field(default_factory=ListenerConfig)
    source_path: Path | None = None


_RESERVED_AGENT_KEYS = frozenset({"id", "role", "kind", "version"})


def find_config(explicit: Path | None = None) -> Path | None:
    """Resolve a config path per the documented lookup order, or ``None``."""
    if explicit is not None:
        return explicit
    env = os.environ.get("CAHOOT_CONFIG")
    if env:
        return Path(env).expanduser()
    xdg = default_config_path()
    if xdg.is_file():
        return xdg
    cwd = Path.cwd() / "cahoot.toml"
    if cwd.is_file():
        return cwd
    return None


def load_config(explicit: Path | None = None) -> CahootConfig:
    """Load and validate a Cahoot config.

    If no config file is found, returns a default :class:`CahootConfig` with
    no agents — the runtime will start, log a warning, and idle.
    """
    path = find_config(explicit)
    if path is None:
        return CahootConfig()
    if not path.is_file():
        raise ConfigError(f"config not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return _parse(raw, source=path)


def _parse(raw: dict[str, Any], *, source: Path) -> CahootConfig:
    section = raw.get("cahoot", {})
    if not isinstance(section, dict):
        raise ConfigError(f"[cahoot] must be a table, got {type(section).__name__}")
    room = section.get("room", "ops")
    log_level = section.get("log_level", "INFO")
    if not isinstance(room, str) or not isinstance(log_level, str):
        raise ConfigError("[cahoot] room and log_level must be strings")

    agent_blocks = raw.get("agents", [])
    if not isinstance(agent_blocks, list):
        raise ConfigError("[[agents]] must be an array of tables")

    agents: list[AgentSpec] = []
    for i, block in enumerate(agent_blocks):
        if not isinstance(block, dict):
            raise ConfigError(f"[[agents]][{i}] must be a table")
        for field_name in ("id", "role", "kind"):
            if field_name not in block:
                raise ConfigError(f"[[agents]][{i}] is missing required field {field_name!r}")
            if not isinstance(block[field_name], str):
                raise ConfigError(f"[[agents]][{i}].{field_name} must be a string")
        version = block.get("version")
        if version is not None and not isinstance(version, str):
            raise ConfigError(f"[[agents]][{i}].version must be a string")
        options = {k: v for k, v in block.items() if k not in _RESERVED_AGENT_KEYS}
        agents.append(
            AgentSpec(
                id=block["id"],
                role=block["role"],
                kind=block["kind"],
                version=version,
                options=options,
            )
        )

    admission = _parse_admission(raw.get("cahoot", {}).get("admission", {}), agents)
    listener = _parse_listener(raw.get("cahoot", {}).get("listener", {}))

    return CahootConfig(
        room=room,
        log_level=log_level,
        agents=tuple(agents),
        admission=admission,
        listener=listener,
        source_path=source,
    )


def _parse_listener(section: Any) -> ListenerConfig:
    if not section:
        return ListenerConfig()
    if not isinstance(section, dict):
        raise ConfigError("[cahoot.listener] must be a table")
    enabled = bool(section.get("enabled", False))
    bind = section.get("bind", "0.0.0.0")
    port = section.get("port", 9876)
    ttl = section.get("invite_ttl_s", 30 * 60)
    if not isinstance(bind, str):
        raise ConfigError("[cahoot.listener].bind must be a string")
    if not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError("[cahoot.listener].port must be a port number")
    if not isinstance(ttl, int) or ttl <= 0:
        raise ConfigError("[cahoot.listener].invite_ttl_s must be a positive int")
    return ListenerConfig(enabled=enabled, bind=bind, port=port, invite_ttl_s=ttl)


def _parse_admission(section: Any, agents: list[AgentSpec]) -> AdmissionPolicy:
    if not section:
        return AdmissionPolicy()
    if not isinstance(section, dict):
        raise ConfigError("[cahoot.admission] must be a table")
    mode = section.get("mode", "open")
    if mode not in {"open", "strict"}:
        raise ConfigError(f"[cahoot.admission].mode must be 'open' or 'strict', got {mode!r}")
    allowed = section.get("allowed_ids", [])
    if not isinstance(allowed, list) or not all(isinstance(s, str) for s in allowed):
        raise ConfigError("[cahoot.admission].allowed_ids must be a list of strings")
    # In strict mode, also implicitly trust every agent listed in [[agents]] —
    # they're already in the config so the operator clearly intended them.
    if mode == "strict":
        allowed = list({*allowed, *(a.id for a in agents)})
    mode_typed: AdmissionMode = mode  # narrow Literal
    return AdmissionPolicy(mode=mode_typed, allowed_ids=frozenset(allowed))
