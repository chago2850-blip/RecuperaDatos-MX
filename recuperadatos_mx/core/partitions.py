"""Tablas de particiones MBR/GPT, búsqueda de particiones perdidas y restauración."""
from __future__ import annotations

import os
import struct
import time
import uuid
import zlib
from dataclasses import dataclass
from typing import Callable, List, Optional

from .device import BlockSource, SliceSource
from .fs import detect, u16, u32, u64

MBR_TYPES = {0x01: "FAT12", 0x04: "FAT16", 0x06: "FAT16", 0x07: "NTFS/exFAT", 0x0B: "FAT32",
             0x0C: "FAT32 LBA", 0x0E: "FAT16 LBA", 0x0F: "Extendida LBA", 0x05: "Extendida",
             0x82: "Linux swap", 0x83: "Linux", 0x8E: "Linux LVM", 0xA5: "FreeBSD", 0xAF: "HFS+",
             0xEE: "GPT protector", 0xFD: "Linux RAID", 0x27: "Recuperación Windows"}

GPT_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "Sistema EFI",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Reservada Microsoft",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Datos básicos Windows",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Recuperación Windows",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Datos Linux",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
    "426f6f74-0000-11aa-aa11-00306543ecac": "Apple Boot",
}
FS_TO_GPT = {"NTFS": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7", "FAT": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
             "exFAT": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7", "ext2": "0fc63daf-8483-4772-8e79-3d69d8477de4",
             "ext3": "0fc63daf-8483-4772-8e79-3d69d8477de4", "ext4": "0fc63daf-8483-4772-8e79-3d69d8477de4",
             "Btrfs": "0fc63daf-8483-4772-8e79-3d69d8477de4", "XFS": "0fc63daf-8483-4772-8e79-3d69d8477de4",
             "HFS+": "48465300-0000-11aa-aa11-00306543ecac", "APFS": "7c3457ef-0000-11aa-aa11-00306543ecac"}
FS_TO_MBR = {"NTFS": 0x07, "exFAT": 0x07, "FAT": 0x0C, "ext2": 0x83, "ext3": 0x83, "ext4": 0x83,
             "Btrfs": 0x83, "XFS": 0x83, "HFS+": 0xAF, "APFS": 0xAF}


@dataclass
class Partition:
    index: int
    start: int          # bytes
    size: int           # bytes
    type_name: str
    fs: Optional[str] = None
    scheme: str = "MBR"
    label: str = ""
    found: bool = False  # True si proviene de la búsqueda de particiones perdidas
    confidence: str = ""

    @property
    def end(self) -> int:
        return self.start + self.size


def _mbr_entries(sec: bytes):
    for i in range(4):
        e = sec[446 + i * 16: 462 + i * 16]
        ptype = e[4]
        lba = u32(e, 8)
        cnt = u32(e, 12)
        if ptype and cnt:
            yield i, ptype, lba, cnt


def read_table(src: BlockSource) -> List[Partition]:
    ss = src.sector_size
    out: List[Partition] = []
    sec = src.read(0, ss)
    if len(sec) < 512 or sec[510:512] != b"\x55\xAA":
        return out
    entries = list(_mbr_entries(sec))
    if any(t == 0xEE for _, t, _, _ in entries):
        gpt = read_gpt(src)
        if gpt:
            return gpt
    idx = 1
    for i, t, lba, cnt in entries:
        if t in (0x05, 0x0F, 0x85):
            ebr_base = lba
            cur = lba
            guard = 0
            while cur and guard < 128:
                guard += 1
                ebr = src.read(cur * ss, ss)
                if ebr[510:512] != b"\x55\xAA":
                    break
                ents = list(_mbr_entries(ebr))
                if not ents:
                    break
                _, t2, l2, c2 = ents[0]
                p = Partition(idx, (cur + l2) * ss, c2 * ss, MBR_TYPES.get(t2, f"0x{t2:02X}"), scheme="MBR lógica")
                p.fs = detect(src, p.start)
                out.append(p)
                idx += 1
                nxt = [e for e in ents[1:] if e[1] in (0x05, 0x0F, 0x85)]
                cur = ebr_base + nxt[0][2] if nxt else 0
            continue
        p = Partition(idx, lba * ss, cnt * ss, MBR_TYPES.get(t, f"0x{t:02X}"))
        p.fs = detect(src, p.start)
        out.append(p)
        idx += 1
    return out


