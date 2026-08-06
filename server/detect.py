"""Is somebody in this picture? NanoDet-Plus-m-416 (OpenCV Zoo, Apache-2.0),
run on the CPU, once per event snapshot, never per frame.

onnxruntime is only Recommended by the package, so everything here degrades
to "no detector": available() gates every caller, and a missing runtime just
means alerts go out unfiltered.
"""

import os
import subprocess

MODEL = os.environ.get("PORCHLIGHT_MODEL",
                       "/usr/share/porchlight/models/nanodet-plus-m-416.onnx")

try:
    import numpy as np
    import onnxruntime
except ImportError:
    onnxruntime = None

_session = None

# Preprocessing constants from the OpenCV Zoo demo; pixel order is BGR.
MEAN = (103.53, 116.28, 123.675)
STD = (57.375, 57.12, 58.395)
REG_MAX = 7
STRIDE_OF = {2704: 8, 676: 16, 169: 32, 36: 64}    # anchor rows -> stride


def available():
    return bool(onnxruntime) and os.path.isfile(MODEL)


def _pixels(jpeg):
    """The picture as 416x416 BGR floats, decoded by ffmpeg (already a dep)."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-vf", "scale=416:416",
                        "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
                       input=jpeg, capture_output=True, timeout=30)
    if p.returncode or len(p.stdout) != 416 * 416 * 3:
        return None
    return np.frombuffer(p.stdout, np.uint8).reshape(416, 416, 3).astype(np.float32)


def person(jpeg, threshold=0.35):
    """(confidence, box) of the surest person, or (0.0, None).

    box is (x1, y1, x2, y2) as 0-1 fractions of the frame.
    ponytail: single best box, no NMS -- the caller only asks "is somebody
    there and where, roughly"; add real NMS if boxes ever need to be counted.
    """
    if not available():
        return 0.0, None
    img = _pixels(jpeg)
    if img is None:
        return 0.0, None
    global _session
    if _session is None:
        _session = onnxruntime.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    blob = ((img - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)
    outs = _session.run(None, {_session.get_inputs()[0].name: blob})

    levels = {}                # anchor rows -> {80: class scores, 32: regression}
    for o in outs:
        a = o[0] if o.ndim == 3 else o
        levels.setdefault(a.shape[0], {})[a.shape[1]] = a
    best, box = 0.0, None
    for rows, pair in levels.items():
        stride, cls, reg = STRIDE_OF.get(rows), pair.get(80), pair.get(32)
        if not stride or cls is None or reg is None:
            continue
        scores = cls[:, 0]                         # COCO class 0 is "person"
        i = int(scores.argmax())
        if scores[i] <= max(best, threshold):
            continue
        # The head predicts each side's distance as a softmax over 0..REG_MAX
        # stride-sized steps (generalized focal loss); its mean is the distance.
        e = np.exp(reg[i].reshape(4, REG_MAX + 1))
        d = (e / e.sum(axis=1, keepdims=True)) @ np.arange(REG_MAX + 1) * stride
        n = 416 // stride
        cx = (i % n) * stride + 0.5 * (stride - 1)
        cy = (i // n) * stride + 0.5 * (stride - 1)
        best = float(scores[i])
        box = (max(cx - d[0], 0) / 416, max(cy - d[1], 0) / 416,
               min(cx + d[2], 416) / 416, min(cy + d[3], 416) / 416)
    return best, box


def draw_box(jpeg, box, out_path):
    """Write the frame with the person outlined; ZoneMinder shows the file as
    the event's objdetect image."""
    x1, y1, x2, y2 = box
    vf = ("drawbox=x=iw*%.4f:y=ih*%.4f:w=iw*%.4f:h=ih*%.4f:color=red@0.9:t=4"
          % (x1, y1, x2 - x1, y2 - y1))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "-", "-frames:v", "1",
                    "-vf", vf, out_path], input=jpeg, capture_output=True, timeout=30)


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as fh:
        conf, where = person(fh.read())
    print("person %.2f at %s" % (conf, where))
