"""Панель видимости слоёв."""
from __future__ import annotations
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QScrollArea,
                               QVBoxLayout, QWidget)

KICAD_LAYERS: list[tuple[str, str]] = [
    ("Pad.Num",   "#cccccc"),
    ("F.Cu",      "#c83434"),
    ("B.Cu",      "#4d7fc4"),
    ("F.Adhes",   "#845650"),
    ("B.Adhes",   "#4d4648"),
    ("F.Paste",   "#c591d1"),
    ("B.Paste",   "#7d6f9c"),
    ("F.SilkS",   "#f2eda1"),
    ("B.SilkS",   "#e8b2a7"),
    ("F.Mask",    "#c591d1"),
    ("B.Mask",    "#7d6f9c"),
    ("Dwgs.User", "#c2c2c2"),
    ("Cmts.User", "#c2c2c2"),
    ("Eco1.User", "#c2c2c2"),
    ("Eco2.User", "#c2c2c2"),
    ("Edge.Cuts", "#d0d2cd"),
    ("Margin",    "#d357d3"),
    ("F.CrtYd",   "#d0d2cd"),
    ("B.CrtYd",   "#585d84"),
    ("F.Fab",     "#afafaf"),
    ("B.Fab",     "#585d84"),
]

DEFAULT_VISIBLE = {
    "Pad.Num", "F.Cu", "B.Cu",
    "F.SilkS", "B.SilkS", "F.Mask", "B.Mask",
    "Edge.Cuts", "F.CrtYd", "F.Fab", "B.Fab",
}


class LayersPanel(QWidget):
    visibility_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._boxes: dict[str, QCheckBox] = {}
        root = QVBoxLayout(self); root.setContentsMargins(4, 4, 4, 4)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(2)

        for name, color in KICAD_LAYERS:
            row = QWidget(); h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            sw = QLabel(); sw.setFixedSize(12, 12)
            sw.setStyleSheet(f"background:{color}; border:1px solid #444; border-radius:2px;")
            h.addWidget(sw)
            cb = QCheckBox(name)
            cb.setChecked(name in DEFAULT_VISIBLE)
            cb.stateChanged.connect(self._on_change)
            h.addWidget(cb); h.addStretch(1)
            layout.addWidget(row)
            self._boxes[name] = cb
        layout.addStretch(1)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setWidget(inner); root.addWidget(scroll)

    def visible_layers(self) -> set[str]:
        return {n for n, cb in self._boxes.items() if cb.isChecked()}

    def set_visible_layers(self, layers):
        for n, cb in self._boxes.items():
            cb.blockSignals(True); cb.setChecked(n in layers); cb.blockSignals(False)
        self.visibility_changed.emit()

    def _on_change(self):
        self.visibility_changed.emit()