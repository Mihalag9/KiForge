"""Диалог выбора модулей из библиотеки для открытия во вкладках."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QVBoxLayout)


class ModulePickerDialog(QDialog):
    """Диалог множественного выбора модулей с поиском по имени.

    Показывает только те модули, которых нет в `exclude`.
    Возвращает список имён выбранных модулей (в порядке отображения).
    """

    def __init__(self, modules, exclude=(), parent=None,
                 title="Открыть модули",
                 subtitle=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(480, 560)

        self._exclude = set(exclude)
        # Порядок модулей — как в библиотеке, чтобы пользователь видел их
        # в стабильном порядке.
        self._all_names = [fp.name for fp in modules
                           if fp.name not in self._exclude]

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        if subtitle:
            hint = QLabel(subtitle)
            hint.setWordWrap(True)
            root.addWidget(hint)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по имени…")
        self.search.textChanged.connect(self._refresh)
        root.addWidget(self.search)

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._sync_ok)
        self.list.itemDoubleClicked.connect(lambda _item: self._on_double_click())
        root.addWidget(self.list, 1)

        self.hint = QLabel("")
        self.hint.setStyleSheet("color: #8A93A8;")
        root.addWidget(self.hint)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Открыть")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._refresh()

        # Если совсем нечего показывать — блокируем всё, кроме отмены.
        if not self._all_names:
            self.search.setEnabled(False)
            self.list.setEnabled(False)

    # ------------------------------------------------------------------

    def _refresh(self):
        query = self.search.text().strip().lower()
        self.list.clear()
        for name in self._all_names:
            if query and query not in name.lower():
                continue
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            self.list.addItem(item)

        if not self._all_names:
            self.hint.setText("Нечего выбирать: все модули уже открыты.")
        elif self.list.count() == 0:
            self.hint.setText("Ничего не найдено.")
        else:
            self.hint.setText(f"Найдено модулей: {self.list.count()}")

        self._sync_ok()

    def _sync_ok(self):
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(self.list.isEnabled() and bool(self.list.selectedItems()))

    def _on_double_click(self):
        # Двойной клик по элементу = выбрать только его и принять.
        item = self.list.currentItem()
        if item is None:
            return
        self.list.clearSelection()
        item.setSelected(True)
        self.accept()

    # ------------------------------------------------------------------

    def selected_names(self) -> list[str]:
        """Имена выбранных модулей в порядке отображения."""
        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]

    def accept(self):
        # Не закрываем диалог «ОК» без выбора.
        if not self.selected_names():
            return
        super().accept()