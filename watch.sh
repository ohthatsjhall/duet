#!/usr/bin/env bash
# watch.sh <run-id> — follow whichever agent is working right now, readably.
# Codex and Claude both stream JSON events; this prints the human parts.
set -euo pipefail
run="${1:?usage: watch.sh <run-id>}"
dir="$HOME/.duet/runs/$run"
[ -d "$dir" ] || { echo "no such run: $run" >&2; exit 1; }

newest() { ls -t "$dir"/jobs/*/*/stdout.txt 2>/dev/null | head -1; }

current=""
while :; do
  f="$(newest || true)"
  if [ -n "$f" ] && [ "$f" != "$current" ]; then
    current="$f"
    printf '\n\033[1m── %s ──\033[0m\n' "${f#"$dir"/jobs/}"
    tail -f -n +1 "$f" &
    tailpid=$!
  fi
  sleep 5
  if [ -n "${tailpid:-}" ] && [ "$(newest || true)" != "$current" ]; then kill "$tailpid" 2>/dev/null || true; fi
done
