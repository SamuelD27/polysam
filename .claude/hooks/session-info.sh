#!/usr/bin/env bash
set -euo pipefail

# SessionStart hook (type: command).
# Informational only: never blocks, always exits 0.
# stdout is added to Claude's context at session start.

REPO="/home/samsam/polymarket-hustle"

input="$(cat 2>/dev/null || true)"
cwd=""
if [ -n "$input" ]; then
  cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null || true)"
fi
[ -z "$cwd" ] && cwd="$PWD"

cd "$REPO" 2>/dev/null || true

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
echo "branch: ${branch}"

env_name="${CONDA_DEFAULT_ENV:-none}"
if [ "$env_name" = "polymarket-env" ]; then
  echo "env: ${env_name}"
else
  echo "env: ${env_name} (WARNING: expected polymarket-env)"
fi

if [ "$cwd" = "$REPO" ]; then
  echo "cwd ok: yes"
else
  echo "cwd ok: no (cwd=${cwd}, expected ${REPO})"
fi

pidfile="${REPO}/daemon_state/daemon.pid"
if [ -f "$pidfile" ]; then
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "daemon: running (pid ${pid})"
  else
    echo "daemon: not running (stale pidfile, pid ${pid:-unknown})"
  fi
else
  echo "daemon: not running"
fi

if [ -f "${REPO}/daemon_state/KILL" ]; then
  echo "KILL file: present"
else
  echo "KILL file: absent"
fi

exit 0
