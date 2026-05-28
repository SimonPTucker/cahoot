# Operations

This document covers running Cahoot day-to-day: launching, attaching, configuration, logs, the Mac `.app` launcher, and troubleshooting.

## The daily pattern

Cahoot is designed to run forever inside a named tmux session on a host you control (Mac mini, home server, cloud VM). You SSH in and `tmux attach` from wherever you happen to be. The standard persistent-SSH pattern: detach with `Ctrl-b d`, the session keeps running, attach again later and you're back exactly where you left it.

```bash
# On the host that runs your agents — one-time setup
tmux new-session -d -s cahoot 'cahoot'

# From any client (Mac, iPad with Blink, work laptop)
ssh agents-box -t tmux attach -t cahoot
```

The `-A` flag is your friend if you want a single idempotent command that creates the session if missing and attaches if present:

```bash
ssh agents-box -t tmux new-session -A -s cahoot 'cahoot'
```

That's the entire operator workflow.

## Configuration

Cahoot reads a single TOML file. Lookup order:

1. `$CAHOOT_CONFIG` (explicit override)
2. `$XDG_CONFIG_HOME/cahoot/cahoot.toml` (default: `~/.config/cahoot/cahoot.toml`)
3. `./cahoot.toml` (useful for development from a checkout)

A minimal config:

```toml
[cahoot]
room = "ops"
log_level = "INFO"

[[agents]]
id = "synthetic-1"
role = "test"
kind = "synthetic"
chatter_interval_s = 2.0

[[agents]]
id = "hermes-main"
role = "orchestrator"
kind = "hermes"
version = "0.9.4"

[[agents]]
id = "openclaw-runner"
role = "formatter"
kind = "openclaw"
```

See [`ADAPTERS.md`](ADAPTERS.md) for what options each adapter accepts. Anything beyond `id`, `role`, `kind`, `version` is passed to the adapter constructor.

## File layout on disk

Cahoot follows the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/):

| What | Where |
|---|---|
| Config | `~/.config/cahoot/cahoot.toml` |
| Logs (rotating, 5MB × 3) | `~/.local/state/cahoot/cahoot.log` |
| Lock file (single-instance) | `~/.local/state/cahoot/cahoot.lock` |
| SQLite event store (phase 1) | `~/.local/state/cahoot/cahoot.db` |

On a fresh machine the only directory you need to create is `~/.config/cahoot/`; the state dir is created automatically on first run.

## Logs

Cahoot writes to a rotating file because Textual owns the terminal — any `print` or stderr-bound log line would corrupt the UI. To watch logs live alongside the dashboard:

```bash
# In another tmux pane
tail -F ~/.local/state/cahoot/cahoot.log
```

The log file rotates at 5MB; three backups are kept (`cahoot.log.1`, `cahoot.log.2`, `cahoot.log.3`). For longer retention, ship them out with logrotate or syslog.

To raise log verbosity, set `log_level = "DEBUG"` in `cahoot.toml`. The adapter state-machine transitions are at DEBUG and are very useful when diagnosing reconnect storms.

## Signal handling

Cahoot traps three signals and translates them to a clean shutdown:

| Signal | When you'd see it | Cahoot's behaviour |
|---|---|---|
| `SIGINT` | `Ctrl-C` | Clean stop; all adapters close transports |
| `SIGTERM` | `kill <pid>`, systemd-style stop | Same as SIGINT |
| `SIGHUP` | `tmux kill-session`, terminal hangup | Same — clean stop |
| `SIGWINCH` | Terminal resize | Passed through to Textual |

What this means in practice:

- **`tmux detach` is free.** No signal sent to Cahoot; it keeps running. Reattach any time.
- **`tmux kill-session -t cahoot` is graceful.** SIGHUP fires; adapters close transports; SQLite flushes; process exits in under a second.
- **`kill -9 <pid>` is not graceful.** SIGKILL can't be trapped; in-flight envelopes may not persist. Only use as a last resort.

## Single-instance lock

Cahoot enforces "one process per user" via `fcntl.flock` on `~/.local/state/cahoot/cahoot.lock`. If you accidentally start a second instance:

```
$ cahoot
cahoot: another Cahoot process is running (pid 12345); `tmux attach -t cahoot` to join it, or kill it first
```

This is intentional. Two processes trying to drive the same Hermes/OpenClaw sessions would compete for the underlying transports — undefined behaviour you don't want to debug at 2am.

If the lock file is stale (process crashed without cleanup), the next start succeeds because `flock` is associated with the file descriptor, not the file itself; the OS releases it on process exit.

## Mac `.app` launcher (phase 5)

