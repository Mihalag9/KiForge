"""Главное окно: вкладки модулей, меню, тулбары."""
from __future__ import annotations
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QFrame,
                               QInputDialog, QLabel, QMainWindow,
                               QMessageBox, QPushButton, QStackedWidget,
                               QTabWidget, QToolBar, QVBoxLayout, QWidget)

from core.commands import CommandStack
from core.footprint import Footprint, new_footprint
from core.library import Library
from io_kicad import reader, writer
from ui.canvas import FootprintView
from ui.layers_panel import LayersPanel
from ui.properties import PropertiesPanel


GRID_PRESETS_MM = [
    2.54, 1.27, 0.635, 0.508, 0.254, 0.127, 0.0635, 0.0508,
    0.0254, 0.0127, 0.0051, 0.0025,
    5.0, 2.5, 1.0, 0.5, 0.25, 0.2, 0.1, 0.05, 0.025, 0.01,
]
MIL_PER_MM = 1.0 / 0.0254
DEFAULT_USER_GRID_MM = 0.254
DEFAULT_GRID_MM = 1.0

TOOLS = [
    ("select",  "select",  "Выбор/перемещение"),
    ("pad",     "pad",     "Добавить пад"),
    ("line",    "line",    "Добавить линию (2 клика)"),
    ("circle",  "circle",  "Добавить окружность (2 клика: центр, радиус)"),
    ("arc",     "arc",     "Добавить дугу (3 клика: центр, начало, направление)"),
    ("text",    "text",    "Добавить текст (клик → ввод)"),
    ("anchor",  "anchor",  "Точка привязки модуля"),
    ("delete",  "delete",  "Удалить элемент"),
]

_ICONS_DIR = Path(__file__).parent / "icons"


def _icon(name: str) -> QIcon:
    """Загрузить SVG-иконку из ui/icons/."""
    p = _ICONS_DIR / f"{name}.svg"
    return QIcon(str(p)) if p.exists() else QIcon()


