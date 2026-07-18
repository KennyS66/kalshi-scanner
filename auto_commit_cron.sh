#!/usr/bin/env bash
# Hourly data/state snapshot (installed in crontab): commit everything the
# collectors and bot journaled, rebase on the cloud routine's pushes, push.
# Never blocks on auth prompts; on a rebase conflict it aborts and leaves the
# working tree alone for the next hour rather than guessing.
set -uo pipefail
cd "$(dirname "$0")"
export GIT_TERMINAL_PROMPT=0

LOG="/tmp/auto_commit.log"
STAMP="$(date '+%Y-%m-%d %H:%M')"

{
  echo "===== $STAMP ====="
  git add -A
  if git diff --cached --quiet; then
    echo "nothing to commit"
  else
    git commit -m "auto: hourly data/state snapshot ($STAMP)" || exit 0
  fi
  # Sync with the cloud pre-market routine before pushing; autostash guards
  # any files the daemons rewrite mid-run.
  if ! git pull --rebase --autostash origin master; then
    git rebase --abort 2>/dev/null
    echo "pull --rebase failed; will retry next hour"
    exit 0
  fi
  git push origin master && echo "pushed" || echo "push failed; will retry next hour"
} >> "$LOG" 2>&1