For daily use on a Mac, drop `Cahoot.app` in `/Applications` and pin it to the Dock. Double-clicking opens Terminal attached to the running session.

The `.app` bundle is just a thin wrapper. Structure:

```
Cahoot.app/
└── Contents/
    ├── Info.plist
    ├── MacOS/
    │   └── run               # shell script, executable
    └── Resources/
        └── icon.icns         # optional
```

`Contents/MacOS/run` (example):

```bash
#!/bin/bash
set -e
HOST="${CAHOOT_HOST:-localhost}"
SESSION="${CAHOOT_SESSION:-cahoot}"

osascript <<EOF
tell application "Terminal"
    activate
    do script "ssh -t ${HOST} 'tmux new-session -A -s ${SESSION} cahoot'"
end tell
EOF
```

For `HOST=localhost`, this attaches to a local tmux session running on the same Mac. For a remote box, set `CAHOOT_HOST` via `launchctl setenv` or hardcode it.

**No code signing required** if you're only using it personally — Gatekeeper will warn on first launch; right-click → Open dismisses it permanently. If you want to distribute the `.app`, you'll need an Apple Developer account ($99/yr) and to notarise the bundle.

For a Python-native packaging path with proper Info.plist generation, consider [Briefcase from BeeWare](https://briefcase.readthedocs.io/) — but the shell-script `.app` above is enough for personal use and has zero build pipeline.

## SSH from iPad or thin client

The pattern works identically from [Blink Shell](https://blink.sh/) on iPad or any other SSH client:

```bash
ssh agents-box -t tmux new-session -A -s cahoot cahoot
```

The tmux session keeps the dashboard alive between connections, and the typing experience is identical to a desktop terminal as long as the client supports xterm escape sequences (Blink, Termius, iTerm2, kitty, alacritty all do).

## Troubleshooting

### "another Cahoot process is running"

Either you actually have one running (`tmux attach -t cahoot` to find it) or a previous crash didn't release the lock. The latter is rare with `flock` — if it happens, manually remove `~/.local/state/cahoot/cahoot.lock` and restart.

### Dashboard renders garbled

Almost always a terminal compatibility issue. Confirm `TERM=xterm-256color` or `tmux-256color`:

```bash
echo $TERM
```

If `TERM` is `xterm` or `vt100`, you're missing 256-colour support and Textual can't render reliably. In `.tmux.conf`:

```
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",xterm-256color:Tc"
```

### Adapter stuck in `CONNECTING`

The adapter's `_open` is blocking forever. Check the log:

```bash
grep "<agent-id>" ~/.local/state/cahoot/cahoot.log
```

You'll see reconnect attempts with their delay. If you see `adapter.transport_error` repeatedly with the same root cause, that's the agent or its transport refusing connections — not a Cahoot problem.

### Adapter stuck in `DEGRADED`

The transport is connected but no inbound messages for `heartbeat_timeout_s`. Either:

- The agent really is silent (legitimate quiet period — increase `heartbeat_timeout_s` for that adapter).
- The agent is hung (kill and restart it; the adapter will reconnect automatically).
- Your `_publish_from_agent` calls are going via the wrong path (check you're not calling `self.bus.publish` directly from `_read_loop`).

### Process keeps dying

Run headless without nohup/tmux to see the crash:

```bash
python -m cahoot --no-ui
```

You'll get the traceback directly. If it's `AlreadyRunning`, see "another Cahoot process is running" above.

### Logs aren't rotating

Cahoot uses `RotatingFileHandler` with 5MB × 3. If the log isn't rotating, something else is holding the file open — usually a forgotten `tail -F` or a backup tool. Restarting Cahoot forces rotation handling to recompute.

## Multi-host considerations

Cahoot v1 is single-host. If you want mission control across two machines:

- **Easy path:** SSH into each host and run a separate Cahoot instance. Two tmux sessions, two attach commands.
- **Less easy path:** wait for v2's cross-process bus, or wrap your remote agents in adapters that connect *from* the Cahoot host *to* the remote agent.

The fundamental architecture supports the latter — adapters can connect anywhere — but the synthetic adapter in v1 is local-only. Real network transports (SSH, gRPC, websocket) are how production deployments will look.

## Backups

The only file worth backing up is `~/.local/state/cahoot/cahoot.db` (after phase 1). It contains your event history. The lock file is ephemeral; the log file is replayable in spirit by reading the DB.

A nightly cron of:

```bash
sqlite3 ~/.local/state/cahoot/cahoot.db ".backup ~/.local/state/cahoot/cahoot.db.$(date +%F)"
find ~/.local/state/cahoot/cahoot.db.* -mtime +14 -delete
```

…gives you 14 days of point-in-time snapshots at the cost of a few MB.