def _fmt_mm(mm): return f"{mm:.4f}".replace(".", ",")
def _grid_label(mm): return f"Сетка: {_fmt_mm(mm)} мм ({_fmt_mm(mm * MIL_PER_MM)} мил)"
def _user_grid_label(mm): return f"Пользовательская: {_fmt_mm(mm)} мм ({_fmt_mm(mm * MIL_PER_MM)} мил)"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KiForge")
        self.resize(1400, 900)

        self.library: Library | None = None
        self.current_path: Path | None = None
        self.user_grid_mm = DEFAULT_USER_GRID_MM
        self.current_grid_mm = DEFAULT_GRID_MM

        # Глобальное состояние сетки и привязки — единое для всех вкладок.
        # При выключении сетки привязка автоматически снимается: рисование
        # и перетаскивание идут свободно (см. canvas.FootprintScene).
        self.grid_visible = True
        self.snap_to_grid = True

        # Канва активного таба
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Панель слоёв
        self.layers = LayersPanel(self)
        self.layers.visibility_changed.connect(self._on_layers_changed)
        self.dock_layers = QDockWidget("Менеджер слоёв", self)
        self.dock_layers.setWidget(self.layers)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock_layers)

        # Панель свойств
        self.props = PropertiesPanel()
        self.dock_props = QDockWidget("Свойства", self)
        self.dock_props.setWidget(self.props)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock_props)

        # Центральный стек
        self.welcome = self._make_welcome()
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.welcome)
        self.stack.addWidget(self.tabs)
        self.setCentralWidget(self.stack)

        self._build_menu()
        self._build_top_toolbar()
        self._build_tools_toolbar()

        self._apply_grid(self.current_grid_mm)
        self._update_state()

    # ------------------------------------------------------------------
    # Табы
    # ------------------------------------------------------------------

    def _current_canvas(self) -> FootprintView | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, FootprintView) else None

    def _current_stack(self) -> CommandStack | None:
        c = self._current_canvas()
        return c.cmd_stack if c else None

    def _iter_canvases(self):
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, FootprintView):
                yield w

    def _add_module_tab(self, fp: Footprint):
        canvas = FootprintView(self)
        stack = CommandStack()
        stack.on_change = lambda c=canvas: self._on_stack_change(c)
        canvas.cmd_stack = stack
        canvas.selection_changed = self._on_selection_changed
        canvas.set_visible_layers(self.layers.visible_layers())
        canvas.set_footprint(fp)
        # Новая вкладка наследует текущее глобальное состояние сетки/привязки.
        s = canvas.scene()
        s.grid_step = self.current_grid_mm
        s.grid_visible = self.grid_visible
        s.snap_to_grid = self.snap_to_grid
        s.update()
        idx = self.tabs.addTab(canvas, fp.name)
        self.tabs.setCurrentIndex(idx)

    def _close_tab(self, index: int):
        w = self.tabs.widget(index)
        if w is None: return
        self.tabs.removeTab(index)
        w.deleteLater()
        # Если удалили модуль из библиотеки — убираем и из модели
        if self.library is not None:
            for fp in list(self.library.modules):
                if getattr(w, "footprint", None) is fp:
                    self.library.modules.remove(fp)
                    break
        self._update_state()

    def _on_tab_changed(self, index: int):
        c = self._current_canvas()
        self.props.set_selection([], self.library)
        if c is not None:
            self._rebind_props(c)
            self._select_tool_action("select")

    def _rebind_props(self, canvas: FootprintView):
        self.props.cmd_stack = canvas.cmd_stack

    # ------------------------------------------------------------------
    # Приветствие
    # ------------------------------------------------------------------

    def _make_welcome(self) -> QWidget:
        w = QWidget()
        w.setObjectName("welcomeRoot")

        outer = QVBoxLayout(w)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setObjectName("welcomeCard")
        card.setFixedWidth(520)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(48, 44, 48, 44)
        cv.setSpacing(0)

        title = QLabel("KiForge")
        title.setObjectName("welcomeTitle")
        title.setAlignment(Qt.AlignCenter)
        cv.addWidget(title)

        cv.addSpacing(36)

        btn_new = QPushButton("Создать новую библиотеку")
        btn_new.setObjectName("welcomeBtnPrimary")
        btn_new.setFixedHeight(46)
        btn_new.setCursor(Qt.PointingHandCursor)
        btn_new.clicked.connect(self.new_library)
        cv.addWidget(btn_new)

        cv.addSpacing(10)

        btn_open = QPushButton("Открыть .mod…")
        btn_open.setObjectName("welcomeBtnSecondary")
        btn_open.setFixedHeight(46)
        btn_open.setCursor(Qt.PointingHandCursor)
        btn_open.clicked.connect(self.open_file)
        cv.addWidget(btn_open)

        outer.addWidget(card)
        return w

    # ------------------------------------------------------------------
    # Состояние «есть ли документ»
    # ------------------------------------------------------------------

    def _update_state(self):
        has = self.library is not None
        for a in self._doc_actions:
            a.setEnabled(has)
        self.top_toolbar.setVisible(has)
        self.tools_toolbar.setVisible(has)
        self.dock_props.setVisible(has)
        self.dock_layers.setVisible(has)
        self.menu_view.menuAction().setVisible(has)
        if has:
            self.stack.setCurrentWidget(self.tabs)
        else:
            self.stack.setCurrentWidget(self.welcome)
            self.props.set_selection([], None)
            # На welcome-экране статусбар всегда пуст: никаких
            # приглашений, подсказок и «залипших» сообщений от прошлых
            # действий. Любой будущий showMessage будет сброшен при
            # возврате в этот режим.
            self.statusBar().clearMessage()
        self._update_title()

    def _update_title(self):
        self.setWindowTitle("KiForge")

    # ------------------------------------------------------------------
    # Меню
    # ------------------------------------------------------------------

    def _build_menu(self):
        mb = self.menuBar()
        self._doc_actions: list[QAction] = []

        m = mb.addMenu("Файл")
        self._act(m, "Новая библиотека…", self.new_library)
        self._act(m, "Открыть…", self.open_file)
        m.addSeparator()
        self._doc_actions.append(self._act(m, "Сохранить", self.save_file))
        self._doc_actions.append(self._act(m, "Сохранить как…", self.save_file_as))
        m.addSeparator()
        self._doc_actions.append(self._act(m, "Добавить модуль…", self.add_module))
        self._doc_actions.append(self._act(m, "Переименовать модуль…", self.rename_module))
        m.addSeparator()
        self._act(m, "Выход", self.close)

        m = mb.addMenu("Вид")
        self.menu_view = m
        self._doc_actions.append(self._act(m, "Сетка вкл/выкл", self.toggle_grid))
        self._doc_actions.append(self._act(m, "Править сетку…", self._edit_user_grid))
        m.addSeparator()
        self._doc_actions.append(self._act(m, "Приблизить", self._zoom_in))
        self._doc_actions.append(self._act(m, "Отдалить", self._zoom_out))
        self._doc_actions.append(self._act(m, "Сброс масштаба", self._zoom_reset))

    def _act(self, menu, text, slot):
        a = QAction(text, self); a.triggered.connect(slot)
        menu.addAction(a); return a

    # ------------------------------------------------------------------
    # Верхний тулбар
    # ------------------------------------------------------------------

    def _build_top_toolbar(self):
        tb = self.addToolBar("Основное")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.top_toolbar = tb

        self.act_save_tb = QAction(_icon("save"), "Сохранить", self)
        self.act_save_tb.setToolTip("Сохранить библиотеку")
        self.act_save_tb.triggered.connect(self.save_file)
        tb.addAction(self.act_save_tb)

        tb.addSeparator()

        self.act_undo_tb = QAction(_icon("undo"), "Отменить", self)
        self.act_undo_tb.setToolTip("Отменить последнее действие (Ctrl+Z)")
        self.act_undo_tb.triggered.connect(self.undo)
        tb.addAction(self.act_undo_tb)

        self.act_redo_tb = QAction(_icon("redo"), "Повторить", self)
        self.act_redo_tb.setToolTip("Повторить отменённое действие (Ctrl+Y)")
        self.act_redo_tb.triggered.connect(self.redo)
        tb.addAction(self.act_redo_tb)

        tb.addSeparator()

        self.act_resetzoom_tb = tb.addAction("Сброс масштаба", self._zoom_reset)
        tb.addSeparator()
        tb.addWidget(QLabel(" Сетка: "))
        self._build_grid_combo(tb)

        self._doc_actions.extend([
            self.act_save_tb, self.act_undo_tb, self.act_redo_tb,
            self.act_resetzoom_tb,
        ])

    def _build_grid_combo(self, tb):
        cb = QComboBox(); cb.setMinimumWidth(240)
        for mm in GRID_PRESETS_MM:
            cb.addItem(_grid_label(mm), mm)
        cb.insertSeparator(cb.count())
        cb.addItem(_user_grid_label(self.user_grid_mm), "user")
        cb.insertSeparator(cb.count())
        cb.addItem("Править сетку…", "edit")
        for i in range(cb.count()):
            if cb.itemData(i) == DEFAULT_GRID_MM:
                cb.setCurrentIndex(i); break
        cb.activated.connect(self._on_grid_combo)
        self.grid_combo = cb
        tb.addWidget(cb)

    def _on_grid_combo(self, index):
        d = self.grid_combo.itemData(index)
        if d == "edit":
            self._edit_user_grid()
            self._select_grid_in_combo(self.current_grid_mm); return
        if d == "user":
            self._apply_grid(self.user_grid_mm); return
        if isinstance(d, float):
            self._apply_grid(d)

    def _select_grid_in_combo(self, mm):
        for i in range(self.grid_combo.count()):
            d = self.grid_combo.itemData(i)
            if isinstance(d, float) and abs(d - mm) < 1e-9:
                self.grid_combo.setCurrentIndex(i); return
        if abs(mm - self.user_grid_mm) < 1e-9:
            for i in range(self.grid_combo.count()):
                if self.grid_combo.itemData(i) == "user":
                    self.grid_combo.setCurrentIndex(i); return

    def _apply_grid(self, mm):
        self.current_grid_mm = mm
        for c in self._iter_canvases():
            c.scene().grid_step = mm
            c.scene().update()
        self.statusBar().showMessage(f"Шаг сетки: {_fmt_mm(mm)} мм", 2000)

    def _edit_user_grid(self):
        v, ok = QInputDialog.getDouble(self, "Сетка", "Шаг, мм:",
                                       self.user_grid_mm, 0.0001, 100.0, 4)
        if not ok: return
        self.user_grid_mm = float(v)
        for i in range(self.grid_combo.count()):
            if self.grid_combo.itemData(i) == "user":
                self.grid_combo.setItemText(i, _user_grid_label(self.user_grid_mm))
                break
        self._apply_grid(self.user_grid_mm)
        self._select_grid_in_combo(self.user_grid_mm)

    # ------------------------------------------------------------------
    # Инструменты
    # ------------------------------------------------------------------

    def _build_tools_toolbar(self):
        tb = QToolBar("Инструменты", self)
        tb.setOrientation(Qt.Vertical)
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setStyleSheet(
            "QToolBar { padding: 4px; spacing: 4px; } "
            "QToolButton { padding: 6px; min-width: 32px; min-height: 32px; "
            "border-radius: 6px; }"
        )
        self.addToolBar(Qt.LeftToolBarArea, tb)
        self.tools_toolbar = tb

        self._tool_group = QActionGroup(self)
        self._tool_group.setExclusive(True)

        for tid, icon_name, tip in TOOLS:
            a = QAction(_icon(icon_name), "", self)
            a.setCheckable(True)
            a.setToolTip(tip)
            a.setData(tid)
            a.triggered.connect(lambda _c=False, t=tid: self._set_tool(t))
            self._tool_group.addAction(a)
            tb.addAction(a)
            if tid == "select":
                a.setChecked(True)

    def _set_tool(self, tid):
        c = self._current_canvas()
        if c is not None:
            c.set_tool(tid)

    def _select_tool_action(self, tid):
        for a in self._tool_group.actions():
            if a.data() == tid:
                a.setChecked(True); return

    # ------------------------------------------------------------------
    # Файл
    # ------------------------------------------------------------------

    def new_library(self):
        name, ok = QInputDialog.getText(self, "Новая библиотека",
                                        "Имя файла (без .mod):", text="my_lib")
        if not ok: return
        name = name.strip() or "my_lib"

        self.library = Library(file_name=name)
        self.current_path = None
        self._clear_tabs()
        fp = new_footprint(name="Module_1")
        self.library.modules.append(fp)
        self._add_module_tab(fp)
        self._update_state()
        self.statusBar().showMessage(
            f"Создана библиотека «{name}». Модулей: 1", 4000)

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть .mod", "",
            "PCBNEW library (*.mod);;Все файлы (*)")
        if not path: return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            lib = reader.read(text)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось прочитать файл:\n{e}")
            return

        self.library = lib
        self.current_path = Path(path)
        if not self.library.file_name:
            self.library.file_name = Path(path).stem

        self._clear_tabs()
        if not lib.modules:
            QMessageBox.information(self, "Пустая библиотека",
                                    "В файле нет ни одного $MODULE. "
                                    "Создайте модуль через меню.")
        for fp in lib.modules:
            self._add_module_tab(fp)
        self._update_state()
        self.statusBar().showMessage(
            f"Открыто: {path}  [модулей: {len(lib.modules)}]", 4000)

    def _clear_tabs(self):
        while self.tabs.count():
            w = self.tabs.widget(0)
            self.tabs.removeTab(0)
            w.deleteLater()

    def save_file(self):
        if self.library is None: return
        if self.current_path is None:
            self.save_file_as(); return
        self._save_to(self.current_path)

    def save_file_as(self):
        if self.library is None: return
        default = f"{self.library.file_name or 'library'}.mod"
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить .mod", default,
            "PCBNEW library (*.mod);;Все файлы (*)")
        if not path: return
        self._save_to(Path(path))

    def _save_to(self, path: Path):
        try:
            text = writer.write(self.library)
            path.write_text(text, encoding="utf-8")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить:\n{e}")
            return
        self.current_path = path
        self.library.file_name = path.stem
        self._update_title()
        self.statusBar().showMessage(f"Сохранено: {path}", 4000)

    # ------------------------------------------------------------------
    # Модули
    # ------------------------------------------------------------------

    def add_module(self):
        if self.library is None: return
        name, ok = QInputDialog.getText(self, "Новый модуль", "Имя:", text="Module")
        if not ok: return
        name = name.strip() or "Module"
        fp = new_footprint(name=name)
        self.library.modules.append(fp)
        self._add_module_tab(fp)
        self._update_title()
        self.statusBar().showMessage(f"Добавлен модуль «{name}»", 3000)

    def rename_module(self):
        if self.library is None: return
        c = self._current_canvas()
        if c is None or c.footprint is None: return
        name, ok = QInputDialog.getText(self, "Переименовать модуль",
                                        "Новое имя:", text=c.footprint.name)
        if not ok: return
        name = name.strip() or c.footprint.name
        c.footprint.name = name
        idx = self.tabs.currentIndex()
        self.tabs.setTabText(idx, name)
        self._update_title()

    # ------------------------------------------------------------------
    # Правка
    # ------------------------------------------------------------------

    def undo(self):
        s = self._current_stack()
        if s is not None: s.undo()

    def redo(self):
        s = self._current_stack()
        if s is not None: s.redo()

    # ------------------------------------------------------------------
    # Вид
    # ------------------------------------------------------------------

    def toggle_grid(self):
        """Сетка вкл/выкл. При выключении сетки привязка снимается.

        Логика: пока сетка видна — элементы «щёлкают» по её узлам;
        как только сетку гасят, инструменты рисуют и таскают элементы
        свободно (с округлением до 0.1 мкм, чтобы не копить float-мусор).
        Состояние применяется ко всем вкладкам сразу.
        """
        self.grid_visible = not self.grid_visible
        # Привязка возможна только при видимой сетке.
        self.snap_to_grid = self.grid_visible
        for c in self._iter_canvases():
            s = c.scene()
            s.grid_visible = self.grid_visible
            s.snap_to_grid = self.snap_to_grid
            s.update()
        if self.grid_visible:
            self.statusBar().showMessage(
                f"Сетка включена (шаг {_fmt_mm(self.current_grid_mm)} мм) — "
                "элементы привязаны к сетке", 2500)
        else:
            self.statusBar().showMessage(
                "Сетка выключена — рисование и перемещение без привязки",
                2500)

    def _zoom_in(self):
        c = self._current_canvas()
        if c is not None: c.zoom_in()

    def _zoom_out(self):
        c = self._current_canvas()
        if c is not None: c.zoom_out()

    def _zoom_reset(self):
        c = self._current_canvas()
        if c is not None: c.reset_zoom()

    # ------------------------------------------------------------------
    # Реакции
    # ------------------------------------------------------------------

    def _on_stack_change(self, canvas: FootprintView):
        if self.library is not None:
            canvas.rebuild()

    def _on_selection_changed(self, items):
        fp = None
        c = self._current_canvas()
        if c is not None:
            fp = c.footprint
            self.props.cmd_stack = c.cmd_stack
        self.props.set_selection(items, fp)

    def _on_layers_changed(self):
        for c in self._iter_canvases():
            c.set_visible_layers(self.layers.visible_layers())