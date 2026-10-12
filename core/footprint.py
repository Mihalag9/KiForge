"""Модель данных KiForge для формата PCBNEW-LibModule-V1 (.mod).

Все координаты и размеры — в мм. В файле .mod они в единицах 0.1 мил
(1 unit = 0.00254 мм), конвертация — в io_kicad.units.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Pad:
    number: str = "1"
    shape: str = "C"                    # C=circle R=rect O=oval T=trapezoid
    x: float = 0.0
    y: float = 0.0
    width: float = 1.524
    height: float = 1.524
    pad_dx: int = 0                     # в файле это Ydelta, обычно 0
    pad_dy: int = 0                     # в файле это Xdelta, обычно 0
    pad_angle: int = 0

    # --- Отверстие -----------------------------------------------------------
    # Строка "Dr" пада (legacy KiCad, единицы 0.1 мил):
    #     Dr <size> <x_off> <y_off>              — круглое
    #     Dr <size> <x_off> <y_off> O <w> <h>    — овальное (slot)
    # <size>  — диаметр; <x_off>/<y_off> — независимые смещения центра
    # отверстия относительно центра пада. Храним в мм.
    drill: float | None = None
    drill_offset_x: float = 0.0
    drill_offset_y: float = 0.0

    attachment: str = "STD"             # STD | SMD | CONN | HOLE
    attach_flag: str = "N"
    flags: str = "00E0FFFF"
    net: int = 0
    net_name: str = ""
    extra: list[str] = field(default_factory=list)

    @property
    def is_smd(self) -> bool:
        return self.drill is None or self.drill <= 0.0

    @property
    def has_drill(self) -> bool:
        return self.drill is not None and self.drill > 0.0


@dataclass
class FpText:
    kind: str = "reference"             # reference (T0) / value (T1) / user (T2)
    text: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 1.524                # W (в файле идёт ВТОРЫМ после H)
    height: float = 1.524               # H (в файле идёт ПЕРВЫМ)
    rotation: float = 0.0               # градусы (в файле 0.1°)
    thickness: float = 0.3048
    mirror: bool = False                # M/N
    visible: bool = True                # V/I
    layer: str = "F.SilkS"
    italic: bool = False                # I/N
    extra: list[str] = field(default_factory=list)


@dataclass
class FpLine:
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    width: float = 0.381
    layer: str = "F.SilkS"
    extra: list[str] = field(default_factory=list)


@dataclass
class FpCircle:
    cx: float = 0.0
    cy: float = 0.0
    ex: float = 0.0
    ey: float = 0.0
    width: float = 0.381
    layer: str = "F.SilkS"
    extra: list[str] = field(default_factory=list)

    @property
    def radius(self) -> float:
        return ((self.ex - self.cx) ** 2 + (self.ey - self.cy) ** 2) ** 0.5


@dataclass
class FpArc:
    cx: float = 0.0
    cy: float = 0.0
    sx: float = 0.0
    sy: float = 0.0
    angle: float = 90.0                 # в градусах (в файле — 0.1°)
    width: float = 0.381
    layer: str = "F.SilkS"
    extra: list[str] = field(default_factory=list)


@dataclass
class Footprint:
    name: str = "New_Module"

    # служебные поля Po-строки модуля
    po_x: int = 0
    po_y: int = 0
    po_rotation: int = 0
    po_layer: int = 15                  # 15 = B.Cu — как в KiCad по умолчанию
    po_tstamp: str = "00000000"         # Tedit (hex)
    po_flags: str = "00000000"          # Tstamp (обычно всегда нули)
    po_misc: str = "~~"                 # Attributes (2 символа, обычно ~~)

    # строки идентификации модуля — идут в этом порядке после Li
    description: str = ""               # Cd
    keywords: str = ""                  # Kw

    sc: str = "00000000"
    ar: str = ""
    op: str = "0 0 0"

    texts: list[FpText] = field(default_factory=list)
    lines: list[FpLine] = field(default_factory=list)
    circles: list[FpCircle] = field(default_factory=list)
    arcs: list[FpArc] = field(default_factory=list)
    pads: list[Pad] = field(default_factory=list)

    # неизвестные строки внутри $MODULE (в т.ч. $SHAPE3D, DP/Dl и т.п.)
    extra: list[str] = field(default_factory=list)


def new_footprint(name: str = "New_Module") -> Footprint:
    """Создать модуль с дефолтными T0/T1, как делает KiCad."""
    import time
    fp = Footprint(name=name)
    fp.po_tstamp = format(int(time.time()) & 0xFFFFFFFF, "08X")
    fp.texts.append(FpText(kind="reference", text=name,
                           x=0.0, y=0.0, layer="F.SilkS"))
    fp.texts.append(FpText(kind="value", text="VAL**",
                           x=0.0, y=0.0, layer="F.SilkS"))
    return fp


def next_pad_number(fp: Footprint) -> str:
    """Следующий числовой номер пада."""
    max_n = 0
    for p in fp.pads:
        try:
            n = int(str(p.number).strip())
        except (TypeError, ValueError):
            continue
        if n > max_n:
            max_n = n
    return str(max_n + 1)