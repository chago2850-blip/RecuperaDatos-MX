"""Páginas de la aplicación (excepto Resultados, que vive en results.py)."""
from __future__ import annotations

import os
import sys
import time
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QMessageBox, QPlainTextEdit, QProgressBar, QScrollArea, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import __app_name__, __author__, __email__, __version__
from ..core import carving, imaging, partitions as P, raid, repair, smart, templates, vault
from ..core.device import BlockSource, SliceSource, human, is_admin, list_devices
from ..core.scanner import ScanOptions
from .common import (SourcePicker, Worker, btn, card, colors, confirm_destructive, error, heading, info,
                     open_source, restart_as_admin, tbtn)
from .hexview import HexView

ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")


def ask_passwords(parent, src, opts) -> None:
    """Si el origen tiene volúmenes cifrados, pide contraseñas/claves antes de escanear."""
    from ..core.scanner import find_encrypted
    try:
        locked = find_encrypted(src)
    except Exception:
        locked = []
    if not locked:
        return
    from .pages2 import PasswordDialog
    d = PasswordDialog(parent, locked)
    if d.exec():
        opts.passwords = d.passwords()
        opts.bek_files = d.bek


def page_layout(w: QWidget) -> QVBoxLayout:
    lay = QVBoxLayout(w)
    lay.setContentsMargins(26, 22, 26, 22)
    lay.setSpacing(14)
    return lay


def scrollable(inner: QWidget) -> QScrollArea:
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QScrollArea.NoFrame)
    sc.setWidget(inner)
    return sc


