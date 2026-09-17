#!/usr/bin/env python3
"""Replay a staged sample with the packaged ZoneMinder engine, in a disposable container.

Only /work contains caller data. No production configuration, sockets or networks
are mounted. Filter/notification daemons are never started.
"""
import glob
import json
import os
import pathlib
import subprocess
import time


def sql(statement):
    return subprocess.run(["mariadb", "-uroot", "-N", "-B", "zm", "-e", statement],
                          check=True, capture_output=True, text=True).stdout


def quote(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def insert(table, values):
    allowed = {line.split("\t")[0] for line in sql("SHOW COLUMNS FROM " + table).splitlines()}
    values = {k: v for k, v in values.items() if k in allowed and v is not None}
    sql("INSERT INTO " + table + " SET " + ",".join("`" + k + "`=" + quote(v) for k, v in values.items()))


def main():
    data = json.loads(pathlib.Path("/work/draft.json").read_text())
    log = open("/work/worker.log", "w")
    db = subprocess.Popen(["mysqld_safe", "--skip-networking"], stdout=log, stderr=log)
    for _ in range(80):
        if subprocess.run(["mariadb-admin", "ping"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(.25)
    with open("/usr/share/zoneminder/db/zm_create.sql") as schema:
        subprocess.run(["mariadb", "-uroot"], stdin=schema, check=True, stdout=log, stderr=log)
    sql("CREATE USER IF NOT EXISTS 'zmuser'@'localhost' IDENTIFIED BY 'zmpass'; GRANT ALL ON zm.* TO 'zmuser'@'localhost'")
    sql("UPDATE Config SET Value='1' WHERE Name IN ('ZM_RECORD_EVENT_STATS','ZM_CREATE_ANALYSIS_IMAGES')")
    sql("UPDATE Config SET Value='0' WHERE Name IN ('ZM_OPT_EMAIL','ZM_OPT_MESSAGE','ZM_OPT_UPLOAD','ZM_OPT_X10','ZM_OPT_TRIGGERS','ZM_OPT_USE_AUTH')")
    for folder in ("/run/zm", "/var/cache/zoneminder/events", "/var/log/zm"):
        os.makedirs(folder, exist_ok=True)
    monitor = dict(data["monitor"])
    # The source and output are always sandbox-local; user input cannot choose them.
    monitor.update(Id=1, Name="Motion test", Type="Ffmpeg", Function="Mocord", Enabled=1,
                   Path="/work/sample.mp4", Method="", Host="", User="", Pass="", Device="",
                   MaxFPS=10, AlarmMaxFPS=10, VideoWriter=0, SaveJPEGs=3, RecordAudio=0,
                   LinkedMonitors="", ServerId=None, StorageId=0,
                   Capturing="Always", Analysing="Always", Recording="Always", Decoding="Always",
                   ZoneCount=len(data["zones"]), ImageBufferCount=3)
    insert("Monitors", monitor)
    for n, zone in enumerate(data["zones"], 1):
        zone = dict(zone, Id=n, MonitorId=1)
        insert("Zones", zone)
    capture = subprocess.Popen(["zmc", "-m", "1"], stdout=log, stderr=log)
    try:
        time.sleep(float(data.get("duration", 10)) + 5)
    finally:
        capture.terminate()
        try:
            capture.wait(timeout=5)
        except subprocess.TimeoutExpired:
            capture.kill(); capture.wait()
    frames = sorted(glob.glob("/var/cache/zoneminder/events/**/*-capture.jpg", recursive=True))
    frames = frames[:int(float(data.get("duration", 10)) * 10)]
    if not frames:
        raise RuntimeError("ZoneMinder produced no frames; inspect worker.log")
    output = pathlib.Path("/work/frames")
    output.mkdir(exist_ok=True)
    hits = 0
    for i, frame in enumerate(frames):
        overlay = frame.replace("-capture.jpg", "-analyse.jpg")
        hit = os.path.isfile(overlay)
        hits += hit
        source = overlay if hit else frame
        (output / ("%06d.jpg" % i)).write_bytes(pathlib.Path(source).read_bytes())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "10", "-i", "/work/frames/%06d.jpg",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "/work/result.mp4"], check=True)
    pathlib.Path("/work/result.json").write_text(json.dumps({"frames": len(frames), "alarm_frames": hits,
        "engine": subprocess.check_output(["zmc", "--version"], text=True).strip()}))
    subprocess.run(["mariadb-admin", "shutdown"], check=False, stdout=log, stderr=log)
    db.wait(timeout=10)


if __name__ == "__main__":
    main()
