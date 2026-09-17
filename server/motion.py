"""Disposable ZoneMinder replay jobs. Never writes to the production API or DB."""
import atexit
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

IMAGE = os.environ.get("PORCHLIGHT_MOTION_IMAGE", "porchlight-motion:1.36.33")
# ponytail: one bounded job per recorder; add a queue only if concurrent tests are needed.
_lock = threading.Lock()
_job = None
_engine = None
_selected_image = IMAGE


def cleanup():
    # Completed replays do not need to survive a normal application shutdown.
    if _job and _job.get("state") in {"ready", "failed"}:
        shutil.rmtree(_job["directory"], ignore_errors=True)


atexit.register(cleanup)


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=120, **kwargs)


def capability(version):
    global _engine, _selected_image
    if not shutil.which("docker"):
        return {"available": False, "reason": "Install the isolated motion worker to test draft settings."}
    version = str(version).lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return {"available": False, "reason": "The recorder did not report a supported release version."}
    image = os.environ.get("PORCHLIGHT_MOTION_IMAGE", "porchlight-motion:" + version)
    if _engine is None or image != _selected_image:
        try:
            _engine = run(["docker", "run", "--rm", "--pull=never", "--network=none", "--entrypoint", "zmc", image, "--version"], text=True).stdout.strip()
            _selected_image = image
        except (OSError, subprocess.SubprocessError):
            return {"available": False, "reason": "The isolated motion worker is not installed or Docker is unavailable."}
    if str(version).lstrip("v") != _engine.lstrip("v"):
        return {"available": False, "reason": "Install a motion worker matching ZoneMinder " + str(version) + ".", "engine": _engine}
    return {"available": True, "engine": _engine}


def validate(saved_monitor, saved_zones, draft):
    """Accept detection fields only. Connection paths and identities never come from the request."""
    if not isinstance(draft, dict) or not isinstance(draft.get("monitor", {}), dict):
        raise ValueError("Invalid motion draft")
    monitor = {k: saved_monitor[k] for k in ("Width", "Height", "Colours", "AlarmFrameCount", "PreEventCount", "PostEventCount", "AnalysisFPS", "AnalysisFPSLimit", "AnalysisImage", "RefBlendPerc", "AlarmRefBlendPerc", "TrackMotion") if k in saved_monitor}
    if not 0 < int(monitor.get("Width", 0)) * int(monitor.get("Height", 0)) <= 3840 * 2160:
        raise ValueError("Motion tests support camera frames up to 3840 × 2160 pixels.")
    if saved_monitor.get("AnalysisSource") == "Secondary":
        raise ValueError("Motion testing currently requires the primary analysis stream.")
    permitted = {"AlarmFrameCount", "PreEventCount", "PostEventCount", "AnalysisFPS", "AnalysisFPSLimit", "RefBlendPerc", "AlarmRefBlendPerc"}
    for k, v in draft.get("monitor", {}).items():
        if k not in permitted:
            raise ValueError("Unsupported motion field: " + k)
        number = float(v or 0)
        limit = 100 if k in {"RefBlendPerc", "AlarmRefBlendPerc"} else 10 if k in {"AnalysisFPS", "AnalysisFPSLimit"} else 10000
        if not 0 <= number <= limit:
            raise ValueError("Invalid value for " + k)
        monitor[k] = v
    from zmapi import ZONE_ADVANCED
    fields = {k: options for k, _, options in ZONE_ADVANCED}
    changes = draft.get("zones", {})
    ids = {str(z["Id"]) for z in saved_zones}
    if not isinstance(changes, dict) or not set(changes) <= ids:
        raise ValueError("Unknown watched area")
    zones = []
    for original in saved_zones:
        z = {k: v for k, v in original.items() if k in fields or k in {"Id", "MonitorId", "Name", "Type", "Coords", "NumCoords", "Area"}}
        if not isinstance(changes.get(str(z["Id"]), {}), dict):
            raise ValueError("Invalid watched-area draft")
        for k, v in changes.get(str(z["Id"]), {}).items():
            if k not in fields:
                raise ValueError("Unsupported zone field: " + k)
            if fields[k]:
                if v not in fields[k]: raise ValueError("Invalid " + k)
            elif not re.fullmatch(r"\d+(\.\d+)?", str(v)) or not 0 <= float(v) <= 100000000:
                raise ValueError("Invalid " + k)
            z[k] = v
        for key in changes.get(str(z["Id"]), {}):
            if key in {"MinPixelThreshold", "MaxPixelThreshold"} and float(z[key]) > 255:
                raise ValueError(key + " must be between 0 and 255")
            if key in {"FilterX", "FilterY"} and (float(z[key]) % 2 != 1 or float(z[key]) > 15):
                raise ValueError(key + " must be an odd number from 1 to 15")
            if key == "AlarmRGB" and float(z[key]) > 16777215:
                raise ValueError("Invalid highlight colour")
        for lo, hi in (("MinPixelThreshold", "MaxPixelThreshold"), ("MinAlarmPixels", "MaxAlarmPixels"), ("MinFilterPixels", "MaxFilterPixels"), ("MinBlobPixels", "MaxBlobPixels"), ("MinBlobs", "MaxBlobs")):
            if lo in z and hi in z and (lo in changes.get(str(z["Id"]), {}) or hi in changes.get(str(z["Id"]), {})):
                if float(z[hi] or 0) and float(z[lo] or 0) > float(z[hi]):
                    raise ValueError(lo + " must not exceed " + hi)
        zones.append(z)
    if not zones:
        raise ValueError("Create a watched area before testing motion.")
    return {"monitor": monitor, "zones": zones, "duration": 10}


