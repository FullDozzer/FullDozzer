# -*- coding: utf-8 -*-
"""Lucide icons for the schedule card.

The SVG files in ``assets/icons/`` are the upstream Lucide set (ISC).
This module rasterizes those vectors — it does not invent icon shapes.
Strokes are drawn from the SVG path data and cached as tinted bitmaps.
"""

import math
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

ICON_DIR = Path(__file__).resolve().parent / "assets" / "icons"

# Names the renderer asks for → vendored Lucide file stem.
ICON_FILES = {
    "calendar": "calendar",
    "clock": "clock",
    "person": "user",
    "user": "user",
    "cap": "graduation-cap",
    "graduation-cap": "graduation-cap",
}

_NUM = re.compile(r"[+-]?(?:\d*\.\d+|\d+)(?:[eE][+-]?\d+)?")


def _tokens(d: str) -> list:
    out = []
    i = 0
    n = len(d)
    while i < n:
        ch = d[i]
        if ch.isalpha():
            out.append(ch)
            i += 1
            continue
        if ch in " ,\t\r\n":
            i += 1
            continue
        match = _NUM.match(d, i)
        if not match:
            i += 1
            continue
        out.append(float(match.group()))
        i = match.end()
    return out


def _angle(ux, uy, vx, vy) -> float:
    return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)


def _arc_points(x1, y1, rx, ry, phi_deg, large, sweep, x2, y2) -> list:
    if math.hypot(x2 - x1, y2 - y1) < 1e-6:
        return []
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    phi = math.radians(phi_deg % 360.0)
    rx, ry = abs(rx), abs(ry)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        scale = math.sqrt(lam)
        rx *= scale
        ry *= scale
    sign = -1.0 if bool(large) == bool(sweep) else 1.0
    num = max(0.0, rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p)
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = sign * math.sqrt(num / den) if den else 0.0
    cxp = coef * rx * y1p / ry
    cyp = coef * -ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0
    theta1 = _angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = _angle(
        (x1p - cxp) / rx, (y1p - cyp) / ry,
        (-x1p - cxp) / rx, (-y1p - cyp) / ry,
    )
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi
    steps = max(12, int(abs(dtheta) / (math.pi / 24.0)))
    points = []
    for i in range(1, steps + 1):
        t = theta1 + dtheta * i / steps
        points.append((
            cx + rx * math.cos(t) * cos_phi - ry * math.sin(t) * sin_phi,
            cy + rx * math.cos(t) * sin_phi + ry * math.sin(t) * cos_phi,
        ))
    if points:
        points[-1] = (x2, y2)
    return points


def _cubic(p0, p1, p2, p3, steps=16) -> list:
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        points.append((
            u ** 3 * p0[0] + 3 * u ** 2 * t * p1[0]
            + 3 * u * t ** 2 * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u ** 2 * t * p1[1]
            + 3 * u * t ** 2 * p2[1] + t ** 3 * p3[1],
        ))
    return points


def _quad(p0, p1, p2, steps=12) -> list:
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        points.append((
            u ** 2 * p0[0] + 2 * u * t * p1[0] + t ** 2 * p2[0],
            u ** 2 * p0[1] + 2 * u * t * p1[1] + t ** 2 * p2[1],
        ))
    return points


