"""Páginas nuevas de la versión 1.1: contraseñas de volúmenes cifrados, móviles
(iPhone, Android, mensajes SQLite borrados) y medio de arranque."""
from __future__ import annotations

import os
import tempfile
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QProgressBar, QTabWidget, QVBoxLayout, QWidget)

from ..core.device import human
from .common import Worker, btn, confirm_destructive, error, heading, info
from .pages import page_layout, set_row, table


class PasswordDialog(QDialog):
    """Pide contraseñas, claves de recuperación de BitLocker (48 dígitos) o archivos .BEK."""

    def __init__(self, parent, volumes: List[Tuple[str, str]]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Volúmenes cifrados")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lst = "".join(f"<li><b>{n}</b> — {k}</li>" for n, k in volumes)
        t = QLabel(f"Se encontraron volúmenes cifrados:<ul>{lst}</ul>Escriba la contraseña o, para BitLocker, la "
                   "clave de recuperación de 48 dígitos (la encuentra en su cuenta Microsoft: "
                   "aka.ms/myrecoverykey). Puede poner varias, una por línea. Sin contraseña se escanean solo "
                   "los volúmenes sin cifrar y el resto se analiza por firmas.")
        t.setWordWrap(True)
        lay.addWidget(t)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("Contraseña\n123456-123456-123456-123456-123456-123456-123456-123456")
        self.edit.setToolTip("Una contraseña o clave de recuperación por línea; se prueban todas")
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        self.bek_label = QLabel("")
        self.bek_label.setObjectName("Muted")
        row.addWidget(btn("Archivo .BEK…", "Clave de inicio de BitLocker guardada en una memoria USB (archivo .BEK)",
                          "open", self.pick_bek))
        row.addWidget(self.bek_label, 1)
        lay.addLayout(row)
        self.bek: List[bytes] = []
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Desbloquear y escanear")
        bb.button(QDialogButtonBox.Ok).setToolTip("Probar las claves y comenzar el escaneo")
        bb.button(QDialogButtonBox.Cancel).setText("Escanear sin descifrar")
        bb.button(QDialogButtonBox.Cancel).setToolTip("Continuar sin contraseña")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def pick_bek(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Archivo de clave BitLocker", "", "Clave BitLocker (*.BEK *.bek);;Todos (*)")
        if p:
            self.bek.append(open(p, "rb").read())
            self.bek_label.setText(os.path.basename(p))

    def passwords(self) -> List[str]:
        return [l.strip() for l in self.edit.toPlainText().splitlines() if l.strip()]


# =========================================================================== #
class MobilePage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        lay = page_layout(self)
        lay.addWidget(heading("Teléfonos y tabletas", "Recupere fotos, videos, mensajes y chats de iPhone/iPad "
                                                     "(desde sus respaldos, incluso cifrados) y de Android (por USB "
                                                     "con depuración activada), y rescate mensajes borrados de las "
                                                     "bases de datos SQLite."))
        tabs = QTabWidget()
        tabs.addTab(self._iphone(), "iPhone / iPad")
        tabs.addTab(self._android(), "Android")
        tabs.addTab(self._sqlite(), "Mensajes borrados (SQLite)")
        lay.addWidget(tabs, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        lay.addWidget(self.log)

    # ---------------------------- iPhone --------------------------------- #
    def _iphone(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        self.ibk = table(["Dispositivo", "Modelo", "iOS", "Fecha", "Cifrado", "Carpeta"])
        self.ibk.setToolTip("Respaldos encontrados en esta computadora (Finder / iTunes / libimobiledevice)")
        l.addWidget(self.ibk, 1)
        row = QHBoxLayout()
        row.addWidget(btn("Buscar respaldos", "Buscar respaldos de iPhone/iPad en las ubicaciones de Finder e iTunes",
                          "refresh", self.load_backups))
        row.addWidget(btn("Abrir carpeta…", "Agregar un respaldo copiado de otra computadora", "open", self.add_backup))
        row.addWidget(btn("Crear respaldo del iPhone", "Respaldar el iPhone conectado por USB (requiere "
                          "libimobiledevice: idevicebackup2)", "save", self.make_backup))
        row.addStretch(1)
        lay2 = QHBoxLayout()
        self.ipw = QLineEdit()
        self.ipw.setEchoMode(QLineEdit.Password)
        self.ipw.setPlaceholderText("Contraseña del respaldo (si está cifrado)")
        self.ipw.setToolTip("La contraseña que se puso al activar «Cifrar respaldo local» en Finder/iTunes")
        lay2.addWidget(self.ipw, 1)
        self.icat = QComboBox()
        self.icat.addItem("Todo el respaldo", ("", ""))
        from ..core.mobile.iphone import INTERESTING
        for k, v in INTERESTING.items():
            self.icat.addItem(k, v)
        self.icat.setToolTip("Qué extraer del respaldo")
        lay2.addWidget(self.icat)
        lay2.addWidget(btn("Extraer", "Extraer los archivos con sus nombres y carpetas originales; las bases de "
                           "mensajes se pueden analizar después en «Mensajes borrados»", "recover",
                           self.extract_iphone, "Primary"))
        l.addLayout(row)
        l.addLayout(lay2)
        self.extra_backups: List[str] = []
        self.backups = []
        return w

    def load_backups(self) -> None:
        from ..core.mobile.iphone import list_backups
        self.backups = list_backups(self.extra_backups)
        self.ibk.setRowCount(len(self.backups))
        for r, b in enumerate(self.backups):
            set_row(self.ibk, r, [b["nombre"], b["modelo"], b["ios"], b["fecha"][:19], "Sí" if b["cifrado"] else "No",
                                  b["ruta"]])
        if not self.backups:
            self.log.appendPlainText("No se encontraron respaldos. Cree uno con Finder (macOS) o iTunes (Windows).")

    def add_backup(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "Carpeta del respaldo")
        if p:
            self.extra_backups.append(p)
            self.load_backups()

    def make_backup(self) -> None:
        from ..core.mobile.iphone import make_backup
        dest = QFileDialog.getExistingDirectory(self, "Dónde guardar el respaldo")
        if not dest:
            return
        self.w = Worker(lambda w: make_backup(dest), self)
        self.w.done.connect(lambda r: (self.log.appendPlainText(r[1]), self.extra_backups.append(dest),
                                       self.load_backups()))
        self.w.start()
        self.log.appendPlainText("Creando respaldo… desbloquee el iPhone y acepte «Confiar en esta computadora».")

    def extract_iphone(self) -> None:
        from ..core.mobile.iphone import Backup
        r = self.ibk.currentRow()
        if r < 0 or r >= len(self.backups):
            info(self, "Seleccione un respaldo.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Carpeta destino")
        if not dest:
            return
        bk, pw = self.backups[r], self.ipw.text()
        dom, path = self.icat.currentData()

        def job(w):
            b = Backup(bk["ruta"], pw)
            return b.extract(dest, dom, path, progress=lambda d, t, n: w.progress.emit(n, d, t))

        self._run(job, lambda n: (self.log.appendPlainText(f"{n} archivos extraídos en {dest}"),
                                  QDesktopServices.openUrl(QUrl.fromLocalFile(dest))))

    # ---------------------------- Android -------------------------------- #
    def _android(self) -> QWidget:
        from ..core.mobile.android import FOLDERS
        w = QWidget()
        l = QVBoxLayout(w)
        row = QHBoxLayout()
        self.adev = QComboBox()
        self.adev.setToolTip("Teléfono Android conectado por USB con la depuración USB activada")
        row.addWidget(self.adev, 1)
        row.addWidget(btn("Detectar", "Buscar teléfonos conectados (acepte el aviso «Permitir depuración USB» en el "
                          "teléfono)", "refresh", self.load_android))
        l.addLayout(row)
        self.afold = QListWidget()
        self.afold.setToolTip("Carpetas a copiar del teléfono")
        for k in FOLDERS:
            it = QListWidgetItem(k)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if k in ("Cámara (DCIM)", "WhatsApp", "Miniaturas (.thumbnails)") else Qt.Unchecked)
            self.afold.addItem(it)
        l.addWidget(self.afold, 1)
        row = QHBoxLayout()
        row.addWidget(btn("Recuperar papelera", "Rescatar las fotos y archivos en la papelera de Android 11+ (archivos "
                          "«.trashed», se eliminan solos a los 30 días)", "recover", self.android_trash, "Primary"))
        row.addWidget(btn("Copiar carpetas", "Copiar las carpetas marcadas a la computadora (fotos, WhatsApp, "
                          "miniaturas de fotos borradas…)", "save", self.android_pull))
        row.addWidget(btn("Imagen de partición (root)", "En teléfonos con root, copiar byte a byte una partición "
                          "(p. ej. userdata o la tarjeta interna) para escanearla con el escaneo profundo", "image",
                          self.android_image))
        row.addStretch(1)
        l.addLayout(row)
        return w

    def load_android(self) -> None:
        from ..core.mobile import android
        try:
            devs = android.devices()
        except FileNotFoundError as exc:
            error(self, str(exc))
            return
        self.adev.clear()
        for s, d in devs:
            self.adev.addItem(d, s)
        if not devs:
            info(self, "No hay teléfonos. Active Opciones de desarrollador → Depuración USB y conecte el cable.")

    def _serial(self) -> Optional[str]:
        s = self.adev.currentData()
        if not s:
            info(self, "Conecte un teléfono y pulse «Detectar».")
        return s

    def android_trash(self) -> None:
        from ..core.mobile import android
        s = self._serial()
        dest = s and QFileDialog.getExistingDirectory(self, "Carpeta destino")
        if s and dest:
            self._run(lambda w: android.recover_trash(s, dest, lambda d, t, n: w.progress.emit(n, d, t)),
                      lambda n: self.log.appendPlainText(f"{n} archivos recuperados de la papelera en {dest}"))

    def android_pull(self) -> None:
        from ..core.mobile import android
        s = self._serial()
        names = [self.afold.item(i).text() for i in range(self.afold.count())
                 if self.afold.item(i).checkState() == Qt.Checked]
        dest = s and names and QFileDialog.getExistingDirectory(self, "Carpeta destino")
        if s and dest:
            self._run(lambda w: android.pull_folders(s, names, dest, lambda d, t, n: w.progress.emit(n, d, t)),
                      lambda done: self.log.appendPlainText(f"Copiadas: {', '.join(done)} → {dest}"))

    def android_image(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        from ..core.mobile import android
        s = self._serial()
        if not s:
            return
        if not android.is_rooted(s):
            error(self, "Este teléfono no tiene root. Sin root Android no permite leer las particiones; use "
                        "«Recuperar papelera» y «Copiar carpetas», o saque la tarjeta SD y escanéela con un lector.")
            return
        parts = android.partitions(s)
        items = [f"{p} ({human(z)})" for p, z in parts]
        choice, ok = QInputDialog.getItem(self, "Partición", "Partición a copiar:", items, 0, False)
        if not ok:
            return
        block = parts[items.index(choice)][0]
        dest, _ = QFileDialog.getSaveFileName(self, "Guardar imagen", os.path.basename(block) + ".img")
        if dest:
            self._run(lambda w: android.image_partition(s, block, dest, lambda d, t, n: w.progress.emit(n, d, t),
                                                        w.cancelled),
                      lambda n: (self.log.appendPlainText(f"Imagen de {human(n)} guardada; se agregó a Inicio."),
                                 self.main.home.add_image(dest)))

    # ---------------------------- SQLite --------------------------------- #
    def _sqlite(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        t = QLabel("Abra una base de mensajes (sms.db de iPhone, mmssms.db de Android, msgstore.db o "
                   "ChatStorage.sqlite de WhatsApp, NoteStore.sqlite, CallHistory.storedata, historial de "
                   "navegadores…) y se buscarán los registros borrados que SQLite dejó en el archivo.")
        t.setWordWrap(True)
        t.setObjectName("Muted")
        l.addWidget(t)
        row = QHBoxLayout()
        self.sql_path = QLineEdit()
        self.sql_path.setToolTip("Base de datos SQLite a analizar (nunca se modifica)")
        row.addWidget(self.sql_path, 1)
        row.addWidget(btn("Elegir…", "Seleccionar la base de datos", "open", self.pick_sqlite))
        row.addWidget(btn("Buscar borrados", "Analizar bloques libres, espacio no asignado y páginas libres",
                          "find", self.run_sqlite, "Primary"))
        row.addWidget(btn("Exportar CSV", "Guardar los registros recuperados en archivos CSV por tabla", "export",
                          self.export_sqlite))
        l.addLayout(row)
        self.sql_table = table(["Tabla", "Ubicación", "Valores"])
        l.addWidget(self.sql_table, 1)
        return w

    def pick_sqlite(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Base de datos", "", "SQLite (*.db *.sqlite *.sqlitedb *.storedata "
                                                                       "*.sqlite3);;Todos (*)")
        if p:
            self.sql_path.setText(p)

    def run_sqlite(self) -> None:
        from ..core.mobile.sqlite_recover import SQLiteRecovery
        p = self.sql_path.text().strip()
        if not p:
            return
        try:
            rows = SQLiteRecovery(p).recover()
        except Exception as exc:
            error(self, str(exc))
            return
        self.sql_table.setRowCount(len(rows))
        for r, (t, where, vals) in enumerate(rows):
            set_row(self.sql_table, r, [t, where, " | ".join("" if v is None else (f"<{len(v)} bytes>" if
                                                                                    isinstance(v, bytes) else str(v))
                                                           for v in vals)])
        self.log.appendPlainText(f"{len(rows)} registros borrados encontrados en {os.path.basename(p)}")

    def export_sqlite(self) -> None:
        from ..core.mobile.sqlite_recover import SQLiteRecovery
        p = self.sql_path.text().strip()
        dest = p and QFileDialog.getExistingDirectory(self, "Carpeta destino")
        if dest:
            files = SQLiteRecovery(p).export(dest)
            self.log.appendPlainText("Exportado: " + ", ".join(os.path.basename(f) for f in files))

    # -------------------------------------------------------------------- #
    def _run(self, fn, done) -> None:
        self.w = Worker(fn, self)
        self.w.progress.connect(lambda s, d, t: (self.bar.setValue(int(1000 * d / max(1, t))),
                                                 self.main.status(str(s))))
        self.w.done.connect(lambda r: (self.bar.setValue(1000), done(r)))
        self.w.failed.connect(lambda m: error(self, m))
        self.w.start()


# =========================================================================== #
class BootMediaPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        lay = page_layout(self)
        lay.addWidget(heading("Medio de arranque", "Cree una memoria USB de arranque con RecuperaDatos-MX Live "
                                                   "(Debian + esta aplicación, para BIOS y UEFI) para recuperar "
                                                   "datos de computadoras cuyo sistema ya no inicia. Todo el "
                                                   "contenido de la memoria USB se borrará."))
        f = QFormLayout()
        row = QHBoxLayout()
        self.iso = QLineEdit()
        self.iso.setPlaceholderText("RecuperaDatos-MX-…-live-amd64.iso")
        self.iso.setToolTip("Imagen ISO a grabar")
        row.addWidget(self.iso, 1)
        row.addWidget(btn("Descargar ISO", "Descargar la versión más reciente de RecuperaDatos-MX Live desde GitHub",
                          "recover", self.download, "Primary"))
        row.addWidget(btn("Elegir ISO…", "Usar un archivo ISO ya descargado (también sirve para otros ISO híbridos)",
                          "open", self.pick_iso))
        f.addRow("Imagen:", row)
        row2 = QHBoxLayout()
        self.usb = QComboBox()
        self.usb.setToolTip("Memoria USB destino (solo se muestran unidades extraíbles)")
        row2.addWidget(self.usb, 1)
        row2.addWidget(btn("Actualizar", "Volver a detectar memorias USB", "refresh", self.refresh))
        f.addRow("Memoria USB:", row2)
        self.verify = QCheckBox("Verificar la grabación (SHA-256)")
        self.verify.setChecked(True)
        self.verify.setToolTip("Leer de nuevo la memoria y comparar con el ISO")
        f.addRow(self.verify)
        lay.addLayout(f)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.stat = QLabel("")
        self.stat.setObjectName("Muted")
        lay.addWidget(self.stat)
        steps = QLabel("<b>Cómo usarla:</b> conecte la memoria en la computadora dañada, enciéndala pulsando la tecla "
                       "del menú de arranque (F12, F11, F9, Esc u Opción ⌥ en Mac Intel) y elija la memoria USB. "
                       "RecuperaDatos-MX se abre solo con acceso a todos los discos. Guarde lo recuperado en otra "
                       "memoria o disco externo. En equipos con Arranque seguro (Secure Boot) puede ser necesario "
                       "desactivarlo en la BIOS/UEFI.")
        steps.setWordWrap(True)
        lay.addWidget(steps)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn("Grabar en USB", "Borrar la memoria USB y grabar el ISO de arranque", "play", self.write,
                          "Danger"))
        lay.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        from ..core.bootmedia import usb_targets
        self.usb.clear()
        for d in usb_targets():
            self.usb.addItem(f"🔌 {d.model or d.name} — {human(d.size)} ({d.path})", d.path)

    def pick_iso(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Imagen ISO", "", "ISO (*.iso *.img)")
        if p:
            self.iso.setText(p)

    def download(self) -> None:
        from ..core import bootmedia

        def job(w):
            meta = bootmedia.latest_iso()
            if not meta:
                raise RuntimeError("Aún no hay un ISO publicado en GitHub")
            dest = os.path.join(os.path.expanduser("~"), "Descargas" if os.path.isdir(
                os.path.expanduser("~/Descargas")) else "Downloads", meta["nombre"])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            return bootmedia.download(meta["url"], dest, lambda d, t: w.progress.emit("Descargando", d, t),
                                      w.cancelled)

        self.w = Worker(job, self)
        self.w.progress.connect(lambda s, d, t: (self.bar.setValue(int(1000 * d / max(1, t))),
                                                 self.stat.setText(f"{s}: {human(d)} de {human(t)}")))
        self.w.done.connect(lambda p: (self.iso.setText(p), self.stat.setText(f"Descargado: {p}")))
        self.w.failed.connect(lambda m: error(self, m))
        self.w.start()

    def write(self) -> None:
        from ..core import bootmedia
        iso, dev = self.iso.text().strip(), self.usb.currentData()
        if not iso or not os.path.exists(iso) or not dev:
            info(self, "Elija el ISO y la memoria USB.")
            return
        if not confirm_destructive(self, f"Se borrará TODO el contenido de {self.usb.currentText()}"):
            return
        verify = self.verify.isChecked()
        self.w = Worker(lambda w: bootmedia.write_iso(iso, dev, lambda d, t, s: w.progress.emit(s, d, t),
                                                      w.cancelled, verify), self)
        self.w.progress.connect(lambda s, d, t: (self.bar.setValue(int(1000 * d / max(1, t))),
                                                 self.stat.setText(f"{s}… {100 * d / max(1, t):.1f}%")))
        self.w.done.connect(lambda r: info(self, "Memoria de arranque lista y verificada." if r["ok"] else
                                           "La verificación falló: pruebe con otra memoria USB."))
        self.w.failed.connect(lambda m: error(self, m))
        self.w.start()
