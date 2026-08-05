#!/usr/bin/env python3
"""Runnable checks for the pure logic in server/zmapi.py. python3 test_api.py"""

import hashlib
import json
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "server"))
import zmapi as z

# --- ONVIF probe output -> camera choices
probe = """Discovery of ONVIF devices
http://192.168.1.64:80/onvif/device_service, HIKVISION, DS-2CD2042WD, 1.0
http://192.168.1.77:8000/onvif/device_service, Dahua, IPC-HDW, 2.4
garbage line without url
"""
found = z.parse_probe(probe)
assert len(found) == 2, found
assert found[0][1] == "http://192.168.1.64:80/onvif/device_service"
assert "192.168.1.64" in found[0][0] and "HIKVISION" in found[0][0]

# --- profiles output -> widest stream wins
profiles = """profile 1, 704x480, rtsp://192.168.1.64:554/Streaming/Channels/102
profile 0, 1920x1080, rtsp://192.168.1.64:554/Streaming/Channels/101
"""
assert z.parse_profiles(profiles).endswith("/101"), z.parse_profiles(profiles)
assert z.parse_profiles("no streams here") is None

# --- credentials get escaped into the fallback URL
assert z.guess_rtsp("10.0.0.5", "admin", "p@ss word") == "rtsp://admin:p%40ss%20word@10.0.0.5:554/"
assert z.guess_rtsp("10.0.0.5", "", "") == "rtsp://10.0.0.5:554/"
assert z.guess_rtsp("10.0.0.5", "admin", "pw", 554, "/live") == "rtsp://admin:pw@10.0.0.5:554/live"

# --- ONVIF gives back a login-less URL; it has to carry one before ffmpeg sees it
assert (z.with_credentials("rtsp://10.0.0.5:554/Streaming/Channels/101?x=1", "admin", "p@ss")
        == "rtsp://admin:p%40ss@10.0.0.5:554/Streaming/Channels/101?x=1")
assert z.with_credentials("rtsp://old:creds@10.0.0.5/live", "admin", "pw") == "rtsp://admin:pw@10.0.0.5/live"
assert z.with_credentials("rtsp://10.0.0.5/live", "", "") == "rtsp://10.0.0.5/live"   # no login to add
assert z.with_credentials(None, "admin", "pw") is None

# --- monitor POST fields
f = z.monitor_fields("Front Door", "rtsp://x/1", "Modect")
assert f["Monitor[Name]"] == "Front Door"
assert f["Monitor[Path]"] == "rtsp://x/1"
assert f["Monitor[Function]"] == "Modect"
assert f["Monitor[Type]"] == "Ffmpeg"

# --- plain-language menus map to real ZoneMinder values
assert dict(z.MODES)["Record"] == "Record always"
assert dict(z.SENSITIVITY)["High"] == 3

# --- other source kinds
web = z.source_fields("webcam", "/dev/video0", 640, 480)
assert web["Device"] == "/dev/video0" and web["Type"] == "Local"
mj = z.source_fields("mjpeg", "http://cam.local:8080/snap.jpg", 800, 600)
assert (mj["Host"], mj["Port"], mj["Path"]) == ("cam.local", "8080", "/snap.jpg"), mj
fl = z.source_fields("file", "/srv/clip.mp4", 640, 480)
assert fl["Path"] == "/srv/clip.mp4" and fl["Type"] == "Ffmpeg"   # ZM's File type never primes
# the sample camera goes down this same path
sm = z.source_fields("file", "/tmp/porchlight-sample.mp4", 1280, 720)
assert sm["Path"] == "/tmp/porchlight-sample.mp4" and (sm["Width"], sm["Height"]) == ("1280", "720")

