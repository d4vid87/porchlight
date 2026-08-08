"""Find one thing in stored recordings: "when did the bike go missing?"

A crop of a snapshot is matched against one frame per second of each
recording by normalized cross-correlation. numpy only -- it rides in with
onnxruntime, and nothing here needs OpenCV.

ponytail: one scale, one camera. Template and frames are sampled to the same
fixed grid, so a thing that moves closer or turns will not match; that is the
knob to turn if results disappoint.
"""

import subprocess

try:
    import numpy as np
except ImportError:
    np = None

W, H = 416, 234                # the grid every frame and template is sampled to
THRESHOLD = 0.8


def available():
    return np is not None


def _integral(a):
    ii = np.cumsum(np.cumsum(a, 0), 1)
    return np.pad(ii, ((1, 0), (1, 0)))


def _windows(ii, h, w):
    """Sum over every h x w window, from an integral image."""
    return ii[h:, w:] - ii[:-h, w:] - ii[h:, :-w] + ii[:-h, :-w]


def score(frame, template):
    """Best normalized cross-correlation of template anywhere in frame, -1..1."""
    h, w = template.shape
    if h > frame.shape[0] or w > frame.shape[1]:
        return -1.0
    t = template - template.mean()
    energy = float(np.sqrt((t * t).sum()))
    # A patch with no detail (a blank wall, a white square) correlates with
    # every other flat patch, and dividing by its tiny energy invents matches.
    # Both the template and each window it lands on must have some contrast.
    floor = float(np.sqrt(t.size))
    if energy < floor:
        return -1.0
    f = np.fft.rfft2(frame)
    g = np.fft.rfft2(t[::-1, ::-1], s=frame.shape)
    corr = np.fft.irfft2(f * g, s=frame.shape)[h - 1:, w - 1:]
    s1 = _windows(_integral(frame), h, w)
    s2 = _windows(_integral(frame * frame), h, w)
    local = np.sqrt(np.maximum(s2 - s1 * s1 / (h * w), 0))
    return float(np.max(np.where(local >= floor, corr / (np.maximum(local, 1e-6) * energy),
                                 -1.0)))


def _raw(args, width, height, stdin=None):
    """Grayscale frames out of ffmpeg, as a list of float arrays."""
    p = subprocess.run(["ffmpeg", "-v", "error"] + args
                       + ["-pix_fmt", "gray", "-f", "rawvideo", "-"],
                       input=stdin, capture_output=True, timeout=300)
    n = width * height
    data = p.stdout[:len(p.stdout) // n * n]
    if not data:
        return []
    a = np.frombuffer(data, np.uint8).reshape(-1, height, width).astype(np.float32)
    return list(a)


def template_from(jpeg, box):
    """The picture cropped to box (x, y, w, h as 0-1 fractions), on our grid."""
    x, y, w, h = box
    vf = ("crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f,scale=%d:%d"
          % (max(w, 0.02), max(h, 0.02), x, y,
             max(int(round(w * W)), 8), max(int(round(h * H)), 8)))
    frames = _raw(["-i", "-", "-vf", vf], max(int(round(w * W)), 8),
                  max(int(round(h * H)), 8), stdin=jpeg)
    return frames[0] if frames else None


def frames_of(url, seconds=None):
    """One frame per second of a recording, oldest first."""
    args = ["-i", url, "-vf", "fps=1,scale=%d:%d" % (W, H)]
    if seconds:
        args = ["-t", str(seconds)] + args
    return _raw(args, W, H)


def search_event(url, template, threshold=THRESHOLD):
    """[(second, score)] for every second the template is visible."""
    hits = []
    for i, frame in enumerate(frames_of(url)):
        s = score(frame, template)
        if s >= threshold:
            hits.append((i, round(s, 3)))
    return hits


if __name__ == "__main__":       # self-check: a planted square is found, noise is not
    rng = np.random.default_rng(0)
    frame = rng.random((H, W)).astype(np.float32) * 50
    patch = (rng.random((24, 32)).astype(np.float32) * 200)
    frame[100:124, 200:232] = patch
    assert score(frame, patch) > 0.99, score(frame, patch)
    assert score(frame, rng.random((24, 32)).astype(np.float32) * 200) < THRESHOLD
    print("ok")
