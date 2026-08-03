#!/usr/bin/env bash
# Kill-switch of last resort: pause/resume the swing bot without the web UI.
#
# The dashboard's STOP button is the normal path. This exists for when the
# UI is down, the browser is closed, or you are on a headless ssh session --
# situations where there would otherwise be no kill-switch at all.
#
#   ./stop.sh          pause (block new entries; open plays still exit)
#   ./stop.sh resume
#
# Writes the same control.json shape as web.bot_control_write, via a tmp
# file + mv so the bot can never read a half-written command.
set -euo pipefail
cd "$(dirname "$0")"

CMD="${1:-pause}"
case "$CMD" in
  pause|resume) ;;
  *) echo "usage: $0 [pause|resume]" >&2; exit 2 ;;
esac

DIR="${BOT_DIR:-data/bot}"
mkdir -p "$DIR"
CTL="$DIR/control.json"

# Bump the existing nonce; the bot only acts on a nonce it hasn't seen.
# Also floor it against the nonce the bot has already consumed: if
# control.json is truncated or corrupt the sed yields nothing, and a
# restart from 1 would be silently ignored (read_control requires
# nonce > last_control_nonce) while this script cheerfully reports success.
NONCE=0
if [[ -f "$CTL" ]]; then
  NONCE=$(sed -n 's/.*"nonce"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$CTL" | head -1)
  [[ -n "$NONCE" ]] || NONCE=0
fi
if [[ -f "$DIR/bot_state.json" ]]; then
  SEEN=$(sed -n 's/.*"last_control_nonce"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' \
         "$DIR/bot_state.json" | head -1)
  [[ -n "$SEEN" && "$SEEN" -gt "$NONCE" ]] && NONCE="$SEEN"
fi
NONCE=$((NONCE + 1))

printf '{"nonce": %d, "cmd": "%s"}' "$NONCE" "$CMD" > "$CTL.tmp"
mv "$CTL.tmp" "$CTL"

echo "$CMD sent (nonce $NONCE) — the bot picks this up on its next tick (~5s)"