# --- RTSP sweep: real networks only, and it must actually find an open port
addrs = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
3: wlp6s0    inet 192.168.1.67/24 brd 192.168.1.255 scope global dynamic wlp6s0\\       valid_lft 1sec
5: tailscale0    inet 100.118.45.114/32 scope global tailscale0\\       valid_lft forever
12: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever
"""
assert z.parse_addresses(addrs) == [("wlp6s0", "192.168.1.67", 24)], z.parse_addresses(addrs)

hosts = z.subnet_hosts("192.168.1.67")
assert len(hosts) == 253 and "192.168.1.67" not in hosts and hosts[0] == "192.168.1.1"
assert z.subnet_hosts("127.0.0.1") == []

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(1)
port = listener.getsockname()[1]
assert z.sweep(["127.0.0.1"], ports=[port]) == [("127.0.0.1", port)]
assert z.sweep(["127.0.0.1"], ports=[port], timeout=0.2) == [("127.0.0.1", port)]
listener.close()
assert z.sweep(["127.0.0.1"], ports=[port], timeout=0.2) == []

assert z.guess_rtsp("10.0.0.5", "", "", 8554) == "rtsp://10.0.0.5:8554/"

# --- zone geometry: a 100x50 rectangle covers 5000 pixels and round trips as text
rect = [(0, 0), (100, 0), (100, 50), (0, 50)]
assert z.polygon_area(rect) == 5000, z.polygon_area(rect)
assert z.parse_coords(z.coords_string(rect)) == rect
zf = z.zone_fields(3, "Drive", "Active", rect, min_pct=6)
assert zf["Zone[Coords]"] == "0,0 100,0 100,50 0,50"
assert zf["Zone[Area]"] == "5000"
assert zf["Zone[MinAlarmPixels]"] == "300"      # 6% of 5000
assert zf["Zone[MaxAlarmPixels]"] == "5000"
assert zf["Zone[MonitorId]"] == "3"

# --- rules -> ZM filter terms
rule = {"name": "Night alerts", "cameras": [1, 2], "what": "motion",
        "between": ["22:00", "06:00"], "email": True, "keep": True}
ff = z.rule_to_filter(rule)
assert ff["Filter[Name]"] == "Night alerts"
assert ff["Filter[AutoEmail]"] == "1" and ff["Filter[AutoArchive]"] == "1"
terms = json.loads(ff["Filter[Query]"])["terms"]
# the two camera terms are bracketed and OR'd together, everything else AND'd
assert terms[0]["obr"] == "1" and terms[0]["val"] == "1"
assert terms[1]["cnj"] == "or" and terms[1]["cbr"] == "1"
assert any(t["attr"] == "Cause" and t["val"] == "Motion" for t in terms)
assert [t["val"] for t in terms if t["attr"] == "StartTime"] == ["22:00:00", "06:00:59"]

# a rule with no conditions must still be a valid (match-everything) filter
plain = json.loads(z.rule_to_filter({"name": "All"})["Filter[Query]"])["terms"]
assert plain == [{"attr": "Id", "op": ">=", "val": "0"}], plain

# delete-after-days turns on AutoDelete and adds an age term
old = z.rule_to_filter({"name": "Tidy", "delete_after_days": 14})
assert old["Filter[AutoDelete]"] == "1"
assert any(t["val"] == "-14 day" for t in json.loads(old["Filter[Query]"])["terms"])

# --- filter -> plain rule (the parts the Rules page shows)
back = z.filter_to_rule({"Id": "7", "Name": "Night alerts", "Query": ff["Filter[Query]"],
                         "AutoEmail": "1", "AutoArchive": "1"})
assert back["cameras"] == ["1", "2"] and back["what"] == "motion"
assert back["email"] and back["keep"]
assert z.filter_to_rule({"Name": "broken", "Query": "not json"})["cameras"] == []
# ZoneMinder's own filters store numbers unquoted, so nothing may assume strings
numeric = json.dumps({"terms": [{"attr": "DiskPercent", "op": ">=", "val": 95}]})
assert z.filter_to_rule({"Name": "PurgeWhenFull", "Query": numeric})["between"] is None
assert back["between"] == ["22:00", "06:00"], back["between"]      # the edit form refills these
assert z.filter_to_rule({"Name": "Tidy", "Query": old["Filter[Query]"],
                         "AutoDelete": "1"})["delete_after_days"] == 14

# --- phone alerts ride the filter's "run a program" column
pf = z.rule_to_filter({"name": "Ping me", "push": "porchlight-abc123"})
assert pf["Filter[AutoExecute]"] == "1"
assert z.push_topic(pf["Filter[AutoExecuteCmd]"]) == "porchlight-abc123"
assert z.filter_to_rule({"Name": "Ping me", "Query": pf["Filter[Query]"],
                         "AutoExecuteCmd": pf["Filter[AutoExecuteCmd]"]})["push"] == "porchlight-abc123"
# a self-hosted server, and a topic with a space in it, survive the round trip
sp = z.rule_to_filter({"name": "x", "push": "my topic", "push_server": "http://nas:8080"})
assert z.push_topic(sp["Filter[AutoExecuteCmd]"]) == "my topic"
assert "http://nas:8080" in sp["Filter[AutoExecuteCmd]"]
# somebody else's command stays a plain command, not a push
other = z.filter_to_rule({"Name": "y", "Query": "{}", "AutoExecuteCmd": "/usr/local/bin/mine"})
assert other["push"] == "" and other["command"] == "/usr/local/bin/mine"

# --- monitor status (the camera cards colour their badge from this)
on = {"Id": "1", "Function": "Modect", "Enabled": "1", "Type": "Ffmpeg"}
assert z.monitor_status(on, {"Status": "Connected", "CaptureFPS": "9.98"}) == "ok"
assert z.monitor_status(on, {"Status": "Connected", "CaptureFPS": "0.00"}) == "offline"
assert z.monitor_status(on, {}) == "offline"          # zmstats deleted the stale row
assert z.monitor_status(on, None) == "offline"        # monitor never started
assert z.monitor_status(dict(on, Function="None"), None) == "off"
assert z.monitor_status(dict(on, Enabled="0"), None) == "off"
assert z.monitor_status(dict(on, Type="WebSite"), None) == "ok"   # nothing to capture

# --- run states + schedule
assert z.state_definition([(1, "Modect", "1"), (2, "None", "0")]) == "1:Modect:1,2:None:0"
lines = z.cron_lines([("Away", "08:30", "1-5"), ("Home", "17:00", "*")])
assert lines[0] == "30 8 * * 1-5 root /usr/bin/zmpkg.pl Away >/dev/null 2>&1", lines[0]
assert lines[1].startswith("0 17 * * * root /usr/bin/zmpkg.pl Home")

# --- SQL literals (rules and users are written straight to the ZM database)
assert z.quote("O'Brien") == "'O''Brien'"
assert z.quote(None) == "NULL"
assert z.quote("back\\slash") == "'back\\\\slash'"

# --- password hashing matches MySQL's legacy scheme
h = z.mysql_password_hash("zmpass")
assert h == "*" + hashlib.sha1(hashlib.sha1(b"zmpass").digest()).hexdigest().upper()
assert len(h) == 41 and h.startswith("*")

print("ok")
