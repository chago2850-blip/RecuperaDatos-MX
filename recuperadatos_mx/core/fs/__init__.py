"""Analizadores de sistemas de archivos."""
from __future__ import annotations

import struct
from typing import Optional

from ..device import BlockSource


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def detect(src: BlockSource, base: int = 0) -> Optional[str]:
    """Identifica el sistema de archivos que empieza en ``base``."""
    head = src.read(base, 4096)
    if len(head) < 512:
        return None
    if head[3:11] == b"NTFS    ":
        return "NTFS"
    if head[3:11] == b"EXFAT   ":
        return "exFAT"
    if head[3:7] == b"ReFS":
        return "ReFS"
    if head[:4] == b"XFSB":
        return "XFS"
    if head[32:36] == b"NXSB":
        return "APFS"
    if head[1024:1026] in (b"H+", b"HX"):
        return "HFS+"
    if len(head) >= 1024 + 58 and u16(head, 1024 + 56) == 0xEF53:
        feat_incompat = u32(head, 1024 + 0x60)
        feat_compat = u32(head, 1024 + 0x5C)
        if feat_incompat & 0x40 or feat_incompat & 0x200:
            return "ext4"
        if feat_compat & 0x4:
            return "ext3"
        return "ext2"
    if head[510:512] == b"\x55\xAA":
        if head[82:87] == b"FAT32" or head[54:59] in (b"FAT12", b"FAT16", b"FAT  "):
            return "FAT"
        bps = u16(head, 11)
        if bps in (512, 1024, 2048, 4096) and head[13] in (1, 2, 4, 8, 16, 32, 64, 128) \
                and u16(head, 14) > 0 and head[16] in (1, 2) and head[21] >= 0xF0:
            return "FAT"
    tail = src.read(base + 65536 + 0x40, 8)
    if tail == b"_BHRfS_M":
        return "Btrfs"
    iso = src.read(base + 32769, 5)
    if iso == b"CD001":
        return "ISO9660"
    return None


def open_fs(src: BlockSource, base: int = 0, kind: Optional[str] = None):
    """Devuelve un analizador con método ``entries(progress, cancel)``."""
    kind = kind or detect(src, base)
    if kind == "NTFS":
        from .ntfs import NTFS
        return NTFS(src, base)
    if kind == "FAT":
        from .fat import FAT
        return FAT(src, base)
    if kind == "exFAT":
        from .exfat import ExFAT
        return ExFAT(src, base)
    if kind in ("ext2", "ext3", "ext4"):
        from .ext import ExtFS
        return ExtFS(src, base)
    if kind == "HFS+":
        from .hfsplus import HFSPlus
        return HFSPlus(src, base)
    return None
