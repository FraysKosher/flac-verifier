"""Generates the line-icon set in `Logo/icons/` (DESIGN.md section 8).

    python Logo/generate_icons.py

The icons are drawn with Pillow on a 24-unit grid, supersampled 8x and scaled
down with Lanczos, so the 1.5 px stroke stays even at 16 px. Nothing is
downloaded: the geometry lives in this file, which is what makes the set
reproducible and keeps the assets free of third-party licences.

Each icon is saved as `<name>_<size>.png` (16, 20 and 24 px) with a white stroke
on a transparent background; `gui.py` tints a copy at load time, so one file per
size is enough for every colour the interface needs.

The output is deliberately small (well under 100 kB): a text-coloured icon costs
a few hundred bytes.
"""
from __future__ import annotations

import os
from math import cos, radians, sin as sine

from PIL import Image, ImageDraw

SIZES = (16, 20, 24)
BOX = 24.0                     # design grid, in icon units
SUPERSAMPLE = 8
STROKE = 1.5                   # final pixels, as specified by the design brief
WHITE = (255, 255, 255)

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


class Pen:
    """Draws in 24-unit coordinates on a supersampled greyscale mask."""

    def __init__(self, draw: ImageDraw.ImageDraw, factor: float) -> None:
        self.draw = draw
        self.factor = factor
        self.width = max(1, round(STROKE * SUPERSAMPLE))

    def _point(self, point: tuple[float, float]) -> tuple[float, float]:
        return point[0] * self.factor, point[1] * self.factor

    def _dot(self, point: tuple[float, float]) -> None:
        """A round cap, which also rounds every joint of a polyline."""
        x, y = self._point(point)
        radius = self.width / 2.0
        self.draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                          fill=255)

    def line(self, points: list[tuple[float, float]], closed: bool = False) -> None:
        scaled = [self._point(point) for point in points]
        if closed and scaled[0] != scaled[-1]:
            scaled.append(scaled[0])
        self.draw.line(scaled, fill=255, width=self.width, joint="curve")
        for point in scaled:
            self._dot((point[0] / self.factor, point[1] / self.factor))

    def box(self, rect: tuple[float, float, float, float], radius: float = 0.0) -> None:
        left, top, right, bottom = (value * self.factor for value in rect)
        self.draw.rounded_rectangle((left, top, right, bottom),
                                    radius=radius * self.factor,
                                    outline=255, width=self.width)

    def circle(self, center: tuple[float, float], radius: float) -> None:
        x, y = self._point(center)
        reach = radius * self.factor
        self.draw.ellipse((x - reach, y - reach, x + reach, y + reach),
                          outline=255, width=self.width)

    def arc(self, center: tuple[float, float], radius: float,
            start: float, end: float) -> None:
        x, y = self._point(center)
        reach = radius * self.factor
        self.draw.arc((x - reach, y - reach, x + reach, y + reach),
                      start=start, end=end, fill=255, width=self.width)
        for angle in (start, end):
            self._dot((center[0] + radius * cos(radians(angle)),
                       center[1] + radius * sine(radians(angle))))

    def arc_with_head(self, center: tuple[float, float], radius: float,
                      start: float, end: float, head: float = 3.0) -> None:
        """A circular arrow: a 270-degree arc plus a corner arrowhead at its end."""
        self.arc(center, radius, start, end)
        tip = (center[0] + radius * cos(radians(end)),
               center[1] + radius * sine(radians(end)))
        self.line([(tip[0], tip[1] - head), tip, (tip[0] - head, tip[1])])


# ─── Geometry (all coordinates in the 24-unit grid) ─────────────────────────

def folder(pen: Pen) -> None:
    pen.line([(3, 19), (3, 6.5), (9, 6.5), (11, 8.5), (21, 8.5),
              (21, 19), (3, 19)])


def folder_plus(pen: Pen) -> None:
    folder(pen)
    pen.line([(12, 11.2), (12, 15.8)])
    pen.line([(9.7, 13.5), (14.3, 13.5)])


def play(pen: Pen) -> None:
    pen.line([(8.5, 5.5), (19, 12), (8.5, 18.5), (8.5, 5.5)])


def stop(pen: Pen) -> None:
    pen.box((6.5, 6.5, 17.5, 17.5), radius=2.0)


def file_text(pen: Pen) -> None:
    pen.line([(5.5, 3.5), (14, 3.5), (18.5, 8), (18.5, 20.5),
              (5.5, 20.5), (5.5, 3.5)])
    pen.line([(14, 3.5), (14, 8), (18.5, 8)])
    pen.line([(8.5, 12), (15.5, 12)])
    pen.line([(8.5, 16), (15.5, 16)])


def external_link(pen: Pen) -> None:
    pen.line([(10.5, 5.5), (4.5, 5.5), (4.5, 19.5), (18.5, 19.5),
              (18.5, 13.5)])
    pen.line([(9.5, 14.5), (19.5, 4.5)])
    pen.line([(14, 4.5), (19.5, 4.5), (19.5, 10)])