def path_polylines(d: str) -> list:
    """SVG path → list of (points, closed)."""
    tokens = _tokens(d)
    i = 0
    cmd = None
    x = y = 0.0
    sx = sy = 0.0
    cx_ctrl = cy_ctrl = 0.0
    qx_ctrl = qy_ctrl = 0.0
    current: list = []
    lines: list = []

    def take(count):
        nonlocal i
        vals = [float(v) for v in tokens[i:i + count]]
        i += count
        return vals

    def flush(closed=False):
        nonlocal current
        if len(current) >= 2:
            lines.append((current, closed))
        current = []

    while i < len(tokens):
        if isinstance(tokens[i], str):
            cmd = tokens[i]
            i += 1
        if cmd is None:
            break
        if cmd in ("M", "m"):
            nx, ny = take(2)
            if cmd == "m":
                nx += x
                ny += y
            flush(False)
            current = [(nx, ny)]
            x, y = sx, sy = nx, ny
            cmd = "L" if cmd == "M" else "l"
        elif cmd in ("L", "l"):
            nx, ny = take(2)
            if cmd == "l":
                nx += x
                ny += y
            current.append((nx, ny))
            x, y = nx, ny
        elif cmd in ("H", "h"):
            nx = take(1)[0]
            if cmd == "h":
                nx += x
            current.append((nx, y))
            x = nx
        elif cmd in ("V", "v"):
            ny = take(1)[0]
            if cmd == "v":
                ny += y
            current.append((x, ny))
            y = ny
        elif cmd in ("C", "c"):
            x1, y1, x2, y2, nx, ny = take(6)
            if cmd == "c":
                x1 += x
                y1 += y
                x2 += x
                y2 += y
                nx += x
                ny += y
            current.extend(_cubic((x, y), (x1, y1), (x2, y2), (nx, ny)))
            cx_ctrl, cy_ctrl = x2, y2
            x, y = nx, ny
        elif cmd in ("S", "s"):
            x2, y2, nx, ny = take(4)
            if cmd == "s":
                x2 += x
                y2 += y
                nx += x
                ny += y
            x1, y1 = 2 * x - cx_ctrl, 2 * y - cy_ctrl
            current.extend(_cubic((x, y), (x1, y1), (x2, y2), (nx, ny)))
            cx_ctrl, cy_ctrl = x2, y2
            x, y = nx, ny
        elif cmd in ("Q", "q"):
            x1, y1, nx, ny = take(4)
            if cmd == "q":
                x1 += x
                y1 += y
                nx += x
                ny += y
            current.extend(_quad((x, y), (x1, y1), (nx, ny)))
            qx_ctrl, qy_ctrl = x1, y1
            x, y = nx, ny
        elif cmd in ("T", "t"):
            nx, ny = take(2)
            if cmd == "t":
                nx += x
                ny += y
            x1, y1 = 2 * x - qx_ctrl, 2 * y - qy_ctrl
            current.extend(_quad((x, y), (x1, y1), (nx, ny)))
            qx_ctrl, qy_ctrl = x1, y1
            x, y = nx, ny
        elif cmd in ("A", "a"):
            rx, ry, rot, large, sweep, nx, ny = take(7)
            if cmd == "a":
                nx += x
                ny += y
            current.extend(_arc_points(
                x, y, rx, ry, rot, int(large), int(sweep), nx, ny,
            ))
            x, y = nx, ny
        elif cmd in ("Z", "z"):
            if current and current[-1] != (sx, sy):
                current.append((sx, sy))
            flush(True)
            x, y = sx, sy
        else:
            raise ValueError(f"unsupported SVG path command {cmd!r}")
        if cmd not in ("C", "c", "S", "s"):
            cx_ctrl, cy_ctrl = x, y
        if cmd not in ("Q", "q", "T", "t"):
            qx_ctrl, qy_ctrl = x, y
    flush(False)
    return lines


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el, name, default=0.0) -> float:
    value = el.attrib.get(name)
    if value is None or value == "":
        return default
    return float(str(value).replace("px", ""))


def _stroke_width(el, default=2.0) -> float:
    raw = el.attrib.get("stroke-width", "")
    if not raw:
        return default
    return float(str(raw).replace("px", ""))


def _stroke(draw, points, width: float, closed: bool = False) -> None:
    if len(points) < 2 or width <= 0:
        return
    seq = [(float(px), float(py)) for px, py in points]
    if closed and seq[0] != seq[-1]:
        seq.append(seq[0])
    radius = width / 2.0
    draw.line(seq, fill=255, width=max(1, int(round(width))), joint="curve")
    for px, py in seq:
        draw.ellipse(
            (px - radius, py - radius, px + radius, py + radius), fill=255,
        )


