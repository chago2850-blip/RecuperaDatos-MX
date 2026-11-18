"""Utilidades compartidas de la interfaz: botones con tooltip, tareas en segundo
plano, selector de origen y confirmaciones de operaciones destructivas."""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
import threading
import traceback
from typing import Callable, List, Optional

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


OPT_EXE = "/opt/recuperadatos-mx/RecuperaDatos-MX"


def _linux_gui_env() -> List[str]:
    """Variables para que la app ejecutada como root pueda abrir su ventana (X11 o Wayland vía XWayland)."""
    xauth = os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority")
    if not os.path.exists(xauth):
        rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        try:
            cands = [os.path.join(rt, f) for f in os.listdir(rt) if "auth" in f.lower()]
            xauth = cands[0] if cands else xauth
        except OSError:
            pass
    return [f"DISPLAY={os.environ.get('DISPLAY', ':0')}", f"XAUTHORITY={xauth}", "QT_QPA_PLATFORM=xcb",
            "XDG_RUNTIME_DIR=/tmp/runtime-root", f"LANG={os.environ.get('LANG', 'es_MX.UTF-8')}",
            f"RECUPERADATOS_USUARIO={os.environ.get('USER', '')}"]


def elevation_command() -> Optional[List[str]]:
    """Comando que relanza la app con privilegios (Linux/macOS); None si no hay forma."""
    import shutil
    frozen = getattr(sys, "frozen", False)
    exe = sys.executable
    args = [] if frozen else ["-m", "recuperadatos_mx"]
    if sys.platform == "darwin":
        cmd = " ".join([f"'{exe}'"] + [f"'{a}'" for a in args] + ["gui"])
        return ["osascript", "-e", f'do shell script "{cmd} &> /dev/null &" with administrator privileges']
    if shutil.which("pkexec"):
        return ["pkexec", "env", *_linux_gui_env(), exe, *args, "gui"]
    return None


def sudo_askpass_command() -> Optional[List[str]]:
    """Alternativa sin agente de polkit: sudo con un diálogo de contraseña propio (SUDO_ASKPASS)."""
    import shutil
    import stat
    if not shutil.which("sudo"):
        return None
    exe = sys.executable
    args = [] if getattr(sys, "frozen", False) else ["-m", "recuperadatos_mx"]
    helper = os.path.join(tempfile.gettempdir(), f"recuperadatos-askpass-{os.getuid()}.sh")
    with open(helper, "w") as fh:
        quoted = " ".join(f"'{a}'" for a in [exe, *args])
        fh.write(f"#!/bin/sh\nexec {quoted} pedir-clave\n")
    os.chmod(helper, stat.S_IRWXU)
    return ["sudo", "-A", "env", *_linux_gui_env(), exe, *args, "gui"], helper


def restart_as_admin(window=None) -> bool:
    """Vuelve a lanzar la aplicación con privilegios de administrador.

    En Linux y macOS la ventana actual se oculta y el proceso sigue vivo mientras
    se pide la contraseña: pkexec se niega a funcionar si su proceso padre ya
    terminó.  Si se cancela, la ventana vuelve a mostrarse."""
    import subprocess
    if sys.platform == "win32":
        try:
            import ctypes
            args = [] if getattr(sys, "frozen", False) else ["-m", "recuperadatos_mx"]
            params = " ".join(f'"{a}"' for a in args + ["gui"])
            r = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
            if r > 32 and window is not None:
                window.close()
            return r > 32
        except Exception:
            return False
    attempts = []
    cmd = elevation_command()
    if cmd:
        attempts.append((cmd, {}))
    if sys.platform.startswith("linux"):
        alt = sudo_askpass_command()
        if alt:
            attempts.append((alt[0], {"SUDO_ASKPASS": alt[1]}))
    if not attempts:
        return False
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    state = {"i": 0, "proc": None, "t0": 0.0}

    def launch() -> bool:
        import time
        while state["i"] < len(attempts):
            c, extra = attempts[state["i"]]
            state["i"] += 1
            try:
                state["proc"] = subprocess.Popen(c, env=dict(os.environ, **extra), stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                state["t0"] = time.time()
                return True
            except OSError:
                continue
        return False

    if not launch():
        return False
    if window is not None:
        window.hide()
    timer = QTimer(window)

    def poll() -> None:
        import time
        p = state["proc"]
        rc = p.poll()
        if rc is None:
            return
        err = (p.stderr.read() or b"").decode("utf-8", "replace") if p.stderr else ""
        quick = time.time() - state["t0"] < 8
        if rc == 0:
            timer.stop()
            QApplication.instance().quit()  # la copia con privilegios ya se usó y se cerró
            return
        if quick and rc != 0 and ("agent" in err.lower() or rc == 127 and "dismissed" not in err.lower()) \
                and launch():
            return  # sin agente de polkit: intentar con sudo y el diálogo propio
        timer.stop()
        if window is not None:
            window.show()
            if rc not in (0, 126):
                error(window, "No se pudieron obtener privilegios de administrador.\n\n" + err[-600:] +
                      "\n\nTambién puede abrir una terminal y ejecutar:\n  sudo recuperadatos-mx")

    timer.timeout.connect(poll)
    timer.start(400)
    return True
