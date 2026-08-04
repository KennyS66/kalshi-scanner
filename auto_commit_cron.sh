#!/usr/bin/env bash
# Daily data/state snapshot (installed in crontab): commit everything the
# collectors and bot journaled, push it, and only bother integrating with
# the remote if the push is actually rejected.
#
# The previous version always ran `git pull --rebase` BEFORE pushing and
# `exit 0`d when that failed. Replaying local commits onto the cloud
# pre-market routine's version of the same append-only journals conflicts
# every time both have appended, so the pull failed, the script aborted, and
# the push never ran -- silently, for two weeks and 16 commits. Two changes
# stop that recurring: push first (a fast-forward needs no pull at all), and
# .gitattributes resolves the append-only conflicts with merge=union.
#
# Never blocks on auth prompts. On a rebase it cannot resolve it leaves the
# working tree alone for the next run rather than guessing.
set -uo pipefail
cd "$(dirname "$0")"
export GIT_TERMINAL_PROMPT=0

# Persistent, NOT /tmp: the old log lived in /tmp and was wiped on every
# reboot, which is the only reason this went unnoticed so long. logs/ is
# gitignored.
mkdir -p logs
LOG="logs/auto_commit.log"
STAMP="$(date '+%Y-%m-%d %H:%M')"

{
  echo "===== $STAMP ====="
  git add -A
  if git diff --cached --quiet; then
    echo "nothing to commit"
  else
    git commit -m "auto: daily data/state snapshot ($STAMP)" \
      || { echo "ERROR: commit failed"; exit 0; }
  fi

  push_out="$(git push origin master 2>&1)"; rc=$?
  echo "$push_out"
  if [ "$rc" -eq 0 ]; then
    echo "pushed"
    exit 0
  fi

  # Distinguish "remote moved" (worth integrating) from network/auth trouble
  # (rebasing would be pointless and would churn the tree for nothing).
  if ! printf '%s' "$push_out" | grep -qiE 'non-fast-forward|fetch first|rejected'; then
    echo "ERROR: push failed for a non-rebase reason (network/auth?) -- not rebasing"
    exit 0
  fi

  echo "remote moved; integrating then retrying"
  if ! git pull --rebase --autostash origin master 2>&1; then
    git rebase --abort 2>/dev/null
    echo "ERROR: rebase failed even with merge=union -- needs a human"
    exit 0
  fi
  git push origin master 2>&1 && echo "pushed after rebase" \
    || echo "ERROR: push still rejected after a clean rebase"
} >> "$LOG" 2>&1
