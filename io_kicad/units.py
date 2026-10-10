"""0.1 мил ↔ мм.

В .mod все координаты целые в единицах 0.1 мил:
1 unit = 0.0001 inch = 0.00254 мм.
"""
from __future__ import annotations

MM_PER_UNIT = 0.00254


def units_to_mm(value) -> float:
    return float(value) * MM_PER_UNIT


def mm_to_units(mm: float) -> int:
    return int(round(mm / MM_PER_UNIT))


def parse_int(token, default: int = 0) -> int:
    try:
        return int(token)
    except (TypeError, ValueError):
        try:
            return int(round(float(token)))
        except (TypeError, ValueError):
            return default