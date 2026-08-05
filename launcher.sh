#!/bin/sh
# Start the Porchlight server if it isn't already running, then open the window.
set -e

PORT="${PORCHLIGHT_PORT:-8321}"
URL="http://127.0.0.1:$PORT/"
SERVER=/usr/share/porchlight/server/porchlight_server.py

if ! python3 -c "import socket,sys; s=socket.socket();
sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)" 2>/dev/null; then
    mkdir -p "${XDG_RUNTIME_DIR:-/tmp}/porchlight"
    # setsid: the server outlives the terminal or launcher that started it.
    setsid python3 "$SERVER" >"${XDG_RUNTIME_DIR:-/tmp}/porchlight/server.log" 2>&1 </dev/null &
    # wait for it to answer before opening the browser
    i=0
    while [ $i -lt 50 ]; do
        python3 -c "import socket,sys; s=socket.socket();
sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)" 2>/dev/null && break
        i=$((i + 1))
        sleep 0.2
    done
fi

exec xdg-open "$URL"
