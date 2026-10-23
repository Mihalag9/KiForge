"""Модель legacy-платы KiCad (PCBNEW-BOARD Version 1/2).

Координаты всех объектов — в мм. Модули переиспользуют ``Footprint`` из
``core.footprint``: внутри ``$MODULE`` структура совпадает с библиотечным
модулем. Локальные координаты модуля тоже в мм, абсолютные вычисляются
функциями ``module_transform`` / ``module_pad_position``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .footprint import Footprint, Pad


@dataclass
class BoardTrack:
    shape: int = 0
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    width: float = 0.0
    layer: str = "F.Cu"
    kind: int = 0                       # 0 = дорожка, 1 = via
    net: int = 0
    timestamp: str = "0"
    status: str = "0"

    @property
    def is_via(self) -> bool:
        return self.kind == 1

    @property
    def is_track(self) -> bool:
        return self.kind != 1


@dataclass
class BoardDrawing:
    shape: int = 0                      # 0 = линия, 1 = круг, 2 = дуга
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    width: float = 0.0
    angle: float = 0.0
    layer: str = "Edge.Cuts"
    timestamp: str = "0"
    status: str = "0"


@dataclass
class BoardText:
    text: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0
    rotation: float = 0.0
    layer: str = "F.SilkS"
    mirror: bool = False
    style: str = "Normal"


@dataclass
class Board:
    version: int = 1
    header_date: str = ""
    general: dict[str, str] = field(default_factory=dict)
    sheet: dict[str, str] = field(default_factory=dict)
    setup_lines: list[str] = field(default_factory=list)
    nets: dict[int, str] = field(default_factory=dict)
    netclass_lines: list[str] = field(default_factory=list)
    modules: list[Footprint] = field(default_factory=list)
    drawings: list[BoardDrawing] = field(default_factory=list)
    tracks: list[BoardTrack] = field(default_factory=list)
    zone_segments: list[BoardTrack] = field(default_factory=list)
    texts: list[BoardText] = field(default_factory=list)
    zone_outlines: list[dict] = field(default_factory=list)
    unknown_blocks: list[str] = field(default_factory=list)


def module_transform(fp: Footprint) -> tuple[float, float, float, bool]:
    """Позиция модуля: (x, y, поворот в градусах, зеркалён ли)."""
    return (float(fp.po_x), float(fp.po_y),
            fp.po_rotation / 10.0, fp.po_layer == 0)


def module_pad_position(fp: Footprint, pad: Pad) -> tuple[float, float]:
    """Абсолютный центр пада с учётом поворота и зеркалирования модуля."""
    mx, my, angle, mirrored = module_transform(fp)
    x, y = pad.x, pad.y
    if mirrored:
        # Модуль на B.Cu зеркалится относительно оси X.
        y = -y
    if angle:
        rad = math.radians(angle)
        c, s = math.cos(rad), math.sin(rad)
        x, y = x * c + y * s, -x * s + y * c
    return mx + x, my + y


def ratsnest_segments(board: Board) -> list[tuple[tuple[float, float],
                                                   tuple[float, float]]]:
    """Минимальные связующие деревья по цепям (подсказка, не DRC).

    Для каждой цепи собираются абсолютные центры падов; при двух и более
    падах строится MST алгоритмом Прима. Цепь 0 (no-connect) пропускается.
    """
    by_net: dict[int, list[tuple[float, float]]] = {}
    for fp in board.modules:
        for pad in fp.pads:
            if pad.net <= 0:
                continue
            by_net.setdefault(pad.net, []).append(module_pad_position(fp, pad))

    result: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for points in by_net.values():
        if len(points) < 2:
            continue
        in_tree = [0]
        rest = list(range(1, len(points)))
        while rest:
            best = None
            for i in in_tree:
                for j in rest:
                    d = math.dist(points[i], points[j])
                    if best is None or d < best[0]:
                        best = (d, i, j)
            assert best is not None
            _, i, j = best
            result.append((points[i], points[j]))
            in_tree.append(j)
            rest.remove(j)
    return result
