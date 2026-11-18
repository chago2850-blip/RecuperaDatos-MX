"""Lector de imágenes forenses EWF / E01 (EnCase 1–7, FTK Imager, ewfacquire),
incluidos varios segmentos (.E01, .E02 … .EAA) y bloques comprimidos con zlib."""
from __future__ import annotations

import os
import re
import struct
import threading
import zlib
from typing import List, Tuple

from .device import BlockSource, FileSource

EVF = b"EVF\x09\x0d\x0a\xff\x00"


def segment_paths(first: str) -> List[str]:
    base, ext = os.path.splitext(first)
    letter = ext[1]  # E, L, S
    out = [first]
    n = 2
    while True:
        if n <= 99:
            ext2 = f"{letter}{n:02d}"
        else:  # E99 → EAA … EZZ, FAA …
            k = n - 100
            first_letter = chr(ord(letter.upper()) + k // 676)
            ext2 = first_letter + chr(65 + (k // 26) % 26) + chr(65 + k % 26)
        cand = [base + "." + ext2, base + "." + ext2.lower()]
        hit = next((c for c in cand if os.path.exists(c)), None)
        if not hit:
            return out
        out.append(hit)
        n += 1


class EWFSource(BlockSource):
    def __init__(self, path: str) -> None:
        self.path = path
        self.name = os.path.basename(path)
        self.segments = [FileSource(p, align=512) for p in segment_paths(path)]
        self.chunks: List[Tuple[int, int, int, bool]] = []  # (segmento, desplazamiento, longitud, comprimido)
        self.chunk_size = 32768
        self.sector_size = 512
        self.size = 0
        self.header_text = ""
        self._lock = threading.Lock()
        self._cache = (-1, b"")
        for si, seg in enumerate(self.segments):
            self._parse_segment(si, seg)
        if not self.size:
            self.size = len(self.chunks) * self.chunk_size

    def _parse_segment(self, si: int, seg: FileSource) -> None:
        if seg.read(0, 8) != EVF:
            raise ValueError(f"{seg.path} no es un segmento EWF")
        off = 13
        sectors_start = sectors_end = None
        guard = 0
        while off < seg.size and guard < 100000:
            guard += 1
            d = seg.read(off, 76)
            if len(d) < 76:
                break
            typ = d[:16].split(b"\0")[0].decode("ascii", "replace")
            nxt, size = struct.unpack_from("<QQ", d, 16)
            body = off + 76
            if typ in ("volume", "disk") and not self.size:
                v = seg.read(body, 94)
                nchunks, spc, bps = struct.unpack_from("<III", v, 4)
                nsect = struct.unpack_from("<Q", v, 16)[0]
                self.sector_size = bps or 512
                self.chunk_size = spc * self.sector_size or 32768
                self.size = nsect * self.sector_size
            elif typ in ("header", "header2") and not self.header_text:
                try:
                    raw = zlib.decompress(seg.read(body, size - 76))
                    self.header_text = raw.decode("utf-16" if typ == "header2" else "latin-1", "replace")
                except zlib.error:
                    pass
            elif typ == "sectors":
                sectors_start, sectors_end = body, off + size
            elif typ == "table":
                t = seg.read(body, 24)
                n = struct.unpack_from("<I", t, 0)[0]
                base = struct.unpack_from("<Q", t, 8)[0]
                raw = seg.read(body + 24, n * 4)
                ents = struct.unpack(f"<{n}I", raw[:n * 4])
                # EnCase 1–5: los bloques siguen a la tabla dentro de la misma sección
                end_all = sectors_end if sectors_end else off + size
                offs = [(base + (e & 0x7FFFFFFF), bool(e & 0x80000000)) for e in ents]
                for i, (o, comp) in enumerate(offs):
                    e = offs[i + 1][0] if i + 1 < len(offs) else end_all
                    self.chunks.append((si, o, max(0, e - o), comp))
                sectors_start = sectors_end = None
            elif typ in ("done", "next"):
                break
            if nxt == off or nxt == 0:
                break
            off = nxt

    def _chunk(self, idx: int) -> bytes:
        co, cd = self._cache
        if co == idx:
            return cd
        si, off, ln, comp = self.chunks[idx]
        raw = self.segments[si].read(off, ln)
        if comp:
            try:
                data = zlib.decompressobj().decompress(raw)
            except zlib.error:
                data = b"\0" * self.chunk_size
        else:
            data = raw[:self.chunk_size]
        data = data[:self.chunk_size].ljust(self.chunk_size, b"\0")
        self._cache = (idx, data)
        return data

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        out = bytearray()
        with self._lock:
            while length > 0:
                idx, inner = divmod(offset, self.chunk_size)
                if idx >= len(self.chunks):
                    out += b"\0" * length
                    break
                n = min(length, self.chunk_size - inner)
                out += self._chunk(idx)[inner:inner + n]
                offset += n
                length -= n
        return bytes(out)

    def describe(self) -> dict:
        return {"tipo": "archivo", "ruta": self.path, "tamano": self.size}

    def close(self) -> None:
        for s in self.segments:
            s.close()


def is_ewf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(8) == EVF
    except OSError:
        return False
