#!/usr/bin/env python3
"""End-to-end check against a real ZoneMinder, run inside the test container.

Starts the Porchlight server, then exercises every page's endpoints the way the
browser does: add cameras, read the live stream bytes, force an event and play
it back, edit a zone, save a rule, switch run states, add a person.

    python3 /src/e2e_drive.py
"""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8321"
SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SRC, "server"))
# The deb installs the model under /usr/share; tests run from the checkout.
os.environ["PORCHLIGHT_MODEL"] = os.path.join(SRC, "models", "nanodet-plus-m-416.onnx")
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


def start_server(extra_env=None):
    env = dict(os.environ, PORCHLIGHT_WEBDIR=os.path.join(SRC, "web"))
    env.update(extra_env or {})
    s = subprocess.Popen([sys.executable, os.path.join(SRC, "server", "porchlight_server.py")],
                         env=env, stdout=log, stderr=log)
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

def force_event(mid, tries=3):
    """A closed event with frames, or None. zmu -a can race zmc's clip-loop
    respawn and leave a frameless event; force again when that happens."""
    for _ in range(tries):
        before = [e["id"] for e in get("events", {"limit": 3})["events"]]
        subprocess.run(["bash", "-c", "zmu -m %s -a >/dev/null 2>&1; sleep 8; "
                                      "zmu -m %s -c >/dev/null 2>&1" % (mid, mid)], check=False)
        for _ in range(10):
            time.sleep(2)
            evs = get("events", {"limit": 1})["events"]
            if evs and evs[0]["id"] not in before and int(evs[0].get("frames") or 0) > 0:
                return evs[0]
    return None


print("recordings")
ev = force_event(ids[0])
check("an event was recorded", lambda: ev or bad("nothing recorded"))
if ev:
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

check("weekday + blip rule saves", lambda: call("rule/save", {
    "name": "E2E days", "what": "motion", "days": "weekdays", "min_frames": 5})["ok"])


def days_rule():
    mine = [r for r in get("rules") if r["name"] == "E2E days"][0]
    if mine["days"] != "weekdays" or mine["min_frames"] != 5:
        bad(str(mine))
    call("rule/save", {"id": mine["id"], "name": "E2E days", "what": "motion",
                       "days": "weekends"})
    mine = [r for r in get("rules") if r["name"] == "E2E days"][0]
    if mine["days"] != "weekends":
        bad(str(mine))
    call("rule/delete", {"id": mine["id"]})


check("weekday rule round trips", days_rule)

# --- phone alerts through the server -----------------------------------------
# push.sh hands events to /api/push, which decides (cooldown, snooze, snapshot)
# and posts to ntfy. A local capture server stands in for ntfy.sh.

print("phone alerts")
NTFY = "http://127.0.0.1:8399"
ntfy_hits = []


class FakeNtfy(http.server.BaseHTTPRequestHandler):
    def _grab(self):
        n = int(self.headers.get("Content-Length") or 0)
        ntfy_hits.append({"method": self.command, "path": self.path,
                          "body": self.rfile.read(n)})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = do_PUT = _grab

    def log_message(self, *a):
        pass


threading.Thread(target=http.server.ThreadingHTTPServer(
    ("127.0.0.1", 8399), FakeNtfy).serve_forever, daemon=True).start()


