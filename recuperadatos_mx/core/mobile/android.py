"""Android por ADB: dispositivos conectados, papelera del sistema (archivos
«.trashed-*» de Android 11+), miniaturas de fotos borradas, extracción de
carpetas (DCIM, WhatsApp, descargas…) e imagen completa de particiones en
teléfonos con root para escanearla con el motor de recuperación.

Requiere Android Platform Tools (adb) y la depuración USB activada."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Callable, List, Optional, Tuple

FOLDERS = {
    "Cámara (DCIM)": "/sdcard/DCIM",
    "Imágenes": "/sdcard/Pictures",
    "WhatsApp": "/sdcard/Android/media/com.whatsapp/WhatsApp",
    "WhatsApp (antiguo)": "/sdcard/WhatsApp",
    "Telegram": "/sdcard/Android/media/org.telegram.messenger",
    "Descargas": "/sdcard/Download",
    "Documentos": "/sdcard/Documents",
    "Música": "/sdcard/Music",
    "Grabaciones": "/sdcard/Recordings",
    "Miniaturas (.thumbnails)": "/sdcard/DCIM/.thumbnails",
}


def adb_path() -> Optional[str]:
    p = shutil.which("adb")
    if p:
        return p
    home = os.path.expanduser("~")
    for c in (os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
              os.path.join(home, "Android", "Sdk", "platform-tools", "adb"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
              r"C:\platform-tools\adb.exe", "/opt/homebrew/bin/adb", "/usr/local/bin/adb"):
        if os.path.exists(c):
            return c
    return None


def _run(args: List[str], timeout: int = 60, binary: bool = False):
    exe = adb_path()
    if not exe:
        raise FileNotFoundError("No se encontró adb. Instale Android Platform Tools "
                                "(developer.android.com/tools/releases/platform-tools).")
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    r = subprocess.run([exe] + args, capture_output=True, timeout=timeout, **kw)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def devices() -> List[Tuple[str, str]]:
    out = []
    for line in _run(["devices", "-l"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            model = next((p.split(":", 1)[1] for p in parts if p.startswith("model:")), "")
            out.append((parts[0], f"{model or parts[0]} ({parts[1]})"))
    return out


def shell(serial: str, cmd: str, timeout: int = 120) -> str:
    return _run(["-s", serial, "shell", cmd], timeout)


def is_rooted(serial: str) -> bool:
    return "uid=0" in shell(serial, "su -c id 2>/dev/null")


def trashed(serial: str) -> List[Tuple[str, int]]:
    """Archivos en la papelera de Android 11+ (se borran solos tras 30 días)."""
    res = shell(serial, "find /sdcard -name '.trashed-*' -type f -exec stat -c '%s %n' {} + 2>/dev/null", 300)
    out = []
    for line in res.splitlines():
        sz, _, path = line.partition(" ")
        if path and sz.isdigit():
            out.append((path, int(sz)))
    return out


def original_name(trashed_path: str) -> str:
    base = os.path.basename(trashed_path)
    parts = base.split("-", 2)
    return parts[2] if len(parts) == 3 else base


def pull(serial: str, remote: str, local: str) -> bool:
    os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
    out = _run(["-s", serial, "pull", "-a", remote, local], timeout=3600)
    return "error" not in out.lower()


def pull_folders(serial: str, names: List[str], dest: str, progress: Optional[Callable] = None) -> List[str]:
    done = []
    for i, n in enumerate(names):
        remote = FOLDERS[n]
        if progress:
            progress(i, len(names), n)
        if pull(serial, remote, os.path.join(dest, n)):
            done.append(n)
    return done


def recover_trash(serial: str, dest: str, progress: Optional[Callable] = None) -> int:
    items = trashed(serial)
    n = 0
    for i, (p, _) in enumerate(items):
        local = os.path.join(dest, "Papelera de Android", original_name(p))
        if pull(serial, p, local):
            n += 1
        if progress:
            progress(i + 1, len(items), original_name(p))
    return n


def partitions(serial: str) -> List[Tuple[str, int]]:
    res = shell(serial, "su -c 'for p in /dev/block/by-name/*; do echo $p $(blockdev --getsize64 $p 2>/dev/null); done'")
    out = []
    for line in res.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out.append((parts[0], int(parts[1])))
    return out


def image_partition(serial: str, block: str, dest: str, progress: Optional[Callable] = None,
                    cancel: Optional[Callable] = None) -> int:
    """Copia byte a byte una partición del teléfono (requiere root) a un archivo de imagen."""
    exe = adb_path()
    if not exe:
        raise FileNotFoundError("No se encontró adb")
    total = next((s for b, s in partitions(serial) if b == block), 0)
    proc = subprocess.Popen([exe, "-s", serial, "exec-out", f"su -c 'dd if={block} bs=4M 2>/dev/null'"],
                            stdout=subprocess.PIPE)
    n = 0
    with open(dest, "wb") as fh:
        while True:
            if cancel and cancel():
                proc.kill()
                break
            chunk = proc.stdout.read(4 << 20)
            if not chunk:
                break
            fh.write(chunk)
            n += len(chunk)
            if progress:
                progress(n, total or n, block)
    proc.wait()
    return n
