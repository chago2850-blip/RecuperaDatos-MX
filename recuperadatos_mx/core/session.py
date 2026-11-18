"""Guardar y reanudar sesiones de escaneo (.rdsesion = JSON comprimido con gzip)."""
from __future__ import annotations

import gzip
import json
import time
from dataclasses import asdict
from typing import List, Tuple

from .. import __version__
from .model import FileEntry
from .partitions import Partition


def save(path: str, source_desc: dict, entries: List[FileEntry], partitions: List[Partition]) -> None:
    data = {"formato": "RecuperaDatos-MX sesión", "version": __version__, "fecha": time.time(),
            "origen": source_desc, "particiones": [asdict(p) for p in partitions],
            "entradas": [e.to_dict() for e in entries]}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(data, fh)


def load(path: str) -> Tuple[dict, List[FileEntry], List[Partition]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    entries = [FileEntry.from_dict(d) for d in data.get("entradas", [])]
    parts = [Partition(**p) for p in data.get("particiones", [])]
    return data.get("origen", {}), entries, parts


def reopen_source(desc: dict):
    """Intenta volver a abrir la fuente descrita en una sesión."""
    from .device import FileSource, SliceSource, open_image
    t = desc.get("tipo")
    if t == "archivo":
        return open_image(desc["ruta"])
    if t == "region":
        parent = reopen_source(desc["padre"])
        return SliceSource(parent, desc["desplazamiento"], desc["tamano"])
    if t == "raid":
        from .raid import VirtualRAID
        return VirtualRAID.from_dict(desc)
    if t == "remoto":
        from .netagent import RemoteSource
        return RemoteSource(desc["host"], desc["puerto"], desc.get("token", ""), desc["ruta"])
    raise ValueError("Origen de sesión desconocido")