def _raster_mask(path: Path, size: int) -> Image.Image:
    root = ET.parse(path).getroot()
    view = root.attrib.get("viewBox", "0 0 24 24").split()
    min_x, min_y, vb_w, vb_h = (float(v) for v in view)
    default_sw = _stroke_width(root, 2.0)
    ss = 6
    hi = max(size * ss, 64)
    scale = hi / max(vb_w, vb_h)
    mask = Image.new("L", (hi, hi), 0)
    draw = ImageDraw.Draw(mask)

    def xy(px, py):
        return ((px - min_x) * scale, (py - min_y) * scale)

    def paint(points, width_units, closed=False):
        _stroke(draw, [xy(px, py) for px, py in points],
                width_units * scale, closed)

    for el in root.iter():
        tag = _local(el.tag)
        sw = _stroke_width(el, default_sw)
        if tag == "path" and el.attrib.get("d"):
            for points, closed in path_polylines(el.attrib["d"]):
                paint(points, sw, closed)
        elif tag == "line":
            paint([(_attr(el, "x1"), _attr(el, "y1")),
                   (_attr(el, "x2"), _attr(el, "y2"))], sw)
        elif tag in ("polyline", "polygon"):
            nums = [float(v) for v in _NUM.findall(el.attrib.get("points", ""))]
            paint(list(zip(nums[0::2], nums[1::2])), sw, tag == "polygon")
        elif tag == "rect":
            x, y = _attr(el, "x"), _attr(el, "y")
            w, h = _attr(el, "width"), _attr(el, "height")
            rx = _attr(el, "rx", _attr(el, "ry"))
            ry = _attr(el, "ry", rx)
            rx, ry = min(rx, w / 2), min(ry, h / 2)
            if rx <= 0 and ry <= 0:
                paint([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], sw, True)
            else:
                d = (
                    f"M{x + rx} {y} H{x + w - rx} "
                    f"A{rx} {ry} 0 0 1 {x + w} {y + ry} "
                    f"V{y + h - ry} A{rx} {ry} 0 0 1 {x + w - rx} {y + h} "
                    f"H{x + rx} A{rx} {ry} 0 0 1 {x} {y + h - ry} "
                    f"V{y + ry} A{rx} {ry} 0 0 1 {x + rx} {y} Z"
                )
                for points, closed in path_polylines(d):
                    paint(points, sw, closed)
        elif tag == "circle":
            cx, cy, r = _attr(el, "cx"), _attr(el, "cy"), _attr(el, "r")
            steps = 72
            paint([
                (cx + r * math.cos(2 * math.pi * i / steps),
                 cy + r * math.sin(2 * math.pi * i / steps))
                for i in range(steps)
            ], sw, True)
    return mask.resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=16)
def _master_mask(stem: str) -> Image.Image:
    path = ICON_DIR / f"{stem}.svg"
    if not path.exists():
        raise FileNotFoundError(f"Lucide icon SVG not found: {path}")
    return _raster_mask(path, 256)


def _hex_rgb(color: str) -> tuple:
    text = str(color).lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


@lru_cache(maxsize=192)
def icon_image(name: str, size: int, color: str) -> Image.Image:
    """Square RGBA icon. ``size`` is the bitmap edge in pixels."""
    stem = ICON_FILES.get(name, name)
    side = max(1, int(size))
    mask = _master_mask(stem).resize((side, side), Image.LANCZOS)
    rgb = _hex_rgb(color)
    return Image.merge("RGBA", (
        Image.new("L", mask.size, rgb[0]),
        Image.new("L", mask.size, rgb[1]),
        Image.new("L", mask.size, rgb[2]),
        mask,
    ))
