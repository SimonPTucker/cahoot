"""Adapter registry.

Adapters are registered explicitly, not via entry-point discovery. The
registry is a plain dict that any reader can scan to see exactly which
agent kinds are supported. To add a new adapter, write the module and add
a single line below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .synthetic import SyntheticAdapter

if TYPE_CHECKING:
    from ..adapter import AgentAdapter

__all__ = ["REGISTRY", "SyntheticAdapter"]


REGISTRY: dict[str, type[AgentAdapter]] = {
    "synthetic": SyntheticAdapter,
}
