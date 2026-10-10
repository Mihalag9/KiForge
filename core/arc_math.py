"""Математика для дуг (чистые функции)."""
from __future__ import annotations
import math


def arc_points_from_center(cx, cy, sx, sy, angle_deg, segments: int = 64):
    """Точки дуги DA: центр (cx,cy), старт (sx,sy), угол в градусах."""
    r = math.hypot(sx - cx, sy - cy)
    if r < 1e-9:
        return [(sx, sy)]
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = a0 + math.radians(angle_deg)
    pts = []
    for i in range(segments + 1):
        t = i / segments
        a = a0 + (a1 - a0) * t
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts