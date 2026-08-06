#!/usr/bin/env python3
"""Porchlight local server: serves the web UI and a small JSON API in front of ZoneMinder.

Listens on 127.0.0.1 only. Run directly, or via the `porchlight` launcher.
"""

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import tarfile
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detect
import zmapi

PORT = int(os.environ.get("PORCHLIGHT_PORT", "8321"))
WEB_DIR = os.environ.get("PORCHLIGHT_WEBDIR",
                         os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))
ADMIN = "/usr/lib/porchlight/porchlight-admin"
CONFIG = os.environ.get("PORCHLIGHT_CONFIG",
                        os.path.expanduser("~/.config/porchlight/config.json"))

TYPES = {".html": "text/html", ".css": "text/css", ".js": "application/javascript",
         ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/manifest+json"}

_scan = {"running": False, "found": [], "done": False, "stage": ""}

# Signed-in phones, by cookie value. Lost on restart, which is fine: signing in
# again is one screen.
_sessions = set()


def load_config():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as fh:
        json.dump(cfg, fh)
    os.chmod(CONFIG, 0o600)


def password_hash(password):
    return hashlib.sha256(("porchlight:" + password).encode()).hexdigest()


def admin(*args):
    """Run a privileged helper action through pkexec."""
    cmd = ["pkexec", ADMIN] + list(args) if os.geteuid() else [ADMIN] + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return {"ok": p.returncode == 0, "out": (p.stdout + p.stderr)[-4000:]}


def monitors():
    return [m["Monitor"] for m in zmapi.api("monitors.json").get("monitors", [])]


def zones_of(mid):
    return [z["Zone"] for z in zmapi.api("zones/forMonitor/%s.json" % mid).get("zones", [])]


# --- endpoint handlers: each returns a JSON-able object ------------------------

def h_status(_):
    out = {"cameras": [], "storage": {},
           "snooze_until": load_config().get("snooze_until", 0)}
    try:
        v = zmapi.api("host/getVersion.json")
        out["ok"] = True
        out["version"] = v.get("version", "")
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
        return out
    try:
        out["daemon"] = zmapi.api("host/daemonCheck.json").get("result") in (1, "1")
    except Exception:
        out["daemon"] = None
    try:
        states = [s.get("State", s) for s in zmapi.api("states.json").get("states") or []]
        active = [s for s in states if s.get("IsActive") in ("1", 1)]
        out["state"] = (active or states or [{}])[0].get("Name", "")
    except Exception:
        pass
    try:
        out["today"] = int(zmapi.sql("SELECT COUNT(*) FROM Events "
                                     "WHERE StartDateTime >= CURDATE()")[0][0])
    except Exception:
        out["today"] = None
    d = zmapi.EVENTS_DIR if os.path.isdir(zmapi.EVENTS_DIR) else "/"
    u = shutil.disk_usage(d)
    out["storage"] = {"free": u.free, "total": u.total, "used": u.used}
    return out


def last_events():
    """monitor id -> epoch of its newest recording. Empty when the DB is unreachable."""
    try:
        rows = zmapi.sql("SELECT MonitorId, UNIX_TIMESTAMP(MAX(StartDateTime)) "
                         "FROM Events GROUP BY MonitorId")
    except Exception:
        return {}
    return {r[0]: int(r[1]) for r in rows if len(r) == 2 and r[1] not in ("NULL", "")}


_swept = False


def h_cameras(_):
    global _swept
    monitors = zmapi.api("monitors.json").get("monitors", [])
    if not _swept:
        _swept = True
        for row in monitors:
            m = row["Monitor"]
            # Our old creation default; it throttles zmc's read loop on stream
            # sources and smears frames. Clear that exact value only, so a cap
            # someone set on purpose survives. Local files keep it: there the
            # cap is what paces playback (see zmapi.source_fields).
            if (m.get("Type") == "Ffmpeg" and str(m.get("MaxFPS")) in ("10", "10.00")
                    and not (m.get("Path") or "").startswith("/")):
                try:
                    zmapi.api("monitors/%s.json" % m["Id"],
                              data={"Monitor[MaxFPS]": ""}, method="PUT")
                    m["MaxFPS"] = None
                except Exception:
                    _swept = False          # retry on the next visit
    out = []
    last = last_events()
    for el in monitors:
        m = el["Monitor"]
        out.append({
            "status": zmapi.monitor_status(m, (el.get("Monitor_Status") or {})),
            "last": last.get(str(m["Id"])),
            "id": m["Id"], "name": m.get("Name"), "function": m.get("Function"),
            "enabled": m.get("Enabled") in ("1", 1), "type": m.get("Type"),
            "width": m.get("Width"), "height": m.get("Height"),
            "controllable": m.get("Controllable") in ("1", 1),
            "snapshot": zmapi.snapshot_url(m["Id"]),
            "stream": zmapi.stream_url(m["Id"]),
        })
    return out


def h_camera(q):
    mid = q["id"]
    m = zmapi.api("monitors/%s.json" % mid)["monitor"]["Monitor"]
    return {"monitor": m, "zones": zones_of(mid),
            "smart": detect.available(),
            "people_only": str(mid) in (load_config().get("people_only") or []),
            "stream": zmapi.stream_url(mid, scale=60, maxfps=10)}


def h_camera_save(body):
    mid = body.pop("id")
    fields = {"Monitor[%s]" % k: str(v) for k, v in body.items()}
    zmapi.api("monitors/%s.json" % mid, data=fields, method="PUT")
    return {"ok": True}


def h_camera_add(body):
    name = body.get("name") or "Camera"
    kind = body.get("kind", "rtsp")
    fields = zmapi.monitor_fields(name, body.get("path", ""), body.get("function", "Modect"))
    src = zmapi.source_fields(kind, body.get("path", ""),
                              body.get("width", 1920), body.get("height", 1080))
    fields.update({"Monitor[%s]" % k: str(v) for k, v in src.items()})
    if kind != "rtsp":
        fields.pop("Monitor[Method]", None)
    zmapi.api("monitors.json", data=fields)
    # ZoneMinder answers {"message":"Saved"} with no id, so find the row we just made.
    mine = [m for m in monitors() if m.get("Name") == name]
    new_id = max((int(m["Id"]) for m in mine), default=None)
    return {"ok": True, "id": new_id}


def sample_clip():
    """A short generated video, so the app can be tried without owning a camera."""
    directory = "/var/cache/zoneminder" if os.access("/var/cache/zoneminder", os.W_OK) else "/tmp"
    path = os.path.join(directory, "porchlight-sample.mp4")
    if os.path.isfile(path):
        return path
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=1280x720:rate=15", "-t", "30",
                    "-vf", "drawtext=text='Sample camera %{pts\\:hms}':x=20:y=20:fontsize=36:fontcolor=white",
                    "-pix_fmt", "yuv420p", path],
                   capture_output=True, text=True, timeout=120, check=True)
    os.chmod(path, 0o644)
    return path


