"""Bóveda de recuperación (protección preventiva).

Indexa periódicamente las carpetas protegidas y guarda, por archivo, su ruta,
tamaño, fechas y — cuando el sistema lo permite — su ubicación física en el
disco (Linux: FIEMAP; Windows: FSCTL_GET_RETRIEVAL_POINTERS; macOS:
F_LOG2PHYS_EXT).  Si el archivo se borra después, la bóveda sabe exactamente
dónde estaban sus datos y su nombre original, lo que permite recuperarlo con
su nombre aunque el sistema de archivos ya no lo registre.
"""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import sys
import time
from typing import Callable, List, Optional, Tuple

from .model import FileEntry


def data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    p = os.path.join(base, "RecuperaDatos-MX")
    os.makedirs(p, exist_ok=True)
    return p


# --------------------------------------------------------------------------- #
# Ubicación física de un archivo
# --------------------------------------------------------------------------- #
def _linux_extents(path: str) -> Tuple[Optional[str], List[Tuple[int, int]]]:
    import fcntl
    FS_IOC_FIEMAP = 0xC020660B
    st = os.stat(path)
    dev = None
    try:
        link = os.path.realpath(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
        dev = "/dev/" + os.path.basename(link)
        start = int(open(os.path.join(link, "start")).read()) * 512 if os.path.exists(os.path.join(link, "start")) else 0
    except OSError:
        start = 0
    n = 64
    req = struct.pack("=QQIIII", 0, 0xFFFFFFFFFFFFFFFF, 1, 0, n, 0) + b"\0" * (56 * n)
    exts = []
    with open(path, "rb") as fh:
        res = fcntl.ioctl(fh.fileno(), FS_IOC_FIEMAP, bytearray(req))
    mapped = struct.unpack_from("=I", res, 20)[0]
    for i in range(mapped):
        logical, physical, length = struct.unpack_from("=QQQ", res, 32 + i * 56)
        exts.append((physical, length))
    # convertir a desplazamientos dentro del disco completo
    disk = dev
    if dev and start:
        parent = os.path.basename(os.path.dirname(link))
        disk = "/dev/" + parent
        exts = [(p + start, l) for p, l in exts]
    return disk, exts


def _windows_extents(path: str) -> Tuple[Optional[str], List[Tuple[int, int]]]:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    spc, bps, fc, tc = (wintypes.DWORD() for _ in range(4))
    k32.GetDiskFreeSpaceW(drive + "\\", ctypes.byref(spc), ctypes.byref(bps), ctypes.byref(fc), ctypes.byref(tc))
    cluster = spc.value * bps.value
    h = k32.CreateFileW(path, 0, 7, None, 3, 0x02000000, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return None, []
    try:
        inbuf = ctypes.c_longlong(0)
        size = 16 + 16 * 512
        out = ctypes.create_string_buffer(size)
        ret = wintypes.DWORD()
        ok = k32.DeviceIoControl(h, 0x00090073, ctypes.byref(inbuf), 8, out, size, ctypes.byref(ret), None)
        if not ok and ret.value == 0:
            return None, []
        count, start_vcn = struct.unpack_from("<I4xq", out.raw, 0)
        exts = []
        prev = start_vcn
        for i in range(count):
            next_vcn, lcn = struct.unpack_from("<qq", out.raw, 16 + i * 16)
            if lcn >= 0:
                exts.append((lcn * cluster, (next_vcn - prev) * cluster))
            else:
                exts.append((-1, (next_vcn - prev) * cluster))
            prev = next_vcn
        return "\\\\.\\" + drive, exts
    finally:
        k32.CloseHandle(h)


def _mac_extents(path: str) -> Tuple[Optional[str], List[Tuple[int, int]]]:
    import fcntl
    F_LOG2PHYS_EXT = 65
    size = os.path.getsize(path)
    exts = []
    with open(path, "rb") as fh:
        pos = 0
        while pos < size and len(exts) < 512:
            arg = struct.pack("=IQQ", 0, size - pos, pos)
            try:
                res = fcntl.fcntl(fh.fileno(), F_LOG2PHYS_EXT, arg)
            except OSError:
                break
            _, contig, dev_off = struct.unpack("=IQQ", res)
            if not contig:
                break
            exts.append((dev_off, contig))
            pos += contig
    st = os.stat(path)
    return None if not exts else f"dev:{st.st_dev}", exts


def physical_extents(path: str):
    try:
        if sys.platform.startswith("linux"):
            return _linux_extents(path)
        if sys.platform == "win32":
            return _windows_extents(path)
        if sys.platform == "darwin":
            return _mac_extents(path)
    except Exception:
        pass
    return None, []


# --------------------------------------------------------------------------- #
class Vault:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.path.join(data_dir(), "boveda.sqlite")
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS carpetas(ruta TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS archivos(ruta TEXT PRIMARY KEY, tamano INTEGER, mtime REAL,
                ctime REAL, dispositivo TEXT, extensiones TEXT, cabecera BLOB, visto REAL, borrado REAL);
        """)

    def folders(self) -> List[str]:
        return [r[0] for r in self.db.execute("SELECT ruta FROM carpetas ORDER BY ruta")]

    def add_folder(self, p: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO carpetas VALUES (?)", (os.path.abspath(p),))
        self.db.commit()

    def remove_folder(self, p: str) -> None:
        self.db.execute("DELETE FROM carpetas WHERE ruta=?", (p,))
        self.db.commit()

    def index(self, progress: Optional[Callable[[int, str], None]] = None, cancel=None,
              max_file: int = 8 << 30) -> dict:
        now = time.time()
        n = 0
        for root in self.folders():
            for dp, dns, fns in os.walk(root):
                if cancel and cancel():
                    break
                for fn in fns:
                    p = os.path.join(dp, fn)
                    try:
                        st = os.stat(p, follow_symlinks=False)
                        if not os.path.isfile(p) or st.st_size > max_file:
                            continue
                        old = self.db.execute("SELECT mtime, tamano FROM archivos WHERE ruta=?", (p,)).fetchone()
                        if old and old[0] == st.st_mtime and old[1] == st.st_size:
                            self.db.execute("UPDATE archivos SET visto=?, borrado=NULL WHERE ruta=?", (now, p))
                            continue
                        dev, exts = physical_extents(p)
                        with open(p, "rb") as fh:
                            head = fh.read(64)
                        self.db.execute("INSERT OR REPLACE INTO archivos VALUES (?,?,?,?,?,?,?,?,NULL)",
                                        (p, st.st_size, st.st_mtime, st.st_ctime, dev, json.dumps(exts), head, now))
                        n += 1
                        if progress and n % 50 == 0:
                            progress(n, p)
                    except OSError:
                        continue
        # marcar los que desaparecieron
        self.db.execute("UPDATE archivos SET borrado=? WHERE visto < ? AND borrado IS NULL", (now, now))
        self.db.commit()
        deleted = self.db.execute("SELECT COUNT(*) FROM archivos WHERE borrado IS NOT NULL").fetchone()[0]
        return {"indexados": n, "borrados": deleted}

    def deleted_entries(self) -> List[Tuple[str, FileEntry]]:
        out = []
        for ruta, tam, mt, ct, dev, exts, head, visto, borrado in self.db.execute(
                "SELECT * FROM archivos WHERE borrado IS NOT NULL ORDER BY borrado DESC"):
            ex = [tuple(x) for x in json.loads(exts or "[]")]
            folder, name = os.path.split(ruta)
            e = FileEntry(name=name, path="Bóveda/" + folder.replace("\\", "/").lstrip("/"), size=tam,
                          extents=ex, deleted=True, fs="Bóveda", origin="boveda", mtime=mt, ctime=ct,
                          chance="Buena" if ex else "Baja",
                          note=f"Disco: {dev or 'desconocido'} · borrado {time.strftime('%Y-%m-%d %H:%M', time.localtime(borrado))}")
            e.codec_args = {"dispositivo": dev, "cabecera": (head or b"").hex()}
            out.append((dev or "", e))
        return out

    def purge(self) -> None:
        self.db.execute("DELETE FROM archivos WHERE borrado IS NOT NULL")
        self.db.commit()


def install_autostart(enable: bool = True) -> str:
    """Programa el indexado de la bóveda al iniciar sesión."""
    exe = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" -m recuperadatos_mx'
    cmd = f"{exe} boveda --vigilar"
    if sys.platform.startswith("linux"):
        p = os.path.expanduser("~/.config/autostart/recuperadatos-mx-boveda.desktop")
        if not enable:
            if os.path.exists(p):
                os.remove(p)
            return "Protección desactivada"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(f"[Desktop Entry]\nType=Application\nName=RecuperaDatos-MX Bóveda\nExec={cmd}\n"
                     "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n")
        return p
    if sys.platform == "darwin":
        p = os.path.expanduser("~/Library/LaunchAgents/mx.recuperadatos.boveda.plist")
        if not enable:
            if os.path.exists(p):
                os.remove(p)
            return "Protección desactivada"
        args = "".join(f"<string>{a}</string>" for a in (cmd.replace('"', "").split()))
        with open(p, "w") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
                     "<key>Label</key><string>mx.recuperadatos.boveda</string>"
                     f"<key>ProgramArguments</key><array>{args}</array>"
                     "<key>RunAtLoad</key><true/></dict></plist>\n")
        return p
    if sys.platform == "win32":
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                           winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(k, "RecuperaDatosMX-Boveda", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "RecuperaDatosMX-Boveda")
            except OSError:
                pass
        return "Registro de inicio de Windows"
    return ""


def watch(interval: int = 600) -> None:
    v = Vault()
    while True:
        v.index()
        time.sleep(interval)