def table(headers: List[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    return t


def set_row(t: QTableWidget, r: int, vals) -> None:
    for c, v in enumerate(vals):
        it = QTableWidgetItem(str(v))
        t.setItem(r, c, it)


# =========================================================================== #
class HomePage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        lay = page_layout(self)
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QPixmap(os.path.join(ASSETS, "logo.png")).scaled(64, 64, Qt.KeepAspectRatio,
                                                                        Qt.SmoothTransformation))
        head.addWidget(logo)
        head.addWidget(heading("¿Dónde estaban sus archivos?",
                               "Elija un disco, partición, memoria USB/SD o imagen y el tipo de escaneo. "
                               "Todos los análisis son de solo lectura: nada se escribe en el origen."), 1)
        lay.addLayout(head)
        if not is_admin():
            row = QHBoxLayout()
            ban = QLabel("🔒  Sin privilegios de administrador solo podrá analizar imágenes de disco. Para leer "
                         "discos físicos reinicie la aplicación como administrador.")
            ban.setObjectName("Banner")
            ban.setWordWrap(True)
            row.addWidget(ban, 1)
            row.addWidget(btn("Reiniciar como administrador", "Vuelve a abrir RecuperaDatos-MX con permisos para "
                              "acceder directamente a los discos", "admin", self.elevate))
            lay.addLayout(row)
        body = QHBoxLayout()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Dispositivo", "Tamaño", "Sistema de archivos", "Montaje / letra", "Ruta"])
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tree.setToolTip("Discos y particiones detectados. Seleccione uno y elija una acción a la derecha.")
        self.tree.itemDoubleClicked.connect(lambda *_: self.scan(False))
        body.addWidget(card(self.tree, title="Unidades"), 3)
        acts = QVBoxLayout()
        acts.setSpacing(8)
        for text, tip, ic, slot, kind in [
            ("Escaneo rápido", "Lee la tabla de archivos (MFT, FAT, inodos, catálogo) para encontrar en segundos los "
             "archivos borrados conservando nombres y carpetas.", "scan", lambda: self.scan(False), "Primary"),
            ("Escaneo profundo", "Escaneo rápido + búsqueda de particiones perdidas + reconstrucción de MFT dañada "
             "+ recuperación por firmas de más de 70 familias de formatos. Ideal para discos formateados o RAW.",
             "deep", lambda: self.scan(True), ""),
            ("Escaneo personalizado…", "Elija qué etapas ejecutar, el rango de sectores y la búsqueda de firmas "
             "sin alineación (más lenta, encuentra archivos incrustados).", "signature", self.custom_scan, ""),
            ("Buscar particiones perdidas", "Busca sectores de arranque y superbloques de NTFS, FAT, exFAT, ext, "
             "HFS+, APFS y XFS en toda la superficie del disco.", "partition", self.find_partitions, ""),
            ("Abrir imagen de disco", "Agregar un archivo .img/.dd/.raw/.001/.vhd/.iso/.rdmx a la lista de "
             "unidades para escanearlo como si fuera un disco.", "open", self.add_image, ""),
            ("Cargar sesión guardada", "Reanudar un escaneo guardado (.rdsesion) sin volver a analizar el disco.",
             "save", self.load_session, ""),
            ("Crear imagen byte a byte", "Copiar el disco a un archivo antes de recuperar: protege discos que están "
             "fallando.", "image", lambda: self.main.go("imagen"), ""),
            ("Actualizar unidades", "Volver a detectar discos conectados (USB, SD, discos externos).", "refresh",
             self.refresh, ""),
        ]:
            b = btn(text, tip, ic, slot, kind or "Big")
            if kind == "Primary":
                b.setMinimumHeight(46)
            acts.addWidget(b)
        acts.addStretch(1)
        side = QWidget()
        side.setLayout(acts)
        body.addWidget(card(side, title="Acciones"), 1)
        lay.addLayout(body, 1)
        self.images: List[str] = []
        self.refresh()

    def elevate(self) -> None:
        if restart_as_admin():
            self.main.close()
        else:
            error(self, "No se pudo solicitar privilegios. Ejecute la aplicación como administrador / con sudo.")

    def refresh(self) -> None:
        self.tree.clear()
        for d in list_devices():
            it = QTreeWidgetItem([f"{'🔌 ' if d.removable else '💽 '}{d.model or d.name}", human(d.size), "", "", d.path])
            it.setData(0, Qt.UserRole, d.path)
            for c in d.children:
                ch = QTreeWidgetItem([c.name, human(c.size), c.fs, c.mountpoint, c.path])
                ch.setData(0, Qt.UserRole, c.path)
                it.addChild(ch)
            self.tree.addTopLevelItem(it)
            it.setExpanded(True)
        for p in self.images:
            it = QTreeWidgetItem([f"🗂 {os.path.basename(p)}", human(os.path.getsize(p)) if os.path.exists(p) else "",
                                  "imagen", "", p])
            it.setData(0, Qt.UserRole, p)
            self.tree.addTopLevelItem(it)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def add_image(self, path: Optional[str] = None) -> None:
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Abrir imagen de disco", "",
                                                  "Imágenes (*.img *.dd *.raw *.bin *.001 *.vhd *.iso *.dmg *.cdr *.E01 *.e01 *.rdmx);;"
                                                  "Todos (*)")
        if path:
            self.images.append(path)
            self.refresh()
            self.tree.setCurrentItem(self.tree.topLevelItem(self.tree.topLevelItemCount() - 1))
            self.main.register_image(path)

    def current(self) -> Optional[str]:
        it = self.tree.currentItem()
        return it.data(0, Qt.UserRole) if it else None

    def _open(self) -> Optional[BlockSource]:
        p = self.current()
        if not p:
            info(self, "Seleccione una unidad o abra una imagen de disco.")
            return None
        try:
            return open_source(p)
        except PermissionError:
            error(self, "Permiso denegado. Reinicie la aplicación como administrador para leer este disco.")
        except OSError as exc:
            error(self, f"No se pudo abrir {p}: {exc}")
        return None

    def scan(self, deep: bool, opts: Optional[ScanOptions] = None) -> None:
        src = self._open()
        if src:
            opts = opts or ScanOptions(deep=deep, signatures=self.main.signatures())
            ask_passwords(self, src, opts)
            self.main.go("resultados")
            self.main.results.start_scan(src, opts, f"{'Escaneo profundo' if opts.deep else 'Escaneo rápido'} · "
                                                    f"{src.name}")

    def custom_scan(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        d = QDialog(self)
        d.setWindowTitle("Escaneo personalizado")
        f = QFormLayout(d)
        checks = {}
        for key, label, tip in [("filesystems", "Leer sistemas de archivos", "MFT, FAT, exFAT, inodos ext, HFS+"),
                                ("lost_partitions", "Buscar particiones perdidas", "Sectores de arranque y superbloques"),
                                ("orphan_records", "Reconstruir NTFS desde registros MFT", "Para MFT dañada o formateo"),
                                ("carving", "Recuperación por firmas", "Busca archivos por su contenido"),
                                ("aligned", "Firmas alineadas a sector (rápido)", "Desactive para buscar archivos "
                                 "incrustados o en imágenes de memoria (más lento)")]:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.setToolTip(tip)
            checks[key] = cb
            f.addRow(cb)
        start = QSpinBox()
        start.setRange(0, 2_000_000_000)
        start.setSuffix(" MiB")
        start.setToolTip("Comenzar el escaneo profundo en esta posición")
        end = QSpinBox()
        end.setRange(0, 2_000_000_000)
        end.setSuffix(" MiB (0 = fin)")
        end.setToolTip("Terminar el escaneo profundo en esta posición")
        f.addRow("Desde", start)
        f.addRow("Hasta", end)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if d.exec():
            o = ScanOptions(deep=True, signatures=self.main.signatures(),
                            **{k: v.isChecked() for k, v in checks.items()},
                            start=start.value() << 20, end=(end.value() << 20) or None)
            self.scan(True, o)

    def find_partitions(self) -> None:
        src = self._open()
        if src:
            self.main.go("particiones")
            self.main.partitions_page.search(src)

    def load_session(self) -> None:
        from ..core import session
        p, _ = QFileDialog.getOpenFileName(self, "Cargar sesión", "", "Sesión (*.rdsesion)")
        if not p:
            return
        try:
            desc, entries, parts = session.load(p)
            try:
                src = session.reopen_source(desc)
            except Exception:
                info(self, "No se encontró el origen original de la sesión; seleccione el disco o la imagen.")
                src = self._open()
                if not src:
                    return
            self.main.go("resultados")
            self.main.results.load_entries(src, entries, f"Sesión · {os.path.basename(p)}", parts)
        except Exception as exc:
            error(self, f"No se pudo cargar la sesión: {exc}")


# =========================================================================== #
class PartitionsPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        self.src: Optional[BlockSource] = None
        self.parts: List[P.Partition] = []
        lay = page_layout(self)
        lay.addWidget(heading("Particiones", "Tabla de particiones actual y particiones perdidas encontradas por "
                                             "sus sectores de arranque. Puede explorarlas sin escribir nada o "
                                             "restaurarlas en la tabla del disco."))
        self.picker = SourcePicker()
        lay.addWidget(self.picker)
        row = QHBoxLayout()
        row.addWidget(btn("Leer tabla", "Mostrar las particiones registradas en MBR/GPT (incluida la copia de "
                          "respaldo de GPT)", "partition", self.read_table))
        row.addWidget(btn("Búsqueda rápida", "Buscar particiones perdidas en límites típicos (MiB y cilindros) "
                          "y en todos los sectores con firma 55AA", "scan", lambda: self.search(None, True)))
        row.addWidget(btn("Búsqueda completa", "Revisar cada sector del disco en busca de sistemas de archivos "
                          "(más lenta, más exhaustiva)", "deep", lambda: self.search(None, False)))
        row.addStretch(1)
        lay.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.table = table(["#", "Esquema", "Inicio (LBA)", "Tamaño", "Sistema", "Tipo", "Etiqueta", "Detección"])
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.doubleClicked.connect(lambda *_: self.scan_selected(False))
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(btn("Explorar (rápido)", "Escanear solo esta partición leyendo su sistema de archivos",
                          "scan", lambda: self.scan_selected(False), "Primary"))
        row.addWidget(btn("Escaneo profundo", "Escaneo profundo limitado a esta partición", "deep",
                          lambda: self.scan_selected(True)))
        row.addWidget(btn("Abrir en editor", "Ver el primer sector de la partición en el editor de disco", "hex",
                          self.open_hex))
        row.addStretch(1)
        row.addWidget(btn("Respaldar sectores", "Guardar una copia de los primeros y últimos 34 sectores del disco "
                          "(MBR/GPT) antes de modificar nada", "save", self.backup))
        row.addWidget(btn("Reparar arranque NTFS", "Copiar el sector de arranque de respaldo (final de la "
                          "partición) sobre el primario dañado", "repair", self.repair_boot))
        row.addWidget(btn("Escribir tabla MBR", "Reescribir la tabla MBR con las particiones seleccionadas "
                          "(máximo 4). ¡Escribe en el disco!", "edit", lambda: self.write("MBR"), "Danger"))
        row.addWidget(btn("Escribir tabla GPT", "Crear una tabla GPT (y su copia) con las particiones "
                          "seleccionadas. ¡Escribe en el disco!", "edit", lambda: self.write("GPT"), "Danger"))
        lay.addLayout(row)

    def set_partitions(self, src: BlockSource, parts: List[P.Partition]) -> None:
        self.src = src
        self.parts = list(parts)
        self._fill()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.parts))
        ss = self.src.sector_size if self.src else 512
        for r, p in enumerate(self.parts):
            set_row(self.table, r, [p.index, p.scheme, p.start // ss, human(p.size), p.fs or "—", p.type_name,
                                    p.label, p.confidence or ("tabla" if not p.found else "")])
            if p.found:
                for c in range(8):
                    self.table.item(r, c).setForeground(QColor(colors()["accent"]))

    def _src(self) -> Optional[BlockSource]:
        try:
            self.src = self.picker.open() if self.picker.path() else self.src
        except Exception as exc:
            error(self, str(exc))
        return self.src

    def read_table(self) -> None:
        src = self._src()
        if src:
            self.parts = P.read_table(src)
            self._fill()
            if not self.parts:
                info(self, "No hay tabla de particiones válida. Use la búsqueda para encontrar particiones perdidas.")

    def search(self, src: Optional[BlockSource] = None, quick: bool = True) -> None:
        src = src or self._src()
        if not src:
            return
        self.src = src
        existing = P.read_table(src)

        def job(w):
            return P.search_lost(src, progress=lambda d, t: w.progress.emit("", d, t), cancel=w.cancelled, quick=quick)

        self.w = Worker(job, self)
        self.w.progress.connect(lambda s, d, t: self.bar.setValue(int(1000 * d / max(1, t))))

        def done(found):
            known = {p.start for p in existing}
            self.parts = existing + [p for p in found if p.start not in known]
            self._fill()
            self.main.status(f"{len(found)} sistemas de archivos encontrados en la superficie del disco")

        self.w.done.connect(done)
        self.w.failed.connect(lambda m: error(self, m))
        self.w.start()

    def selected(self) -> List[P.Partition]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.parts[r] for r in rows]

    def scan_selected(self, deep: bool) -> None:
        sel = self.selected()
        if not sel or not self.src:
            return
        p = sel[0]
        sub = SliceSource(self.src, p.start, p.size, name=f"{self.src.name} · partición {p.index} ({p.fs})")
        self.main.go("resultados")
        opts = ScanOptions(deep=deep, signatures=self.main.signatures())
        ask_passwords(self, sub, opts)
        self.main.results.start_scan(sub, opts, f"Partición {p.index} ({p.fs}) · {self.src.name}")

    def open_hex(self) -> None:
        sel = self.selected()
        if sel and self.src:
            self.main.go("editor")
            self.main.hex_page.show_source(self.src, sel[0].start)

    def backup(self) -> None:
        if not self.src:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Guardar respaldo de sectores", "respaldo_sectores.bin")
        if p:
            P.backup_sectors(self.src, p)
            info(self, f"Respaldo guardado en {p}")

    def _writable(self):
        path = getattr(self.src, "path", None)
        if not path:
            error(self, "Seleccione un disco completo o una imagen (no una vista de partición).")
            return None
        return open_source(path, writable=True)

    def repair_boot(self) -> None:
        sel = [p for p in self.selected() if p.fs == "NTFS"]
        if not sel:
            info(self, "Seleccione una partición NTFS encontrada por su sector de respaldo.")
            return
        if not confirm_destructive(self, "Reparar el sector de arranque NTFS"):
            return
        dev = self._writable()
        if dev and P.repair_ntfs_boot(dev, sel[0]):
            info(self, "Sector de arranque restaurado desde el respaldo.")
        elif dev:
            error(self, "No se encontró un sector de arranque de respaldo válido al final de la partición.")

    def write(self, scheme: str) -> None:
        sel = self.selected()
        if not sel:
            info(self, "Seleccione las particiones que deben quedar en la tabla.")
            return
        if not confirm_destructive(self, f"Escribir una tabla {scheme} con {len(sel)} partición(es)"):
            return
        dev = self._writable()
        if not dev:
            return
        bk = os.path.join(os.path.expanduser("~"), f"respaldo_tabla_{int(time.time())}.bin")
        P.backup_sectors(dev, bk)
        try:
            (P.write_mbr if scheme == "MBR" else P.write_gpt)(dev, sel)
            info(self, f"Tabla {scheme} escrita. Respaldo previo: {bk}\nDesconecte y vuelva a conectar el disco.")
        except Exception as exc:
            error(self, f"Error al escribir: {exc}\nRespaldo previo: {bk}")
        finally:
            dev.close()


