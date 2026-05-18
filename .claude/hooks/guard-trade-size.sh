#!/usr/bin/env bash
set -euo pipefail

# PreToolUse hook (matcher: Bash).
# Blocks any command that sets MAX_TRADE_SIZE_USDC above the 25 USDC ceiling.

input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"

if [ -z "$cmd" ]; then
  exit 0
fi

# Collect every MAX_TRADE_SIZE_USDC=<number> assignment (int or float).
matches="$(printf '%s' "$cmd" | grep -oE 'MAX_TRADE_SIZE_USDC=[0-9]+(\.[0-9]+)?' || true)"

if [ -z "$matches" ]; then
  exit 0
fi

while IFS= read -r m; do
  [ -z "$m" ] && continue
  num="${m#MAX_TRADE_SIZE_USDC=}"
  if awk -v n="$num" 'BEGIN { exit !((n + 0) > 25) }'; then
    jq -n --arg reason "MAX_TRADE_SIZE_USDC=${num} exceeds the 25 USDC ceiling. Requires explicit chat go-ahead per operating instructions." '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: $reason
      }
    }'
    exit 0
  fi
done <<< "$matches"

exit 0
