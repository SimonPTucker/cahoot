#!/usr/bin/env bash
#
# Cahoot — uninstaller.
#
# Removes the uv tool install (cahoot + cahoot-join CLIs), and on
# macOS removes /Applications/Cahoot.app if present. Leaves your
# config and event store in place by default (they're small and
# preserve history); pass --purge to remove them too.
#
# Usage:
#   bash scripts/uninstall.sh           # remove CLIs + Cahoot.app
#   bash scripts/uninstall.sh --purge   # also drop ~/.config/cahoot
#                                       # and ~/.local/state/cahoot

set -euo pipefail

PURGE=0
for arg in "$@"; do
  case "${arg}" in
    --purge) PURGE=1 ;;
    *)       echo "unknown arg: ${arg}"; exit 2 ;;
  esac
done

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

bold "Cahoot — uninstall"

if command -v uv >/dev/null 2>&1; then
  if uv tool list 2>/dev/null | grep -q '^cahoot'; then
    uv tool uninstall cahoot
    ok "uninstalled cahoot via uv"
  else
    warn "cahoot was not installed via uv tool (nothing to uninstall there)"
  fi
else
  warn "uv not on PATH — skipping CLI uninstall"
fi

if [[ "$(uname)" == Darwin && -d /Applications/Cahoot.app ]]; then
  rm -rf /Applications/Cahoot.app
  ok "removed /Applications/Cahoot.app"
fi

if [[ "${PURGE}" == 1 ]]; then
  for d in "${HOME}/.config/cahoot" "${HOME}/.local/state/cahoot"; do
    if [[ -d "${d}" ]]; then
      rm -rf "${d}"
      ok "removed ${d}"
    fi
  done
fi

bold "Done."
