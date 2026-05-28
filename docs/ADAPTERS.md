# Writing an adapter

An adapter wraps one agent runtime and translates its native protocol to Cahoot's `Envelope` format. The base class handles all the boring lifecycle work (heartbeats, reconnect, error reporting, clean shutdown); you write four methods.

## The contract

```python
class AgentAdapter(ABC):
    @abstractmethod
    async def _open(self) -> None: ...
    @abstractmethod
    async def _close(self) -> None: ...
    @abstractmethod
    async def _read_loop(self) -> None: ...
    @abstractmethod
    async def _write(self, envelope: Envelope) -> None: ...
```

That's it. Subclass `AgentAdapter`, implement these four, register in `REGISTRY`, add a `[[agents]]` block to `cahoot.toml`. Done.

## What each method must do

### `_open`

Open the underlying transport — connect a socket, spawn a subprocess, instantiate an SDK client. Store the handle on `self`.

- Block until the connection is genuinely usable.
- Raise on failure. The base class will catch, emit an `ErrorPayload`, and retry with backoff.
- Don't emit envelopes from here — the base class handles status transitions.

### `_close`

Tear down whatever `_open` set up. **Must be idempotent and safe to call when already closed** — the base class calls it on every disconnect path, including from `_open` failure.

```python
async def _close(self) -> None:
    if self._client is not None:
        with suppress(Exception):
            await self._client.aclose()
        self._client = None
```

### `_read_loop`

Consume messages from the agent and publish envelopes onto the bus. Returns cleanly when the remote closes (treated as expected); raises on transport errors (triggers reconnect).

**Always use `self._publish_from_agent(env)`, not `self.bus.publish(env)`.** The base class needs to update liveness tracking on inbound traffic — using the wrong publish method breaks DEGRADED detection.

```python
async def _read_loop(self) -> None:
    async for msg in self._client:
        env = Envelope(
            source=self.agent_id,
            target="operator",
            payload=_translate(msg),
        )
        await self._publish_from_agent(env)
```

### `_write`

Translate one outbound envelope to the agent's native format and send it. Called from the dispatch loop only when the adapter is `CONNECTED` or `DEGRADED`.