def start(url, data):
    global _job
    if not _lock.acquire(blocking=False):
        raise ValueError("Another motion test is running. Wait for it to finish.")
    if _job and _job.get("directory"):
        shutil.rmtree(_job["directory"], ignore_errors=True)
    directory = tempfile.mkdtemp(prefix="porchlight-motion-")
    job = {"id": secrets.token_hex(16), "state": "capturing", "directory": directory, "created": time.time()}
    _job = job
    threading.Thread(target=_work, args=(job, url, data, _selected_image), daemon=True).start()
    return status(job["id"])


def _work(job, url, data, image):
    directory = Path(job["directory"])
    name = "porchlight-motion-" + job["id"]
    created = False
    try:
        run(["ffmpeg", "-v", "error", "-y", "-rw_timeout", "15000000", "-i", url, "-t", "10",
             "-an", "-vf", "fps=10", "-c:v", "libx264", "-g", "1", "-bf", "0", "-pix_fmt", "yuv420p", str(directory / "sample.mp4")])
        (directory / "draft.json").write_text(json.dumps(data))
        run(["docker", "create", "--pull=never", "--name", name, "--network=none", "--memory=2g", "--cpus=2",
             "--pids-limit=256", "--shm-size=512m", "--security-opt=no-new-privileges", image])
        created = True
        run(["docker", "cp", str(directory) + "/.", name + ":/work"])
        job["state"] = "analysing"
        run(["docker", "start", "-a", name])
        run(["docker", "cp", name + ":/work/result.mp4", str(directory / "result.mp4")])
        run(["docker", "cp", name + ":/work/result.json", str(directory / "result.json")])
        job.update(json.loads((directory / "result.json").read_text()), state="ready")
    except (OSError, ValueError, subprocess.SubprocessError):
        job.update(state="failed", error="Motion test failed. Check that the camera is online and the matching motion worker is installed.")
    finally:
        if created:
            try:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
        for path in (directory / "sample.mp4", directory / "draft.json"):
            path.unlink(missing_ok=True)
        _lock.release()
        timer = threading.Timer(600, shutil.rmtree, args=(directory,), kwargs={"ignore_errors": True})
        timer.daemon = True
        timer.start()


def status(job_id):
    if not _job or job_id != _job["id"] or time.time() - _job["created"] > 600:
        raise ValueError("Motion test expired. Start another test.")
    return {k: v for k, v in _job.items() if k not in ("directory", "created")}


def result(job_id):
    if status(job_id)["state"] != "ready":
        raise ValueError("Motion test is not ready")
    return Path(_job["directory"]) / "result.mp4"
