"""Чтение .mod (PCBNEW-LibModule-V1) в модель Library."""
from __future__ import annotations

from core.footprint import Footprint, Pad, FpText, FpLine, FpCircle, FpArc
from core.library import Library
from .layers import parse_layer
from .units import parse_int, units_to_mm


def read(text: str) -> Library:
    lines = text.splitlines()
    lib = Library()
    i, n = 0, len(lines)

    # --- Заголовок ---
    if i < n and lines[i].lstrip().startswith("PCBNEW-LibModule-V1"):
        lib.header_date = lines[i].split(None, 1)[1].strip() if " " in lines[i] else ""
        i += 1

    # --- Пролог до $MODULE / $INDEX ---
    while i < n:
        s = lines[i].strip()
        if s.startswith("$INDEX"):
            i += 1
            while i < n and not lines[i].strip().startswith("$EndINDEX"):
                i += 1
            if i < n: i += 1
            continue
        if s.startswith("$MODULE") or s.startswith("$EndLIBRARY"):
            break
        if s and not s.startswith("#"):
            lib.extra_header.append(lines[i])
        i += 1

    # --- Модули ---
    while i < n:
        s = lines[i].strip()
        if s.startswith("$EndLIBRARY"):
            break
        if s.startswith("$MODULE"):
            fp, i = _read_module(lines, i)
            lib.modules.append(fp)
            continue
        if s:
            lib.extra_before_end.append(lines[i])
        i += 1

    return lib


# --------------------------------------------------------------------------
# Модуль
# --------------------------------------------------------------------------

def _read_module(lines, i, to_mm=units_to_mm):
    parts = lines[i].strip().split(None, 1)
    name = parts[1].strip() if len(parts) > 1 else "Module"
    fp = Footprint(name=name)
    i += 1
    n = len(lines)

    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1; continue
        if s.startswith("$EndMODULE"):
            return fp, i + 1
        if s.startswith("$PAD"):
            pad, i = _read_pad(lines, i + 1, to_mm)
            fp.pads.append(pad)
            continue
        _read_module_line(fp, s, to_mm)
        i += 1
    return fp, i


def _read_module_line(fp: Footprint, s: str, to_mm=units_to_mm) -> None:
    parts = s.split()
    tag = parts[0]
    rest = parts[1:]

    if tag == "Po":
        if len(rest) >= 1: fp.po_x = parse_int(rest[0])
        if len(rest) >= 2: fp.po_y = parse_int(rest[1])
        if len(rest) >= 3: fp.po_rotation = parse_int(rest[2])
        if len(rest) >= 4: fp.po_layer = parse_int(rest[3], 15)
        if len(rest) >= 5: fp.po_tstamp = rest[4]
        if len(rest) >= 6: fp.po_flags = rest[5]
        if len(rest) >= 7: fp.po_misc = " ".join(rest[6:])
    elif tag == "Li":
        if rest: fp.name = " ".join(rest)
    elif tag == "Cd":
        # Описание модуля. Строка идёт сразу после Li, часто без кавычек.
        fp.description = " ".join(rest)
    elif tag == "Kw":
        # Ключевые слова. Тоже сразу после Li (после Cd).
        fp.keywords = " ".join(rest)
    elif tag == "Sc":
        fp.sc = " ".join(rest)
    elif tag == "AR":
        fp.ar = " ".join(rest)
    elif tag == "Op":
        fp.op = " ".join(rest)
    elif tag in ("T0", "T1", "T2"):
        # T0 = reference, T1 = value, T2 = user-текст
        _read_text(fp, tag, rest, to_mm)
    elif tag == "DS":
        _read_ds(fp, rest, to_mm)
    elif tag == "DC":
        _read_dc(fp, rest, to_mm)
    elif tag == "DA":
        _read_da(fp, rest, to_mm)
    else:
        fp.extra.append(s)


