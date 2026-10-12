"""Панель свойств выбранного объекта."""
from __future__ import annotations
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QLabel, QLineEdit, QVBoxLayout,
                               QWidget)

from core.commands import (EditArcCommand, EditCircleCommand,
                           EditLineCommand, EditPadCommand, EditTextCommand,
                           RemoveTextCommand)
from core.footprint import Footprint
from ui.layers_panel import KICAD_LAYERS


# Слои, доступные для графических элементов (без виртуального Pad.Num).
_ELEMENT_LAYERS: list[str] = [name for name, _color in KICAD_LAYERS
                              if name != "Pad.Num"]

# Отображаемые имена типов текстовых элементов.
_TEXT_KIND_LABELS = {
    "reference": "обозначение",
    "value": "значение",
    "user": "текст",
}

# Типы контактной площадки — те же три, что в UI KiCad. В самом .mod
# есть ещё `HOLE` (неметаллизированное отверстие), но полноценно
# редактировать его нельзя без отдельного поля под маску слоёв, поэтому
# в списке его не показываем. Если такой пад встретится в файле, reader
# его прочитает, а writer сохранит без искажений — просто в комбобоксе
# отобразится не он.
_PAD_ATTACHMENTS = ["STD", "SMD", "CONN"]


class PropertiesPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.cmd_stack = None
        self.footprint: Footprint | None = None
        self.item = None
        self._loading = False

        root = QVBoxLayout(self)
        self.title = QLabel("Ничего не выбрано")
        self.title.setStyleSheet("font-weight: bold; padding: 4px;")
        root.addWidget(self.title)
        self.form = QFormLayout(); root.addLayout(self.form)

        self._build_pad()
        self._build_line()
        self._build_circle()
        self._build_arc()
        self._build_text()
        root.addStretch(1)

        for g in (self._pad_w, self._line_w, self._circle_w,
                  self._arc_w, self._text_w):
            self._show(g, False)

    def _spin(self, lo, hi):
        s = QDoubleSpinBox()
        s.setRange(lo, hi); s.setDecimals(4); s.setSingleStep(0.05)
        return s

    def _layer_combo(self) -> QComboBox:
        """Выпадающий список слоёв — не редактируется, только выбор."""
        cb = QComboBox()
        cb.setEditable(False)
        cb.addItems(_ELEMENT_LAYERS)
        return cb

    def _show(self, group, visible):
        for w in group:
            w.setVisible(visible)
            lb = self.form.labelForField(w)
            if lb is not None: lb.setVisible(visible)

    # --- построение ---
    def _build_pad(self):
        self.p_number = QLineEdit(); self.p_number.editingFinished.connect(self._apply_pad)
        self.p_shape = QComboBox(); self.p_shape.addItems(["C", "R", "O", "T"])
        self.p_shape.currentTextChanged.connect(self._apply_pad)
        self.p_x = self._spin(-1000, 1000); self.p_x.valueChanged.connect(self._apply_pad)
        self.p_y = self._spin(-1000, 1000); self.p_y.valueChanged.connect(self._apply_pad)
        self.p_w = self._spin(0.01, 100); self.p_w.valueChanged.connect(self._apply_pad)
        self.p_h = self._spin(0.01, 100); self.p_h.valueChanged.connect(self._apply_pad)
        self.p_drill = self._spin(0.0, 50); self.p_drill.valueChanged.connect(self._apply_pad)
        self.p_drill.setSpecialValueText("нет")
        # Смещение центра отверстия относительно центра пада. Значения
        # могут быть отрицательными и положительными.
        self.p_offx = self._spin(-50, 50); self.p_offx.valueChanged.connect(self._apply_pad)
        self.p_offy = self._spin(-50, 50); self.p_offy.valueChanged.connect(self._apply_pad)
        self.p_attach = QComboBox(); self.p_attach.addItems(_PAD_ATTACHMENTS)
        self.p_attach.currentTextChanged.connect(self._apply_pad)

        self.form.addRow("Номер", self.p_number)
        self.form.addRow("Форма", self.p_shape)
        self.form.addRow("X (мм)", self.p_x)
        self.form.addRow("Y (мм)", self.p_y)
        self.form.addRow("Ширина", self.p_w)
        self.form.addRow("Высота", self.p_h)
        self.form.addRow("Drill (0=нет)", self.p_drill)
        self.form.addRow("Смещ. отв. X", self.p_offx)
        self.form.addRow("Смещ. отв. Y", self.p_offy)
        self.form.addRow("Attachment", self.p_attach)
        self._pad_w = [self.p_number, self.p_shape, self.p_x, self.p_y,
                       self.p_w, self.p_h, self.p_drill,
                       self.p_offx, self.p_offy, self.p_attach]

    def _build_line(self):
        self.l_x1 = self._spin(-1000, 1000); self.l_x1.valueChanged.connect(self._apply_line)
        self.l_y1 = self._spin(-1000, 1000); self.l_y1.valueChanged.connect(self._apply_line)
        self.l_x2 = self._spin(-1000, 1000); self.l_x2.valueChanged.connect(self._apply_line)
        self.l_y2 = self._spin(-1000, 1000); self.l_y2.valueChanged.connect(self._apply_line)
        self.l_layer = self._layer_combo()
        self.l_layer.currentTextChanged.connect(self._apply_line)
        self.l_width = self._spin(0.01, 5); self.l_width.valueChanged.connect(self._apply_line)
        self.form.addRow("X1", self.l_x1); self.form.addRow("Y1", self.l_y1)
        self.form.addRow("X2", self.l_x2); self.form.addRow("Y2", self.l_y2)
        self.form.addRow("Слой", self.l_layer); self.form.addRow("Ширина", self.l_width)
        self._line_w = [self.l_x1, self.l_y1, self.l_x2, self.l_y2,
                        self.l_layer, self.l_width]

    def _build_circle(self):
        self.c_cx = self._spin(-1000, 1000); self.c_cx.valueChanged.connect(self._apply_circle)
        self.c_cy = self._spin(-1000, 1000); self.c_cy.valueChanged.connect(self._apply_circle)
        self.c_r  = self._spin(0.01, 500);   self.c_r.valueChanged.connect(self._apply_circle)
        self.c_layer = self._layer_combo()
        self.c_layer.currentTextChanged.connect(self._apply_circle)
        self.c_width = self._spin(0.01, 5); self.c_width.valueChanged.connect(self._apply_circle)
        self.form.addRow("Cx", self.c_cx); self.form.addRow("Cy", self.c_cy)
        self.form.addRow("Радиус", self.c_r)
        self.form.addRow("Слой", self.c_layer); self.form.addRow("Ширина", self.c_width)
        self._circle_w = [self.c_cx, self.c_cy, self.c_r, self.c_layer, self.c_width]

    def _build_arc(self):
        self.a_cx = self._spin(-1000, 1000); self.a_cx.valueChanged.connect(self._apply_arc)
        self.a_cy = self._spin(-1000, 1000); self.a_cy.valueChanged.connect(self._apply_arc)
        self.a_sx = self._spin(-1000, 1000); self.a_sx.valueChanged.connect(self._apply_arc)
        self.a_sy = self._spin(-1000, 1000); self.a_sy.valueChanged.connect(self._apply_arc)
        self.a_ang = self._spin(-360, 360); self.a_ang.valueChanged.connect(self._apply_arc)
        self.a_layer = self._layer_combo()
        self.a_layer.currentTextChanged.connect(self._apply_arc)
        self.a_width = self._spin(0.01, 5); self.a_width.valueChanged.connect(self._apply_arc)
        self.form.addRow("Cx", self.a_cx); self.form.addRow("Cy", self.a_cy)
        self.form.addRow("Sx", self.a_sx); self.form.addRow("Sy", self.a_sy)
        self.form.addRow("Угол", self.a_ang)
        self.form.addRow("Слой", self.a_layer); self.form.addRow("Ширина", self.a_width)
        self._arc_w = [self.a_cx, self.a_cy, self.a_sx, self.a_sy,
                       self.a_ang, self.a_layer, self.a_width]

    def _build_text(self):
        self.t_content = QLineEdit(); self.t_content.editingFinished.connect(self._apply_text)
        self.t_x = self._spin(-1000, 1000); self.t_x.valueChanged.connect(self._apply_text)
        self.t_y = self._spin(-1000, 1000); self.t_y.valueChanged.connect(self._apply_text)
        self.t_rot = self._spin(-360, 360); self.t_rot.valueChanged.connect(self._apply_text)
        self.t_w = self._spin(0.01, 100); self.t_w.valueChanged.connect(self._apply_text)
        self.t_h = self._spin(0.01, 100); self.t_h.valueChanged.connect(self._apply_text)
        self.t_layer = self._layer_combo()
        self.t_layer.currentTextChanged.connect(self._apply_text)
        self.t_visible = QCheckBox(); self.t_visible.stateChanged.connect(self._apply_text)

        self.form.addRow("Текст", self.t_content)
        self.form.addRow("X", self.t_x); self.form.addRow("Y", self.t_y)
        self.form.addRow("Поворот", self.t_rot)
        self.form.addRow("Ширина шрифта", self.t_w)
        self.form.addRow("Высота шрифта", self.t_h)
        self.form.addRow("Слой", self.t_layer)
        self.form.addRow("Видим", self.t_visible)
        self._text_w = [self.t_content, self.t_x, self.t_y, self.t_rot,
                        self.t_w, self.t_h, self.t_layer, self.t_visible]

    # --- выбор ---
    def set_selection(self, items, footprint):
        self.footprint = footprint
        if not items:
            self.item = None
            self.title.setText("Ничего не выбрано")
            for g in (self._pad_w, self._line_w, self._circle_w,
                      self._arc_w, self._text_w):
                self._show(g, False)
            return

        from ui.canvas import (PadItem, LineItem, CircleItem, ArcItem, TextItem)
        it = items[0]
        for g in (self._pad_w, self._line_w, self._circle_w,
                  self._arc_w, self._text_w):
            self._show(g, False)

        if isinstance(it, PadItem):
            self.item = it.pad
            self.title.setText(f"Пад {it.pad.number}")
            self._show(self._pad_w, True)
            self._loading = True
            self.p_number.setText(it.pad.number)
            self.p_shape.setCurrentText(it.pad.shape)
            self.p_x.setValue(it.pad.x); self.p_y.setValue(it.pad.y)
            self.p_w.setValue(it.pad.width); self.p_h.setValue(it.pad.height)
            self.p_drill.setValue(it.pad.drill or 0.0)
            self.p_offx.setValue(it.pad.drill_offset_x)
            self.p_offy.setValue(it.pad.drill_offset_y)
            if it.pad.attachment in _PAD_ATTACHMENTS:
                self.p_attach.setCurrentText(it.pad.attachment)
            self._refresh_pad_limits()
            self._loading = False
        elif isinstance(it, LineItem):
            self.item = it.line
            self.title.setText("Линия")
            self._show(self._line_w, True)
            self._loading = True
            self.l_x1.setValue(it.line.x1); self.l_y1.setValue(it.line.y1)
            self.l_x2.setValue(it.line.x2); self.l_y2.setValue(it.line.y2)
            self.l_layer.setCurrentText(it.line.layer)
            self.l_width.setValue(it.line.width)
            self._loading = False
        elif isinstance(it, CircleItem):
            self.item = it.circle
            self.title.setText("Окружность")
            self._show(self._circle_w, True)
            self._loading = True
            self.c_cx.setValue(it.circle.cx); self.c_cy.setValue(it.circle.cy)
            self.c_r.setValue(it.circle.radius)
            self.c_layer.setCurrentText(it.circle.layer)
            self.c_width.setValue(it.circle.width)
            self._loading = False
        elif isinstance(it, ArcItem):
            self.item = it.arc
            self.title.setText("Дуга")
            self._show(self._arc_w, True)
            self._loading = True
            self.a_cx.setValue(it.arc.cx); self.a_cy.setValue(it.arc.cy)
            self.a_sx.setValue(it.arc.sx); self.a_sy.setValue(it.arc.sy)
            self.a_ang.setValue(it.arc.angle)
            self.a_layer.setCurrentText(it.arc.layer)
            self.a_width.setValue(it.arc.width)
            self._loading = False
        elif isinstance(it, TextItem):
            self.item = it.text_obj
            kind_name = _TEXT_KIND_LABELS.get(it.text_obj.kind, it.text_obj.kind)
            self.title.setText(f"Текст ({kind_name})")
            self._show(self._text_w, True)
            self._loading = True
            self.t_content.setText(it.text_obj.text)
            self.t_x.setValue(it.text_obj.x); self.t_y.setValue(it.text_obj.y)
            self.t_rot.setValue(it.text_obj.rotation)
            self.t_w.setValue(it.text_obj.width); self.t_h.setValue(it.text_obj.height)
            self.t_layer.setCurrentText(it.text_obj.layer)
            self.t_visible.setChecked(it.text_obj.visible)
            self._loading = False
        else:
            self.item = None
            self.title.setText("Объект не редактируется")

    # --- применение ---
    def _refresh_pad_limits(self):
        """Синхронизировать допустимые диапазоны drill и offset с размером пада.

        Пока спинбоксы работают, физически невозможно ввести:
          * диаметр отверстия больше `min(width, height)`;
          * смещение, при котором контур отверстия вылезает за рамку пада.

        Используем `_loading`, чтобы setMaximum()/setRange() не вызвали
        повторных valueChanged и не ушли в рекурсию.
        """
        w = max(self.p_w.value(), 0.01)
        h = max(self.p_h.value(), 0.01)
        max_drill = min(w, h)

        prev_loading = self._loading
        self._loading = True
        try:
            self.p_drill.setMaximum(max_drill)
            drill = self.p_drill.value()
            max_offx = max((w - drill) / 2.0, 0.0)
            max_offy = max((h - drill) / 2.0, 0.0)
            self.p_offx.setRange(-max_offx, max_offx)
            self.p_offy.setRange(-max_offy, max_offy)
        finally:
            self._loading = prev_loading

    def _apply_pad(self, *_):
        if self._loading or self.item is None: return
        self._refresh_pad_limits()

        drill = self.p_drill.value() or None
        # Если отверстия нет — смещения обнуляем, иначе KiCad может
        # показать фантомное смещённое отверстие.
        if drill is None:
            offx = 0.0; offy = 0.0
        else:
            offx = self.p_offx.value()
            offy = self.p_offy.value()
        self._push(EditPadCommand(
            self.item,
            number=self.p_number.text(),
            shape=self.p_shape.currentText(),
            x=self.p_x.value(), y=self.p_y.value(),
            width=self.p_w.value(), height=self.p_h.value(),
            drill=drill,
            drill_offset_x=offx,
            drill_offset_y=offy,
            attachment=self.p_attach.currentText(),
        ))

    def _apply_line(self, *_):
        if self._loading or self.item is None: return
        self._push(EditLineCommand(
            self.item,
            x1=self.l_x1.value(), y1=self.l_y1.value(),
            x2=self.l_x2.value(), y2=self.l_y2.value(),
            layer=self.l_layer.currentText(),
            width=self.l_width.value()))

    def _apply_circle(self, *_):
        if self._loading or self.item is None: return
        r = self.c_r.value()
        self._push(EditCircleCommand(
            self.item,
            cx=self.c_cx.value(), cy=self.c_cy.value(),
            ex=self.c_cx.value() + r, ey=self.c_cy.value(),
            layer=self.c_layer.currentText(),
            width=self.c_width.value()))

    def _apply_arc(self, *_):
        if self._loading or self.item is None: return
        self._push(EditArcCommand(
            self.item,
            cx=self.a_cx.value(), cy=self.a_cy.value(),
            sx=self.a_sx.value(), sy=self.a_sy.value(),
            angle=self.a_ang.value(),
            layer=self.a_layer.currentText(),
            width=self.a_width.value()))

    def _apply_text(self, *_):
        if self._loading or self.item is None:
            return

        new_text = self.t_content.text()
        kind = getattr(self.item, "kind", "")

        # 1) Reference и value — обязательные поля KiCad. Пустое
        #    значение не применяем: возвращаем прежний текст в поле и
        #    показываем причину в статусбаре.
        if kind in ("reference", "value") and not new_text.strip():
            self._loading = True
            try:
                self.t_content.setText(getattr(self.item, "text", ""))
            finally:
                self._loading = False
            self._notify_cannot_clear()
            return

        # 2) Пользовательский текст, если пользователь стёр его целиком,
        #    удаляем сам элемент — иначе на канве останется невидимый
        #    пустой TextItem, который невозможно ни выделить, ни удалить.
        if kind == "user" and not new_text.strip() \
                and self.footprint is not None:
            self._push(RemoveTextCommand(self.footprint, self.item))
            return

        # 3) Обычное обновление.
        self._push(EditTextCommand(
            self.item,
            text=new_text,
            x=self.t_x.value(), y=self.t_y.value(),
            rotation=self.t_rot.value(),
            width=self.t_w.value(), height=self.t_h.value(),
            layer=self.t_layer.currentText(),
            visible=self.t_visible.isChecked()))

    def _notify_cannot_clear(self):
        """Короткое сообщение в статусбар главного окна."""
        win = self.window()
        status = getattr(win, "statusBar", None)
        if callable(status):
            status().showMessage(
                "Текст «обозначение»/«значение» нельзя оставить пустым — "
                "это обязательные поля KiCad.", 3500)

    def _push(self, cmd):
        if self.cmd_stack is not None:
            self.cmd_stack.push(cmd)