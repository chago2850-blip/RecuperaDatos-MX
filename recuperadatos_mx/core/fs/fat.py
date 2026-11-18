"""FAT12/16/32 con recuperación de entradas borradas (0xE5) y nombres largos (LFN)."""
from __future__ import annotations

import datetime
import struct
from typing import Dict, List, Optional, Set, Tuple

from ..device import BlockSource
from ..model import FileEntry
from . import u16, u32


def fat_time(d: int, t: int = 0) -> float:
    try:
        dt = datetime.datetime(1980 + (d >> 9), (d >> 5) & 0xF, d & 0x1F,
                               t >> 11, (t >> 5) & 0x3F, (t & 0x1F) * 2)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


class FAT:
    name = "FAT"

    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src = src
        self.base = base
        b = src.read(base, 512)
        self.bps = u16(b, 11)
        self.spc = b[13]
        if not self.bps or not self.spc:
            raise ValueError("BPB FAT inválido")
        self.reserved = u16(b, 14)
        self.nfats = b[16]
        self.root_entries = u16(b, 17)
        total = u16(b, 19) or u32(b, 32)
        fatsz = u16(b, 22) or u32(b, 36)
        self.fatsz = fatsz
        self.cluster = self.bps * self.spc
        root_secs = (self.root_entries * 32 + self.bps - 1) // self.bps
        self.first_data = self.reserved + self.nfats * fatsz + root_secs
        self.nclusters = (total - self.first_data) // self.spc
        self.size = total * self.bps
        if self.nclusters < 4085:
            self.kind = 12
        elif self.nclusters < 65525:
            self.kind = 16
        else:
            self.kind = 32
        self.root_cluster = u32(b, 44) if self.kind == 32 else 0
        self.root_offset = base + (self.reserved + self.nfats * fatsz) * self.bps
        self.root_size = root_secs * self.bps
        self.label = (b[71:82] if self.kind == 32 else b[43:54]).decode("ascii", "replace").strip()
        self._fat: Optional[bytes] = None

    def fat(self) -> bytes:
        if self._fat is None:
            self._fat = self.src.read(self.base + self.reserved * self.bps, self.fatsz * self.bps)
        return self._fat

    def next_cluster(self, c: int) -> int:
        f = self.fat()
        if self.kind == 32:
            if c * 4 + 4 > len(f):
                return 0x0FFFFFFF
            return u32(f, c * 4) & 0x0FFFFFFF
        if self.kind == 16:
            if c * 2 + 2 > len(f):
                return 0xFFFF
            return u16(f, c * 2)
        o = c + c // 2
        if o + 2 > len(f):
            return 0xFFF
        v = u16(f, o)
        return (v >> 4) if c & 1 else (v & 0xFFF)

    def eoc(self, v: int) -> bool:
        return v >= {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[self.kind] or v < 2

    def is_free(self, c: int) -> bool:
        return self.next_cluster(c) == 0

    def coff(self, c: int) -> int:
        return self.base + (self.first_data + (c - 2) * self.spc) * self.bps

    def chain(self, start: int, limit: int = 1 << 22) -> List[int]:
        out = []
        seen: Set[int] = set()
        c = start
        while 2 <= c < self.nclusters + 2 and c not in seen and len(out) < limit:
            out.append(c)
            seen.add(c)
            n = self.next_cluster(c)
            if self.eoc(n):
                break
            c = n
        return out

    def _extents_from_clusters(self, clusters: List[int], size: int) -> List[Tuple[int, int]]:
        ext: List[Tuple[int, int]] = []
        for c in clusters:
            o = self.coff(c)
            if ext and ext[-1][0] + ext[-1][1] == o:
                ext[-1] = (ext[-1][0], ext[-1][1] + self.cluster)
            else:
                ext.append((o, self.cluster))
        # recortar al tamaño
        out, rem = [], size
        for o, l in ext:
            if rem <= 0:
                break
            out.append((o, min(l, rem)))
            rem -= l
        return out

    def _deleted_clusters(self, start: int, size: int) -> Tuple[List[int], str]:
        """Supone asignación contigua (el método estándar) y evalúa su probabilidad."""
        n = max(1, (size + self.cluster - 1) // self.cluster)
        clusters = list(range(start, start + n))
        busy = sum(1 for c in clusters[:2048] if not self.is_free(c))
        chance = "Buena" if busy == 0 else ("Regular" if busy < len(clusters[:2048]) / 2 else "Baja")
        return clusters, chance

    def _dir_data(self, cluster: int, deleted: bool) -> bytes:
        if cluster == 0 and self.kind != 32:
            return self.src.read(self.root_offset, self.root_size)
        if deleted:
            return self.src.read(self.coff(cluster), self.cluster * 4)
        cl = self.chain(cluster, limit=65536)
        return b"".join(self.src.read(self.coff(c), self.cluster) for c in cl)

    def entries(self, progress=None, cancel=None) -> List[FileEntry]:
        out: List[FileEntry] = []
        visited: Set[int] = set()
        stack = [(self.root_cluster, "", False)]
        steps = 0
        while stack:
            if cancel and cancel():
                break
            cl, path, deleted_parent = stack.pop()
            if cl in visited and cl != 0:
                continue
            visited.add(cl)
            data = self._dir_data(cl, deleted_parent)
            lfn: Dict[int, str] = {}
            for i in range(0, len(data) - 31, 32):
                e = data[i:i + 32]
                first = e[0]
                if first == 0x00:
                    if not deleted_parent:
                        break
                    lfn = {}
                    continue
                attr = e[11]
                if attr == 0x0F:
                    seq = (e[0] & 0x1F) if first != 0xE5 else 0
                    part = (e[1:11] + e[14:26] + e[28:32]).decode("utf-16-le", "ignore")
                    part = part.split("\x00")[0].replace("￿", "")
                    # las entradas LFN aparecen en orden inverso; si están borradas no hay número
                    key = seq or -(len(lfn) + 1)
                    lfn[key] = part
                    continue
                deleted = first == 0xE5 or deleted_parent
                base = e[0:8]
                if first == 0xE5:
                    base = b"_" + e[1:8]
                if first == 0x05:
                    base = b"\xe5" + e[1:8]
                short = base.decode("cp437", "replace").rstrip()
                ext = e[8:11].decode("cp437", "replace").rstrip()
                if e[12] & 0x08:  # indicadores de minúsculas de Windows NT
                    short = short.lower()
                if e[12] & 0x10:
                    ext = ext.lower()
                sname = short + ("." + ext if ext else "")
                if lfn:
                    name = "".join(lfn[k] for k in sorted(lfn)) or sname
                else:
                    name = sname
                lfn = {}
                if sname in (".", "..") or name in (".", "..") or attr & 0x08:
                    continue
                if first == 0xE5 and not name[0].isprintable():
                    continue
                hi = u16(e, 20) if self.kind == 32 else 0
                start = (hi << 16) | u16(e, 26)
                size = u32(e, 28)
                mtime = fat_time(u16(e, 24), u16(e, 22))
                ctime = fat_time(u16(e, 16), u16(e, 14))
                atime = fat_time(u16(e, 18))
                is_dir = bool(attr & 0x10)
                if start >= self.nclusters + 2:
                    continue
                if is_dir:
                    out.append(FileEntry(name=name, path=path, is_dir=True, deleted=deleted, fs=f"FAT{self.kind}",
                                         mtime=mtime, ctime=ctime, ident=f"cl{start}"))
                    if start >= 2:
                        stack.append((start, (path + "/" + name) if path else name, deleted))
                    continue
                fe = FileEntry(name=name, path=path, size=size, deleted=deleted, fs=f"FAT{self.kind}",
                               mtime=mtime, ctime=ctime, atime=atime, ident=f"cl{start}")
                if size and start >= 2:
                    if deleted:
                        clusters, fe.chance = self._deleted_clusters(start, size)
                    else:
                        clusters = self.chain(start, limit=(size // self.cluster) + 2)
                        fe.chance = "Excelente"
                    fe.extents = self._extents_from_clusters(clusters, size)
                elif size == 0:
                    fe.resident = b""
                    fe.chance = "Excelente"
                out.append(fe)
            steps += 1
            if progress:
                progress(steps, steps + len(stack))
        return out
