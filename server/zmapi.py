"""Talk to ZoneMinder: REST API, token auth, zmonvif-probe.pl, and the pure
request-building helpers. No UI or HTTP-serving code here."""

import glob
import hashlib
import json
import os
import random
import re
import shlex
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

ZM_WEB = os.environ.get("PORCHLIGHT_WEB", "http://localhost/zm")
ZM_API = os.environ.get("PORCHLIGHT_API", ZM_WEB + "/api")
PROBE = "/usr/bin/zmonvif-probe.pl"
EVENTS_DIR = "/var/cache/zoneminder/events"

# Plain language for every ZoneMinder monitor Function, in menu order.
MODES = [
    ("Modect", "Record on movement"),
    ("Record", "Record always"),
    ("Mocord", "Record always, mark movement"),
    ("Nodect", "Record when triggered"),
    ("Monitor", "Watch only"),
    ("None", "Off"),
]

# Plain language -> percent of a zone that must change to count as motion.
SENSITIVITY = [("Low", 12), ("Normal", 6), ("High", 3)]

ZONE_TYPES = [
    ("Active", "Watch this area for movement"),
    ("Inclusive", "Only counts with another area"),
    ("Exclusive", "Only counts on its own"),
    ("Preclusive", "Movement here cancels the alarm"),
    ("Inactive", "Ignore this area"),
    ("Privacy", "Black this area out"),
]


# --- auth ---------------------------------------------------------------------

_auth = {"user": None, "password": None, "access": None, "until": 0}


def set_credentials(user, password):
    _auth.update(user=user, password=password, access=None, until=0)


def clear_credentials():
    _auth.update(user=None, password=None, access=None, until=0)


def _login():
    body = urllib.parse.urlencode({"user": _auth["user"], "pass": _auth["password"]}).encode()
    with urllib.request.urlopen(ZM_API + "/host/login.json", data=body, timeout=15) as r:
        j = json.loads(r.read().decode())
    _auth["access"] = j.get("access_token")
    _auth["until"] = time.time() + int(j.get("access_token_expires") or 3600) - 60


def token():
    """Current ZM access token, or None when we hold no credentials."""
    if not _auth["user"]:
        return None
    if not _auth["access"] or time.time() > _auth["until"]:
        _login()
    return _auth["access"]


def _with_token(url):
    t = token()
    if t:
        url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(t)
    return url


# --- API + media URLs ---------------------------------------------------------

def api(path, data=None, method=None):
    """Call the ZoneMinder API. data is a dict of form fields, or None for GET."""
    url = _with_token("%s/%s" % (ZM_API, path.lstrip("/")))
    body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode()
    return json.loads(text) if text.strip() else {}


def db_config():
    """ZoneMinder's own DB credentials, from /etc/zm."""
    conf = {}
    files = ["/etc/zm/zm.conf"] + sorted(glob.glob("/etc/zm/conf.d/*.conf"))
    for path in files:
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("ZM_DB_") and "=" in line:
                        k, v = line.strip().split("=", 1)
                        conf[k] = v
        except OSError:
            pass
    return conf


