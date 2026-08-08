"""Who is in this picture? NanoDet-Plus-m-416 (OpenCV Zoo, Apache-2.0),
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

# The COCO classes worth telling somebody about, by their column in the model's
# 80-class output.
CLASSES = {0: "person", 14: "bird", 15: "cat", 16: "dog"}


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


def _box(i, reg, stride):
    """The i-th anchor's box as (x1, y1, x2, y2), 0-1 fractions of the frame."""
    # The head predicts each side's distance as a softmax over 0..REG_MAX
    # stride-sized steps (generalized focal loss); its mean is the distance.
    e = np.exp(reg[i].reshape(4, REG_MAX + 1))
    d = (e / e.sum(axis=1, keepdims=True)) @ np.arange(REG_MAX + 1) * stride
    n = 416 // stride
    cx = (i % n) * stride + 0.5 * (stride - 1)
    cy = (i // n) * stride + 0.5 * (stride - 1)
    return (max(cx - d[0], 0) / 416, max(cy - d[1], 0) / 416,
            min(cx + d[2], 416) / 416, min(cy + d[3], 416) / 416)


def look(jpeg, threshold=0.35):
    """What is in the picture: {"person": (confidence, box), "dog": ...}.

    Only CLASSES are looked for, boxes are 0-1 fractions of the frame.
    ponytail: one best box per class, no NMS -- the caller only asks "is it
    there and where, roughly"; add real NMS if boxes ever need to be counted.
    """
    if not available():
        return {}
    img = _pixels(jpeg)
    if img is None:
        return {}
    global _session
    if _session is None:
        _session = onnxruntime.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    blob = ((img - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)
    outs = _session.run(None, {_session.get_inputs()[0].name: blob})

    levels = {}                # anchor rows -> {80: class scores, 32: regression}
    for o in outs:
        a = o[0] if o.ndim == 3 else o
        levels.setdefault(a.shape[0], {})[a.shape[1]] = a
    hits = {}
    for rows, pair in levels.items():
        stride, cls, reg = STRIDE_OF.get(rows), pair.get(80), pair.get(32)
        if not stride or cls is None or reg is None:
            continue
        for ci, name in CLASSES.items():
            scores = cls[:, ci]
            i = int(scores.argmax())
            if scores[i] <= max(hits.get(name, (threshold,))[0], threshold):
                continue
            hits[name] = (float(scores[i]), _box(i, reg, stride))
    return hits


def person(jpeg, threshold=0.35):
    """(confidence, box) of the surest person, or (0.0, None)."""
    return look(jpeg, threshold).get("person", (0.0, None))


def draw_box(jpeg, boxes, out_path):
    """Write the frame with each box outlined; ZoneMinder shows the file as
    the event's objdetect image."""
    vf = ",".join("drawbox=x=iw*%.4f:y=ih*%.4f:w=iw*%.4f:h=ih*%.4f:color=red@0.9:t=4"
                  % (x1, y1, x2 - x1, y2 - y1) for x1, y1, x2, y2 in boxes)
    if not vf:
        return
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "-", "-frames:v", "1",
                    "-vf", vf, out_path], input=jpeg, capture_output=True, timeout=30)


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as fh:
        found = look(fh.read())
    for name, (conf, where) in found.items():
        print("%s %.2f at %s" % (name, conf, where))
    print(found or "nothing")
