"""Чтение legacy-платы KiCad (``PCBNEW-BOARD Version 1/2``) в ``core.board``.

V1 хранит координаты целыми в 1/10000 дюйма, V2 — float в мм. Файл только
читается: ничего не сохраняется и не изменяется.
"""
from __future__ import annotations

import re

from core.board import Board, BoardDrawing, BoardText, BoardTrack
from . import reader
from .layers import board_layer_name
from .units import parse_int, units_to_mm

_VERSION_RE = re.compile(r"Version\s+(\d+)", re.IGNORECASE)


def read(text: str) -> Board:
    lines = text.splitlines()
    board = Board()
    if not lines:
        return board

    first = lines[0].strip()
    m = _VERSION_RE.search(first)
    if m:
        board.version = int(m.group(1))
    if " date " in first:
        board.header_date = first.split(" date ", 1)[1].strip()

    to_mm = _mm_converter(board.version)

    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("$GENERAL"):
            board.general, i = _read_key_values(lines, i + 1, "$EndGENERAL")
        elif s.startswith("$SHEETDESCR"):
            board.sheet, i = _read_key_values(lines, i + 1, "$EndSHEETDESCR")
        elif s.startswith("$SETUP"):
            board.setup_lines, i = _collect_until(lines, i + 1, "$EndSETUP")
        elif s.startswith("$EQUIPOT"):
            i = _read_equipot(lines, i, board)
        elif s.startswith("$NCLASS"):
            board.netclass_lines, i = _collect_until(lines, i + 1, "$EndNCLASS")
        elif s.startswith("$MODULE"):
            i = _read_board_module(lines, i, board, to_mm)
        elif s.startswith("$DRAWSEGMENT"):
            i = _read_drawsegment(lines, i, board, to_mm)
        elif s.startswith("$TRACK"):
            items, i = _read_track_section(lines, i, to_mm)
            board.tracks.extend(items)
        elif s.startswith("$ZONE"):
            items, i = _read_track_section(lines, i, to_mm)
            board.zone_segments.extend(items)
        elif s.startswith("$TEXTPCB"):
            i = _read_textpcb(lines, i, board, to_mm)
        elif s.startswith("$CZONE_OUTLINE"):
            i = _read_czone(lines, i, board, to_mm)
        elif s.startswith("$EndBOARD"):
            break
        elif s.startswith("$End"):
            i += 1
        elif s.startswith("$"):
            raw, i = _read_unknown(lines, i)
            board.unknown_blocks.append("\n".join(raw))
        else:
            i += 1
    return board


def _mm_converter(version: int):
    if version >= 2:
        def to_mm(token) -> float:
            try:
                return float(token)
            except (TypeError, ValueError):
                return 0.0
        return to_mm
    return lambda token: units_to_mm(parse_int(token))


# --------------------------------------------------------------------------
# Простые секции
# --------------------------------------------------------------------------

def _read_key_values(lines, i, end_tag):
    values: dict[str, str] = {}
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        i += 1
        if not s:
            continue
        if s.startswith(end_tag):
            break
        parts = s.split(None, 1)
        values[parts[0]] = parts[1] if len(parts) > 1 else ""
    return values, i


def _collect_until(lines, i, end_tag):
    collected: list[str] = []
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        i += 1
        if s.startswith(end_tag):
            break
        collected.append(lines[i - 1])
    return collected, i


def _read_equipot(lines, i, board: Board):
    n = len(lines)
    i += 1
    while i < n:
        s = lines[i].strip()
        i += 1
        if s.startswith("$EndEQUIPOT"):
            break
        if s.startswith("Na "):
            parts = s.split(None, 1)
            rest = parts[1] if len(parts) > 1 else ""
            num_part, _, quoted = rest.partition(" ")
            number = parse_int(num_part)
            name, _ = reader._parse_quoted(quoted)
            board.nets[number] = name
    return i


def _read_unknown(lines, i):
    header = lines[i].strip()
    name = header.split(None, 1)[0][1:]
    end_tag = f"$End{name}".lower()
    raw = [lines[i]]
    i += 1
    n = len(lines)
    while i < n:
        raw.append(lines[i])
        stripped = lines[i].strip()
        i += 1
        if stripped.lower().startswith(end_tag):
            break
    return raw, i


# --------------------------------------------------------------------------
# Модули
# --------------------------------------------------------------------------

