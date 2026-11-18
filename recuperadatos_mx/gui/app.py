"""Ventana principal de RecuperaDatos-MX."""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QStackedWidget,
                               QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget)

from .. import __app_name__, __version__
from . import common
from .icons import icon
from .theme import DARK, LIGHT, qss

ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")

NAV = [
    ("inicio", "Inicio", "home", "Elegir disco, partición o imagen y tipo de escaneo"),
    ("resultados", "Resultados", "results", "Archivos encontrados: filtrar, previsualizar y recuperar"),
    ("particiones", "Particiones", "partition", "Ver tablas de particiones, buscar y restaurar particiones perdidas"),
    ("raid", "RAID virtual", "raid", "Reconstruir arreglos RAID 0/1/1E/4/5/6/10 y JBOD a partir de sus discos"),
    ("imagen", "Imagen y clonado", "image", "Copias byte a byte de discos con sectores dañados"),
    ("editor", "Editor de disco", "hex", "Editor hexadecimal de sectores con plantillas de estructuras"),
    ("salud", "Salud S.M.A.R.T.", "health", "Estado de salud, temperatura y alertas de discos y SSD"),
    ("reparar", "Reparar archivos", "repair", "Reparar fotos JPEG, PDF y documentos de Office/ZIP dañados"),
    ("boveda", "Bóveda", "vault", "Protección preventiva de carpetas importantes"),
    ("red", "Por red", "network", "Recuperar datos de otra computadora de la red"),
    ("moviles", "Teléfonos", "phone", "iPhone/iPad (respaldos, incluso cifrados), Android por USB y mensajes borrados"),
    ("arranque", "Medio de arranque", "usb", "Crear una memoria USB de arranque para equipos que no inician"),
    ("firmas", "Tipos de archivo", "signature", "Firmas del escaneo profundo y firmas personalizadas"),
    ("acerca", "Acerca de", "about", "Versión, licencia GPL y autor"),
]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("RecuperaDatos-MX", "RecuperaDatos-MX")
        self.dark = self.settings.value("tema_oscuro", True, type=bool)
        self.apply_theme()
        self.setWindowTitle(f"{__app_name__} {__version__}")
        self.setWindowIcon(QIcon(os.path.join(ASSETS, "logo.png")))
        self.resize(1360, 860)
        self.setMinimumSize(980, 640)
        root = QWidget()
        root.setObjectName("Root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(236)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 16, 14, 14)
        sl.setSpacing(2)
        brand = QHBoxLayout()
        lg = QLabel()
        lg.setPixmap(QPixmap(os.path.join(ASSETS, "logo.png")).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        brand.addWidget(lg)
        bt = QVBoxLayout()
        n = QLabel("RecuperaDatos-MX")
        n.setObjectName("AppName")
        s = QLabel("Recuperación libre · GPL-3")
        s.setObjectName("AppSub")
        bt.addWidget(n)
        bt.addWidget(s)
        brand.addLayout(bt)
        sl.addLayout(brand)
        sl.addSpacing(14)
        self.stack = QStackedWidget()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.nav_buttons = {}
        from .pages import (AboutPage, HexPage, HomePage, ImagingPage, NetworkPage, PartitionsPage, RaidPage,
                            RepairPage, SignaturesPage, SmartPage, VaultPage)
        from .results import ResultsPage
        from .pages2 import BootMediaPage, MobilePage
        self.signatures_page = SignaturesPage(self)
        self.results = ResultsPage(self)
        self.partitions_page = PartitionsPage(self)
        self.hex_page = HexPage(self)
        self.home = HomePage(self)
        self.pages = {"inicio": self.home, "resultados": self.results, "particiones": self.partitions_page,
                      "raid": RaidPage(self), "imagen": ImagingPage(self), "editor": self.hex_page,
                      "salud": SmartPage(self), "reparar": RepairPage(self), "boveda": VaultPage(self),
                      "red": NetworkPage(self), "moviles": MobilePage(self), "arranque": BootMediaPage(self),
                      "firmas": self.signatures_page, "acerca": AboutPage(self)}
        for key, text, ic, tip in NAV:
            b = QToolButton()
            b.setObjectName("Nav")
            b.setText("  " + text)
            b.setIcon(icon(ic, common.colors()["icon"]))
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setCheckable(True)
            b.setToolTip(f"<b>{text}</b><br>{tip}")
            b.setMinimumWidth(200)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.go(k))
            self.group.addButton(b)
            sl.addWidget(b)
            self.nav_buttons[key] = b
            self.stack.addWidget(self.pages[key])
        sl.addStretch(1)
        tb = QToolButton()
        tb.setObjectName("Nav")
        tb.setText("  Tema claro / oscuro")
        tb.setIcon(icon("theme", common.colors()["icon"]))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setToolTip("Cambiar entre el tema oscuro y el claro")
        tb.clicked.connect(self.toggle_theme)
        sl.addWidget(tb)
        h.addWidget(side)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Listo. Consejo: no instale ni guarde nada en la unidad que desea recuperar.")
        self.tray = QSystemTrayIcon(QIcon(os.path.join(ASSETS, "logo.png")), self)
        self.tray.setToolTip(__app_name__)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.go("inicio")

    def apply_theme(self) -> None:
        common.STATE["colors"] = DARK if self.dark else LIGHT
        QApplication.instance().setStyleSheet(qss(common.colors()))

    def toggle_theme(self) -> None:
        self.dark = not self.dark
        self.settings.setValue("tema_oscuro", self.dark)
        self.apply_theme()
        self.status("El tema se aplicará por completo (iconos incluidos) al reiniciar la aplicación")
        for w in self.findChildren(QWidget):
            w.update()

    def go(self, key: str) -> None:
        self.stack.setCurrentWidget(self.pages[key])
        self.nav_buttons[key].setChecked(True)

    def status(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 15000)

    def notify(self, title: str, msg: str) -> None:
        if self.tray.isVisible():
            self.tray.showMessage(title, msg, QSystemTrayIcon.Warning)

    def signatures(self):
        return self.signatures_page.enabled()

    def register_image(self, path: str) -> None:
        for page in self.pages.values():
            for picker in page.findChildren(common.SourcePicker):
                if all(p != path for p, _ in picker.extra):
                    picker.extra.append((path, f"🗂 {os.path.basename(path)}  ({path})"))
                    picker.refresh()


def main(argv=None) -> None:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv or sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("RecuperaDatos-MX")
    app.setWindowIcon(QIcon(os.path.join(ASSETS, "logo.png")))
    if sys.platform.startswith("linux"):
        app.setDesktopFileName("recuperadatos-mx")
    w = MainWindow()
    geo = app.primaryScreen().availableGeometry() if app.primaryScreen() else None
    if geo is not None and (geo.width() < 1440 or geo.height() < 900):
        w.showMaximized()  # pantallas pequeñas (laptops, modo live): usar todo el espacio
    else:
        w.show()
    for a in (argv or sys.argv)[1:]:
        if os.path.isfile(a):
            w.home.add_image(a)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
