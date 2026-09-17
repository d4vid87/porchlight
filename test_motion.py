"""Draft isolation checks; add --engine to exercise the real disposable worker."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, str(Path(__file__).parent / "server"))
import motion

monitor = {"Width": 640, "Height": 360, "Colours": 4, "AlarmFrameCount": 1, "AnalysisFPS": 10}
zones = [{"Id": 42, "MonitorId": 7, "Name": "Test", "Type": "Active", "Units": "Pixels",
          "Coords": "0,0 639,0 639,359 0,359", "NumCoords": 4, "Area": 230400,
          "CheckMethod": "AlarmedPixels", "AlarmRGB": 16711680, "MinPixelThreshold": 20,
          "MaxPixelThreshold": 0, "MinAlarmPixels": 100, "MaxAlarmPixels": 0}]
before = copy.deepcopy((monitor, zones))
draft = motion.validate(monitor, zones, {"zones": {"42": {"MinAlarmPixels": "200"}}})
assert draft["zones"][0]["MinAlarmPixels"] == "200"
assert (monitor, zones) == before
for bad in ({"monitor": {"Path": "bad"}}, {"monitor": {"AnalysisFPS": "nan"}},
            {"zones": {"99": {}}}, {"zones": {"42": {"Coords": "bad"}}},
            {"zones": {"42": {"MinPixelThreshold": "256"}}},
            {"zones": {"42": {"FilterX": "2"}}}):
    try:
        motion.validate(monitor, zones, bad)
        raise AssertionError("Invalid draft accepted: " + repr(bad))
    except ValueError:
        pass

# A browser can seek the private replay; arbitrary paths never come from the URL.
import porchlight_server as server
from http.server import ThreadingHTTPServer
with tempfile.TemporaryDirectory() as tmp:
    result = Path(tmp) / "result.mp4"
    result.write_bytes(b"0123456789")
    motion._job = {"id": "test", "state": "ready", "directory": tmp, "created": motion.time.time()}
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request("http://127.0.0.1:%s/api/motion/file?id=test" % http.server_port, headers={"Range": "bytes=3-5"})
        with urllib.request.urlopen(req) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 3-5/10"
            assert response.read() == b"345"
    finally:
        http.shutdown(); http.server_close(); motion._job = None

if "--engine" in sys.argv:
    with tempfile.TemporaryDirectory(prefix="porchlight-check-") as tmp:
        root = Path(tmp)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10", "-t", "10", "-c:v", "libx264", "-g", "1", "-bf", "0", str(root / "sample.mp4")], check=True)
        results = []
        for threshold in (20, 255):
            data = motion.validate(monitor, zones, {"zones": {"42": {"MinPixelThreshold": str(threshold)}}})
            (root / "draft.json").write_text(json.dumps(data))
            name = "porchlight-check-" + str(threshold)
            try:
                subprocess.run(["docker", "create", "--name", name, "--network=none", "--memory=2g", "--cpus=2", "--pids-limit=256", "--shm-size=512m", "--security-opt=no-new-privileges", motion.IMAGE], check=True, stdout=subprocess.DEVNULL)
                for file in ("draft.json", "sample.mp4"):
                    subprocess.run(["docker", "cp", str(root / file), name + ":/work/" + file], check=True)
                subprocess.run(["docker", "start", "-a", name], check=True, timeout=120)
                subprocess.run(["docker", "cp", name + ":/work/result.json", str(root / "result.json")], check=True)
                result = json.loads((root / "result.json").read_text())
                assert result["frames"] == 100, result
                results.append(result["alarm_frames"])
            finally:
                subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL)
        assert results[0] > 0 and results[1] == 0, results
        print("Engine threshold comparison:", results)
print("Motion checks passed")
