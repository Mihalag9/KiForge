"""Диалог настроек алгоритма авторазмещения."""
from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)


class AutoPlacementSettingsDialog(QDialog):
    """Настройки anchor-компонента, метрики, геометрии и потоков."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки авторазмещения")
        self.setModal(True)
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        description = QLabel(
            "Выберите начальный фиксированный корпус, критерий выбора "
            "следующих корпусов и параметры геометрического поиска."
        )
        description.setWordWrap(True)
        root.addWidget(description)

        form = QFormLayout()
        form.setSpacing(9)
        root.addLayout(form)

        self.priority_combo = QComboBox()
        self.priority_combo.addItem("Максимальная площадь корпуса", "max_size")
        self.priority_combo.addItem(
            "Максимальное количество связей", "max_connections"
        )
        self.priority_combo.setCurrentIndex(1)
        form.addRow("Начальный корпус:", self.priority_combo)

        self.anchor_combo = QComboBox()
        self.anchor_combo.addItem("Левый верхний угол платы", "top_left")
        self.anchor_combo.addItem("Центр платы", "center")
        self.anchor_combo.setCurrentIndex(1)
        form.addRow("Фиксированная позиция:", self.anchor_combo)

        self.criteria_combo = QComboBox()
        self.criteria_combo.addItem(
            "Максимальная единичная связь (3.3.1)", "max_connection"
        )
        self.criteria_combo.addItem(
            "Сумма связей с размещёнными (3.3.3)", "sum_placed"
        )
        self.criteria_combo.addItem(
            "Количество общих цепей (3.3.4)", "common_nets"
        )
        self.criteria_combo.addItem(
            "Разность связей: размещённые − неразмещённые (3.3.5)",
            "absolute",
        )
        self.criteria_combo.addItem(
            "Относительная связность (3.3.6)", "relative"
        )
        self.criteria_combo.setCurrentIndex(3)
        form.addRow("Критерий выбора следующего корпуса:", self.criteria_combo)

        self.distance_combo = QComboBox()
        self.distance_combo.addItem("Евклидова", "euclidean")
        self.distance_combo.addItem("Манхэттенская", "manhattan")
        form.addRow("Метрика расстояния d:", self.distance_combo)

        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.0, 100.0)
        self.margin_spin.setDecimals(2)
        self.margin_spin.setSingleStep(0.5)
        self.margin_spin.setValue(5.0)
        self.margin_spin.setSuffix(" мм")
        self.margin_spin.setToolTip(
            "A — отступ физического Bounding Box корпуса от контура платы."
        )
        form.addRow("A — отступ от контура платы:", self.margin_spin)

        self.clearance_spin = QDoubleSpinBox()
        self.clearance_spin.setRange(0.0, 50.0)
        self.clearance_spin.setDecimals(2)
        self.clearance_spin.setSingleStep(0.25)
        self.clearance_spin.setValue(1.0)
        self.clearance_spin.setSuffix(" мм")
        self.clearance_spin.setToolTip(
            "B — минимальное расстояние между Bounding Box соседних корпусов."
        )
        form.addRow("B — зазор между корпусами:", self.clearance_spin)

        self.grid_step_spin = QDoubleSpinBox()
        self.grid_step_spin.setRange(0.05, 100.0)
        self.grid_step_spin.setDecimals(3)
        self.grid_step_spin.setSingleStep(0.25)
        self.grid_step_spin.setValue(1.27)
        self.grid_step_spin.setSuffix(" мм")
        self.grid_step_spin.setToolTip(
            "C — шаг сетки кандидатных позиций. Если число точек превышает "
            "лимит, алгоритм увеличивает фактический шаг, сохраняя покрытие области."
        )
        form.addRow("C — шаг сетки:", self.grid_step_spin)

        self.max_grid_spin = QSpinBox()
        self.max_grid_spin.setRange(1_000, 1_000_000)
        self.max_grid_spin.setSingleStep(10_000)
        self.max_grid_spin.setValue(100_000)
        self.max_grid_spin.setToolTip(
            "Максимальное число кандидатных позиций в одной сетке поиска."
        )
        form.addRow("Лимит кандидатных позиций:", self.max_grid_spin)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 10)
        self.workers_spin.setSingleStep(1)
        self.workers_spin.setValue(min(4, max(1, os.cpu_count() or 1)))
        self.workers_spin.setToolTip(
            "Потоки могут использоваться при расчёте J, фильтрации Rₖ "
            "и оценке F. Для маленьких наборов часть расчётов выполняется "
            "последовательно, чтобы избежать накладных расходов."
        )
        form.addRow("Количество потоков (1–10):", self.workers_spin)

        criterion_help = QLabel(
            "Веса цепей рассчитываются автоматически: ωₛ = Pₛ / Pср, "
            "где Pₛ — количество контактных площадок цепи, Pср — среднее "
            "количество площадок по непустым цепям. В формуле (3.3.2) "
            "используется λ = 0. Длина связей в результатах является "
            "взвешенной оценкой расстояний между корпусами, а не длиной "
            "медных дорожек."
        )
        criterion_help.setWordWrap(True)
        criterion_help.setStyleSheet("color: #8A93A8;")
        root.addWidget(criterion_help)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Запустить"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "Отмена"
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

    def settings(self) -> dict[str, object]:
        """Вернуть параметры в форме, ожидаемой auto_place_board()."""
        return {
            "component_priority": self.priority_combo.currentData(),
            "anchor": self.anchor_combo.currentData(),
            "criteria": self.criteria_combo.currentData(),
            "distance_metric": self.distance_combo.currentData(),
            "board_margin_mm": self.margin_spin.value(),
            "min_clearance_mm": self.clearance_spin.value(),
            "grid_step_mm": self.grid_step_spin.value(),
            "max_grid_positions": self.max_grid_spin.value(),
            "max_workers": self.workers_spin.value(),
        }
