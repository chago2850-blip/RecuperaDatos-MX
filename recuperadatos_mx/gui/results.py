"""Página de resultados: lista en vivo, filtros, árbol de carpetas, vista previa y recuperación."""
from __future__ import annotations

import csv
import os
import tempfile
import time
from typing import List, Optional

from PySide6.QtCore import (QAbstractTableModel, QByteArray, QBuffer, QIODevice, QModelIndex,
                            QSortFilterProxyModel, Qt, QTimer, QUrl)
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
                               QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QTableView,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..core import session
from ..core.device import BlockSource, human
from ..core.model import CATEGORIES, FileEntry, iter_content, read_head
from ..core.recover import recover, same_device
from .common import Worker, btn, colors, error, fmt_time, info

COLS = ["Nombre", "Carpeta", "Tamaño", "Modificado", "Tipo", "Estado", "Probabilidad", "Origen"]
CHANCE_ORDER = {"Excelente": 4, "Buena": 3, "Regular": 2, "Baja": 1, "": 0}
TEXT_EXT = {"txt", "csv", "log", "md", "json", "xml", "html", "htm", "ini", "cfg", "conf", "py", "js", "css",
            "c", "h", "cpp", "java", "sql", "yaml", "yml", "sh", "bat", "ps1", "srt", "eml", "rtf", "tex"}
IMG_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "ico", "svg", "pbm", "pgm", "ppm", "tga"}


def state_of(e: FileEntry) -> str:
    if e.origin == "raw":
        return "Por firma"
    if e.origin == "boveda":
        return "Bóveda"
    if e.origin == "ntfs-huerfano":
        return "Reconstruido"
    return "Borrado" if e.deleted else "Existente"


class EntryModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.rows: List[FileEntry] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[s]
        return None

    def data(self, idx, role=Qt.DisplayRole):
        e = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.DisplayRole:
            return [e.name, e.path, human(e.size) if not e.is_dir else "", fmt_time(e.mtime), e.category,
                    state_of(e), e.chance, e.ident][c]
        if role == Qt.UserRole:  # clave de ordenamiento
            return [e.name.lower(), e.path.lower(), e.size, e.mtime, e.category, state_of(e),
                    CHANCE_ORDER.get(e.chance, 0), e.ident][c]
        if role == Qt.ForegroundRole:
            col = colors()
            if c == 6:
                return QColor({"Excelente": col["good"], "Buena": col["accent"], "Regular": col["warn"],
                               "Baja": col["bad"]}.get(e.chance, col["muted"]))
            if c == 5 and e.deleted:
                return QColor(col["warn"])
        if role == Qt.ToolTipRole:
            return f"{e.full_path}\n{e.size:,} bytes · {e.fs} · {e.ident}" + (f"\n{e.note}" if e.note else "")
        return None

    def add(self, items: List[FileEntry]) -> None:
        if not items:
            return
        n = len(self.rows)
        self.beginInsertRows(QModelIndex(), n, n + len(items) - 1)
        self.rows.extend(items)
        self.endInsertRows()

    def reset(self, items: List[FileEntry]) -> None:
        self.beginResetModel()
        self.rows = list(items)
        self.endResetModel()


