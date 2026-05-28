#!/usr/bin/env bash
#
# Cahoot — server installer.
#
# Run this on the Mac mini (or other always-on box) that will host
# Cahoot. The script:
#
#   1. Verifies Python 3.11+ is on PATH.
#   2. Installs `uv` if missing (Astral's package launcher).
#   3. Installs tmux if missing (via Homebrew on macOS).
#   4. Installs the Cahoot CLIs (`cahoot`, `cahoot-join`) via
#      `uv tool install` straight from git — no clone required.
#   5. Seeds a default `~/.config/cahoot/cahoot.toml` if absent.
#   6. On macOS, offers to drop `Cahoot.app` into `/Applications`.
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/SimonPTucker/cahoot/main/scripts/install-server.sh | bash
#
# Optional env knobs:
#   CAHOOT_REPO   override git URL (default github.com/SimonPTucker/cahoot)
#   CAHOOT_REF    pin a branch / tag / sha (default `main`)
#   CAHOOT_NO_APP set to 1 to skip the .app prompt
#   CAHOOT_YES    set to 1 to answer 'yes' to every prompt
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

bold "Cahoot — server installer"
echo  "ref: ${REF}"

# ──────────────────────────────────────────────────────────────────────────
# Platform
# ──────────────────────────────────────────────────────────────────────────
case "$(uname)" in
  Darwin) PLATFORM=macos ;;
  Linux)  PLATFORM=linux ;;
  *)      err "unsupported platform: $(uname)"; exit 1 ;;
esac
if [[ "${PLATFORM}" != macos ]]; then
  warn "Cahoot's reference target is Apple Silicon macOS — Linux works in CI but isn't the day-to-day target."
fi

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
  if [[ "${PLATFORM}" == macos ]]; then
    echo "    install via:  brew install python@3.13"
  else
    echo "    install Python 3.11+ from your distro or https://www.python.org/downloads/"
  fi
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
# tmux
# ──────────────────────────────────────────────────────────────────────────
step "Checking tmux 3.0+"
if command -v tmux >/dev/null 2>&1; then
  ok "tmux: $(tmux -V)"
else
  if [[ "${PLATFORM}" == macos ]] && command -v brew >/dev/null 2>&1; then
    warn "tmux not found; installing via Homebrew"
    brew install tmux
    ok "tmux: $(tmux -V)"
  else
    warn "tmux not installed — install before launching Cahoot."
    if [[ "${PLATFORM}" == macos ]]; then
      echo "    brew install tmux"
    else
      echo "    apt install tmux  (or your distro's equivalent)"
    fi
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Install Cahoot CLIs
# ──────────────────────────────────────────────────────────────────────────
step "Installing Cahoot CLIs (cahoot + cahoot-join)"
# Force a fresh install so re-running picks up newer code.
# PEP 508 direct-reference syntax (`name[extras] @ source`) is uv's
# required form for installing a package with extras from a non-PyPI
# source — see https://docs.astral.sh/uv/concepts/tools/.
uv tool install --force "cahoot[acp,network] @ git+${REPO}@${REF}"
ok "Installed."

# Add uv's tool bin dir to PATH for the rest of this script.
UV_BIN="${HOME}/.local/bin"
export PATH="${UV_BIN}:${PATH}"

# ──────────────────────────────────────────────────────────────────────────
# Verify
# ──────────────────────────────────────────────────────────────────────────
step "Verifying CLIs"
if command -v cahoot >/dev/null 2>&1; then
  ok "cahoot on PATH"
else
  err "cahoot not on PATH after install"
  echo "    add this to your shell rc:  export PATH=\"\$HOME/.local/bin:\$PATH\""
  exit 1
fi
if command -v cahoot-join >/dev/null 2>&1; then
  ok "cahoot-join on PATH"
fi

# ──────────────────────────────────────────────────────────────────────────
# Default config
# ──────────────────────────────────────────────────────────────────────────
step "Seeding default config"
CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}/cahoot"
CONFIG="${CONFIG_HOME}/cahoot.toml"
if [[ -f "${CONFIG}" ]]; then
  ok "config already exists at ${CONFIG} — leaving it alone"
