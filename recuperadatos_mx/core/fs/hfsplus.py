"""HFS+/HFSX: lectura del catálogo B-tree, incluidos registros que quedan en
nodos liberados (archivos borrados)."""
from __future__ import annotations

import struct
from typing import Dict, List, Tuple

from ..device import BlockSource
from ..model import FileEntry

HFS_EPOCH = 2082844800


def be16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def be32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def be64(b, o):
    return struct.unpack_from(">Q", b, o)[0]


def hfs_time(v: int) -> float:
    return float(v - HFS_EPOCH) if v > HFS_EPOCH else 0.0


class HFSPlus:
    name = "HFS+"

    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src = src
        self.base = base
        vh = src.read(base + 1024, 512)
        if vh[:2] not in (b"H+", b"HX"):
            raise ValueError("No es HFS+")
        self.bs = be32(vh, 0x28)
        self.total = be32(vh, 0x2C)
        self.size = self.bs * self.total
        self.catalog = self._fork(vh, 0x110)

    def _fork(self, b: bytes, o: int) -> dict:
        size = be64(b, o)
        exts = []
        for i in range(8):
            sb = be32(b, o + 16 + i * 8)
            cnt = be32(b, o + 20 + i * 8)
            if cnt:
                exts.append((sb, cnt))
        return {"size": size, "extents": exts}

    def _fork_extents(self, fork: dict) -> List[Tuple[int, int]]:
        out = []
        for sb, cnt in fork["extents"]:
            out.append((self.base + sb * self.bs, cnt * self.bs))
        return out

    def _read_fork(self, fork: dict) -> bytes:
        out = bytearray()
        for off, ln in self._fork_extents(fork):
            out += self.src.read(off, ln)
        return bytes(out[:fork["size"]])

    def entries(self, progress=None, cancel=None) -> List[FileEntry]:
        cat = self._read_fork(self.catalog)
        if len(cat) < 512:
            return []
        node_size = be16(cat, 14 + 18)
        first_leaf = be32(cat, 14 + 10)
        nnodes = len(cat) // node_size
        live_nodes = set()
        n = first_leaf
        while n and n < nnodes and n not in live_nodes:
            live_nodes.add(n)
            n = be32(cat, n * node_size)
        folders: Dict[int, Tuple[str, int]] = {2: ("", 1)}
        files = []
        for idx in range(1, nnodes):
            if cancel and cancel():
                break
            node = cat[idx * node_size:(idx + 1) * node_size]
            if len(node) < 14 or struct.unpack_from(">b", node, 8)[0] != -1:
                continue
            nrec = be16(node, 10)
            deleted_node = idx not in live_nodes
            if nrec > node_size // 8:
                continue
            for r in range(nrec):
                po = node_size - 2 * (r + 1)
                off = be16(node, po)
                if off < 14 or off + 8 > node_size:
                    continue
                klen = be16(node, off)
                if klen < 6 or off + 2 + klen + 2 > node_size:
                    continue
                parent = be32(node, off + 2)
                nlen = be16(node, off + 6)
                name = node[off + 8: off + 8 + nlen * 2].decode("utf-16-be", "replace")
                d = off + 2 + klen
                if d % 2:
                    d += 1
                if d + 2 > node_size:
                    continue
                rtype = be16(node, d)
                if rtype == 1 and d + 16 <= node_size:
                    fid = be32(node, d + 8)
                    if fid not in folders or not deleted_node:
                        folders[fid] = (name, parent)
                elif rtype == 2 and d + 168 <= node_size:
                    fork = self._fork(node, d + 88)
                    files.append((name, parent, fork, hfs_time(be32(node, d + 12)),
                                  hfs_time(be32(node, d + 16)), be32(node, d + 8), deleted_node))
            if progress:
                progress(idx, nnodes)

        cache: Dict[int, str] = {2: "", 1: ""}

        def path_of(fid, depth=0):
            if fid in cache:
                return cache[fid]
            if fid not in folders or depth > 64:
                return "$Huérfanos"
            nm, par = folders[fid]
            p = path_of(par, depth + 1)
            cache[fid] = (p + "/" + nm) if p else nm
            return cache[fid]

        out: List[FileEntry] = []
        seen = set()
        for name, parent, fork, ctime, mtime, fid, deleted in files:
            key = (fid, name)
            if key in seen:
                continue
            seen.add(key)
            e = FileEntry(name=name, path=path_of(parent), size=fork["size"], deleted=deleted,
                          fs="HFS+", ctime=ctime, mtime=mtime, ident=f"CNID {fid}",
                          extents=self._fork_extents(fork))
            e.chance = "Excelente" if not deleted else "Regular"
            if len(fork["extents"]) == 8:
                e.note = "Puede tener extensiones adicionales en el archivo de desbordamiento"
            out.append(e)
        for fid, (nm, par) in folders.items():
            if fid > 2:
                out.append(FileEntry(name=nm, path=path_of(par), is_dir=True, fs="HFS+",
                                     ident=f"CNID {fid}"))
        return out