def settings(pen: Pen) -> None:
    """Gear: hub, rim and eight teeth (a sun would have a gap instead)."""
    pen.circle((12, 12), 3.0)
    pen.circle((12, 12), 6.2)
    for step in range(8):
        angle = radians(step * 45.0)
        pen.line([(12 + 6.2 * cos(angle), 12 + 6.2 * sine(angle)),
                  (12 + 8.8 * cos(angle), 12 + 8.8 * sine(angle))])


def sun(pen: Pen) -> None:
    pen.circle((12, 12), 4.0)
    for step in range(8):
        angle = radians(step * 45.0)
        pen.line([(12 + 6.8 * cos(angle), 12 + 6.8 * sine(angle)),
                  (12 + 9.6 * cos(angle), 12 + 9.6 * sine(angle))])


def moon(pen: Pen) -> None:
    """Crescent: the outer circle minus an offset one, as a single outline."""
    outer = [(12 + 8 * cos(radians(angle)),
              12 + 8 * sine(radians(angle)))
             for angle in range(61, 300, 4)]
    inner = [(16.5 + 7 * cos(radians(angle)),
              12 + 7 * sine(radians(angle)))
             for angle in range(265, 94, -4)]
    pen.line(outer + inner + [outer[0]])


def chevron_down(pen: Pen) -> None:
    pen.line([(7, 9.5), (12, 14.5), (17, 9.5)])


def chevron_right(pen: Pen) -> None:
    pen.line([(9.5, 7), (14.5, 12), (9.5, 17)])


def check(pen: Pen) -> None:
    pen.line([(5.5, 12.5), (10, 17), (18.5, 7.5)])


def cross(pen: Pen) -> None:
    pen.line([(6.5, 6.5), (17.5, 17.5)])
    pen.line([(17.5, 6.5), (6.5, 17.5)])


def alert_triangle(pen: Pen) -> None:
    pen.line([(12, 4.5), (20.5, 20), (3.5, 20), (12, 4.5)])
    pen.line([(12, 10), (12, 15)])
    pen.line([(12, 17.6), (12, 17.61)])


def info(pen: Pen) -> None:
    pen.circle((12, 12), 8.5)
    pen.line([(12, 10.8), (12, 16.8)])
    pen.line([(12, 7.4), (12, 7.41)])


def refresh(pen: Pen) -> None:
    pen.arc_with_head((12, 12), 8.0, start=90, end=359.9)


def trash(pen: Pen) -> None:
    pen.line([(4, 7), (20, 7)])
    pen.line([(9, 7), (9, 4.5), (15, 4.5), (15, 7)])
    pen.line([(6, 7), (7, 20), (17, 20), (18, 7)])
    pen.line([(10, 11), (10, 16.5)])
    pen.line([(14, 11), (14, 16.5)])


def search(pen: Pen) -> None:
    pen.circle((10.5, 10.5), 6.2)
    pen.line([(15, 15), (20, 20)])


def music(pen: Pen) -> None:
    pen.circle((7.5, 17.5), 2.4)
    pen.circle((17.5, 15.5), 2.4)
    pen.line([(9.9, 17.5), (9.9, 6.5), (19.9, 4.5), (19.9, 15.5)])


ICONS = {
    "folder": folder,
    "folder-plus": folder_plus,
    "play": play,
    "stop": stop,
    "file-text": file_text,
    "external-link": external_link,
    "settings": settings,
    "sun": sun,
    "moon": moon,
    "chevron-down": chevron_down,
    "chevron-right": chevron_right,
    "check": check,
    "x": cross,
    "alert-triangle": alert_triangle,
    "info": info,
    "refresh": refresh,
    "trash": trash,
    "search": search,
    "music": music,
}


def render(name: str, draw_icon, size: int) -> Image.Image:
    """Draws one icon at one size and returns an RGBA image."""
    canvas = size * SUPERSAMPLE
    mask = Image.new("L", (canvas, canvas), 0)
    draw_icon(Pen(ImageDraw.Draw(mask), canvas / BOX))
    mask = mask.resize((size, size), Image.LANCZOS)
    image = Image.new("RGBA", (size, size), WHITE + (0,))
    image.putalpha(mask)
    return image


def generate() -> list[str]:
    os.makedirs(OUTPUT, exist_ok=True)
    written = []
    for name, draw_icon in ICONS.items():
        for size in SIZES:
            path = os.path.join(OUTPUT, f"{name}_{size}.png")
            render(name, draw_icon, size).save(path, optimize=True)
            written.append(path)
    return written


def main() -> int:
    written = generate()
    total = sum(os.path.getsize(path) for path in written)
    print(f"{len(written)} files in {OUTPUT} ({total / 1024:.1f} kB)")
    if total >= 100_000:
        print("warning: the icon set is over the 100 kB budget of the design brief")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