def push_form(fields):
    """POST the way push.sh does: curl form fields, not JSON."""
    req = urllib.request.Request(BASE + "/api/push",
                                 data=urllib.parse.urlencode(fields).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


# The recordings section deleted its event, so force a fresh one to alert on.
push_ev = force_event(ids[0])
push_dir = "/var/cache/zoneminder/events/%s/2026-01-01/%s" % (
    push_ev["monitor"], push_ev["id"]) if push_ev else ""


def snapshot_push():
    r = push_form({"topic": "e2e-push", "server": NTFY, "dir": push_dir})
    if not r.get("ok") or r.get("skipped"):
        bad(str(r))
    hit = ntfy_hits[-1]
    if hit["method"] != "PUT" or not hit["body"].startswith(b"\xff\xd8"):
        bad("method=%s body=%r" % (hit["method"], hit["body"][:20]))
    if "priority=" not in hit["path"] or "title=" not in hit["path"]:
        bad(hit["path"])


check("alert carries the snapshot", snapshot_push)


def cooldown():
    n = len(ntfy_hits)
    r = push_form({"topic": "e2e-push", "server": NTFY, "dir": push_dir})
    if r.get("skipped") != "cooldown" or len(ntfy_hits) != n:
        bad(str(r))


check("second alert inside a minute is swallowed", cooldown)


def snooze_swallows():
    call("snooze", {"minutes": 5})
    n = len(ntfy_hits)
    r = push_form({"topic": "e2e-push", "server": NTFY, "message": "hi"})
    call("snooze", {"minutes": 0})
    if r.get("skipped") != "snoozed" or len(ntfy_hits) != n:
        bad(str(r))
    if get("status")["snooze_until"] != 0:
        bad("snooze did not clear")


check("snooze swallows alerts and clears", snooze_swallows)

PUSH = os.path.join(SRC, "push.sh")


def script_relays():
    n = len(ntfy_hits)
    # Any directory exercises the event-dir branch; this one names no event,
    # so the server sends a plain text alert.
    p = subprocess.run(["sh", PUSH, "e2e-push2", NTFY, "/var/cache/zoneminder/events"],
                       capture_output=True, text=True, timeout=90)
    if p.returncode or len(ntfy_hits) == n:
        bad(p.stderr[-200:] or "no hit reached ntfy")


check("push.sh hands the event to the server", script_relays)


def injection_safe():
    n = len(ntfy_hits)
    evil = "$(touch /tmp/e2e-pwned); `id`"
    p = subprocess.run(["sh", PUSH, "e2e-push3", NTFY, evil],
                       capture_output=True, text=True, timeout=90)
    if p.returncode or len(ntfy_hits) == n:
        bad(p.stderr[-200:] or "no hit reached ntfy")
    if os.path.exists("/tmp/e2e-pwned"):
        bad("the message was executed as shell")
    if b"$(touch" not in ntfy_hits[-1]["body"]:
        bad(str(ntfy_hits[-1]["body"][:100]))


check("hostile text rides as text", injection_safe)


def fallback_direct():
    """Server down (fresh boot): push.sh must still reach ntfy on its own."""
    n = len(ntfy_hits)
    p = subprocess.run(["sh", PUSH, "e2e-push4", NTFY],
                       env=dict(os.environ, PORCHLIGHT_PORT="9"),
                       capture_output=True, text=True, timeout=60)
    if p.returncode or len(ntfy_hits) == n:
        bad(p.stderr[-200:] or "no hit reached ntfy")
    if "e2e-push4" not in ntfy_hits[-1]["path"]:
        bad(ntfy_hits[-1]["path"])


check("push.sh alone still alerts when the server is down", fallback_direct)


def timeline_counts():
    t = get("timeline", {})
    if len(t["hours"]) != 24 or sum(t["hours"]) < 1:
        bad(str(t))


check("timeline strip has today's recordings", timeline_counts)
check("recordings sort by activity",
      lambda: isinstance(get("events", {"sort": "score"})["events"], list)
      or bad("no list came back"))

# --- watchdog ----------------------------------------------------------------

print("watchdog")
# Stale push rules (screenshot runs leave one) would send these test alerts to
# a real phone; the watchdog alerts every push target, so clear them first.
for r in get("rules"):
    if r.get("push"):
        call("rule/delete", {"id": r["id"]})
call("rule/save", {"name": "E2E watch", "what": "any",
                   "push": "e2e-watch", "push_server": NTFY})
server.kill()
server.wait(timeout=10)
server = start_server({"PORCHLIGHT_WATCHDOG": "2"})
# The rtsp camera points at an unreachable address: turning it on makes a
# camera that should capture but can't.
call("camera/mode", {"id": rtsp_id, "function": "Monitor"})


def offline_alert():
    for _ in range(30):
        if any("e2e-watch" in h["path"] and b"stopped" in h["body"] for h in ntfy_hits):
            return
        time.sleep(1)
    bad("no offline alert within 30s")


check("dead camera raises a phone alert", offline_alert)
call("camera/mode", {"id": rtsp_id, "function": "None"})


def disk_guard():
    rows = zmapi.sql("SELECT Background FROM Filters WHERE Name='PurgeWhenFull'")
    if not rows or rows[0][0] != "1":
        bad(str(rows))


check("disk guard filter runs in the background", disk_guard)
for r in get("rules"):
    if r["name"] == "E2E watch":
        call("rule/delete", {"id": r["id"]})

# --- person detection --------------------------------------------------------

print("person detection")
import detect  # noqa: E402  (sys.path + PORCHLIGHT_MODEL set at the top)


def person_in_demo():
    if not detect.available():
        bad("onnxruntime or the model is missing in this container")
    clip = "/var/cache/zoneminder/demo/garage.mp4"
    if not os.path.isfile(clip):
        return                                   # demo clips not staged
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", clip,
                        "-frames:v", "1", "-f", "mjpeg", "-"], capture_output=True)
    conf, box = detect.person(p.stdout)
    if conf < 0.35 or not box:
        bad("person score %.2f" % conf)
    detect.draw_box(p.stdout, box, "/tmp/e2e-objdetect.jpg")
    with open("/tmp/e2e-objdetect.jpg", "rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            bad("draw_box wrote no JPEG")


check("somebody in the demo clip is seen", person_in_demo)


def test_pattern_is_nobody():
    p = subprocess.run(["ffmpeg", "-v", "error", "-i",
                        "/var/cache/zoneminder/testsrc/a.mp4",
                        "-frames:v", "1", "-f", "mjpeg", "-"], capture_output=True)
    conf, _ = detect.person(p.stdout)
    if conf:
        bad("saw a person in a test pattern: %.2f" % conf)


check("test pattern shows nobody", test_pattern_is_nobody)


def people_only_skips():
    call("camera/people-only", {"id": ids[0], "on": True})
    r = push_form({"topic": "e2e-person", "server": NTFY, "dir": push_dir})
    call("camera/people-only", {"id": ids[0], "on": False})
    if r.get("skipped") != "no person":
        bad(str(r))


check("people-only camera skips person-free events", people_only_skips)

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

# --- share links + backup ----------------------------------------------------

print("share and backup")
check("share refuses while phones are off",
      lambda: call("share", {"id": 1}).get("ok") is False or bad("shared with lan off"))

# Turning LAN viewing on re-execs the server on 0.0.0.0; wait for it to return.
call("access/save", {"lan": True, "password": "e2e-share-pw"})
time.sleep(2)
for _ in range(40):
    try:
        urllib.request.urlopen(BASE + "/api/status", timeout=5)
        break
    except Exception:
        time.sleep(0.5)


def share_link_works():
    evs = get("events", {"limit": 1})["events"]
    if not evs:
        bad("no event to share")
    r = call("share", {"id": evs[0]["id"]})
    if not r.get("ok"):
        bad(str(r))
    token = r["url"].rsplit("/share/", 1)[1]
    eid = str(evs[0]["id"])
    page = fetch(BASE + "/share/" + token, 4000)
    if b"<video" not in page:
        bad(str(page[:100]))
    if b"ftyp" not in fetch(BASE + "/zm/index.php?view=view_video&eid=%s&share=%s"
                            % (eid, token), 4000):
        bad("share video did not play")
    # The gate itself, called directly: this container has no /24 address, so
    # a stranger's-eye HTTP request can't be made here (loopback is trusted).
    import hmac as hm
    import porchlight_server as ps
    if ps.share_eid(token) != eid:
        bad("valid token did not validate")
    if not ps.share_ok("/zm/index.php?view=view_video&eid=%s&share=%s" % (eid, token)):
        bad("valid share request refused")
    if ps.share_ok("/zm/index.php?view=view_video&eid=999999&share=%s" % token):
        bad("token unlocked a different event")
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    if ps.share_eid(tampered) is not None:
        bad("tampered signature validated")
    secret = ps.load_config()["share_secret"]
    stale = "%s.%d" % (eid, int(time.time()) - 10)
    expired = stale + "." + hm.new(secret.encode(), stale.encode(), "sha256").hexdigest()
    if ps.share_eid(expired) is not None:
        bad("expired token validated")


check("share link plays, tampered and expired tokens do not", share_link_works)


def backup_restores():
    blob = fetch(BASE + "/api/backup", 10 ** 7)
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = {m.name for m in tar.getmembers()}
    if not {"config.json", "monitors.json", "rules.json"} <= names:
        bad(str(names))
    victim = [c for c in get("cameras") if c["id"] == ids[1]][0]
    call("camera/delete", {"id": ids[1]})
    req = urllib.request.Request(BASE + "/api/restore", data=blob)
    r = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    if not r.get("ok"):
        bad(str(r))
    back = [c for c in get("cameras") if c["name"] == victim["name"]]
    if not back:
        bad("the deleted camera did not come back")
    ids[1] = back[0]["id"]


check("backup round-trips a deleted camera", backup_restores)

call("access/save", {"lan": False})
time.sleep(2)
for _ in range(40):
    try:
        urllib.request.urlopen(BASE + "/api/status", timeout=5)
        break
    except Exception:
        time.sleep(0.5)

# --- cleanup -----------------------------------------------------------------

for mid in ids:
    call("camera/delete", {"id": mid})
check("cameras removed", lambda: len(get("cameras")) == 0 or bad(str(get("cameras"))))

server.kill()
server.wait(timeout=10)
try:
    # Leftover LAN config breaks the next run's access checks.
    os.remove(os.path.expanduser("~/.config/porchlight/config.json"))
except OSError:
    pass

print()
if failures:
    print("E2E FAILED (%d)" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("E2E OK")
