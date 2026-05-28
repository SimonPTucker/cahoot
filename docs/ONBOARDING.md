# Agent onboarding

How agents join a Cahoot fleet — the design, the wire, the security model, and the troubleshooting playbook. If you've already done it once successfully and just want a reminder, jump to [the cheat sheet](#cheat-sheet) at the bottom.

> **Audience.** Operators who run Cahoot, plus engineers who want to build new adapters or run agents in unusual topologies. Agents themselves should read [`AGENT_GUIDE.md`](AGENT_GUIDE.md) — that's the participation rulebook from the agent's point of view; this document is the operator's view of the same thing.

---

## TL;DR

There are **two ways** for an agent to join a Cahoot fleet:

1. **Local spawn.** Cahoot starts the agent itself as a subprocess on the same Mac. Use this when the agent lives on the same box as Cahoot.
2. **Network join.** The agent runs on a different machine on the LAN. The operator types `/invite <agent_id>` in the Cahoot TUI, gets a copy-pasteable `cahoot-join` command, and pastes it onto the agent's machine. Cahoot's listener accepts the WebSocket connection, validates the single-use token, and the agent appears in the roster within a second.

Both paths run the same ACP onboarding handshake (welcome → `READY` ACK → admission decision → instructions prompt) and produce the same operator experience. The agent doesn't need to know which path it took.

---

## When to use which path

| Situation | Path |
|---|---|
| Single Mac mini host running Cahoot + agents | **Local spawn** ([`README.md`](../README.md) §"Adding real agents") |
| Mac mini runs Cahoot; agent lives on a workstation / GPU box / laptop on the same LAN | **Network join** (this doc) |
| Multiple seats of the same agent kind spread across machines | **Network join** for each seat |
| Quick test of the bus, no real agent yet | Either — `kind = "synthetic"` works in both |

Local spawn is simpler — one process, one config block, no port to open. Network join scales out to whatever boxes you have on the LAN without restarting Cahoot.

---

## Architecture of the network-join path

```
   ┌─────────────────────────── Mac mini (Cahoot host) ──────────────────────────┐
   │                                                                             │
   │   ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐     │
   │   │  Textual TUI     │    │   Event bus      │    │  SQLite store    │     │
   │   │  (operator)      │◄──►│  (in-process)    │◄──►│  (WAL)           │     │
   │   └──────────────────┘    └────────┬─────────┘    └──────────────────┘     │
   │                                    │                                        │
   │   ┌──────────────────┐    ┌────────┴─────────┐                              │
   │   │ Invite registry  │    │  RemoteAdapter   │  one per joined seat         │
   │   │ (in-memory)      │    │  (WebSocket)     │                              │
   │   └────────┬─────────┘    └────────┬─────────┘                              │
   │            │                       │                                        │
   │   ┌────────┴───────────────────────┴──────────┐                             │
   │   │  WebSocket listener  :9876                │  + mDNS advert              │
   │   └─────────────────────────┬─────────────────┘                             │
   │                             │                                               │
   └─────────────────────────────┼───────────────────────────────────────────────┘
                                 │ LAN (your trust boundary)
   ┌─────────────────────────────┼─── Other machine (agent's host) ─────────────┐
   │                             │                                              │
   │   ┌──────────────────┐      │     ┌───────────────────────┐                 │
   │   │  cahoot-join     │ ◄────┘     │  RemoteBridgeBus      │                 │
   │   │  CLI (bridge)    │◄──────────►│  (in-process)         │                 │
   │   └────────┬─────────┘            └──────────┬────────────┘                 │
   │            │                                 │                              │
   │            │ spawns + drives                 │                              │
   │            ▼                                 ▼                              │
   │   ┌────────────────────────┐        ┌──────────────────────┐                │
   │   │  hermes-acp / openclaw │        │  ACPAdapter (local)  │                │
   │   │  acp (ACP stdio)       │◄──────►│  onboarding + xlate  │                │
   │   └────────────────────────┘        └──────────────────────┘                │
   │                                                                              │
   └──────────────────────────────────────────────────────────────────────────────┘
```

