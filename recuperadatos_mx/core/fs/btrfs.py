"""Btrfs: árbol de chunks (lógico→físico), árbol de raíces, subvolúmenes, archivos
en línea y extensiones (zlib/zstd), y recuperación de archivos borrados a partir de
hojas de generaciones anteriores que el copy-on-write dejó en el disco."""
from __future__ import annotations

import struct
import zlib
from typing import Dict, List, Optional, Tuple

from ..device import BlockSource
from ..model import FileEntry

INODE_ITEM, INODE_REF, DIR_ITEM, DIR_INDEX, EXTENT_DATA = 1, 12, 84, 96, 108
ROOT_ITEM, CHUNK_ITEM = 132, 228
FS_TREE = 5


def u8(b, o): return b[o]
def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]


def _key(b, o):
    return u64(b, o), b[o + 8], u64(b, o + 9)


class Btrfs:
    name = "Btrfs"

    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src, self.base = src, base
        sb = src.read(base + 0x10000, 4096)
        if sb[0x40:0x48] != b"_BHRfS_M":
            raise ValueError("No es Btrfs")
        self.fsid = sb[0x20:0x30]
        self.root = u64(sb, 0x50)
        self.chunk_root = u64(sb, 0x58)
        self.size = u64(sb, 0x70)
        self.sectorsize = u32(sb, 0x90)
        self.nodesize = u32(sb, 0x94)
        self.generation = u64(sb, 0x48)
        self.label = sb[0x12B:0x22B].split(b"\0")[0].decode("utf-8", "replace")
        self.chunks: List[Tuple[int, int, int, int]] = []  # (lógico, longitud, físico, tipo)
        n = u32(sb, 0xA0)
        arr = sb[0x32B:0x32B + n]
        p = 0
        while p + 17 + 48 <= len(arr):
            _, typ, logical = _key(arr, p)
            p += 17
            ln = self._chunk_item(arr, p, logical)
            p += ln
        self._read_chunk_tree()

    def _chunk_item(self, b: bytes, p: int, logical: int) -> int:
        length, ctype = u64(b, p), u64(b, p + 24)
        nst = u16(b, p + 44)
        phys = u64(b, p + 48 + 8)
        if (logical, length) not in [(c[0], c[1]) for c in self.chunks]:
            self.chunks.append((logical, length, phys, ctype))
        return 48 + nst * 32

    def phys(self, logical: int) -> Optional[int]:
        for lo, ln, ph, _ in self.chunks:
            if lo <= logical < lo + ln:
                return self.base + ph + (logical - lo)
        return None

    def read_logical(self, logical: int, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            p = self.phys(logical)
            chunk_end = next((lo + ln for lo, ln, _, _ in self.chunks if lo <= logical < lo + ln), logical + n)
            k = min(n, chunk_end - logical)
            out += self.src.read(p, k) if p is not None else b"\0" * k
            logical += k
            n -= k
        return bytes(out)

    def node(self, logical: int) -> Optional[bytes]:
        b = self.read_logical(logical, self.nodesize)
        if len(b) < 0x65 or b[0x20:0x30] != self.fsid or u64(b, 0x30) != logical:
            return None
        return b

    def walk(self, logical: int, guard: int = 0):
        """Genera (clave, datos) de todas las hojas del árbol."""
        b = self.node(logical)
        if b is None or guard > 10:
            return
        nritems, level = u32(b, 0x60), b[0x64]
        if level == 0:
            for i in range(nritems):
                o = 0x65 + i * 25
                k = _key(b, o)
                off, sz = u32(b, o + 17), u32(b, o + 21)
                yield k, b[0x65 + off:0x65 + off + sz]
        else:
            for i in range(nritems):
                o = 0x65 + i * 33
                yield from self.walk(u64(b, o + 17), guard + 1)

    def _read_chunk_tree(self) -> None:
        for (obj, typ, off), data in self.walk(self.chunk_root):
            if typ == CHUNK_ITEM:
                self._chunk_item(data, 0, off)

    def fs_roots(self) -> Dict[int, int]:
        roots = {}
        for (obj, typ, off), data in self.walk(self.root):
            if typ == ROOT_ITEM and (obj == FS_TREE or 256 <= obj < (1 << 63)) and len(data) >= 239:
                roots[obj] = u64(data, 176)
        return roots

    # ------------------------------------------------------------------ #
    @staticmethod
    def _collect(items, store: dict, gen: int = 0) -> None:
        for (obj, typ, off), data in items:
            if typ in (INODE_ITEM, INODE_REF, EXTENT_DATA, DIR_INDEX):
                key = (obj, typ, off)
                if key not in store or store[key][0] <= gen:
                    store[key] = (gen, data)

    def _extent_entry(self, store: dict, ino: int, size: int) -> Tuple[List[Tuple[int, int]], Optional[bytes]]:
        exts = sorted((k[2], v[1]) for k, v in store.items() if k[0] == ino and k[1] == EXTENT_DATA)
        out: List[Tuple[int, int]] = []
        flat = bytearray()
        need_flat = False
        pos = 0
        for foff, d in exts:
            if len(d) < 21:
                continue
            comp, etype = d[16], d[20]
            if foff > pos:
                out.append((-1, foff - pos))
                flat += b"\0" * (foff - pos)
            if etype == 0:
                raw = d[21:]
                data = self._decompress(raw, comp, u64(d, 8))
                need_flat = True
                flat += data
                out.append((-2, len(data)))
                pos = foff + len(data)
                continue
            dbyte, dnum, eoff, nbytes = u64(d, 21), u64(d, 29), u64(d, 37), u64(d, 45)
            if comp:
                data = self._decompress(self.read_logical(dbyte, dnum), comp, u64(d, 8))[eoff:eoff + nbytes]
                need_flat = True
                flat += data
                out.append((-2, len(data)))
            elif dbyte == 0:
                out.append((-1, nbytes))
                flat += b"\0" * nbytes
            else:
                logical = dbyte + eoff
                p = self.phys(logical)
                out.append((p if p is not None else -1, nbytes))
                if need_flat or len(flat) < (64 << 20):
                    flat += self.read_logical(logical, nbytes) if len(flat) < (64 << 20) else b""
            pos = foff + nbytes
        if need_flat:
            return [], bytes(flat[:size])
        res, rem = [], size
        for o, l in out:
            if rem <= 0:
                break
            res.append((o, min(l, rem)))
            rem -= l
        return res, None

    @staticmethod
    def _decompress(raw: bytes, comp: int, ram: int) -> bytes:
        try:
            if comp == 0:
                return raw
            if comp == 1:
                return zlib.decompress(raw)
            if comp == 3:
                try:
                    from compression import zstd  # Python 3.14+
                    return zstd.decompress(raw)
                except ImportError:
                    import zstandard
                    return zstandard.ZstdDecompressor().decompress(raw, max_output_size=ram or (1 << 27))
            if comp == 2:
                # LZO por segmentos: [long total][long seg][datos]…
                from .lzo import lzo_btrfs
                return lzo_btrfs(raw, ram)
        except Exception:
            pass
        return b"\0" * ram

    def _scan_old_leaves(self, owners: set, cancel=None) -> dict:
        """Recorre los chunks de metadatos buscando hojas antiguas de los árboles de archivos."""
        store: dict = {}
        for lo, ln, ph, ctype in self.chunks:
            if not ctype & 0x4:  # METADATA
                continue
            for o in range(0, ln, self.nodesize):
                if cancel and cancel():
                    return store
                b = self.src.read(self.base + ph + o, self.nodesize)
                if len(b) < 0x65 or b[0x20:0x30] != self.fsid or u64(b, 0x30) != lo + o:
                    continue
                if b[0x64] != 0 or u64(b, 0x58) not in owners:
                    continue
                gen = u64(b, 0x50)
                items = []
                for i in range(u32(b, 0x60)):
                    q = 0x65 + i * 25
                    if q + 25 > len(b):
                        break
                    k = _key(b, q)
                    off, sz = u32(b, q + 17), u32(b, q + 21)
                    items.append((k, b[0x65 + off:0x65 + off + sz]))
                self._collect(items, store, gen)
        return store

    def entries(self, progress=None, cancel=None) -> List[FileEntry]:
        out: List[FileEntry] = []
        roots = self.fs_roots()
        old = self._scan_old_leaves(set(roots), cancel)
        for root_id, root_log in roots.items():
            store: dict = {}
            self._collect(self.walk(root_log), store, 1 << 62)
            prefix = "" if root_id == FS_TREE else f"subvolumen {root_id}"
            inodes = {k[0]: v[1] for k, v in store.items() if k[1] == INODE_ITEM}
            parent: Dict[int, Tuple[int, str]] = {}
            for (obj, typ, off), (_, d) in store.items():
                if typ == INODE_REF and len(d) >= 10:
                    nl = u16(d, 8)
                    parent[obj] = (off, d[10:10 + nl].decode("utf-8", "replace"))
            cache = {256: prefix}

            def path_of(ino, depth=0):
                if ino in cache:
                    return cache[ino]
                if ino not in parent or depth > 64:
                    return (prefix + "/" if prefix else "") + "$Huérfanos"
                par, nm = parent[ino]
                pp = path_of(par, depth + 1)
                cache[ino] = (pp + "/" + nm) if pp else nm
                return cache[ino]

            for ino, d in inodes.items():
                if ino == 256 or ino not in parent:
                    continue
                mode = u32(d, 52)
                par, nm = parent[ino]
                if mode & 0xF000 == 0x4000:
                    out.append(FileEntry(name=nm, path=path_of(par), is_dir=True, fs="Btrfs", mtime=u64(d, 136),
                                         ident=f"inodo {ino}"))
                elif mode & 0xF000 == 0x8000:
                    size = u64(d, 16)
                    exts, flat = self._extent_entry(store, ino, size)
                    e = FileEntry(name=nm, path=path_of(par), size=size, fs="Btrfs", mtime=u64(d, 136),
                                  ctime=u64(d, 124), atime=u64(d, 112), ident=f"inodo {ino}", extents=exts,
                                  resident=flat, chance="Excelente")
                    out.append(e)
            # borrados: inodos que solo aparecen en hojas antiguas
            if old:
                old_inodes = {k[0]: v[1] for k, v in old.items() if k[1] == INODE_ITEM and k[0] not in inodes}
                old_parent = {}
                for (obj, typ, off), (_, d) in old.items():
                    if typ == INODE_REF and obj in old_inodes and len(d) >= 10:
                        nl = u16(d, 8)
                        old_parent[obj] = (off, d[10:10 + nl].decode("utf-8", "replace"))
                for ino, d in old_inodes.items():
                    mode = u32(d, 52)
                    if mode & 0xF000 != 0x8000:
                        continue
                    size = u64(d, 16)
                    merged = dict(old)
                    exts, flat = self._extent_entry(merged, ino, size)
                    if not exts and flat is None:
                        continue
                    par, nm = old_parent.get(ino, (0, f"inodo_{ino}"))
                    e = FileEntry(name=nm, path=path_of(par) if par in cache or par in parent else
                                  (prefix + "/" if prefix else "") + "$Huérfanos", size=size, deleted=True,
                                  fs="Btrfs", mtime=u64(d, 136), ctime=u64(d, 124), ident=f"inodo {ino}",
                                  extents=exts, resident=flat, chance="Regular",
                                  note="Recuperado de una generación anterior del árbol (copy-on-write)")
                    out.append(e)
        return out
