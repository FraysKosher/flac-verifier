"""Generates the FLAC VERIFIER logo assets.

The design is defined here, in the constants below (background #0f0f0d, five white
bars of 8 px with radius 3 and the teal check #4f98a3), and this script renders it
in units so that it can be rasterised at the sizes the application needs. All the
proportions in this file are the single source of truth for the brand mark.

    python Logo/generar_logo.py        # rewrites the assets of this folder

It only needs Pillow (also used to render the spectrograms).
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))

# ─── The design, in the units of the splash ──────────────────────────────────
BACKGROUND   = "#0f0f0d"      # splash background
BAR   = "#ffffff"      # .bar { background: #ffffff }
TEAL    = "#4f98a3"      # .subtitle and the check
HEIGHTS = (28, 44, 60, 36, 52)   # .bar:nth-child(n) { height: ... }
BAR_WIDTH = 8          # .bar { width: 8px }
GAP       = 5          # .bars { gap: 5px }
BAR_RADIUS = 3          # .bar { border-radius: 3px 3px 0 0 }
TOTAL_HEIGHT  = 60         # the tallest bar
# .check { right: -4px; bottom: -4px } with a 24x18 SVG and a stroke of 3
CHECK_POINTS = ((2, 10), (8, 16), (22, 2))
CHECK_BOX   = (24, 18)
CHECK_OVERHANG = 4
CHECK_STROKE  = 3

WIDTH_UNITS = len(HEIGHTS) * BAR_WIDTH + (len(HEIGHTS) - 1) * GAP   # 60
HEIGHT_UNITS  = TOTAL_HEIGHT + CHECK_OVERHANG                               # 64
WIDTH_WITH_CHECK = WIDTH_UNITS + CHECK_OVERHANG                          # 64


def draw_brand(image: Image.Image, x: float, y: float, height: float,
                  bars: bool = True, check: bool = True) -> None:
    """Draws the bars and the check inside a square of side (alto) at (x, y)."""
    scale = height / HEIGHT_UNITS
    pen = ImageDraw.Draw(image)

    def px(u: float) -> float:                       # units -> pixels
        return u * scale

    base = y + px(TOTAL_HEIGHT)                        # the bars rest on the bottom
    if bars:
        for index, height in enumerate(HEIGHTS):
            left = x + px(index * (BAR_WIDTH + GAP))
            right = left + px(BAR_WIDTH)
            top = base - px(height)
            pen.rounded_rectangle(
                [left, top, right, base],
                radius=max(1, px(BAR_RADIUS)), fill=BAR, corners=(True, True, False, False))

    if check:
        # the check lives in its own box and sticks out to the right and downwards
        box_x = x + px(WIDTH_UNITS + CHECK_OVERHANG - CHECK_BOX[0])
        box_y = base + px(CHECK_OVERHANG - CHECK_BOX[1])
        points = [(box_x + px(px_x), box_y + px(px_y)) for px_x, px_y in CHECK_POINTS]
        thickness = max(1, round(px(CHECK_STROKE)))
        pen.line(points, fill=TEAL, width=thickness, joint="curve")
        radius = thickness / 2                          # rounded ends
        for point in (points[0], points[-1]):
            pen.ellipse([point[0] - radius, point[1] - radius,
                           point[0] + radius, point[1] + radius], fill=TEAL)


def mark(side: int, background: str | None, margin: float = 0.16) -> Image.Image:
    """Square mark. With `fondo` the rounded tile of the icon is drawn."""
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    if background:
        pen = ImageDraw.Draw(image)
        pen.rounded_rectangle([0, 0, side - 1, side - 1],
                                radius=int(side * 0.22), fill=background)
    available = side * (1 - 2 * margin)
    draw_brand(image, (side - available) / 2, (side - available) / 2, available)
    return image


def svg() -> str:
    """Vector version with the same geometry, for GitHub and the web."""
    radius = BAR_RADIUS
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH_WITH_CHECK}" '
        f'height="{HEIGHT_UNITS}" viewBox="0 0 {WIDTH_WITH_CHECK} {HEIGHT_UNITS}" '
        f'fill="none" role="img" aria-label="FLAC Verifier">',
        '  <title>FLAC Verifier</title>',
        f'  <rect width="{WIDTH_WITH_CHECK}" height="{HEIGHT_UNITS}" rx="14" fill="{BACKGROUND}"/>',
    ]
    for index, height in enumerate(HEIGHTS):
        x = 2 + index * (BAR_WIDTH + GAP)
        parts.append(f'  <rect x="{x}" y="{60 - height}" width="{BAR_WIDTH}" '
                      f'height="{height}" rx="{radius}" fill="{BAR}"/>')
    points = " ".join(f"{40 + px_x},{46 + px_y}" for px_x, px_y in CHECK_POINTS)
    parts.append(f'  <polyline points="{points}" stroke="{TEAL}" '
                  f'stroke-width="{CHECK_STROKE}" stroke-linecap="round" '
                  f'stroke-linejoin="round"/>')
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def main() -> None:
    generated = []

    square = mark(1024, BACKGROUND)
    path = os.path.join(HERE, "logo_flac_verifier.png")
    square.save(path)
    generated.append(path)

    transparent = mark(1024, None)
    path = os.path.join(HERE, "logo_flac_verifier_transparente.png")
    transparent.save(path)
    generated.append(path)

    # Small version for iconphoto (Tk reads PNG, but it is not advisable to hand it 1024 px)
    path = os.path.join(HERE, "logo_flac_verifier_128.png")
    mark(128, BACKGROUND).save(path)
    generated.append(path)

    # Multi-resolution icon for the window and for the future .exe
    path = os.path.join(HERE, "icono_app.ico")
    mark(256, BACKGROUND).save(path, format="ICO",
                           sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                  (64, 64), (128, 128), (256, 256)])
    generated.append(path)

    # Vector version, for the web and for the GitHub README
    path = os.path.join(HERE, "logo_flac_verifier.svg")
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg())
    generated.append(path)

    print("Logo assets generated from the splash design:")
    for path in generated:
        print(f"  {os.path.basename(path):38s} {os.path.getsize(path) / 1024:8.1f} KB")


if __name__ == "__main__":
    main()
