"""KiForge — точка входа."""
import sys
from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow
from ui.theme import DARK_QSS


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("KiForge")
    app.setOrganizationName("KiForge")
    app.setStyleSheet(DARK_QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()