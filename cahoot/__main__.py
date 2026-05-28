"""Cahoot CLI entry point.

Ties together the runtime infrastructure (logging, single-instance lock,
signal handlers), the bus, the SQLite event store, the configured
adapters, and either the Textual UI or a headless log monitor.

Run with::

    python -m cahoot                       # full UI
    cahoot --no-ui                         # headless (log only)
    cahoot -c path/to/cahoot.toml          # custom config
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
from .invites import InviteRegistry
from .listener import run_listener
from .runtime import (
    AlreadyRunning,
    db_path,
    install_signal_handlers,
    log_path,
    session_context,
    setup_logging,
    single_instance_lock,
    state_dir,
)
from .store import open_event_store

log = logging.getLogger("cahoot")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cahoot", description="Mission control for agent fleets.")
    p.add_argument("--config", "-c", type=Path, default=None, help="Path to cahoot.toml")
    p.add_argument("--no-ui", action="store_true", help="Run headless (log only, no Textual UI)")
    p.add_argument("--no-banner", action="store_true", help="Skip the startup splash banner")
    p.add_argument(
        "--no-store",
        action="store_true",
        help="Disable SQLite persistence (in-memory bus only)",
    )
    return p


async def _amain(no_ui: bool, no_store: bool, cfg_path: Path | None) -> None:
    cfg = load_config(cfg_path)
    setup_logging(level=getattr(logging, cfg.log_level.upper(), logging.INFO))
    log.info("cahoot starting; state_dir=%s log=%s", state_dir(), log_path())
    log.info("session: %s", session_context())

    bus = InMemoryBus()

    # Persistence — open the event store and wire it as a wiretap subscriber
    # so every envelope is durably appended in lockstep with delivery.
    store = None
    store_drain: asyncio.Task[None] | None = None
    if not no_store:
        store = await open_event_store(db_path())
        store_drain = await store.subscribe_to(bus, subscriber_id="_store")
        log.info("event store ready at %s (count=%d)", store.path, await store.count())

    # Instantiate adapters from config. Inject room + admission policy as
    # kwargs so adapters that care (ACP-based ones) can run the onboarding
    # handshake; others (synthetic) ignore them via **_ in their signature.
    adapters_list = []
    for spec in cfg.agents:
        factory = REGISTRY.get(spec.kind)
        if factory is None:
            log.error("unknown adapter kind %r for agent %r; skipping", spec.kind, spec.id)
            continue
        kwargs = dict(spec.options)
        kwargs.setdefault("room", cfg.room)
        kwargs.setdefault("admission_policy", cfg.admission)
        adapter = factory(
            agent_id=spec.id,
            role=spec.role,
            bus=bus,
            config=AdapterConfig(version=spec.version),
            **kwargs,
        )
        adapters_list.append(adapter)

    adapters = {a.agent_id: a for a in adapters_list}
    log.info("loaded %d adapter(s): %s", len(adapters), list(adapters))

    stop = asyncio.Event()
    install_signal_handlers(stop)

    adapter_tasks = [
        asyncio.create_task(a.run(), name=f"adapter.{a.agent_id}") for a in adapters_list
    ]

    # Network listener for inbound `cahoot-join` connections (Phase B).
    invites = InviteRegistry(ttl_s=cfg.listener.invite_ttl_s)
    remote_adapter_tasks: dict[str, asyncio.Task[None]] = {}
    listener_task: asyncio.Task[None] | None = None
    server_url: str | None = None
    if cfg.listener.enabled:
        # Surface a friendly URL the operator can paste alongside an invite.
        import socket as _socket

        host_hint = _socket.gethostname()
        server_url = f"ws://{host_hint}:{cfg.listener.port}"
        listener_task = asyncio.create_task(
            run_listener(
                bus=bus,
                invites=invites,
                adapters=adapters,
                adapter_tasks=remote_adapter_tasks,
                stop=stop,
                bind=cfg.listener.bind,
                port=cfg.listener.port,
                room=cfg.room,
            ),
            name="listener",
        )
        log.info("listener: announcing %s for invites", server_url)

    if no_ui:
        # Headless monitor: drain the operator queue to the log until stop.
        op_queue = bus.subscribe("operator")

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
        await stop.wait()
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
    else:
        # Lazy import so headless installs without textual still work.
        from .ui import ConnApp

        app = ConnApp(
            bus,
            adapters,
            store=store,
            room=cfg.room,
            stop_event=stop,
            invites=invites,
            server_url=server_url,
        )
        ui_task = asyncio.create_task(app.run_async(), name="ui")
        # Either signal handler or /quit-from-UI flips `stop`.
        await stop.wait()
        if not ui_task.done():
            app.exit(0)
            with __import__("contextlib").suppress(Exception):
                await asyncio.wait_for(ui_task, timeout=2.0)

    log.info("shutdown signalled; stopping adapters")

    for a in adapters_list:
        await a.stop()
    # Stop any remote (inbound) adapters too.
    for ra in list(adapters.values()):
        with __import__("contextlib").suppress(Exception):
            await ra.stop()
    await asyncio.gather(*adapter_tasks, return_exceptions=True)
    if remote_adapter_tasks:
        await asyncio.gather(*remote_adapter_tasks.values(), return_exceptions=True)
    if listener_task is not None:
        listener_task.cancel()
        await asyncio.gather(listener_task, return_exceptions=True)
    if store_drain is not None:
        store_drain.cancel()
        await asyncio.gather(store_drain, return_exceptions=True)
    if store is not None:
        await store.close()
    log.info("cahoot stopped cleanly")


def main() -> None:
    args = _build_argparser().parse_args()
    if not args.no_banner:
        print_banner()
    try:
        with single_instance_lock():
            asyncio.run(
                _amain(
                    no_ui=args.no_ui,
                    no_store=args.no_store,
                    cfg_path=args.config,
                )
            )
    except AlreadyRunning as exc:
        print(f"cahoot: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
