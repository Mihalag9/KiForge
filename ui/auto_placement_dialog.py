"""Диалог настроек алгоритма авторазмещения."""
from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)


class AutoPlacementSettingsDialog(QDialog):
    """Настройки anchor-компонента, начальной позиции, потоков и критерия J."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки авторазмещения")
        self.setModal(True)
        self.setMinimumWidth(540)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        description = QLabel(
            "Выберите начальный фиксированный компонент и правило, по которому "
            "алгоритм будет выбирать следующие компоненты."
        )
        description.setWordWrap(True)
        root.addWidget(description)

        form = QFormLayout()
        form.setSpacing(10)
        root.addLayout(form)

        self.priority_combo = QComboBox()
        self.priority_combo.addItem("Максимальная площадь корпуса", "max_size")
        self.priority_combo.addItem("Максимальное количество связей", "max_connections")
        self.priority_combo.setCurrentIndex(1)
        form.addRow("Начальный компонент:", self.priority_combo)

        self.anchor_combo = QComboBox()
        self.anchor_combo.addItem("Левый верхний угол платы", "top_left")
        self.anchor_combo.addItem("Центр платы", "center")
        self.anchor_combo.setCurrentIndex(1)
        form.addRow("Начальная позиция:", self.anchor_combo)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 10)
        self.workers_spin.setSingleStep(1)
        self.workers_spin.setValue(min(4, max(1, os.cpu_count() or 1)))
        self.workers_spin.setToolTip(
            "Потоки используются для параллельной оценки больших наборов "
            "кандидатных позиций. Для маленьких наборов расчёт выполняется "
            "в одном потоке, чтобы не тратить время на запуск ThreadPoolExecutor."
        )
        form.addRow("Количество потоков (1–10):", self.workers_spin)

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
            "Разность связей: размещённые − неразмещённые (3.3.5)", "absolute"
        )
        self.criteria_combo.addItem(
            "Относительная связность (3.3.6)", "relative"
        )
        # Сохраняем поведение существующего запуска, в котором criteria="absolute".
        self.criteria_combo.setCurrentIndex(3)
        form.addRow("Критерий выбора следующего элемента:", self.criteria_combo)

        criterion_help = QLabel(
            "Во всех формулах rᵢⱼ — характеристика связи двух компонентов "
            "по формуле (3.3.2). Сейчас используется λ = 0 и ωₛ = 1, поэтому "
            "вес каждой общей цепи равен единице. При равных значениях критерия "
            "сохраняется исходный порядок компонентов в файле."
        )
        criterion_help.setWordWrap(True)
        criterion_help.setStyleSheet("color: #8A93A8;")
        root.addWidget(criterion_help)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Запустить")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

    def settings(self) -> dict[str, object]:
        """Вернуть параметры в форме, ожидаемой auto_place_board()."""
        return {
            "component_priority": self.priority_combo.currentData(),
            "anchor": self.anchor_combo.currentData(),
            "max_workers": self.workers_spin.value(),
            "criteria": self.criteria_combo.currentData(),
        }
