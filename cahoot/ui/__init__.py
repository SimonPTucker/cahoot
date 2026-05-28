"""Textual UI for the Cahoot operator dashboard.

The :class:`ConnApp` (defined in :mod:`cahoot.ui.app`) is the entry point.
It composes four widgets — roster, feed, inspector, command box — and
drives them all from a single operator subscription on the bus, with
optional backfill from the :mod:`cahoot.store`.

The UI is intentionally small. Widgets read state from envelopes alone;
they never call back into adapters except through commands routed via
:mod:`cahoot.commands`. That keeps rendering decoupled from runtime, and
makes the UI swappable without touching adapter code.
"""

from __future__ import annotations

from .app import ConnApp

__all__ = ["ConnApp"]
