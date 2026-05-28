"""Cahoot CLI entry point.

This minimal entry point ties together the runtime infrastructure (logging,
single-instance lock, signal handlers) with the bus and configured adapters.

It currently prints envelope traffic to the log file. The Textual UI lives
in ``cahoot/ui/`` and will replace the print-loop in the build phase 4
(see CLAUDE.md).

Run with::

    python -m cahoot

or, after install::

    cahoot
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .adapter import AdapterConfig
from .adapters import REGISTRY
from .banner import print_banner
from .bus import InMemoryBus
from .config import load_config
from .runtime import (
    AlreadyRunning,
    install_signal_handlers,
    log_path,
    session_context,
    setup_logging,
    single_instance_lock,
    state_dir,
)

log = logging.getLogger("cahoot")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cahoot", description="Mission control for agent fleets.")
    p.add_argument("--config", "-c", type=Path, default=None, help="Path to cahoot.toml")
    p.add_argument("--no-ui", action="store_true", help="Run headless (log only, no Textual UI)")
    p.add_argument("--no-banner", action="store_true", help="Skip the startup splash banner")
    return p


async def _amain(no_ui: bool, cfg_path: Path | None) -> None:
    cfg = load_config(cfg_path)
    setup_logging(level=getattr(logging, cfg.log_level.upper(), logging.INFO))
    log.info("cahoot starting; state_dir=%s log=%s", state_dir(), log_path())
    log.info("session: %s", session_context())

    bus = InMemoryBus()
    op_queue = bus.subscribe("operator")

    # Instantiate adapters from config.
    adapters = []
    for spec in cfg.agents:
        factory = REGISTRY.get(spec.kind)
        if factory is None:
            log.error("unknown adapter kind %r for agent %r; skipping", spec.kind, spec.id)
            continue
        adapter = factory(
            agent_id=spec.id,
            role=spec.role,
            bus=bus,
            config=AdapterConfig(version=spec.version),
            **spec.options,
        )
        adapters.append(adapter)

    log.info("loaded %d adapter(s): %s", len(adapters), [a.agent_id for a in adapters])

    stop = asyncio.Event()
    install_signal_handlers(stop)

    adapter_tasks = [asyncio.create_task(a.run(), name=f"adapter.{a.agent_id}") for a in adapters]

    # Headless monitor: drain the operator queue to the log until stop.
    async def _monitor() -> None:
        while not stop.is_set():
            try:
                env = await asyncio.wait_for(op_queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            log.info(
                "[%s] %s → %s: %s",
                env.kind,
                env.source,
                env.target,
                getattr(env.payload, "text", env.payload.model_dump(exclude={"kind"})),
            )

    monitor_task = asyncio.create_task(_monitor(), name="monitor")

    # NOTE: when the Textual UI is built (phase 4), replace the monitor
    # task with `await ui.run(bus, stop)`.
    if not no_ui:
        log.warning("UI not yet implemented; running headless. See CLAUDE.md phase 4.")

    await stop.wait()
    log.info("shutdown signalled; stopping adapters")

    for a in adapters:
        await a.stop()
    monitor_task.cancel()
    await asyncio.gather(*adapter_tasks, return_exceptions=True)
    await asyncio.gather(monitor_task, return_exceptions=True)
    log.info("cahoot stopped cleanly")


def main() -> None:
    args = _build_argparser().parse_args()
    if not args.no_banner:
        print_banner()
    try:
        with single_instance_lock():
            asyncio.run(_amain(no_ui=args.no_ui, cfg_path=args.config))
    except AlreadyRunning as exc:
        print(f"cahoot: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