class EntryFilter(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.text = ""
        self.category = ""
        self.state = ""
        self.min_chance = 0
        self.min_size = 0
        self.folder = ""
        self.show_dirs = False
        self.setSortRole(Qt.UserRole)

    def filterAcceptsRow(self, row, parent):
        e = self.sourceModel().rows[row]
        if e.is_dir and not self.show_dirs:
            return False
        if self.folder and not (e.path == self.folder or e.path.startswith(self.folder + "/")):
            return False
        if self.text and self.text not in e.name.lower():
            return False
        if self.category and e.category != self.category:
            return False
        if self.state:
            st = state_of(e)
            if self.state == "Perdidos (borrados, por firma, reconstruidos)":
                if st == "Existente":
                    return False
            elif st != self.state:
                return False
        if self.min_chance and CHANCE_ORDER.get(e.chance, 0) < self.min_chance:
            return False
        if self.min_size and e.size < self.min_size:
            return False
        return True


class Preview(QWidget):
    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("Vista previa")
        self.title.setObjectName("H2")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        self.meta = QLabel("")
        self.meta.setObjectName("Muted")
        self.meta.setWordWrap(True)
        self.meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.meta)
        self.stack = QStackedWidget()
        self.img = QLabel("Seleccione un archivo para verlo antes de recuperarlo")
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setWordWrap(True)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.img)
        sc.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.hex = QPlainTextEdit()
        self.hex.setReadOnly(True)
        self.hex.setStyleSheet("font-family: 'JetBrains Mono','DejaVu Sans Mono','Consolas','Menlo',monospace; font-size: 12px;")
        self.stack.addWidget(sc)
        self.stack.addWidget(self.text)
        self.stack.addWidget(self.hex)
        self.pdf = None
        try:
            from PySide6.QtPdf import QPdfDocument
            from PySide6.QtPdfWidgets import QPdfView
            self.pdfdoc = QPdfDocument(self)
            self.pdf = QPdfView()
            self.pdf.setDocument(self.pdfdoc)
            self.pdf.setPageMode(QPdfView.PageMode.MultiPage)
            self.pdf.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            self.stack.addWidget(self.pdf)
        except Exception:
            self.pdf = None
        lay.addWidget(self.stack, 1)
        row = QHBoxLayout()
        self.btn_hex = btn("Hex", "Ver el contenido en hexadecimal (útil para comprobar si los datos son válidos)",
                           "hex", self.show_hex)
        self.btn_open = btn("Abrir", "Extraer a una carpeta temporal y abrir con la aplicación predeterminada "
                            "(videos, audio, documentos de Office…)", "external", self.open_external)
        row.addWidget(self.btn_hex)
        row.addWidget(self.btn_open)
        lay.addLayout(row)
        self.src: Optional[BlockSource] = None
        self.entry: Optional[FileEntry] = None
        self._buf = None

    def show_entry(self, src: BlockSource, e: FileEntry) -> None:
        self.src, self.entry = src, e
        self.title.setText(e.name)
        self.meta.setText(f"{e.full_path}<br>{e.size:,} bytes · {e.fs} · {e.ident}<br>"
                          f"Creado: {fmt_time(e.ctime)} · Modificado: {fmt_time(e.mtime)}<br>"
                          f"Estado: {state_of(e)} · Probabilidad: {e.chance or '—'}"
                          + (f"<br><i>{e.note}</i>" if e.note else ""))
        if e.is_dir:
            self.stack.setCurrentIndex(0)
            self.img.setText("Carpeta")
            return
        ext = e.ext
        try:
            if ext in IMG_EXT or (e.category == "Imágenes" and e.size < 40 << 20):
                data = read_head(src, e, min(e.size, 40 << 20))
                im = QImage.fromData(data)
                if not im.isNull():
                    pm = QPixmap.fromImage(im)
                    w = max(200, self.width() - 30)
                    self.img.setPixmap(pm.scaledToWidth(min(w, pm.width()), Qt.SmoothTransformation))
                    self.stack.setCurrentIndex(0)
                    return
            if ext == "pdf" and self.pdf is not None and e.size < 200 << 20:
                data = read_head(src, e, e.size)
                self._buf = QBuffer(self)
                self._buf.setData(QByteArray(data))
                self._buf.open(QIODevice.ReadOnly)
                self.pdfdoc.load(self._buf)
                if self.pdfdoc.pageCount() > 0:
                    self.stack.setCurrentWidget(self.pdf)
                    return
            if ext in TEXT_EXT:
                data = read_head(src, e, 256 << 10)
                for enc in ("utf-8", "utf-16", "cp1252"):
                    try:
                        self.text.setPlainText(data.decode(enc))
                        break
                    except UnicodeDecodeError:
                        continue
                self.stack.setCurrentIndex(1)
                return
        except Exception as exc:
            self.img.setText(f"No se pudo generar la vista previa: {exc}")
            self.stack.setCurrentIndex(0)
            return
        self.show_hex()

    def show_hex(self) -> None:
        if not self.entry or not self.src:
            return
        data = read_head(self.src, self.entry, 4096)
        lines = []
        for i in range(0, len(data), 16):
            ch = data[i:i + 16]
            lines.append(f"{i:08X}  {' '.join(f'{b:02X}' for b in ch):<48}  "
                         + "".join(chr(b) if 32 <= b < 127 else "·" for b in ch))
        self.hex.setPlainText("\n".join(lines) or "(vacío)")
        self.stack.setCurrentIndex(2)

    def open_external(self) -> None:
        if not self.entry or self.entry.is_dir:
            return
        d = tempfile.mkdtemp(prefix="recuperadatos_")
        p = os.path.join(d, self.entry.name.replace("/", "_").replace(":", "_"))
        with open(p, "wb") as fh:
            for c in iter_content(self.src, self.entry, limit=2 << 30):
                fh.write(c)
        QDesktopServices.openUrl(QUrl.fromLocalFile(p))


