"""Process-level concerns that exist because Cahoot lives in a tmux session.

* XDG-compliant state and config paths.
* Single-instance enforcement via ``fcntl.flock`` advisory lock.
* Rotating log file (Textual owns the TTY; logs must go to a file).
* Signal handlers (``SIGINT``/``SIGTERM``/``SIGHUP``) that translate to a
  clean ``asyncio.Event`` so the main loop can shut down adapters in order.
* ``session_context()`` probe powering ``/whoami`` and ``/where``.

Everything here is deliberately synchronous (paths, locks, signal hooks)
except where it has to integrate with the running event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import socket
import sys
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = [
    "AlreadyRunning",
    "config_dir",
    "config_path",
    "install_signal_handlers",
    "log_path",
    "session_context",
    "setup_logging",
    "single_instance_lock",
    "state_dir",
]


# ---------------------------------------------------------------------------
# XDG paths
# ---------------------------------------------------------------------------


def _xdg_home(env_var: str, default: str) -> Path:
    """Return ``$env_var`` if set, else ``$HOME/<default>``."""
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / default


def state_dir() -> Path:
    """``$XDG_STATE_HOME/cahoot`` (default ``~/.local/state/cahoot``)."""
    d = _xdg_home("XDG_STATE_HOME", ".local/state") / "cahoot"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/cahoot`` (default ``~/.config/cahoot``)."""
    return _xdg_home("XDG_CONFIG_HOME", ".config") / "cahoot"


def log_path() -> Path:
    return state_dir() / "cahoot.log"


def lock_path() -> Path:
    return state_dir() / "cahoot.lock"


def db_path() -> Path:
    return state_dir() / "cahoot.db"


def config_path() -> Path:
    """Default config path; the CLI may override via ``$CAHOOT_CONFIG``."""
    return config_dir() / "cahoot.toml"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(
    level: int = logging.INFO,
    *,
    path: Path | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 3,
) -> None:
    """Install a rotating file handler on the root logger.

    Safe to call multiple times — replaces any existing handler we added.
    """
    target = path or log_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # Remove any previously installed Cahoot handler to keep idempotent.
    for h in list(root.handlers):
        if getattr(h, "_cahoot", False):
            root.removeHandler(h)

    handler = RotatingFileHandler(target, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler._cahoot = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


class AlreadyRunning(RuntimeError):
    """Raised when another Cahoot process holds the single-instance lock."""


@contextlib.contextmanager
def single_instance_lock(path: Path | None = None) -> Iterator[Path]:
    """Acquire an exclusive ``flock`` on the lock file for the duration of the context.

    Uses ``fcntl.flock(LOCK_EX | LOCK_NB)`` so a second process gets
    :class:`AlreadyRunning` immediately rather than hanging.

    The lock is tied to the file descriptor: if the process exits the OS
    releases it, so crashes don't leave a stale lock.
    """
    target = path or lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Open for writing so we can stamp our PID for human debugging.
    fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AlreadyRunning(
                "another Cahoot process is running; "
                "`tmux attach -t cahoot` to join it, or kill it first"
            ) from exc
        # Stamp our PID (informational only).
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield target
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def install_signal_handlers(stop: asyncio.Event) -> None:
    """Wire ``SIGINT``/``SIGTERM``/``SIGHUP`` to set ``stop``.

    Uses ``loop.add_signal_handler`` where supported (POSIX) so handlers run
    on the event loop. ``SIGWINCH`` is intentionally untouched so Textual's
    own resize plumbing keeps working.
    """
    loop = asyncio.get_running_loop()

    def _request_stop(sig: signal.Signals) -> None:
        if not stop.is_set():
            logging.getLogger("cahoot").info("received %s; requesting clean shutdown", sig.name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _request_stop, sig)


# ---------------------------------------------------------------------------
# Session probe
# ---------------------------------------------------------------------------


def session_context() -> dict[str, str]:
    """Return a snapshot of the runtime context — used by ``/whoami`` and ``/where``.

    All values are strings; missing fields render as empty string rather
    than ``None`` so the UI can format them without conditionals.
    """
    return {
        "hostname": socket.gethostname(),
        "user": os.environ.get("USER", ""),
        "tmux": os.environ.get("TMUX", ""),
        "ssh_connection": os.environ.get("SSH_CONNECTION", ""),
        "pid": str(os.getpid()),
        "python": sys.version.split()[0],
    }