def h_camera_sample(_):
    """Add a fake camera that plays a generated clip."""
    path = sample_clip()
    return h_camera_add({"name": "Sample camera", "kind": "file", "path": path,
                         "width": 1280, "height": 720, "function": "Monitor"})


def h_camera_delete(body):
    zmapi.api("monitors/%s.json" % body["id"], method="DELETE", data={})
    return {"ok": True}


def h_camera_mode(body):
    zmapi.api("monitors/%s.json" % body["id"],
              data={"Monitor[Function]": body["function"]}, method="PUT")
    return {"ok": True}


def h_sensitivity(body):
    pct = dict(zmapi.SENSITIVITY)[body["level"]]
    for z in zones_of(body["id"]):
        area = int(z.get("Area") or 0)
        if not area:
            continue
        zmapi.api("zones/%s.json" % z["Id"], method="PUT", data={
            "Zone[MinAlarmPixels]": str(area * pct // 100),
            "Zone[MaxAlarmPixels]": str(area),
        })
    return {"ok": True}


def h_scan_start(_):
    if _scan["running"]:
        return {"running": True}
    _scan.update(running=True, found=[], done=False, stage="onvif")

    def work():
        try:
            text = zmapi.run_probe(["probe"], timeout=25)
        except Exception:
            text = ""
        found = [{"label": a, "url": b, "host": zmapi.host_of(b)} for a, b in zmapi.parse_probe(text)]
        _scan.update(found=found, stage="sweep")

        # Cameras that ignore ONVIF discovery still answer on their RTSP port.
        known = {f["host"] for f in found}
        for ip in zmapi.lan_addresses():
            for host, port in zmapi.sweep(zmapi.subnet_hosts(ip)):
                if host in known:
                    continue
                known.add(host)
                found.append({"label": "Camera at %s (port %d)" % (host, port),
                              "url": "", "host": host, "port": port})
        _scan.update(found=found, running=False, done=True, stage="done")

    threading.Thread(target=work, daemon=True).start()
    return {"running": True}


def h_scan_result(_):
    return dict(_scan)


def h_probe_profiles(body):
    user, password = body.get("user", ""), body.get("password", "")
    url = None
    if body.get("onvif_url"):
        try:
            url = zmapi.parse_profiles(zmapi.run_probe(
                ["profiles", body["onvif_url"], user, password], timeout=25))
        except Exception:
            url = None
        # ONVIF hands back the URL without a login; ffmpeg needs one or it gets a 401.
        url = zmapi.with_credentials(url, user, password)
    if url and zmapi.rtsp_works(url):
        return {"path": url, "verified": True}
    # No ONVIF, or ONVIF named a stream the camera won't actually serve us.
    host = body.get("host", "") or zmapi.host_of(body.get("onvif_url", ""))
    url = zmapi.find_rtsp(host, user, password, body.get("port") or 554)
    if zmapi.rtsp_works(url):
        return {"path": url, "verified": True}
    # Saving an address the camera refuses only produces a card that says "Offline"
    # with no reason, so say what went wrong while the login is still on screen.
    return {"path": url, "verified": False,
            "error": zmapi.rtsp_refusal(host, body.get("port") or 554, user, password)}


def h_webcams(_):
    return zmapi.list_webcams()


def h_events(q):
    parts = []
    if q.get("camera"):
        parts.append("MonitorId:%s" % q["camera"])
    if q.get("from"):
        parts.append("StartTime >=:%s" % q["from"])
    if q.get("to"):
        parts.append("StartTime <=:%s" % q["to"])
    if q.get("cause"):
        parts.append("Cause:%s" % q["cause"])
    if q.get("archived"):
        parts.append("Archived:1")
    path = "events"
    if parts:
        path += "/index/" + "/".join(parts)
    sort = "MaxScore" if q.get("sort") == "score" else "StartTime"
    path += ".json?sort=%s&direction=desc&limit=%s&page=%s" % (
        sort, q.get("limit", "60"), q.get("page", "1"))
    r = zmapi.api(path)
    out = []
    for e in r.get("events", []):
        ev = e["Event"]
        out.append({
            "id": ev["Id"], "monitor": ev.get("MonitorId"), "name": ev.get("Name"),
            "start": ev.get("StartTime"), "length": ev.get("Length"),
            "frames": ev.get("Frames"), "alarm": ev.get("AlarmFrames"),
            "score": ev.get("MaxScore"),
            "cause": ev.get("Cause"), "archived": ev.get("Archived") in ("1", 1),
            "video": zmapi.video_url(ev["Id"]),
            "thumb": zmapi.thumb_url(ev["Id"]),
            "replay": zmapi.event_replay_url(ev["Id"]),
        })
    return {"events": out, "pagination": r.get("pagination", {})}


def h_event_action(body):
    eid = body["id"]
    if body["action"] == "delete":
        zmapi.api("events/%s.json" % eid, method="DELETE", data={})
    else:
        keep = "1" if body["action"] == "keep" else "0"
        zmapi.api("events/%s.json" % eid, data={"Event[Archived]": keep}, method="PUT")
    return {"ok": True}


def sql_sets(values):
    """`Col`='value' pairs. Columns are backticked: ZoneMinder uses reserved words."""
    return ", ".join("`%s`=%s" % (k, zmapi.quote(v)) for k, v in values.items())


RULE_COLUMNS = ["Name", "Query_json", "AutoEmail", "AutoArchive", "AutoDelete",
                "AutoExecute", "AutoExecuteCmd", "EmailTo"]


def h_rules(_):
    cols = ", ".join(["Id"] + RULE_COLUMNS)
    rules = []
    for row in zmapi.sql("SELECT %s FROM Filters ORDER BY Name" % cols):
        f = dict(zip(["Id"] + RULE_COLUMNS, row))
        f["Query"] = f.pop("Query_json")
        rules.append(zmapi.filter_to_rule(f))
    return rules


def h_rule_save(body):
    """Filters have no API endpoint in ZoneMinder 1.36, so write the row ourselves."""
    fields = zmapi.rule_to_filter(body)
    values = {c: fields.get("Filter[%s]" % ("Query" if c == "Query_json" else c),
                            "0" if c.startswith("Auto") and c != "AutoExecuteCmd" else "")
              for c in RULE_COLUMNS}
    sets = sql_sets(values)
    if body.get("id"):
        zmapi.sql("UPDATE Filters SET %s WHERE Id=%s" % (sets, zmapi.quote(body["id"])))
    else:
        zmapi.sql("INSERT INTO Filters SET %s" % sets)
    return {"ok": True}


def h_rule_delete(body):
    zmapi.sql("DELETE FROM Filters WHERE Id=%s" % zmapi.quote(body["id"]))
    return {"ok": True}


def h_configs(q):
    rows = [c["Config"] for c in zmapi.api("configs.json").get("configs", [])]
    needle = (q.get("q") or "").lower()
    if needle:
        rows = [r for r in rows if needle in (r.get("Name", "") + r.get("Prompt", "")).lower()]
    return [{"name": r.get("Name"), "value": r.get("Value"), "prompt": r.get("Prompt"),
             "type": r.get("Type"), "category": r.get("Category")} for r in rows[:400]]


def h_config_set(body):
    """Try the API first; fall back to the root helper (1.36 often rejects config PUTs)."""
    name, value = body["name"], str(body["value"])
    try:
        zmapi.api("configs/edit/%s.json" % name, data={"Config[Value]": value}, method="PUT")
        return {"ok": True, "via": "api"}
    except Exception:
        r = admin("set-config", name, value)
        r["via"] = "helper"
        return r


def h_states(_):
    return [s["State"] for s in zmapi.api("states.json").get("states", [])]


def h_state_apply(body):
    zmapi.api("states/change/%s.json" % body["name"])
    return {"ok": True}


def h_state_save(body):
    rows = [(r["id"], r["function"], "1" if r.get("enabled", True) else "0")
            for r in body["rows"]]
    fields = {"State[Name]": body["name"], "State[Definition]": zmapi.state_definition(rows)}
    try:
        zmapi.api("states.json", data=fields)
    except Exception:
        zmapi.api("states/%s.json" % body["name"], data=fields, method="PUT")
    return {"ok": True}


def h_schedule_save(body):
    entries = [(e["state"], e["time"], e.get("days", "*")) for e in body["entries"]]
    return admin("schedule", "\n".join(zmapi.cron_lines(entries)))


def h_users(_):
    return [u["User"] for u in zmapi.api("users.json").get("users", [])]


def h_user_save(body):
    """The users API answers 'Insufficient Privileges' in 1.36, so write the row."""
    level = "Edit" if body.get("role") == "manager" else "View"
    values = {"Username": body["username"], "Enabled": "1", "Stream": "View"}
    for k in ("Events", "Control", "Monitors", "System", "Groups", "Devices", "Snapshots"):
        values[k] = level
    if body.get("password"):
        values["Password"] = zmapi.mysql_password_hash(body["password"])
    sets = sql_sets(values)
    if body.get("id"):
        zmapi.sql("UPDATE Users SET %s WHERE Id=%s" % (sets, zmapi.quote(body["id"])))
    else:
        zmapi.sql("INSERT INTO Users SET %s" % sets)
    return {"ok": True}


def h_user_delete(body):
    zmapi.sql("DELETE FROM Users WHERE Id=%s" % zmapi.quote(body["id"]))
    return {"ok": True}


def h_login(body):
    zmapi.set_credentials(body["user"], body["password"])
    try:
        zmapi.token()
    except Exception as e:
        zmapi.clear_credentials()
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def h_zone_save(body):
    points = [tuple(p) for p in body["points"]]
    fields = zmapi.zone_fields(body["monitor"], body.get("name") or "Zone",
                               body.get("type", "Active"), points,
                               dict(zmapi.SENSITIVITY).get(body.get("level", "Normal"), 6))
    for k, v in (body.get("advanced") or {}).items():
        fields["Zone[%s]" % k] = str(v)
    if body.get("id"):
        zmapi.api("zones/%s.json" % body["id"], data=fields, method="PUT")
    else:
        zmapi.api("zones.json", data=fields)
    return {"ok": True}


def h_zone_delete(body):
    zmapi.api("zones/%s.json" % body["id"], method="DELETE", data={})
    return {"ok": True}


def h_ptz(body):
    """Pan/tilt/zoom through ZM's control request endpoint."""
    import urllib.parse
    import urllib.request
    args = {"view": "request", "request": "control", "id": body["id"],
            "control": body["command"], "xge": "30", "yge": "30"}
    if body.get("preset"):
        args["preset"] = body["preset"]
    # Server-side fetch, so this needs ZoneMinder's real address, not the
    # app-relative /zm/ form that media_url now returns for browsers.
    url = zmapi._with_token(zmapi.ZM_WEB + "/index.php?" + urllib.parse.urlencode(args))
    with urllib.request.urlopen(url, timeout=10) as r:
        r.read(200)
    return {"ok": True}


def h_restart(_):
    return admin("restart")


def h_logs(q):
    return admin("logs", str(int(q.get("lines", "200"))))


def h_test_email(body):
    return admin("test-email", body.get("to", ""))


def h_test_push(body):
    """Send one alert through the same script the rules use."""
    p = subprocess.run([zmapi.PUSH_SCRIPT, body.get("topic", ""),
                        body.get("server", "https://ntfy.sh"),
                        "Test alert", "Your cameras can reach this phone."],
                       capture_output=True, text=True, timeout=30)
    return {"ok": p.returncode == 0, "out": (p.stdout + p.stderr)[-500:]}


# --- phone alerts ------------------------------------------------------------
# ZoneMinder filters run push.sh per event; it hands the event here so the
# decisions (cooldown, snooze, snapshot, priority) live in one place.

_push_last = {}          # monitor id -> epoch of the last alert sent for it


def snapshot_jpeg(eid):
    """The event's best frame as JPEG bytes, or None.

    Fetched from ZoneMinder over HTTP rather than read from the event
    directory: ZM builds snapshot.jpg from the mp4 on first request, so this
    works whether or not the file exists yet. A still-open event may not
    answer right away, hence the retries.
    """
    url = zmapi._with_token("%s/index.php?view=image&eid=%s&fid=snapshot&width=1280"
                            % (zmapi.ZM_WEB, eid))
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = r.read()
            if data[:2] == b"\xff\xd8":
                return data
        except Exception:
            pass
        time.sleep(2)
    return None


def send_ntfy(server, topic, title, message, priority, jpeg=None, click=""):
    """Deliver one ntfy notification, snapshot attached when there is one.

    Text rides in query parameters (ntfy accepts them UTF-8 percent-encoded,
    where headers would choke on non-ASCII), leaving the body free for the
    attachment.
    """
    q = {"title": title, "priority": str(priority)}
    if click:
        q["click"] = click
    if jpeg:
        q["message"] = message
        q["filename"] = "snapshot.jpg"
    url = "%s/%s?%s" % (server.rstrip("/"), topic, urllib.parse.urlencode(q))
    req = urllib.request.Request(url, data=jpeg if jpeg else message.encode(),
                                 method="PUT" if jpeg else "POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read(200)
        return True
    except Exception:
        return False


def event_dir_ids(path):
    """(monitor id, event id) out of ZM's events/<mid>/<date>/<eid> layout."""
    parts = (path or "").strip("/").split("/")
    if "events" not in parts:
        return None, None
    rest = parts[parts.index("events") + 1:]
    mid = rest[0] if rest and rest[0].isdigit() else None
    eid = rest[-1] if len(rest) >= 2 and rest[-1].isdigit() else None
    return mid, eid


def h_push(body):
    """One alert, decided here. Loopback only; phones never reach this."""
    topic = re.sub(r"[^A-Za-z0-9_-]", "_", body.get("topic") or "")
    if not topic:
        return {"ok": False, "error": "no topic"}
    server = body.get("server") or zmapi.NTFY_SERVER
    now = time.time()
    cfg = load_config()
    if now < cfg.get("snooze_until", 0):
        return {"ok": True, "skipped": "snoozed"}
    mid, eid = event_dir_ids(body.get("dir"))
    if mid and now - _push_last.get(mid, 0) < 60:
        return {"ok": True, "skipped": "cooldown"}

    title, message, priority, jpeg = "Camera alert", body.get("message") or "", 3, None
    when = time.strftime("%H:%M")
    if eid:
        try:
            ev = zmapi.api("events/%s.json" % eid)["event"]["Event"]
            names = {str(m["Id"]): m.get("Name") for m in monitors()}
            title = names.get(str(ev.get("MonitorId")), title)
            when = (ev.get("StartTime") or "")[11:16] or when
            message = "%s at %s" % (ev.get("Cause") or "Movement", when)
            if float(ev.get("MaxScore") or 0) >= 50:
                priority = 4
        except Exception:
            pass
        jpeg = snapshot_jpeg(eid)

    # No snapshot means nobody can tell who is there: alert anyway (fail open)
    # rather than let a broken snapshot pipeline silence the phone.
    if jpeg and detect.available():
        conf, box = detect.person(jpeg)
        if conf:
            priority = 5
            message = "Somebody seen at %s" % when
            if box and body.get("dir") and os.path.isdir(body["dir"]):
                try:
                    # ZoneMinder's event pages pick this file up by name.
                    detect.draw_box(jpeg, box, os.path.join(body["dir"], "objdetect.jpg"))
                except Exception:
                    pass                      # event dirs belong to www-data
        elif mid and mid in (cfg.get("people_only") or []):
            return {"ok": True, "skipped": "no person"}

    click = lan_url() + "/#recordings" if cfg.get("lan") else ""
    ok = send_ntfy(server, topic, title, message or "Something moved",
                   priority, jpeg, click)
    if ok and mid:
        _push_last[mid] = now
    return {"ok": ok}


def h_snooze(body):
    """Silence phone alerts for a while. minutes=0 turns them back on."""
    minutes = int(body.get("minutes") or 0)
    cfg = load_config()
    cfg["snooze_until"] = now = time.time() + minutes * 60 if minutes else 0
    save_config(cfg)
    return {"ok": True, "until": now}


def snoozed():
    return time.time() < load_config().get("snooze_until", 0)


def h_people_only(body):
    """Per-camera 'alert only when somebody is seen' toggle."""
    cfg = load_config()
    only = set(cfg.get("people_only") or [])
    (only.add if body.get("on") else only.discard)(str(body["id"]))
    cfg["people_only"] = sorted(only)
    save_config(cfg)
    return {"ok": True}


# --- share links --------------------------------------------------------------
# One recording, one signed link, three days. Nothing stored per link: the
# token carries the event id and expiry, signed with a local secret.

def share_secret():
    cfg = load_config()
    if not cfg.get("share_secret"):
        cfg["share_secret"] = secrets.token_hex(16)
        save_config(cfg)
    return cfg["share_secret"]


def share_eid(token):
    """The event id a valid, unexpired share token names, else None."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1].isdigit() or time.time() > int(parts[1]):
        return None
    secret = load_config().get("share_secret") or ""
    want = hmac.new(secret.encode(), ("%s.%s" % (parts[0], parts[1])).encode(),
                    "sha256").hexdigest()
    return parts[0] if secret and hmac.compare_digest(want, parts[2]) else None


def share_ok(full_path):
    """May this request pass without the sign-in cookie on a share token?"""
    u = urllib.parse.urlparse(full_path)
    if u.path.startswith("/share/"):
        return share_eid(u.path[len("/share/"):]) is not None
    if u.path.startswith("/zm/"):
        q = urllib.parse.parse_qs(u.query)
        eid = share_eid((q.get("share") or [""])[0])
        return eid is not None and (q.get("eid") or [""])[0] == eid
    return False


def h_share(body):
    """A link that shows one recording to somebody without the password."""
    if not load_config().get("lan"):
        return {"ok": False, "error": "Turn on “Watch from your phone” "
                                      "first: share links use your network address."}
    eid = str(int(body["id"]))
    expiry = str(int(time.time()) + 3 * 86400)
    sig = hmac.new(share_secret().encode(), ("%s.%s" % (eid, expiry)).encode(),
                   "sha256").hexdigest()
    return {"ok": True, "url": "%s/share/%s.%s.%s" % (lan_url(), eid, expiry, sig)}


# --- backup and restore -------------------------------------------------------

# Monitor columns worth carrying to a new install; ids and runtime state stay.
RESTORE_COLUMNS = [
    "Name", "Type", "Function", "Enabled", "Path", "Method", "Width", "Height",
    "Colours", "SaveJPEGs", "VideoWriter", "RecordAudio", "Device", "Channel",
    "Format", "Palette", "Protocol", "Host", "Port", "User", "Pass", "MaxFPS",
    "AlarmMaxFPS", "AnalysisFPS", "AlarmFrameCount", "PreEventCount",
    "PostEventCount", "SectionLength", "EventPrefix", "Controllable",
    "ControlId", "ControlDevice", "ControlAddress",
]


def backup_tar():
    """Cameras, rules and app settings as one tar.gz."""
    payload = {
        "config.json": json.dumps(load_config()),
        "monitors.json": json.dumps(monitors()),
        "rules.json": json.dumps(h_rules(None)),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in payload.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def restore_tar(blob):
    """Recreate missing cameras and rules from a backup. Existing rows stay."""
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            files = {m.name: tar.extractfile(m).read().decode()
                     for m in tar.getmembers()}
    except Exception:
        return {"ok": False, "error": "That does not look like a Porchlight backup."}
    added = 0
    have = {m.get("Name") for m in monitors()}
    saved = json.loads(files.get("monitors.json") or "[]")
    for m in saved:
        if m.get("Name") in have:
            continue
        fields = {"Monitor[%s]" % k: str(m[k]) for k in RESTORE_COLUMNS
                  if m.get(k) is not None}
        try:
            zmapi.api("monitors.json", data=fields)
            added += 1
        except Exception:
            pass
    # Rules point at monitor ids, which a fresh install hands out anew.
    name_to_id = {m.get("Name"): str(m["Id"]) for m in monitors()}
    idmap = {str(m["Id"]): name_to_id.get(m.get("Name")) for m in saved}
    have_rules = {r["name"] for r in h_rules(None)}
    for r in json.loads(files.get("rules.json") or "[]"):
        if r.get("name") in have_rules:
            continue
        r.pop("id", None)
        r["cameras"] = [idmap.get(str(c)) or c for c in (r.get("cameras") or [])]
        try:
            h_rule_save(r)
            added += 1
        except Exception:
            pass
    cfg = json.loads(files.get("config.json") or "{}")
    if cfg:
        keep = load_config()
        keep.update({k: v for k, v in cfg.items() if k != "lan"})  # rebinding is a choice
        save_config(keep)
    return {"ok": True, "added": added}


# --- watchdog ----------------------------------------------------------------
# ZoneMinder never says a word when a camera dies or the disk fills up; this
# thread does.

WATCHDOG_INTERVAL = int(os.environ.get("PORCHLIGHT_WATCHDOG", "60"))


def push_targets():
    """Every distinct (topic, server) the rules alert a phone on."""
    out = []
    try:
        for row in zmapi.sql("SELECT `AutoExecuteCmd` FROM Filters WHERE `AutoExecute`='1'"):
            t = zmapi.push_topic(row[0])
            if t and (t, zmapi.push_server(row[0])) not in out:
                out.append((t, zmapi.push_server(row[0])))
    except Exception:
        pass
    return out


def ensure_disk_guard():
    """A full disk silently stops recording. ZoneMinder ships a PurgeWhenFull
    filter for exactly this, but not every install has it running."""
    try:
        rows = zmapi.sql("SELECT Id, Background FROM Filters WHERE Name='PurgeWhenFull'")
        if rows:
            if rows[0][1] != "1":
                zmapi.sql("UPDATE Filters SET `Background`='1' WHERE Id=%s"
                          % zmapi.quote(rows[0][0]))
            return
        query = {"terms": [{"attr": "DiskPercent", "op": ">=", "val": "95"},
                           {"cnj": "and", "attr": "Archived", "op": "=", "val": "0"}],
                 "sort_field": "Id", "sort_asc": "1", "limit": "50"}
        zmapi.sql("INSERT INTO Filters SET " + sql_sets({
            "Name": "PurgeWhenFull", "Query_json": json.dumps(query),
            "AutoDelete": "1", "Background": "1", "AutoEmail": "0",
            "AutoArchive": "0", "AutoExecute": "0", "AutoExecuteCmd": "",
            "EmailTo": ""}))
    except Exception:
        pass


def watchdog():
    """Cameras that should be capturing but aren't get one phone alert after
    two consecutive misses, and one more when they come back."""
    ensure_disk_guard()
    down, told = {}, set()
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        try:
            rows = zmapi.api("monitors.json").get("monitors", [])
        except Exception:
            continue
        targets = push_targets()
        for row in rows:
            m = row["Monitor"]
            mid = str(m["Id"])
            status = zmapi.monitor_status(m, row.get("Monitor_Status") or {})
            if status == "offline":
                down[mid] = down.get(mid, 0) + 1
                if down[mid] == 2 and mid not in told:
                    told.add(mid)
                    if not snoozed():
                        for topic, server in targets:
                            send_ntfy(server, topic, m.get("Name") or "Camera",
                                      "This camera stopped responding.", 4)
            else:
                down[mid] = 0
                if mid in told:
                    told.discard(mid)
                    if status == "ok" and not snoozed():
                        for topic, server in targets:
                            send_ntfy(server, topic, m.get("Name") or "Camera",
                                      "Back online.", 3)


def h_timeline(q):
    """Recordings per hour of one day, for the heat strip above the list."""
    day = q.get("day") or time.strftime("%Y-%m-%d")
    where = ["StartDateTime >= %s" % zmapi.quote(day + " 00:00:00"),
             "StartDateTime <= %s" % zmapi.quote(day + " 23:59:59")]
    if q.get("camera"):
        where.append("MonitorId = %s" % zmapi.quote(q["camera"]))
    hours = [0] * 24
    for row in zmapi.sql("SELECT HOUR(StartDateTime), COUNT(*) FROM Events "
                         "WHERE %s GROUP BY 1" % " AND ".join(where)):
        hours[int(row[0])] = int(row[1])
    return {"hours": hours}


# --- viewing from a phone -----------------------------------------------------

def lan_url():
    ips = zmapi.lan_addresses()
    return "http://%s:%d" % (ips[0], PORT) if ips else ""


def h_access(_):
    cfg = load_config()
    return {"lan": bool(cfg.get("lan")), "has_password": bool(cfg.get("password")),
            "url": lan_url()}


def h_access_save(body):
    cfg = load_config()
    if body.get("password"):
        cfg["password"] = password_hash(body["password"])
    want_lan = bool(body.get("lan"))
    if want_lan and not cfg.get("password"):
        return {"ok": False,
                "error": "Set a password first: anyone on your Wi-Fi could watch otherwise."}
    was = bool(cfg.get("lan"))
    cfg["lan"] = want_lan
    save_config(cfg)
    if was != want_lan:
        restart_self()
    return {"ok": True, "url": lan_url(), "restarting": was != want_lan}


def restart_self():
    """Rebind on the other address. The launcher and browser both reconnect."""
    threading.Timer(0.5, lambda: os.execv(sys.executable,
                                          [sys.executable, os.path.abspath(__file__)])).start()


def h_session(_):
    return {"signed_in": True}


def h_signin(body):
    cfg = load_config()
    if not cfg.get("password") or password_hash(body.get("password", "")) != cfg["password"]:
        return {"ok": False, "error": "Wrong password."}
    token = secrets.token_urlsafe(24)
    _sessions.add(token)
    return {"ok": True, "token": token}


GET_ROUTES = {
    "status": h_status, "cameras": h_cameras, "camera": h_camera, "events": h_events,
    "rules": h_rules, "configs": h_configs, "states": h_states, "users": h_users,
    "scan": h_scan_result, "webcams": h_webcams, "logs": h_logs,
    "access": h_access, "session": h_session, "timeline": h_timeline,
    "modes": lambda q: [{"value": v, "label": l} for v, l in zmapi.MODES],
    "zonetypes": lambda q: [{"value": v, "label": l} for v, l in zmapi.ZONE_TYPES],
    "zonefields": lambda q: [{"name": n, "label": l, "options": o}
                             for n, l, o in zmapi.ZONE_ADVANCED],
}

POST_ROUTES = {
    "camera/add": h_camera_add, "camera/sample": h_camera_sample,
    "camera/save": h_camera_save, "camera/delete": h_camera_delete,
    "camera/mode": h_camera_mode, "camera/sensitivity": h_sensitivity,
    "scan/start": h_scan_start, "probe/profiles": h_probe_profiles,
    "event/action": h_event_action, "rule/save": h_rule_save, "rule/delete": h_rule_delete,
    "config/set": h_config_set, "state/apply": h_state_apply, "state/save": h_state_save,
    "schedule/save": h_schedule_save, "user/save": h_user_save, "user/delete": h_user_delete,
    "login": h_login, "zone/save": h_zone_save, "zone/delete": h_zone_delete,
    "ptz": h_ptz, "restart": h_restart, "test-email": h_test_email,
    "test-push": h_test_push, "access/save": h_access_save, "signin": h_signin,
    "push": h_push, "snooze": h_snooze, "camera/people-only": h_people_only,
    "share": h_share,
}

# What a phone may touch before it has signed in: the app shell and the sign-in
# call itself. Everything else needs a session.
PUBLIC_FILES = {"/", "", "/index.html", "/app.css", "/app.js", "/logo.png", "/manifest.json"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        if cookie:
            self.send_header("Set-Cookie",
                             "nfsession=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000" % cookie)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def allowed(self, path):
        """Loopback is trusted as before. Anything off this machine must sign in."""
        if self.local():
            return True
        if path in PUBLIC_FILES or path in ("/api/signin", "/api/session"):
            return True
        if share_ok(self.path):
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nfsession" and v in _sessions:
                return True
        return False

    def do_GET(self):
        import urllib.parse
        u = urllib.parse.urlparse(self.path)
        if not self.allowed(u.path):
            return self.send_json({"error": "sign in first", "signin": True}, 401)
        if u.path == "/api/session" and not self.local():
            return self.send_json({"signed_in": self.allowed("/api/status")})
        if u.path.startswith("/zm/"):
            return self.proxy_media()
        if u.path.startswith("/share/"):
            return self.share_page(u.path[len("/share/"):])
        if u.path == "/api/backup":
            return self.send_backup()
        if u.path.startswith("/api/"):
            fn = GET_ROUTES.get(u.path[5:])
            if not fn:
                return self.send_json({"error": "no such endpoint"}, 404)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                return self.send_json(fn(q))
            except Exception as e:
                return self.send_json({"error": describe(e)}, 500)
        self.serve_file(u.path)

    def do_POST(self):
        if not self.allowed(self.path):
            return self.send_json({"error": "sign in first", "signin": True}, 401)
        if self.path == "/api/push" and not self.local():
            # Only ZoneMinder's own filters (via push.sh) may raise alerts.
            return self.send_json({"error": "no such endpoint"}, 404)
        if self.path == "/api/restore":
            # The body is a tar.gz, not JSON; hand it over before decoding.
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return self.send_json(restore_tar(self.rfile.read(n)))
            except Exception as e:
                return self.send_json({"error": describe(e)}, 500)
        fn = POST_ROUTES.get(self.path[5:]) if self.path.startswith("/api/") else None
        if not fn:
            return self.send_json({"error": "no such endpoint"}, 404)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        try:
            if raw.lstrip().startswith("{") or not raw:
                body = json.loads(raw or "{}")
            else:
                # push.sh posts curl form fields; everything else sends JSON.
                body = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        except ValueError:
            return self.send_json({"error": "bad request body"}, 400)
        try:
            r = fn(body)
            if self.path == "/api/signin" and r.get("ok"):
                return self.send_json({"ok": True}, cookie=r["token"])
            return self.send_json(r)
        except Exception as e:
            return self.send_json({"error": describe(e)}, 500)

    def proxy_media(self):
        """Relay one ZoneMinder media request, streamed through as it arrives.

        Media URLs are app-relative (/zm/...), so the browser only ever talks
        to this server: phones on the LAN can reach camera video, and the
        sign-in check above covers it like everything else.
        """
        if ".." in self.path:
            return self.send_json({"error": "bad path"}, 404)
        req = urllib.request.Request(zmapi.ZM_WEB + self.path[len("/zm"):])
        if self.headers.get("Range"):
            req.add_header("Range", self.headers["Range"])   # mp4 seeking on phones
        try:
            upstream = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            return self.send_json({"error": "ZoneMinder said %s" % e.code}, e.code)
        except Exception:
            return self.send_json({"error": "ZoneMinder is not answering"}, 502)
        with upstream:
            # Live streams have no length, so no keep-alive on this connection.
            self.close_connection = True
            self.send_response(upstream.status)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if upstream.headers.get(h):
                    self.send_header(h, upstream.headers[h])
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = upstream.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except OSError:
                pass                          # viewer closed the tab mid-stream

    def share_page(self, token):
        """A bare player page for one shared recording."""
        eid = share_eid(token)
        if not eid:
            return self.send_json({"error": "This link has expired."}, 404)
        video = zmapi.media_url("/index.php?view=view_video&eid=%s" % eid) \
            + "&share=" + token
        page = ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<title>Shared recording</title></head>"
                "<body style=\"margin:0;background:#000\">"
                "<video src=\"%s\" controls autoplay playsinline "
                "style=\"width:100%%;height:100vh\"></video></body></html>" % video)
        data = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_backup(self):
        data = backup_tar()
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition",
                         'attachment; filename="porchlight-backup.tar.gz"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_file(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, name))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(os.path.splitext(full)[1], "text/plain"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def describe(e):
    """Plain-ish error text; HTTP errors from ZM carry their body, which helps."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            return "ZoneMinder said: %s %s" % (e.code, e.read().decode()[:300])
        except Exception:
            return "ZoneMinder said: %s" % e.code
    return str(e) or e.__class__.__name__


def main():
    host = "0.0.0.0" if load_config().get("lan") else "127.0.0.1"
    srv = ThreadingHTTPServer((host, PORT), Handler)
    threading.Thread(target=watchdog, daemon=True).start()
    print("Porchlight on http://%s:%d" % (host, PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
