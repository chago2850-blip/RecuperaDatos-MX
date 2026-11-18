"""exFAT con recuperación de entradas borradas (bit InUse = 0)."""
from __future__ import annotations

from typing import List, Optional, Set, Tuple

from ..device import BlockSource
from ..model import FileEntry
from . import u16, u32, u64


def exfat_time(ts: int) -> float:
    import datetime
    try:
        d, t = ts >> 16, ts & 0xFFFF
        return datetime.datetime(1980 + (d >> 9), (d >> 5) & 0xF, d & 0x1F, t >> 11,
                                 (t >> 5) & 0x3F, (t & 0x1F) * 2).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


class ExFAT:
    name = "exFAT"

    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src = src
        self.base = base
        b = src.read(base, 512)
        if b[3:11] != b"EXFAT   ":
            raise ValueError("No es exFAT")
        self.bps = 1 << b[108]
        self.cluster = self.bps << b[109]
        self.fat_off = u32(b, 80) * self.bps
        self.fat_len = u32(b, 84) * self.bps
        self.heap = u32(b, 88) * self.bps
        self.nclusters = u32(b, 92)
        self.root = u32(b, 96)
        self.size = u64(b, 72) * self.bps
        self._fat: Optional[bytes] = None
        self._bitmap: Optional[bytes] = None

    def fat(self) -> bytes:
        if self._fat is None:
            self._fat = self.src.read(self.base + self.fat_off, min(self.fat_len, (self.nclusters + 2) * 4))
        return self._fat

    def next_cluster(self, c: int) -> int:
        f = self.fat()
        return u32(f, c * 4) if c * 4 + 4 <= len(f) else 0xFFFFFFFF

    def coff(self, c: int) -> int:
        return self.base + self.heap + (c - 2) * self.cluster

    def chain(self, c: int, limit: int) -> List[int]:
        out, seen = [], set()
        while 2 <= c < self.nclusters + 2 and c not in seen and len(out) < limit:
            out.append(c)
            seen.add(c)
            c = self.next_cluster(c)
        return out

    def clusters_for(self, first: int, size: int, nofat: bool, deleted: bool) -> List[int]:
        n = max(1, (size + self.cluster - 1) // self.cluster)
        if nofat or deleted:
            return list(range(first, first + n))
        return self.chain(first, n + 1)

    def extents(self, clusters: List[int], size: int) -> List[Tuple[int, int]]:
        ext: List[Tuple[int, int]] = []
        for c in clusters:
            o = self.coff(c)
            if ext and ext[-1][0] + ext[-1][1] == o:
                ext[-1] = (ext[-1][0], ext[-1][1] + self.cluster)
            else:
                ext.append((o, self.cluster))
        out, rem = [], size
        for o, l in ext:
            if rem <= 0:
                break
            out.append((o, min(l, rem)))
            rem -= l
        return out

    def read_dir(self, first: int, nofat: bool, length: int, deleted: bool) -> bytes:
        cl = self.clusters_for(first, length or self.cluster, nofat, deleted) if length else \
            self.chain(first, 65536)
        return b"".join(self.src.read(self.coff(c), self.cluster) for c in cl)

    def entries(self, progress=None, cancel=None) -> List[FileEntry]:
        out: List[FileEntry] = []
        stack = [(self.root, False, 0, "", False)]
        seen: Set[int] = set()
        steps = 0
        while stack:
            if cancel and cancel():
                break
            first, nofat, length, path, del_parent = stack.pop()
            if first in seen:
                continue
            seen.add(first)
            data = self.read_dir(first, nofat, length, del_parent)
            i = 0
            while i + 32 <= len(data):
                t = data[i]
                if t == 0x00 and not del_parent:
                    break
                if t in (0x85, 0x05):
                    deleted = t == 0x05 or del_parent
                    count = data[i + 1]
                    attrs = u16(data, i + 4)
                    ctime = exfat_time(u32(data, i + 8))
                    mtime = exfat_time(u32(data, i + 12))
                    atime = exfat_time(u32(data, i + 16))
                    j = i + 32
                    if j + 32 > len(data) or data[j] & 0x7F != 0x40:
                        i += 32
                        continue
                    flags = data[j + 1]
                    nlen = data[j + 3]
                    first_cl = u32(data, j + 20)
                    size = u64(data, j + 24)
                    name = ""
                    for k in range(2, count + 1):
                        p = i + k * 32
                        if p + 32 > len(data) or data[p] & 0x7F != 0x41:
                            break
                        name += data[p + 2:p + 32].decode("utf-16-le", "ignore")
                    name = name[:nlen] or f"sin_nombre_{first_cl}"
                    nofat_f = bool(flags & 2)
                    if attrs & 0x10:
                        out.append(FileEntry(name=name, path=path, is_dir=True, deleted=deleted, fs="exFAT",
                                             ctime=ctime, mtime=mtime, ident=f"cl{first_cl}"))
                        if first_cl >= 2:
                            stack.append((first_cl, nofat_f, size, (path + "/" + name) if path else name,
                                          deleted))
                    else:
                        fe = FileEntry(name=name, path=path, size=size, deleted=deleted, fs="exFAT",
                                       ctime=ctime, mtime=mtime, atime=atime, ident=f"cl{first_cl}")
                        if size and first_cl >= 2:
                            cl = self.clusters_for(first_cl, size, nofat_f, deleted)
                            fe.extents = self.extents(cl, size)
                        else:
                            fe.resident = b""
                        fe.chance = "Excelente" if not deleted else ("Buena" if nofat_f else "Regular")
                        out.append(fe)
                    i += 32 * (count + 1)
                    continue
                i += 32
            steps += 1
            if progress:
                progress(steps, steps + len(stack))
        return out
