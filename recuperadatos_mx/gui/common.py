"""Utilidades compartidas de la interfaz: botones con tooltip, tareas en segundo
plano, selector de origen y confirmaciones de operaciones destructivas."""
from __future__ import annotations

import datetime
import os
import sys
import threading
import traceback
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
                               QMessageBox, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..core.device import BlockSource, FileSource, human, list_devices, open_image
from .icons import icon

STATE = {"colors": None}


def colors() -> dict:
    return STATE["colors"]


def btn(text: str, tip: str, icon_name: Optional[str] = None, slot: Optional[Callable] = None,
        kind: str = "", parent=None) -> QPushButton:
    """Crea un botón con tooltip descriptivo (todas las acciones de la app lo usan)."""
    b = QPushButton(text, parent)
    b.setToolTip(f"<b>{text}</b><br>{tip}" if text else tip)
    b.setCursor(Qt.PointingHandCursor)
    if icon_name:
        ic_color = "#04121F" if kind == "Primary" else (colors() or {}).get("icon", "#CFE3F5")
        b.setIcon(icon(icon_name, ic_color))
    if kind:
        b.setObjectName(kind)
    if slot:
        b.clicked.connect(slot)
    return b


def tbtn(icon_name: str, tip: str, slot: Optional[Callable] = None) -> QToolButton:
    b = QToolButton()
    b.setIcon(icon(icon_name, (colors() or {}).get("icon", "#CFE3F5")))
    b.setToolTip(tip)
    b.setAutoRaise(True)
    b.setCursor(Qt.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    return b


def card(*widgets, title: str = "", layout: str = "v") -> QFrame:
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f) if layout == "v" else QHBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    if title:
        t = QLabel(title)
        t.setObjectName("H2")
        lay.addWidget(t)
    for w in widgets:
        if isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    return f


def heading(text: str, sub: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 6)
    lay.setSpacing(2)
    h = QLabel(text)
    h.setObjectName("H1")
    lay.addWidget(h)
    if sub:
        s = QLabel(sub)
        s.setObjectName("Muted")
        s.setWordWrap(True)
        lay.addWidget(s)
    return w


def fmt_time(ts: float) -> str:
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


class Worker(QThread):
    """Ejecuta ``fn(progress, cancel)`` en segundo plano."""
    progress = Signal(str, object, object)
    batch = Signal(object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable, parent=None) -> None:
        super().__init__(parent)
        self.fn = fn
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        try:
            res = self.fn(self)
            self.done.emit(res)
        except Exception as exc:  # se muestra en la interfaz
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=4)}")


def confirm_destructive(parent, what: str) -> bool:
    txt, ok = QInputDialog.getText(
        parent, "Operación de escritura",
        f"<b>{what}</b><br><br>Esta operación escribe directamente en el disco y puede destruir datos "
        "si se elige el destino equivocado.<br>Escriba <b>CONFIRMAR</b> para continuar:")
    return ok and txt.strip().upper() == "CONFIRMAR"


def error(parent, msg: str) -> None:
    QMessageBox.critical(parent, "RecuperaDatos-MX", msg)


def info(parent, msg: str) -> None:
    QMessageBox.information(parent, "RecuperaDatos-MX", msg)


class SourcePicker(QWidget):
    """Combo de discos/particiones + botón para abrir imágenes."""
    changed = Signal()

    def __init__(self, parent=None, allow_images: bool = True, label: str = "Origen:") -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lay.addWidget(QLabel(label))
        self.combo = QComboBox()
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.combo.setToolTip("Disco, partición o imagen sobre la que se trabajará")
        self.combo.currentIndexChanged.connect(lambda _: self.changed.emit())
        lay.addWidget(self.combo, 1)
        lay.addWidget(tbtn("refresh", "Actualizar la lista de dispositivos", self.refresh))
        if allow_images:
            lay.addWidget(tbtn("open", "Agregar una imagen de disco (.img, .dd, .raw, .001, .vhd, .E01, .iso, .rdmx)",
                               self.add_image))
        self.extra = []
        self.refresh()

    def refresh(self) -> None:
        cur = self.combo.currentData()
        self.combo.blockSignals(True)
        self.combo.clear()
        for d in list_devices():
            self.combo.addItem(f"💽 {d.model or d.name} — {human(d.size)}  ({d.path})", d.path)
            for c in d.children:
                self.combo.addItem(f"      └ {c.name} {c.fs} {c.mountpoint} — {human(c.size)}", c.path)
        for path, label in self.extra:
            self.combo.addItem(label, path)
        i = self.combo.findData(cur)
        if i >= 0:
            self.combo.setCurrentIndex(i)
        self.combo.blockSignals(False)

    def add_image(self, path: Optional[str] = None) -> None:
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Abrir imagen de disco", "",
                                                  "Imágenes de disco (*.img *.dd *.raw *.bin *.001 *.vhd *.iso "
                                                  "*.dmg *.cdr *.E01 *.e01 *.rdmx);;Todos los archivos (*)")
        if path:
            self.extra.append((path, f"🗂 {os.path.basename(path)}  ({path})"))
            self.refresh()
            self.combo.setCurrentIndex(self.combo.count() - 1)

    def path(self) -> Optional[str]:
        return self.combo.currentData()

    def open(self, writable: bool = False) -> BlockSource:
        p = self.path()
        if not p:
            raise ValueError("Seleccione un origen")
        return open_source(p, writable)


def open_source(p: str, writable: bool = False) -> BlockSource:
    if p.startswith(("/dev/", "\\\\.\\")) or writable:
        return FileSource(p, writable=writable)
    return open_image(p)


def restart_as_admin() -> bool:
    """Vuelve a lanzar la aplicación con privilegios de administrador."""
    import subprocess
    exe = sys.executable
    args = [] if getattr(sys, "frozen", False) else ["-m", "recuperadatos_mx"]
    try:
        if sys.platform == "win32":
            import ctypes
            params = " ".join(f'"{a}"' for a in args + ["gui"])
            r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
            return r > 32
        if sys.platform == "darwin":
            cmd = " ".join([f"'{exe}'"] + [f"'{a}'" for a in args] + ["gui"])
            subprocess.Popen(["osascript", "-e", f'do shell script "{cmd} &> /dev/null &" with administrator privileges'])
            return True
        env = [f"DISPLAY={os.environ.get('DISPLAY', ':0')}",
               f"XAUTHORITY={os.environ.get('XAUTHORITY', os.path.expanduser('~/.Xauthority'))}"]
        if os.environ.get("WAYLAND_DISPLAY"):
            env += [f"WAYLAND_DISPLAY={os.environ['WAYLAND_DISPLAY']}",
                    f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '')}"]
        subprocess.Popen(["pkexec", "env", *env, exe, *args, "gui"])
        return True
    except Exception:
        return False