class ResultsPage(QWidget):
    def __init__(self, main) -> None:
        super().__init__()
        self.main = main
        self.src: Optional[BlockSource] = None
        self.partitions = []
        self.worker: Optional[Worker] = None
        self.folder_items = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        # encabezado y progreso
        top = QHBoxLayout()
        self.title = QLabel("Resultados")
        self.title.setObjectName("H1")
        top.addWidget(self.title, 1)
        self.stats = QLabel("")
        self.stats.setObjectName("Muted")
        top.addWidget(self.stats)
        lay.addLayout(top)
        prow = QHBoxLayout()
        self.stage = QLabel("Sin escaneo activo")
        self.stage.setObjectName("Muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.eta = QLabel("")
        self.eta.setObjectName("Muted")
        self.stop_btn = btn("Detener", "Detener el escaneo; los archivos ya encontrados se conservan y se pueden "
                            "recuperar", "stop", self.stop)
        self.stop_btn.setEnabled(False)
        prow.addWidget(self.stage)
        prow.addWidget(self.bar, 1)
        prow.addWidget(self.eta)
        prow.addWidget(self.stop_btn)
        lay.addLayout(prow)
        # filtros
        frow = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por nombre…")
        self.search.setToolTip("Filtra la lista por nombre de archivo mientras escribe")
        self.search.textChanged.connect(self.apply_filters)
        self.cat = QComboBox()
        self.cat.addItem("Todos los tipos", "")
        for c in list(CATEGORIES) + ["Otros"]:
            self.cat.addItem(c, c)
        self.cat.setToolTip("Mostrar solo una categoría de archivos")
        self.cat.currentIndexChanged.connect(self.apply_filters)
        self.state = QComboBox()
        for s in ("Todos los estados", "Perdidos (borrados, por firma, reconstruidos)", "Borrado", "Existente",
                  "Por firma", "Reconstruido", "Bóveda"):
            self.state.addItem(s, "" if s.startswith("Todos") else s)
        self.state.setToolTip("Filtrar por cómo se encontró el archivo")
        self.state.currentIndexChanged.connect(self.apply_filters)
        self.chance = QComboBox()
        for s, v in (("Cualquier probabilidad", 0), ("Regular o mejor", 2), ("Buena o mejor", 3), ("Excelente", 4)):
            self.chance.addItem(s, v)
        self.chance.setToolTip("Probabilidad estimada de recuperación (según si sus clústeres fueron reutilizados)")
        self.chance.currentIndexChanged.connect(self.apply_filters)
        self.minsize = QSpinBox()
        self.minsize.setRange(0, 10_000_000)
        self.minsize.setSuffix(" KB mín.")
        self.minsize.setToolTip("Ocultar archivos más pequeños que este tamaño")
        self.minsize.valueChanged.connect(self.apply_filters)
        self.dirs = QCheckBox("Carpetas")
        self.dirs.setToolTip("Mostrar también las entradas de carpetas en la lista")
        self.dirs.toggled.connect(self.apply_filters)
        for w in (self.search, self.cat, self.state, self.chance, self.minsize, self.dirs):
            frow.addWidget(w)
        frow.setStretch(0, 2)
        lay.addLayout(frow)
        # cuerpo
        split = QSplitter()
        tabs = QTabWidget()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setToolTip("Estructura de carpetas reconstruida; seleccione una para filtrar")
        self.tree.itemSelectionChanged.connect(self.folder_selected)
        self.types = QTreeWidget()
        self.types.setHeaderLabels(["Tipo", "Archivos"])
        self.types.setToolTip("Resumen por tipo de archivo; doble clic para filtrar")
        self.types.itemDoubleClicked.connect(self.type_selected)
        tabs.addTab(self.tree, "Carpetas")
        tabs.addTab(self.types, "Tipos")
        split.addWidget(tabs)
        self.model = EntryModel()
        self.proxy = EntryFilter()
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 260)
        self.table.setColumnWidth(1, 220)
        self.table.selectionModel().currentRowChanged.connect(self.row_changed)
        split.addWidget(self.table)
        self.preview = Preview()
        split.addWidget(self.preview)
        split.setSizes([230, 720, 360])
        lay.addWidget(split, 1)
        # acciones
        arow = QHBoxLayout()
        self.keep = QCheckBox("Conservar estructura de carpetas")
        self.keep.setChecked(True)
        self.keep.setToolTip("Recrear las carpetas originales dentro del destino")
        arow.addWidget(self.keep)
        arow.addStretch(1)
        arow.addWidget(btn("Guardar sesión", "Guardar el resultado del escaneo para continuar después sin volver a "
                           "escanear", "save", self.save_session))
        arow.addWidget(btn("Exportar lista", "Exportar la lista filtrada a CSV (Excel/LibreOffice)", "export",
                           self.export_csv))
        arow.addWidget(btn("Recuperar filtrados", "Recuperar todos los archivos visibles con los filtros actuales",
                           "recover", lambda: self.recover(False)))
        arow.addWidget(btn("Recuperar seleccionados", "Guardar los archivos seleccionados en otra unidad (nunca en "
                           "la misma de origen)", "recover", lambda: self.recover(True), "Primary"))
        lay.addLayout(arow)
        self.pending: List[FileEntry] = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.flush)
        self.timer.start(400)
        self.t0 = 0.0

    # ------------------------------------------------------------------ #
    def start_scan(self, src: BlockSource, opts, label: str) -> None:
        from ..core.scanner import scan
        if self.worker and self.worker.isRunning():
            error(self, "Ya hay un escaneo en curso")
            return
        self.clear()
        self.src = src
        self.title.setText(label)
        self.t0 = time.time()

        def job(w: Worker):
            return scan(src, opts, progress=lambda s, d, t: w.progress.emit(s, d, t), cancel=w.cancelled,
                        on_batch=lambda b: w.batch.emit(b))

        self.worker = Worker(job, self)
        self.worker.progress.connect(self.on_progress)
        self.worker.batch.connect(lambda b: self.pending.extend(b))
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(lambda m: (error(self, m), self.on_done(None)))
        self.stop_btn.setEnabled(True)
        self.worker.start()

    def stop(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.stage.setText("Deteniendo…")

    def on_progress(self, stage, d, t) -> None:
        self.stage.setText(stage)
        if t:
            frac = d / t
            self.bar.setValue(int(1000 * frac))
            el = time.time() - self.t0
            if frac > 0.01:
                rem = el / frac - el
                self.eta.setText(f"{int(rem // 60)} min {int(rem % 60)} s restantes")

    def on_done(self, res) -> None:
        self.flush()
        self.stop_btn.setEnabled(False)
        self.bar.setValue(1000)
        self.eta.setText("")
        if res is not None:
            self.partitions = res.partitions
            el = res.finished - res.started
            msg = f"Escaneo terminado en {int(el // 60)} min {int(el % 60)} s"
            if res.bad_bytes:
                msg += f" · {human(res.bad_bytes)} ilegibles (se rellenaron con ceros)"
            self.stage.setText(msg)
            self.main.partitions_page.set_partitions(self.src, res.partitions)
            if res.log:
                self.main.status(" | ".join(res.log[-3:]))
            if res.locked:
                info(self, "Hay volúmenes cifrados que no se pudieron desbloquear:\n\n" +
                     "\n".join(f"• {n} ({k})" for n, k, _ in res.locked) +
                     "\n\nVuelva a escanear e indique la contraseña o la clave de recuperación.")

    def clear(self) -> None:
        self.model.reset([])
        self.tree.clear()
        self.types.clear()
        self.folder_items = {}
        self.pending = []
        self.proxy.folder = ""

    def flush(self) -> None:
        if not self.pending:
            return
        items, self.pending = self.pending, []
        self.model.add(items)
        for e in items:
            if e.is_dir:
                self._folder(e.full_path, e.deleted)
            else:
                self._folder(e.path, False)
        self._update_types()
        files = [e for e in self.model.rows if not e.is_dir]
        self.stats.setText(f"{len(files):,} archivos · {human(sum(e.size for e in files))} · "
                           f"{sum(1 for e in files if e.deleted or e.origin != 'fs'):,} perdidos")

    def _folder(self, path: str, deleted: bool) -> None:
        if not path or path in self.folder_items:
            return
        parent_path, _, name = path.rpartition("/")
        if parent_path:
            self._folder(parent_path, False)
            parent = self.folder_items[parent_path]
        else:
            parent = self.tree.invisibleRootItem()
        it = QTreeWidgetItem([name])
        it.setData(0, Qt.UserRole, path)
        if deleted:
            it.setForeground(0, QColor(colors()["warn"]))
        parent.addChild(it)
        self.folder_items[path] = it

    def _update_types(self) -> None:
        counts = {}
        for e in self.model.rows:
            if not e.is_dir:
                counts[e.category] = counts.get(e.category, 0) + 1
        self.types.clear()
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            QTreeWidgetItem(self.types, [k, f"{v:,}"]).setData(0, Qt.UserRole, k)

    def load_entries(self, src: BlockSource, entries: List[FileEntry], label: str, partitions=None) -> None:
        self.clear()
        self.src = src
        self.title.setText(label)
        self.pending = list(entries)
        self.partitions = partitions or []
        self.flush()
        self.stage.setText("Sesión cargada")

    # ------------------------------------------------------------------ #
    def apply_filters(self) -> None:
        p = self.proxy
        p.text = self.search.text().strip().lower()
        p.category = self.cat.currentData()
        p.state = self.state.currentData()
        p.min_chance = self.chance.currentData()
        p.min_size = self.minsize.value() * 1024
        p.show_dirs = self.dirs.isChecked()
        p.invalidateFilter()

    def folder_selected(self) -> None:
        its = self.tree.selectedItems()
        self.proxy.folder = its[0].data(0, Qt.UserRole) if its else ""
        self.proxy.invalidateFilter()

    def type_selected(self, it, _col) -> None:
        i = self.cat.findData(it.data(0, Qt.UserRole))
        if i >= 0:
            self.cat.setCurrentIndex(i)

    def row_changed(self, cur, _prev) -> None:
        if not cur.isValid() or not self.src:
            return
        e = self.model.rows[self.proxy.mapToSource(cur).row()]
        self.preview.show_entry(self.src, e)

    def selected(self, only_selected: bool) -> List[FileEntry]:
        if only_selected:
            rows = {self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
            sel = [self.model.rows[r] for r in sorted(rows)]
            out = []
            for e in sel:
                if e.is_dir:  # recuperar el contenido completo de la carpeta
                    pref = e.full_path
                    out += [x for x in self.model.rows if not x.is_dir and (x.path == pref or x.path.startswith(pref + "/"))]
                else:
                    out.append(e)
            if not out and self.proxy.folder:
                f = self.proxy.folder
                out = [x for x in self.model.rows if not x.is_dir and (x.path == f or x.path.startswith(f + "/"))]
            return out
        return [self.model.rows[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
                for r in range(self.proxy.rowCount())]

    def recover(self, only_selected: bool) -> None:
        if not self.src:
            return
        items = [e for e in self.selected(only_selected) if not e.is_dir]
        if not items:
            info(self, "Seleccione archivos (o una carpeta del árbol) para recuperar.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Carpeta destino (en OTRA unidad)")
        if not dest:
            return
        path = getattr(self.src, "path", "") or getattr(getattr(self.src, "parent", None), "path", "")
        if path and same_device(path, dest):
            if QMessageBox.warning(self, "Destino peligroso",
                                   "La carpeta destino está en la MISMA unidad que se está recuperando.\n"
                                   "Escribir ahí puede sobrescribir los datos perdidos.\n\n¿Continuar de todos modos?",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        total = sum(e.size for e in items)
        src = self.src
        keep = self.keep.isChecked()

        def job(w: Worker):
            return recover(src, items, dest, keep_structure=keep,
                           progress=lambda d, t, n: w.progress.emit(f"Recuperando {n}", d, t), cancel=w.cancelled)

        self.t0 = time.time()
        self.worker = Worker(job, self)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(lambda r: (self.stop_btn.setEnabled(False), self.stage.setText(r["resumen"]),
                                            info(self, r["resumen"] + f"\n\nDestino: {dest}"),
                                            QDesktopServices.openUrl(QUrl.fromLocalFile(dest))))
        self.worker.failed.connect(lambda m: error(self, m))
        self.stop_btn.setEnabled(True)
        self.stage.setText(f"Recuperando {len(items):,} archivos ({human(total)})…")
        self.worker.start()

    def save_session(self) -> None:
        if not self.src or not self.model.rows:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Guardar sesión", "escaneo.rdsesion", "Sesión (*.rdsesion)")
        if p:
            session.save(p, self.src.describe(), self.model.rows, self.partitions)
            self.main.status(f"Sesión guardada en {p}")

    def export_csv(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "Exportar lista", "archivos_encontrados.csv", "CSV (*.csv)")
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(COLS + ["Bytes", "Nota"])
            for e in self.selected(False):
                w.writerow([e.name, e.path, human(e.size), fmt_time(e.mtime), e.category, state_of(e), e.chance,
                            e.ident, e.size, e.note])
        self.main.status(f"Lista exportada a {p}")