def _parse_quoted(s: str):
    """Вернуть (текст, остаток). Поддерживает \"...\" и голые слова."""
    s = s.lstrip()
    if s.startswith('"'):
        buf, j = [], 1
        while j < len(s):
            c = s[j]
            if c == "\\" and j + 1 < len(s):
                buf.append(s[j + 1]); j += 2; continue
            if c == '"':
                return "".join(buf), s[j + 1:]
            buf.append(c); j += 1
        return "".join(buf), ""
    parts = s.split(None, 1)
    if not parts: return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def _read_text(fp, tag, rest, to_mm=units_to_mm):
    """Разобрать строку T0/T1/T2.

    Формат (legacy KiCad):
        T<kind> X Y H W A Pen Mirror Visible Layer Italic "Text"
    H — высота символа, W — ширина. ВАЖНО: H идёт ПЕРВЫМ.
    Угол A — в 0.1°, поэтому делим на 10.
    """
    if len(rest) < 9:
        fp.extra.append(f"{tag} " + " ".join(rest)); return
    t = FpText()
    t.kind = {"T0": "reference", "T1": "value", "T2": "user"}.get(tag, "value")
    t.x = to_mm(parse_int(rest[0]))
    t.y = to_mm(parse_int(rest[1]))
    t.height = to_mm(parse_int(rest[2]))   # H — первым
    t.width = to_mm(parse_int(rest[3]))    # W — вторым
    t.rotation = parse_int(rest[4]) / 10.0       # 0.1° → градусы
    t.thickness = to_mm(parse_int(rest[5]))
    t.mirror = rest[6].upper() == "M"
    t.visible = rest[7].upper() != "I"
    t.layer = parse_layer(rest[8])
    if len(rest) >= 10: t.italic = rest[9].upper() == "I"
    if len(rest) >= 11:
        txt, _ = _parse_quoted(" ".join(rest[10:]))
        t.text = txt
    fp.texts.append(t)


def _read_ds(fp, rest, to_mm=units_to_mm):
    if len(rest) < 6:
        fp.extra.append("DS " + " ".join(rest)); return
    ln = FpLine()
    ln.x1 = to_mm(parse_int(rest[0]))
    ln.y1 = to_mm(parse_int(rest[1]))
    ln.x2 = to_mm(parse_int(rest[2]))
    ln.y2 = to_mm(parse_int(rest[3]))
    ln.width = to_mm(parse_int(rest[4]))
    ln.layer = parse_layer(rest[5])
    fp.lines.append(ln)


def _read_dc(fp, rest, to_mm=units_to_mm):
    if len(rest) < 6:
        fp.extra.append("DC " + " ".join(rest)); return
    c = FpCircle()
    c.cx = to_mm(parse_int(rest[0]))
    c.cy = to_mm(parse_int(rest[1]))
    c.ex = to_mm(parse_int(rest[2]))
    c.ey = to_mm(parse_int(rest[3]))
    c.width = to_mm(parse_int(rest[4]))
    c.layer = parse_layer(rest[5])
    fp.circles.append(c)


def _read_da(fp, rest, to_mm=units_to_mm):
    """Дуга. Формат: DA X Y Xp Yp Angle Pen Layer.

    X,Y — центр базовой окружности; Xp,Yp — начальная точка дуги
    (задаёт радиус и стартовый угол); Angle — угловой размах в 0.1°.
    Храним угол в градусах.
    """
    if len(rest) < 7:
        fp.extra.append("DA " + " ".join(rest)); return
    a = FpArc()
    a.cx = to_mm(parse_int(rest[0]))
    a.cy = to_mm(parse_int(rest[1]))
    a.sx = to_mm(parse_int(rest[2]))
    a.sy = to_mm(parse_int(rest[3]))
    a.angle = parse_int(rest[4]) / 10.0          # 0.1° → градусы
    a.width = to_mm(parse_int(rest[5]))
    a.layer = parse_layer(rest[6])
    fp.arcs.append(a)


# --------------------------------------------------------------------------
# Пад
# --------------------------------------------------------------------------

