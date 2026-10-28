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

    # --- фон, сетка и рамка чертежа -----------------------------------
    def drawBackground(self, painter, rect):
        painter.fillRect(rect, BG_COLOR)
        
        # Отключаем сглаживание для фона, чтобы линии сетки и рамки были 
        # идеально четкими (в 1 пиксель) и не выглядели "размазанными"
        painter.setRenderHint(QPainter.Antialiasing, False)
        
        # 1. Сначала рисуем сетку, чтобы она была ПОД рамкой
        if self.grid_visible:
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

        # 2. Оси координат
        axis = QPen(AXIS_COLOR)
        axis.setCosmetic(True)
        axis.setWidthF(1.3)
        painter.setPen(axis)
        painter.drawLine(QPointF(rect.left(), 0.0), QPointF(rect.right(), 0.0))
        painter.drawLine(QPointF(0.0, rect.top()), QPointF(0.0, rect.bottom()))

        # 3. Рисуем рамку и штамп ПОВЕРХ сетки
        self._draw_sheet(painter)

    def _draw_sheet(self, painter):
        """Отрисовка стандартной рамки чертежа KiCad (A4) и штампа."""
        if not self.board:
            return

        # 1. Извлекаем размеры страницы из $SHEETDESCR (по умолчанию A4)
        w_mil, h_mil = 11700.0, 8267.0
        sheet_str = self.board.sheet.get("Sheet", "")
        parts = sheet_str.split()
        if len(parts) >= 3:
            try:
                w_mil = float(parts[-2])
                h_mil = float(parts[-1])
            except ValueError:
                pass
                
        w_mm = w_mil * 0.0254
        h_mm = h_mil * 0.0254

        # 2. Настройка пера (красный цвет, толщина 1.5 для четкости)
        red = QColor("#ff0000")
        pen = QPen(red)
        pen.setCosmetic(True)
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        # 3. Внешняя и внутренняя рамки (поля по 10 мм)
        m = 10.0
        rect = QRectF(m, m, w_mm - 2 * m, h_mm - 2 * m)
        painter.drawRect(rect)
        
        m_inner = 2.0
        rect_inner = QRectF(m + m_inner, m + m_inner,
                            rect.width() - 2 * m_inner,
                            rect.height() - 2 * m_inner)
        painter.drawRect(rect_inner)

        # 4. Шрифт
        font = QFont("Sans")
        font.setPixelSize(100)
        fm = QFontMetricsF(font)
        cap = fm.capHeight() or 70.0

        def draw_text(txt, x, y, w, h, size_mm, align=Qt.AlignLeft, bold=False):
            if not txt:
                return
            painter.save()
            sc = size_mm / cap
            painter.translate(x, y)
            painter.scale(sc, sc)
            f = QFont(font)
            f.setBold(bold)
            painter.setFont(f)
            painter.setPen(QPen(red))
            painter.drawText(QRectF(0, 0, w / sc, h / sc), align | Qt.AlignVCenter, str(txt))
            painter.restore()

        # 5. Деления по горизонтали (1, 2, 3, 4)
        n_x = 4
        dx = rect.width() / n_x
        for i in range(1, n_x):
            x = rect.left() + i * dx
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.top() + m_inner))
            painter.drawLine(QPointF(x, rect.bottom() - m_inner), QPointF(x, rect.bottom()))
            draw_text(str(i), x - dx, rect.top(), dx, m_inner, 1.5, Qt.AlignHCenter)
            draw_text(str(i), x - dx, rect.bottom() - m_inner, dx, m_inner, 1.5, Qt.AlignHCenter)
            
        draw_text(str(n_x), rect.right() - dx, rect.top(), dx, m_inner, 1.5, Qt.AlignHCenter)
        draw_text(str(n_x), rect.right() - dx, rect.bottom() - m_inner, dx, m_inner, 1.5, Qt.AlignHCenter)

        # 6. Деления по вертикали (A, B, C, D)
        n_y = 4
        dy = rect.height() / n_y
        letters = "ABCD"
        for i in range(1, n_y):
            y = rect.top() + i * dy
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.left() + m_inner, y))
            painter.drawLine(QPointF(rect.right() - m_inner, y), QPointF(rect.right(), y))
            draw_text(letters[i - 1], rect.left(), y - dy, m_inner, dy, 1.5, Qt.AlignHCenter)
            draw_text(letters[i - 1], rect.right() - m_inner, y - dy, m_inner, dy, 1.5, Qt.AlignHCenter)
            
        draw_text(letters[n_y - 1], rect.left(), rect.bottom() - dy, m_inner, dy, 1.5, Qt.AlignHCenter)
        draw_text(letters[n_y - 1], rect.right() - m_inner, rect.bottom() - dy, m_inner, dy, 1.5, Qt.AlignHCenter)

        # 7. Штамп (Title Block) в правом нижнем углу (уменьшенный до 90x24 мм)
        tb_w = 90.0
        tb_h = 24.0
        tb_right = w_mm - m - m_inner
        tb_bottom = h_mm - m - m_inner
        tb_left = tb_right - tb_w
        tb_top = tb_bottom - tb_h

        painter.drawRect(QRectF(tb_left, tb_top, tb_w, tb_h))

        # Горизонтальные линии штампа
        y1 = tb_top + 6.0
        y2 = tb_top + 14.0
        y3 = tb_top + 19.0
        y4 = tb_bottom
        
        painter.drawLine(QPointF(tb_left, y1), QPointF(tb_left + tb_w, y1))
        painter.drawLine(QPointF(tb_left, y2), QPointF(tb_left + tb_w, y2))
        painter.drawLine(QPointF(tb_left, y3), QPointF(tb_left + tb_w, y3))

        # Вертикальные разделители (Size/Date/Rev и KiCad/Id)
        x1 = tb_left + 30.0
        x2 = tb_left + 60.0
        
        painter.drawLine(QPointF(x1, y2), QPointF(x1, y3))
        painter.drawLine(QPointF(x2, y2), QPointF(x2, y4))

        # 8. Текст в штампе
        def get_val(key):
            v = self.board.sheet.get(key, "")
            return v.strip('"')

        # Имя файла (извлекаем из заголовка окна, если доступно)
        file_name = "lab2_gr346_br2.brd"
        if self.views():
            title = self.views()[0].window().windowTitle()
            if " — плата: " in title:
                file_name = title.split(" — плата: ")[1]

        file_text = "File: " + file_name
        sheet_text = "Sheet: 1/1"
        title_text = "Title: " + get_val("Title")
        size_text = "Size: A4"
        date_text = "Date: " + get_val("Date")
        rev_text = "Rev: " + get_val("Rev")
        kicad_text = "KiCad E.D.A. pcbnew (2011-04-29 BZR 2986)-stable"
        id_text = "Id: 1/1"

        draw_text(file_text, tb_left + 2, tb_top + 0.5, tb_w - 4, 3.0, 1.5)
        draw_text(sheet_text, tb_left + 2, tb_top + 2.5, tb_w - 4, 3.0, 1.5)
        
        draw_text(title_text, tb_left + 2, y1 + 1, tb_w - 4, y2 - y1 - 2, 2.0, Qt.AlignLeft, True)
        
        draw_text(size_text, tb_left + 2, y2 + 1, x1 - tb_left - 4, y3 - y2 - 2, 1.5)
        draw_text(date_text, x1 + 2, y2 + 1, x2 - x1 - 4, y3 - y2 - 2, 1.5)
        draw_text(rev_text, x2 + 2, y2 + 1, tb_w - (x2 - tb_left) - 4, y3 - y2 - 2, 1.5)
        
        draw_text(kicad_text, tb_left + 2, y3 + 1, x2 - tb_left - 4, y4 - y3 - 2, 1.5)
        draw_text(id_text, x2 + 2, y3 + 1, tb_w - (x2 - tb_left) - 4, y4 - y3 - 2, 1.5)

    # --- содержимое (плата) -------------------------------------------
    def drawForeground(self, painter, rect):
        if self.board is None:
            return
        # Включаем сглаживание обратно для красивой отрисовки платы (кругов и т.д.)
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
            
        # Вычисляем габариты листа чертежа
        w_mil, h_mil = 11700.0, 8267.0
        sheet_str = board.sheet.get("Sheet", "")
        parts = sheet_str.split()
        if len(parts) >= 3:
            try:
                w_mil = float(parts[-2])
                h_mil = float(parts[-1])
            except ValueError:
                pass
                
        w_mm = w_mil * 0.0254
        h_mm = h_mil * 0.0254
        
        # Устанавливаем границы сцены равными всему листу, 
        # чтобы масштабирование и скролл не ограничивались только платой
        rect = QRectF(0, 0, w_mm, h_mm)
        self._scene.content_rect = rect
        self._scene.setSceneRect(rect)

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