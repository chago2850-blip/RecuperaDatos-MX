"""ext2/ext3/ext4: árbol de directorios, extents, inline data y recuperación de
archivos borrados a partir de copias antiguas de inodos en el journal (JBD2)."""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from ..device import BlockSource
from ..model import FileEntry
from . import u16, u32

S_IFDIR = 0x4000
S_IFREG = 0x8000
S_IFLNK = 0xA000


def be32(b, o):
    return struct.unpack_from(">I", b, o)[0]


class ExtFS:
    name = "ext"

    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src = src
        self.base = base
        sb = src.read(base + 1024, 1024)
        if u16(sb, 0x38) != 0xEF53:
            raise ValueError("No es ext2/3/4")
        self.sb = sb
        self.bs = 1024 << u32(sb, 0x18)
        self.inodes_count = u32(sb, 0x00)
        self.blocks = u32(sb, 0x04) | (u32(sb, 0x150) << 32 if u32(sb, 0x60) & 0x80 else 0)
        self.first_data = u32(sb, 0x14)
        self.bpg = u32(sb, 0x20)
        self.ipg = u32(sb, 0x28)
        self.inode_size = u16(sb, 0x58) if u32(sb, 0x4C) >= 1 else 128
        self.incompat = u32(sb, 0x60)
        self.is64 = bool(self.incompat & 0x80)
        self.desc_size = (u16(sb, 0xFE) or 32) if self.is64 else 32
        self.label = sb[0x78:0x88].split(b"\0")[0].decode("utf-8", "replace")
        self.journal_inum = u32(sb, 0xE0) or 8
        self.size = self.blocks * self.bs
        self.ngroups = (self.blocks - self.first_data + self.bpg - 1) // self.bpg
        gdt_block = self.first_data + 1
        raw = src.read(base + gdt_block * self.bs, self.ngroups * self.desc_size)
        self.itables: List[int] = []
        self.gflags: List[int] = []
        for g in range(self.ngroups):
            o = g * self.desc_size
            lo = u32(raw, o + 8) if o + 12 <= len(raw) else 0
            hi = u32(raw, o + 0x28) if self.is64 and o + 0x2C <= len(raw) else 0
            self.itables.append(lo | (hi << 32))
            self.gflags.append(u16(raw, o + 0x12) if o + 0x14 <= len(raw) else 0)
        self.fs_name = "ext4" if self.incompat & 0x40 else ("ext3" if u32(sb, 0x5C) & 4 else "ext2")

    # ------------------------------------------------------------------ #
    def inode_raw(self, n: int) -> bytes:
        g, idx = divmod(n - 1, self.ipg)
        if g >= len(self.itables):
            return b""
        off = self.itables[g] * self.bs + idx * self.inode_size
        return self.src.read(self.base + off, self.inode_size)

    def block_off(self, blk: int) -> int:
        return self.base + blk * self.bs

    def _ext_tree(self, node: bytes, depth_guard=0) -> List[Tuple[int, int, int]]:
        """[(bloque_lógico, bloque_físico, cantidad)]"""
        if len(node) < 12 or u16(node, 0) != 0xF30A or depth_guard > 6:
            return []
        entries = u16(node, 2)
        depth = u16(node, 6)
        out = []
        for i in range(entries):
            o = 12 + i * 12
            if o + 12 > len(node):
                break
            if depth == 0:
                lblk = u32(node, o)
                ln = u16(node, o + 4)
                if ln > 32768:
                    ln -= 32768
                phys = (u16(node, o + 6) << 32) | u32(node, o + 8)
                out.append((lblk, phys, ln))
            else:
                leaf = (u16(node, o + 8) << 32) | u32(node, o + 4)
                child = self.src.read(self.block_off(leaf), self.bs)
                out.extend(self._ext_tree(child, depth_guard + 1))
        return out

    def _blockmap(self, iblock: bytes, nblocks: int) -> List[Tuple[int, int, int]]:
        ptrs = list(struct.unpack("<15I", iblock[:60]))
        out: List[Tuple[int, int, int]] = []
        per = self.bs // 4
        lb = 0

        def add(p):
            nonlocal lb
            if p:
                out.append((lb, p, 1))
            lb += 1

        def walk(p, level):
            if lb >= nblocks:
                return
            if not p:
                return
            data = self.src.read(self.block_off(p), self.bs)
            for q in struct.unpack(f"<{per}I", data[:per * 4]):
                if lb >= nblocks:
                    return
                if level == 1:
                    add(q)
                else:
                    walk(q, level - 1)

        for p in ptrs[:12]:
            if lb >= nblocks:
                break
            add(p)
        walk(ptrs[12], 1)
        walk(ptrs[13], 2)
        walk(ptrs[14], 3)
        return out

    def parse_inode(self, raw: bytes) -> Optional[dict]:
        if len(raw) < 128:
            return None
        mode = u16(raw, 0)
        size = u32(raw, 4) | (u32(raw, 0x6C) << 32)
        flags = u32(raw, 0x20)
        return {"mode": mode, "size": size, "atime": u32(raw, 8), "ctime": u32(raw, 0xC),
                "mtime": u32(raw, 0x10), "dtime": u32(raw, 0x14), "links": u16(raw, 0x1A),
                "flags": flags, "iblock": raw[0x28:0x28 + 60], "raw": raw}

    def mapping(self, ino: dict) -> List[Tuple[int, int, int]]:
        nblocks = (ino["size"] + self.bs - 1) // self.bs
        if ino["flags"] & 0x80000:
            return sorted(self._ext_tree(ino["iblock"]))
        return self._blockmap(ino["iblock"], nblocks)

    def extents_of(self, ino: dict) -> List[Tuple[int, int]]:
        out: List[Tuple[int, int]] = []
        expect = 0
        for lblk, phys, cnt in self.mapping(ino):
            if lblk > expect:
                out.append((-1, (lblk - expect) * self.bs))
            o = self.block_off(phys)
            if out and out[-1][0] >= 0 and out[-1][0] + out[-1][1] == o:
                out[-1] = (out[-1][0], out[-1][1] + cnt * self.bs)
            else:
                out.append((o, cnt * self.bs))
            expect = lblk + cnt
        return out

    def content_entry(self, ino: dict) -> FileEntry:
        e = FileEntry(name="", size=ino["size"], mtime=ino["mtime"], ctime=ino["ctime"],
                      atime=ino["atime"], fs=self.fs_name)
        if ino["flags"] & 0x10000000:  # inline data
            e.resident = ino["iblock"][:ino["size"]]
        elif (ino["mode"] & 0xF000) == S_IFLNK and ino["size"] < 60:
            e.resident = ino["iblock"][:ino["size"]]
        else:
            e.extents = self.extents_of(ino)
        return e

    def read_all(self, ino: dict, limit: int = 64 << 20) -> bytes:
        from ..model import iter_content
        e = self.content_entry(ino)
        e.size = min(e.size, limit)
        return b"".join(iter_content(self.src, e))

    # ------------------------------------------------------------------ #
    def _parse_dir(self, data: bytes):
        """Devuelve (vivas, borradas) como listas de (inodo, nombre, tipo)."""
        live, dead = [], []
        i = 0
        while i + 8 <= len(data):
            inum = u32(data, i)
            rec = u16(data, i + 4)
            nlen = data[i + 6]
            ftype = data[i + 7]
            if rec < 8 or i + rec > len(data):
                break
            if inum and nlen:
                live.append((inum, data[i + 8:i + 8 + nlen].decode("utf-8", "replace"), ftype))
            # buscar entradas borradas en el espacio sobrante
            real = (8 + nlen + 3) & ~3
            j = i + real
            while j + 8 <= i + rec:
                di = u32(data, j)
                drec = u16(data, j + 4)
                dn = data[j + 6]
                dt = data[j + 7]
                if di and 0 < dn <= 255 and j + 8 + dn <= i + rec and dt <= 7 and di <= self.inodes_count:
                    nm = data[j + 8:j + 8 + dn]
                    try:
                        name = nm.decode("utf-8")
                        if name.isprintable() and "/" not in name:
                            dead.append((di, name, dt))
                            j += (8 + dn + 3) & ~3
                            continue
                    except UnicodeDecodeError:
                        pass
                j += 4
            i += rec
        return live, dead

    def _looks_dir_block(self, data: bytes) -> bool:
        i = 0
        while i + 8 <= len(data):
            rec = u16(data, i + 4)
            if rec < 8 or rec % 4 or i + rec > len(data) or data[i + 6] + 8 > rec:
                return False
            i += rec
        return i == len(data)

    def journal_inodes(self, progress=None, cancel=None) -> Dict[int, dict]:
        """Extrae copias de inodos (y nombres de bloques de directorio) guardadas en el journal."""
        found: Dict[int, dict] = {}
        self.journal_names: Dict[int, Tuple[str, int]] = {}
        try:
            jino = self.parse_inode(self.inode_raw(self.journal_inum))
            if not jino or not jino["size"]:
                return found
            jmap = []
            for lblk, phys, cnt in self.mapping(jino):
                for k in range(cnt):
                    jmap.append(phys + k)
            jsb = self.src.read(self.block_off(jmap[0]), self.bs)
            if be32(jsb, 0) != 0xC03B3998:
                return found
            incompat = be32(jsb, 0x28) if be32(jsb, 4) == 4 else 0
            csum3 = bool(incompat & 0x10)
            is64 = bool(incompat & 0x2)
            csum2 = bool(incompat & 0x8)
            tag_size = 16 if csum3 else (8 + (4 if is64 else 0) + (0 if not csum2 else 0))
            if not csum3 and not csum2:
                tag_size = 8 + (4 if is64 else 0)
            # rangos de tablas de inodos
            it_blocks = (self.ipg * self.inode_size + self.bs - 1) // self.bs
            tables = sorted((t, g) for g, t in enumerate(self.itables) if t)
            n = len(jmap)
            i = 1
            while i < n:
                if cancel and cancel():
                    break
                blk = self.src.read(self.block_off(jmap[i]), self.bs)
                if be32(blk, 0) != 0xC03B3998 or be32(blk, 4) != 1:
                    i += 1
                    continue
                o = 12
                k = i + 1
                while o + tag_size <= self.bs:
                    fsblk = be32(blk, o)
                    if csum3:
                        tflags = be32(blk, o + 4)
                        if is64:
                            fsblk |= be32(blk, o + 8) << 32
                    else:
                        tflags = struct.unpack_from(">H", blk, o + 6)[0]
                        if is64:
                            fsblk |= be32(blk, o + 8) << 32
                    o += tag_size
                    if not tflags & 0x2:
                        o += 16
                    if k < n:
                        hit_table = False
                        for t, g in tables:
                            if t <= fsblk < t + it_blocks:
                                data = self.src.read(self.block_off(jmap[k]), self.bs)
                                first = g * self.ipg + (fsblk - t) * (self.bs // self.inode_size) + 1
                                for q in range(self.bs // self.inode_size):
                                    raw = data[q * self.inode_size:(q + 1) * self.inode_size]
                                    ino = self.parse_inode(raw)
                                    if ino and ino["mode"] and ino["links"] and ino["size"]:
                                        mp = ino["iblock"]
                                        has = (u16(mp, 0) == 0xF30A and u16(mp, 2) > 0) or \
                                              (not ino["flags"] & 0x80000 and u32(mp, 0))
                                        if has:
                                            found[first + q] = ino
                                hit_table = True
                                break
                        if not hit_table:
                            data = self.src.read(self.block_off(jmap[k]), self.bs)
                            if data[8:10] in (b".\0", b".\x00") or self._looks_dir_block(data):
                                if self._looks_dir_block(data):
                                    live, dead = self._parse_dir(data)
                                    dir_ino = live[0][0] if live and live[0][1] == "." else 0
                                    for ci, nm, ft in live + dead:
                                        if nm not in (".", "..") and ft != 2:
                                            self.journal_names.setdefault(ci, (nm, dir_ino))
                    k += 1
                    if tflags & 0x8:
                        break
                i = k
                if progress and i % 256 == 0:
                    progress(i, n)
        except Exception:
            pass
        return found

    def entries(self, progress=None, cancel=None) -> List[FileEntry]:
        out: List[FileEntry] = []
        names: Dict[int, Tuple[str, str]] = {}
        live_inodes = set()
        stack = [(2, "")]
        seen = set()
        dir_paths: Dict[int, str] = {2: ""}
        while stack:
            if cancel and cancel():
                break
            inum, path = stack.pop()
            if inum in seen:
                continue
            seen.add(inum)
            ino = self.parse_inode(self.inode_raw(inum))
            if not ino:
                continue
            data = self.read_all(ino, limit=32 << 20)
            live, dead = self._parse_dir(data)
            for ci, nm, ft in live:
                if nm in (".", "..") or ci in seen:
                    continue
                live_inodes.add(ci)
                child = self.parse_inode(self.inode_raw(ci))
                if not child:
                    continue
                if child["mode"] & 0xF000 == S_IFDIR:
                    out.append(FileEntry(name=nm, path=path, is_dir=True, fs=self.fs_name,
                                         mtime=child["mtime"], ctime=child["ctime"], ident=f"inodo {ci}"))
                    stack.append((ci, (path + "/" + nm) if path else nm))
                    dir_paths[ci] = (path + "/" + nm) if path else nm
                elif child["mode"] & 0xF000 == S_IFREG:
                    fe = self.content_entry(child)
                    fe.name, fe.path, fe.ident, fe.chance = nm, path, f"inodo {ci}", "Excelente"
                    out.append(fe)
            for ci, nm, ft in dead:
                names.setdefault(ci, (nm, path))
            if progress:
                progress(len(seen), len(seen) + len(stack))
        # archivos borrados: inodo actual y, si fue borrado, copia previa del journal
        jcopies = self.journal_inodes(progress, cancel)
        first_ino = u32(self.sb, 0x54) if u32(self.sb, 0x4C) >= 1 else 11
        for ci, (nm, dino) in getattr(self, "journal_names", {}).items():
            if ci not in names and ci not in live_inodes:
                names[ci] = (nm, dir_paths.get(dino, "$Huérfanos"))
        cand = set(names) | set(jcopies)
        for ci in sorted(cand):
            if ci in live_inodes or ci < first_ino:
                continue
            cur = self.parse_inode(self.inode_raw(ci))
            ino = None
            if cur and cur["mode"] & 0xF000 == S_IFREG and cur["size"] and self.mapping(cur):
                ino = cur
            elif ci in jcopies and jcopies[ci]["mode"] & 0xF000 == S_IFREG:
                ino = jcopies[ci]
            nm, path = names.get(ci, (f"inodo_{ci}", "$Huérfanos"))
            if path == "" and ci not in names:
                path = "$Huérfanos"
            if ino is None:
                if ci in names and cur and cur["mode"] & 0xF000 != S_IFDIR:
                    out.append(FileEntry(name=nm, path=path, deleted=True, fs=self.fs_name,
                                         ident=f"inodo {ci}", chance="Baja",
                                         note="Solo el nombre sobrevive; use el escaneo profundo"))
                continue
            fe = self.content_entry(ino)
            fe.name, fe.path, fe.deleted, fe.ident = nm, path, True, f"inodo {ci}"
            fe.chance = "Buena" if ino is cur else "Regular"
            fe.note = "" if ino is cur else "Recuperado desde copia del journal"
            out.append(fe)
        return out
