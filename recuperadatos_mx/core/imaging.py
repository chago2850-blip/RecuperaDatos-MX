"""Copias byte a byte: crear imágenes, clonar discos y restaurar imágenes, con
manejo de sectores dañados, copia inversa, reanudación y verificación por hash."""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Callable, List, Optional

from .device import BlockSource, FileSource

Progress = Callable[[int, int, dict], None]


def _mapfile(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def copy(src: BlockSource, write: Callable[[int, bytes], None], start: int = 0, end: Optional[int] = None,
         block: int = 1 << 20, reverse: bool = False, progress: Optional[Progress] = None,
         cancel: Optional[Callable[[], bool]] = None, map_path: Optional[str] = None,
         hashes=("md5", "sha256"), skip_on_error: int = 0) -> dict:
    """Copia ``src`` a través de ``write(offset, datos)``.

    * ``map_path``: archivo de control para reanudar copias interrumpidas.
    * ``skip_on_error``: bytes a saltar tras un bloque dañado (como ddrescue), 0 = no saltar.
    """
    end = end or src.size
    state = _mapfile(map_path) if map_path else {}
    pos = state.get("pos", end if reverse else start) if state.get("reverse") == reverse else (end if reverse else start)
    bad: List[list] = state.get("bad", [])
    hs = {h: hashlib.new(h) for h in hashes} if not reverse and pos == start else {}
    t0 = time.time()
    copied = 0
    errors = getattr(src, "errors", None)

    def save_map():
        if map_path:
            with open(map_path, "w", encoding="utf-8") as fh:
                json.dump({"pos": pos, "reverse": reverse, "bad": bad, "start": start, "end": end}, fh)

    last_save = time.time()
    while (pos > start) if reverse else (pos < end):
        if cancel and cancel():
            save_map()
            return {"estado": "cancelado", "pos": pos, "malos": bad}
        if reverse:
            n = min(block, pos - start)
            off = pos - n
        else:
            n = min(block, end - pos)
            off = pos
        before = len(errors.bad) if errors else 0
        data = src.read(off, n)
        if len(data) < n:
            data = data.ljust(n, b"\0")
        if errors and len(errors.bad) > before:
            for o, l in errors.bad[before:]:
                bad.append([o, l])
        write(off, data)
        for h in hs.values():
            h.update(data)
        copied += n
        pos = off if reverse else off + n
        if skip_on_error and errors and len(errors.bad) > before:
            pos = max(start, pos - skip_on_error) if reverse else min(end, pos + skip_on_error)
        if progress:
            el = max(1e-6, time.time() - t0)
            progress(copied, end - start, {"velocidad": copied / el, "malos": len(bad), "pos": pos})
        if time.time() - last_save > 5:
            save_map()
            last_save = time.time()
    save_map()
    return {"estado": "completado", "bytes": copied, "malos": bad,
            "hashes": {k: v.hexdigest() for k, v in hs.items()}, "segundos": time.time() - t0}


def create_image(src: BlockSource, dest: str, split_size: int = 0, **kw) -> dict:
    """Crea una imagen RAW (.img/.dd); con ``split_size`` genera partes .001, .002 …"""
    handles = {}

    def target(off):
        if not split_size:
            idx, inner = 0, off
            name = dest
        else:
            idx, inner = divmod(off, split_size)
            name = f"{os.path.splitext(dest)[0]}.{idx + 1:03d}"
        fh = handles.get(name)
        if fh is None:
            mode = "r+b" if os.path.exists(name) else "wb"
            fh = open(name, mode)
            handles[name] = fh
        return fh, inner

    def write(off, data):
        while data:
            fh, inner = target(off)
            n = len(data) if not split_size else min(len(data), split_size - inner)
            fh.seek(inner)
            fh.write(data[:n])
            data = data[n:]
            off += n

    try:
        res = copy(src, write, map_path=kw.pop("map_path", dest + ".mapa.json"), **kw)
    finally:
        for fh in handles.values():
            fh.close()
    if res.get("estado") == "completado":
        info = {"origen": src.describe(), "tamano": src.size, "hashes": res.get("hashes", {}),
                "sectores_danados": res["malos"], "fecha": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(dest + ".info.json", "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=2, ensure_ascii=False)
    return res


def clone_to_device(src: BlockSource, dest_path: str, **kw) -> dict:
    dst = FileSource(dest_path, writable=True)
    if dst.size and dst.size < src.size:
        dst.close()
        raise ValueError("El destino es más pequeño que el origen")
    try:
        return copy(src, dst.write, **kw)
    finally:
        dst.close()


def verify(src: BlockSource, algo: str = "sha256", progress: Optional[Progress] = None,
           cancel=None) -> str:
    h = hashlib.new(algo)
    pos = 0
    B = 4 << 20
    while pos < src.size:
        if cancel and cancel():
            return ""
        d = src.read(pos, min(B, src.size - pos))
        h.update(d)
        pos += len(d) or B
        if progress:
            progress(pos, src.size, {})
    return h.hexdigest()
