#!/usr/bin/env bash
set -euo pipefail

# PreToolUse hook (matcher: Bash).
# Blocks forbidden git push operations:
#   - any push to "origin" (the Wailydest fork)
#   - --force / --force-with-lease on a git push
#   - "-f" flag on a git push
#   - bare "git push" whose upstream is not a polysam/ branch
# Allows "git push polysam ..." and any non-push git command.

input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty')"

if [ -z "$cmd" ]; then
  exit 0
fi

deny() {
  jq -n --arg reason "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

# Tokenize on whitespace. Word-boundary token tests avoid
# false positives on filenames that merely contain "-f".
read -ra TOKS <<< "$cmd"

has_token() {
  local needle="$1" t
  for t in "${TOKS[@]}"; do
    [ "$t" = "$needle" ] && return 0
  done
  return 1
}

# Not a push at all -> allow (covers every non-push git command).
if ! has_token "push"; then
  exit 0
fi

# Rule 1: push + origin as separate tokens.
if has_token "origin"; then
  deny "git-push guard: pushing to the Wailydest fork (origin) is forbidden. Offending command: ${cmd}"
fi

# Rule 2: forced push.
if has_token "--force" || has_token "--force-with-lease"; then
  deny "git-push guard: force-push is forbidden anywhere. Offending command: ${cmd}"
fi

# Rule 3: -f flag (exact token only).
if has_token "-f"; then
  deny "git-push guard: forced push via -f is forbidden. Offending command: ${cmd}"
fi

# Find the remote: first non-option token after "push".
remote=""
seen_push=0
for t in "${TOKS[@]}"; do
  if [ "$seen_push" -eq 1 ]; then
    case "$t" in
      -*) continue ;;
      *) remote="$t"; break ;;
    esac
  fi
  [ "$t" = "push" ] && seen_push=1
done

# Explicit remote.
if [ -n "$remote" ]; then
  if [ "$remote" = "polysam" ]; then
    exit 0
  fi
  # Any other explicit remote (origin already handled above): allow,
  # only the enumerated rules block.
  exit 0
fi

# Bare "git push": allow only if upstream is a polysam/ branch.
[ -n "$cwd" ] && cd "$cwd" 2>/dev/null || true
upstream="$(git rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || true)"

if [ -z "$upstream" ]; then
  deny "git-push guard: bare 'git push' with no resolvable upstream. Specify the remote explicitly (use 'git push polysam ...'). Offending command: ${cmd}"
fi

case "$upstream" in
  polysam/*) exit 0 ;;
  *)
    deny "git-push guard: bare 'git push' would target upstream '${upstream}' (not polysam/). Specify the remote explicitly (use 'git push polysam ...'). Offending command: ${cmd}"
    ;;
esac

exit 0
