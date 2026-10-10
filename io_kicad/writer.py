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
    out.append(f"Sc {fp.sc}")
    out.append(f"AR {fp.ar}".rstrip() if fp.ar else "AR")
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
    tag = "T0" if t.kind == "reference" else "T1"
    mirror = "M" if t.mirror else "N"
    visible = "V" if t.visible else "I"
    italic = "I" if t.italic else "N"
    layer = name_to_num(t.layer)
    txt = t.text.replace("\\", "\\\\").replace('"', '\\"')
    return (f"{tag} {_i(t.x)} {_i(t.y)} {_i(t.width)} {_i(t.height)} "
            f"{int(round(t.rotation))} {_i(t.thickness)} "
            f"{mirror} {visible} {layer} {italic} \"{txt}\"")


def _ds_line(ln: FpLine) -> str:
    return (f"DS {_i(ln.x1)} {_i(ln.y1)} {_i(ln.x2)} {_i(ln.y2)} "
            f"{_i(ln.width)} {name_to_num(ln.layer)}")


def _dc_line(c: FpCircle) -> str:
    return (f"DC {_i(c.cx)} {_i(c.cy)} {_i(c.ex)} {_i(c.ey)} "
            f"{_i(c.width)} {name_to_num(c.layer)}")


def _da_line(a: FpArc) -> str:
    return (f"DA {_i(a.cx)} {_i(a.cy)} {_i(a.sx)} {_i(a.sy)} "
            f"{int(round(a.angle))} {_i(a.width)} {name_to_num(a.layer)}")


def _pad_lines(pad: Pad) -> list[str]:
    out = ["$PAD"]
    sh = f"Sh \"{pad.number}\" {pad.shape} {_i(pad.width)} {_i(pad.height)}"
    if pad.pad_dx or pad.pad_dy or pad.pad_angle:
        sh += f" {pad.pad_dx} {pad.pad_dy} {pad.pad_angle}"
    out.append(sh)

    if pad.drill is not None and pad.drill > 0:
        dx = _i(pad.drill)
        dy = _i(pad.drill_y) if pad.drill_y else dx
        off = _i(pad.drill_offset) if pad.drill_offset else 0
        out.append(f"Dr {dx} {dy} {off}")
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