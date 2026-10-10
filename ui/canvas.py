"""Канва: сцена с сеткой, слоями, инструментами, групповым drag."""
from __future__ import annotations
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter,
                           QPainterPath, QPen, QTransform)
from PySide6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem,
                               QGraphicsLineItem, QGraphicsPathItem,
                               QGraphicsScene, QGraphicsSimpleTextItem,
                               QGraphicsView)

from core.arc_math import arc_points_from_center
from core.commands import (AddArcCommand, AddCircleCommand, AddLineCommand,
                           AddPadCommand, MoveManyCommand, RemoveArcCommand,
                           RemoveCircleCommand, RemoveLineCommand,
                           RemovePadCommand, SetOriginCommand)
from core.footprint import (Footprint, FpArc, FpCircle, FpLine, Pad,
                            next_pad_number)


LAYER_COLORS = {
    "Pad.Num":   "#cccccc",
    "F.Cu":      "#c83434",
    "B.Cu":      "#4d7fc4",
    "In1.Cu":    "#a86c3a",
    "F.Adhes":   "#845650",
    "B.Adhes":   "#4d4648",
    "F.Paste":   "#c591d1",
    "B.Paste":   "#7d6f9c",
    "F.SilkS":   "#f2eda1",
    "B.SilkS":   "#e8b2a7",
    "F.Mask":    "#c591d1",
    "B.Mask":    "#7d6f9c",
    "F.CrtYd":   "#d0d2cd",
    "B.CrtYd":   "#585d84",
    "F.Fab":     "#afafaf",
    "B.Fab":     "#585d84",
    "Edge.Cuts": "#d0d2cd",
    "Margin":    "#d357d3",
    "Dwgs.User": "#c2c2c2",
    "Cmts.User": "#c2c2c2",
    "Eco1.User": "#c2c2c2",
    "Eco2.User": "#c2c2c2",
}
DEFAULT_LAYER_COLOR = "#b8b8b8"

BG_COLOR     = QColor("#1f1f1f")
GRID_COLOR   = QColor("#4a4a4a")
GRID_PEN_WIDTH = 2.4
AXIS_COLOR   = QColor("#4d6bfe")
PREVIEW_COLOR = QColor("#ffffff")
PAD_NUM_COLOR = QColor("#111111")
PAD_COLOR    = QColor("#c8b464")


def _layer_qcolor(layer: str) -> QColor:
    return QColor(LAYER_COLORS.get(layer, DEFAULT_LAYER_COLOR))


def _layer_pen(layer: str, width_mm: float) -> QPen:
    pen = QPen(_layer_qcolor(layer))
    pen.setCosmetic(False)
    pen.setWidthF(max(width_mm, 0.02))
    pen.setCapStyle(Qt.FlatCap)
    return pen


# --------------------------------------------------------------------------
# Формы
# --------------------------------------------------------------------------

def _pad_path(shape: str, w: float, h: float) -> QPainterPath:
    path = QPainterPath()
    hw, hh = w / 2.0, h / 2.0
    if shape == "C":
        r = min(hw, hh)
        path.addEllipse(QPointF(0.0, 0.0), r, r)
    elif shape == "O":
        r = min(hw, hh)
        path.addRoundedRect(QRectF(-hw, -hh, w, h), r, r)
    else:   # "R" и "T" — прямоугольник (T трапецией позже)
        path.addRect(QRectF(-hw, -hh, w, h))
    return path