# =========================================================================== #
class RaidPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        lay = page_layout(self)
        lay.addWidget(heading("RAID virtual", "Reconstruya arreglos RAID dañados o de NAS a partir de sus discos o "
                                              "imágenes individuales, sin la controladora original. Admite miembros "
                                              "faltantes en RAID 4/5/6 y detección automática de parámetros."))
        self.members = table(["Orden", "Miembro", "Tamaño", "Desplazamiento (sectores)"])
        self.members.setEditTriggers(QAbstractItemView.DoubleClicked)
        row = QHBoxLayout()
        self.picker = SourcePicker(label="")
        row.addWidget(self.picker, 1)
        row.addWidget(btn("Agregar", "Agregar el disco/imagen seleccionado como miembro del RAID", "plus", self.add))
        row.addWidget(btn("Faltante", "Agregar un miembro ausente o dañado (se reconstruye por paridad)", "minus",
                          self.add_missing))
        row.addWidget(tbtn("up", "Subir el miembro en el orden", lambda: self.move(-1)))
        row.addWidget(tbtn("down", "Bajar el miembro en el orden", lambda: self.move(1)))
        row.addWidget(btn("Quitar", "Quitar el miembro seleccionado", "minus", self.remove))
        lay.addLayout(row)
        lay.addWidget(self.members, 1)
        form = QHBoxLayout()
        self.level = QComboBox()
        for lv in raid.LEVELS:
            self.level.addItem({"JBOD": "JBOD / volumen distribuido"}.get(lv, f"RAID {lv}"), lv)
        self.level.setCurrentIndex(raid.LEVELS.index("5"))
        self.level.setToolTip("Nivel del arreglo")
        self.stripe = QComboBox()
        for k in (4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048):
            self.stripe.addItem(f"{k} KB", k << 10)
        self.stripe.setCurrentIndex(4)
        self.stripe.setToolTip("Tamaño de franja (bloque) del RAID")
        self.layout_ = QComboBox()
        for l in raid.LAYOUTS:
            self.layout_.addItem(raid.LAYOUT_NAMES[l], l)
        self.layout_.setCurrentIndex(1)
        self.layout_.setToolTip("Rotación de la paridad (RAID 5/6)")
        self.delay = QSpinBox()
        self.delay.setRange(1, 64)
        self.delay.setToolTip("Retardo de paridad: número de filas que comparten disco de paridad (HP/Compaq usan 16)")
        for lbl, w in (("Nivel", self.level), ("Franja", self.stripe), ("Esquema", self.layout_),
                       ("Retardo de paridad", self.delay)):
            form.addWidget(QLabel(lbl))
            form.addWidget(w)
        form.addStretch(1)
        lay.addLayout(form)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.result = QLabel("")
        self.result.setObjectName("Muted")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        row = QHBoxLayout()
        row.addWidget(btn("Detectar parámetros", "Probar órdenes, tamaños de franja, niveles y esquemas hasta "
                          "encontrar uno con sistema de archivos coherente", "find", self.detect))
        row.addWidget(btn("Cargar definición", "Abrir una definición de RAID guardada (.rdmx)", "open", self.load))
        row.addWidget(btn("Guardar definición", "Guardar los parámetros como .rdmx para reabrir el RAID", "save",
                          self.save))
        row.addStretch(1)
        row.addWidget(btn("Construir y escanear", "Crear el RAID virtual y escanearlo como un disco", "play",
                          self.build_scan, "Primary"))
        row.addWidget(btn("Construir y abrir en editor", "Ver el RAID virtual en el editor de disco para validar "
                          "los parámetros", "hex", self.build_hex))
        lay.addLayout(row)
        self.items: List[Optional[str]] = []

    def _refresh(self) -> None:
        self.members.setRowCount(len(self.items))
        for r, p in enumerate(self.items):
            size = ""
            if p:
                try:
                    s = open_source(p)
                    size = human(s.size)
                    s.close()
                except OSError:
                    size = "?"
            set_row(self.members, r, [r + 1, p or "⚠ faltante", size, "0" if not self.members.item(r, 3)
                                      else self.members.item(r, 3).text()])

    def add(self) -> None:
        p = self.picker.path()
        if p:
            self.items.append(p)
            self._refresh()

    def add_missing(self) -> None:
        self.items.append(None)
        self._refresh()

    def remove(self) -> None:
        r = self.members.currentRow()
        if 0 <= r < len(self.items):
            self.items.pop(r)
            self._refresh()

    def move(self, d: int) -> None:
        r = self.members.currentRow()
        j = r + d
        if 0 <= r < len(self.items) and 0 <= j < len(self.items):
            self.items[r], self.items[j] = self.items[j], self.items[r]
            self._refresh()
            self.members.selectRow(j)

    def build(self) -> Optional[raid.VirtualRAID]:
        if len(self.items) < 2:
            info(self, "Agregue al menos dos miembros.")
            return None
        try:
            mem = [open_source(p) if p else None for p in self.items]
            offs = []
            for r in range(len(self.items)):
                it = self.members.item(r, 3)
                offs.append(int(it.text() or 0) * 512 if it else 0)
            return raid.VirtualRAID(mem, self.level.currentData(), self.stripe.currentData(),
                                    self.layout_.currentData(), offs, self.delay.value())
        except Exception as exc:
            error(self, f"No se pudo construir el RAID: {exc}")
            return None

    def build_scan(self) -> None:
        v = self.build()
        if v:
            self.main.go("resultados")
            self.main.results.start_scan(v, ScanOptions(deep=True, signatures=self.main.signatures()),
                                         f"RAID {v.level} virtual ({v.n} discos)")

    def build_hex(self) -> None:
        v = self.build()
        if v:
            self.main.go("editor")
            self.main.hex_page.show_source(v, 0)

    def detect(self) -> None:
        present = [p for p in self.items if p]
        if len(present) < 2 or len(present) != len(self.items):
            info(self, "La detección automática necesita todos los miembros presentes.")
            return
        mem = [open_source(p) for p in present]

        def job(w):
            return raid.auto_detect(mem, levels=("0", "5", "6", "10", "1E"),
                                    progress=lambda d, t: w.progress.emit("", d, t), cancel=w.cancelled)

        self.w = Worker(job, self)
        self.w.progress.connect(lambda s, d, t: self.bar.setValue(int(1000 * d / max(1, t))))

        def done(res):
            self.bar.setValue(1000)
            if not res:
                self.result.setText("No se encontró una configuración coherente. Pruebe a ajustar los "
                                    "desplazamientos (p. ej. metadatos de la controladora al inicio).")
                return
            best = res[0]
            self.items = [present[i] for i in best["orden"]]
            self._refresh()
            self.level.setCurrentIndex(raid.LEVELS.index(best["nivel"]))
            self.stripe.setCurrentIndex(self.stripe.findData(best["franja"]))
            self.layout_.setCurrentIndex(raid.LAYOUTS.index(best["esquema"]))
            self.result.setText("Mejores candidatos: " + "; ".join(
                f"RAID {r['nivel']} · {r['franja'] >> 10} KB · {raid.LAYOUT_NAMES.get(r['esquema'], '')} · "
                f"orden {[i + 1 for i in r['orden']]} · {r['puntos']:.1f} pts" for r in res))

        self.w.done.connect(done)
        self.w.failed.connect(lambda m: error(self, m))
        self.w.start()

    def save(self) -> None:
        v = self.build()
        if not v:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Guardar definición RAID", "raid.rdmx", "RAID virtual (*.rdmx)")
        if p:
            raid.save_raid_definition(p, v)
            self.main.status(f"Definición guardada en {p}")

    def load(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Cargar definición RAID", "", "RAID virtual (*.rdmx)")
        if not p:
            return
        import json
        d = json.load(open(p, encoding="utf-8"))
        self.items = [(m.get("ruta") if m else None) for m in d["miembros"]]
        self._refresh()
        for r, o in enumerate(d.get("desplazamientos", [])):
            self.members.setItem(r, 3, QTableWidgetItem(str(o // 512)))
        self.level.setCurrentIndex(raid.LEVELS.index(d["nivel"]))
        self.stripe.setCurrentIndex(max(0, self.stripe.findData(d["franja"])))
        self.layout_.setCurrentIndex(raid.LAYOUTS.index(d.get("esquema", "left-symmetric")))
        self.delay.setValue(d.get("retardo_paridad", 1))


# =========================================================================== #
class ImagingPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        lay = page_layout(self)
        lay.addWidget(heading("Imagen y clonado byte a byte", "Copie un disco dañado sector por sector antes de "
                                                              "recuperar. Los sectores ilegibles se reintentan, se "
                                                              "rellenan con ceros y se registran; la copia puede "
                                                              "reanudarse si se interrumpe."))
        self.mode = QComboBox()
        self.mode.addItem("Crear imagen de disco (archivo .img)", "imagen")
        self.mode.addItem("Clonar disco a disco", "clonar")
        self.mode.addItem("Restaurar imagen a un disco", "restaurar")
        self.mode.setToolTip("Tipo de copia")
        lay.addWidget(self.mode)
        self.src = SourcePicker(label="Origen:")
        lay.addWidget(self.src)
        drow = QHBoxLayout()
        drow.addWidget(QLabel("Destino:"))
        self.dest = QLineEdit()
        self.dest.setPlaceholderText("Archivo de imagen o dispositivo destino")
        self.dest.setToolTip("Para imagen: ruta del archivo .img. Para clonar/restaurar: el dispositivo destino")
        drow.addWidget(self.dest, 1)
        drow.addWidget(btn("Elegir…", "Elegir el archivo o dispositivo destino", "open", self.pick_dest))
        lay.addLayout(drow)
        opts = QHBoxLayout()
        self.block = QComboBox()
        for k in (64, 256, 1024, 4096, 16384):
            self.block.addItem(f"Bloque {k} KB", k << 10)
        self.block.setCurrentIndex(2)
        self.block.setToolTip("Tamaño de lectura; bloques pequeños manejan mejor los discos con fallas")
        self.reverse = QCheckBox("Copia inversa")
        self.reverse.setToolTip("Leer del final al principio (ayuda con algunos discos que fallan en una zona)")
        self.skip = QSpinBox()
        self.skip.setRange(0, 1 << 20)
        self.skip.setSuffix(" KB salto tras error")
        self.skip.setToolTip("Saltar esta cantidad tras un área dañada para rescatar primero las zonas sanas")
        self.split = QSpinBox()
        self.split.setRange(0, 4096)
        self.split.setSuffix(" GB por parte")
        self.split.setToolTip("Dividir la imagen en partes (.001, .002…) para memorias FAT32; 0 = un solo archivo")
        self.verify = QCheckBox("Calcular MD5 y SHA-256")
        self.verify.setChecked(True)
        self.verify.setToolTip("Huellas para verificar la integridad de la copia (uso forense)")
        for w in (self.block, self.reverse, self.skip, self.split, self.verify):
            opts.addWidget(w)
        opts.addStretch(1)
        lay.addLayout(opts)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.stat = QLabel("")
        self.stat.setObjectName("Muted")
        lay.addWidget(self.stat)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.stop_b = btn("Detener", "Interrumpir la copia (podrá reanudarla después con el mismo destino)", "stop",
                          self.stop)
        self.stop_b.setEnabled(False)
        row.addWidget(self.stop_b)
        row.addWidget(btn("Iniciar copia", "Comenzar la copia byte a byte", "play", self.start, "Primary"))
        lay.addLayout(row)

    def pick_dest(self) -> None:
        if self.mode.currentData() == "imagen":
            p, _ = QFileDialog.getSaveFileName(self, "Guardar imagen", "disco.img", "Imagen RAW (*.img *.dd)")
        else:
            p, _ = QInputDialog.getText(self, "Dispositivo destino",
                                        "Ruta del dispositivo destino (p. ej. /dev/sdc, \\\\.\\PhysicalDrive2, "
                                        "/dev/rdisk3):")
        if p:
            self.dest.setText(p)

    def stop(self) -> None:
        if getattr(self, "w", None):
            self.w.cancel()

    def start(self) -> None:
        mode = self.mode.currentData()
        dest = self.dest.text().strip()
        if not dest:
            info(self, "Indique el destino.")
            return
        try:
            src = self.src.open()
        except Exception as exc:
            error(self, str(exc))
            return
        if mode != "imagen" and not confirm_destructive(self, f"Se sobrescribirá TODO el contenido de {dest}"):
            return
        kw = dict(block=self.block.currentData(), reverse=self.reverse.isChecked(), skip_on_error=self.skip.value() << 10,
                  hashes=("md5", "sha256") if self.verify.isChecked() else ())

        def job(w):
            prog = lambda d, t, i: w.progress.emit(f"{human(i['velocidad'])}/s · {i['malos']} zonas dañadas", d, t)
            if mode == "imagen":
                return imaging.create_image(src, dest, split_size=self.split.value() << 30, progress=prog,
                                            cancel=w.cancelled, **kw)
            return imaging.clone_to_device(src, dest, progress=prog, cancel=w.cancelled,
                                           map_path=os.path.join(os.path.expanduser("~"), "clonado.mapa.json"), **kw)

        self.w = Worker(job, self)
        self.w.progress.connect(lambda s, d, t: (self.bar.setValue(int(1000 * d / max(1, t))),
                                                 self.stat.setText(f"{human(d)} de {human(t)} · {s}")))
        self.w.done.connect(self.done)
        self.w.failed.connect(lambda m: (error(self, m), self.stop_b.setEnabled(False)))
        self.stop_b.setEnabled(True)
        self.log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {self.mode.currentText()}: {src.name} → {dest}")
        self.w.start()

    def done(self, res) -> None:
        self.stop_b.setEnabled(False)
        self.log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {res.get('estado')}: {human(res.get('bytes', 0))}, "
                                 f"{len(res.get('malos', []))} zonas dañadas")
        for k, v in res.get("hashes", {}).items():
            self.log.appendPlainText(f"   {k.upper()}: {v}")
        if res.get("estado") == "completado" and self.mode.currentData() == "imagen":
            self.main.home.add_image(self.dest.text().strip() if not self.split.value()
                                     else os.path.splitext(self.dest.text().strip())[0] + ".001")


# =========================================================================== #
class HexPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        self.src: Optional[BlockSource] = None
        lay = page_layout(self)
        lay.addWidget(heading("Editor de disco", "Inspeccione sectores en hexadecimal, salte a estructuras y "
                                                 "consulte su interpretación con plantillas (MBR, GPT, NTFS, FAT, ext, MFT)."))
        self.picker = SourcePicker()
        row = QHBoxLayout()
        row.addWidget(self.picker, 1)
        row.addWidget(btn("Abrir", "Mostrar el origen seleccionado en el editor", "open", self.open_picker))
        lay.addLayout(row)
        nav = QHBoxLayout()
        self.lba = QLineEdit("0")
        self.lba.setToolTip("Sector (LBA) o desplazamiento: 2048, 0x100000, 1048576b")
        self.lba.returnPressed.connect(self.go_lba)
        nav.addWidget(QLabel("Ir a sector:"))
        nav.addWidget(self.lba)
        for ic, tip, d in (("first", "Primer sector", "first"), ("left", "Sector anterior", -1),
                           ("right", "Sector siguiente", 1), ("last", "Último sector", "last")):
            nav.addWidget(tbtn(ic, tip, lambda _=False, d=d: self.step(d)))
        self.find_in = QLineEdit()
        self.find_in.setPlaceholderText("Buscar texto o hex (p. ej. 55AA, FILE0, %PDF)")
        self.find_in.setToolTip("Buscar desde la posición actual; use «hex:» para bytes, p. ej. hex:FFD8FF")
        self.find_in.returnPressed.connect(self.find)
        nav.addWidget(self.find_in, 1)
        nav.addWidget(btn("Buscar", "Buscar la siguiente coincidencia hacia adelante", "find", self.find))
        nav.addWidget(btn("Editar bytes", "Modificar bytes en el desplazamiento actual (requiere confirmación; "
                          "se escribe directamente en el origen)", "edit", self.edit))
        lay.addLayout(nav)
        split = QSplitter()
        self.view = HexView()
        self.view.offsetChanged.connect(self.on_offset)
        split.addWidget(self.view)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.tmpl = QComboBox()
        for k in templates.TEMPLATES:
            self.tmpl.addItem(k)
        self.tmpl.setToolTip("Plantilla para interpretar el sector actual")
        self.tmpl.currentIndexChanged.connect(lambda _: self.on_offset(self.view.top_row * 16))
        rl.addWidget(self.tmpl)
        self.tmpl_table = table(["Despl.", "Campo", "Valor"])
        rl.addWidget(self.tmpl_table, 1)
        self.pos = QLabel("")
        self.pos.setObjectName("Muted")
        rl.addWidget(self.pos)
        split.addWidget(right)
        split.setSizes([800, 360])
        lay.addWidget(split, 1)

    def open_picker(self) -> None:
        try:
            self.show_source(self.picker.open(), 0)
        except Exception as exc:
            error(self, str(exc))

    def show_source(self, src: BlockSource, offset: int = 0) -> None:
        self.src = src
        self.view.set_source(src)
        self.view.goto(offset)

    def go_lba(self) -> None:
        t = self.lba.text().strip().lower()
        try:
            if t.endswith("b"):
                off = int(t[:-1], 0)
            elif t.startswith("0x"):
                off = int(t, 16)
            else:
                off = int(t) * (self.src.sector_size if self.src else 512)
        except ValueError:
            return
        self.view.goto(off, 512)

    def step(self, d) -> None:
        if not self.src:
            return
        ss = self.src.sector_size
        cur = (self.view.top_row * 16) // ss
        if d == "first":
            cur = 0
        elif d == "last":
            cur = self.src.size // ss - 1
        else:
            cur = max(0, cur + d)
        self.view.goto(cur * ss, ss)

    def on_offset(self, off: int) -> None:
        if not self.src:
            return
        ss = self.src.sector_size
        lba = off // ss
        self.pos.setText(f"Sector {lba:,} · desplazamiento 0x{off:X} · {human(off)} de {human(self.src.size)}")
        data = self.src.read(lba * ss, 4096)
        name = self.tmpl.currentText()
        if name == "Automática":
            name, fields = templates.auto(data, lba)
        else:
            fields = templates.TEMPLATES[name](data)
        self.tmpl_table.setRowCount(len(fields))
        for r, (o, l, n, v) in enumerate(fields):
            set_row(self.tmpl_table, r, [f"+{o}", n, v])

    def find(self) -> None:
        if not self.src:
            return
        t = self.find_in.text().strip()
        if not t:
            return
        try:
            pat = bytes.fromhex(t[4:]) if t.lower().startswith("hex:") else (
                bytes.fromhex(t) if all(c in "0123456789abcdefABCDEF " for c in t) and len(t.replace(" ", "")) % 2 == 0
                else t.encode())
        except ValueError:
            pat = t.encode()
        start = self.view.highlight[0] + 1 if self.view.highlight[1] else self.view.top_row * 16
        src = self.src

        def job(w):
            pos = start
            CH = 8 << 20
            while pos < src.size and not w.cancelled():
                blk = src.read(pos, CH + len(pat))
                i = blk.find(pat)
                if i >= 0:
                    return pos + i
                pos += CH
                w.progress.emit("", pos, src.size)
            return -1

        self.w = Worker(job, self)
        self.w.progress.connect(lambda s, d, t: self.main.status(f"Buscando… {100 * d / max(1, t):.1f}%"))
        self.w.done.connect(lambda r: self.view.goto(r, len(pat)) if r >= 0 else info(self, "Sin coincidencias."))
        self.w.start()

    def edit(self) -> None:
        if not self.src or not getattr(self.src, "path", None):
            info(self, "La edición solo está disponible para discos o imágenes abiertos directamente.")
            return
        off_s, ok = QInputDialog.getText(self, "Editar bytes", "Desplazamiento (decimal o 0x…):",
                                         text=f"0x{self.view.highlight[0] or self.view.top_row * 16:X}")
        if not ok:
            return
        data_s, ok = QInputDialog.getText(self, "Editar bytes", "Nuevos bytes en hexadecimal:")
        if not ok or not data_s.strip():
            return
        try:
            off = int(off_s, 0)
            data = bytes.fromhex(data_s)
        except ValueError:
            error(self, "Valores inválidos")
            return
        if not confirm_destructive(self, f"Escribir {len(data)} bytes en 0x{off:X} de {self.src.path}"):
            return
        dev = open_source(self.src.path, writable=True)
        dev.write(off, data)
        dev.close()
        self.show_source(open_source(self.src.path), off)


# =========================================================================== #
class Gauge(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.value = -1
        self.setMinimumSize(180, 180)

    def set(self, v: int) -> None:
        self.value = v
        self.update()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = colors()
        r = min(self.width(), self.height()) - 20
        rect = ((self.width() - r) // 2, (self.height() - r) // 2, r, r)
        p.setPen(QPen(QColor(c["border"]), 14, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(*rect, 225 * 16, -270 * 16)
        if self.value >= 0:
            col = c["good"] if self.value >= 80 else (c["warn"] if self.value >= 50 else c["bad"])
            p.setPen(QPen(QColor(col), 14, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(*rect, 225 * 16, int(-270 * 16 * self.value / 100))
            p.setPen(QColor(c["text"]))
            f = p.font()
            f.setPointSize(26)
            f.setBold(True)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, f"{self.value}%")


class SmartPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        lay = page_layout(self)
        lay.addWidget(heading("Salud del disco (S.M.A.R.T.)", "Temperatura, horas de uso, sectores reasignados y "
                                                              "pendientes, desgaste de SSD/NVMe y alertas tempranas."))
        row = QHBoxLayout()
        self.picker = SourcePicker(allow_images=False, label="Disco:")
        row.addWidget(self.picker, 1)
        row.addWidget(btn("Analizar", "Leer los datos S.M.A.R.T. del disco seleccionado (usa smartctl)", "health",
                          self.run, "Primary"))
        self.auto = QCheckBox("Vigilar cada 10 min")
        self.auto.setToolTip("Volver a leer periódicamente y avisar si aparecen nuevos problemas")
        self.auto.toggled.connect(lambda on: self.timer.start(600_000) if on else self.timer.stop())
        row.addWidget(self.auto)
        lay.addLayout(row)
        top = QHBoxLayout()
        self.gauge = Gauge()
        top.addWidget(card(self.gauge, title="Salud estimada"))
        self.info = QLabel("Seleccione un disco y pulse Analizar.")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        top.addWidget(card(self.info, title="Información"), 2)
        self.alerts = QListWidget()
        top.addWidget(card(self.alerts, title="Alertas"), 2)
        lay.addLayout(top)
        self.attrs = table(["ID", "Atributo", "Valor", "Peor", "Umbral", "Bruto", "Falla"])
        lay.addWidget(self.attrs, 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.run)
        self.last_alerts = set()

    def run(self) -> None:
        p = self.picker.path()
        if not p:
            return
        self.w = Worker(lambda w: smart.read(p), self)
        self.w.done.connect(self.show)
        self.w.start()

    def show(self, d: dict) -> None:
        if d.get("error") and not d.get("atributos"):
            self.info.setText(d["error"])
            self.gauge.set(-1)
            return
        self.gauge.set(int(d["salud"]))
        st = {True: "<span style='color:#2ED47A'>APROBADO</span>", False: "<span style='color:#F0525A'>FALLÓ</span>",
              None: "desconocido"}[d.get("aprobado")]
        self.info.setText(f"<b>{d['modelo']}</b><br>Serie: {d['serie']}<br>Firmware: {d['firmware']}<br>"
                          f"Capacidad: {human(d['capacidad'])}<br>Temperatura: {d.get('temperatura') or '—'} °C<br>"
                          f"Horas encendido: {d.get('horas') or '—'} · Ciclos: {d.get('ciclos') or '—'}<br>"
                          f"Autoevaluación: {st}")
        self.alerts.clear()
        for a in d["alertas"] or ["Sin alertas"]:
            self.alerts.addItem(a)
        new = set(d["alertas"]) - self.last_alerts
        if new and self.last_alerts:
            self.main.notify("Nueva alerta S.M.A.R.T.", "; ".join(new))
        self.last_alerts = set(d["alertas"])
        self.attrs.setRowCount(len(d["atributos"]))
        for r, a in enumerate(d["atributos"]):
            set_row(self.attrs, r, [a["id"], a["nombre"], a["valor"], a["peor"], a["umbral"], a["bruto"], a["falla"]])


# =========================================================================== #
class RepairPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        lay = page_layout(self)
        lay.addWidget(heading("Reparar archivos", "Repare fotos JPEG truncadas o con cabecera dañada (con foto de "
                                                  "referencia de la misma cámara), documentos ZIP/DOCX/XLSX/PPTX/ODT "
                                                  "con el directorio central roto, PDF con la tabla xref dañada y "
                                                  "videos MP4/MOV cortados (grabación interrumpida o recuperada sin "
                                                  "índice), usando un video de referencia de la misma cámara. "
                                                  "El original nunca se modifica."))
        self.files = QListWidget()
        self.files.setToolTip("Archivos a reparar")
        row = QHBoxLayout()
        row.addWidget(btn("Agregar archivos", "Elegir archivos dañados", "plus", self.add))
        row.addWidget(btn("Quitar", "Quitar los archivos seleccionados de la lista", "minus",
                          lambda: [self.files.takeItem(self.files.row(i)) for i in self.files.selectedItems()]))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self.files, 1)
        rrow = QHBoxLayout()
        self.ref = QLineEdit()
        self.ref.setPlaceholderText("Foto o video de referencia (misma cámara y ajustes; obligatorio para video)")
        self.ref.setToolTip("Foto: se usa su cabecera para JPEG sin cabecera. Video: se copian sus parámetros de "
                            "códec para reconstruir el índice del video dañado")
        rrow.addWidget(self.ref, 1)
        rrow.addWidget(btn("Elegir…", "Seleccionar la foto de referencia", "open", self.pick_ref))
        lay.addLayout(rrow)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        lay.addWidget(self.out, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn("Reparar", "Crear copias reparadas junto a los originales (sufijo _reparado)", "repair",
                          self.run, "Primary"))
        lay.addLayout(row)

    def add(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(self, "Archivos dañados", "",
                                             "Compatibles (*.jpg *.jpeg *.pdf *.zip *.docx *.xlsx *.pptx *.odt *.ods "
                                             "*.odp *.epub *.mp4 *.mov *.m4v *.3gp);;Todos (*)")
        for p in ps:
            self.files.addItem(p)

    def pick_ref(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Referencia", "", "Foto o video (*.jpg *.jpeg *.mp4 *.mov *.m4v *.3gp)")
        if p:
            self.ref.setText(p)

    def run(self) -> None:
        for i in range(self.files.count()):
            p = self.files.item(i).text()
            try:
                ref = self.ref.text().strip() or None
                is_media = p.lower().endswith((".jpg", ".jpeg", ".mp4", ".mov", ".m4v", ".3gp"))
                out, note = repair.repair(p, ref if is_media else None)
                self.out.appendPlainText(f"✔ {os.path.basename(p)} → {out}\n   {note}")
            except Exception as exc:
                self.out.appendPlainText(f"✖ {os.path.basename(p)}: {exc}")


# =========================================================================== #
class VaultPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        lay = page_layout(self)
        lay.addWidget(heading("Bóveda de recuperación", "Protección preventiva: registra el nombre y la ubicación "
                                                        "física de los archivos de las carpetas protegidas. Si se "
                                                        "borran, se recuperan con su nombre original aunque el "
                                                        "sistema de archivos ya no los conozca."))
        try:
            self.v = vault.Vault()
        except Exception as exc:
            self.v = None
            lay.addWidget(QLabel(f"No se pudo abrir la base de la bóveda: {exc}"))
            return
        row = QHBoxLayout()
        self.folders = QListWidget()
        self.folders.setToolTip("Carpetas protegidas")
        row.addWidget(card(self.folders, title="Carpetas protegidas"), 1)
        col = QVBoxLayout()
        col.addWidget(btn("Agregar carpeta", "Proteger una carpeta (Documentos, Fotos, Escritorio…)", "plus", self.add))
        col.addWidget(btn("Quitar carpeta", "Dejar de proteger la carpeta seleccionada", "minus", self.remove))
        col.addWidget(btn("Indexar ahora", "Actualizar la bóveda inmediatamente", "refresh", self.index, "Primary"))
        self.autostart = QCheckBox("Proteger automáticamente al iniciar sesión")
        self.autostart.setToolTip("Indexa en segundo plano cada 10 minutos desde que inicia sesión")
        self.autostart.toggled.connect(self.toggle_auto)
        col.addWidget(self.autostart)
        col.addWidget(btn("Vaciar registros borrados", "Eliminar de la bóveda las entradas de archivos borrados",
                          "minus", self.purge))
        col.addStretch(1)
        w = QWidget()
        w.setLayout(col)
        row.addWidget(w)
        lay.addLayout(row)
        self.stat = QLabel("")
        self.stat.setObjectName("Muted")
        lay.addWidget(self.stat)
        self.table = table(["Archivo", "Carpeta original", "Tamaño", "Fragmentos", "Detalles"])
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        lay.addWidget(card(self.table, title="Archivos protegidos que fueron borrados"), 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn("Ver en resultados", "Abrir los archivos borrados de la bóveda en la página de resultados "
                          "para previsualizarlos y recuperarlos", "recover", self.to_results, "Primary"))
        lay.addLayout(row)
        self._load()

    def _load(self) -> None:
        if not self.v:
            return
        self.folders.clear()
        for f in self.v.folders():
            self.folders.addItem(f)
        self.entries = self.v.deleted_entries()
        self.table.setRowCount(len(self.entries))
        for r, (dev, e) in enumerate(self.entries):
            set_row(self.table, r, [e.name, e.path[len("Bóveda/"):], human(e.size), len(e.extents), e.note])

    def add(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "Carpeta a proteger")
        if p:
            self.v.add_folder(p)
            self._load()

    def remove(self) -> None:
        it = self.folders.currentItem()
        if it:
            self.v.remove_folder(it.text())
            self._load()

    def index(self) -> None:
        self.w = Worker(lambda w: vault.Vault(self.v.path).index(progress=lambda n, p: w.progress.emit(p, n, 0)), self)
        self.w.progress.connect(lambda s, n, _: self.stat.setText(f"Indexando… {n} archivos · {s}"))
        self.w.done.connect(lambda r: (self.stat.setText(f"{r['indexados']} archivos nuevos o modificados · "
                                                         f"{r['borrados']} borrados registrados"), self._load()))
        self.w.failed.connect(lambda m: error(self, m))
        self.w.start()

    def toggle_auto(self, on: bool) -> None:
        try:
            r = vault.install_autostart(on)
            self.main.status(f"Protección automática {'activada' if on else 'desactivada'} ({r})")
        except Exception as exc:
            error(self, str(exc))

    def purge(self) -> None:
        self.v.purge()
        self._load()

    def to_results(self) -> None:
        if not self.entries:
            return
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()}) or range(len(self.entries))
        chosen = [self.entries[r] for r in rows]
        dev = chosen[0][0]
        if not dev or dev.startswith("dev:"):
            dev, ok = QInputDialog.getText(self, "Disco", "Dispositivo donde estaban los archivos:")
            if not ok:
                return
        try:
            src = open_source(dev)
        except Exception as exc:
            error(self, f"No se pudo abrir {dev}: {exc}. Ejecute como administrador.")
            return
        self.main.go("resultados")
        self.main.results.load_entries(src, [e for d, e in chosen if d == chosen[0][0]], "Bóveda de recuperación")


# =========================================================================== #
class NetworkPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        self.agent = None
        self.client = None
        lay = page_layout(self)
        lay.addWidget(heading("Recuperación por red", "Escanee discos de otra computadora de la red como si "
                                                      "fueran locales. En la computadora remota inicie el agente "
                                                      "(o ejecute «recuperadatos-mx agente --clave …»); el acceso es "
                                                      "de solo lectura y requiere la clave."))
        g1 = QGroupBox("Esta computadora como agente")
        f1 = QFormLayout(g1)
        self.a_port = QSpinBox()
        self.a_port.setRange(1, 65535)
        self.a_port.setValue(7878)
        self.a_port.setToolTip("Puerto TCP en el que escuchará el agente")
        self.a_key = QLineEdit()
        self.a_key.setEchoMode(QLineEdit.Password)
        self.a_key.setToolTip("Clave compartida; el cliente debe usar la misma")
        f1.addRow("Puerto", self.a_port)
        f1.addRow("Clave", self.a_key)
        self.a_btn = btn("Iniciar agente", "Permitir que otra computadora lea (sin escribir) los discos de esta",
                         "network", self.toggle_agent, "Primary")
        f1.addRow(self.a_btn)
        self.a_stat = QLabel("Detenido")
        self.a_stat.setObjectName("Muted")
        f1.addRow(self.a_stat)
        g2 = QGroupBox("Conectarse a un agente remoto")
        f2 = QFormLayout(g2)
        self.c_host = QLineEdit()
        self.c_host.setPlaceholderText("192.168.1.20")
        self.c_host.setToolTip("Dirección IP o nombre de la computadora remota")
        self.c_port = QSpinBox()
        self.c_port.setRange(1, 65535)
        self.c_port.setValue(7878)
        self.c_key = QLineEdit()
        self.c_key.setEchoMode(QLineEdit.Password)
        self.c_key.setToolTip("Clave del agente remoto")
        f2.addRow("Equipo", self.c_host)
        f2.addRow("Puerto", self.c_port)
        f2.addRow("Clave", self.c_key)
        f2.addRow(btn("Conectar", "Conectarse y listar los discos remotos", "network", self.connect))
        top = QHBoxLayout()
        top.addWidget(g1)
        top.addWidget(g2)
        lay.addLayout(top)
        self.remote = QTreeWidget()
        self.remote.setHeaderLabels(["Dispositivo remoto", "Tamaño", "Sistema", "Ruta"])
        self.remote.setToolTip("Discos de la computadora remota")
        lay.addWidget(self.remote, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn("Escaneo rápido remoto", "Escanear el disco remoto seleccionado leyendo su sistema de "
                          "archivos", "scan", lambda: self.scan(False)))
        row.addWidget(btn("Escaneo profundo remoto", "Escaneo profundo del disco remoto (transfiere todo el disco "
                          "por la red)", "deep", lambda: self.scan(True), "Primary"))
        lay.addLayout(row)

    def toggle_agent(self) -> None:
        from ..core.netagent import run_agent
        if self.agent:
            self.agent.shutdown()
            self.agent.server_close()
            self.agent = None
            self.a_btn.setText("Iniciar agente")
            self.a_stat.setText("Detenido")
            return
        if not self.a_key.text():
            info(self, "Defina una clave.")
            return
        try:
            self.agent = run_agent("0.0.0.0", self.a_port.value(), self.a_key.text(), background=True)
            import socket
            ip = socket.gethostbyname(socket.gethostname())
            self.a_btn.setText("Detener agente")
            self.a_stat.setText(f"Escuchando en {ip}:{self.a_port.value()}")
        except Exception as exc:
            error(self, str(exc))

    def connect(self) -> None:
        from ..core.netagent import RemoteClient
        try:
            self.client = RemoteClient(self.c_host.text().strip(), self.c_port.value(), self.c_key.text())
            devs = self.client.devices()
        except Exception as exc:
            error(self, f"No se pudo conectar: {exc}")
            return
        self.remote.clear()
        for d in devs:
            it = QTreeWidgetItem([d.get("model") or d["name"], human(d["size"]), "", d["path"]])
            it.setData(0, Qt.UserRole, d["path"])
            for c in d.get("children", []):
                ch = QTreeWidgetItem([c["name"], human(c["size"]), c.get("fs", ""), c["path"]])
                ch.setData(0, Qt.UserRole, c["path"])
                it.addChild(ch)
            self.remote.addTopLevelItem(it)
            it.setExpanded(True)

    def scan(self, deep: bool) -> None:
        from ..core.netagent import RemoteSource
        it = self.remote.currentItem()
        if not it or not self.client:
            return
        try:
            src = RemoteSource(self.client.host, self.client.port, "", it.data(0, Qt.UserRole), client=self.client)
        except Exception as exc:
            error(self, str(exc))
            return
        self.main.go("resultados")
        self.main.results.start_scan(src, ScanOptions(deep=deep, signatures=self.main.signatures()),
                                     f"Remoto · {src.name}")


# =========================================================================== #
class SignaturesPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        self.path = os.path.join(vault.data_dir(), "firmas_personalizadas.json")
        self.custom = carving.load_custom(self.path)
        lay = page_layout(self)
        lay.addWidget(heading("Tipos de archivo (firmas)", "Formatos reconocidos por el escaneo profundo. Desactive "
                                                          "los que no necesite para acelerar el escaneo o agregue "
                                                          "los suyos con cabecera y pie en hexadecimal."))
        self.table = table(["Usar", "Extensión", "Descripción", "Categoría", "Tamaño máx.", "Validación"])
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(btn("Marcar todos", "Activar todas las firmas", "plus", lambda: self.set_all(True)))
        row.addWidget(btn("Desmarcar todos", "Desactivar todas las firmas", "minus", lambda: self.set_all(False)))
        row.addStretch(1)
        row.addWidget(btn("Agregar firma…", "Definir un formato propio por su cabecera (y pie opcional) en "
                          "hexadecimal", "plus", self.add, "Primary"))
        row.addWidget(btn("Eliminar personalizada", "Quitar la firma personalizada seleccionada", "minus", self.remove))
        lay.addLayout(row)
        self._fill()

    def all(self):
        return carving.SIGNATURES + self.custom

    def _fill(self) -> None:
        sigs = self.all()
        self.table.setRowCount(len(sigs))
        for r, s in enumerate(sigs):
            it = QTableWidgetItem()
            it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            it.setCheckState(Qt.Checked if s.enabled else Qt.Unchecked)
            self.table.setItem(r, 0, it)
            for c, v in enumerate([s.ext.upper(), s.desc, s.category, human(s.max_size),
                                   "estructural" if s.sizer else ("cabecera + pie" if s.footer else
                                                                  "hasta la siguiente cabecera")], 1):
                self.table.setItem(r, c, QTableWidgetItem(v))
        self.table.itemChanged.connect(self.changed)

    def changed(self, it) -> None:
        if it.column() == 0:
            self.all()[it.row()].enabled = it.checkState() == Qt.Checked

    def set_all(self, on: bool) -> None:
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def add(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        import re as _re
        d = QDialog(self)
        d.setWindowTitle("Nueva firma")
        f = QFormLayout(d)
        ext, desc, hdr, ftr = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        hdr.setPlaceholderText("p. ej. 4D 4D 00 2A")
        ftr.setPlaceholderText("opcional")
        off = QSpinBox()
        off.setRange(0, 1 << 20)
        mx = QSpinBox()
        mx.setRange(1, 1 << 20)
        mx.setValue(50)
        mx.setSuffix(" MB")
        for lbl, w in (("Extensión", ext), ("Descripción", desc), ("Cabecera (hex)", hdr), ("Pie (hex)", ftr),
                       ("Desplazamiento de la cabecera", off), ("Tamaño máximo", mx)):
            f.addRow(lbl, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if not d.exec():
            return
        try:
            h = bytes.fromhex(hdr.text())
            fb = bytes.fromhex(ftr.text()) if ftr.text().strip() else None
        except ValueError:
            error(self, "Cabecera o pie no son hexadecimales válidos")
            return
        if not ext.text().strip() or not h:
            return
        self.custom.append(carving.Signature(ext.text().strip().lower().lstrip("."), desc.text() or ext.text(),
                                             _re.escape(h), None, mx.value() << 20, off.value(), fb, custom=True))
        carving.save_custom(self.path, self.custom)
        self.table.itemChanged.disconnect(self.changed)
        self._fill()

    def remove(self) -> None:
        r = self.table.currentRow()
        sigs = self.all()
        if 0 <= r < len(sigs) and sigs[r].custom:
            self.custom.remove(sigs[r])
            carving.save_custom(self.path, self.custom)
            self.table.itemChanged.disconnect(self.changed)
            self._fill()

    def enabled(self):
        return [s for s in self.all() if s.enabled]


# =========================================================================== #
class AboutPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        lay = page_layout(self)
        logo = QLabel()
        logo.setPixmap(QPixmap(os.path.join(ASSETS, "logo.png")).scaled(150, 150, Qt.KeepAspectRatio,
                                                                        Qt.SmoothTransformation))
        logo.setAlignment(Qt.AlignCenter)
        lay.addWidget(logo)
        t = QLabel(f"<h1>{__app_name__}</h1><p>Versión {__version__}</p>")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        txt = QLabel(
            "<p>Recuperación de datos profesional, libre y sin límites: escaneo rápido y profundo, NTFS, FAT, exFAT, "
            "ext2/3/4, HFS+, APFS, ReFS, Btrfs y XFS; BitLocker, FileVault y LUKS; particiones perdidas, RAID virtual "
            "(incluido RAID 6 con dos discos fallados), imágenes byte a byte y E01, editor de disco, S.M.A.R.T., "
            "reparación de fotos, documentos y videos, teléfonos, medio de arranque, bóveda de protección y "
            "recuperación por red.</p>"
            f"<p>Desarrollador: <b>{__author__}</b> · {__email__} · 744 354 8848</p>"
            "<p>Este programa es software libre: puede redistribuirlo y/o modificarlo bajo los términos de la "
            "<b>Licencia Pública General GNU versión 3</b> o (a su elección) cualquier versión posterior. Se distribuye "
            "SIN NINGUNA GARANTÍA. Consulte el archivo LICENSE.</p>"
            "<p><a href='https://github.com/chago2850-blip/RecuperaDatos-MX'>github.com/chago2850-blip/RecuperaDatos-MX</a></p>")
        txt.setWordWrap(True)
        txt.setAlignment(Qt.AlignCenter)
        txt.setOpenExternalLinks(True)
        lay.addWidget(txt)
        lay.addStretch(1)