def read_gpt(src: BlockSource, backup: bool = False) -> List[Partition]:
    ss = src.sector_size
    hdr_lba = (src.size // ss - 1) if backup else 1
    h = src.read(hdr_lba * ss, ss)
    if h[:8] != b"EFI PART":
        if not backup:
            return read_gpt(src, backup=True)
        return []
    ent_lba = u64(h, 72)
    n = u32(h, 80)
    esz = u32(h, 84)
    raw = src.read(ent_lba * ss, n * esz)
    out = []
    for i in range(n):
        e = raw[i * esz:(i + 1) * esz]
        if len(e) < 128 or e[:16] == b"\0" * 16:
            continue
        tguid = str(uuid.UUID(bytes_le=e[:16]))
        first = u64(e, 32)
        last = u64(e, 40)
        name = e[56:128].decode("utf-16-le", "ignore").split("\0")[0]
        p = Partition(i + 1, first * ss, (last - first + 1) * ss, GPT_TYPES.get(tguid, tguid),
                      scheme="GPT" + (" (copia)" if backup else ""), label=name)
        p.fs = detect(src, p.start)
        out.append(p)
    return out


def _fs_size(src: BlockSource, start: int, fs: str) -> int:
    b = src.read(start, 4096)
    try:
        if fs == "NTFS":
            return u64(b, 0x28) * u16(b, 0x0B) + u16(b, 0x0B)
        if fs == "exFAT":
            return u64(b, 72) << b[108]
        if fs == "FAT":
            return (u16(b, 19) or u32(b, 32)) * u16(b, 11)
        if fs.startswith("ext"):
            sb = b[1024:2048]
            return u32(sb, 4) * (1024 << u32(sb, 0x18))
        if fs == "HFS+":
            return struct.unpack_from(">I", b, 1024 + 0x28)[0] * struct.unpack_from(">I", b, 1024 + 0x2C)[0]
        if fs == "APFS":
            return u32(b, 36) * u64(b, 40)
        if fs == "XFS":
            return struct.unpack_from(">I", b, 4)[0] * struct.unpack_from(">Q", b, 8)[0]
        if fs == "Btrfs":
            s = src.read(start + 65536, 4096)
            return u64(s, 0x70)
    except struct.error:
        pass
    return 0


def search_lost(src: BlockSource, step: int = 512, start: int = 0, end: Optional[int] = None,
                progress: Optional[Callable] = None, cancel: Optional[Callable] = None,
                quick: bool = True) -> List[Partition]:
    """Busca sectores de arranque / superbloques en toda la superficie.

    En modo rápido se revisan los límites típicos (cada MiB y cada cilindro de 63
    sectores) además de los sectores de arranque de respaldo de NTFS.
    """
    end = end or src.size
    found: List[Partition] = []
    seen = set()
    CH = 4 << 20
    pos = start

    def consider(off: int, fs: str, why: str, size: int = 0) -> None:
        if off in seen or off < 0:
            return
        size = size or _fs_size(src, off, fs)
        if size <= 0 or off + size > src.size + 1024 * 1024:
            return
        seen.add(off)
        found.append(Partition(len(found) + 1, off, size, fs, fs=fs, scheme="encontrada",
                               found=True, confidence=why))

    while pos < end:
        if cancel and cancel():
            break
        blk = src.read(pos, min(CH, end - pos))
        if not blk:
            break
        for i in range(0, len(blk) - 511, step):
            abs_off = pos + i
            sec_no = abs_off // 512
            if quick and not (abs_off % (1 << 20) == 0 or sec_no % 63 == 0 or blk[i + 510:i + 512] == b"\x55\xAA"):
                continue
            s = blk[i:i + 512]
            if s[510:512] == b"\x55\xAA":
                if s[3:11] == b"NTFS    ":
                    bps = u16(s, 0x0B) or 512
                    total = u64(s, 0x28) * bps
                    spc = s[0x0D] if s[0x0D] <= 0x80 else 1 << (256 - s[0x0D])
                    cand = abs_off - total
                    if cand >= 0 and src.read(cand, 11)[3:11] == b"NTFS    ":
                        pass  # es el respaldo de un sector primario intacto
                    elif src.read(abs_off + total, 11)[3:11] == b"NTFS    ":
                        consider(abs_off, "NTFS", "sector de arranque + respaldo")
                    elif cand >= 0 and src.read(cand + u64(s, 0x30) * bps * spc, 4) == b"FILE":
                        consider(cand, "NTFS", "solo respaldo (arranque primario dañado)", total + bps)
                    else:
                        consider(abs_off, "NTFS", "sector de arranque")
                elif s[3:11] == b"EXFAT   ":
                    consider(abs_off, "exFAT", "sector de arranque")
                elif s[82:87] == b"FAT32" or s[54:59] in (b"FAT12", b"FAT16"):
                    if detect(src, abs_off) == "FAT":
                        consider(abs_off, "FAT", "sector de arranque")
            if i + 1024 + 58 <= len(blk) and u16(blk, i + 1024 + 56) == 0xEF53 and (abs_off % 1024 == 0):
                group = u16(blk, i + 1024 + 0x5A)
                if group == 0:
                    f = detect(src, abs_off)
                    if f and f.startswith("ext"):
                        consider(abs_off, f, "superbloque")
            if i + 1026 <= len(blk) and blk[i + 1024:i + 1026] in (b"H+", b"HX") and abs_off % 4096 == 0:
                consider(abs_off, "HFS+", "cabecera de volumen")
            if blk[i + 32:i + 36] == b"NXSB" and abs_off % 4096 == 0:
                consider(abs_off, "APFS", "superbloque contenedor")
            if blk[i:i + 4] == b"XFSB" and abs_off % 4096 == 0:
                consider(abs_off, "XFS", "superbloque")
        pos += CH
        if progress:
            progress(pos - start, end - start)
    # filtrar particiones anidadas falsas (p. ej. superbloques de respaldo)
    found.sort(key=lambda p: p.start)
    clean: List[Partition] = []
    for p in found:
        if clean and p.start < clean[-1].end and p.fs == clean[-1].fs:
            continue
        clean.append(p)
    for i, p in enumerate(clean):
        p.index = i + 1
    return clean


# --------------------------------------------------------------------------- #
# Restauración (escritura) de tablas de particiones
# --------------------------------------------------------------------------- #
def backup_sectors(src: BlockSource, path: str, count: int = 34) -> None:
    with open(path, "wb") as fh:
        fh.write(src.read(0, count * src.sector_size))
        if src.size > count * src.sector_size:
            fh.write(src.read(src.size - count * src.sector_size, count * src.sector_size))


def write_mbr(dev, parts: List[Partition]) -> bytes:
    """Escribe una tabla MBR con hasta 4 particiones primarias."""
    ss = dev.sector_size
    sec = bytearray(dev.read(0, ss).ljust(512, b"\0"))
    for i in range(4):
        sec[446 + i * 16: 462 + i * 16] = b"\0" * 16
    for i, p in enumerate(parts[:4]):
        t = FS_TO_MBR.get(p.fs or "", 0x07)
        e = bytearray(16)
        e[1:4] = b"\xFE\xFF\xFF"
        e[4] = t
        e[5:8] = b"\xFE\xFF\xFF"
        struct.pack_into("<II", e, 8, p.start // ss, p.size // ss)
        sec[446 + i * 16: 462 + i * 16] = e
    sec[510:512] = b"\x55\xAA"
    if not any(sec[440:444]):
        sec[440:444] = os.urandom(4)
    dev.write(0, bytes(sec))
    return bytes(sec)


def write_gpt(dev, parts: List[Partition]) -> None:
    ss = dev.sector_size
    total = dev.size // ss
    n, esz = 128, 128
    ent_secs = n * esz // ss
    entries = bytearray(n * esz)
    for i, p in enumerate(parts[:n]):
        e = bytearray(esz)
        t = FS_TO_GPT.get(p.fs or "", FS_TO_GPT["NTFS"])
        e[0:16] = uuid.UUID(t).bytes_le
        e[16:32] = uuid.uuid4().bytes_le
        struct.pack_into("<QQQ", e, 32, p.start // ss, (p.start + p.size) // ss - 1, 0)
        nm = (p.label or f"Recuperada {i + 1}")[:36].encode("utf-16-le")
        e[56:56 + len(nm)] = nm
        entries[i * esz:(i + 1) * esz] = e
    ecrc = zlib.crc32(entries) & 0xFFFFFFFF
    disk_guid = uuid.uuid4().bytes_le

    def header(cur, alt, ent_lba):
        h = bytearray(ss)
        h[0:8] = b"EFI PART"
        struct.pack_into("<IIIIQQQQ", h, 8, 0x00010000, 92, 0, 0, cur, alt, 34, total - 34)
        h[56:72] = disk_guid
        struct.pack_into("<QIII", h, 72, ent_lba, n, esz, ecrc)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h[:92])) & 0xFFFFFFFF)
        return bytes(h)

    # MBR protector
    pm = bytearray(ss)
    struct.pack_into("<BBBBBBBBII", pm, 446, 0, 0, 2, 0, 0xEE, 0xFF, 0xFF, 0xFF, 1, min(total - 1, 0xFFFFFFFF))
    pm[510:512] = b"\x55\xAA"
    dev.write(0, bytes(pm))
    dev.write(ss, header(1, total - 1, 2))
    dev.write(2 * ss, bytes(entries))
    dev.write((total - 1 - ent_secs) * ss, bytes(entries))
    dev.write((total - 1) * ss, header(total - 1, 1, total - 1 - ent_secs))


def partition_source(src: BlockSource, p: Partition) -> SliceSource:
    return SliceSource(src, p.start, p.size, name=f"{src.name} · partición {p.index} ({p.fs or p.type_name})")


def repair_ntfs_boot(dev, part: Partition) -> bool:
    """Copia el sector de arranque NTFS de respaldo (último sector) sobre el primario."""
    back = part.start + part.size - 512
    s = dev.read(back, 512)
    if s[3:11] != b"NTFS    ":
        return False
    dev.write(part.start, s)
    return True