def quote(value):
    """Single-quoted SQL literal. Only ever used for values, never identifiers."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def sql(statement):
    """Run one statement against the ZoneMinder database, rows back as lists.

    ponytail: the 1.36 API has no filters endpoint and refuses user writes, so
    those two features go straight to the DB with ZoneMinder's own credentials.
    """
    c = db_config()
    cmd = ["mysql", "-h", c.get("ZM_DB_HOST", "localhost"), "-u", c.get("ZM_DB_USER", "zmuser"),
           "-p" + c.get("ZM_DB_PASS", ""), c.get("ZM_DB_NAME", "zm"),
           "-N", "-B", "--default-character-set=utf8mb4", "-e", statement]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr.strip().splitlines() or ["mysql failed"])[-1])
    return [line.split("\t") for line in p.stdout.splitlines()]


def media_url(path_query):
    """Absolute ZM URL for <img>/<video>, token appended when auth is on."""
    return _with_token(ZM_WEB + path_query)


def connkey():
    """A unique id per stream. Two live panes sharing one connkey kill each other."""
    return random.randint(100000, 999999)


def stream_url(mid, scale=50, maxfps=5):
    return media_url("/cgi-bin/nph-zms?mode=jpeg&monitor=%s&scale=%s&maxfps=%s&connkey=%d"
                     % (mid, scale, maxfps, connkey()))


def snapshot_url(mid, scale=40):
    return media_url("/cgi-bin/nph-zms?mode=single&monitor=%s&scale=%s&rand=%d"
                     % (mid, scale, time.time()))


def video_url(eid):
    return media_url("/index.php?view=view_video&eid=%s" % eid)


def thumb_url(eid):
    return media_url("/index.php?view=image&eid=%s&fid=snapshot&width=640" % eid)


def event_replay_url(eid, scale=100):
    return media_url("/cgi-bin/nph-zms?mode=jpeg&source=event&event=%s&scale=%s&replay=single&connkey=%d"
                     % (eid, scale, connkey()))


def frame_url(eid, fid=1, scale=25):
    return media_url("/index.php?view=image&eid=%s&fid=%s&scale=%s" % (eid, fid, scale))


# --- monitors -----------------------------------------------------------------

def monitor_status(m, ms):
    """'ok' (capturing), 'off' (turned off on purpose), or 'offline' (should run, isn't).

    ponytail: ZoneMinder 1.36's zmc never writes Status='NotRunning' -- its UPDATE
    is malformed SQL -- and zmstats.pl deletes Monitor_Status rows older than a
    minute, so a dead capture shows up as a missing/stale row. Same heuristic as
    ZoneMinder's own console.php.
    """
    if m.get("Function") == "None" or m.get("Enabled") not in ("1", 1):
        return "off"
    if m.get("Type") == "WebSite":
        return "ok"                       # nothing to capture, so nothing to be down
    ms = ms or {}
    if ms.get("Status") != "Connected":
        return "offline"
    return "offline" if str(ms.get("CaptureFPS") or "0") in ("0", "0.00") else "ok"


def monitor_fields(name, url, function, prefix="Monitor"):
    """Form fields for POST /api/monitors.json. Conservative, works for any RTSP camera."""
    f = {
        "Name": name,
        "Type": "Ffmpeg",
        "Function": function,
        "Enabled": "1",
        "Path": url,
        "Method": "rtpRtsp",
        "Width": "1920",
        "Height": "1080",
        "Colours": "4",
        "MaxFPS": "10",
        "SaveJPEGs": "0",
        "VideoWriter": "1",  # store the camera's own video stream, no re-encoding
        "RecordAudio": "0",
    }
    return {"%s[%s]" % (prefix, k): v for k, v in f.items()}


# Extra source kinds beyond the ONVIF/RTSP wizard. value -> (ZM Type, path field label)
SOURCE_KINDS = {
    "rtsp": ("Ffmpeg", "Camera video address (rtsp://...)"),
    "webcam": ("Local", "Device (/dev/video0)"),
    # ponytail: ZoneMinder's own "File" type never primes capture in 1.36, and
    # ffmpeg reads still images and videos alike, so files go through Ffmpeg too.
    "file": ("Ffmpeg", "Video or image file on this computer"),
    "mjpeg": ("Remote", "Picture address (http://...)"),
}


def source_fields(kind, path, width, height):
    """Type-specific monitor fields for the 'more camera types' paths."""
    ztype = SOURCE_KINDS[kind][0]
    f = {"Type": ztype, "Width": str(width), "Height": str(height)}
    if kind == "webcam":
        f.update(Device=path, Channel="0", Format="0", Palette="0", Colours="3", VideoWriter="1")
    elif kind == "mjpeg":
        u = urllib.parse.urlparse(path)
        f.update(Protocol="http", Method="simple", Host=u.hostname or "",
                 Port=str(u.port or 80), Path=u.path or "/", Colours="4", VideoWriter="1")
    else:
        f.update(Path=path, Colours="4", VideoWriter="1", SaveJPEGs="0")
        if kind == "rtsp":
            f["Method"] = "rtpRtsp"
    return f


# --- ONVIF discovery (zmonvif-probe.pl) ---------------------------------------

def run_probe(args, timeout):
    out = subprocess.run([PROBE] + args, capture_output=True, text=True, timeout=timeout)
    return out.stdout + out.stderr


def parse_probe(text):
    """Pull (name, onvif_url) pairs out of zmonvif-probe.pl probe output.

    Lines look like:
        http://192.168.1.64:80/onvif/device_service, HIKVISION, DS-2CD..., 1.2.3
    """
    found = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].startswith("http"):
            continue
        url = parts[0]
        host = urllib.parse.urlparse(url).hostname or url
        label = " ".join(p for p in parts[1:3] if p) or "Camera"
        found.append(("%s (%s)" % (label, host), url))
    return found


def host_of(url):
    return urllib.parse.urlparse(url).hostname or ""


def parse_profiles(text):
    """Pick the best rtsp:// URL out of zmonvif-probe.pl profiles output.

    Lines carry the stream URL plus a WxH somewhere; take the widest.
    """
    best, best_w = None, -1
    for line in text.splitlines():
        m = re.search(r"rtsp://\S+", line)
        if not m:
            continue
        url = m.group(0).rstrip(",")
        dim = re.search(r"(\d{3,5})\s*[xX]\s*(\d{3,5})", line)
        w = int(dim.group(1)) if dim else 0
        if w > best_w:
            best, best_w = url, w
    return best


# --- RTSP sweep ---------------------------------------------------------------
#
# Plenty of cheap cameras never answer ONVIF discovery. Knocking on the usual
# camera ports across the local network finds those too.

RTSP_PORTS = [554, 8554, 10554, 88]


# Virtual machinery, not networks a camera lives on.
SKIP_INTERFACES = ("lo", "docker", "br-", "virbr", "tailscale", "veth", "zt", "wg", "tun", "tap")


def parse_addresses(text):
    """(interface, ip, prefix) for every real IPv4 address in `ip -4 -o addr` output."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        name, cidr = parts[1], parts[3]
        if name.startswith(SKIP_INTERFACES):
            continue
        ip, _, prefix = cidr.partition("/")
        out.append((name, ip, int(prefix or 32)))
    return out


def lan_addresses():
    """This machine's addresses on real local networks."""
    try:
        text = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    # A /24 is the normal home network; anything wider would take too long to sweep.
    return [ip for _, ip, prefix in parse_addresses(text) if prefix >= 24]


def subnet_hosts(ip):
    """Every address in this machine's /24, minus itself."""
    if not ip or ip.startswith("127."):
        return []
    head = ip.rsplit(".", 1)[0]
    return ["%s.%d" % (head, n) for n in range(1, 255) if "%s.%d" % (head, n) != ip]


def port_open(host, port, timeout=0.6):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def sweep(hosts, ports=None, workers=64, timeout=0.6):
    """Hosts with a camera port open, as (host, port), lowest port first."""
    ports = ports or RTSP_PORTS
    found = []
    lock = threading.Lock()
    queue = list(hosts)
    index = [0]

    def work():
        while True:
            with lock:
                if index[0] >= len(queue):
                    return
                host = queue[index[0]]
                index[0] += 1
            for port in ports:
                if port_open(host, port, timeout):
                    with lock:
                        found.append((host, port))
                    break

    threads = [threading.Thread(target=work, daemon=True) for _ in range(min(workers, len(queue) or 1))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(found, key=lambda hp: [int(n) for n in hp[0].split(".")])


def with_credentials(url, user, password):
    """Put the camera's login into an rtsp:// URL, replacing any already there.

    ponytail: ONVIF GetStreamUri hands back a credential-free URL -- the spec
    expects the client to authenticate separately -- but ZoneMinder gives ffmpeg
    nothing but the URL, so the login has to live in it or capture 401s.
    """
    if not url or not user:
        return url
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    if u.port:
        host += ":%d" % u.port
    cred = "%s:%s@" % (urllib.parse.quote(user, safe=""), urllib.parse.quote(password or "", safe=""))
    return urllib.parse.urlunsplit((u.scheme, cred + host, u.path, u.query, u.fragment))


# Cheap cameras answer on their own fixed path and nothing else. Ordered by how
# often they turn up; the wizard tries them in turn when ONVIF gave us nothing.
RTSP_PATHS = [
    "/Streaming/Channels/101",              # Hikvision and its many clones
    "/cam/realmonitor?channel=1&subtype=0",  # Dahua, Amcrest, Lorex
    "/h264Preview_01_main",                 # Reolink
    "/stream1",                             # Tapo, Wyze bridge
    "/stream2",                             # the same cameras' lower-quality stream
    "/live/ch00_0",                         # generic Chinese firmware
    "/onvif1",                              # cheap ONVIF-only boards
    "/live",
    "/",
]


def guess_rtsp(host, user, password, port=554, path="/"):
    return with_credentials("rtsp://%s:%s%s" % (host, port or 554, path), user, password) \
        or "rtsp://%s:%s%s" % (host, port or 554, path)


def rtsp_works(url, timeout=8):
    """True when ffmpeg can actually open this stream -- the same test zmc makes."""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                            "-rw_timeout", "5000000", "-i", url,
                            "-show_entries", "stream=codec_type", "-of", "csv"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and "video" in p.stdout


def find_rtsp(host, user, password, port=554):
    """The camera's real stream URL, or the plain guess when none of them answer."""
    for path in RTSP_PATHS:
        url = guess_rtsp(host, user, password, port, path)
        if rtsp_works(url):
            return url
    return guess_rtsp(host, user, password, port)


def rtsp_realm(host, port=554, timeout=5):
    """The name the camera calls itself in its RTSP 401, e.g. 'TP-Link IP-Camera'.

    ponytail: raw socket rather than a library -- one request, one regex, and it
    is the only RTSP verb we ever need to speak ourselves.
    """
    try:
        s = socket.create_connection((host, port or 554), timeout)
    except OSError:
        return ""
    try:
        s.settimeout(timeout)
        s.sendall(b"DESCRIBE rtsp://%s/ RTSP/1.0\r\nCSeq: 1\r\n\r\n" % host.encode())
        reply = s.recv(2048).decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        s.close()
    m = re.search(r'realm="([^"]*)"', reply)
    return m.group(1) if "401" in reply.split("\n")[0] and m else ""


# Cameras whose RTSP login is a separate account you create in the vendor's phone app.
SEPARATE_ACCOUNT = {
    "tp-link": "Tapo or Tapo Care app: Device Settings, Advanced Settings, Camera Account.",
    "tapo": "Tapo app: Device Settings, Advanced Settings, Camera Account.",
    "kasa": "Kasa app: Device Settings, Advanced Settings, Camera Account.",
}


def rtsp_refusal(host, port, user, password):
    """Plain-language reason the camera would not hand over its video."""
    if not port_open(host, port or 554, timeout=2):
        return ("This camera is not answering on port %s. Check it is switched on and that "
                "its video stream is turned on in the maker's app." % (port or 554))
    realm = rtsp_realm(host, port)
    if not user:
        return "This camera needs a username and password."
    for key, where in SEPARATE_ACCOUNT.items():
        if key in realm.lower():
            return ("The camera turned down that username and password. %s cameras need their "
                    "own separate camera account, not the one you sign in to the app with. "
                    "Make one here: %s" % (realm.split()[0] or key.title(), where))
    return ("The camera turned down that username and password. Some cameras want a separate "
            "account for video, made in the maker's own app or web page.")


def list_webcams():
    """USB cameras plugged into this computer."""
    out = []
    base = "/sys/class/video4linux"
    for dev in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        try:
            with open(os.path.join(base, dev, "name")) as fh:
                name = fh.read().strip()
        except OSError:
            name = dev
        out.append({"path": "/dev/" + dev, "name": name})
    return out


# --- zones --------------------------------------------------------------------

def polygon_area(points):
    """Shoelace formula. points is [(x, y), ...]."""
    s = 0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        s += x1 * y2 - x2 * y1
    return abs(s) // 2


def coords_string(points):
    return " ".join("%d,%d" % (x, y) for x, y in points)


def parse_coords(s):
    return [tuple(int(v) for v in p.split(",")) for p in s.split()]


def zone_fields(mid, name, ztype, points, min_pct=6, prefix="Zone"):
    """Full field set for creating or replacing a zone from a polygon."""
    area = polygon_area(points)
    f = {
        "MonitorId": str(mid),
        "Name": name,
        "Type": ztype,
        "Units": "Pixels",
        "NumCoords": str(len(points)),
        "Coords": coords_string(points),
        "Area": str(area),
        "AlarmRGB": "16711680",
        "CheckMethod": "Blobs",
        "MinPixelThreshold": "25",
        "MinAlarmPixels": str(area * min_pct // 100),
        "MaxAlarmPixels": str(area),
        "FilterX": "3",
        "FilterY": "3",
        "MinFilterPixels": str(area * min_pct // 100),
        "MaxFilterPixels": str(area),
        "MinBlobPixels": str(max(1, area * 2 // 100)),
        "MinBlobs": "1",
        "OverloadFrames": "0",
        "ExtendAlarmFrames": "0",
    }
    return {"%s[%s]" % (prefix, k): v for k, v in f.items()}


# Every zone column the polygon editor doesn't set for you, in ZoneMinder's own
# order. name -> (plain label, options or None for a number).
ZONE_ADVANCED = [
    ("Units", "Measure in", ["Pixels", "Percent"]),
    ("CheckMethod", "How movement is judged", ["AlarmedPixels", "FilteredPixels", "Blobs"]),
    ("AlarmRGB", "Highlight colour (as a number)", None),
    ("MinPixelThreshold", "A pixel counts as changed above", None),
    ("MaxPixelThreshold", "...and below", None),
    ("MinAlarmPixels", "Alarm when changed pixels reach", None),
    ("MaxAlarmPixels", "...but no more than", None),
    ("FilterX", "Filter width", None),
    ("FilterY", "Filter height", None),
    ("MinFilterPixels", "Filtered pixels needed", None),
    ("MaxFilterPixels", "...but no more than", None),
    ("MinBlobPixels", "Smallest blob that counts", None),
    ("MaxBlobPixels", "...largest blob that counts", None),
    ("MinBlobs", "Blobs needed", None),
    ("MaxBlobs", "...but no more than", None),
    ("OverloadFrames", "Ignore this many frames after an overload", None),
    ("ExtendAlarmFrames", "Keep the alarm on for extra frames", None),
]


# --- phone alerts -------------------------------------------------------------
#
# ZoneMinder filters can run a program per event, so a push notification is just
# that program. The topic lives in the command itself: nothing else to store.

PUSH_SCRIPT = os.environ.get("PORCHLIGHT_PUSH", "/usr/share/porchlight/push.sh")
NTFY_SERVER = "https://ntfy.sh"


def push_command(topic, server=None):
    """Filter command that alerts a phone, or None when the rule doesn't push."""
    if not topic:
        return None
    return "%s %s %s" % (PUSH_SCRIPT, shlex.quote(str(topic)),
                         shlex.quote(server or NTFY_SERVER))


def push_topic(command):
    """The topic out of a command push_command wrote, or '' for any other command."""
    parts = shlex.split(command or "")
    if len(parts) >= 2 and os.path.basename(parts[0]) == os.path.basename(PUSH_SCRIPT):
        return parts[1]
    return ""


# --- rules (ZM filters) -------------------------------------------------------

def rule_to_filter(rule):
    """Map a plain-language rule to ZM Filter form fields.

    rule: {name, cameras: [ids], what: "motion"|"any", between: ["HH:MM","HH:MM"],
           email: bool, keep: bool, delete_after_days: int, command: str}
    """
    terms = []

    def joined(t):
        if terms:
            t["cnj"] = "and"
        return t

    cams = [str(c) for c in rule.get("cameras") or []]
    for i, mid in enumerate(cams):
        t = {"attr": "MonitorId", "op": "=", "val": mid}
        if i == 0:
            t = joined(t)
            t["obr"] = "1"
        else:
            t["cnj"] = "or"
        if i == len(cams) - 1:
            t["cbr"] = "1"
        terms.append(t)

    if rule.get("what") == "motion":
        terms.append(joined({"attr": "Cause", "op": "=", "val": "Motion"}))
    between = rule.get("between")
    if between:
        # ponytail: time of day only; day-of-week filtering differs across ZM versions.
        terms.append(joined({"attr": "StartTime", "op": ">=", "val": between[0] + ":00"}))
        terms.append(joined({"attr": "StartTime", "op": "<=", "val": between[1] + ":59"}))
    days = rule.get("delete_after_days")
    if days:
        terms.append(joined({"attr": "StartDateTime", "op": "<", "val": "-%d day" % int(days)}))
    if not terms:
        terms.append({"attr": "Id", "op": ">=", "val": "0"})

    query = {"terms": terms, "sort_field": "StartDateTime", "sort_asc": "1", "limit": "100"}
    f = {"Name": rule["name"], "Query": json.dumps(query), "Background": "1"}
    if rule.get("email"):
        f["AutoEmail"] = "1"
    command = push_command(rule.get("push"), rule.get("push_server")) or rule.get("command")
    if command:
        f.update(AutoExecute="1", AutoExecuteCmd=command)
    if rule.get("keep"):
        f["AutoArchive"] = "1"
    if days:
        f["AutoDelete"] = "1"
    return {"Filter[%s]" % k: v for k, v in f.items()}


def filter_to_rule(row):
    """Best-effort reverse of rule_to_filter, for showing saved rules in plain words."""
    try:
        query = json.loads(row.get("Query") or "{}")
    except ValueError:
        query = {}
    terms = query.get("terms") or []
    return {
        "id": row.get("Id"),
        "name": row.get("Name"),
        "cameras": [t["val"] for t in terms if t.get("attr") == "MonitorId"],
        "what": "motion" if any(t.get("attr") == "Cause" for t in terms) else "any",
        "email": row.get("AutoEmail") in ("1", 1),
        "keep": row.get("AutoArchive") in ("1", 1),
        "delete": row.get("AutoDelete") in ("1", 1),
        "push": push_topic(row.get("AutoExecuteCmd")),
        "command": "" if push_topic(row.get("AutoExecuteCmd")) else (row.get("AutoExecuteCmd") or ""),
        "between": _between(terms),
        "delete_after_days": _age_days(terms),
    }


def _between(terms):
    """The two StartTime bounds a time-of-day rule was saved with, HH:MM."""
    times = [str(t["val"])[:5] for t in terms
             if t.get("attr") == "StartTime" and ":" in str(t.get("val") or "")]
    return times[:2] if len(times) >= 2 else None


def _age_days(terms):
    for t in terms:
        m = re.match(r"-(\d+) day", str(t.get("val") or ""))   # ZM stores some values as numbers
        if m:
            return int(m.group(1))
    return None


# --- run states + schedule ----------------------------------------------------

def state_definition(rows):
    """rows: [(monitor_id, function, enabled)] -> ZM States.Definition string."""
    return ",".join("%s:%s:%s" % (mid, fn, en) for mid, fn, en in rows)


def cron_lines(entries):
    """entries: [(state, "HH:MM", days)] with days like "*", "1-5", "0,6".
    -> /etc/cron.d lines that switch the ZM run state."""
    out = []
    for state, hhmm, days in entries:
        h, m = hhmm.split(":")
        out.append("%d %d * * %s root /usr/bin/zmpkg.pl %s >/dev/null 2>&1"
                   % (int(m), int(h), days, state))
    return out


# --- users --------------------------------------------------------------------

def mysql_password_hash(pw):
    """'*' + SHA1(SHA1(pw)), the hash ZoneMinder stores for its own accounts."""
    return "*" + hashlib.sha1(hashlib.sha1(pw.encode()).digest()).hexdigest().upper()