else
  mkdir -p "${CONFIG_HOME}"
  cat > "${CONFIG}" <<'TOML'
[cahoot]
room = "ops"
log_level = "INFO"

# A synthetic (fake) agent so you can verify the dashboard immediately.
# Delete this block once you have real agents configured.
[[agents]]
id = "synthetic-1"
role = "test"
kind = "synthetic"
chatter_interval_s = 2.0

# ────────────────────────────────────────────────────────────────────────
# Accept inbound `cahoot-join` connections from other machines on the
# LAN. With `advertise = true` (default), the listener broadcasts itself
# over mDNS / Bonjour so cahoot-join on other boxes can auto-find it.
# Uncomment to enable.
# ────────────────────────────────────────────────────────────────────────
# [cahoot.listener]
# enabled = true
# bind = "0.0.0.0"
# port = 9876
# advertise = true
TOML
  ok "wrote default config: ${CONFIG}"
fi

# ──────────────────────────────────────────────────────────────────────────
# Mac .app
# ──────────────────────────────────────────────────────────────────────────
if [[ "${PLATFORM}" == macos && "${CAHOOT_NO_APP:-0}" != "1" ]]; then
  step "Mac .app launcher"
  if [[ -d "/Applications/Cahoot.app" ]]; then
    ok "Cahoot.app already in /Applications"
  else
    echo  "  Cahoot ships a tiny .app that opens Terminal into a tmux session."
    REPLY="${CAHOOT_YES:-}"
    if [[ -z "${REPLY}" ]]; then
      printf "  Install Cahoot.app to /Applications? [y/N] "
      read -r REPLY < /dev/tty || REPLY=""
    fi
    if [[ "${REPLY}" =~ ^[Yy1]$ ]]; then
      TMP=$(mktemp -d)
      # shellcheck disable=SC2064  # we want the expansion now
      trap "rm -rf ${TMP}" EXIT
      # GitHub serves a tarball for any ref via this stable URL pattern.
      TAR_URL="https://github.com/SimonPTucker/cahoot/archive/${REF}.tar.gz"
      if curl -fsSL "${TAR_URL}" | tar -xz -C "${TMP}"; then
        SRC=$(find "${TMP}" -maxdepth 3 -type d -name "Cahoot.app" | head -1)
        if [[ -n "${SRC}" ]]; then
          rm -rf "/Applications/Cahoot.app"
          cp -R "${SRC}" "/Applications/Cahoot.app"
          chmod +x "/Applications/Cahoot.app/Contents/MacOS/run"
          ok "installed /Applications/Cahoot.app"
        else
          warn "couldn't locate Cahoot.app inside ${TAR_URL} — skipping .app install"
        fi
      else
        warn "couldn't download ${TAR_URL} — skipping .app install"
      fi
    else
      ok "skipped .app install (you can re-run this script later)"
    fi
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────────────────
echo
bold "All set. Quick reference:"
cat <<MSG

  Launch Cahoot:                cahoot
  Edit config:                  \$EDITOR ${CONFIG}
  Inside the TUI, try:          /help    /whoami    /roster    /quit

  To accept agents from another LAN machine:
    1. Uncomment [cahoot.listener] in ${CONFIG} and restart Cahoot.
    2. In the TUI:               /invite <agent_id> [role]
    3. Run the script printed on the agent's box (use install-agent.sh
       below to set that box up).

  Agent box installer (one-liner, paste on the agent's machine):
    curl -fsSL https://raw.githubusercontent.com/SimonPTucker/cahoot/${REF}/scripts/install-agent.sh | bash

  Docs:
    Onboarding flow:   https://github.com/SimonPTucker/cahoot/blob/main/docs/ONBOARDING.md
    Operations:        https://github.com/SimonPTucker/cahoot/blob/main/docs/OPERATIONS.md
MSG
