#!/usr/bin/env bash
#
# Cahoot — agent bridge installer.
#
# Run this on a machine where Hermes or OpenClaw will live and that
# needs to talk to a Cahoot instance elsewhere on the LAN. The script:
#
#   1. Verifies Python 3.11+ is on PATH.
#   2. Installs `uv` if missing.
#   3. Installs the Cahoot package via `uv tool install` so the
#      `cahoot-join` CLI is available (no clone required).
#   4. Checks for `uvx` (needed by Hermes) and `openclaw` (needed by
#      OpenClaw), with install hints if either is missing.
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/SimonPTucker/cahoot/main/scripts/install-agent.sh | bash
#
# Optional env knobs:
#   CAHOOT_REPO   override git URL (default github.com/SimonPTucker/cahoot)
#   CAHOOT_REF    pin a branch / tag / sha (default `main`)
#
# Re-run any time; the script is idempotent.

set -euo pipefail

REPO="${CAHOOT_REPO:-https://github.com/SimonPTucker/cahoot.git}"
REF="${CAHOOT_REF:-main}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*"; }
step() { printf "\n\033[36m──\033[0m %s\n" "$*"; }

bold "Cahoot — agent bridge installer"
echo  "ref: ${REF}"

# ──────────────────────────────────────────────────────────────────────────
# Python 3.11+
# ──────────────────────────────────────────────────────────────────────────
step "Checking Python 3.11+"
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "${cand}" >/dev/null 2>&1; then
    v=$("${cand}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
    if [[ "${v}" =~ ^3\.(11|12|13|14)$ ]]; then
      PY="${cand}"; break
    fi
  fi
done
if [[ -z "${PY}" ]]; then
  err "no Python 3.11+ on PATH"
  case "$(uname)" in
    Darwin) echo "    install via:  brew install python@3.13" ;;
    *)      echo "    install Python 3.11+ from your distro or https://www.python.org/downloads/" ;;
  esac
  exit 1
fi
ok "Python: $("${PY}" --version)"

# ──────────────────────────────────────────────────────────────────────────
# uv
# ──────────────────────────────────────────────────────────────────────────
step "Checking uv (Astral)"
if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found; installing"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi
if ! command -v uv >/dev/null 2>&1; then
  err "uv install completed but uv is still not on PATH — add \$HOME/.local/bin to PATH and re-run."
  exit 1
fi
ok "uv: $(uv --version)"

# ──────────────────────────────────────────────────────────────────────────
# Install the bridge
# ──────────────────────────────────────────────────────────────────────────
step "Installing cahoot-join (and cahoot CLI, which comes along for the ride)"
# PEP 508 direct-reference form — see install-server.sh for why.
uv tool install --force "cahoot[acp,network] @ git+${REPO}@${REF}"
ok "Installed."

UV_BIN="${HOME}/.local/bin"
export PATH="${UV_BIN}:${PATH}"

# ──────────────────────────────────────────────────────────────────────────
# Verify
# ──────────────────────────────────────────────────────────────────────────
step "Verifying cahoot-join"
if command -v cahoot-join >/dev/null 2>&1; then
  ok "cahoot-join on PATH"
else
  err "cahoot-join not on PATH after install"
  echo "    add this to your shell rc:  export PATH=\"\$HOME/.local/bin:\$PATH\""
  exit 1
fi

# ──────────────────────────────────────────────────────────────────────────
# Agent runtime auto-detection
# ──────────────────────────────────────────────────────────────────────────
step "Auto-detecting agent runtimes via cahoot-join"
echo
# This is the authoritative report — version-aware, with install hints
# for whatever isn't here. Anything we'd hand-roll in this script would
# go stale; let the CLI tell us.
if ! cahoot-join --detect; then
  warn "cahoot-join --detect exited non-zero — see message above"
fi

# ──────────────────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────────────────
echo
bold "All set."
cat <<'MSG'

  Next steps:
    1. Re-run the detection at any time:        cahoot-join --detect
    2. See which Cahoot servers are reachable:  cahoot-join --list
    3. When your operator types /invite, paste their `cahoot-join …`
       block here. In the typical case (one of Hermes / OpenClaw
       installed, Cahoot reachable via mDNS) it'll be just:
           cahoot-join --token <T> --as <agent_id> --role <role>

  Docs:
    Onboarding flow:    https://github.com/SimonPTucker/cahoot/blob/main/docs/ONBOARDING.md
    Agent guide (LLMs): https://github.com/SimonPTucker/cahoot/blob/main/docs/AGENT_GUIDE.md
MSG