Key idea: the **bridge** (`cahoot-join`) is a tiny process on the agent's box that runs the *normal* local-spawn `ACPAdapter` — except that adapter's bus is a `RemoteBridgeBus` that ships every envelope over the WebSocket to Cahoot instead of routing locally. Cahoot has a `RemoteAdapter` that does the inverse — it presents the WebSocket to the operator's bus as if it were a regular adapter. The two halves are symmetric; neither has to know there's a network between them.

---

## The five-step onboarding flow

### Step 0 — enable the listener (once, on Cahoot's box)

```toml
# ~/.config/cahoot/cahoot.toml
[cahoot.listener]
enabled      = true       # accept inbound cahoot-join connections
bind         = "0.0.0.0"  # 0.0.0.0 = LAN-wide; "127.0.0.1" locks to localhost
port         = 9876
invite_ttl_s = 1800       # token expiry, seconds (default: 30 min)
advertise    = true       # broadcast over mDNS so cahoot-join can auto-find us
```

Restart Cahoot. The log shows:

```
listener: ws server bound to 0.0.0.0:9876
discovery: advertised mac-mini._cahoot._tcp.local. on port 9876 (room=ops)
listener: announcing ws://mac-mini.local:9876 for invites
```

If the listener is **disabled** (the default) then `/invite` reports the feature is off — that's how you can be sure no inbound connections are possible.

### Step 1 — mint an invite

In the Cahoot TUI:

```
/invite hermes-main planner
```

Cahoot prints right in the feed:

```
invite for hermes-main (role: planner)
  token expires in 30 minutes; single-use
  paste this on the box where the agent will live:

    cahoot-join \
      --server ws://mac-mini.local:9876 \
      --token CH7-9X42-8K3M \
      --as hermes-main --role planner \
      --kind hermes \
      -- uvx --from 'hermes-agent[acp]' hermes-acp
```

The block contains everything the user needs. `--server` is filled in from your hostname, `--token` is the freshly-minted token, `--as` matches the agent_id you typed.

To see every outstanding invite: `/invites` (alias `/tokens`).

### Step 2 — run the bridge on the agent's box

On any LAN host with Python 3.11+:

```bash
pip install -e ".[acp,network]"
# paste the block from Cahoot
```

With mDNS auto-discovery enabled (default), you can also drop the `--server` flag entirely:

```bash
cahoot-join \
  --token CH7-9X42-8K3M \
  --as hermes-main --role planner \
  --kind hermes \
  -- uvx --from 'hermes-agent[acp]' hermes-acp
```

`cahoot-join` browses the LAN for `_cahoot._tcp.local.` for 2.5 seconds, finds the host, and connects.

If you have multiple Cahoot instances on the LAN, `cahoot-join --list` shows them all and you pick one with `--server-name <name>`.

### Step 3 — handshake on the wire

`cahoot-join` opens the WebSocket and sends:

```json
{
  "type":    "hello",
  "version": 1,
  "id":      "hermes-main",
  "role":    "planner",
  "token":   "CH7-9X42-8K3M"
}
```

Cahoot validates the token against the invite registry. On success:

```json
{
  "type":     "welcome",
  "ok":       true,
  "agent_id": "hermes-main",
  "room":     "ops"
}
```

On failure (unknown / expired token, wrong claimed agent_id, duplicate connection):

```json
{
  "type":   "rejected",
  "reason": "invite rejected (expired): token expired at …"
}
```

…and the WebSocket closes.

### Step 4 — ACP onboarding (the same as local spawn)

