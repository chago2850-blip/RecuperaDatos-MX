"""Guardado de archivos recuperados, protección contra sobrescritura del origen e informes."""
from __future__ import annotations

import csv
import hashlib
import os
import re
import sys
import time
from typing import Callable, Iterable, List, Optional

from .device import BlockSource, human
from .model import FileEntry, iter_content

_BAD = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    name = _BAD.sub("_", name).strip().rstrip(".")
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                       *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    return name[:240] or "sin_nombre"


def same_device(source_path: str, dest_dir: str) -> bool:
    """True si la carpeta destino está en el mismo disco/partición que el origen."""
    try:
        if sys.platform == "win32":
            m = re.match(r"\\\\\.\\([A-Za-z]):", source_path)
            if m:
                return os.path.splitdrive(os.path.abspath(dest_dir))[0].upper() == m.group(1).upper() + ":"
            if "PhysicalDrive" in source_path:
                import subprocess
                n = source_path.split("PhysicalDrive")[-1]
                letter = os.path.splitdrive(os.path.abspath(dest_dir))[0][:1]
                out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                      f"(Get-Partition -DriveLetter {letter}).DiskNumber"],
                                     capture_output=True, text=True, timeout=15,
                                     creationflags=0x08000000).stdout.strip()
                return out == n
            return False
        if not source_path.startswith("/dev/"):
            return os.path.realpath(os.path.dirname(source_path)) == os.path.realpath(dest_dir) and False
        st_dev = os.stat(dest_dir).st_dev
        rdev = os.stat(source_path.replace("/dev/rdisk", "/dev/disk")).st_rdev
        if st_dev == rdev:
            return True
        # disco completo: comparar el número mayor (Linux) o el prefijo (macOS)
        if sys.platform.startswith("linux"):
            dev_name = os.path.basename(os.path.realpath(source_path))
            maj, minr = os.major(st_dev), os.minor(st_dev)
            link = os.path.realpath(f"/sys/dev/block/{maj}:{minr}")
            return f"/{dev_name}/" in link + "/" or link.endswith("/" + dev_name)
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["df", dest_dir], capture_output=True, text=True).stdout
            disk = os.path.basename(source_path).replace("rdisk", "disk")
            return f"/dev/{disk}" in out
    except Exception:
        return False
    return False


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def recover(src: BlockSource, entries: Iterable[FileEntry], dest: str, keep_structure: bool = True,
            progress: Optional[Callable[[int, int, str], None]] = None,
            cancel: Optional[Callable[[], bool]] = None, write_report: bool = True,
            compute_hash: bool = False) -> dict:
    files = [e for e in entries if not e.is_dir]
    total = sum(e.size for e in files) or 1
    done = 0
    ok = errors = 0
    rows = []
    os.makedirs(dest, exist_ok=True)
    t0 = time.time()
    for e in files:
        if cancel and cancel():
            break
        rel = "/".join(safe_name(p) for p in e.path.split("/") if p) if keep_structure else ""
        folder = os.path.join(dest, *rel.split("/")) if rel else dest
        os.makedirs(folder, exist_ok=True)
        out_path = unique_path(os.path.join(folder, safe_name(e.name)))
        h = hashlib.sha256() if compute_hash else None
        try:
            with open(out_path, "wb") as fh:
                for chunk in iter_content(src, e):
                    fh.write(chunk)
                    if h:
                        h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total, e.name)
            ts = e.mtime or e.ctime
            if ts:
                try:
                    os.utime(out_path, (e.atime or ts, ts))
                except (OSError, OverflowError, ValueError):
                    pass
            ok += 1
            rows.append([e.full_path, out_path, e.size, "Recuperado", e.chance, e.origin, h.hexdigest() if h else ""])
        except OSError as exc:
            errors += 1
            rows.append([e.full_path, out_path, e.size, f"Error: {exc}", e.chance, e.origin, ""])
    if write_report:
        rp = unique_path(os.path.join(dest, "informe_recuperacion.csv"))
        with open(rp, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["Ruta original", "Guardado en", "Tamaño (bytes)", "Estado", "Probabilidad", "Origen", "SHA-256"])
            w.writerows(rows)
    return {"recuperados": ok, "errores": errors, "bytes": done, "segundos": time.time() - t0,
            "resumen": f"{ok} archivos recuperados ({human(done)}), {errors} errores"}
