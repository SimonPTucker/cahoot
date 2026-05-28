#!/usr/bin/env bash
#
# cahoot-launch — start or attach the Cahoot tmux session.
#
# Idempotent: creates the session if missing, attaches if present.
# Use this as your daily entry point, either directly or wrapped by
# the macOS .app bundle in this directory.
#
# Configurable via environment:
#   CAHOOT_HOST     — host to SSH into. Empty/unset = run locally.
#   CAHOOT_SESSION  — tmux session name. Default: cahoot.
#   CAHOOT_CMD      — command tmux should run. Default: cahoot (the installed CLI).
#                   Use `python -m cahoot` for a development checkout.
#
# Examples:
#   cahoot-launch                           # local, default session
#   CAHOOT_HOST=agents-box cahoot-launch      # SSH into agents-box
#   CAHOOT_CMD='python -m cahoot' cahoot-launch # dev checkout

set -euo pipefail

SESSION="${CAHOOT_SESSION:-cahoot}"
CMD="${CAHOOT_CMD:-cahoot}"
HOST="${CAHOOT_HOST:-}"

# `new-session -A` attaches if the session exists, creates it otherwise.
TMUX_CMD="tmux new-session -A -s ${SESSION} '${CMD}'"

if [[ -n "${HOST}" ]]; then
    # -t forces a pseudo-tty, which tmux requires.
    exec ssh -t "${HOST}" "${TMUX_CMD}"
else
    # Local: just exec tmux directly.
    exec bash -c "${TMUX_CMD}"
fi