def _arc_path(a: FpArc) -> QPainterPath:
    pts = arc_points_from_center(a.cx, a.cy, a.sx, a.sy, a.angle)
    path = QPainterPath()
    if not pts:
        return path
    path.moveTo(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        path.lineTo(x, y)
    return path


# --------------------------------------------------------------------------
# Подстановка %R / %V не нужна — .mod хранит имена как есть
# --------------------------------------------------------------------------

def _fit_pad_number(text, w, h, target_mm=1.0):
    if not text: return None
    font = QFont("Sans"); font.setPixelSize(100)
    fm = QFontMetricsF(font)
    cap = fm.capHeight() or 70.0
    tw = fm.horizontalAdvance(text) or 1.0
    sc = min(target_mm / cap, w * 0.85 / tw, h * 0.85 / cap)
    if cap * sc < 0.15: return None
    return sc, cap * sc


# --------------------------------------------------------------------------
# Редактируемые элементы
# --------------------------------------------------------------------------

class PadItem(QGraphicsPathItem):
    def __init__(self, pad: Pad):
        super().__init__()
        self.pad = pad
        self._hole_item = None
        self._number_item = None
        self._number_visible = True
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._sync()

    def _sync(self):
        w = max(self.pad.width, 0.01)
        h = max(self.pad.height, 0.01)
        self.setPath(_pad_path(self.pad.shape, w, h))
        self.setRotation(self.pad.pad_angle)

        pen = QPen(PAD_COLOR.darker(140))
        pen.setCosmetic(False)
        pen.setWidthF(max(min(w, h) * 0.05, 0.02))
        self.setPen(pen)
        fill = QColor(PAD_COLOR); fill.setAlpha(220)
        self.setBrush(QBrush(fill))

        self._remove_hole()
        if self.pad.drill and self.pad.drill > 0:
            rx = self.pad.drill / 2.0
            ry = (self.pad.drill_y or self.pad.drill) / 2.0
            hole = QGraphicsEllipseItem(-rx, -ry, 2 * rx, 2 * ry, self)
            hole.setBrush(QBrush(BG_COLOR))
            hole.setPen(Qt.NoPen)
            hole.setAcceptedMouseButtons(Qt.NoButton)
            self._hole_item = hole

        self._remove_number()
        self._add_number(w, h)

    def _remove_hole(self):
        if self._hole_item is not None:
            self._hole_item.setParentItem(None); self._hole_item = None

    def _remove_number(self):
        if self._number_item is not None:
            self._number_item.setParentItem(None); self._number_item = None

    def _add_number(self, w, h):
        fit = _fit_pad_number(str(self.pad.number), w, h)
        if fit is None: return
        sc, _ = fit
        it = QGraphicsSimpleTextItem(str(self.pad.number), self)
        font = QFont("Sans"); font.setPixelSize(100)
        it.setFont(font)
        it.setBrush(QBrush(PAD_NUM_COLOR))
        it.setAcceptedMouseButtons(Qt.NoButton)
        it.setFlag(QGraphicsItem.ItemIsSelectable, False)
        it.setZValue(10)
        br = it.boundingRect(); c = br.center()
        tr = QTransform(); tr.scale(sc, sc); tr.translate(-c.x(), -c.y())
        it.setTransform(tr)
        it.setVisible(self._number_visible)
        self._number_item = it

    def set_number_visible(self, v: bool):
        self._number_visible = v
        if self._number_item is not None:
            self._number_item.setVisible(v)


class LineItem(QGraphicsLineItem):
    def __init__(self, line: FpLine):
        super().__init__()
        self.line = line
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._sync()

    def _sync(self):
        mx = (self.line.x1 + self.line.x2) / 2.0
        my = (self.line.y1 + self.line.y2) / 2.0
        self.setLine(self.line.x1 - mx, self.line.y1 - my,
                     self.line.x2 - mx, self.line.y2 - my)
        self.setPos(mx, my)
        self.setPen(_layer_pen(self.line.layer, self.line.width))


class CircleItem(QGraphicsEllipseItem):
    def __init__(self, c: FpCircle):
        super().__init__()
        self.circle = c
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._sync()

    def _sync(self):
        r = self.circle.radius
        self.setRect(self.circle.cx - r, self.circle.cy - r, 2 * r, 2 * r)
        self.setPen(_layer_pen(self.circle.layer, self.circle.width))


class ArcItem(QGraphicsPathItem):
    def __init__(self, a: FpArc):
        super().__init__()
        self.arc = a
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._sync()

    def _sync(self):
        self.setPath(_arc_path(self.arc))
        self.setPen(_layer_pen(self.arc.layer, self.arc.width))


class TextItem(QGraphicsSimpleTextItem):
    def __init__(self, t, fp: Footprint):
        super().__init__()
        self.text_obj = t
        self.footprint = fp
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._sync()

    def _sync(self):
        self.setText(self.text_obj.text)
        font = QFont("Sans"); font.setPixelSize(100)
        self.setFont(font)
        fm = QFontMetricsF(font)
        cap = fm.capHeight() or 70.0
        sc = (self.text_obj.height if self.text_obj.height > 0 else 1.0) / cap
        self.setBrush(QBrush(_layer_qcolor(self.text_obj.layer)))
        self.setPos(self.text_obj.x, self.text_obj.y)
        br = self.boundingRect(); c = br.center()
        tr = QTransform(); tr.rotate(-self.text_obj.rotation)
        tr.scale(sc, sc); tr.translate(-c.x(), -c.y())
        self.setTransform(tr)


# --------------------------------------------------------------------------
# Сцена
# --------------------------------------------------------------------------

class FootprintScene(QGraphicsScene):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid_step = 1.0
        self.grid_visible = True
        self.setBackgroundBrush(QBrush(BG_COLOR))
        self.setSceneRect(-100, -100, 200, 200)

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, BG_COLOR)
        if not self.grid_visible:
            return
        step = self.grid_step
        px = abs(painter.worldTransform().m11()) or 1.0
        while step * px < 7.0 and step < 1000.0:
            step *= 10.0
        left = int(rect.left() / step) * step
        top = int(rect.top() / step) * step
        pen = QPen(GRID_COLOR); pen.setCosmetic(True)
        pen.setWidthF(GRID_PEN_WIDTH); pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        pts = []
        y = top
        while y < rect.bottom() and len(pts) < 40000:
            x = left
            while x < rect.right() and len(pts) < 40000:
                pts.append(QPointF(x, y)); x += step
            y += step
        if pts: painter.drawPoints(pts)
        axis = QPen(AXIS_COLOR); axis.setCosmetic(True); axis.setWidthF(1.3)
        painter.setPen(axis)
        painter.drawLine(QPointF(rect.left(), 0.0), QPointF(rect.right(), 0.0))
        painter.drawLine(QPointF(0.0, rect.top()), QPointF(0.0, rect.bottom()))