def _read_board_module(lines, i, board: Board, to_mm):
    start = i
    fp, i = reader._read_module(lines, i, to_mm=to_mm)
    # _read_module читает Po как есть (в .mod это единицы); для платы
    # приводим абсолютную позицию к мм тем же конвертером.
    for line in lines[start:i]:
        s = line.strip()
        if s.startswith("Po "):
            parts = s.split()
            if len(parts) >= 3:
                fp.po_x = to_mm(parts[1])
                fp.po_y = to_mm(parts[2])
            break
    board.modules.append(fp)
    return i


# --------------------------------------------------------------------------
# Графика платы
# --------------------------------------------------------------------------

def _read_drawsegment(lines, i, board: Board, to_mm):
    d = BoardDrawing()
    n = len(lines)
    i += 1
    while i < n:
        s = lines[i].strip()
        i += 1
        if s.startswith("$EndDRAWSEGMENT"):
            break
        if s.startswith("Po "):
            p = s.split()
            if len(p) >= 7:
                d.shape = parse_int(p[1])
                d.x1, d.y1 = to_mm(p[2]), to_mm(p[3])
                d.x2, d.y2 = to_mm(p[4]), to_mm(p[5])
                d.width = to_mm(p[6])
        elif s.startswith("De "):
            p = s.split()
            if len(p) >= 2:
                d.layer = board_layer_name(parse_int(p[1]))
            if len(p) >= 3:
                d.angle = parse_int(p[2]) / 10.0
            if len(p) >= 4:
                d.timestamp = p[3]
            if len(p) >= 5:
                d.status = p[4]
    board.drawings.append(d)
    return i


def _read_track_section(lines, i, to_mm):
    items: list[BoardTrack] = []
    pending = None
    n = len(lines)
    i += 1
    while i < n:
        s = lines[i].strip()
        i += 1
        if s.startswith("$EndTRACK") or s.startswith("$EndZONE"):
            break
        if not s:
            continue
        if s.startswith("Po "):
            p = s.split()
            # В $TRACK после ширины иногда стоит лишний токен -1:
            # первые 6 чисел Po всегда shape x1 y1 x2 y2 width.
            if len(p) >= 7:
                pending = BoardTrack(
                    shape=parse_int(p[1]),
                    x1=to_mm(p[2]), y1=to_mm(p[3]),
                    x2=to_mm(p[4]), y2=to_mm(p[5]),
                    width=to_mm(p[6]),
                )
        elif s.startswith("De ") and pending is not None:
            p = s.split()
            if len(p) >= 2:
                pending.layer = board_layer_name(parse_int(p[1]))
            if len(p) >= 3:
                pending.kind = parse_int(p[2])
            if len(p) >= 4:
                pending.net = parse_int(p[3])
            if len(p) >= 5:
                pending.timestamp = p[4]
            if len(p) >= 6:
                pending.status = p[5]
            items.append(pending)
            pending = None
    return items, i


def _read_textpcb(lines, i, board: Board, to_mm):
    t = BoardText()
    n = len(lines)
    i += 1
    while i < n:
        s = lines[i].strip()
        i += 1
        if s.startswith("$EndTEXTPCB"):
            break
        if s.startswith("Te "):
            parts = s.split(None, 1)
            if len(parts) > 1:
                t.text, _ = reader._parse_quoted(parts[1])
        elif s.startswith("Po "):
            p = s.split()
            if len(p) >= 7:
                t.x, t.y = to_mm(p[1]), to_mm(p[2])
                t.width, t.height = to_mm(p[3]), to_mm(p[4])
                t.rotation = parse_int(p[6]) / 10.0
        elif s.startswith("De "):
            p = s.split()
            if len(p) >= 2:
                t.layer = board_layer_name(parse_int(p[1]))
            if len(p) >= 3:
                t.mirror = p[2] == "0"
            if len(p) >= 5:
                t.style = p[4]
    board.texts.append(t)
    return i


def _read_czone(lines, i, board: Board, to_mm):
    net = ""
    layer = "F.Cu"
    corners: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    n = len(lines)
    i += 1
    while i < n:
        s = lines[i].strip()
        i += 1
        if s.lower().startswith("$endCZONE_OUTLINE"):
            break
        if s.startswith("ZInfo "):
            p = s.split(None, 2)
            if len(p) >= 3:
                net, _ = reader._parse_quoted(p[2])
        elif s.startswith("ZLayer "):
            p = s.split()
            if len(p) >= 2:
                layer = board_layer_name(parse_int(p[1]))
        elif s.startswith("ZCorner "):
            p = s.split()
            if len(p) >= 4:
                current.append((to_mm(p[1]), to_mm(p[2])))
                if parse_int(p[3]) == 1:
                    corners.append(current)
                    current = []
    if current:
        corners.append(current)
    board.zone_outlines.append(
        {"net": net, "layer": layer, "corners": corners})
    return i
