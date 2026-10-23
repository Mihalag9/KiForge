"""Главное окно: вкладки модулей, меню, тулбары."""
from __future__ import annotations
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PySide6.QtWidgets import (QComboBox, QDialog, QDockWidget, QFileDialog,
                               QFrame, QInputDialog, QLabel, QMainWindow,
                               QMenu, QMessageBox, QPushButton, QStackedWidget,
                               QTabWidget, QToolBar, QVBoxLayout, QWidget)

from core.commands import CommandStack
from core.footprint import Footprint, new_footprint
from core.library import Library
from io_kicad import reader, writer
from ui.board_window import BoardWindow
from ui.canvas import FootprintView
from ui.layers_panel import LayersPanel
from ui.module_picker import ModulePickerDialog
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
        self.board_windows: list[BoardWindow] = []
        self.user_grid_mm = DEFAULT_USER_GRID_MM
        self.current_grid_mm = DEFAULT_GRID_MM

        # Глобальное состояние сетки и привязки — единое для всех вкладок.
        self.grid_visible = True
        self.snap_to_grid = True

        # Вкладки модулей
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        # Контекстное меню на самом табе (правый клик) — быстрый доступ
        # к «Переименовать»/«Удалить» без захода в верхнее меню.
        self._setup_tab_context_menu()

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

        # Центральный стек: welcome → empty → tabs
        self.welcome = self._make_welcome()
        self.empty = self._make_empty()
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.welcome)
        self.stack.addWidget(self.empty)
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

    def _open_module(self, fp: Footprint):
        """Открыть модуль в новой вкладке; если уже открыт — переключиться."""
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if getattr(w, "footprint", None) is fp:
                self.tabs.setCurrentIndex(i)
                return
        self._add_module_tab(fp)

    def _close_tab(self, index: int):
        w = self.tabs.widget(index)
        if w is None:
            return
        self.tabs.removeTab(index)
        w.deleteLater()
        # ВАЖНО: модуль остаётся в library.modules — вкладка была лишь
        # представлением. Пользователь может снова открыть его через
        # «Файл → Открыть модуль…» или кнопку в тулбаре.
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
    # Диалоги с русскими надписями кнопок
    # ------------------------------------------------------------------

    def _msg_info(self, title: str, text: str):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText("ОК")
        box.exec()

    def _msg_critical(self, title: str, text: str):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText("ОК")
        box.exec()

    def _msg_ask(self, title: str, text: str,
                 yes_text: str = "Да", no_text: str = "Нет") -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Yes |
                               QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText(yes_text)
        box.button(QMessageBox.StandardButton.No).setText(no_text)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.exec()
        return box.standardButton(box.clickedButton()) == \
            QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------
    # Контекстное меню на вкладке
    # ------------------------------------------------------------------

    def _setup_tab_context_menu(self):
        bar = self.tabs.tabBar()
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        bar.customContextMenuRequested.connect(self._on_tab_context_menu)

    def _on_tab_context_menu(self, pos):
        bar = self.tabs.tabBar()
        idx = bar.tabAt(pos)
        if idx < 0:
            return
        # Активировать вкладку, по которой щёлкнули, — чтобы действия
        # ниже работали над «тем самым» модулем, а не над случайно
        # выбранным.
        self.tabs.setCurrentIndex(idx)

        menu = QMenu(self)
        menu.addAction("Переименовать модуль…", self.rename_module)
        menu.addAction("Удалить модуль…", self.delete_module)
        menu.addSeparator()
        menu.addAction("Открыть ещё модуль…", self.add_modules)
        menu.addAction("Создать модуль…", self.add_module)
        menu.addSeparator()
        menu.addAction("Закрыть вкладку",
                       lambda: self._close_tab(self.tabs.currentIndex()))
        menu.exec(bar.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Приветствие / пустая библиотека
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

        cv.addSpacing(10)

        btn_board = QPushButton("Открыть плату .brd…")
        btn_board.setObjectName("welcomeBtnSecondary")
        btn_board.setFixedHeight(46)
        btn_board.setCursor(Qt.PointingHandCursor)
        btn_board.clicked.connect(self.open_board_file)
        cv.addWidget(btn_board)

        outer.addWidget(card)
        return w

    def _make_empty(self) -> QWidget:
        """Экран «библиотека загружена, но ни одна вкладка не открыта»."""
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

        title = QLabel("Библиотека загружена")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 24px; font-weight: 700;")
        cv.addWidget(title)

        cv.addSpacing(12)

        sub = QLabel("Выберите модуль")
        sub.setObjectName("welcomeSub")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        cv.addWidget(sub)

        cv.addSpacing(28)

        btn_open = QPushButton("Открыть модуль…")
        btn_open.setObjectName("welcomeBtnPrimary")
        btn_open.setFixedHeight(46)
        btn_open.setCursor(Qt.PointingHandCursor)
        btn_open.clicked.connect(self.add_modules)
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
        self.menu_edit.menuAction().setVisible(has)

        if not has:
            self.stack.setCurrentWidget(self.welcome)
            self.props.set_selection([], None)
            self.statusBar().clearMessage()
        elif self.tabs.count() == 0:
            # Библиотека загружена, но пользователь ещё не открыл вкладок.
            self.stack.setCurrentWidget(self.empty)
            self.props.set_selection([], None)
        else:
            self.stack.setCurrentWidget(self.tabs)
        self._update_title()

    def _update_title(self):
        if self.library is not None and self.library.file_name:
            self.setWindowTitle(f"KiForge — {self.library.file_name}")
        else:
            self.setWindowTitle("KiForge")

    # ------------------------------------------------------------------
    # Меню
    # ------------------------------------------------------------------

    def _build_menu(self):
        mb = self.menuBar()
        self._doc_actions: list[QAction] = []

        m = mb.addMenu("Файл")
        # Ctrl+N / Ctrl+O работают всегда, даже без загруженной библиотеки,
        # поэтому не добавляем их в _doc_actions (которые гасятся без документа).
        self._act(m, "Новая библиотека…", self.new_library, shortcuts="Ctrl+N")
        self._act(m, "Открыть…", self.open_file, shortcuts="Ctrl+O")
        # Плата — отдельный документ, не зависит от загруженной библиотеки.
        self._act(m, "Открыть плату .brd…", self.open_board_file)
        m.addSeparator()
        self._doc_actions.append(
            self._act(m, "Сохранить", self.save_file, shortcuts="Ctrl+S"))
        self._doc_actions.append(
            self._act(m, "Сохранить как…", self.save_file_as,
                      shortcuts="Ctrl+Shift+S"))
        m.addSeparator()
        self._doc_actions.append(self._act(m, "Открыть модуль…", self.add_modules))
        self._doc_actions.append(
            self._act(m, "Создать модуль…", self.add_module,
                      shortcuts="Ctrl+Shift+N"))
        self._doc_actions.append(self._act(m, "Переименовать модуль…",
                                           self.rename_module))
        self._doc_actions.append(self._act(m, "Удалить модуль…", self.delete_module))
        m.addSeparator()
        self._act(m, "Выход", self.close)

        m = mb.addMenu("Правка")
        self.menu_edit = m
        self._doc_actions.append(
            self._act(m, "Отменить", self.undo, shortcuts="Ctrl+Z"))
        self._doc_actions.append(
            self._act(m, "Повторить", self.redo,
                      shortcuts=["Ctrl+Y", "Ctrl+Shift+Z"]))
        m.addSeparator()
        self._doc_actions.append(
            self._act(m, "Дублировать", self.duplicate_selected,
                      shortcuts="Ctrl+D"))

        m = mb.addMenu("Вид")
        self.menu_view = m
        self._doc_actions.append(self._act(m, "Сетка вкл/выкл", self.toggle_grid))
        self._doc_actions.append(self._act(m, "Править сетку…", self._edit_user_grid))
        m.addSeparator()
        # Ctrl+= покрывает раскладки, где "+" требует Shift; Qt нормализует
        # оба варианта в один и тот же аккорд, поэтому оставляем оба для
        # надёжности на разных клавиатурах.
        self._doc_actions.append(
            self._act(m, "Приблизить", self._zoom_in,
                      shortcuts=["Ctrl+=", "Ctrl++"]))
        self._doc_actions.append(
            self._act(m, "Отдалить", self._zoom_out, shortcuts="Ctrl+-"))
        self._doc_actions.append(
            self._act(m, "Сброс масштаба", self._zoom_reset, shortcuts="Ctrl+0"))

    def _act(self, menu, text, slot, shortcuts=None):
        """Создать QAction и добавить в меню.

        shortcuts — строка или список строк в формате QKeySequence
        ("Ctrl+S", "Ctrl+Shift+S", …). Все действия принадлежат окну,
        поэтому их шорткаты работают при активном окне независимо от
        того, какой виджет сейчас в фокусе — это важно для undo/redo,
        когда пользователь правит значения в панели свойств.

        ВАЖНО: не назначайте тот же аккорд на второй QAction (например,
        на кнопку тулбара). Qt увидит два действия с одинаковым шорткатом
        в контексте WindowShortcut и напечатает «Ambiguous shortcut
        overload», не вызвав ни одного из них.
        """
        a = QAction(text, self)
        a.triggered.connect(slot)
        if shortcuts:
            if isinstance(shortcuts, str):
                shortcuts = [shortcuts]
            a.setShortcuts([QKeySequence(s) for s in shortcuts])
        menu.addAction(a)
        return a

    # ------------------------------------------------------------------
    # Верхний тулбар
    # ------------------------------------------------------------------

    def _build_top_toolbar(self):
        tb = self.addToolBar("Основное")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.top_toolbar = tb

        # ВАЖНО про шорткаты.
        # Все горячие клавиши (Ctrl+S/Z/Y/Shift+S/Shift+Z/Shift+N/N/O/D/=/-/0)
        # назначены РОВНО ОДИН РАЗ — на QAction пунктов меню в _build_menu().
        # Кнопки тулбара — это отдельные QAction на те же слоты, но БЕЗ
        # собственного setShortcut(): если назначить шорткат и там, и в
        # меню, Qt получит два действия с одинаковым аккордом в контексте
        # WindowShortcut и напечатает «Ambiguous shortcut overload», не
        # вызвав ни одного из них. Тултипы ниже просто напоминают
        # пользователю, какие клавиши реально работают.

        self.act_save_tb = QAction(_icon("save"), "Сохранить", self)
        self.act_save_tb.setToolTip("Сохранить библиотеку (Ctrl+S)")
        self.act_save_tb.triggered.connect(self.save_file)
        tb.addAction(self.act_save_tb)

        tb.addSeparator()

        self.act_undo_tb = QAction(_icon("undo"), "Отменить", self)
        self.act_undo_tb.setToolTip("Отменить последнее действие (Ctrl+Z)")
        self.act_undo_tb.triggered.connect(self.undo)
        tb.addAction(self.act_undo_tb)

        self.act_redo_tb = QAction(_icon("redo"), "Повторить", self)
        self.act_redo_tb.setToolTip(
            "Повторить отменённое действие (Ctrl+Y, Ctrl+Shift+Z)")
        self.act_redo_tb.triggered.connect(self.redo)
        tb.addAction(self.act_redo_tb)

        self.act_duplicate = QAction("Дублировать", self)
        self.act_duplicate.setToolTip("Дублировать выделение (Ctrl+D)")
        self.act_duplicate.triggered.connect(self.duplicate_selected)
        tb.addAction(self.act_duplicate)

        tb.addSeparator()

        self.act_add_modules_tb = QAction("Открыть модуль…", self)
        self.act_add_modules_tb.setToolTip(
            "Выбрать модули из библиотеки для открытия во вкладках")
        self.act_add_modules_tb.triggered.connect(self.add_modules)
        tb.addAction(self.act_add_modules_tb)

        tb.addSeparator()

        self.act_resetzoom_tb = tb.addAction("Сброс масштаба", self._zoom_reset)
        tb.addSeparator()
        tb.addWidget(QLabel(" Сетка: "))
        self._build_grid_combo(tb)

        self._doc_actions.extend([
            self.act_save_tb, self.act_undo_tb, self.act_redo_tb,
            self.act_duplicate,
            self.act_add_modules_tb, self.act_resetzoom_tb,
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

    def open_board_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть плату", "",
            "Платы KiCad (*.brd);;Все файлы (*)")
        if not path:
            return
        win = BoardWindow(self)
        if win.load_file(Path(path)):
            # Не даём окну погибнуть сборщиком мусора.
            self.board_windows.append(win)
            win.destroyed.connect(
                lambda _obj=None, w=win: self._remove_board_window(w))
            win.show()
            win.raise_()

    def _remove_board_window(self, win: BoardWindow):
        if win in self.board_windows:
            self.board_windows.remove(win)

    # ------------------------------------------------------------------
    # Файл
    # ------------------------------------------------------------------

    def new_library(self):
        name, ok = QInputDialog.getText(self, "Новая библиотека",
                                        "Имя файла (без .mod):", text="my_lib")
        if not ok:
            return
        name = name.strip()
        if not name:
            self._msg_info("Пустое имя",
                           "Имя библиотеки не может быть пустым.")
            return

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
            self._msg_critical("Ошибка",
                               f"Не удалось прочитать файл:\n{e}")
            return

        self.library = lib
        self.current_path = Path(path)
        if not self.library.file_name:
            self.library.file_name = Path(path).stem

        self._clear_tabs()
        self._update_state()
        self.statusBar().showMessage(
            f"Открыто: {path}  [модулей: {len(lib.modules)}]", 4000)

        if not lib.modules:
            self._msg_info("Пустая библиотека",
                           "В файле нет ни одного $MODULE. "
                           "Создайте модуль через «Файл → Создать модуль…».")
            return

        # Не открываем сразу всё: предлагаем выбрать модули для вкладок.
        self.add_modules()

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
            self._msg_critical("Ошибка",
                               f"Не удалось сохранить:\n{e}")
            return
        self.current_path = path
        self.library.file_name = path.stem
        self._update_title()
        self.statusBar().showMessage(f"Сохранено: {path}", 4000)

    # ------------------------------------------------------------------
    # Модули
    # ------------------------------------------------------------------

    def add_modules(self):
        """Открыть диалог выбора модулей из текущей библиотеки.

        Показывает только те модули, которых ещё нет во вкладках.
        """
        if self.library is None:
            return

        if not self.library.modules:
            self._msg_info("Нет модулей",
                           "В библиотеке нет ни одного модуля. Создайте "
                           "новый через «Файл → Создать модуль…».")
            return

        opened = {c.footprint.name for c in self._iter_canvases()
                  if c.footprint is not None}
        remaining = [fp for fp in self.library.modules
                     if fp.name not in opened]
        if not remaining:
            self._msg_info("Все модули открыты",
                           "Все модули этой библиотеки уже открыты "
                           "во вкладках.")
            return

        dlg = ModulePickerDialog(
            self.library.modules,
            exclude=opened,
            parent=self,
            title="Открыть модуль",
            subtitle="Выберите модуль.",
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        names = set(dlg.selected_names())
        for fp in self.library.modules:
            if fp.name in names:
                self._open_module(fp)

        self._update_state()
        self.statusBar().showMessage(
            f"Открыто модулей: {len(names)}", 3000)

    def add_module(self):
        """Создать новый пустой модуль и сразу открыть его во вкладке."""
        if self.library is None:
            return
        name, ok = QInputDialog.getText(self, "Новый модуль", "Имя:",
                                        text="Module")
        if not ok:
            return
        name = name.strip()
        if not name:
            self._msg_info("Пустое имя",
                           "Имя модуля не может быть пустым.")
            return
        fp = new_footprint(name=name)
        self.library.modules.append(fp)
        self._add_module_tab(fp)
        self._update_state()
        self._update_title()
        self.statusBar().showMessage(f"Создан модуль «{name}»", 3000)

    def rename_module(self):
        if self.library is None:
            return
        c = self._current_canvas()
        if c is None or c.footprint is None:
            return
        name, ok = QInputDialog.getText(self, "Переименовать модуль",
                                        "Новое имя:", text=c.footprint.name)
        if not ok:
            return
        name = name.strip()
        if not name:
            self._msg_info("Пустое имя",
                           "Имя модуля не может быть пустым.")
            return
        c.footprint.name = name
        idx = self.tabs.currentIndex()
        self.tabs.setTabText(idx, name)
        self._update_title()

    def delete_module(self):
        """Удалить модуль текущей вкладки из библиотеки.

        Модуль убирается из `library.modules` и его вкладка закрывается.
        Действие необратимое (нет undo на уровне библиотеки), поэтому
        сначала спрашиваем подтверждение. Если открыты другие вкладки
        с другими модулями — они не затрагиваются.
        """
        if self.library is None:
            return
        c = self._current_canvas()
        if c is None or c.footprint is None:
            self._msg_info("Нет активного модуля",
                           "Откройте модуль во вкладке — его можно удалить.")
            return

        fp = c.footprint
        name = fp.name

        if not self._msg_ask(
                "Удалить модуль",
                f"Удалить модуль «{name}» из библиотеки?\n\n"
                "Он будет убран из файла при следующем сохранении. "
                "Отменить это действие нельзя.",
                yes_text="Удалить", no_text="Отмена"):
            return

        # Убрать из модели библиотеки
        if fp in self.library.modules:
            self.library.modules.remove(fp)

        # Закрыть вкладку (одну, с этим конкретным модулем)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if getattr(w, "footprint", None) is fp:
                self.tabs.removeTab(i)
                w.deleteLater()
                break

        self._update_state()
        self.statusBar().showMessage(
            f"Модуль «{name}» удалён из библиотеки", 4000)

    # ------------------------------------------------------------------
    # Правка
    # ------------------------------------------------------------------

    def undo(self):
        s = self._current_stack()
        if s is not None: s.undo()

    def redo(self):
        s = self._current_stack()
        if s is not None: s.redo()

    def duplicate_selected(self):
        """Ctrl+D: продублировать выделенное на активной вкладке."""
        c = self._current_canvas()
        if c is not None:
            c.duplicate_selected()

    # ------------------------------------------------------------------
    # Вид
    # ------------------------------------------------------------------

    def toggle_grid(self):
        """Сетка вкл/выкл. При выключении сетки привязка снимается."""
        self.grid_visible = not self.grid_visible
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