DRAGGABLE = (PadItem, LineItem, CircleItem, ArcItem, TextItem)


class FootprintView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = FootprintScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setMouseTracking(True)
        self.scale(20, 20)
        self._zoom = 20.0

        self.cmd_stack = None
        self.footprint: Footprint | None = None
        self.selection_changed = None
        self.tool_changed = None
        self.visible_layers: set[str] = set()
        self.default_graphics_layer = "F.SilkS"
        self.current_tool = "select"
        self._preview_item = None
        self._pending_points: list[QPointF] = []
        self._group_drag: dict | None = None
        self._scene.selectionChanged.connect(self._on_selection_changed)

    # --- модель и слои ---
    def set_footprint(self, fp: Footprint):
        self.footprint = fp
        self.rebuild()
        self.fit_to_content()

    def fit_to_content(self, attempt=0):
        r = self._scene.itemsBoundingRect()
        if not r.isValid() or r.width() <= 0 or r.height() <= 0:
            return
        vp = self.viewport().size()
        if (vp.width() < 50 or vp.height() < 50) and attempt < 10:
            QTimer.singleShot(30, lambda: self.fit_to_content(attempt + 1))
            return
        self.fitInView(r.adjusted(-3, -3, 3, 3), Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()

    def set_visible_layers(self, layers):
        self.visible_layers = set(layers)
        self._apply_layer_visibility()

    def _apply_layer_visibility(self):
        vis = self.visible_layers
        show_numbers = "Pad.Num" in vis
        for it in self._scene.items():
            if isinstance(it, PadItem):
                it.setVisible(bool({"F.Cu", "B.Cu"} & vis))
                it.set_number_visible(show_numbers)
                continue
            layers = self._layers_of(it)
            if not layers: continue
            it.setVisible(bool(layers & vis))

    def _layers_of(self, item):
        if isinstance(item, LineItem):   return {item.line.layer}
        if isinstance(item, CircleItem): return {item.circle.layer}
        if isinstance(item, ArcItem):    return {item.arc.layer}
        if isinstance(item, TextItem):   return {item.text_obj.layer}
        return set()

    def set_tool(self, tool_id: str):
        self._cancel_pending()
        self.current_tool = tool_id
        if tool_id == "select":
            self.setDragMode(QGraphicsView.RubberBandDrag)
            self.setCursor(Qt.ArrowCursor)
        else:
            self.setDragMode(QGraphicsView.NoDrag)
            self.setCursor(Qt.CrossCursor)
        if self.tool_changed is not None:
            self.tool_changed(tool_id)

    # --- перестроение ---
    def rebuild(self):
        selected = set()
        for it in self._scene.selectedItems():
            if isinstance(it, PadItem):    selected.add(("pad", id(it.pad)))
            elif isinstance(it, LineItem): selected.add(("line", id(it.line)))
            elif isinstance(it, CircleItem): selected.add(("circle", id(it.circle)))
            elif isinstance(it, ArcItem):  selected.add(("arc", id(it.arc)))
            elif isinstance(it, TextItem): selected.add(("text", id(it.text_obj)))

        self._cancel_pending()
        self._scene.clear()
        if self.footprint is None:
            return

        for ln in self.footprint.lines:
            item = LineItem(ln); self._scene.addItem(item)
            if ("line", id(ln)) in selected: item.setSelected(True)
        for c in self.footprint.circles:
            item = CircleItem(c); self._scene.addItem(item)
            if ("circle", id(c)) in selected: item.setSelected(True)
        for a in self.footprint.arcs:
            item = ArcItem(a); self._scene.addItem(item)
            if ("arc", id(a)) in selected: item.setSelected(True)
        for p in self.footprint.pads:
            item = PadItem(p); self._scene.addItem(item)
            item.setPos(p.x, p.y)
            if ("pad", id(p)) in selected: item.setSelected(True)
        for t in self.footprint.texts:
            if not t.visible: continue
            item = TextItem(t, self.footprint); self._scene.addItem(item)
            if ("text", id(t)) in selected: item.setSelected(True)

        self._apply_layer_visibility()

    def delete_selected(self):
        if self.cmd_stack is None or self.footprint is None: return
        for it in list(self._scene.selectedItems()):
            if isinstance(it, PadItem):
                self.cmd_stack.push(RemovePadCommand(self.footprint, it.pad))
            elif isinstance(it, LineItem):
                self.cmd_stack.push(RemoveLineCommand(self.footprint, it.line))
            elif isinstance(it, CircleItem):
                self.cmd_stack.push(RemoveCircleCommand(self.footprint, it.circle))
            elif isinstance(it, ArcItem):
                self.cmd_stack.push(RemoveArcCommand(self.footprint, it.arc))

    # --- зум ---
    def wheelEvent(self, ev):
        f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
        self._zoom *= f; self.scale(f, f); self._scene.update()
    def zoom_in(self):
        self._zoom *= 1.2; self.scale(1.2, 1.2); self._scene.update()
    def zoom_out(self):
        self._zoom /= 1.2; self.scale(1/1.2, 1/1.2); self._scene.update()
    def reset_zoom(self):
        self.resetTransform(); self.scale(20, 20); self._zoom = 20.0
        self._scene.update()

    # --- мышь ---
    def mousePressEvent(self, ev):
        if self.footprint is None:
            super().mousePressEvent(ev); return
        if self.current_tool != "select":
            if ev.button() == Qt.LeftButton:
                pos = self._snap(self.mapToScene(ev.position().toPoint()))
                self._handle_tool_click(pos); ev.accept(); return
            if ev.button() == Qt.RightButton:
                self._cancel_pending(); ev.accept(); return
            super().mousePressEvent(ev); return
        if ev.button() == Qt.LeftButton:
            if not (ev.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
                sp = self.mapToScene(ev.position().toPoint())
                if self._start_group_drag(sp):
                    ev.accept(); return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._group_drag is not None:
            self._update_group_drag(self.mapToScene(ev.position().toPoint()))
            ev.accept(); return
        pos = self._snap(self.mapToScene(ev.position().toPoint()))
        pts = self._pending_points
        tool = self.current_tool
        if tool == "line" and len(pts) == 1:
            self._preview_line(pts[0], pos)
        elif tool == "circle" and len(pts) == 1:
            r = math.hypot(pos.x() - pts[0].x(), pos.y() - pts[0].y())
            self._preview_circle(pts[0], r)
        elif tool == "arc" and len(pts) == 1:
            self._preview_line(pts[0], pos)
        elif tool == "arc" and len(pts) == 2:
            self._preview_path([pts[0], pts[1], pos])
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._group_drag is not None:
            self._finish_group_drag(); ev.accept(); return
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self._cancel_pending(); return
        super().keyPressEvent(ev)

    # --- групповое перетаскивание ---
    def _hit_draggable(self, sp):
        for it in self._scene.items(sp):
            if isinstance(it, DRAGGABLE): return it
        return None

    def _start_group_drag(self, sp):
        hit = self._hit_draggable(sp)
        if hit is None: return False
        if not hit.isSelected():
            self._scene.clearSelection(); hit.setSelected(True)
        sel = [it for it in self._scene.selectedItems() if isinstance(it, DRAGGABLE)]
        if not sel: return False
        origins = {id(it): self._snapshot(it) for it in sel}
        self._group_drag = {"start": sp, "items": sel, "origins": origins,
                            "last_dx": 0.0, "last_dy": 0.0}
        return True

    def _snapshot(self, it):
        pos = (it.pos().x(), it.pos().y())
        if isinstance(it, PadItem):
            return {"pos": pos, "kind": "pad", "model": (it.pad.x, it.pad.y)}
        if isinstance(it, LineItem):
            return {"pos": pos, "kind": "line",
                    "model": (it.line.x1, it.line.y1, it.line.x2, it.line.y2)}
        if isinstance(it, CircleItem):
            return {"pos": pos, "kind": "circle",
                    "model": (it.circle.cx, it.circle.cy,
                              it.circle.ex, it.circle.ey)}
        if isinstance(it, ArcItem):
            return {"pos": pos, "kind": "arc",
                    "model": (it.arc.cx, it.arc.cy, it.arc.sx, it.arc.sy)}
        if isinstance(it, TextItem):
            return {"pos": pos, "kind": "text",
                    "model": (it.text_obj.x, it.text_obj.y)}
        return None

    def _update_group_drag(self, sp):
        start = self._group_drag["start"]
        step = self._scene.grid_step
        dx = round((sp.x() - start.x()) / step) * step
        dy = round((sp.y() - start.y()) / step) * step
        self._group_drag["last_dx"] = dx
        self._group_drag["last_dy"] = dy
        for it in self._group_drag["items"]:
            snap = self._group_drag["origins"][id(it)]
            ox, oy = snap["pos"]
            it.setPos(ox + dx, oy + dy)

    def _finish_group_drag(self):
        drag = self._group_drag
        self._group_drag = None
        if drag is None: return
        dx, dy = drag["last_dx"], drag["last_dy"]
        if abs(dx) < 1e-9 and abs(dy) < 1e-9: return
        moves = []
        for it in drag["items"]:
            snap = drag["origins"][id(it)]
            k, m = snap["kind"], snap["model"]
            if k == "pad":
                moves.append((it.pad, {"x": m[0] + dx, "y": m[1] + dy}))
            elif k == "line":
                moves.append((it.line, {"x1": m[0] + dx, "y1": m[1] + dy,
                                        "x2": m[2] + dx, "y2": m[3] + dy}))
            elif k == "circle":
                moves.append((it.circle, {"cx": m[0] + dx, "cy": m[1] + dy,
                                          "ex": m[2] + dx, "ey": m[3] + dy}))
            elif k == "arc":
                moves.append((it.arc, {"cx": m[0] + dx, "cy": m[1] + dy,
                                       "sx": m[2] + dx, "sy": m[3] + dy}))
            elif k == "text":
                moves.append((it.text_obj, {"x": m[0] + dx, "y": m[1] + dy}))
        if moves:
            self._push_command(MoveManyCommand(moves))

    # --- инструменты ---
    def _handle_tool_click(self, pos):
        t = self.current_tool
        if t == "pad":       self._tool_add_pad(pos)
        elif t == "line":    self._tool_two_point(pos, "line")
        elif t == "circle":  self._tool_two_point(pos, "circle")
        elif t == "arc":     self._tool_arc(pos)
        elif t == "anchor":  self._tool_set_anchor(pos)
        elif t == "delete":  self._tool_delete(pos)

    def _tool_add_pad(self, pos):
        pad = Pad(number=next_pad_number(self.footprint),
                  x=pos.x(), y=pos.y(),
                  width=1.524, height=1.524,
                  drill=0.8128, attachment="STD")
        self._push_command(AddPadCommand(self.footprint, pad))

    def _tool_two_point(self, pos, kind):
        if not self._pending_points:
            self._pending_points.append(pos); return
        p1 = self._pending_points[0]
        self._cancel_pending()
        if kind == "line":
            ln = FpLine(x1=p1.x(), y1=p1.y(), x2=pos.x(), y2=pos.y(),
                        layer=self.default_graphics_layer)
            self._push_command(AddLineCommand(self.footprint, ln))
        elif kind == "circle":
            c = FpCircle(cx=p1.x(), cy=p1.y(), ex=pos.x(), ey=pos.y(),
                         layer=self.default_graphics_layer)
            self._push_command(AddCircleCommand(self.footprint, c))

    def _tool_arc(self, pos):
        self._pending_points.append(pos)
        if len(self._pending_points) == 3:
            p0, p1, p2 = self._pending_points
            # p0 = центр, p1 = начало, p2 = точка для определения направления
            cx, cy = p0.x(), p0.y()
            a0 = math.atan2(p1.y() - cy, p1.x() - cx)
            a2 = math.atan2(p2.y() - cy, p2.x() - cx)
            # берём разницу в пределах [-180, 180] и округляем до целого
            ang = math.degrees((a2 - a0 + math.pi) % (2 * math.pi) - math.pi)
            arc = FpArc(cx=cx, cy=cy, sx=p1.x(), sy=p1.y(),
                        angle=ang, layer=self.default_graphics_layer)
            self._cancel_pending()
            self._push_command(AddArcCommand(self.footprint, arc))

    def _tool_set_anchor(self, pos):
        self._push_command(SetOriginCommand(self.footprint, pos.x(), pos.y()))

    def _tool_delete(self, pos):
        for it in self._scene.items(pos):
            if isinstance(it, PadItem):
                self._push_command(RemovePadCommand(self.footprint, it.pad)); return
            if isinstance(it, LineItem):
                self._push_command(RemoveLineCommand(self.footprint, it.line)); return
            if isinstance(it, CircleItem):
                self._push_command(RemoveCircleCommand(self.footprint, it.circle)); return
            if isinstance(it, ArcItem):
                self._push_command(RemoveArcCommand(self.footprint, it.arc)); return

    # --- предпросмотр ---
    def _style_preview(self, it):
        pen = QPen(PREVIEW_COLOR); pen.setCosmetic(True)
        pen.setWidthF(1.5); pen.setStyle(Qt.DashLine)
        it.setPen(pen); it.setZValue(1000)

    def _clear_preview_item(self):
        if self._preview_item is not None:
            self._scene.removeItem(self._preview_item)
            self._preview_item = None

    def _preview_line(self, p1, p2):
        if isinstance(self._preview_item, QGraphicsLineItem):
            self._preview_item.setLine(p1.x(), p1.y(), p2.x(), p2.y()); return
        self._clear_preview_item()
        it = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
        self._style_preview(it); self._scene.addItem(it)
        self._preview_item = it

    def _preview_circle(self, c, r):
        if isinstance(self._preview_item, QGraphicsEllipseItem):
            self._preview_item.setRect(c.x() - r, c.y() - r, 2 * r, 2 * r); return
        self._clear_preview_item()
        it = QGraphicsEllipseItem(c.x() - r, c.y() - r, 2 * r, 2 * r)
        self._style_preview(it); self._scene.addItem(it)
        self._preview_item = it

    def _preview_path(self, points):
        path = QPainterPath()
        if not points: return
        path.moveTo(points[0])
        for p in points[1:]: path.lineTo(p)
        if isinstance(self._preview_item, QGraphicsPathItem):
            self._preview_item.setPath(path); return
        self._clear_preview_item()
        it = QGraphicsPathItem(path); self._style_preview(it)
        self._scene.addItem(it); self._preview_item = it

    def _cancel_pending(self):
        self._clear_preview_item(); self._pending_points = []

    # --- утилиты ---
    def _snap(self, pos):
        s = self._scene.grid_step
        return QPointF(round(pos.x() / s) * s, round(pos.y() / s) * s)

    def _push_command(self, cmd):
        if self.cmd_stack is not None:
            self.cmd_stack.push(cmd)

    def _on_selection_changed(self):
        if self.selection_changed is not None:
            self.selection_changed(self._scene.selectedItems())