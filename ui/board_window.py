"""Окно просмотра legacy-платы KiCad."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QDockWidget, QFileDialog, QMainWindow,
                               QMessageBox)

from core.board import Board
from io_kicad import board_reader
from ui.board_canvas import BoardView
from ui.canvas import DEFAULT_LAYER_COLOR, LAYER_COLORS
from ui.layers_panel import LayersPanel


BOARD_LAYERS: list[tuple[str, str]] = [
    (name, LAYER_COLORS.get(name, DEFAULT_LAYER_COLOR))
    for name in (
        ["B.Cu"] + [f"In{i}.Cu" for i in range(1, 15)] + ["F.Cu"] + [
            "B.Adhes", "F.Adhes", "B.Paste", "F.Paste",
            "B.SilkS", "F.SilkS", "B.Mask", "F.Mask",
            "Dwgs.User", "Cmts.User", "Eco1.User", "Eco2.User",
            "Edge.Cuts", "Margin", "B.CrtYd", "F.CrtYd",
        ]
    )
]

BOARD_DEFAULT_VISIBLE = {"F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "Edge.Cuts"}


class BoardWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KiForge — плата")
        self.resize(1200, 800)

        self.board: Board | None = None
        self.current_path: Path | None = None

        self.view = BoardView(self)
        self.setCentralWidget(self.view)

        self.layers = LayersPanel(self, layers=BOARD_LAYERS,
                                  default_visible=BOARD_DEFAULT_VISIBLE)
        self.layers.visibility_changed.connect(self._on_layers_changed)
        self.dock_layers = QDockWidget("Слои", self)
        self.dock_layers.setWidget(self.layers)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock_layers)
        self.view.set_visible_layers(self.layers.visible_layers())

        self._build_menu()
        self.statusBar().showMessage("Плата не открыта")

    def _build_menu(self):
        m = self.menuBar().addMenu("Файл")
        m.addAction("Открыть плату…", self.open_file)
        m.addSeparator()
        m.addAction("Закрыть", self.close)

        m = self.menuBar().addMenu("Вид")
        self.act_ratsnest = QAction("Связи", self)
        self.act_ratsnest.setCheckable(True)
        self.act_ratsnest.setChecked(True)
        self.act_ratsnest.toggled.connect(self.view.set_ratsnest_visible)
        m.addAction(self.act_ratsnest)

        self.act_grid = QAction("Сетка", self)
        self.act_grid.setCheckable(True)
        self.act_grid.setChecked(True)
        self.act_grid.toggled.connect(self.view.set_grid_visible)
        m.addAction(self.act_grid)

        m.addSeparator()
        m.addAction("Приблизить", self.view.zoom_in)
        m.addAction("Отдалить", self.view.zoom_out)
        m.addAction("Сброс масштаба", self.view.reset_zoom)

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть плату", "",
            "Платы KiCad (*.brd);;Все файлы (*)")
        if not path:
            return
        self.load_file(Path(path))

    def load_file(self, path: Path) -> bool:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            board = board_reader.read(text)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка",
                                 f"Не удалось прочитать плату:\n{e}")
            return False

        self.board = board
        self.current_path = Path(path)
        self.setWindowTitle(f"KiForge — плата: {Path(path).name}")
        self.view.set_board(board)
        self.view.set_visible_layers(self.layers.visible_layers())

        pads = sum(len(m.pads) for m in board.modules)
        nets = len([n for n in board.nets if n > 0])
        self.statusBar().showMessage(
            f"Модули: {len(board.modules)}  |  Пады: {pads}  |  "
            f"Дорожки: {len(board.tracks)}  |  Цепи: {nets}")
        return True

    def _on_layers_changed(self):
        self.view.set_visible_layers(self.layers.visible_layers())
