"""mDNS / Bonjour service discovery for Cahoot instances on the LAN.

When the listener starts, Cahoot can optionally advertise itself as
``_cahoot._tcp.local.`` so the ``cahoot-join`` bridge can find it
without the user having to type the hostname. The bridge takes
``--server auto`` (or just omits ``--server``) and Cahoot tells it
where it lives.

Wire details:

* Service type: ``_cahoot._tcp.local.``
* Service name: ``<short-hostname>._cahoot._tcp.local.``
* Port: whatever the listener is bound to (e.g. 9876)
* TXT record: ``version`` (protocol), ``room``, ``host`` (real
  hostname; useful when the short name conflicts), and ``proto``
  (currently always ``"ws"`` — v1.5 will add ``"wss"``).

This module is **only imported when the user has the ``[network]``
extra installed**, and even then it's lazy-loaded so a Cahoot install
without ``zeroconf`` can still run the listener (it simply won't
advertise). The bridge is the same way — without ``zeroconf`` the
``--server auto`` flag errors clearly.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_BROWSE_TIMEOUT_S",
    "DiscoveredInstance",
    "DiscoveryError",
    "advertise",
    "browse",
    "current_short_hostname",
]

log = logging.getLogger(__name__)

SERVICE_TYPE = "_cahoot._tcp.local."
DEFAULT_BROWSE_TIMEOUT_S = 2.5


class DiscoveryError(RuntimeError):
    """Raised when the optional ``zeroconf`` package is missing."""


def _require_zeroconf() -> tuple[Any, Any, Any]:
    try:
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
    except ImportError as exc:  # pragma: no cover — exercised without [network]
        raise DiscoveryError(
            "the `zeroconf` package is required for mDNS discovery. "
            'Install with: `pip install -e ".[network]"`'
        ) from exc
    return (AsyncZeroconf, ServiceInfo, AsyncServiceBrowser)


@dataclass(frozen=True)
class DiscoveredInstance:
    """A Cahoot instance found by browsing the LAN."""

    name: str
    """Service name without the type suffix (e.g. ``mac-mini``)."""

    host: str
    """Resolvable hostname or IP, suitable for ``ws://<host>:<port>``."""

    port: int
    room: str
    proto: str = "ws"
    version: str = "1"

    @property
    def url(self) -> str:
        return f"{self.proto}://{self.host}:{self.port}"


def current_short_hostname() -> str:
    """Return a sanitised short hostname suitable for the service name."""
    fqdn = socket.gethostname()
    short = fqdn.split(".")[0]
    # zeroconf service names must be DNS-safe.
    return short.replace(" ", "-").lower() or "cahoot"


# ---------------------------------------------------------------------------
# Advertise
# ---------------------------------------------------------------------------


@asynccontextmanager
async def advertise(
    *,
    port: int,
    room: str = "ops",
    name: str | None = None,
    extra_txt: dict[str, str] | None = None,
) -> AsyncIterator[Any]:
    """Async context manager that registers / unregisters a Cahoot service.

    Usage::

        async with advertise(port=9876, room="ops"):
            ...  # listener loop here

    On context exit the service is unregistered cleanly.
    """
    AsyncZeroconf, ServiceInfo, _ = _require_zeroconf()
    instance_name = (name or current_short_hostname()).strip("._- ")
    if not instance_name:
        instance_name = "cahoot"
    service_name = f"{instance_name}.{SERVICE_TYPE}"

    txt: dict[str, str] = {
        "version": "1",
        "room": room,
        "host": socket.gethostname(),
        "proto": "ws",
    }
    if extra_txt:
        txt.update(extra_txt)
    properties = {k.encode(): v.encode() for k, v in txt.items()}

    info = ServiceInfo(
        type_=SERVICE_TYPE,
        name=service_name,
        addresses=_local_addresses(),
        port=port,
        properties=properties,
        server=f"{instance_name}.local.",
    )

    azc = AsyncZeroconf()
    try:
        await azc.async_register_service(info)
        log.info(
            "discovery: advertised %s on port %d (room=%s)",
            service_name,
            port,
            room,
        )
        yield info
    finally:
        with suppress(Exception):
            await azc.async_unregister_service(info)
        with suppress(Exception):
            await azc.async_close()
        log.info("discovery: stopped advertising %s", service_name)


def _local_addresses() -> list[bytes]:
    """Best-effort list of bindable IPv4 addresses for the machine."""
    seen: set[str] = set()
    out: list[bytes] = []
    try:
        # getaddrinfo turns the local hostname into one or more A records.
        for _family, *_rest, sockaddr in socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        ):
            host_field = sockaddr[0]
            if not isinstance(host_field, str):
                continue
            if host_field in seen or host_field.startswith("127."):
                continue
            seen.add(host_field)
            out.append(socket.inet_aton(host_field))
    except OSError:
        pass
    if not out:
        # Always include loopback so tests on the same machine work.
        out.append(socket.inet_aton("127.0.0.1"))
    return out


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------


async def browse(
    *,
    timeout_s: float = DEFAULT_BROWSE_TIMEOUT_S,
) -> list[DiscoveredInstance]:
    """Spend ``timeout_s`` seconds collecting Cahoot instances on the LAN."""
    AsyncZeroconf, ServiceInfo, AsyncServiceBrowser = _require_zeroconf()
    azc = AsyncZeroconf()
    found: dict[str, DiscoveredInstance] = {}

    def _record(info: Any) -> None:
        try:
            host = (info.parsed_addresses() or [""])[0] or info.server.rstrip(".")
            port = int(info.port or 0)
            props = info.properties or {}
            room = _prop(props, "room", "ops")
            proto = _prop(props, "proto", "ws")
            version = _prop(props, "version", "1")
            name = info.name.replace(SERVICE_TYPE, "").rstrip(".")
            found[info.name] = DiscoveredInstance(
                name=name,
                host=host,
                port=port,
                room=room,
                proto=proto,
                version=version,
            )
        except Exception as exc:
            log.debug("discovery: ignoring malformed record %r: %r", info, exc)

    def _on_state_change(zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        # We don't actually need to react in-handler — we'll do a bulk
        # resolve at the end. This handler exists so zeroconf considers
        # the browser engaged.
        pass

    try:
        browser = AsyncServiceBrowser(
            azc.zeroconf,
            SERVICE_TYPE,
            handlers=[_on_state_change],
        )
        await asyncio.sleep(timeout_s)
        await browser.async_cancel()
        # Resolve every name we observed.
        for service_name in list(azc.zeroconf.cache.names()):
            if not service_name.endswith(SERVICE_TYPE):
                continue
            info = ServiceInfo(SERVICE_TYPE, service_name)
            if await info.async_request(azc.zeroconf, 1500):
                _record(info)
    finally:
        with suppress(Exception):
            await azc.async_close()

    return sorted(found.values(), key=lambda i: i.name)


def _prop(props: dict[bytes, bytes | None], key: str, default: str) -> str:
    raw = props.get(key.encode())
    if raw is None:
        return default
    try:
        return raw.decode()
    except Exception:
        return default
