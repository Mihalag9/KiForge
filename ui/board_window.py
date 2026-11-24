"""Окно просмотра legacy-платы KiCad."""
from __future__ import annotations

from pathlib import Path
import traceback

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCursor
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QDialog,
    QMainWindow,
    QMessageBox,
)

from core.board import Board
from core.auto_placement import auto_place_board
from io_kicad import board_reader
from ui.auto_placement_dialog import AutoPlacementSettingsDialog
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

BOARD_DEFAULT_VISIBLE = {
    "F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "Edge.Cuts"
}


class BoardWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KiForge — плата")
        self.resize(1200, 800)

        self.board: Board | None = None
        self.current_path: Path | None = None

        self.view = BoardView(self)
        self.setCentralWidget(self.view)

        self.layers = LayersPanel(
            self,
            layers=BOARD_LAYERS,
            default_visible=BOARD_DEFAULT_VISIBLE,
        )
        self.layers.visibility_changed.connect(self._on_layers_changed)

        self.dock_layers = QDockWidget("Слои", self)
        self.dock_layers.setWidget(self.layers)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock_layers)
        self.view.set_visible_layers(self.layers.visible_layers())

        self._build_menu()
        self.statusBar().showMessage("Плата не открыта")

    def _build_menu(self):
        menu = self.menuBar().addMenu("Файл")
        menu.addAction("Открыть плату…", self.open_file)
        menu.addSeparator()
        menu.addAction("Закрыть", self.close)

        menu = self.menuBar().addMenu("Вид")
        self.act_ratsnest = QAction("Связи", self)
        self.act_ratsnest.setCheckable(True)
        self.act_ratsnest.setChecked(True)
        self.act_ratsnest.toggled.connect(self.view.set_ratsnest_visible)
        menu.addAction(self.act_ratsnest)

        self.act_grid = QAction("Сетка", self)
        self.act_grid.setCheckable(True)
        self.act_grid.setChecked(True)
        self.act_grid.toggled.connect(self.view.set_grid_visible)
        menu.addAction(self.act_grid)

        menu.addSeparator()
        menu.addAction("Приблизить", self.view.zoom_in)
        menu.addAction("Отдалить", self.view.zoom_out)
        menu.addAction("Сброс масштаба", self.view.reset_zoom)

        menu = self.menuBar().addMenu("Инструменты")
        self.act_auto_place = QAction("Авторазмещение компонентов…", self)
        self.act_auto_place.triggered.connect(self.auto_place)
        menu.addAction(self.act_auto_place)

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть плату",
            "",
            "Платы KiCad (*.brd);;Все файлы (*)",
        )
        if path:
            self.load_file(Path(path))

    def load_file(self, path: Path) -> bool:
        try:
            text = Path(path).read_text(
                encoding="utf-8",
                errors="replace",
            )
            board = board_reader.read(text)
        except Exception as error:
            QMessageBox.critical(
                self,
                "Ошибка",
                f"Не удалось прочитать плату:\n{error}",
            )
            return False

        self.board = board
        self.current_path = Path(path)
        self.setWindowTitle(f"KiForge — плата: {Path(path).name}")
        self.view.set_board(board)
        self.view.set_visible_layers(self.layers.visible_layers())

        pad_count = sum(len(module.pads) for module in board.modules)
        net_count = len([net for net in board.nets if net > 0])
        self.statusBar().showMessage(
            f"Модули: {len(board.modules)}  |  "
            f"Пады: {pad_count}  |  "
            f"Дорожки: {len(board.tracks)}  |  "
            f"Цепи: {net_count}"
        )
        return True

    def _on_layers_changed(self):
        self.view.set_visible_layers(self.layers.visible_layers())

    def auto_place(self):
        if self.board is None:
            QMessageBox.warning(
                self,
                "Авторазмещение",
                "Сначала откройте плату (.brd файл).",
            )
            return

        if not self.board.modules:
            QMessageBox.warning(
                self,
                "Авторазмещение",
                "На плате нет компонентов для размещения.",
            )
            return

        dialog = AutoPlacementSettingsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        settings = dialog.settings()

        confirmation = QMessageBox.question(
            self,
            "Авторазмещение компонентов",
            f"Будет размещено {len(self.board.modules)} корпусов.\n"
            "Текущие координаты корпусов будут изменены.\n\n"
            f"Критерий выбора: {dialog.criteria_combo.currentText()}\n"
            f"Метрика d: {dialog.distance_combo.currentText()}\n"
            f"Количество потоков: {settings['max_workers']}\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        self.setCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            result = auto_place_board(
                board=self.board,
                neighbor_offset_mm=15.0,
                **settings,
            )

            self.view.set_board(self.board)
            self.view.update()

            initial_label = (
                "Исходная оценка: "
                f"{result.initial_weighted_connection_length_mm:.2f} мм"
            )
            placement_label = (
                "После последовательного размещения: "
                f"{result.after_placement_weighted_connection_length_mm:.2f} мм"
            )
            local_label = (
                "После локальной оптимизации: "
                f"{result.after_local_optimization_weighted_connection_length_mm:.2f} мм"
            )
            placement_gain = (
                f"Изменение после размещения: "
                f"{result.placement_reduction_mm:+.2f} мм "
                f"({result.placement_improvement_percent:+.2f} %)"
            )
            local_gain = (
                f"Изменение после ЛО: "
                f"{result.local_optimization_reduction_mm:+.2f} мм "
                f"({result.local_optimization_improvement_percent:+.2f} %)"
            )

            metric_name = (
                "Манхэттенская"
                if settings["distance_metric"] == "manhattan"
                else "Евклидова"
            )

            self.statusBar().showMessage(
                "Авторазмещение выполнено! "
                f"Время: {result.execution_time_sec:.3f} с  |  "
                f"Связей: {result.connection_count}  |  "
                f"После ЛО: "
                f"{result.after_local_optimization_weighted_connection_length_mm:.2f} мм  |  "
                f"Размещено: {result.placed_count}/{result.component_count}",
                10000,
            )

            details = (
                "Авторазмещение завершено.\n\n"
                f"Время выполнения: {result.execution_time_sec:.4f} сек\n"
                f"Корпусов размещено: {result.placed_count} из {result.component_count}\n"
                f"Связей между корпусами: {result.connection_count}\n\n"
                "Взвешенная оценка расстояний между корпусами "
                "(не физическая длина медных дорожек):\n"
                f"  {initial_label}\n"
                f"  {placement_label}\n"
                f"  {local_label}\n"
                f"  {placement_gain}\n"
                f"  {local_gain}\n\n"
                f"Метрика d: {metric_name}\n"
                f"A — отступ от контура: {settings['board_margin_mm']:.2f} мм\n"
                f"B — минимальный зазор: {settings['min_clearance_mm']:.2f} мм\n"
                f"C — шаг сетки: {settings['grid_step_mm']:.3f} мм\n"
                f"Лимит кандидатных позиций: {settings['max_grid_positions']}\n"
                f"Максимальное количество потоков: {settings['max_workers']}\n\n"
                "Порядок размещения:\n"
                + ", ".join(result.placement_order)
            )

            if result.failed_to_place:
                details += (
                    "\n\nНе удалось разместить без пересечений:\n"
                    + ", ".join(result.failed_to_place)
                )

            QMessageBox.information(
                self,
                "Авторазмещение завершено",
                details,
            )

        except Exception as error:
            QMessageBox.critical(
                self,
                "Ошибка авторазмещения",
                f"Произошла ошибка:\n{error}",
            )
            traceback.print_exc()
        finally:
            self.unsetCursor()
