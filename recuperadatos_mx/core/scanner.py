"""Orquestación de escaneos: rápido (sistemas de archivos), profundo (sistemas de
archivos + particiones perdidas + registros huérfanos + firmas)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import carving
from .device import BlockSource
from .fs import detect, open_fs
from .model import EntrySource, FileEntry, read_head
from .partitions import Partition, read_table, search_lost

Progress = Callable[[str, int, int], None]


@dataclass
class ScanOptions:
    deep: bool = False
    filesystems: bool = True
    lost_partitions: bool = True
    orphan_records: bool = True
    carving: bool = True
    aligned: bool = True
    signatures: Optional[List[carving.Signature]] = None
    start: int = 0
    end: Optional[int] = None


@dataclass
class ScanResult:
    entries: List[FileEntry] = field(default_factory=list)
    partitions: List[Partition] = field(default_factory=list)
    log: List[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    bad_bytes: int = 0


def _volume_label(p: Partition) -> str:
    if p.index == 0:
        return f"{p.fs} (volumen completo)"
    lab = f"Partición {p.index} ({p.fs or p.type_name})"
    if p.found:
        lab = f"Partición perdida {p.index} ({p.fs})"
    if p.label:
        lab += f" «{p.label}»"
    return lab


def _scan_volume(src: BlockSource, base: int, fs_kind: str, root: str, res: ScanResult,
                 progress: Optional[Progress], cancel) -> List[FileEntry]:
    try:
        parser = open_fs(src, base, fs_kind)
    except Exception as exc:  # estructura dañada
        res.log.append(f"{root}: no se pudo leer {fs_kind}: {exc}")
        return []
    if parser is None:
        res.log.append(f"{root}: {fs_kind} se recupera mediante escaneo por firmas")
        return []
    try:
        ents = parser.entries(progress=(lambda d, t: progress(f"Leyendo {fs_kind} — {root}", d, t))
                              if progress else None, cancel=cancel)
    except Exception as exc:
        res.log.append(f"{root}: error al recorrer {fs_kind}: {exc}")
        return []
    for e in ents:
        e.path = root + ("/" + e.path if e.path else "")
        if e.name.startswith("inodo_") and not e.is_dir and "." not in e.name:
            try:
                ext = carving.identify(read_head(src, e, 4096), EntrySource(src, e))
                if ext:
                    e.name += "." + ext
            except Exception:
                pass
    res.log.append(f"{root}: {len(ents)} elementos ({sum(1 for e in ents if e.deleted)} borrados)")
    return ents


def scan(src: BlockSource, opts: ScanOptions, progress: Optional[Progress] = None,
         cancel: Optional[Callable[[], bool]] = None,
         on_batch: Optional[Callable[[List[FileEntry]], None]] = None) -> ScanResult:
    res = ScanResult()
    cancel = cancel or (lambda: False)

    def emit(batch):
        res.entries.extend(batch)
        if on_batch and batch:
            on_batch(batch)

    # 1. Volúmenes conocidos
    parts = read_table(src)
    volumes: List[Partition] = list(parts)
    if not parts:
        fs0 = detect(src, 0)
        if fs0:
            volumes = [Partition(0, 0, src.size, fs0, fs=fs0, scheme="volumen")]
    res.partitions = list(volumes)
    if opts.filesystems:
        for p in volumes:
            if cancel():
                break
            if p.fs:
                root = _volume_label(p)
                emit(_scan_volume(src, p.start, p.fs, root, res, progress, cancel))

    if not opts.deep or cancel():
        res.finished = time.time()
        return res

    # 2. Particiones perdidas
    if opts.lost_partitions:
        found = search_lost(src, progress=(lambda d, t: progress("Buscando particiones perdidas", d, t))
                            if progress else None, cancel=cancel)
        known = {p.start for p in volumes}
        for p in found:
            if p.start in known or cancel():
                continue
            res.partitions.append(p)
            emit(_scan_volume(src, p.start, p.fs, _volume_label(p), res, progress, cancel))

    # 3. Registros MFT huérfanos (MFT dañada o volumen formateado)
    if opts.orphan_records and not cancel():
        from .fs.ntfs import NTFS, scan_orphan_records
        for p in [p for p in res.partitions if p.fs == "NTFS"]:
            try:
                ntfs = NTFS(src, p.start)
            except Exception:
                continue
            recs = scan_orphan_records(src, p.start, p.end, ntfs.rec_size, ntfs.bps,
                                       progress=(lambda d, t: progress("Reconstruyendo NTFS desde registros MFT", d, t))
                                       if progress else None, cancel=cancel)
            known_ids = {e.ident for e in res.entries if e.fs == "NTFS" and e.path.startswith(_volume_label(p))}
            extra = [e for e in ntfs.entries(records=recs) if e.ident not in known_ids]
            root = _volume_label(p) + " · reconstruida"
            for e in extra:
                e.path = root + ("/" + e.path if e.path else "")
                e.origin = "ntfs-huerfano"
                e.deleted = True
                if e.chance == "Excelente" and not e.resident:
                    e.chance = "Buena"
            if extra:
                res.log.append(f"{root}: {len(extra)} registros adicionales")
            emit(extra)

    # 4. Firmas
    if opts.carving and not cancel():
        known_starts = {e.extents[0][0] for e in res.entries if e.extents and e.extents[0][0] >= 0}
        carver = carving.Carver(opts.signatures, aligned=opts.aligned)
        batch: List[FileEntry] = []
        last = [time.time()]

        def found(e: FileEntry):
            if e.extents[0][0] in known_starts:
                return
            batch.append(e)
            if time.time() - last[0] > 0.5:
                emit(list(batch))
                batch.clear()
                last[0] = time.time()

        carver.scan(src, opts.start, opts.end,
                    progress=(lambda d, t: progress("Escaneo profundo por firmas", d, t)) if progress else None,
                    cancel=cancel, on_found=found)
        emit(list(batch))
    res.bad_bytes = getattr(getattr(src, "errors", None), "bad_bytes", 0)
    res.finished = time.time()
    return res
