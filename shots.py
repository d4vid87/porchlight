#!/usr/bin/env python3
"""Fill ZoneMinder with demo cameras and take the README screenshots.

Runs inside the test container: python3 /src/scripts_shots.py /src/screenshots
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8321"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/src/screenshots"
CAMERAS = [("Front Door", "testsrc"), ("Back Yard", "smptebars"),
           ("Driveway", "rgbtestsrc"), ("Garage", "testsrc2")]


def call(path, body=None):
    req = urllib.request.Request(BASE + "/api/" + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def get(path):
    return json.loads(urllib.request.urlopen(BASE + "/api/" + path, timeout=120).read())


def sh(cmd):
    return subprocess.run(["bash", "-c", cmd], check=False)


os.makedirs(OUT, exist_ok=True)
SETUP = len(get("cameras")) != len(CAMERAS)   # demo data survives between runs
if SETUP:
    sh("mkdir -p /var/cache/zoneminder/demo")
    for name, pattern in CAMERAS:
        f = "/var/cache/zoneminder/demo/%s.mp4" % pattern
        sh("test -f %s || ffmpeg -loglevel quiet -f lavfi -i %s=size=1280x720:rate=10 -t 30 "
           "-pix_fmt yuv420p -y %s" % (f, pattern, f))
    sh("chown -R www-data:www-data /var/cache/zoneminder/demo")

    for old in get("cameras"):
        call("camera/delete", {"id": old["id"]})
    for ev in get("events?limit=200")["events"]:      # recordings of long-gone cameras
        call("event/action", {"id": ev["id"], "action": "delete"})
    ids = []
    for name, pattern in CAMERAS:
        ids.append(call("camera/add", {"name": name, "kind": "file", "width": 1280, "height": 720,
                                       "path": "/var/cache/zoneminder/demo/%s.mp4" % pattern,
                                       "function": "Mocord"})["id"])

    call("rule/save", {"name": "Email me at night", "cameras": ids[:2], "what": "motion",
                       "between": ["22:00", "06:00"], "email": True})
    call("rule/save", {"name": "Keep the driveway for a month", "cameras": [ids[2]],
                       "delete_after_days": 30})
    call("state/save", {"name": "Away", "rows": [{"id": i, "function": "Modect", "enabled": True}
                                                 for i in ids]})
    call("state/save", {"name": "Home", "rows": [{"id": i, "function": "Monitor", "enabled": True}
                                                 for i in ids]})
    call("user/save", {"username": "guest", "password": "guestpass1", "role": "viewer"})

    sh("zmpkg.pl start >/dev/null 2>&1; sleep 25")
    sh("zmu -m %s -a >/dev/null 2>&1; sleep 10; zmu -m %s -c >/dev/null 2>&1; sleep 15" % (ids[0], ids[0]))
    sh("zmu -m %s -a >/dev/null 2>&1; sleep 8; zmu -m %s -c >/dev/null 2>&1; sleep 15" % (ids[1], ids[1]))

# --- shoot --------------------------------------------------------------------

sh("pkill Xvfb; sleep 1; (Xvfb :99 -screen 0 1440x900x24 >/dev/null 2>&1 &); sleep 2")
env = "DISPLAY=:99"

for page, wait in [("cameras", 10), ("live", 16), ("recordings", 12), ("rules", 8),
                   ("modes", 8), ("people", 8), ("system", 12)]:
    sh("%s timeout 120 python3 %s/shot_page.py '%s/#%s' %s/%s.png %d"
       % (env, os.path.dirname(os.path.abspath(__file__)), BASE, page, OUT, page, wait))
    print("shot", page, flush=True)

sh("pkill Xvfb")
print("screenshots in", OUT)
