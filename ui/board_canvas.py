"""Просмотр legacy-платы: сцена с сеткой, слоями и ratsnest."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter,
                           QPen, QTransform)
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from core.arc_math import arc_points_from_center
from core.board import (Board, BoardDrawing, BoardText, BoardTrack,
                        module_pad_position, module_transform, ratsnest_segments)
from core.footprint import Footprint, Pad
from ui.canvas import (AXIS_COLOR, BG_COLOR, GRID_COLOR, GRID_PEN_WIDTH,
                       PAD_COLOR, _pad_path, _layer_pen)


RATSNEST_COLOR = QColor(120, 150, 255, 160)


class BoardScene(QGraphicsScene):
    """Сцена платы. Все объекты рисуются на лету по видимым слоям."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.board: Board | None = None
        self.grid_step = 1.0
        self.grid_visible = True
        self.show_ratsnest = True
        self.visible_layers: set[str] = set()
        self.ratsnest: list = []
        self.content_rect = QRectF()
        self.setBackgroundBrush(QBrush(BG_COLOR))

    # --- фон и сетка --------------------------------------------------
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
        pen = QPen(GRID_COLOR)
        pen.setCosmetic(True)
        pen.setWidthF(GRID_PEN_WIDTH)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        pts = []
        y = top
        while y < rect.bottom() and len(pts) < 40000:
            x = left
            while x < rect.right() and len(pts) < 40000:
                pts.append(QPointF(x, y))
                x += step
            y += step
        if pts:
            painter.drawPoints(pts)
        axis = QPen(AXIS_COLOR)
        axis.setCosmetic(True)
        axis.setWidthF(1.3)
        painter.setPen(axis)
        painter.drawLine(QPointF(rect.left(), 0.0), QPointF(rect.right(), 0.0))
        painter.drawLine(QPointF(0.0, rect.top()), QPointF(0.0, rect.bottom()))

    # --- содержимое ---------------------------------------------------
    def drawForeground(self, painter, rect):
        if self.board is None:
            return
        painter.setRenderHint(QPainter.Antialiasing, True)

        if self.show_ratsnest and self.ratsnest:
            self._draw_ratsnest(painter)
        for fp in self.board.modules:
            self._draw_module(painter, fp)
        for t in self.board.tracks:
            self._draw_track(painter, t)
        for t in self.board.zone_segments:
            self._draw_track(painter, t)
        for d in self.board.drawings:
            if d.layer != "Edge.Cuts":
                self._draw_drawing(painter, d)
        for d in self.board.drawings:
            if d.layer == "Edge.Cuts":
                self._draw_drawing(painter, d)
        for t in self.board.texts:
            if t.layer in self.visible_layers:
                self._draw_text(painter, t.text, t.x, t.y, t.width, t.height,
                                t.rotation, t.layer, t.mirror)

    def _draw_ratsnest(self, painter):
        pen = QPen(RATSNEST_COLOR)
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        for (x1, y1), (x2, y2) in self.ratsnest:
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def _draw_module(self, painter, fp: Footprint):
        px, py, angle, mirrored = module_transform(fp)
        front = fp.po_layer != 0
        painter.save()
        painter.translate(px, py)
        if mirrored:
            painter.scale(1.0, -1.0)
        if angle:
            painter.rotate(-angle)

        for ln in fp.lines:
            if ln.layer in self.visible_layers:
                painter.setPen(_layer_pen(ln.layer, ln.width))
                painter.drawLine(QPointF(ln.x1, ln.y1), QPointF(ln.x2, ln.y2))
        for c in fp.circles:
            if c.layer in self.visible_layers:
                painter.setPen(_layer_pen(c.layer, c.width))
                r = c.radius
                painter.drawEllipse(QPointF(c.cx, c.cy), r, r)
        for a in fp.arcs:
            if a.layer in self.visible_layers:
                painter.setPen(_layer_pen(a.layer, a.width))
                pts = arc_points_from_center(a.cx, a.cy, a.sx, a.sy, a.angle)
                for p1, p2 in zip(pts, pts[1:]):
                    painter.drawLine(QPointF(*p1), QPointF(*p2))
        for t in fp.texts:
            if t.visible and t.layer in self.visible_layers:
                self._draw_text(painter, t.text, t.x, t.y, t.width, t.height,
                                t.rotation, t.layer, t.mirror)
        if ("F.Cu" if front else "B.Cu") in self.visible_layers:
            for pad in fp.pads:
                self._draw_pad(painter, pad)
        painter.restore()

    def _draw_pad(self, painter, pad: Pad):
        painter.save()
        painter.translate(pad.x, pad.y)
        if pad.pad_angle:
            painter.rotate(-pad.pad_angle / 10.0)
        path = _pad_path(pad.shape, max(pad.width, 0.01),
                         max(pad.height, 0.01))
        fill = QColor(PAD_COLOR)
        fill.setAlpha(220)
        pen = QPen(PAD_COLOR.darker(140))
        pen.setWidthF(max(min(pad.width, pad.height) * 0.05, 0.02))
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawPath(path)
        if pad.has_drill:
            r = pad.drill / 2.0
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(BG_COLOR))
            painter.drawEllipse(QPointF(pad.drill_offset_x, pad.drill_offset_y),
                                r, r)
        painter.restore()

    def _draw_track(self, painter, t: BoardTrack):
        if t.layer not in self.visible_layers:
            return
        color = _layer_pen(t.layer, t.width).color()
        if t.is_via:
            painter.setPen(Qt.NoPen)
            fill = QColor(color)
            fill.setAlpha(210)
            painter.setBrush(QBrush(fill))
            r = max(t.width / 2.0, 0.01)
            painter.drawEllipse(QPointF(t.x1, t.y1), r, r)
        else:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(_layer_pen(t.layer, t.width))
            painter.drawLine(QPointF(t.x1, t.y1), QPointF(t.x2, t.y2))

    def _draw_drawing(self, painter, d: BoardDrawing):
        if d.layer not in self.visible_layers:
            return
        painter.setBrush(Qt.NoBrush)
        painter.setPen(_layer_pen(d.layer, d.width))
        if d.shape == 1:
            r = math.hypot(d.x2 - d.x1, d.y2 - d.y1)
            painter.drawEllipse(QPointF(d.x1, d.y1), r, r)
        else:
            painter.drawLine(QPointF(d.x1, d.y1), QPointF(d.x2, d.y2))

    def _draw_text(self, painter, text, x, y, width, height, rotation,
                   layer, mirror):
        if not text:
            return
        painter.save()
        painter.translate(x, y)
        if mirror:
            painter.scale(-1.0, 1.0)
        if rotation:
            painter.rotate(-rotation)
        font = QFont("Sans")
        font.setPixelSize(100)
        fm = QFontMetricsF(font)
        cap = fm.capHeight() or 70.0
        painter.scale((width or 1.0) / cap, (height or 1.0) / cap)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_layer_pen(layer, 0.0).color())))
        
        # Центрируем текст, чтобы он совпадал с KiCad
        tw = fm.horizontalAdvance(str(text))
        painter.drawText(QPointF(-tw / 2.0, cap / 2.0), str(text))
        
        painter.restore()


