#!/bin/sh
# Hand a camera alert to the Porchlight server, which decides whether and how
# to send it (cooldown, snooze, snapshot attached). Called by ZoneMinder
# filters as:
#   push.sh TOPIC SERVER [words...]
# ZoneMinder appends the event's directory as the last argument; test alerts
# pass plain words instead, which become the message.
set -e
[ -n "$1" ] || { echo "no topic given" >&2; exit 1; }
TOPIC="$1"
SERVER="${2:-https://ntfy.sh}"
shift
[ "$#" -gt 0 ] && shift

DIR=""
MSG=""
for a in "$@"; do
    if [ -d "$a" ]; then DIR="$a"; else MSG="$MSG $a"; fi
done

if curl -fsS -m 40 \
    --data-urlencode "topic=$TOPIC" \
    --data-urlencode "server=$SERVER" \
    --data-urlencode "message=${MSG# }" \
    --data-urlencode "dir=$DIR" \
    "http://127.0.0.1:${PORCHLIGHT_PORT:-8321}/api/push" -o /dev/null; then
    exit 0
fi

# The server is not running (machine rebooted, app never opened): send a plain
# alert straight to ntfy so no event goes unannounced.
TOPIC=$(printf '%s' "$TOPIC" | tr -c 'A-Za-z0-9_-' '_')
exec curl -fsS -m 20 -H "Title: Camera alert" \
    -d "Something moved at $(date '+%H:%M')" "$SERVER/$TOPIC" -o /dev/null
