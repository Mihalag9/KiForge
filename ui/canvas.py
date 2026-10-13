"""Канва: сцена с сеткой, слоями, инструментами, групповым drag."""
from __future__ import annotations
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter,
                           QPainterPath, QPen, QTransform)
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGraphicsEllipseItem,
                               QGraphicsItem, QGraphicsLineItem,
                               QGraphicsPathItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView, QLabel,
                               QLineEdit, QMenu, QStyle,
                               QStyleOptionGraphicsItem, QVBoxLayout)

from core.arc_math import arc_points_from_center
from core.commands import (AddArcCommand, AddCircleCommand, AddLineCommand,
                           AddPadCommand, AddTextCommand, MoveManyCommand,
                           RemoveArcCommand, RemoveCircleCommand,
                           RemoveLineCommand, RemovePadCommand,
                           RemoveTextCommand, SetOriginCommand)
from core.footprint import (Footprint, FpArc, FpCircle, FpLine, FpText, Pad,
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

BG_COLOR       = QColor("#0B1220")
GRID_COLOR     = QColor("#26324A")
GRID_PEN_WIDTH = 2.4
AXIS_COLOR     = QColor("#4d6bfe")
PREVIEW_COLOR  = QColor("#ffffff")
PAD_NUM_COLOR  = QColor("#ffffff")
PAD_COLOR      = QColor("#c14a4a")

GHOST_TEXT_COLOR = QColor("#808a99")

FREE_ROUND_DIGITS = 4

# --- Индикатор выделения ---------------------------------------------------
SELECTION_COLOR  = QColor("#4d6bfe")
SELECTION_PAD_MM = 1.0
SELECTION_GAP_PX = 3.0
HANDLE_PX        = 4.0

DEFAULT_TEXT = "Текст"

# Типы текстов, которые нельзя удалять: они критичны для KiCad (T0/T1).
# Пользовательские надписи (kind="user") удаляются как обычно.
PROTECTED_TEXT_KINDS = frozenset({"reference", "value"})


def _layer_qcolor(layer: str) -> QColor:
    return QColor(LAYER_COLORS.get(layer, DEFAULT_LAYER_COLOR))


def _layer_pen(layer: str, width_mm: float) -> QPen:
    pen = QPen(_layer_qcolor(layer))
    pen.setCosmetic(False)
    pen.setWidthF(max(width_mm, 0.02))
    # KiCad рисует концы линий закруглёнными, а не обрезанными «по
    # линейке». FlatCap даёт острые срезы; RoundCap — полукруг на
    # каждом конце, как видит пользователь в самом KiCad.
    pen.setCapStyle(Qt.RoundCap)
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
    else:
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


def _fit_pad_number(text, w, h, target_mm=1.0):
    if not text: return None
    font = QFont("Sans"); font.setPixelSize(100)
    fm = QFontMetricsF(font)
    cap = fm.capHeight() or 70.0
    tw = fm.horizontalAdvance(text) or 1.0
    sc = min(target_mm / cap, w * 0.85 / tw, h * 0.85 / cap)
    if cap * sc < 0.15: return None
    return sc, cap * sc


def _is_protected_text(text_obj) -> bool:
    """Текст относится к reference/value — его нельзя удалять из модуля."""
    return getattr(text_obj, "kind", "") in PROTECTED_TEXT_KINDS


# --------------------------------------------------------------------------
# Индикатор выделения
# --------------------------------------------------------------------------

def _draw_selection(item, painter, option):
    """Кастомный индикатор выделения: пунктир по контуру + маркеры углов."""
    if not (option.state & QStyle.StateFlag.State_Selected):
        return

    lod = option.levelOfDetailFromTransform(painter.worldTransform())
    if lod <= 0.0:
        return

    gap = SELECTION_GAP_PX / lod
    rect = item._content_rect().adjusted(-gap, -gap, gap, gap)

    painter.save()

    pen = QPen(SELECTION_COLOR)
    pen.setCosmetic(True)
    pen.setWidthF(1.0)
    pen.setStyle(Qt.DashLine)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(rect)

    s = HANDLE_PX / lod
    painter.setPen(Qt.NoPen)
    painter.setBrush(SELECTION_COLOR)
    for x in (rect.left(), rect.right()):
        for y in (rect.top(), rect.bottom()):
            painter.drawRect(QRectF(x - s / 2.0, y - s / 2.0, s, s))

    painter.restore()


class _SelectableMixin:
    """Общая логика выделения для всех графических элементов модуля."""

    def _content_rect(self) -> QRectF:
        return super().boundingRect()

    def boundingRect(self) -> QRectF:
        m = SELECTION_PAD_MM
        return self._content_rect().adjusted(-m, -m, m, m)

    def paint(self, painter, option, widget=None):
        opt = QStyleOptionGraphicsItem(option)
        opt.state = opt.state & ~QStyle.StateFlag.State_Selected
        super().paint(painter, opt, widget)
        _draw_selection(self, painter, option)


# --------------------------------------------------------------------------
# Диалог ввода текста
# --------------------------------------------------------------------------

class _NewTextDialog(QDialog):
    """Модальное окно для инструмента «Текст»."""

    def __init__(self, parent=None, default: str = DEFAULT_TEXT):
        super().__init__(parent)
        self.setWindowTitle("Новый текст")
        self.setModal(True)
        self.setMinimumWidth(340)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        root.addWidget(QLabel("Текст:"))

        self.edit = QLineEdit(default)
        self.edit.selectAll()
        root.addWidget(self.edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Создать")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.edit.textChanged.connect(self._refresh_ok_state)
        self._refresh_ok_state(default)

    def _refresh_ok_state(self, *_):
        self._ok.setEnabled(bool(self.edit.text().strip()))

    def text_value(self) -> str:
        return self.edit.text()


# --------------------------------------------------------------------------
# Редактируемые элементы
# --------------------------------------------------------------------------

class PadItem(_SelectableMixin, QGraphicsPathItem):
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
        if self.pad.has_drill:
            r = self.pad.drill / 2.0
            ox = self.pad.drill_offset_x
            oy = self.pad.drill_offset_y
            hole = QGraphicsEllipseItem(ox - r, oy - r, 2 * r, 2 * r, self)
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


class LineItem(_SelectableMixin, QGraphicsLineItem):
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


class CircleItem(_SelectableMixin, QGraphicsEllipseItem):
    def __init__(self, c: FpCircle):
        super().__init__()
        self.circle = c
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._sync()

    def _sync(self):
        r = self.circle.radius
        self.setRect(self.circle.cx - r, self.circle.cy - r, 2 * r, 2 * r)
        self.setPen(_layer_pen(self.circle.layer, self.circle.width))


class ArcItem(_SelectableMixin, QGraphicsPathItem):
    def __init__(self, a: FpArc):
        super().__init__()
        self.arc = a
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._sync()

    def _sync(self):
        self.setPath(_arc_path(self.arc))
        self.setPen(_layer_pen(self.arc.layer, self.arc.width))


class TextItem(_SelectableMixin, QGraphicsSimpleTextItem):
    """Текстовый элемент модуля.

    Если ``text_obj.visible == False`` (в .mod хранится как флаг I), в
    редакторе элемент остаётся видимым, но рисуется приглушённым серым —
    «ghost»-стиль. Так пользователь видит, что объект есть, может его
    выделить и вернуть к обычному виду через чекбокс «Видим» в панели
    свойств. В файл при этом по-прежнему уходит флаг I.
    """

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

        h = self.text_obj.height if self.text_obj.height > 0 else 1.0
        w = self.text_obj.width  if self.text_obj.width  > 0 else 1.0
        # W и H в .mod — независимые габариты символа: KiCad растягивает
        # глиф по обеим осям. Поэтому и мы масштабируем X и Y раздельно,
        # а не одним общим коэффициентом из высоты.
        sc_y = h / cap
        sc_x = w / cap

        if self.text_obj.visible:
            self.setBrush(QBrush(_layer_qcolor(self.text_obj.layer)))
        else:
            self.setBrush(QBrush(GHOST_TEXT_COLOR))

        self.setPos(self.text_obj.x, self.text_obj.y)
        br = self.boundingRect(); c = br.center()
        tr = QTransform()
        tr.rotate(-self.text_obj.rotation)
        tr.scale(sc_x, sc_y)
        tr.translate(-c.x(), -c.y())
        self.setTransform(tr)


# --------------------------------------------------------------------------
# Сцена
# --------------------------------------------------------------------------

class FootprintScene(QGraphicsScene):
    """Сцена модуля. Хранит состояние сетки и привязки к ней."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid_step = 1.0
        self.grid_visible = True
        self.snap_to_grid = True
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
            item = TextItem(t, self.footprint); self._scene.addItem(item)
            if ("text", id(t)) in selected: item.setSelected(True)

        self._apply_layer_visibility()

    # --- удаление ---
    def delete_selected(self):
        """Удалить выделенные элементы.

        Reference и value текста пропускаются: без них модуль в KiCad
        перестанет нормально открываться (не подставится обозначение и
        номинал). Пользовательские тексты удаляются как раньше.
        """
        if self.cmd_stack is None or self.footprint is None:
            return
        skipped_protected = 0
        for it in list(self._scene.selectedItems()):
            if isinstance(it, PadItem):
                self.cmd_stack.push(RemovePadCommand(self.footprint, it.pad))
            elif isinstance(it, LineItem):
                self.cmd_stack.push(RemoveLineCommand(self.footprint, it.line))
            elif isinstance(it, CircleItem):
                self.cmd_stack.push(RemoveCircleCommand(self.footprint, it.circle))
            elif isinstance(it, ArcItem):
                self.cmd_stack.push(RemoveArcCommand(self.footprint, it.arc))
            elif isinstance(it, TextItem):
                if _is_protected_text(it.text_obj):
                    skipped_protected += 1
                    continue
                self.cmd_stack.push(RemoveTextCommand(self.footprint, it.text_obj))

        if skipped_protected:
            self._notify_protected(skipped_protected)

    def _notify_protected(self, count: int):
        """Сообщить пользователю, что reference/value удалять нельзя."""
        msg = (f"Текст «обозначение»/«значение» удалить нельзя "
               f"({count} пропущено) — это обязательные поля KiCad.")
        scene = self.scene()
        if scene is not None and scene.views():
            view = scene.views()[0]
            win = view.window()
            status = getattr(win, "statusBar", None)
            if callable(status):
                status().showMessage(msg, 3500)

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
                sp = self.mapToScene(ev.position().toPoint())
                if self.current_tool == "delete":
                    self._tool_delete(sp)
                else:
                    self._handle_tool_click(self._snap(sp))
                ev.accept(); return
            if ev.button() == Qt.RightButton:
                self._cancel_pending(); ev.accept(); return
            super().mousePressEvent(ev); return

        # --- режим «Выбор» ---
        # Alt+клик — циклический перебор объектов под курсором. Удобно,
        # когда нужный элемент скрыт под другими и его не «достать»
        # обычным кликом.
        if ev.button() == Qt.LeftButton and (ev.modifiers() & Qt.AltModifier):
            sp = self.mapToScene(ev.position().toPoint())
            if self._cycle_overlapping(sp):
                ev.accept(); return

        # Правый клик — выпадающее меню со списком всех объектов под
        # курсором. Это альтернатива Alt+клику: удобно, когда объектов
        # много и хочется выбрать сразу.
        if ev.button() == Qt.RightButton:
            sp = self.mapToScene(ev.position().toPoint())
            if self._show_overlap_menu(sp, ev.globalPosition().toPoint(),
                                       ev.modifiers()):
                ev.accept(); return

        if ev.button() == Qt.LeftButton:
            if not (ev.modifiers() &
                    (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier)):
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
        if ev.key() == Qt.Key_Delete:
            self.delete_selected(); return
        super().keyPressEvent(ev)

    # --- выбор из наложенных объектов ---

    def _items_at(self, sp) -> list:
        """Все перетаскиваемые объекты под точкой, сверху вниз.

        `QGraphicsScene.items(pos)` возвращает элементы в порядке
        «верхний → нижний» с учётом Z-порядка, что и нужно для меню
        и для циклического перебора: первый элемент — тот, который
        пользователь видит поверх остальных.
        """
        return [it for it in self._scene.items(sp)
                if isinstance(it, DRAGGABLE)]

    def _describe(self, it) -> str:
        """Короткое человекочитаемое описание объекта для меню."""
        if isinstance(it, PadItem):
            return f"Пад {it.pad.number} ({it.pad.shape})"
        if isinstance(it, LineItem):
            return f"Линия на {it.line.layer}"
        if isinstance(it, CircleItem):
            return f"Окружность на {it.circle.layer}"
        if isinstance(it, ArcItem):
            return f"Дуга на {it.arc.layer}"
        if isinstance(it, TextItem):
            t = (it.text_obj.text or "").strip()
            if len(t) > 40:
                t = t[:37] + "…"
            label = t if t else "(пустой текст)"
            return f"Текст «{label}»"
        return "Объект"

    def _cycle_overlapping(self, sp) -> bool:
        """Alt+клик: переключиться на следующий объект под курсором.

        Логика:
          * если под курсором ничего нет — ничего не делаем, даём
            обычному клику шанс начать rubber band;
          * если ни один объект не выделен — выделяем верхний;
          * если что-то уже выделено — переходим к следующему
            в списке `_items_at` (сверху вниз), зацикливаясь в конце.
        """
        items = self._items_at(sp)
        if not items:
            return False

        selected = [it for it in items if it.isSelected()]
        if not selected:
            target = items[0]
        else:
            current = selected[0]
            try:
                idx = items.index(current)
            except ValueError:
                idx = -1
            target = items[(idx + 1) % len(items)]

        self._scene.clearSelection()
        target.setSelected(True)
        return True

    def _show_overlap_menu(self, sp, global_pos, modifiers) -> bool:
        """Правый клик: показать меню со списком объектов под курсором.

        Возвращает True, если меню действительно было показано (то есть
        под курсором есть хотя бы один объект) — тогда вызывающая
        сторона поглотит событие. Иначе возвращает False, чтобы
        правый клик никак не мешал.
        """
        items = self._items_at(sp)
        if not items:
            return False

        menu = QMenu(self)
        header = menu.addAction("Объекты под курсором:")
        header.setEnabled(False)
        menu.addSeparator()

        actions = []
        for idx, it in enumerate(items):
            act = menu.addAction(self._describe(it))
            act.setData(idx)
            if it.isSelected():
                act.setCheckable(True)
                act.setChecked(True)
            actions.append(act)

        chosen = menu.exec(global_pos)
        if chosen is None:
            return True  # меню закрыто без выбора — событие поглощено
        idx = chosen.data()
        if idx is None:
            return True
        it = items[idx]

        # Ctrl/Shift при правом клике = добавить к выделению, иначе —
        # заменить его выбранным объектом.
        if not (modifiers & (Qt.ControlModifier | Qt.ShiftModifier)):
            self._scene.clearSelection()
        it.setSelected(True)
        return True

    # --- групповое перетаскивание ---
    def _selection_rect_scene(self, item) -> QRectF:
        """Рамка выделения элемента в координатах сцены.

        Это тот же зазор, что рисует _draw_selection: SELECTION_GAP_PX
        экранных пикселей вокруг видимого контура. Переводим их в
        сценовые единицы через текущий масштаб вида, чтобы попадание
        мышью совпадало с тем, что видит пользователь.
        """
        lod = self.transform().m11() or 1.0
        gap = SELECTION_GAP_PX / lod
        rect = item.mapRectToScene(item._content_rect())
        return rect.adjusted(-gap, -gap, gap, gap)

    def _hit_draggable(self, sp):
        # 1) Точное попадание в контур (тело) элемента — приоритет выше,
        #    чтобы кликом по видимой части соседнего объекта можно было
        #    переключить выделение.
        for it in self._scene.items(sp):
            if isinstance(it, DRAGGABLE):
                return it
        # 2) Клик в пустой зоне рамки выделения уже выделенного элемента —
        #    тоже начинает drag. Если выделено несколько, вернём первый
        #    подходящий; _start_group_drag подхватит всю группу.
        for it in self._scene.selectedItems():
            if not isinstance(it, DRAGGABLE):
                continue
            if self._selection_rect_scene(it).contains(sp):
                return it
        return None

    def _start_group_drag(self, sp):
        hit = self._hit_draggable(sp)
        if hit is None: return False
        if not hit.isSelected():
            self._scene.clearSelection(); hit.setSelected(True)
        sel = [it for it in self._scene.selectedItems() if isinstance(it, DRAGGABLE)]
        if not sel: return False
        origins = {id(it): self._snapshot(it) for it in sel}
        # Якорь — элемент, за который схватили мышью. Его позицию будем
        # снапить к сетке, а остальных сдвигать на получившуюся разницу,
        # чтобы группа двигалась жёстко и не рассыпалась.
        ax, ay = hit.pos().x(), hit.pos().y()
        self._group_drag = {
            "start": sp, "items": sel, "origins": origins,
            "anchor_orig": (ax, ay),
            "last_dx": 0.0, "last_dy": 0.0,
        }
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
        raw_dx = sp.x() - start.x()
        raw_dy = sp.y() - start.y()

        if self._scene.snap_to_grid:
            step = self._scene.grid_step
            ax, ay = self._group_drag["anchor_orig"]
            # Куда бы встал якорь без снапа — и потом округляем эту
            # АБСОЛЮТНУЮ позицию к сетке. Отсюда получаем снапнутое
            # смещение группы. Так объект всегда встаёт на узел, а не
            # «прыгает относительно старого места».
            target_x = ax + raw_dx
            target_y = ay + raw_dy
            snapped_x = round(target_x / step) * step
            snapped_y = round(target_y / step) * step
            dx = snapped_x - ax
            dy = snapped_y - ay
        else:
            dx = round(raw_dx, FREE_ROUND_DIGITS)
            dy = round(raw_dy, FREE_ROUND_DIGITS)

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
        elif t == "text":    self._tool_add_text(pos)
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
            cx, cy = p0.x(), p0.y()
            a0 = math.atan2(p1.y() - cy, p1.x() - cx)
            a2 = math.atan2(p2.y() - cy, p2.x() - cx)
            ang = math.degrees((a2 - a0 + math.pi) % (2 * math.pi) - math.pi)
            arc = FpArc(cx=cx, cy=cy, sx=p1.x(), sy=p1.y(),
                        angle=ang, layer=self.default_graphics_layer)
            self._cancel_pending()
            self._push_command(AddArcCommand(self.footprint, arc))

    def _tool_add_text(self, pos):
        dlg = _NewTextDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        text = dlg.text_value()
        if not text.strip():
            return
        item = FpText(
            kind="user",
            text=text,
            x=pos.x(),
            y=pos.y(),
            layer=self.default_graphics_layer,
        )
        self._push_command(AddTextCommand(self.footprint, item))

    def _tool_set_anchor(self, pos):
        self._push_command(SetOriginCommand(self.footprint, pos.x(), pos.y()))

    def _tool_delete(self, pos):
        """Удалить верхний элемент под курсором.

        Reference/value пропускаются: это обязательные поля KiCad.
        """
        for it in self._scene.items(pos):
            if isinstance(it, PadItem):
                self._push_command(RemovePadCommand(self.footprint, it.pad)); return
            if isinstance(it, LineItem):
                self._push_command(RemoveLineCommand(self.footprint, it.line)); return
            if isinstance(it, CircleItem):
                self._push_command(RemoveCircleCommand(self.footprint, it.circle)); return
            if isinstance(it, ArcItem):
                self._push_command(RemoveArcCommand(self.footprint, it.arc)); return
            if isinstance(it, TextItem):
                if _is_protected_text(it.text_obj):
                    self._notify_protected(1)
                    return
                self._push_command(RemoveTextCommand(self.footprint, it.text_obj)); return

    # --- предпросмотр ---
    def _style_preview(self, it):
        pen = QPen(PREVIEW_COLOR); pen.setCosmetic(True)
        pen.setWidthF(1.5); pen.setStyle(Qt.DashLine)
        # Тот же RoundCap, что и у настоящих линий, — превью не должно
        # выглядеть резче, чем результат клика.
        pen.setCapStyle(Qt.RoundCap)
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
        if not self._scene.snap_to_grid:
            return QPointF(round(pos.x(), FREE_ROUND_DIGITS),
                           round(pos.y(), FREE_ROUND_DIGITS))
        s = self._scene.grid_step
        return QPointF(round(pos.x() / s) * s, round(pos.y() / s) * s)

    def _push_command(self, cmd):
        if self.cmd_stack is not None:
            self.cmd_stack.push(cmd)

    def _on_selection_changed(self):
        if self.selection_changed is not None:
            self.selection_changed(self._scene.selectedItems())