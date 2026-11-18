"""ReFS 3.x (Windows Server 2016+ / Windows 10+): superbloque, puntos de control,
tabla de contenedores (traducción de LCN virtuales), tabla de objetos, tablas de
directorio, registros de archivo incrustados con sus extensiones de datos, y
recuperación de archivos borrados desde páginas de directorio anteriores que el
copy-on-write dejó en el disco.

ReFS no está documentado por Microsoft; este lector se basa en el análisis de
volúmenes reales (versión 3.7) y en publicaciones forenses sobre ReFS 3.x."""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from ..device import BlockSource
from ..model import FileEntry
from .ntfs import filetime

ROOT_DIR = 0x600


def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]


def node_rows(pg: bytes, start: int = 0x50) -> Tuple[int, List[Tuple[bytes, bytes, int]]]:
    """Filas (clave, valor, indicadores) de un nodo MSB+ o de una tabla incrustada."""
    try:
        h = start + u32(pg, start)
        ks, nk = u32(pg, h + 16), u32(pg, h + 20)
        height = pg[h + 12]
        rows = []
        if nk > 4096:
            return 0, []
        for i in range(nk):
            ko = u32(pg, h + ks + 4 * i) & 0xFFFF
            e = h + ko
            size, koff, klen, flg, voff, vlen = struct.unpack_from("<IHHHHI", pg, e)
            if size == 0 or e + size > len(pg) + 16:
                continue
            rows.append((pg[e + koff:e + koff + klen], pg[e + voff:e + voff + vlen], flg))
        return height, rows
    except struct.error:
        return 0, []


def stale_rows(pg: bytes, start: int = 0x50) -> List[Tuple[bytes, bytes, int]]:
    """Filas que siguen en el área de datos del nodo pero ya no están indexadas (borradas)."""
    out = []
    try:
        h = start + u32(pg, start)
        ds, de = u32(pg, h), u32(pg, h + 4)
        ks, nk = u32(pg, h + 16), u32(pg, h + 20)
        live = {h + (u32(pg, h + ks + 4 * i) & 0xFFFF) for i in range(min(nk, 4096))}
        pos, end = h + ds, min(len(pg) - 16, h + max(de, ds))
        end = len(pg) - 16
        while pos < end:
            size, koff, klen, flg, voff, vlen = struct.unpack_from("<IHHHHI", pg, pos)
            ok = (16 <= size <= 0x4000 and koff == 0x10 and 4 < klen < 600 and voff >= koff + klen
                  and voff + vlen <= size + 8 and pos + size <= len(pg))
            if ok and pos in live:
                pos += (size + 7) & ~7
                continue
            if ok:
                out.append((pg[pos + koff:pos + koff + klen], pg[pos + voff:pos + voff + vlen], flg))
            pos += 4
    except struct.error:
        pass
    return out


