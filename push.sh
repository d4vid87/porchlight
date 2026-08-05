#!/bin/sh
# Send a phone alert. Called by ZoneMinder filters:
#   push.sh TOPIC SERVER [title] [message...]
# ZoneMinder appends the event details it knows about, which become the message.
set -e

# ntfy topics are part of a URL, so anything odd becomes an underscore.
TOPIC=$(printf '%s' "$1" | tr -c 'A-Za-z0-9_-' '_')
SERVER="${2:-https://ntfy.sh}"
TITLE="${3:-Camera alert}"
[ -n "$TOPIC" ] || { echo "no topic given" >&2; exit 1; }

MESSAGE=""
if [ "$#" -gt 3 ]; then
    shift 3
    MESSAGE="$*"
fi
[ -n "$MESSAGE" ] || MESSAGE="Something moved at $(date '+%H:%M')"

exec curl -fsS -m 20 -H "Title: $TITLE" -d "$MESSAGE" "$SERVER/$TOPIC" -o /dev/null
