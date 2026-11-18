"""Medio de arranque: descarga el ISO «RecuperaDatos-MX Live» (Debian + esta app,
arranca en BIOS y UEFI) y lo graba en una memoria USB, con verificación.

Sirve para recuperar datos de computadoras cuyo sistema ya no inicia."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Callable, Optional

from .device import FileSource, list_devices

REPO = "chago2850-blip/RecuperaDatos-MX"


def latest_iso() -> Optional[dict]:
    """Busca el ISO live en las publicaciones del repositorio."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases?per_page=10",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "RecuperaDatos-MX"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rels = json.load(r)
    for rel in rels:
        for a in rel.get("assets", []):
            if a["name"].endswith(".iso"):
                return {"nombre": a["name"], "url": a["browser_download_url"], "tamano": a["size"],
                        "version": rel.get("tag_name", "")}
    return None


def download(url: str, dest: str, progress: Optional[Callable] = None, cancel: Optional[Callable] = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "RecuperaDatos-MX"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        n = 0
        while True:
            if cancel and cancel():
                raise InterruptedError("Descarga cancelada")
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            n += len(chunk)
            if progress:
                progress(n, total or n)
    os.replace(dest + ".part", dest)
    return dest


def usb_targets():
    """Discos extraíbles candidatos (nunca el disco del sistema)."""
    out = []
    for d in list_devices():
        if d.removable or any(c.mountpoint.startswith(("/media", "/run/media", "/Volumes")) for c in d.children):
            if any(c.mountpoint in ("/", "C:\\") for c in d.children):
                continue
            out.append(d)
    return out


def _prepare(dev_path: str):
    """Desmonta/bloquea los volúmenes del disco destino; devuelve manejadores a cerrar."""
    if sys.platform.startswith("linux"):
        base = os.path.basename(dev_path)
        with open("/proc/self/mounts") as fh:
            for line in fh:
                src, mp = line.split()[:2]
                if os.path.basename(src).startswith(base):
                    subprocess.run(["umount", src], capture_output=True)
        return []
    if sys.platform == "darwin":
        subprocess.run(["diskutil", "unmountDisk", dev_path.replace("/dev/rdisk", "/dev/disk")], capture_output=True)
        return []
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        n = int(dev_path.split("PhysicalDrive")[-1])
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        handles = []
        ps = f"(Get-Partition -DiskNumber {n} | Where-Object DriveLetter).DriveLetter"
        letters = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 creationflags=0x08000000).stdout.split()
        for L in letters:
            h = k32.CreateFileW(f"\\\\.\\{L}:", 0xC0000000, 3, None, 3, 0, None)
            ret = wintypes.DWORD()
            k32.DeviceIoControl(h, 0x00090018, None, 0, None, 0, ctypes.byref(ret), None)  # LOCK
            k32.DeviceIoControl(h, 0x00090020, None, 0, None, 0, ctypes.byref(ret), None)  # DISMOUNT
            handles.append(h)
        return handles
    return []


def write_iso(iso: str, dev_path: str, progress: Optional[Callable] = None,
              cancel: Optional[Callable] = None, verify: bool = True) -> dict:
    size = os.path.getsize(iso)
    handles = _prepare(dev_path)
    try:
        dst = FileSource(dev_path, writable=True)
        if dst.size and dst.size < size:
            raise ValueError("La memoria USB es más pequeña que el ISO")
        h = hashlib.sha256()
        t0 = time.time()
        B = 4 << 20
        with open(iso, "rb") as fh:
            pos = 0
            while pos < size:
                if cancel and cancel():
                    raise InterruptedError("Grabación cancelada")
                data = fh.read(B)
                pad = data + b"\0" * ((-len(data)) % 512)
                dst.write(pos, pad)
                h.update(data)
                pos += len(data)
                if progress:
                    progress(pos, size * (2 if verify else 1), "Grabando")
        try:
            os.fsync(dst.f.fileno())
        except OSError:
            pass
        ok = True
        if verify:
            v = hashlib.sha256()
            pos = 0
            while pos < size:
                n = min(B, size - pos)
                v.update(dst.read(pos, n))
                pos += n
                if progress:
                    progress(size + pos, size * 2, "Verificando")
            ok = v.hexdigest() == h.hexdigest()
        dst.close()
        return {"ok": ok, "sha256": h.hexdigest(), "segundos": time.time() - t0}
    finally:
        if sys.platform == "win32":
            import ctypes
            for hd in handles:
                ctypes.windll.kernel32.CloseHandle(hd)