class ReFS:
    name = "ReFS"

    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src, self.base = src, base
        b = src.read(base, 512)
        if b[3:7] != b"ReFS":
            raise ValueError("No es ReFS")
        self.cluster = u32(b, 0x20) * u32(b, 0x24)
        self.version = (b[0x28], b[0x29])
        self.size = u64(b, 0x18) * u32(b, 0x20)
        if self.version[0] < 3:
            raise NotImplementedError("ReFS 1.x/2.x: use el escaneo profundo por firmas")
        self.page_clusters = max(1, 16384 // self.cluster)
        sb = self.read_clusters(0x1E, 1)
        if sb[:4] != b"SUPB":
            raise ValueError("Superbloque ReFS no encontrado")
        off, cnt = u32(sb, 0x70), u32(sb, 0x74)
        cps = [u64(sb, off + 8 * i) for i in range(cnt)]
        best = None
        for c in cps:
            cp = self.read_clusters(c, 1)
            if cp[:4] == b"CHKP" and (best is None or u64(cp, 0x60) > u64(best, 0x60)):
                best = cp
        if best is None:
            raise ValueError("Punto de control ReFS no encontrado")
        self.cp = best
        n = u32(best, 0x90)
        self.tables = []
        for i in range(n):
            o = u32(best, 0x94 + 4 * i)
            self.tables.append([u64(best, o + 8 * k) for k in range(4)])
        self.containers: Dict[int, int] = {}
        self.shift = 0
        self._load_containers()
        self._objects: Optional[Dict[int, List[int]]] = None

    def read_clusters(self, lcn: int, n: int) -> bytes:
        return self.src.read(self.base + lcn * self.cluster, n * self.cluster)

    def _load_containers(self) -> None:
        ref = self.tables[7] if len(self.tables) > 7 else []
        pg = self.page(ref, virtual=False)
        _, rows = node_rows(pg)
        cont = {}
        for k, v, _ in rows:
            if len(k) >= 8 and len(v) >= 0xA0:
                cont[u64(k, 0)] = u64(v, 0x90)
        self.containers = cont
        # deducir el tamaño de banda virtual: el que traduce la tabla de objetos a una página válida
        target = self.tables[0][0] if self.tables else 0
        for sh in range(10, 24):
            cid = target >> sh
            if cid in cont:
                ph = cont[cid] + (target & ((1 << sh) - 1))
                b = self.read_clusters(ph, 1)
                if b[:4] == b"MSB+" and u64(b, 0x20) == target:
                    self.shift = sh
                    return
        self.shift = 0

    def v2p(self, v: int) -> int:
        if not self.shift:
            return v
        cid = v >> self.shift
        return self.containers.get(cid, 0) + (v & ((1 << self.shift) - 1))

    def page(self, lcns: List[int], virtual: bool = True) -> bytes:
        parts = [l for l in lcns if l] or lcns[:1]
        return b"".join(self.read_clusters(self.v2p(l) if virtual else l, 1) for l in parts[:self.page_clusters])

    def tree_rows(self, lcns: List[int], guard: int = 0) -> List[Tuple[bytes, bytes, int]]:
        pg = self.page(lcns)
        if pg[:4] != b"MSB+" or guard > 8:
            return []
        height, rows = node_rows(pg)
        if height == 0:
            return rows
        out = []
        for k, v, _ in rows:
            if len(v) >= 32:
                out += self.tree_rows([u64(v, 8 * i) for i in range(4)], guard + 1)
        return out

    def objects(self) -> Dict[int, List[int]]:
        if self._objects is None:
            self._objects = {}
            for k, v, _ in self.tree_rows(self.tables[0]):
                if len(k) >= 16 and len(v) >= 0x40:
                    self._objects[u64(k, 8)] = [u64(v, 0x20 + 8 * i) for i in range(4)]
        return self._objects

    # ------------------------------------------------------------------ #
    def _runs(self, rec: bytes) -> List[Tuple[int, int, int]]:
        """Extensiones (vcn, lcn_virtual, clusters) del atributo de datos del registro."""
        runs = []
        _, attrs = node_rows(rec, 0)
        attrs = attrs + stale_rows(rec, 0)
        for k, v, _ in attrs:
            if runs:
                break
            if len(k) < 14 or u16(k, 12) != 0x80 or len(v) < 0x40:
                continue
            # filas de la tabla de extensiones: (lcn u64, …, vcn u32 @12, clusters u32 @20)
            try:
                h = u32(v, 0)
                ds, ks, nk = u32(v, h), u32(v, h + 16), u32(v, h + 20)
                offs = [h + (u32(v, h + ks + 4 * i) & 0xFFFF) for i in range(min(nk, 4096))]
                if not offs:  # registro de un archivo borrado: el índice se vació pero las filas siguen
                    offs = [h + ds + 24 * i for i in range(64)]
                for off in offs:
                    row = v[off:off + 24]
                    if len(row) < 24:
                        break
                    lcn, vcn, cnt = u64(row, 0), u32(row, 12), u32(row, 20)
                    if not lcn or not cnt or (self.shift and (lcn >> self.shift) not in self.containers):
                        if nk:
                            continue
                        break
                    runs.append((vcn, lcn, cnt))
            except struct.error:
                continue
        return sorted(runs)

    def _file(self, name: str, path: str, rec: bytes, deleted: bool, ident: str) -> FileEntry:
        size = u64(rec, 0x58) if len(rec) >= 0x60 else 0
        e = FileEntry(name=name, path=path, size=size, deleted=deleted, fs="ReFS",
                      ctime=filetime(u64(rec, 0x28)), mtime=filetime(u64(rec, 0x30)), atime=filetime(u64(rec, 0x40)),
                      ident=ident)
        exts, pos = [], 0
        for vcn, lcn, cnt in self._runs(rec):
            if vcn > pos:
                exts.append((-1, (vcn - pos) * self.cluster))
            exts.append((self.base + self.v2p(lcn) * self.cluster, cnt * self.cluster))
            pos = vcn + cnt
        rem = size
        for o, l in exts:
            if rem <= 0:
                break
            e.extents.append((o, min(l, rem)))
            rem -= l
        if size and not e.extents:
            e.note = "No se encontraron las extensiones; use el escaneo profundo"
        return e

    def _dir_rows(self, rows):
        for k, v, fl in rows:
            if len(k) < 6 or u16(k, 0) != 0x30:
                continue
            kind = u16(k, 2)
            name = k[4:].decode("utf-16-le", "replace")
            yield kind, name, v

    def entries(self, progress=None, cancel=None) -> List[FileEntry]:
        objs = self.objects()
        out: List[FileEntry] = []
        dirs: Dict[int, str] = {ROOT_DIR: ""}
        live_names: Dict[int, set] = {}
        stack = [ROOT_DIR]
        seen = set()
        while stack:
            if cancel and cancel():
                break
            oid = stack.pop()
            if oid in seen or oid not in objs:
                continue
            seen.add(oid)
            path = dirs[oid]
            names = live_names.setdefault(oid, set())
            for kind, name, v in self._dir_rows(self.tree_rows(objs[oid])):
                names.add(name)
                if kind == 2 and len(v) >= 16:
                    child = u64(v, 8)
                    if name == "System Volume Information":
                        continue
                    dirs[child] = (path + "/" + name) if path else name
                    out.append(FileEntry(name=name, path=path, is_dir=True, fs="ReFS",
                                         mtime=filetime(u64(v, 24)) if len(v) >= 32 else 0,
                                         ident=f"objeto 0x{child:X}"))
                    stack.append(child)
                elif kind == 1:
                    e = self._file(name, path, v, False, f"dir 0x{oid:X}")
                    e.chance = "Excelente"
                    out.append(e)
            if progress:
                progress(len(seen), len(seen) + len(stack))
        # filas no indexadas de las páginas vivas y páginas antiguas (copy-on-write)
        found_old: Dict[Tuple[int, str], bytes] = {}
        for oid in list(dirs):
            if oid in objs:
                pg = self.page(objs[oid])
                for kind, name, v in self._dir_rows(stale_rows(pg)):
                    if kind == 1 and name not in live_names.get(oid, ()):
                        found_old.setdefault((oid, name), v)
        total = self.size // self.cluster
        step = 256
        for start in range(0, total, step):
            if cancel and cancel():
                break
            blk = self.read_clusters(start, step)
            for i in range(0, len(blk), self.cluster):
                if blk[i:i + 4] != b"MSB+":
                    continue
                oid = u64(blk, i + 0x48)
                if oid not in dirs:
                    continue
                pg = self.read_clusters(start + i // self.cluster, self.page_clusters)
                _, rows = node_rows(pg)
                for kind, name, v in self._dir_rows(rows + stale_rows(pg)):
                    if kind == 1 and name not in live_names.get(oid, ()):
                        found_old.setdefault((oid, name), v)
        for (oid, name), v in found_old.items():
            e = self._file(name, dirs[oid], v, True, f"dir 0x{oid:X}")
            if not e.extents:
                continue
            e.chance = "Regular"
            e.note = "Recuperado de una versión anterior de la tabla de directorio"
            out.append(e)
        return out
