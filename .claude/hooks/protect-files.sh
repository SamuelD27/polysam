#!/usr/bin/env bash
set -euo pipefail

# PreToolUse hook (matcher: Edit|Write|MultiEdit|Create).
# Hard-denies edits to protected live-execution files.
# Warns (but allows) edits to refined_strategy.py.

input="$(cat)"
fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')"

if [ -z "$fp" ]; then
  # No file_path in payload: nothing to protect, allow.
  exit 0
fi

# Normalize to a repo-relative path.
rel="${fp#/home/samsam/polymarket-hustle/}"
rel="${rel#./}"

PROTECTED=(
  "active_bots/execution/live_executor.py"
  "active_bots/execution/reconciler.py"
  "active_bots/execution/risk_manager.py"
)

for p in "${PROTECTED[@]}"; do
  if [ "$rel" = "$p" ]; then
    jq -n --arg reason "protected live-execution file: ${rel}. Requires explicit chat approval before edit (see RUNBOOK.md)." '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: $reason
      }
    }'
    exit 0
  fi
done

if [ "$rel" = "active_bots/refined_strategy.py" ]; then
  echo "WARNING: refined_strategy.py edit. The squeeze path is intentionally disabled (docs/SESSION_2026-04-22_STRATEGY_TUNING.md finding #8). Confirm in chat which block you are touching." >&2
  exit 0
fi

exit 0
