#!/usr/bin/env bash
# Example helper for dream-review's optional rule candidates.
#
# Call it from your own blocking hooks, right before they block:
#   log_hook_block.sh <hook-name> <tool-name> <reason-label> <session-id>
#
# It appends one tab-separated line to the log named below. It records only
# what was blocked by which rule, never the blocked command or file path.
# Point dream-review at the same file with "hook_block_log" in the
# workspace config.json (see the README).
LOG="${DREAM_REVIEW_HOOK_LOG:-$HOME/.claude/hook_blocks.log}"
printf '%s\t%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "${1:-unknown}" "${2:-unknown}" "${3:-unknown}" "${4:-unknown}" >> "$LOG"
