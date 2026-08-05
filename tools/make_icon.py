#!/usr/bin/env python3
"""Draw the Porchlight icon: a wall lantern glowing above a doorway.

Everything is drawn at 8x and shrunk, which is cheaper than hand-rolling
anti-aliasing. python3 make_icon.py OUT256.png OUT48.png
"""

import sys

from PIL import Image, ImageDraw, ImageFilter

S = 8                      # supersample factor
N = 256 * S                # working canvas

BG_TOP = (26, 32, 44)
BG_BOTTOM = (16, 20, 28)
BRASS = (198, 152, 76)
BRASS_DARK = (140, 102, 44)
GLASS = (255, 226, 156)
FLAME = (255, 246, 214)
GLOW = (255, 190, 90)


def rounded_bg():
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    grad = Image.new("RGBA", (1, N))
    gd = ImageDraw.Draw(grad)
    for y in range(N):
        t = y / N
        gd.point((0, y), tuple(round(a + (b - a) * t) for a, b in
                               zip(BG_TOP, BG_BOTTOM)) + (255,))
    grad = grad.resize((N, N))
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, N - 1, N - 1], radius=56 * S, fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def halo(img):
    """The light the lamp throws, as a blurred cone plus a soft ball."""
    layer = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = N // 2, int(N * 0.44)
    # cone of light spilling down the wall
    d.polygon([(cx - 26 * S, cy), (cx + 26 * S, cy),
               (cx + 92 * S, N), (cx - 92 * S, N)], fill=GLOW + (70,))
    d.ellipse([cx - 78 * S, cy - 78 * S, cx + 78 * S, cy + 78 * S], fill=GLOW + (110,))
    layer = layer.filter(ImageFilter.GaussianBlur(18 * S))
    return Image.alpha_composite(img, layer)


def lantern(img):
    d = ImageDraw.Draw(img)
    cx = N // 2
    top, bottom = int(N * 0.30), int(N * 0.62)
    half_top, half_bottom = 30 * S, 46 * S

    # bracket and cap
    d.rounded_rectangle([cx - 8 * S, int(N * 0.17), cx + 8 * S, top + 4 * S],
                        radius=4 * S, fill=BRASS_DARK)
    d.ellipse([cx - 14 * S, int(N * 0.15), cx + 14 * S, int(N * 0.19)], fill=BRASS)
    d.polygon([(cx - half_top - 12 * S, top), (cx + half_top + 12 * S, top),
               (cx + half_top - 2 * S, top - 26 * S), (cx - half_top + 2 * S, top - 26 * S)],
              fill=BRASS)

    # glass body, lit from inside
    body = [(cx - half_top, top), (cx + half_top, top),
            (cx + half_bottom, bottom), (cx - half_bottom, bottom)]
    d.polygon(body, fill=GLASS)
    glass = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glass)
    gd.ellipse([cx - 30 * S, top + 14 * S, cx + 30 * S, bottom - 4 * S], fill=FLAME + (255,))
    glass = glass.filter(ImageFilter.GaussianBlur(9 * S))
    img.alpha_composite(glass)

    # frame: outline plus the two glazing bars that read as a lantern at any size
    d.line(body + [body[0]], fill=BRASS, width=6 * S, joint="curve")
    d.line([(cx, top), (cx, bottom)], fill=BRASS_DARK, width=3 * S)

    # base
    d.polygon([(cx - half_bottom - 10 * S, bottom), (cx + half_bottom + 10 * S, bottom),
               (cx + 12 * S, bottom + 22 * S), (cx - 12 * S, bottom + 22 * S)], fill=BRASS)
    return img


def main(out256, out48):
    img = rounded_bg()
    img = halo(img)
    img = lantern(img)
    big = img.resize((256, 256), Image.LANCZOS)
    big.save(out256)
    big.resize((48, 48), Image.LANCZOS).save(out48)
    print("wrote", out256, out48)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