def _read_pad(lines, i, to_mm=units_to_mm):
    pad = Pad()
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1; continue
        if s.startswith("$EndPAD"):
            return pad, i + 1
        parts = s.split(None, 1)
        tag = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if tag == "Sh":   _read_pad_sh(pad, rest, to_mm)
        elif tag == "Dr": _read_pad_dr(pad, rest, to_mm)
        elif tag == "At": _read_pad_at(pad, rest)
        elif tag == "Ne": _read_pad_ne(pad, rest)
        elif tag == "Po":
            r = rest.split()
            if len(r) >= 1: pad.x = to_mm(parse_int(r[0]))
            if len(r) >= 2: pad.y = to_mm(parse_int(r[1]))
        else:
            pad.extra.append(s)
        i += 1
    return pad, i


def _read_pad_sh(pad, rest, to_mm=units_to_mm):
    name, r = _parse_quoted(rest)
    pad.number = name
    parts = r.split()
    if len(parts) >= 1: pad.shape = parts[0].upper()
    if len(parts) >= 2: pad.width = to_mm(parse_int(parts[1]))
    if len(parts) >= 3: pad.height = to_mm(parse_int(parts[2]))
    # В файле порядок Ydelta Xdelta (см. PDF), не Xdelta Ydelta.
    if len(parts) >= 4: pad.pad_dx = parse_int(parts[3])   # Ydelta
    if len(parts) >= 5: pad.pad_dy = parse_int(parts[4])   # Xdelta
    if len(parts) >= 6: pad.pad_angle = parse_int(parts[5])


def _read_pad_dr(pad, rest, to_mm=units_to_mm):
    """Разобрать строку 'Dr' пада.

    Формат legacy KiCad (единицы 0.1 мил):
        Dr <size> <x_off> <y_off>              # круглое
        Dr <size> <x_off> <y_off> O <w> <h>    # овальное (slot)

    <size> — диаметр отверстия; <x_off>/<y_off> — смещения центра
    отверстия относительно центра пада.

    ВАЖНО: в legacy-формате оба смещения хранятся с ОБРАТНЫМИ знаками
    относительно «естественного» направления, которое видит пользователь
    на канве (X+ = вправо, Y+ = вниз). В файле положительный X означает
    влево, положительный Y — вверх. KiCad сам компенсирует это при
    чтении/записи; делаем то же самое, чтобы направление смещения,
    нарисованное в редакторе, совпадало с тем, что покажет KiCad.
    """
    parts = rest.split()
    if not parts:
        pad.drill = None
        pad.drill_offset_x = 0.0
        pad.drill_offset_y = 0.0
        return

    size = parse_int(parts[0], 0)
    off_x = parse_int(parts[1], 0) if len(parts) >= 2 else 0
    off_y = parse_int(parts[2], 0) if len(parts) >= 3 else 0

    if len(parts) >= 4 and parts[3].upper() == "O":
        w = parse_int(parts[4], 0) if len(parts) >= 5 else 0
        h = parse_int(parts[5], 0) if len(parts) >= 6 else 0
        # Для редактора достаточно «эффективного диаметра» — меньшей
        # стороны слота. Смещение остаётся тем же.
        cands = [v for v in (w, h) if v > 0]
        size = min(cands) if cands else 0

    pad.drill = to_mm(size) if size > 0 else None
    pad.drill_offset_x = to_mm(-off_x)
    pad.drill_offset_y = to_mm(-off_y)


def _read_pad_at(pad, rest):
    parts = rest.split()
    if len(parts) >= 1: pad.attachment = parts[0]
    if len(parts) >= 2: pad.attach_flag = parts[1]
    if len(parts) >= 3: pad.flags = parts[2]


def _read_pad_ne(pad, rest):
    parts = rest.split(None, 1)
    if parts: pad.net = parse_int(parts[0])
    if len(parts) > 1:
        pad.net_name, _ = _parse_quoted(parts[1])