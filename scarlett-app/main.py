"""
Scarlett Control — Linux Desktop App
A native PyQt6 desktop application for Focusrite Scarlett interfaces.
No browser, no localhost — just a proper app window.
"""

import sys
import os
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtCore import QUrl, Qt, QSize
from PyQt6.QtGui import QIcon

from backend import ScarlettBackend


class ScarlettWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scarlett Control")
        self.setMinimumSize(QSize(900, 680))
        self.resize(1100, 760)
        self.setWindowIcon(self._make_icon())

        # ── Web view ───────────────────────────────────────────────────────
        self.view = QWebEngineView()

        # Allow the web page to call Python methods
        self.channel = QWebChannel()
        self.backend = ScarlettBackend()
        self.channel.registerObject("backend", self.backend)
        self.view.page().setWebChannel(self.channel)

        # Settings
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)

        # Load the UI
        ui_path = os.path.join(os.path.dirname(__file__), "ui", "index.html")
        self.view.load(QUrl.fromLocalFile(os.path.abspath(ui_path)))

        # ── Layout ─────────────────────────────────────────────────────────
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.setCentralWidget(container)

    def _make_icon(self):
        """Create a simple coloured icon from an SVG string."""
        from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont
        from PyQt6.QtCore import Qt
        px = QPixmap(64, 64)
        px.fill(Qt.GlobalColor.transparent)
        painter = QPainter(px)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#e8472a"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, 64, 64, 14, 14)
        painter.setPen(QColor("white"))
        font = QFont("Arial", 32, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, "S")
        painter.end()
        return QIcon(px)


def main():
    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Scarlett Control")
    app.setOrganizationName("scarlett-linux")
    app.setStyle("Fusion")

    window = ScarlettWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