class BoardView(QGraphicsView):
    """Вид платы: зум колесом, панорама средней кнопкой, Fit к содержимому."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = BoardScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setMouseTracking(True)
        self.setDragMode(QGraphicsView.NoDrag)
        self._zoom = 1.0
        self._panning = False
        self._pan_last = None
        self.scale(4, 4)
        self._zoom = 4.0

    def set_board(self, board: Board):
        self._scene.board = board
        self._scene.ratsnest = ratsnest_segments(board)
        self._compute_content_rect()
        self.fit_to_content()

    def set_visible_layers(self, layers):
        self._scene.visible_layers = set(layers)
        self._scene.update()

    def set_grid_visible(self, visible: bool):
        self._scene.grid_visible = bool(visible)
        self._scene.update()

    def set_ratsnest_visible(self, visible: bool):
        self._scene.show_ratsnest = bool(visible)
        self._scene.update()

    def _compute_content_rect(self):
        board = self._scene.board
        if board is None:
            return
        xs: list[float] = []
        ys: list[float] = []
        for fp in board.modules:
            px, py, _angle, _mirror = module_transform(fp)
            xs.append(px)
            ys.append(py)
            for pad in fp.pads:
                ax, ay = module_pad_position(fp, pad)
                xs.extend((ax - pad.width / 2.0, ax + pad.width / 2.0))
                ys.extend((ay - pad.height / 2.0, ay + pad.height / 2.0))
            for ln in fp.lines:
                xs.extend((px + ln.x1, px + ln.x2))
                ys.extend((py + ln.y1, py + ln.y2))
        for t in board.tracks:
            xs.extend((t.x1, t.x2))
            ys.extend((t.y1, t.y2))
        for d in board.drawings:
            xs.extend((d.x1, d.x2))
            ys.extend((d.y1, d.y2))
        if not xs or not ys:
            self._scene.content_rect = QRectF(-10, -10, 20, 20)
            self._scene.setSceneRect(self._scene.content_rect)
            return
        rect = QRectF(min(xs), min(ys),
                      max(max(xs) - min(xs), 1.0),
                      max(max(ys) - min(ys), 1.0))
        self._scene.content_rect = rect
        self._scene.setSceneRect(rect.adjusted(-5, -5, 5, 5))

    def fit_to_content(self, attempt=0):
        r = self._scene.content_rect
        if not r.isValid() or r.width() <= 0 or r.height() <= 0:
            return
        vp = self.viewport().size()
        if (vp.width() < 50 or vp.height() < 50) and attempt < 10:
            QTimer.singleShot(30, lambda: self.fit_to_content(attempt + 1))
            return
        self.fitInView(r.adjusted(-2, -2, 2, 2), Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()

    # --- зум ----------------------------------------------------------
    def wheelEvent(self, ev):
        f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
        self._zoom *= f
        self.scale(f, f)
        self._scene.update()

    def zoom_in(self):
        self._zoom *= 1.2
        self.scale(1.2, 1.2)
        self._scene.update()

    def zoom_out(self):
        self._zoom /= 1.2
        self.scale(1 / 1.2, 1 / 1.2)
        self._scene.update()

    def reset_zoom(self):
        self.fit_to_content()

    # --- панорама средней кнопкой -----------------------------------
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_last = ev.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._panning and self._pan_last is not None:
            pos = ev.position().toPoint()
            delta = pos - self._pan_last
            self._pan_last = pos
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self._pan_last = None
            self.setCursor(Qt.ArrowCursor)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)