#!/usr/bin/env python3
"""End-to-end check against a real ZoneMinder, run inside the test container.

Starts the Porchlight server, then exercises every page's endpoints the way the
browser does: add cameras, read the live stream bytes, force an event and play
it back, edit a zone, save a rule, switch run states, add a person.

    python3 /src/e2e_drive.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8321"
SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SRC, "server"))
import zmapi  # noqa: E402  (needs the sys.path line above)

failures = []


def call(path, body=None):
    req = urllib.request.Request(
        BASE + "/api/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def get(path, params=None):
    """GET endpoints take a query string, the way the browser calls them."""
    url = BASE + "/api/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read().decode())


def fetch(url, n=2048):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read(n)


def bad(msg):
    raise AssertionError(msg)


def same(got, want):
    if got != want:
        bad("got %r, wanted %r" % (got, want))
    return True


def check(name, fn):
    try:
        fn()
        print("  ok   %s" % name)
    except Exception as e:
        failures.append("%s: %s" % (name, e))
        print("  FAIL %s: %s" % (name, e))


# --- start the server --------------------------------------------------------

# ponytail: keep the server off our stdout, or a piped run blocks until it dies.
log = open("/tmp/porchlight-server.log", "wb")


def start_server():
    s = subprocess.Popen([sys.executable, os.path.join(SRC, "server", "porchlight_server.py")],
                         env=dict(os.environ, PORCHLIGHT_WEBDIR=os.path.join(SRC, "web")),
                         stdout=log, stderr=log)
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/api/status", timeout=5)
            break
        except Exception:
            time.sleep(0.3)
    if s.poll() is not None:
        # A leftover server on 8321 would answer for us and hide real failures.
        raise RuntimeError("server died at start, see /tmp/porchlight-server.log")
    return s


server = start_server()

print("status page")
status = call("status")
check("ZoneMinder is up", lambda: status.get("ok") or bad(status.get("error")))
check("storage reported", lambda: status["storage"]["total"] > 0 or bad("no storage"))
check("web UI served", lambda: b"Porchlight" in fetch(BASE + "/", 4000) or bad("index missing"))
check("stylesheet served", lambda: b"topnav" in fetch(BASE + "/app.css", 4000) or bad("css missing"))
check("script served", lambda: b"cameraCard" in fetch(BASE + "/app.js", 40000) or bad("js missing"))

# --- cameras -----------------------------------------------------------------

print("cameras")
# Generated video files as camera sources, so this works in a container with no
# camera and no network.
subprocess.run(["bash", "-c",
                "mkdir -p /var/cache/zoneminder/testsrc && "
                "ffmpeg -loglevel quiet -f lavfi -i testsrc=size=640x480:rate=10 -t 20 "
                "  -pix_fmt yuv420p -y /var/cache/zoneminder/testsrc/a.mp4 && "
                "ffmpeg -loglevel quiet -f lavfi -i smptebars=size=640x480:rate=10 -t 20 "
                "  -pix_fmt yuv420p -y /var/cache/zoneminder/testsrc/b.mp4 && "
                "chown -R www-data:www-data /var/cache/zoneminder/testsrc"], check=False)

for old in get("cameras"):          # start from a clean slate
    call("camera/delete", {"id": old["id"]})

ids = []
for name, path in [("Front", "/var/cache/zoneminder/testsrc/a.mp4"),
                   ("Back", "/var/cache/zoneminder/testsrc/b.mp4")]:
    r = call("camera/add", {"name": name, "kind": "file", "path": path,
                            "function": "Monitor", "width": 640, "height": 480})
    ids.append(r["id"])
print("  added camera ids:", ids)

cams = get("cameras")
check("both cameras listed", lambda: len(cams) == 2 or bad(str(cams)))
check("camera fields present",
      lambda: (cams[0]["name"] == "Front" and cams[0]["stream"].startswith("/zm/"))
      or bad(str(cams[0])))
check("status field present",
      lambda: cams[0]["status"] in ("ok", "off", "offline") or bad(str(cams[0])))
check("mode change saves", lambda: call("camera/mode", {"id": ids[0], "function": "Modect"})["ok"])
check("mode really changed",
      lambda: same(get("camera", {"id": ids[0]})["monitor"]["Function"], "Modect"))
check("settings save", lambda: call("camera/save", {"id": ids[0], "Name": "Front Door"})["ok"])
check("settings really saved",
      lambda: same(get("camera", {"id": ids[0]})["monitor"]["Name"], "Front Door"))

# --- live view ---------------------------------------------------------------

print("live view")
subprocess.run(["bash", "-c", "zmpkg.pl start >/dev/null 2>&1; sleep 15"], check=False)


def stream_ok(mid):
    def run():
        # Media URLs are app-relative; the server proxies /zm/* to ZoneMinder.
        data = fetch(BASE + zmapi.snapshot_url(mid, scale=50), 3000)
        if b"\xff\xd8" not in data:
            bad("not a JPEG: %r" % data[:80])
    return run


for i, mid in enumerate(ids):
    check("pane %d streams a JPEG" % (i + 1), stream_ok(mid))

check("running camera reads as ok",
      lambda: same([c["status"] for c in get("cameras") if c["id"] == ids[0]][0], "ok"))


def two_streams_at_once():
    """Two live streams with distinct connkeys must not kill each other."""
    import threading
    out = {}

    def grab(k):
        try:
            out[k] = fetch(BASE + zmapi.stream_url(ids[0]), 2048)
        except Exception as e:
            out[k] = e
    threads = [threading.Thread(target=grab, args=(k,)) for k in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    for k in (0, 1):
        if not isinstance(out.get(k), bytes) or b"\xff\xd8" not in out[k]:
            bad(str(out.get(k))[:120])


check("two streams run at once", two_streams_at_once)

# --- MaxFPS repair ------------------------------------------------------------
# MaxFPS=10 on a network camera throttles zmc's read loop and smears frames,
# but on a file camera it is what paces playback. New network cameras must not
# get it, a restarted server must clear it from old ones, and file cameras
# must keep theirs.

print("maxfps repair")


def cap_of(mid):
    return str(get("camera", {"id": mid})["monitor"]["MaxFPS"])


check("file camera keeps its pacing cap",
      lambda: cap_of(ids[0]) in ("10", "10.00") or bad("MaxFPS=%s" % cap_of(ids[0])))

# An unreachable, switched-off network camera: only its saved fields matter.
rtsp_id = call("camera/add", {"name": "Net", "kind": "rtsp",
                              "path": "rtsp://192.0.2.9/e2e", "function": "None"})["id"]
ids.append(rtsp_id)
check("network camera gets no frame cap",
      lambda: cap_of(rtsp_id) not in ("10", "10.00") or bad("MaxFPS=10 on a fresh camera"))

call("camera/save", {"id": rtsp_id, "MaxFPS": "10"})
server.kill()
server.wait(timeout=10)
server = start_server()
get("cameras")                                   # first listing runs the sweep
check("old network frame cap swept away",
      lambda: cap_of(rtsp_id) not in ("10", "10.00") or bad("sweep left MaxFPS=10"))
check("sweep spared the file camera",
      lambda: cap_of(ids[0]) in ("10", "10.00") or bad("MaxFPS=%s" % cap_of(ids[0])))

# --- zones -------------------------------------------------------------------

print("zones")
rect = [[0, 0], [640, 0], [640, 480], [0, 480]]
existing = get("camera", {"id": ids[0]})["zones"]
check("zone saves", lambda: call("zone/save", {
    "id": existing[0]["Id"] if existing else None, "monitor": ids[0],
    "name": "Whole picture", "type": "Active", "level": "High", "points": rect})["ok"])


def zone_round_trip():
    z = get("camera", {"id": ids[0]})["zones"][0]
    pts = zmapi.parse_coords(z["Coords"])
    if pts != [tuple(p) for p in rect]:
        bad(z["Coords"])
    if int(z["MinAlarmPixels"]) != 307200 * 3 // 100:   # High = 3% of 640x480
        bad("MinAlarmPixels=%s" % z["MinAlarmPixels"])


check("zone polygon round trips", zone_round_trip)
check("advanced zone fields are offered",
      lambda: any(f["name"] == "CheckMethod" and f["options"] for f in get("zonefields"))
      or bad("no zone fields"))
check("an advanced zone value saves", lambda: call("zone/save", {
    "id": get("camera", {"id": ids[0]})["zones"][0]["Id"], "monitor": ids[0],
    "name": "Whole picture", "type": "Active", "level": "High", "points": rect,
    "advanced": {"CheckMethod": "AlarmedPixels", "ExtendAlarmFrames": "7"}})["ok"])
check("the advanced value stuck",
      lambda: same([str(get("camera", {"id": ids[0]})["zones"][0][k]) for k in
                    ("CheckMethod", "ExtendAlarmFrames")], ["AlarmedPixels", "7"]))
check("sensitivity preset applies",
      lambda: call("camera/sensitivity", {"id": ids[0], "level": "Low"})["ok"])

# --- recordings --------------------------------------------------------------

print("recordings")
subprocess.run(["bash", "-c", "zmu -m %s -a >/dev/null 2>&1; sleep 8; "
                              "zmu -m %s -c >/dev/null 2>&1; sleep 8" % (ids[0], ids[0])], check=False)
events = get("events", {"limit": 10})
check("an event was recorded", lambda: events["events"] or bad("nothing recorded"))
if events["events"]:
    ev = events["events"][0]
    # The player uses the mp4 and only falls back to the jpeg replay stream for
    # installs that store recordings as stills, which this one does not.
    check("recording plays back",
          lambda: b"ftyp" in fetch(BASE + ev["video"], 4000) or bad("no mp4 came back"))

    def range_ok():
        """A Range request must still come back playable through the proxy.

        ponytail: ZoneMinder 1.36's view_video ignores Range and sends the
        whole file (200), which Android plays fine; iPhone Safari needs real
        206 slices to seek -- synthesize them in proxy_media if that matters.
        """
        req = urllib.request.Request(BASE + ev["video"], headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status not in (200, 206) or not r.read(200):
                bad("status=%s" % r.status)

    check("recording survives a Range request", range_ok)
    check("today's count feeds the tile", lambda: get("status")["today"] >= 1
          or bad(str(get("status")["today"])))
    check("last activity feeds the card",
          lambda: [c["last"] for c in get("cameras") if c["id"] == ids[0]][0]
          or bad("no last event time"))
    check("keep forever", lambda: call("event/action", {"id": ev["id"], "action": "keep"})["ok"])
    check("delete recording", lambda: call("event/action", {"id": ev["id"], "action": "delete"})["ok"])

# --- rules -------------------------------------------------------------------

print("rules")
check("rule saves", lambda: call("rule/save", {
    "name": "E2E rule", "cameras": [ids[0]], "what": "motion", "keep": True})["ok"])


def find_rule():
    mine = [r for r in get("rules") if r["name"] == "E2E rule"]
    if not mine:
        bad("rule not listed")
    if mine[0]["what"] != "motion" or not mine[0]["keep"]:
        bad(str(mine[0]))
    return mine[0]


check("rule reads back in plain words", find_rule)
check("rule deletes", lambda: call("rule/delete", {"id": find_rule()["id"]})["ok"])

# A phone alert is a rule that runs the push script, so the topic has to survive
# the trip through the Filters table.
check("phone alert rule saves", lambda: call("rule/save", {
    "name": "E2E push", "cameras": [ids[0]], "what": "motion", "push": "e2e-topic-123"})["ok"])


def push_rule():
    mine = [r for r in get("rules") if r["name"] == "E2E push"]
    if not mine or mine[0]["push"] != "e2e-topic-123":
        bad(str(mine))
    return mine[0]


check("phone alert reads back", push_rule)
check("phone alert rule deletes", lambda: call("rule/delete", {"id": push_rule()["id"]})["ok"])

# --- run states --------------------------------------------------------------

print("home / away")
check("mode saves", lambda: call("state/save", {
    "name": "Away", "rows": [{"id": i, "function": "Modect", "enabled": True} for i in ids]})["ok"])
check("mode listed", lambda: any(s["Name"] == "Away" for s in get("states"))
      or bad([s["Name"] for s in get("states")]))
check("mode applies", lambda: call("state/apply", {"name": "Away"})["ok"])

# --- people ------------------------------------------------------------------

print("people")
check("person added", lambda: call("user/save", {"username": "e2eviewer",
                                                 "password": "e2epass123", "role": "viewer"})["ok"])
check("person listed", lambda: any(u["Username"] == "e2eviewer" for u in get("users"))
      or bad([u["Username"] for u in get("users")]))


def remove_person():
    u = [u for u in get("users") if u["Username"] == "e2eviewer"][0]
    call("user/delete", {"id": u["Id"]})
    if any(x["Username"] == "e2eviewer" for x in get("users")):
        bad("still listed")


check("person removed", remove_person)

# --- system ------------------------------------------------------------------

print("system")
check("settings table populated", lambda: len(get("configs")) > 50 or bad("too few settings"))
check("settings search filters",
      lambda: all("email" in (c["name"] + (c["prompt"] or "")).lower()
                  for c in get("configs", {"q": "email"})) or bad("search leaked rows"))
check("a setting can be changed",
      lambda: call("config/set", {"name": "ZM_WEB_H_REFRESH_MAIN", "value": "45"}).get("ok", True))


def setting_stuck():
    row = [r for r in get("configs", {"q": "WEB_H_REFRESH_MAIN"})
           if r["name"] == "ZM_WEB_H_REFRESH_MAIN"][0]
    same(str(row["value"]), "45")


check("setting really changed", setting_stuck)

# --- first run and phone access ----------------------------------------------

print("first run and phones")
check("sample camera adds itself",
      lambda: call("camera/sample", {})["id"] or bad("no monitor came back"))
check("sample camera listed",
      lambda: any(c["name"] == "Sample camera" for c in get("cameras"))
      or bad([c["name"] for c in get("cameras")]))


def remove_sample():
    c = [c for c in get("cameras") if c["name"] == "Sample camera"][0]
    call("camera/delete", {"id": c["id"]})


check("sample camera removes", remove_sample)

check("phone viewing starts off", lambda: get("access")["lan"] is False or bad("lan already on"))
check("phone viewing refuses without a password",
      lambda: call("access/save", {"lan": True})["ok"] is False or bad("opened up unprotected"))


def remote_needs_signin():
    """Anything that isn't loopback must sign in, even with the toggle still off."""
    ip = (zmapi.lan_addresses() or ["127.0.0.1"])[0]
    if ip == "127.0.0.1":
        return                                  # nothing but loopback in this container
    url = "http://%s:8321/api/status" % ip
    try:
        urllib.request.urlopen(url, timeout=10)
    except urllib.error.HTTPError as e:
        return same(e.code, 401)
    except urllib.error.URLError:
        return                                  # not even listening off this machine
    bad("a non-loopback request was served without signing in")


check("phones must sign in", remote_needs_signin)


def remote_media_needs_signin():
    """Camera video is behind the same sign-in as the API."""
    ip = (zmapi.lan_addresses() or ["127.0.0.1"])[0]
    if ip == "127.0.0.1":
        return
    url = "http://%s:8321%s" % (ip, zmapi.snapshot_url(ids[0]))
    try:
        urllib.request.urlopen(url, timeout=10)
    except urllib.error.HTTPError as e:
        return same(e.code, 401)
    except urllib.error.URLError:
        return                                  # not even listening off this machine
    bad("a camera picture was served without signing in")


check("camera video needs sign-in too", remote_media_needs_signin)

# --- cleanup -----------------------------------------------------------------

for mid in ids:
    call("camera/delete", {"id": mid})
check("cameras removed", lambda: len(get("cameras")) == 0 or bad(str(get("cameras"))))

server.kill()
server.wait(timeout=10)

print()
if failures:
    print("E2E FAILED (%d)" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("E2E OK")
