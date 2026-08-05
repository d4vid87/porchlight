#!/usr/bin/env python3
"""Porchlight local server: serves the web UI and a small JSON API in front of ZoneMinder.

Listens on 127.0.0.1 only. Run directly, or via the `porchlight` launcher.
"""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
    out = {"cameras": [], "storage": {}}
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


def h_cameras(_):
    out = []
    last = last_events()
    for el in zmapi.api("monitors.json").get("monitors", []):
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
    path += ".json?sort=StartTime&direction=desc&limit=%s&page=%s" % (
        q.get("limit", "60"), q.get("page", "1"))
    r = zmapi.api(path)
    out = []
    for e in r.get("events", []):
        ev = e["Event"]
        out.append({
            "id": ev["Id"], "monitor": ev.get("MonitorId"), "name": ev.get("Name"),
            "start": ev.get("StartTime"), "length": ev.get("Length"),
            "frames": ev.get("Frames"), "alarm": ev.get("AlarmFrames"),
            "cause": ev.get("Cause"), "archived": ev.get("Archived") in ("1", 1),
            "video": zmapi.video_url(ev["Id"]),
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
    url = zmapi.media_url("/index.php?" + urllib.parse.urlencode(args))
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
    "access": h_access, "session": h_session,
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
        fn = POST_ROUTES.get(self.path[5:]) if self.path.startswith("/api/") else None
        if not fn:
            return self.send_json({"error": "no such endpoint"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except ValueError:
            return self.send_json({"error": "bad request body"}, 400)
        try:
            r = fn(body)
            if self.path == "/api/signin" and r.get("ok"):
                return self.send_json({"ok": True}, cookie=r["token"])
            return self.send_json(r)
        except Exception as e:
            return self.send_json({"error": describe(e)}, 500)

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
    print("Porchlight on http://%s:%d" % (host, PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
