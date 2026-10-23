"""Номера слоёв legacy KiCad ↔ имена."""
from __future__ import annotations

LAYER_NUM_TO_NAME: dict[int, str] = {
    0: "F.Cu",
    1: "In1.Cu", 2: "In2.Cu", 3: "In3.Cu", 4: "In4.Cu",
    5: "In5.Cu", 6: "In6.Cu", 7: "In7.Cu", 8: "In8.Cu",
    9: "In9.Cu", 10: "In10.Cu", 11: "In11.Cu", 12: "In12.Cu",
    13: "In13.Cu", 14: "In14.Cu",
    15: "B.Cu",
    16: "B.Adhes", 17: "F.Adhes",
    18: "B.Paste", 19: "F.Paste",
    20: "B.SilkS", 21: "F.SilkS",
    22: "B.Mask", 23: "F.Mask",
    24: "Dwgs.User", 25: "Cmts.User",
    26: "Eco1.User", 27: "Eco2.User",
    28: "Edge.Cuts", 29: "Margin",
    30: "B.CrtYd", 31: "F.CrtYd",
    32: "B.Fab", 33: "F.Fab",
}

LAYER_NAME_TO_NUM: dict[str, int] = {v: k for k, v in LAYER_NUM_TO_NAME.items()}

# Нумерация слоёв именно legacy-платы: 0 = B.Cu (Back), 15 = F.Cu (Front).
# Для .mod (LAYER_NUM_TO_NAME выше) медь традиционно считается наоборот,
# поэтому таблица отдельная и существующее поведение не меняется.
BOARD_LAYER_NUM_TO_NAME: dict[int, str] = {
    0: "B.Cu",
    1: "In1.Cu", 2: "In2.Cu", 3: "In3.Cu", 4: "In4.Cu",
    5: "In5.Cu", 6: "In6.Cu", 7: "In7.Cu", 8: "In8.Cu",
    9: "In9.Cu", 10: "In10.Cu", 11: "In11.Cu", 12: "In12.Cu",
    13: "In13.Cu", 14: "In14.Cu",
    15: "F.Cu",
    16: "B.Adhes", 17: "F.Adhes",
    18: "B.Paste", 19: "F.Paste",
    20: "B.SilkS", 21: "F.SilkS",
    22: "B.Mask", 23: "F.Mask",
    24: "Dwgs.User", 25: "Cmts.User",
    26: "Eco1.User", 27: "Eco2.User",
    28: "Edge.Cuts", 29: "Margin",
    30: "B.CrtYd", 31: "F.CrtYd",
}


def num_to_name(num: int) -> str:
    return LAYER_NUM_TO_NAME.get(num, f"L{num}")


def board_layer_name(num: int) -> str:
    """Имя слоя платы; для via старшие биты содержат конечный слой."""
    if num not in BOARD_LAYER_NUM_TO_NAME:
        num = num & 0x0F
    return BOARD_LAYER_NUM_TO_NAME.get(num, f"L{num}")


def name_to_num(name: str) -> int:
    return LAYER_NAME_TO_NUM.get(name, 21)


def parse_layer(token) -> str:
    try:
        return num_to_name(int(token))
    except (TypeError, ValueError):
        return str(token)