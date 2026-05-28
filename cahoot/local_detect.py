"""Auto-detect agent runtimes installed on this machine.

When ``cahoot-join`` is invoked without ``--kind``, the bridge calls into
this module to figure out which runtime to drive. Three probes:

* **Hermes** — present iff ``uvx`` is on ``PATH`` (Hermes is fetched on
  demand by uvx; we don't need it pre-installed). Tries ``uvx --version``
  for a best-effort version string.
* **OpenClaw** — present iff the ``openclaw`` CLI is on ``PATH``.
  Reports its version and offers ``~/.openclaw/main.token`` as a default
  ``token_file`` if the file exists.
* **Synthetic** — always present (it's part of this package). Reported
  but only chosen when explicitly requested.

Resolution rules (:func:`pick_default`):

* Exactly one real runtime available → return it.
* Multiple → raise so the caller forces the user to pass ``--kind``.
* None → raise with install hints.

The module is pure (no network, no async). Detection takes ≤ 6 seconds
in the worst case (two subprocess version probes with 3 s timeouts) and
is safe to call from any synchronous context.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "RUNTIME_KINDS",
    "RuntimeKind",
    "RuntimeProbe",
    "RuntimeUnavailableError",
    "detect_all",
    "detect_hermes",
    "detect_openclaw",
    "detect_synthetic",
    "format_report",
    "pick_default",
]

RuntimeKind = Literal["hermes", "openclaw", "synthetic"]
RUNTIME_KINDS: tuple[RuntimeKind, ...] = ("hermes", "openclaw", "synthetic")

# Real (non-synthetic) runtimes — the ones we'll auto-pick when only one
# is present. Synthetic is a test agent and never gets auto-picked.
_REAL_RUNTIMES: frozenset[RuntimeKind] = frozenset({"hermes", "openclaw"})


@dataclass(frozen=True)
class RuntimeProbe:
    """One detection result.

    ``available`` is ``True`` when the runtime can be driven from this
    machine — for Hermes that's "uvx is on PATH" (Hermes itself is
    fetched on demand); for OpenClaw it's "openclaw CLI is on PATH";
    for synthetic it's always ``True``.
    """

    kind: RuntimeKind
    available: bool
    version: str | None = None
    notes: tuple[str, ...] = ()
    suggested_kwargs: dict[str, str] = field(default_factory=dict)
    """Adapter constructor kwargs we can fill in for free — e.g. the
    detected ``~/.openclaw/main.token`` becomes ``token_file=…``."""


class RuntimeUnavailableError(RuntimeError):
    """Raised by :func:`pick_default` when no real runtime is available
    or when more than one is and the caller must disambiguate."""


# ---------------------------------------------------------------------------
# Per-runtime probes
# ---------------------------------------------------------------------------


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _quick_version(cmd: str, *args: str, timeout_s: float = 3.0) -> str | None:
    """Best-effort `<cmd> <args>` → first line of stdout. Returns ``None``
    on any failure so detection is robust against weird local installs."""
    try:
        result = subprocess.run(
            [cmd, *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (result.stdout or result.stderr or "").strip().splitlines()
    return out[0] if out else None


def detect_hermes() -> RuntimeProbe:
    uvx = _which("uvx")
    if uvx is None:
        return RuntimeProbe(
            kind="hermes",
            available=False,
            notes=(
                "`uvx` not on PATH; required to launch Hermes.",
                "install via:  curl -LsSf https://astral.sh/uv/install.sh | sh",
            ),
        )
    uvx_version = _quick_version(uvx, "--version")
    return RuntimeProbe(
        kind="hermes",
        available=True,
        version=uvx_version,
        notes=(
            "uvx detected — Hermes will be fetched on first launch via "
            "`uvx --from 'hermes-agent[acp]' hermes-acp`.",
        ),
    )


def detect_openclaw() -> RuntimeProbe:
    openclaw = _which("openclaw")
    if openclaw is None:
        return RuntimeProbe(
            kind="openclaw",
            available=False,
            notes=(
                "`openclaw` CLI not on PATH.",
                "install via:  brew install openclaw  (or your distribution's path)",
                "then run:     openclaw onboard",
            ),
        )
    version = _quick_version(openclaw, "--version")
    notes: list[str] = ["openclaw CLI detected"]
    suggested: dict[str, str] = {}
    default_token = Path("~/.openclaw/main.token").expanduser()
    if default_token.is_file():
        suggested["token_file"] = str(default_token)
        notes.append(f"default token file detected: {default_token}")
    else:
        notes.append(
            "no ~/.openclaw/main.token found — pass --token-file or set up "
            "OpenClaw with `openclaw onboard` first."
        )
    return RuntimeProbe(
        kind="openclaw",
        available=True,
        version=version,
        notes=tuple(notes),
        suggested_kwargs=suggested,
    )


def detect_synthetic() -> RuntimeProbe:
    return RuntimeProbe(
        kind="synthetic",
        available=True,
        notes=(
            "built-in test agent — no external runtime required.",
            "useful for smoke-testing the network onboarding path.",
        ),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def detect_all() -> list[RuntimeProbe]:
    """Run every probe and return the results, in display order."""
    return [detect_hermes(), detect_openclaw(), detect_synthetic()]


def pick_default(probes: list[RuntimeProbe]) -> RuntimeProbe:
    """Choose the single available real runtime, or raise.

    Synthetic is intentionally never auto-picked — it's a test agent.
    """
    real_available = [p for p in probes if p.kind in _REAL_RUNTIMES and p.available]
    if not real_available:
        hints: list[str] = []
        for probe in probes:
            if probe.kind in _REAL_RUNTIMES and not probe.available:
                hints.extend(f"  - {note}" for note in probe.notes)
        joined = "\n" + "\n".join(hints) if hints else ""
        raise RuntimeUnavailableError(
            "no real agent runtime detected on this machine (Hermes or "
            "OpenClaw).\n"
            f"install one of them and re-run, or pass `--kind synthetic` to "
            f"use the built-in test agent.{joined}"
        )
    if len(real_available) > 1:
        kinds = ", ".join(p.kind for p in real_available)
        raise RuntimeUnavailableError(
            f"multiple runtimes detected on this machine ({kinds}). "
            f"Pass `--kind <name>` to choose which one this seat will drive."
        )
    return real_available[0]


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------


def format_report(probes: list[RuntimeProbe]) -> str:
    """Format a human-readable summary, for `cahoot-join --detect`."""
    lines = ["agent runtimes detected on this machine:", ""]
    for p in probes:
        mark = "✓" if p.available else "✗"
        version_bit = f"  {p.version}" if p.version else ""
        lines.append(f"  {mark} {p.kind}{version_bit}")
        for note in p.notes:
            lines.append(f"      {note}")
        if p.suggested_kwargs:
            for k, v in p.suggested_kwargs.items():
                lines.append(f"      default: --{k.replace('_', '-')} {v}")
        lines.append("")

    # Tail hint.
    real = [p for p in probes if p.kind in _REAL_RUNTIMES and p.available]
    if len(real) == 1:
        only = real[0]
        lines.append(
            f"tip: run `cahoot-join --token <T> --as <id> --role <role>` "
            f"and cahoot-join will auto-pick `--kind {only.kind}`."
        )
    elif len(real) > 1:
        lines.append(
            "tip: multiple real runtimes available — pass `--kind <name>` to disambiguate."
        )
    else:
        lines.append(
            "tip: install Hermes or OpenClaw (see hints above), or use "
            "`--kind synthetic` to smoke-test the network onboarding path."
        )
    return "\n".join(lines)