- Don't retry. If it fails, raise — the base class emits an `ErrorPayload` and the envelope is dropped. The operator decides what to do next.
- If your agent doesn't accept the envelope's payload kind (e.g. an `OperatorOnlyAgent` that doesn't accept commands), emit an `ErrorPayload` and return without doing anything.

```python
async def _write(self, envelope: Envelope) -> None:
    if envelope.kind != "chat":
        log.warning("hermes adapter received unsupported kind: %s", envelope.kind)
        return
    await self._client.send_message(envelope.payload.text)
```

## Worked example: a minimal echo agent

A complete adapter for a hypothetical "echo agent" that just replies with whatever you send it, demonstrating every method:

```python
"""Echo adapter — replies to every chat with the same text, uppercased."""

import asyncio

from ..adapter import AgentAdapter
from ..envelope import ChatPayload, Envelope


class EchoAdapter(AgentAdapter):
    """A trivial agent that uppercases whatever you DM it."""

    def __init__(self, agent_id, role, bus, config=None, **opts):
        super().__init__(agent_id, role, bus, config)
        self._open_event = asyncio.Event()
        self._inbound: asyncio.Queue[str] = asyncio.Queue()

    async def _open(self) -> None:
        # Nothing to connect to — this agent lives in-process.
        # In a real adapter you'd: open a socket, start a subprocess,
        # call an SDK's connect(), etc.
        self._open_event.set()

    async def _close(self) -> None:
        self._open_event.clear()

    async def _read_loop(self) -> None:
        # Block until we have something to echo, then publish the reply.
        while self._open_event.is_set():
            text = await self._inbound.get()
            await self._publish_from_agent(
                Envelope(
                    source=self.agent_id,
                    target="operator",
                    payload=ChatPayload(text=text.upper()),
                )
            )

    async def _write(self, envelope: Envelope) -> None:
        text = getattr(envelope.payload, "text", None)
        if text is None:
            return
        # In a real adapter, this would be: `await self._client.send(...)`.
        # We just push it into our internal queue so _read_loop picks it up.
        await self._inbound.put(text)
```

Register it:

```python
# cahoot/adapters/__init__.py
from .echo import EchoAdapter

REGISTRY = {
    "synthetic": SyntheticAdapter,
    "echo": EchoAdapter,
}
```

Use it:

```toml
# ~/.config/cahoot/cahoot.toml
[[agents]]
id = "echo-1"
role = "test"
kind = "echo"
```

That's the entire integration path.

## How extra config reaches your adapter

Anything you put in the `[[agents]]` block that isn't `id`, `role`, `kind`, or `version` is passed as a keyword argument to your adapter's constructor:

```toml
[[agents]]
id = "noisy-synth"
role = "test"
kind = "synthetic"
chatter_interval_s = 0.5
drop_probability = 0.1
```

```python
class SyntheticAdapter(AgentAdapter):
    def __init__(
        self,
        agent_id, role, bus, config=None,
        *,
        chatter_interval_s: float = 3.0,
        drop_probability: float = 0.0,
    ):
        super().__init__(agent_id, role, bus, config)
        ...
```

Keep options keyword-only and provide defaults so the adapter works without explicit config.

## Tuning the lifecycle

`AdapterConfig` exposes the knobs:

| Field | Default | When to change |
|---|---|---|
| `heartbeat_interval_s` | 5.0 | Reduce for noisy/fast agents; increase to cut log noise |
| `heartbeat_timeout_s` | 15.0 | Increase if your agent has long quiet periods between bursts |
| `reconnect_initial_s` | 1.0 | Increase if your agent rate-limits reconnects |
| `reconnect_max_s` | 30.0 | Increase for very flaky transports |
| `reconnect_jitter` | 0.2 | Rarely worth changing |
| `inbox_maxsize` | 256 | Increase if your agent is the bottleneck and outbound bursts are expected |

Pass a custom config when constructing:

```python
adapter = HermesAdapter(
    "hermes-main", "orchestrator", bus,
    AdapterConfig(
        heartbeat_interval_s=2.0,
        heartbeat_timeout_s=10.0,
        version="0.9.4",
    ),
)
```

## Things adapters must NOT do

1. **Don't import `InMemoryBus`.** Depend on the `Bus` Protocol from `cahoot.bus`. The bus implementation will change; the Protocol won't.
2. **Don't mutate envelopes.** They're frozen. If you need to derive a new envelope, construct one.
3. **Don't `bus.publish` from `_read_loop`.** Use `_publish_from_agent`. It updates liveness.
4. **Don't retry inside `_write`.** Raise; let the base class log the error and let the operator decide.
5. **Don't block the event loop.** Wrap blocking SDK calls in `asyncio.to_thread`. No `time.sleep`, no synchronous network I/O.
6. **Don't swallow `CancelledError`.** It's how clean shutdown works.

## Testing your adapter

Pattern that works (see `tests/test_adapter.py`):

```python
async def test_my_adapter_connects():
    bus = InMemoryBus()
    op = bus.subscribe("operator")
    adapter = MyAdapter("test-1", "test-role", bus)
    task = asyncio.create_task(adapter.run())
    try:
        # Wait for the CONNECTED status envelope
        while True:
            env = await asyncio.wait_for(op.get(), timeout=2.0)
            if env.kind == "status" and env.payload.state == AgentState.CONNECTED:
                break
    finally:
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)
```

Always `asyncio.wait_for` queue reads with a small timeout — a hung adapter test should fail in seconds.

## When to write a new adapter vs. extend an existing one

**Write new** when the agent has a fundamentally different transport (HTTP vs. websocket vs. stdio) or message format.

**Extend existing** when the agent speaks the same protocol as an existing adapter but with different defaults or extra translation rules — just subclass the existing adapter and override the methods that differ.

## Naming convention

- Module name: `cahoot/adapters/<agent_name>.py` (lowercase, no suffix).
- Class name: `<AgentName>Adapter` (PascalCase).
- Registry key: lowercase agent name without `Adapter` suffix (`hermes`, `openclaw`, `synthetic`).
- Agent ID in config: dash-separated, includes a role hint if you'll run multiple (`hermes-main`, `hermes-secondary`).
