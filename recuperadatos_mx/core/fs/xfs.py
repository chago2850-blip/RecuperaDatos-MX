"""XFS (v4 y v5): árbol de directorios (shortform, bloque, hoja y nodo), extensiones
y B+tree de extensiones, y recuperación de archivos borrados.

Al borrar, XFS pone el modo del inodo a 0 pero suele dejar intactas las
extensiones dentro del inodo; los nombres sobreviven en el espacio libre de
los bloques de directorio (con los 32 bits bajos del número de inodo).
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from ..device import BlockSource
from ..model import FileEntry

DIR2_LEAF_OFFSET = 1 << 35  # 32 GiB en bytes


def be16(b, o): return struct.unpack_from(">H", b, o)[0]
def be32(b, o): return struct.unpack_from(">I", b, o)[0]
def be64(b, o): return struct.unpack_from(">Q", b, o)[0]


class XFS:
    name = "XFS"

    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src, self.base = src, base
        sb = src.read(base, 512)
        if sb[:4] != b"XFSB":
            raise ValueError("No es XFS")
        self.bs = be32(sb, 4)
        self.dblocks = be64(sb, 8)
        self.size = self.bs * self.dblocks
        self.rootino = be64(sb, 56)
        self.agblocks = be32(sb, 84)
        self.agcount = be32(sb, 88)
        self.version = be16(sb, 100) & 0xF
        self.sectsize = be16(sb, 102)
        self.isize = be16(sb, 104)
        self.blocklog, self.sectlog, self.inodelog, self.inopblog, self.agblklog = sb[120:125]
        self.dirbs = self.bs << sb[192]
        self.v5 = self.version == 5
        self.ftype = bool(be32(sb, 216) & 1) if self.v5 else bool(be32(sb, 200) & 0x200)
        self.core = 176 if self.v5 else 100
        self.label = sb[108:120].split(b"\0")[0].decode("utf-8", "replace")
        self.logstart = be64(sb, 48)
        self.logblocks = be32(sb, 96)

    # ------------------------------------------------------------------ #
    def fsb_to_off(self, fsb: int) -> int:
        ag = fsb >> self.agblklog
        agb = fsb & ((1 << self.agblklog) - 1)
        return self.base + (ag * self.agblocks + agb) * self.bs

    def ino_off(self, ino: int) -> int:
        ag = ino >> (self.agblklog + self.inopblog)
        agb = (ino >> self.inopblog) & ((1 << self.agblklog) - 1)
        idx = ino & ((1 << self.inopblog) - 1)
        return self.base + (ag * self.agblocks + agb) * self.bs + idx * self.isize

    def inode(self, ino: int) -> Optional[dict]:
        raw = self.src.read(self.ino_off(ino), self.isize)
        if raw[:2] != b"IN":
            return None
        ver = raw[4]
        core = 176 if ver == 3 else 100
        forkoff = raw[82] * 8
        dsize = (forkoff or (self.isize - core))
        return {"mode": be16(raw, 2), "fmt": raw[5], "nlink": be32(raw, 16) if ver >= 2 else be16(raw, 6),
                "atime": be32(raw, 32), "mtime": be32(raw, 40), "ctime": be32(raw, 48),
                "size": be64(raw, 56), "nextents": be32(raw, 76), "fork": raw[core:core + dsize], "ino": ino}

    @staticmethod
    def _extent(rec: bytes) -> Tuple[int, int, int, bool]:
        v = int.from_bytes(rec, "big")
        return ((v >> 73) & ((1 << 54) - 1), (v >> 21) & ((1 << 52) - 1), v & ((1 << 21) - 1), bool(v >> 127))

    def extents(self, ino: dict, raw_scan: bool = False) -> List[Tuple[int, int, int]]:
        """[(bloque_lógico, fsb, cantidad)]"""
        fork = ino["fork"]
        out = []
        if ino["fmt"] == 2:
            n = len(fork) // 16 if raw_scan else ino["nextents"]
            for i in range(min(n, len(fork) // 16)):
                r = fork[i * 16:(i + 1) * 16]
                if raw_scan and r == b"\0" * 16:
                    break
                off, fsb, cnt, _ = self._extent(r)
                if cnt == 0 or fsb == 0 or self.fsb_to_off(fsb) >= self.base + self.size:
                    if raw_scan:
                        break
                    continue
                out.append((off, fsb, cnt))
        elif ino["fmt"] == 3:
            level, nrecs = be16(fork, 0), be16(fork, 2)
            maxrecs = (len(fork) - 4) // 16
            ptrs = [be64(fork, 4 + maxrecs * 8 + i * 8) for i in range(nrecs)]
            for p in ptrs:
                out += self._bmbt(p, level - 1)
        return sorted(out)

    def _bmbt(self, fsb: int, level: int, guard: int = 0) -> List[Tuple[int, int, int]]:
        if guard > 8:
            return []
        blk = self.src.read(self.fsb_to_off(fsb), self.bs)
        hdr = 72 if blk[:4] == b"BMA3" else 24
        if blk[:4] not in (b"BMA3", b"BMAP"):
            return []
        nrecs = be16(blk, 6)
        if level == 0 or be16(blk, 4) == 0:
            return [self._extent(blk[hdr + i * 16:hdr + (i + 1) * 16])[:3] for i in range(nrecs)]
        maxrecs = (self.bs - hdr) // 16
        out = []
        for i in range(nrecs):
            out += self._bmbt(be64(blk, hdr + maxrecs * 8 + i * 8), level - 1, guard + 1)
        return out

    def byte_extents(self, exts) -> List[Tuple[int, int]]:
        res: List[Tuple[int, int]] = []
        expect = 0
        for lblk, fsb, cnt in exts:
            if lblk > expect:
                res.append((-1, (lblk - expect) * self.bs))
            o = self.fsb_to_off(fsb)
            if res and res[-1][0] >= 0 and res[-1][0] + res[-1][1] == o:
                res[-1] = (res[-1][0], res[-1][1] + cnt * self.bs)
            else:
                res.append((o, cnt * self.bs))
            expect = lblk + cnt
        return res

    def read_blocks(self, exts, limit_lblk: Optional[int] = None) -> List[Tuple[int, bytes]]:
        """Bloques de directorio: [(desplazamiento_lógico, datos)]"""
        out = []
        for lblk, fsb, cnt in exts:
            if limit_lblk is not None and lblk * self.bs >= limit_lblk:
                continue
            data = self.src.read(self.fsb_to_off(fsb), cnt * self.bs)
            for k in range(0, len(data), self.dirbs):
                out.append(((lblk * self.bs) + k, data[k:k + self.dirbs]))
        return out

    # ------------------------------------------------------------------ #
    def _shortform(self, fork: bytes):
        count, i8 = fork[0], fork[1]
        isz = 8 if i8 else 4
        p = 2 + isz
        out = []
        for _ in range(count + i8 if not count else count):
            if p >= len(fork):
                break
            nl = fork[p]
            name = fork[p + 3:p + 3 + nl].decode("utf-8", "replace")
            p += 3 + nl
            ft = None
            if self.ftype:
                ft = fork[p]
                p += 1
            ino = int.from_bytes(fork[p:p + isz], "big")
            p += isz
            out.append((ino, name, ft))
        return out, []

    def _data_block(self, blk: bytes):
        magic = blk[:4]
        if magic in (b"XDB3", b"XDD3"):
            p = 64
        elif magic in (b"XD2B", b"XD2D"):
            p = 16
        else:
            return [], []
        end = len(blk)
        if magic in (b"XDB3", b"XD2B"):
            cnt = be32(blk, end - 8)
            end = end - 8 - cnt * 8
        live, dead = [], []
        while p + 8 <= end:
            if be16(blk, p) == 0xFFFF:
                ln = be16(blk, p + 2)
                if ln < 8 or p + ln > end:
                    break
                # buscar entradas borradas dentro del hueco
                q = p
                while q + 12 <= p + ln:
                    ino_lo = be32(blk, q + 4)
                    nl = blk[q + 8]
                    if 0 < nl < 255 and q + 9 + nl <= p + ln and ino_lo:
                        nm = blk[q + 9:q + 9 + nl]
                        try:
                            name = nm.decode("utf-8")
                            if name.isprintable() and "/" not in name and name not in (".", ".."):
                                dead.append((ino_lo, name, None))
                                q += (9 + nl + (1 if self.ftype else 0) + 2 + 7) & ~7
                                continue
                        except UnicodeDecodeError:
                            pass
                    q += 8
                p += ln
                continue
            ino = be64(blk, p)
            nl = blk[p + 8]
            name = blk[p + 9:p + 9 + nl].decode("utf-8", "replace")
            ft = blk[p + 9 + nl] if self.ftype else None
            live.append((ino, name, ft))
            p += (9 + nl + (1 if self.ftype else 0) + 2 + 7) & ~7
        return live, dead

    def read_dir(self, ino: dict):
        if ino["fmt"] == 1:
            return self._shortform(ino["fork"])
        live, dead = [], []
        for off, blk in self.read_blocks(self.extents(ino), DIR2_LEAF_OFFSET):
            l, d = self._data_block(blk)
            live += l
            dead += d
        return live, dead

    def inodes_from_inobt(self) -> List[int]:
        out = []
        for ag in range(self.agcount):
            agi = self.src.read(self.base + ag * self.agblocks * self.bs + 2 * self.sectsize, self.sectsize)
            if agi[:4] != b"XAGI":
                continue
            root, level = be32(agi, 20), be32(agi, 24)
            out += self._inobt(ag, root, level - 1)
        return out

    def _inobt(self, ag: int, agbno: int, level: int, guard: int = 0) -> List[int]:
        if guard > 8:
            return []
        blk = self.src.read(self.base + (ag * self.agblocks + agbno) * self.bs, self.bs)
        hdr = 56 if blk[:4] == b"IAB3" else 16
        n = be16(blk, 6)
        out = []
        if be16(blk, 4) == 0:
            for i in range(n):
                start = be32(blk, hdr + i * 16)
                for k in range(64):
                    out.append((ag << (self.agblklog + self.inopblog)) | (start + k))
            return out
        maxrecs = (self.bs - hdr) // 8
        for i in range(n):
            out += self._inobt(ag, be32(blk, hdr + maxrecs * 4 + i * 4), level - 1, guard + 1)
        return out

    def _entry(self, ino: dict, name: str, path: str, deleted: bool, raw: bool = False) -> FileEntry:
        exts = self.extents(ino, raw_scan=raw)
        size = ino["size"] or sum(c for _, _, c in exts) * self.bs
        e = FileEntry(name=name, path=path, size=size, deleted=deleted, fs="XFS", mtime=ino["mtime"],
                      ctime=ino["ctime"], atime=ino["atime"], ident=f"inodo {ino['ino']}",
                      extents=self.byte_extents(exts))
        if ino["fmt"] == 1 and not deleted:
            e.resident = ino["fork"][:ino["size"]]
            e.extents = []
        return e

    # ------------------------------------------------------------------ #
    def log_items(self, cancel=None):
        """Recorre el log interno y devuelve copias de inodos: {ino: [(core, fork, tipo_fork)]}."""
        found: Dict[int, list] = {}
        if not self.logstart or not self.logblocks:
            return found
        off0 = self.fsb_to_off(self.logstart)
        total = self.logblocks * self.bs
        data = self.src.read(off0, total)
        pos = 0
        while pos + 512 <= len(data):
            if cancel and cancel():
                break
            if be32(data, pos) != 0xFEEDBABE:
                pos += 512
                continue
            h = data[pos:pos + 512]
            ver, hlen, nops = be32(h, 8), be32(h, 12), be32(h, 40)
            hsize = be32(h, 320) if ver == 2 else 32768
            hsect = max(1, (hsize + 32767) // 32768) if ver == 2 else 1
            cyc = [h[44 + i * 4:48 + i * 4] for i in range(64)]
            for x in range(1, hsect):
                eh = data[pos + x * 512:pos + (x + 1) * 512]
                cyc += [eh[4 + i * 4:8 + i * 4] for i in range(63)]
            start = pos + hsect * 512
            if hlen <= 0 or start + hlen > len(data):
                pos += 512
                continue
            rec = bytearray(data[start:start + hlen])
            for i in range(0, min(len(cyc), (hlen + 511) // 512)):
                rec[i * 512:i * 512 + 4] = cyc[i]
            self._parse_ops(bytes(rec), nops, found)
            pos = start + ((hlen + 511) // 512) * 512
        return found

    def _parse_ops(self, rec: bytes, nops: int, found: Dict[int, list]) -> None:
        p = 0
        ops = []
        while p + 12 <= len(rec) and len(ops) < nops:
            ln = be32(rec, p + 4)
            if p + 12 + ln > len(rec):
                break
            ops.append(rec[p + 12:p + 12 + ln])
            p += 12 + ln
        i = 0
        while i < len(ops):
            o = ops[i]
            if len(o) >= 16 and struct.unpack_from("<H", o, 0)[0] == 0x123B:
                nreg = struct.unpack_from("<H", o, 2)[0]
                fields = struct.unpack_from("<I", o, 4)[0]
                ino = struct.unpack_from("<Q", o, 16)[0]
                regs = ops[i + 1:i + nreg]
                if regs and len(regs[0]) >= 96:
                    core = regs[0]
                    fork = regs[1] if len(regs) > 1 and fields & 0x0E else b""
                    kind = "ext" if fields & 0x4 else ("local" if fields & 0x2 else ("btree" if fields & 0x8 else ""))
                    found.setdefault(ino, []).append((core, fork, kind))
                i += max(1, nreg)
                continue
            i += 1

    def entries(self, progress=None, cancel=None) -> List[FileEntry]:
        out: List[FileEntry] = []
        seen = set()
        names: Dict[int, Tuple[str, str]] = {}
        stack = [(self.rootino, "")]
        while stack:
            if cancel and cancel():
                break
            ino_n, path = stack.pop()
            if ino_n in seen:
                continue
            seen.add(ino_n)
            ino = self.inode(ino_n)
            if not ino:
                continue
            live, dead = self.read_dir(ino)
            for ci, nm, ft in live:
                if nm in (".", "..") or ci in seen:
                    continue
                child = self.inode(ci)
                if not child:
                    continue
                kind = child["mode"] & 0xF000
                if kind == 0x4000:
                    out.append(FileEntry(name=nm, path=path, is_dir=True, fs="XFS", mtime=child["mtime"],
                                         ident=f"inodo {ci}"))
                    stack.append((ci, (path + "/" + nm) if path else nm))
                elif kind == 0x8000:
                    seen.add(ci)
                    e = self._entry(child, nm, path, False)
                    e.chance = "Excelente"
                    out.append(e)
            for lo, nm, _ in dead:
                names.setdefault(lo, (nm, path))
            if progress:
                progress(len(seen), len(seen) + len(stack))
        # copias del log: nombres de directorios pequeños y extensiones previas al borrado
        log = self.log_items(cancel)
        dir_paths = {self.rootino: ""}
        for e in out:
            if e.is_dir:
                dir_paths[int(e.ident.split()[1])] = e.full_path
        log_ext: Dict[int, dict] = {}
        for ino_n, copies in log.items():
            for core, fork, kind in copies:
                mode = struct.unpack_from("<H", core, 2)[0]
                if mode & 0xF000 == 0x4000 and kind == "local" and ino_n in dir_paths:
                    try:
                        l, _ = self._shortform(fork)
                    except (IndexError, struct.error):
                        continue
                    for ci, nm, _ in l:
                        if ci not in seen:
                            names.setdefault(ci & 0xFFFFFFFF, (nm, dir_paths[ino_n]))
                elif mode & 0xF000 == 0x8000 and kind == "ext" and fork.strip(b"\0"):
                    log_ext[ino_n] = {"mode": mode, "fmt": 2, "nlink": 1, "size": struct.unpack_from("<Q", core, 56)[0],
                                      "nextents": struct.unpack_from("<I", core, 76)[0], "fork": fork,
                                      "mtime": struct.unpack_from("<I", core, 40)[0],
                                      "ctime": struct.unpack_from("<I", core, 48)[0],
                                      "atime": struct.unpack_from("<I", core, 32)[0], "ino": ino_n}
        for ino_n, ino in log_ext.items():
            if ino_n in seen:
                continue
            cur = self.inode(ino_n)
            if cur and cur["mode"] != 0:
                continue
            if not ino["size"] or not self.extents(ino):
                continue
            seen.add(ino_n)
            nm, path = names.get(ino_n & 0xFFFFFFFF, (f"inodo_{ino_n}", "$Huérfanos"))
            e = self._entry(ino, nm, path, True)
            e.chance = "Buena"
            e.note = "Recuperado desde copia del log de XFS"
            out.append(e)
        # inodos liberados: modo 0 pero con extensiones aún presentes
        for ino_n in self.inodes_from_inobt():
            if cancel and cancel():
                break
            if ino_n in seen:
                continue
            ino = self.inode(ino_n)
            if not ino or ino["mode"] != 0:
                continue
            if ino["fmt"] != 2:
                continue
            exts = self.extents(ino, raw_scan=True)
            if not exts:
                continue
            nm, path = names.get(ino_n & 0xFFFFFFFF, (f"inodo_{ino_n}", "$Huérfanos"))
            e = self._entry(ino, nm, path, True, raw=True)
            e.chance = "Buena"
            e.note = "Tamaño aproximado al bloque; se ajusta con la firma del archivo"
            out.append(e)
        return out