Once welcomed, the local agent (running on the bridge's box) goes through the standard handshake — Cahoot is unaware it's remote:

1. **Cahoot → agent:** ACP `initialize` request.
2. **Agent → Cahoot:** ACP `initialize` response with capabilities.
3. **Cahoot → agent:** `session/new`.
4. **Cahoot → agent:** welcome prompt — *"You are connected to Cahoot mission control. Your agent_id is `hermes-main`… Reply with `READY` to confirm."*
5. **Agent → Cahoot:** any reply containing the literal `READY` (case-insensitive). The agent's own words come through to the operator's feed verbatim.
6. **Cahoot:** decides admission per [`[cahoot.admission]`](#admission-modes).
7. **Cahoot → agent:** the condensed participation guide (or the quarantine notice if not admitted).
8. From here, normal traffic: chat envelopes, task updates, metrics, errors. The operator's `/dm`, `/approve`, `/deny` all work as if the agent were local.

### Step 5 — disconnection

Three normal end-of-life paths, all clean:

- **Bridge process exits** (Ctrl-C, agent's host reboots, etc.) → Cahoot sees the WS close, the `RemoteAdapter` reports OFFLINE, the agent's row leaves the roster.
- **Operator runs `/quit`** in the Cahoot TUI → Cahoot sends `bye`, every bridge tears down, every agent process exits.
- **`tmux kill-session`** on Cahoot's host → same as `/quit` via the SIGHUP path.

`RemoteAdapter` is **one-shot**: the invite token was single-use, so a disconnected agent doesn't auto-reconnect. To re-join you mint a fresh `/invite` and run the new command.

---

## Invite tokens

Tokens are minted on the operator's request and live entirely in memory. There's no on-disk store and no validation server.

| Property | Value |
|---|---|
| Format | `CH7-XXXX-YYYY` (versioned prefix + two random chunks) |
| Alphabet | base32 minus `0/O/I/1` so hand-typing is painless |
| Entropy | ~40 bits — adequate for an in-LAN admission gate, not for identity claims |
| Default TTL | 30 minutes (`[cahoot.listener].invite_ttl_s`) |
| Lifecycle | Single-use; consumed on first valid connect |
| Binding | A token is bound to a specific `agent_id` + role at mint time. Using the token to claim a *different* `agent_id` is rejected. |
| Persistence | None — restarting Cahoot invalidates every outstanding invite |
| Revocation | Not exposed in v1, but `InviteRegistry.revoke(token)` exists for v1.5 |

> Tokens are short on purpose. They're an authentication factor inside an already-trusted LAN — not a long-lived credential — so a 12-character hand-typeable string is the right shape.

`/invites` (alias `/tokens`) lists outstanding invitations with their remaining TTL so you can see at a glance whether a stale token is still active.

---

## Local runtime auto-detection

`cahoot-join` can figure out which agent runtime to drive without being told. When you omit `--kind`, the bridge probes the local machine:

| Runtime | Detected when… |
|---|---|
| **Hermes** | `uvx` is on `PATH` (Hermes itself is fetched on demand by `uvx`). |
| **OpenClaw** | `openclaw` CLI is on `PATH`. If `~/.openclaw/main.token` exists, it's used as the default `--token-file`. |
| **Synthetic** | always available — but **never auto-picked**; pass `--kind synthetic` to opt in. |

Resolution:

- **One real runtime available** → used silently.
- **Both Hermes and OpenClaw available** → bridge refuses to guess; pass `--kind hermes` or `--kind openclaw`.
- **Neither** → bridge prints the install hints (one for each runtime it tried) and exits non-zero.

`cahoot-join --detect` runs the probes and prints a report, then exits. Useful when you're setting up a new agent box and want to see what's installed before pasting an invite:

```text
agent runtimes detected on this machine:

  ✓ hermes  uvx 0.9.7
      uvx detected — Hermes will be fetched on first launch via `uvx --from 'hermes-agent[acp]' hermes-acp`.

  ✓ openclaw  OpenClaw 1.4.2
      openclaw CLI detected
      default token file detected: /Users/you/.openclaw/main.token

  ✓ synthetic
      built-in test agent — no external runtime required.

tip: multiple real runtimes available — pass `--kind <name>` to disambiguate.
```

The detection takes under a second in the typical case; in the worst case (two subprocess version probes time out) it caps at six seconds.

## Service discovery (mDNS / Bonjour)

When `[cahoot.listener].advertise = true` (the default), Cahoot publishes itself as `_cahoot._tcp.local.` via Bonjour on macOS / Avahi on Linux. The TXT record carries:

| Key | Value |
|---|---|
| `version` | Protocol version (currently `"1"`) |
| `room` | The default room name (e.g. `"ops"`) |
| `host` | The full hostname (useful when the short name collides) |
| `proto` | Currently always `"ws"`; v1.5 adds `"wss"` |

`cahoot-join` browses for this type during its discovery window (2.5 s by default, override with `--discover-timeout-s`):

- **Zero instances found** → exits with a clear error.
- **One instance found** → uses its URL automatically.
- **Multiple instances found** → demands `--server-name <name>` to pick one.

`cahoot-join --list` is the quick way to inspect what's on the LAN.

mDNS doesn't traverse VLANs or NATs. If your agent box is on a different subnet from Cahoot, supply `--server` explicitly with the routable host or IP.

---

## Admission modes

The listener is the door; admission is what happens after the door opens. Two modes, set in `cahoot.toml`:

```toml
[cahoot.admission]
mode = "open"     # or "strict"
allowed_ids = []  # extra IDs for strict mode
```

| Mode | Behaviour |
|---|---|
| `open` *(default)* | Every agent that completes the ACK gets admitted. Pair with single-use tokens — the token already gates who can connect. |
| `strict` | An admitted-list. Every `[[agents]]` block plus every entry in `allowed_ids` is implicitly trusted; everyone else lands in **quarantine**. |

A quarantined agent is connected and visible to the operator, but its outbound routing is clamped to `target = "operator"` (no DMs to peers, no broadcasts) and it doesn't receive inbound traffic from anyone but the operator. The operator can change a quarantined agent's status at runtime:

```
/approve hermes-main             # quarantined → admitted
/deny    openclaw-1 too noisy    # admitted → quarantined
```

These commands send `admit` / `quarantine` control frames over the WebSocket; the bridge applies them to the local adapter so the agent receives a runtime notice the moment the operator decides.

---

## Disconnection and reconnection semantics

`RemoteAdapter` is intentionally **non-reconnecting**. Reasons:

- The invite token was single-use. A reconnect would need a fresh token anyway.
- An unexpected disconnect should be visible to the operator, not silently papered over.
- Reconnect logic at this layer would have to invent identity continuity decisions that the spawn-side adapter doesn't have to make.

If the bridge re-establishes its WebSocket with a stale token, Cahoot replies `rejected` with `unknown_token`. The operator must `/invite <same-agent-id>` again. The new connection takes the same `agent_id` slot, so the roster line continues from where it was.

The bridge itself has its usual `AgentAdapter` reconnect-with-jitter loop around the **local** agent process — so if the local `hermes-acp` subprocess crashes, the bridge restarts it without involving Cahoot. Cahoot only sees stable session state.

---

## Security model

Cahoot is designed to live behind your network's existing trust boundary, not to be the trust boundary itself.

| Concern | v1 stance |
|---|---|
| **Network transport** | Plain WebSocket (`ws://`). No TLS. Anyone on the LAN can sniff. |
| **Authentication** | Single-use, time-limited invite tokens bound to a specific `agent_id`. Adequate for inside a trusted LAN, not adequate as an Internet-facing identity layer. |
| **Authorisation** | Admission policy (`open` or `strict`) + runtime `/approve` / `/deny`. |
| **Identity** | Self-claimed by the bridge. The token + binding prevents trivial impersonation; nothing prevents an authenticated bridge from misrepresenting its own behaviour. |
| **Replay** | Tokens are consumed on first valid connect, so replay is bounded to the TTL window before first use. |
| **Confidentiality of stored events** | The SQLite store on Cahoot's host. Protect it with normal Unix file permissions. |
| **External exposure** | Don't open `0.0.0.0:9876` to the internet. If you must reach Cahoot from outside the LAN, tunnel over SSH (`ssh -L 9876:localhost:9876 mac-mini`) and connect `cahoot-join --server ws://localhost:9876`. |

`v1.5` will add `wss://` with a self-signed cert (operators trust-pin via the TUI on first connection), operator-driven approval queue (every new connection lands in a holding pen the operator clears with `/approve`), and persistent tokens with explicit revocation.

---

## Troubleshooting

### `no Cahoot instance discovered on the LAN`
Most likely causes, in order: listener isn't enabled in `cahoot.toml`; advertise is off; the agent box is on a different VLAN/subnet from Cahoot; macOS Firewall is blocking Bonjour. Try `--server ws://<hostname>:9876` explicitly to confirm the listener itself is reachable.

### `cahoot rejected our hello: invite rejected (unknown_token)`
Token was already used, was revoked, or doesn't exist. Mint a new one with `/invite`.

### `cahoot rejected our hello: invite rejected (expired)`
Past the TTL. Mint a new token; consider raising `[cahoot.listener].invite_ttl_s` if 30 min is too short for your workflow.

### `cahoot rejected our hello: invite rejected (wrong_agent_id)`
The bridge's `--as` doesn't match the `agent_id` the operator bound the token to. Re-check the `/invite` line in the feed.

### `agent_id 'hermes-main' already connected`
There's already a live `RemoteAdapter` for that ID. Either it's a previous bridge you forgot about (kill it), or the previous bridge's TCP socket hasn't fully closed yet (wait ~10 seconds and retry; or `/deny hermes-main; /approve hermes-main` on the operator side to force a status churn that surfaces the stale connection).

### Bridge connects but agent never reaches `READY`
The local agent might not have an LLM credential — `hermes-acp` will fail silently if its OAuth isn't set up. Run `hermes-acp` on its own first to make sure it starts. Logs go to the bridge's stderr.

### Cahoot's feed shows the bridge but no chat traffic
Quarantined. Look for `[admission]` lines in the feed. `/approve <agent_id>` lifts the gate.

### Discovery finds two Cahoot instances with the same short name
Both Macs are named `mac-mini.local`. Override one with `[cahoot.listener]`-adjacent metadata isn't currently exposed, so set `HOSTNAME` for one of them or rename it via System Settings → Sharing.

---

## What's on the wire

A complete frame catalogue. Every frame is a UTF-8 text WebSocket message containing a single JSON object.

**`hello`** (bridge → Cahoot, mandatory first frame)
```json
{ "type": "hello", "version": 1,
  "id":   "hermes-main", "role": "planner",
  "token":"CH7-9X42-8K3M",
  "client_version": "0.1.0"  // optional, informational
}
```

**`welcome`** (Cahoot → bridge, on accept)
```json
{ "type": "welcome", "ok": true,
  "agent_id": "hermes-main", "room": "ops" }
```

**`rejected`** (Cahoot → bridge, on refusal)
```json
{ "type": "rejected", "reason": "human-readable reason" }
```

**`envelope`** (bidirectional, the workhorse)
```json
{ "type": "envelope",
  "data": { /* full Envelope JSON, including kind via discriminator */ } }
```
The `data` field is a Cahoot `Envelope` serialised exactly as it appears in the SQLite event store — same fields, same discriminator, same validation rules.

**`admit`** (Cahoot → bridge, on `/approve`)
```json
{ "type": "admit", "by": "operator" }
```

**`quarantine`** (Cahoot → bridge, on `/deny`)
```json
{ "type": "quarantine", "by": "operator", "reason": "optional" }
```

**`ready`** (Cahoot → bridge, just after open) — informational; the bridge can use it to sync its enrollment view with Cahoot's.
```json
{ "type": "ready", "agent_id": "hermes-main", "enrollment": "admitted" }
```

**`ping`** / **`pong`** — optional liveness probe (either direction).

**`bye`** — graceful close (either direction).

Order of operations is enforced: `hello` must be first; everything else only flows after `welcome`.

---

## Cheat sheet

```bash
# On Cahoot's box (Mac mini), once, in ~/.config/cahoot/cahoot.toml:
[cahoot.listener]
enabled = true
bind    = "0.0.0.0"
port    = 9876
advertise = true

# In the Cahoot TUI:
/invite hermes-main planner

# On the agent's box (anywhere on the LAN):
pip install -e ".[acp,network]"
cahoot-join \
  --token CH7-9X42-8K3M \
  --as hermes-main --role planner \
  --kind hermes \
  -- uvx --from 'hermes-agent[acp]' hermes-acp
# --server omitted → mDNS auto-discovery

# Useful operator commands:
/invites              # list outstanding tokens + TTLs
/roster               # who's connected, lifecycle + enrollment state
/dm hermes-main hi    # direct message
/all standby          # broadcast to every other agent
/approve hermes-main  # admit a quarantined agent
/deny hermes-main x   # quarantine an admitted agent (with optional reason)
/quit                 # clean shutdown
```
