"""Recuperación de registros borrados en bases SQLite (mensajes SMS/iMessage,
WhatsApp, llamadas, notas, contactos, historial de navegación…).

SQLite no borra físicamente los registros: quedan en bloques libres de las
páginas, en el espacio no asignado de cada página y en páginas de la lista
libre.  Este módulo recorre esas zonas y decodifica los registros que encuentra."""
from __future__ import annotations

import csv
import os
import sqlite3
import struct
from typing import Dict, List, Optional, Tuple


def _varint(b: bytes, p: int) -> Tuple[int, int]:
    v = 0
    for i in range(9):
        if p + i >= len(b):
            raise IndexError
        c = b[p + i]
        if i == 8:
            return (v << 8) | c, p + 9
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            return v, p + i + 1
    return v, p + 9


def _col_size(t: int) -> int:
    if t in (0, 8, 9, 12, 13):
        return 0
    if t in (1, 2, 3, 4):
        return t
    if t == 5:
        return 6
    if t in (6, 7):
        return 8
    if t >= 12:
        return (t - 12) // 2 if t % 2 == 0 else (t - 13) // 2
    raise ValueError


def _value(t: int, b: bytes, enc: str):
    if t == 0:
        return None
    if t == 8:
        return 0
    if t == 9:
        return 1
    if 1 <= t <= 6:
        n = _col_size(t)
        return int.from_bytes(b[:n], "big", signed=True)
    if t == 7:
        return struct.unpack(">d", b[:8])[0]
    if t >= 13 and t % 2 == 1:
        return b.decode(enc, "replace")
    return b  # BLOB


def decode_record(b: bytes, p: int, ncols: Optional[int], enc: str, max_end: int):
    """Decodifica un registro (cabecera + cuerpo) que empieza en ``p``."""
    hlen, q = _varint(b, p)
    if hlen < 2 or hlen > 512 or p + hlen > max_end:
        return None
    types = []
    end_h = p + hlen
    while q < end_h:
        t, q = _varint(b, q)
        if t in (10, 11):
            return None
        types.append(t)
    if not types or (ncols and not (ncols - 2 <= len(types) <= ncols)):
        return None
    body = end_h
    vals = []
    for t in types:
        n = _col_size(t)
        if body + n > max_end:
            return None
        vals.append(_value(t, b[body:body + n], enc))
        body += n
    if not any(isinstance(v, str) and len(v) > 1 for v in vals):
        return None
    return vals, body


def decode_partial(b: bytes, s: int, ncols: int, enc: str, end: int):
    """Registro cuya cabecera perdió sus primeros bytes (bloque libre: 4 bytes sobrescritos).
    Se suponen nulas las primeras columnas perdidas (p. ej. el ROWID alias)."""
    for missing in (1, 0, 2):
        try:
            q = s
            types = [0] * missing
            while len(types) < ncols:
                t, q = _varint(b, q)
                if t in (10, 11) or t > 2 * 65536:
                    raise ValueError
                types.append(t)
            body = q
            vals = []
            for t in types:
                n = _col_size(t)
                if body + n > end:
                    raise ValueError
                vals.append(_value(t, b[body:body + n], enc))
                body += n
            strs = [v for v in vals if isinstance(v, str)]
            if strs and all(v.isprintable() for v in strs) and any(len(v) > 1 for v in strs):
                return vals, body
        except (IndexError, ValueError):
            continue
    return None


