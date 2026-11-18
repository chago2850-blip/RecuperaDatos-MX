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
    passwords: List[str] = field(default_factory=list)   # contraseñas / claves de recuperación
    bek_files: List[bytes] = field(default_factory=list)  # archivos .BEK de BitLocker


@dataclass
class ScanResult:
    entries: List[FileEntry] = field(default_factory=list)
    partitions: List[Partition] = field(default_factory=list)
    log: List[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    bad_bytes: int = 0
    locked: List[tuple] = field(default_factory=list)      # volúmenes cifrados sin desbloquear
    decrypted: List[BlockSource] = field(default_factory=list)  # fuentes descifradas usadas


def _volume_label(p: Partition) -> str:
    if p.index == 0:
        return f"{p.fs} (volumen completo)"
    lab = f"Partición {p.index} ({p.fs or p.type_name})"
    if p.found:
        lab = f"Partición perdida {p.index} ({p.fs})"
    if p.label:
        lab += f" «{p.label}»"
    return lab


_FAMILY = {"docx": "zip", "xlsx": "zip", "pptx": "zip", "odt": "zip", "ods": "zip", "odp": "zip", "epub": "zip",
           "jar": "zip", "apk": "zip", "jpeg": "jpg", "jpe": "jpg", "tiff": "tif", "xls": "doc", "ppt": "doc",
           "msg": "doc", "mov": "mp4", "m4a": "mp4", "m4v": "mp4", "heic": "mp4", "3gp": "mp4", "avi": "wav",
           "webp": "wav", "htm": "html", "dll": "exe", "sys": "exe", "tgz": "gz", "cr2": "tif", "nef": "tif",
           "arw": "tif", "dng": "tif", "webm": "mkv"}


def _verify_deleted(src: BlockSource, ents: List[FileEntry]) -> None:
    """Comprueba el contenido de los borrados: ceros (TRIM) o cabecera ajena = clústeres reutilizados."""
    checked = 0
    for e in ents:
        if not e.deleted or e.is_dir or not e.size or checked > 20000:
            continue
        checked += 1
        try:
            head = read_head(src, e, 4096)
        except Exception:
            continue
        if not head.strip(b"\0"):
            e.chance = "Baja"
            e.note = "El contenido está en ceros: el sistema liberó los clústeres (TRIM) o fueron sobrescritos"
            continue
        if head[:4] in (b"MSB+", b"FILE", b"INDX") or head[32:36] == b"NXSB":
            e.chance = "Baja"
            e.note = "Los clústeres fueron reutilizados por metadatos del sistema de archivos"
            continue
        want = _FAMILY.get(e.ext, e.ext)
        got = carving.identify(head)
        if got and want in {s.ext for s in carving.SIGNATURES} and _FAMILY.get(got, got) != want:
            e.chance = "Baja"
            e.note = f"El contenido actual parece {got.upper()}: los clústeres fueron reutilizados"


def unlock_volume(src: BlockSource, base: int, size: int, kind: str, passwords: List[str],
                  bek_files: List[bytes] = ()) -> Optional[BlockSource]:
    """Intenta abrir un volumen BitLocker o LUKS con las contraseñas dadas."""
    from .device import SliceSource
    part = SliceSource(src, base, size) if base or size != src.size else src
    if kind == "BitLocker":
        from .crypto.bitlocker import BitLockerVolume
        vol = BitLockerVolume(part)
        for pw in list(passwords) + [""]:
            for bek in list(bek_files) + [None]:
                if not pw and bek is None and "Clave sin protección" not in vol.protectors():
                    continue
                try:
                    return vol.open(pw, bek)
                except PermissionError:
                    continue
        return None
    if kind == "LUKS":
        from .crypto.luks import LUKSVolume
        vol = LUKSVolume(part)
        for pw in passwords:
            try:
                return vol.open(pw)
            except PermissionError:
                continue
    return None


def _scan_volume(src: BlockSource, base: int, fs_kind: str, root: str, res: ScanResult,
                 progress: Optional[Progress], cancel, opts: Optional[ScanOptions] = None,
                 size: int = 0) -> List[FileEntry]:
    if fs_kind in ("BitLocker", "LUKS"):
        dec = None
        try:
            dec = unlock_volume(src, base, size or (src.size - base), fs_kind,
                                opts.passwords if opts else [], opts.bek_files if opts else [])
        except Exception as exc:
            res.log.append(f"{root}: error al abrir {fs_kind}: {exc}")
        if dec is None:
            res.log.append(f"{root}: volumen {fs_kind} cifrado — indique la contraseña o clave de recuperación")
            res.locked.append((root, fs_kind, base))
            return []
        inner = detect(dec, 0)
        res.log.append(f"{root}: {fs_kind} desbloqueado ({inner or 'sistema desconocido'})")
        res.decrypted.append(dec)
        if not inner:
            return []
        ents = _scan_volume(dec, 0, inner, root + f" · {fs_kind} descifrado", res, progress, cancel, opts)
        for e in ents:
            if getattr(e, "_src", None) is None:
                e._src = dec  # el contenido se lee del volumen descifrado
        return ents
    if fs_kind == "APFS" and opts and opts.passwords:
        from .fs.apfs import APFS
        APFS.password_list = list(opts.passwords)
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
    _verify_deleted(src, ents)
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
                emit(_scan_volume(src, p.start, p.fs, root, res, progress, cancel, opts, p.size))

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
            emit(_scan_volume(src, p.start, p.fs, _volume_label(p), res, progress, cancel, opts, p.size))

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

    # 4. Firmas (en el disco y dentro de cada volumen descifrado)
    if opts.carving and not cancel():
        targets = [(src, "")] + [(d, f"Volumen descifrado {i + 1}/") for i, d in enumerate(res.decrypted)]
        for tsrc, prefix in targets:
            if cancel():
                break
            known_starts = {e.extents[0][0] for e in res.entries
                            if e.extents and e.extents[0][0] >= 0 and getattr(e, "_src", src) is tsrc}
            carver = carving.Carver(opts.signatures, aligned=opts.aligned)
            batch: List[FileEntry] = []
            last = [time.time()]

            def found(e: FileEntry, tsrc=tsrc, prefix=prefix, known=known_starts, batch=batch):
                if e.extents[0][0] in known:
                    return
                if tsrc is not src:
                    e._src = tsrc
                    e.path = prefix + e.path
                batch.append(e)
                if time.time() - last[0] > 0.5:
                    emit(list(batch))
                    batch.clear()
                    last[0] = time.time()

            carver.scan(tsrc, opts.start if tsrc is src else 0, opts.end if tsrc is src else None,
                        progress=(lambda d, t: progress("Escaneo profundo por firmas", d, t)) if progress else None,
                        cancel=cancel, on_found=found)
            emit(list(batch))
    res.bad_bytes = getattr(getattr(src, "errors", None), "bad_bytes", 0)
    res.finished = time.time()
    return res


def find_encrypted(src: BlockSource) -> List[tuple]:
    """Volúmenes cifrados del origen: [(descripción, tipo)] — BitLocker, LUKS y APFS/FileVault."""
    out = []
    parts = read_table(src)
    if not parts:
        fs0 = detect(src, 0)
        parts = [Partition(0, 0, src.size, fs0 or "", fs=fs0, scheme="volumen")] if fs0 else []
    for p in parts:
        if p.fs in ("BitLocker", "LUKS"):
            out.append((_volume_label(p), p.fs))
        elif p.fs == "APFS":
            try:
                from .fs.apfs import APFSContainer
                for v in APFSContainer(src, p.start).volumes():
                    if v.encrypted:
                        out.append((f"Volumen APFS «{v.name}»", "FileVault"))
            except Exception:
                pass
    return out
