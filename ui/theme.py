"""Тёмно-синяя тема KiForge. Применяется через QApplication.setStyleSheet."""
from __future__ import annotations

from pathlib import Path

# ─── Токены ─────────────────────────────────────────────────────────────────
BG           = "#0B1220"
SURFACE      = "#131A2A"
SURFACE_2    = "#1A2337"
BORDER       = "#1E2A44"
BORDER_2     = "#2A3550"

TEXT         = "#E6ECF5"
TEXT_MUTED   = "#8A93A8"
TEXT_DISABLED= "#5A6478"

PRIMARY      = "#3E6FE0"
PRIMARY_HOV  = "#4F7FE8"
PRIMARY_PRES = "#2F57B8"


# ─── Пути к иконкам ────────────────────────────────────────────────────────
# ВАЖНО: оставляем as_posix() — этот формат URL работает с текущей связкой
# Qt + QSS на нашей конфигурации. QUrl.fromLocalFile в прошлый раз ломал
# загрузку галочки (Qt склеивал путь дважды), поэтому не трогаем.
_ICONS_DIR = Path(__file__).parent / "icons"
_CHECK_URL = (_ICONS_DIR / "check.svg").as_posix()


DARK_QSS = f"""
/* ─── Основа ──────────────────────────────────────────────────────────── */
QMainWindow, QDialog, QWidget#welcomeRoot {{
    background-color: {BG};
}}
QWidget {{
    color: {TEXT};
    font-family: 'Segoe UI', 'Inter', 'Sans', sans-serif;
    font-size: 13px;
}}

/* ─── Меню ────────────────────────────────────────────────────────────── */
QMenuBar {{
    background-color: {SURFACE};
    color: {TEXT};
    border-bottom: 1px solid {BORDER};
    padding: 2px 4px;
}}
QMenuBar::item {{
    padding: 6px 12px;
    border-radius: 4px;
    background: transparent;
}}
QMenuBar::item:selected {{ background-color: {SURFACE_2}; }}

QMenu {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background-color: {SURFACE_2}; }}
QMenu::item:disabled {{ color: {TEXT_DISABLED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

/* ─── Тулбары ─────────────────────────────────────────────────────────── */
QToolBar {{
    background-color: {SURFACE};
    border: none;
    border-bottom: 1px solid {BORDER};
    spacing: 4px;
    padding: 4px 8px;
}}
QToolBar[orientation="vertical"] {{
    border-bottom: none;
    border-right: 1px solid {BORDER};
}}
/* Разделители тулбара — цвета BORDER, а не белые (фикс Win10). */
QToolBar::separator {{
    background-color: {BORDER};
    width: 1px;
    margin: 6px 4px;
}}
QToolBar[orientation="vertical"]::separator {{
    background-color: {BORDER};
    height: 1px;
    margin: 4px 6px;
    width: auto;
}}
QToolButton {{
    background: transparent;
    color: {TEXT};
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 6px 10px;
}}
QToolButton:hover {{
    background-color: {SURFACE_2};
    border-color: {BORDER_2};
}}
QToolButton:pressed, QToolButton:checked {{
    background-color: {PRIMARY};
    color: #FFFFFF;
    border-color: {PRIMARY};
}}
QToolButton:disabled {{ color: {TEXT_DISABLED}; }}

/* ─── Dock-панели ─────────────────────────────────────────────────────── */
QDockWidget {{ color: {TEXT}; }}
QDockWidget::title {{
    background-color: {SURFACE};
    padding: 6px 10px;
    border-bottom: 1px solid {BORDER};
    text-align: left;
}}
/* Внутренний контейнер QDockWidget — иначе на Win10 он берёт системный
   светлый фон, и содержимое панели сливается с ним. */
QDockWidget > QWidget {{
    background-color: {SURFACE};
}}

/* ─── Скролл-области (в т.ч. viewport) ─────────────────────────────────── */
/* Ключевой фикс: QScrollArea и её viewport должны быть прозрачными, чтобы
   показывать фон родителя, а не системный белый. Именно из-за этого
   менеджер слоёв на Win10 был белым. */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QAbstractScrollArea {{
    background: transparent;
}}
QAbstractScrollArea::corner {{
    background: transparent;
    border: none;
}}

/* ─── Статус-бар ──────────────────────────────────────────────────────── */
QStatusBar {{
    background-color: {SURFACE};
    color: {TEXT_MUTED};
    border-top: 1px solid {BORDER};
}}

/* ─── Кнопки ──────────────────────────────────────────────────────────── */
QPushButton {{
    background-color: {SURFACE_2};
    color: {TEXT};
    border: 1px solid {BORDER_2};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {PRIMARY}; }}
QPushButton:pressed {{ background-color: {PRIMARY}; border-color: {PRIMARY}; }}
QPushButton:disabled {{ color: {TEXT_DISABLED}; }}

/* ─── Поля ввода ──────────────────────────────────────────────────────── */
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER_2};
    border-radius: 4px;
    padding: 4px 8px;
    selection-background-color: {PRIMARY};
    selection-color: #FFFFFF;
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {PRIMARY};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {SURFACE_2};
    outline: none;
}}

/* ─── Чекбоксы ────────────────────────────────────────────────────────── */
/* НЕ ТРОГАЕМ. Именно эта секция работает в текущей конфигурации Qt.
   Любые манипуляции с QUrl.fromLocalFile, Fusion-стилем и т.п. её ломают. */
QCheckBox {{ color: {TEXT}; spacing: 8px; padding: 2px 0; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER_2};
    border-radius: 4px;
    background: {SURFACE};
}}
QCheckBox::indicator:hover {{
    border-color: {PRIMARY};
}}
QCheckBox::indicator:checked {{
    background-color: {PRIMARY};
    border-color: {PRIMARY};
    image: url("{_CHECK_URL}");
}}
QCheckBox::indicator:checked:hover {{
    background-color: {PRIMARY_HOV};
    border-color: {PRIMARY_HOV};
}}

/* ─── Табы модулей ────────────────────────────────────────────────────── */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    background: {BG};
    top: -1px;
}}
QTabBar::tab {{
    background: {SURFACE};
    color: {TEXT_MUTED};
    padding: 7px 16px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
    min-width: 80px;
}}
QTabBar::tab:selected {{
    background: {SURFACE_2};
    color: {TEXT};
    border-bottom: 2px solid {PRIMARY};
}}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

/* ─── Скроллбары ──────────────────────────────────────────────────────── */
QScrollBar:vertical {{ width: 8px; background: transparent; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {BORDER_2}; border-radius: 4px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {PRIMARY}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0; background: transparent;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{ height: 8px; background: transparent; }}
QScrollBar::handle:horizontal {{
    background: {BORDER_2}; border-radius: 4px; min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {PRIMARY}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0; background: transparent;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

/* ─── Канва ───────────────────────────────────────────────────────────── */
QGraphicsView {{ border: none; background: {BG}; }}

/* ─── Метки ───────────────────────────────────────────────────────────── */
QLabel {{ color: {TEXT}; background: transparent; }}

/* ─── Welcome-экран ───────────────────────────────────────────────────── */
QWidget#welcomeRoot {{ background-color: {BG}; }}

QFrame#welcomeCard {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}

QLabel#welcomeTitle {{
    color: {TEXT};
    font-size: 44px;
    font-weight: 700;
    letter-spacing: 3px;
}}
QLabel#welcomeSub {{
    color: {TEXT_MUTED};
    font-size: 14px;
}}
QLabel#welcomeHint {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}

QPushButton#welcomeBtnPrimary {{
    background-color: {PRIMARY};
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    padding: 10px 24px;
}}
QPushButton#welcomeBtnPrimary:hover  {{ background-color: {PRIMARY_HOV}; }}
QPushButton#welcomeBtnPrimary:pressed{{ background-color: {PRIMARY_PRES}; }}

QPushButton#welcomeBtnSecondary {{
    background-color: transparent;
    color: {TEXT};
    border: 1px solid {BORDER_2};
    border-radius: 8px;
    font-size: 14px;
    padding: 10px 24px;
}}
QPushButton#welcomeBtnSecondary:hover {{
    background-color: {SURFACE_2};
    border-color: {PRIMARY};
}}

/* ─── Прочее ──────────────────────────────────────────────────────────── */
QToolTip {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER_2};
    padding: 4px 6px;
}}
"""