class SQLiteRecovery:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data = open(path, "rb").read()
        h = self.data[:100]
        if h[:16] != b"SQLite format 3\0":
            raise ValueError("No es una base SQLite")
        ps = struct.unpack_from(">H", h, 16)[0]
        self.ps = 65536 if ps == 1 else ps
        self.reserved = h[20]
        self.enc = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}.get(struct.unpack_from(">I", h, 56)[0], "utf-8")
        self.npages = len(self.data) // self.ps
        self.tables: Dict[int, Tuple[str, int]] = {}
        self.page_owner: Dict[int, str] = {}
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            for name, root, sql in con.execute("SELECT name, rootpage, sql FROM sqlite_master WHERE type='table'"):
                if not root:
                    continue
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
                self.tables[root] = (name, cols)
            con.close()
        except sqlite3.Error:
            pass
        self._map_pages()

    def _page(self, n: int) -> bytes:
        return self.data[(n - 1) * self.ps: n * self.ps]

    def _map_pages(self) -> None:
        for root, (name, cols) in self.tables.items():
            stack, seen = [root], set()
            while stack:
                n = stack.pop()
                if n in seen or not 1 <= n <= self.npages:
                    continue
                seen.add(n)
                self.page_owner[n] = name
                pg = self._page(n)
                off = 100 if n == 1 else 0
                kind = pg[off]
                if kind == 0x05:
                    nc = struct.unpack_from(">H", pg, off + 3)[0]
                    stack.append(struct.unpack_from(">I", pg, off + 8)[0])
                    for i in range(nc):
                        cp = struct.unpack_from(">H", pg, off + 12 + 2 * i)[0]
                        stack.append(struct.unpack_from(">I", pg, cp)[0])

    def _freelist(self) -> List[int]:
        out = []
        trunk = struct.unpack_from(">I", self.data, 32)[0]
        seen = set()
        while trunk and trunk not in seen and trunk <= self.npages:
            seen.add(trunk)
            pg = self._page(trunk)
            nxt, cnt = struct.unpack_from(">II", pg, 0)
            out.append(trunk)
            out += [struct.unpack_from(">I", pg, 8 + 4 * i)[0] for i in range(min(cnt, (self.ps - 8) // 4))]
            trunk = nxt
        return out

    def _scan_region(self, pg: bytes, start: int, end: int, table: Optional[str], found: list, where: str,
                     partial: bool = False) -> None:
        cols = None
        if table:
            cols = next((len(c) for r, (n, c) in self.tables.items() if n == table), None)
        p = start
        if partial and cols:
            rec = decode_partial(pg, start, cols, self.enc, end)
            if rec:
                found.append((table, where, rec[0]))
                p = rec[1]
        while p < end - 3:
            try:
                rec = decode_record(pg, p, cols, self.enc, end)
            except (IndexError, ValueError):
                rec = None
            if rec:
                vals, q = rec
                found.append((table or "(desconocida)", where, vals))
                p = q
            else:
                p += 1

    def recover(self) -> List[Tuple[str, str, list]]:
        found: List[Tuple[str, str, list]] = []
        free = set(self._freelist())
        for n in range(1, self.npages + 1):
            pg = self._page(n)
            off = 100 if n == 1 else 0
            table = self.page_owner.get(n)
            if n in free:
                self._scan_region(pg, 8 if n in free else 0, self.ps - self.reserved, table, found,
                                  f"página libre {n}")
                continue
            kind = pg[off]
            if kind != 0x0D:  # solo hojas de tabla
                continue
            nc = struct.unpack_from(">H", pg, off + 3)[0]
            content = struct.unpack_from(">H", pg, off + 5)[0] or 65536
            # espacio no asignado entre el arreglo de punteros y el contenido
            self._scan_region(pg, off + 8 + 2 * nc, content, table, found, f"no asignado, página {n}")
            # bloques libres encadenados
            fb = struct.unpack_from(">H", pg, off + 1)[0]
            guard = 0
            while fb and fb < self.ps and guard < 1000:
                guard += 1
                nxt, size = struct.unpack_from(">HH", pg, fb)
                self._scan_region(pg, fb + 4, min(self.ps, fb + size), table, found, f"bloque libre, página {n}",
                                  partial=True)
                fb = nxt
        return found

    def export(self, dest_dir: str) -> List[str]:
        os.makedirs(dest_dir, exist_ok=True)
        by_table: Dict[str, list] = {}
        for t, where, vals in self.recover():
            by_table.setdefault(t, []).append((where, vals))
        files = []
        for t, rows in by_table.items():
            cols = next((c for r, (n, c) in self.tables.items() if n == t), [])
            p = os.path.join(dest_dir, f"{os.path.basename(self.path)}_{t}_borrados.csv".replace("/", "_"))
            with open(p, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["Ubicación"] + (cols or []))
                for where, vals in rows:
                    w.writerow([where] + [v.hex() if isinstance(v, bytes) else v for v in vals])
            files.append(p)
        return files
