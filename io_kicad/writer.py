"""Запись Library в текст .mod (PCBNEW-LibModule-V1)."""
from __future__ import annotations
from datetime import datetime

from core.footprint import Footprint, Pad, FpText, FpLine, FpCircle, FpArc
from core.library import Library
from .layers import name_to_num
from .units import mm_to_units


def write(lib: Library) -> str:
    date = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    out: list[str] = [f"PCBNEW-LibModule-V1  {date}", "# encoding utf-8"]

    for line in lib.extra_header:
        out.append(line)

    out.append("$INDEX")
    for fp in lib.modules:
        out.append(fp.name)
    out.append("$EndINDEX")

    for fp in lib.modules:
        out.extend(_module_lines(fp))

    for line in lib.extra_before_end:
        out.append(line)
    out.append("$EndLIBRARY")
    return "\n".join(out) + "\n"


def _i(x) -> int:
    return mm_to_units(x)


def _module_lines(fp: Footprint) -> list[str]:
    out = [f"$MODULE {fp.name}"]
    po = f"Po {fp.po_x} {fp.po_y} {fp.po_rotation} {fp.po_layer} {fp.po_tstamp} {fp.po_flags}"
    if fp.po_misc:
        po += f" {fp.po_misc}"
    out.append(po)
    out.append(f"Li {fp.name}")
    # Cd/Kw идут сразу после Li. Пишем только непустые значения,
    # чтобы не менять файлы, где этих строк не было.
    if fp.description:
        out.append(f"Cd {fp.description}")
    if fp.keywords:
        out.append(f"Kw {fp.keywords}")
    out.append(f"Sc {fp.sc}")
    if fp.ar:
        out.append(f"AR {fp.ar}")
    out.append(f"Op {fp.op}")

    for t in fp.texts:
        out.append(_text_line(t))
    for ln in fp.lines:
        out.append(_ds_line(ln))
    for c in fp.circles:
        out.append(_dc_line(c))
    for a in fp.arcs:
        out.append(_da_line(a))
    for pad in fp.pads:
        out.extend(_pad_lines(pad))

    for extra in fp.extra:
        out.append(extra)

    out.append(f"$EndMODULE  {fp.name}")
    return out


def _text_line(t: FpText) -> str:
    """Строка T0/T1/T2.

    Формат: T<kind> X Y H W A Pen Mirror Visible Layer Italic "Text".
    ВАЖНО: H идёт первым, W — вторым. Угол — в 0.1°.
    """
    if t.kind == "reference":
        tag = "T0"
    elif t.kind == "value":
        tag = "T1"
    else:
        tag = "T2"
    mirror = "M" if t.mirror else "N"
    visible = "V" if t.visible else "I"
    italic = "I" if t.italic else "N"
    layer = name_to_num(t.layer)
    txt = t.text.replace("\\", "\\\\").replace('"', '\\"')
    angle10 = int(round(t.rotation * 10))
    return (f"{tag} {_i(t.x)} {_i(t.y)} {_i(t.height)} {_i(t.width)} "
            f"{angle10} {_i(t.thickness)} "
            f"{mirror} {visible} {layer} {italic} \"{txt}\"")


def _ds_line(ln: FpLine) -> str:
    return (f"DS {_i(ln.x1)} {_i(ln.y1)} {_i(ln.x2)} {_i(ln.y2)} "
            f"{_i(ln.width)} {name_to_num(ln.layer)}")


def _dc_line(c: FpCircle) -> str:
    return (f"DC {_i(c.cx)} {_i(c.cy)} {_i(c.ex)} {_i(c.ey)} "
            f"{_i(c.width)} {name_to_num(c.layer)}")


def _da_line(a: FpArc) -> str:
    """Строка DA. Угол в файле — в 0.1°."""
    angle10 = int(round(a.angle * 10))
    return (f"DA {_i(a.cx)} {_i(a.cy)} {_i(a.sx)} {_i(a.sy)} "
            f"{angle10} {_i(a.width)} {name_to_num(a.layer)}")


def _pad_lines(pad: Pad) -> list[str]:
    out = ["$PAD"]
    sh = f"Sh \"{pad.number}\" {pad.shape} {_i(pad.width)} {_i(pad.height)}"
    if pad.pad_dx or pad.pad_dy or pad.pad_angle:
        sh += f" {pad.pad_dx} {pad.pad_dy} {pad.pad_angle}"
    out.append(sh)

    # Dr <size> <x_off> <y_off>. При отсутствии отверстия — «Dr 0 0 0».
    # ВАЖНО: оба смещения в файле хранятся с обратными знаками — см.
    # комментарий в reader._read_pad_dr. Инвертируем, чтобы направление,
    # заданное пользователем на канве, совпадало с тем, что покажет KiCad.
    if pad.has_drill:
        size = _i(pad.drill)
        off_x = _i(-pad.drill_offset_x)
        off_y = _i(-pad.drill_offset_y)
        out.append(f"Dr {size} {off_x} {off_y}")
    else:
        out.append("Dr 0 0 0")

    out.append(f"At {pad.attachment} {pad.attach_flag} {pad.flags}")
    name = pad.net_name.replace("\\", "\\\\").replace('"', '\\"')
    out.append(f"Ne {pad.net} \"{name}\"")
    out.append(f"Po {_i(pad.x)} {_i(pad.y)}")

    for extra in pad.extra:
        out.append(extra)
    out.append("$EndPAD")
    return out