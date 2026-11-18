"""Recuperación por firmas (escaneo profundo / RAW).

Cada firma tiene un patrón de cabecera y un "medidor" que valida la estructura
interna del archivo y calcula su tamaño real.  Los formatos sin tamaño
deducible se cierran en la siguiente cabecera encontrada o en su tamaño máximo.
Se pueden añadir firmas personalizadas (cabecera/pie en hexadecimal) desde la
interfaz o con un archivo JSON.
"""
from __future__ import annotations

import bz2
import json
import lzma
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .device import BlockSource
from .model import FileEntry, category_of

MB = 1 << 20


class Win:
    """Lector con ventana de caché para los medidores."""

    def __init__(self, src: BlockSource, start: int, limit: int) -> None:
        self.src = src
        self.start = start
        self.limit = min(limit, max(0, src.size - start))
        self.wpos = -1
        self.wdata = b""

    def get(self, off: int, n: int) -> bytes:
        if off < 0 or off >= self.limit:
            return b""
        n = min(n, self.limit - off)
        if self.wpos <= off and off + n <= self.wpos + len(self.wdata):
            s = off - self.wpos
            return self.wdata[s:s + n]
        if n > MB:
            return self.src.read(self.start + off, n)
        self.wpos = off
        self.wdata = self.src.read(self.start + off, max(n, MB))
        return self.wdata[:n]

    def find(self, pat: bytes, off: int, maxlen: int) -> int:
        """Busca ``pat`` desde ``off``; devuelve desplazamiento relativo o -1."""
        end = min(self.limit, off + maxlen)
        pos = off
        CH = 4 * MB
        while pos < end:
            blk = self.get(pos, min(CH + len(pat), end - pos))
            if not blk:
                return -1
            i = blk.find(pat)
            if i != -1:
                return pos + i
            pos += CH
        return -1

    def regex(self, rx, off: int, maxlen: int):
        end = min(self.limit, off + maxlen)
        pos = off
        CH = 4 * MB
        while pos < end:
            blk = self.get(pos, min(CH + 16, end - pos))
            if not blk:
                return None
            m = rx.search(blk)
            if m:
                return pos + m.start(), m
            pos += CH
        return None


Result = Optional[Tuple[int, Optional[str]]]


def be16(b, o): return struct.unpack_from(">H", b, o)[0]
def be32(b, o): return struct.unpack_from(">I", b, o)[0]
def be64(b, o): return struct.unpack_from(">Q", b, o)[0]
def le16(b, o): return struct.unpack_from("<H", b, o)[0]
def le32(b, o): return struct.unpack_from("<I", b, o)[0]
def le64(b, o): return struct.unpack_from("<Q", b, o)[0]


# --------------------------------------------------------------------------- #
# Medidores
# --------------------------------------------------------------------------- #
_JPEG_MARK = re.compile(rb"\xFF[^\x00\xD0-\xD7\xFF]")


def size_jpeg(w: Win, maxs: int) -> Result:
    if w.get(0, 3) != b"\xFF\xD8\xFF":
        return None
    p = 2
    seen_sos = False
    guard = 0
    while p < maxs and guard < 100000:
        guard += 1
        h = w.get(p, 4)
        if len(h) < 2 or h[0] != 0xFF:
            if not seen_sos:
                return None
            r = w.regex(_JPEG_MARK, p, maxs - p)
            if not r:
                return None
            p = r[0]
            continue
        m = h[1]
        if m == 0xFF:
            p += 1
            continue
        if m == 0xD9:
            return (p + 2, None) if seen_sos else None
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            p += 2
            continue
        if len(h) < 4:
            return None
        ln = be16(h, 2)
        if ln < 2:
            return None
        if m == 0xDA:
            seen_sos = True
            p += 2 + ln
            r = w.regex(_JPEG_MARK, p, maxs - p)
            if not r:
                return None
            p = r[0]
            continue
        if not (0xC0 <= m <= 0xFE):
            return None
        p += 2 + ln
    return None


def size_png(w: Win, maxs: int) -> Result:
    p = 8
    first = True
    while p < maxs:
        h = w.get(p, 8)
        if len(h) < 8:
            return None
        ln, typ = be32(h, 0), h[4:8]
        if not typ.isalpha() or ln > maxs:
            return None
        if first and typ != b"IHDR":
            return None
        first = False
        p += 12 + ln
        if typ == b"IEND":
            return p, None
    return None


def size_gif(w: Win, maxs: int) -> Result:
    h = w.get(0, 13)
    if len(h) < 13:
        return None
    p = 13
    if h[10] & 0x80:
        p += 3 * (2 << (h[10] & 7))
    guard = 0
    while p < maxs and guard < 200000:
        guard += 1
        b = w.get(p, 1)
        if not b:
            return None
        if b == b"\x3B":
            return p + 1, None
        if b == b"\x21":
            p += 2
        elif b == b"\x2C":
            d = w.get(p, 10)
            if len(d) < 10:
                return None
            p += 10
            if d[9] & 0x80:
                p += 3 * (2 << (d[9] & 7))
            p += 1
        else:
            return None
        while True:
            s = w.get(p, 1)
            if not s:
                return None
            p += 1 + s[0]
            if s[0] == 0:
                break
    return None


def size_bmp(w: Win, maxs: int) -> Result:
    h = w.get(0, 30)
    if len(h) < 30:
        return None
    size, res, off, dib = le32(h, 2), le32(h, 6), le32(h, 10), le32(h, 14)
    if res != 0 or dib not in (12, 40, 52, 56, 64, 108, 124) or not (26 <= off < size <= maxs):
        return None
    return size, None


TIFF_MAKES = {b"NIKON": "nef", b"SONY": "arw", b"PENTAX": "pef", b"SAMSUNG": "srw", b"OLYMPUS": "orf",
              b"Panasonic": "rw2", b"FUJIFILM": "raf", b"Hasselblad": "3fr", b"Leaf": "mos",
              b"KODAK": "dcr", b"Mamiya": "mef"}


def _tiff_walk(w: Win, be: bool, ifd: int, maxs: int, state: dict, depth: int = 0) -> None:
    U16 = be16 if be else le16
    U32 = be32 if be else le32
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
    guard = 0
    while ifd and ifd < maxs and depth < 8 and guard < 64 and ifd not in state["seen"]:
        state["seen"].add(ifd)
        guard += 1
        n = U16(w.get(ifd, 2).ljust(2, b"\0"), 0)
        if n == 0 or n > 1000:
            return
        ents = w.get(ifd + 2, n * 12 + 4)
        if len(ents) < n * 12 + 4:
            return
        state["end"] = max(state["end"], ifd + 2 + n * 12 + 4)
        offs: Dict[int, List[int]] = {}
        for i in range(n):
            e = ents[i * 12:(i + 1) * 12]
            tag, typ, cnt = U16(e, 0), U16(e, 2), U32(e, 4)
            sz = sizes.get(typ, 1) * cnt
            if sz > 4:
                vo = U32(e, 8)
                if vo + sz <= maxs:
                    state["end"] = max(state["end"], vo + sz)
                vals_raw = w.get(vo, min(sz, 4 * 4096)) if tag in (273, 279, 324, 325, 330) else b""
            else:
                vo = None
                vals_raw = e[8:12]
            if tag in (273, 279, 324, 325, 330, 513, 514, 34665, 50706, 271):
                vals = []
                if typ in (3, 4, 13) and tag != 271:
                    step = 2 if typ == 3 else 4
                    fn = U16 if typ == 3 else U32
                    for k in range(min(cnt, len(vals_raw) // step)):
                        vals.append(fn(vals_raw, k * step))
                offs[tag] = vals
                if tag == 271:
                    mk = w.get(vo, cnt) if vo is not None else e[8:8 + cnt]
                    state["make"] = mk.split(b"\0")[0].strip()
                if tag == 50706:
                    state["dng"] = True
        for a, b in ((273, 279), (324, 325), (513, 514)):
            for o, l in zip(offs.get(a, []), offs.get(b, [])):
                if o + l <= maxs:
                    state["end"] = max(state["end"], o + l)
        for sub in offs.get(330, []) + offs.get(34665, []):
            _tiff_walk(w, be, sub, maxs, state, depth + 1)
        ifd = U32(ents, n * 12)


def size_tiff(w: Win, maxs: int) -> Result:
    h = w.get(0, 16)
    if len(h) < 8:
        return None
    be = h[:2] == b"MM"
    U32 = be32 if be else le32
    first = U32(h, 4)
    if first < 8 or first > maxs:
        return None
    st = {"end": 8, "seen": set(), "make": b"", "dng": False}
    _tiff_walk(w, be, first, maxs, st)
    if st["end"] <= 16:
        return None
    ext = "tif"
    if h[8:10] == b"CR":
        ext = "cr2"
    elif h[:4] in (b"IIRO", b"IIRS"):
        ext = "orf"
    elif h[:4] == b"IIU\0":
        ext = "rw2"
    elif st["dng"]:
        ext = "dng"
    else:
        for k, v in TIFF_MAKES.items():
            if st["make"].upper().startswith(k.upper()):
                ext = v
                break
    return st["end"], ext


def size_pdf(w: Win, maxs: int) -> Result:
    p = w.find(b"%%EOF", 5, maxs)
    if p < 0:
        return None
    end = p + 5
    while True:
        nxt = w.find(b"%%EOF", end, min(4 * MB, maxs - end))
        if nxt < 0:
            break
        newpdf = w.find(b"%PDF-", end, nxt - end)
        if newpdf >= 0:
            break
        end = nxt + 5
    tail = w.get(end, 2)
    if tail[:1] in (b"\r", b"\n"):
        end += 1
        if tail == b"\r\n":
            end += 1
    head = w.get(0, 1024)
    if b"Illustrator" in head or b"/AIPrivateData" in w.get(0, 65536):
        return end, "ai"
    return end, None


def _zip_kind(names: List[str], w: Win) -> str:
    s = set(names)
    joined = "\n".join(names[:2000])
    if "[Content_Types].xml" in s:
        if "word/document.xml" in s or "\nword/" in "\n" + joined:
            return "docx"
        if "\nxl/" in "\n" + joined:
            return "xlsx"
        if "\nppt/" in "\n" + joined:
            return "pptx"
        if "\nvisio/" in "\n" + joined:
            return "vsdx"
        return "docx"
    if "mimetype" in s:
        mt = w.get(38, 80)
        for k, v in ((b"opendocument.text", "odt"), (b"opendocument.spreadsheet", "ods"),
                     (b"opendocument.presentation", "odp"), (b"opendocument.graphics", "odg"),
                     (b"epub+zip", "epub")):
            if k in mt:
                return v
    if "AndroidManifest.xml" in s:
        return "apk"
    if "META-INF/MANIFEST.MF" in s:
        return "jar"
    if "install.rdf" in s or "manifest.json" in s and any(n.endswith(".js") for n in names):
        return "xpi" if "install.rdf" in s else "zip"
    if "document.json" in s and "meta.json" in s:
        return "sketch"
    if "Index/Document.iwa" in s:
        return "pages"
    return "zip"


def size_zip(w: Win, maxs: int) -> Result:
    if w.get(0, 4) != b"PK\x03\x04":
        return None
    pos = 0
    while True:
        e = w.find(b"PK\x05\x06", pos + 4, maxs - pos)
        if e < 0:
            return None
        rec = w.get(e, 22)
        if len(rec) < 22:
            return None
        cd_size, cd_off, clen = le32(rec, 12), le32(rec, 16), le16(rec, 20)
        if cd_off == 0xFFFFFFFF or cd_off + cd_size == e:
            cd = w.get(cd_off, min(cd_size, 4 * MB)) if cd_off != 0xFFFFFFFF else b""
            names = []
            q = 0
            while q + 46 <= len(cd) and cd[q:q + 4] == b"PK\x01\x02":
                nl, xl, cl = le16(cd, q + 28), le16(cd, q + 30), le16(cd, q + 32)
                names.append(cd[q + 46:q + 46 + nl].decode("utf-8", "replace"))
                q += 46 + nl + xl + cl
            return e + 22 + clen, _zip_kind(names, w)
        pos = e


def size_rar(w: Win, maxs: int) -> Result:
    h = w.get(0, 8)
    if h[:7] == b"Rar!\x1a\x07\x00":
        p = 7
        while p < maxs:
            b = w.get(p, 11)
            if len(b) < 7:
                return None
            typ, flags, hs = b[2], le16(b, 3), le16(b, 5)
            if hs < 7:
                return None
            add = le32(b, 7) if flags & 0x8000 and len(b) >= 11 else 0
            p += hs + add
            if typ == 0x7B:
                return p, None
            if typ < 0x72 or typ > 0x7B:
                return None
        return None
    if h[:8] == b"Rar!\x1a\x07\x01\x00":
        p = 8

        def vint(o):
            v, s = 0, 0
            for k in range(10):
                c = w.get(o + k, 1)
                if not c:
                    return None, o
                v |= (c[0] & 0x7F) << s
                s += 7
                if not c[0] & 0x80:
                    return v, o + k + 1
            return None, o
        while p < maxs:
            hsz, q = vint(p + 4)
            if hsz is None or hsz == 0:
                return None
            start = q
            typ, q = vint(q)
            flags, q = vint(q)
            if typ is None or flags is None:
                return None
            data = 0
            if flags & 1:
                _, q = vint(q)
            if flags & 2:
                data, q = vint(q)
            p = start + hsz + (data or 0)
            if typ == 5:
                return p, None
            if typ > 5:
                return None
        return None
    return None


def size_7z(w: Win, maxs: int) -> Result:
    h = w.get(0, 32)
    if len(h) < 32:
        return None
    off, sz = le64(h, 12), le64(h, 20)
    total = 32 + off + sz
    if sz == 0 or total > maxs:
        return None
    return total, None


def _stream(w: Win, maxs: int, dec) -> Result:
    p = 0
    CH = MB
    while p < maxs:
        blk = w.get(p, CH)
        if not blk:
            return None
        try:
            dec.decompress(blk, 1 << 16) if hasattr(dec, "unconsumed_tail") else dec.decompress(blk)
        except Exception:
            return None
        if getattr(dec, "eof", False):
            return p + len(blk) - len(dec.unused_data), None
        if hasattr(dec, "unconsumed_tail") and dec.unconsumed_tail:
            # zlib: alimentar el resto
            tail = dec.unconsumed_tail
            while tail and not dec.eof:
                dec.decompress(tail, 1 << 16)
                tail = dec.unconsumed_tail
            if dec.eof:
                return p + len(blk) - len(dec.unused_data), None
        p += len(blk)
    return None


def size_gz(w: Win, maxs: int) -> Result:
    r = _stream(w, min(maxs, 512 * MB), zlib.decompressobj(31))
    if r and w.get(0, 4096).find(b"ustar", 0) >= 0:
        return r[0], "tgz"
    return r


def size_bz2(w: Win, maxs: int) -> Result:
    return _stream(w, min(maxs, 256 * MB), bz2.BZ2Decompressor())


def size_xz(w: Win, maxs: int) -> Result:
    return _stream(w, min(maxs, 256 * MB), lzma.LZMADecompressor(format=lzma.FORMAT_XZ))


_MP3_BR = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
           2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
_MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def size_mp3(w: Win, maxs: int) -> Result:
    h = w.get(0, 10)
    p = 0
    if h[:3] == b"ID3":
        p = 10 + ((h[6] & 0x7F) << 21 | (h[7] & 0x7F) << 14 | (h[8] & 0x7F) << 7 | (h[9] & 0x7F))
        if h[5] & 0x10:
            p += 10
    frames = 0
    while p < maxs:
        f = w.get(p, 4)
        if len(f) < 4 or f[0] != 0xFF or (f[1] & 0xE0) != 0xE0:
            break
        ver = (f[1] >> 3) & 3
        layer = (f[1] >> 1) & 3
        bri = f[2] >> 4
        sri = (f[2] >> 2) & 3
        pad = (f[2] >> 1) & 1
        if ver == 1 or layer != 1 or bri in (0, 15) or sri == 3:
            break
        br = _MP3_BR[1 if ver == 3 else 2][bri] * 1000
        sr = _MP3_SR[ver][sri]
        ln = (144 * br // sr + pad) if ver == 3 else (72 * br // sr + pad)
        if ln < 24:
            break
        p += ln
        frames += 1
    if frames < 4:
        return None
    if w.get(p, 3) == b"TAG":
        p += 128
    return p, None


def size_riff(w: Win, maxs: int) -> Result:
    h = w.get(0, 16)
    if len(h) < 12:
        return None
    sz = le32(h, 4) + 8
    kind = h[8:12]
    ext = {b"WAVE": "wav", b"AVI ": "avi", b"WEBP": "webp", b"RMID": "mid", b"ACON": "ani",
           b"CDXA": "dat", b"AMV ": "amv"}.get(kind)
    if kind.startswith(b"CDR"):
        ext = "cdr"
    if not ext or sz < 16 or sz > maxs:
        return None
    if ext == "avi":
        # algunos AVI >1 GB usan bloques RIFF AVIX encadenados
        p = sz
        while p < maxs:
            nh = w.get(p, 12)
            if len(nh) == 12 and nh[:4] == b"RIFF" and nh[8:12] == b"AVIX":
                p += le32(nh, 4) + 8
            else:
                break
        sz = p
    return sz + (sz & 1), ext


def size_aiff(w: Win, maxs: int) -> Result:
    h = w.get(0, 12)
    if h[8:12] not in (b"AIFF", b"AIFC"):
        if h[8:12] == b"DJVU" or h[8:12] == b"DJVM":
            return None
        return None
    return be32(h, 4) + 8, "aiff"


def size_djvu(w: Win, maxs: int) -> Result:
    h = w.get(0, 16)
    if h[:8] != b"AT&TFORM" or h[12:15] != b"DJV":
        return None
    return be32(h, 8) + 12, None


_ISO_BRANDS = {b"qt  ": "mov", b"heic": "heic", b"heix": "heic", b"mif1": "heic", b"msf1": "heic",
               b"heim": "heic", b"hevc": "heic", b"avif": "avif", b"avis": "avif", b"M4A ": "m4a",
               b"M4B ": "m4a", b"M4P ": "m4a", b"M4V ": "m4v", b"M4VH": "m4v", b"crx ": "cr3",
               b"3gp4": "3gp", b"3gp5": "3gp", b"3gp6": "3gp", b"3g2a": "3gp", b"f4v ": "mp4",
               b"jp2 ": "jp2", b"XAVC": "mp4", b"insv": "insv"}
_ATOMS = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"pdin", b"moof",
          b"mfra", b"sidx", b"styp", b"emsg", b"prft", b"pnot", b"PICT", b"junk", b"beam", b"jp2h",
          b"jp2c", b"jP  ", b"colr", b"rsrc", b"ssix", b"idat", b"iinf", b"iloc"}


def size_isobmff(w: Win, maxs: int) -> Result:
    h = w.get(0, 12)
    if h[4:8] != b"ftyp":
        return None
    brand = h[8:12]
    ext = _ISO_BRANDS.get(brand, "mp4")
    p = 0
    has_data = False
    while p < maxs:
        a = w.get(p, 16)
        if len(a) < 8:
            break
        sz, typ = be32(a, 0), a[4:8]
        if typ not in _ATOMS:
            break
        if sz == 1:
            if len(a) < 16:
                break
            sz = be64(a, 8)
        elif sz == 0:
            sz = max(8, w.limit - p)
        if sz < 8:
            break
        if typ in (b"mdat", b"moov", b"moof", b"meta", b"jp2c"):
            has_data = True
        p += sz
    if not has_data or p <= 12:
        return None
    return min(p, maxs), ext


def size_jp2(w: Win, maxs: int) -> Result:
    p = 0
    while p < maxs:
        a = w.get(p, 16)
        if len(a) < 8:
            break
        sz, typ = be32(a, 0), a[4:8]
        if typ not in _ATOMS and typ not in (b"ftyp", b"rreq", b"res "):
            break
        if sz == 1:
            sz = be64(a, 8)
        if sz == 0:
            return None
        if sz < 8:
            break
        p += sz
        if typ == b"jp2c":
            return p, "jp2"
    return None


def _ebml_vint(b: bytes, o: int, keep_marker=False):
    if o >= len(b):
        return None, 0
    first = b[o]
    for ln in range(1, 9):
        if first & (0x80 >> (ln - 1)):
            break
    else:
        return None, 0
    v = first if keep_marker else first & (0xFF >> ln)
    for k in range(1, ln):
        if o + k >= len(b):
            return None, 0
        v = (v << 8) | b[o + k]
    return v, ln


def size_ebml(w: Win, maxs: int) -> Result:
    h = w.get(0, 256)
    sz, ln = _ebml_vint(h, 4)
    if sz is None:
        return None
    hdr = h[4 + ln:4 + ln + sz]
    ext = "webm" if b"webm" in hdr else "mkv"
    p = 4 + ln + sz
    seg = w.get(p, 16)
    if seg[:4] != b"\x18\x53\x80\x67":
        return None
    ssz, sl = _ebml_vint(seg, 4)
    if ssz is None:
        return None
    if ssz == (1 << (7 * sl)) - 1:  # tamaño desconocido (transmisión en vivo)
        return None
    return p + 4 + sl + ssz, ext


def size_ogg(w: Win, maxs: int) -> Result:
    p = 0
    ext = "ogg"
    pages = 0
    while p < maxs:
        h = w.get(p, 27 + 255)
        if len(h) < 27 or h[:4] != b"OggS":
            break
        nseg = h[26]
        seg = h[27:27 + nseg]
        if pages == 0:
            body = w.get(p + 27 + nseg, 16)
            if body[:8] == b"OpusHead":
                ext = "opus"
            elif body[1:7] == b"theora":
                ext = "ogv"
        p += 27 + nseg + sum(seg)
        pages += 1
        if h[5] & 4:
            return p, ext
    return (p, ext) if pages > 2 else None


def size_flv(w: Win, maxs: int) -> Result:
    h = w.get(0, 9)
    p = be32(h, 5) + 4
    tags = 0
    while p < maxs:
        t = w.get(p, 11)
        if len(t) < 11 or t[0] not in (8, 9, 18):
            break
        ds = int.from_bytes(t[1:4], "big")
        p += 11 + ds + 4
        tags += 1
    return (p, None) if tags else None


def size_asf(w: Win, maxs: int) -> Result:
    p = 0
    objs = 0
    ext = "wmv"
    head = w.get(0, 8192)
    if b"\x40\x9E\x69\xF8\x4D\x5B\xCF\x11\xA8\xFD\x00\x80\x5F\x5C\x44\x2B" in head and \
            b"\xC0\xEF\x19\xBC\x4D\x5B\xCF\x11\xA8\xFD\x00\x80\x5F\x5C\x44\x2B" not in head:
        ext = "wma"
    while p < maxs:
        o = w.get(p, 24)
        if len(o) < 24:
            break
        sz = le64(o, 16)
        if sz < 24 or (objs and o[:16] not in (
                bytes.fromhex("3626B2758E66CF11A6D900AA0062CE6C"),
                bytes.fromhex("90080033B1E5CF1189F400A0C90349CB"),
                bytes.fromhex("D329E2D6DA35D111903400A0C90349BE"),
                bytes.fromhex("F803B1FEAD12644C840F2A1D2F7AD48C"),
                bytes.fromhex("D029E2D6DA35D111903400A0C90349BE"))):
            break
        p += sz
        objs += 1
    return (p, ext) if objs >= 2 else None


def size_sqlite(w: Win, maxs: int) -> Result:
    h = w.get(0, 100)
    ps = be16(h, 16)
    ps = 65536 if ps == 1 else ps
    pages = be32(h, 28)
    if ps < 512 or ps & (ps - 1) or not pages:
        return None
    return ps * pages, None


def size_ole(w: Win, maxs: int) -> Result:
    h = w.get(0, 512)
    shift = le16(h, 30)
    if shift not in (9, 12):
        return None
    ss = 1 << shift
    nfat = le32(h, 44)
    dir_start = le32(h, 48)
    difat = [le32(h, 76 + i * 4) for i in range(109)]
    fat_secs = [s for s in difat[:nfat] if s < 0xFFFFFFFA]
    maxsec = 0
    per = ss // 4
    for i, fs in enumerate(fat_secs):
        data = w.get((fs + 1) * ss, ss)
        if len(data) < ss:
            return None
        vals = struct.unpack(f"<{per}I", data)
        for j, v in enumerate(vals):
            if v != 0xFFFFFFFF:
                maxsec = max(maxsec, i * per + j)
        maxsec = max(maxsec, fs)
    total = (maxsec + 2) * ss
    if total > maxs or total < 1024:
        return None
    names = []
    d = w.get((dir_start + 1) * ss, ss)
    for k in range(0, len(d), 128):
        nl = le16(d, k + 64)
        if 2 <= nl <= 64:
            names.append(d[k:k + nl - 2].decode("utf-16-le", "ignore"))
    s = set(names)
    ext = "ole"
    if "WordDocument" in s:
        ext = "doc"
    elif "Workbook" in s or "Book" in s:
        ext = "xls"
    elif "PowerPoint Document" in s:
        ext = "ppt"
    elif any(n.startswith("__substg1.0_") for n in names) or "__properties_version1.0" in s:
        ext = "msg"
    elif "VisioDocument" in s:
        ext = "vsd"
    elif "Contents" in s and "Quill" in s:
        ext = "pub"
    elif any("䡀" in n for n in names):
        ext = "msi"
    elif "Thumbs" in "".join(names) or "Catalog" in s:
        ext = "db"
    return total, ext


def size_pe(w: Win, maxs: int) -> Result:
    h = w.get(0, 64)
    lf = le32(h, 0x3C)
    if lf < 0x40 or lf > 4096:
        return None
    pe = w.get(lf, 24)
    if pe[:4] != b"PE\0\0":
        return None
    nsec = le16(pe, 6)
    optsz = le16(pe, 20)
    chars = le16(pe, 22)
    opt = w.get(lf + 24, optsz)
    if len(opt) < 2 or nsec > 96:
        return None
    end = lf + 24 + optsz + nsec * 40
    secs = w.get(lf + 24 + optsz, nsec * 40)
    for i in range(nsec):
        s = secs[i * 40:(i + 1) * 40]
        if len(s) < 40:
            return None
        end = max(end, le32(s, 20) + le32(s, 16))
    magic = le16(opt, 0)
    dd = 96 if magic == 0x10B else 112
    if len(opt) >= dd + 40:
        coff, csz = le32(opt, dd + 32), le32(opt, dd + 36)  # tabla de certificados
        if coff and csz and coff + csz < maxs:
            end = max(end, coff + csz)
    if end > maxs:
        return None
    ext = "dll" if chars & 0x2000 else ("sys" if chars & 0x1000 else "exe")
    return end, ext


def size_elf(w: Win, maxs: int) -> Result:
    h = w.get(0, 64)
    if len(h) < 52:
        return None
    is64 = h[4] == 2
    le = h[5] == 1
    U16 = le16 if le else be16
    U32 = le32 if le else be32
    U64 = le64 if le else be64
    if is64:
        shoff, shentsize, shnum = U64(h, 0x28), U16(h, 0x3A), U16(h, 0x3C)
        phoff, phentsize, phnum = U64(h, 0x20), U16(h, 0x36), U16(h, 0x38)
    else:
        shoff, shentsize, shnum = U32(h, 0x20), U16(h, 0x2E), U16(h, 0x30)
        phoff, phentsize, phnum = U32(h, 0x1C), U16(h, 0x2A), U16(h, 0x2C)
    end = max(shoff + shentsize * shnum, phoff + phentsize * phnum)
    if end > maxs or end < 64:
        return None
    ext = "so" if U16(h, 0x10) == 3 else "elf"
    return end, ext


def size_psd(w: Win, maxs: int) -> Result:
    h = w.get(0, 26)
    ver = be16(h, 4)
    if ver not in (1, 2):
        return None
    ch, hgt, wid, depth = be16(h, 12), be32(h, 14), be32(h, 18), be16(h, 22)
    p = 26
    p += 4 + be32(w.get(p, 4), 0)
    p += 4 + be32(w.get(p, 4), 0)
    lm = w.get(p, 8)
    p += (8 + be64(lm, 0)) if ver == 2 else (4 + be32(lm, 0))
    comp = be16(w.get(p, 2), 0)
    p += 2
    if comp == 0:
        p += ch * hgt * wid * max(1, depth // 8)
    elif comp == 1:
        rows = ch * hgt
        step = 4 if ver == 2 else 2
        tbl = w.get(p, rows * step)
        if len(tbl) < rows * step:
            return None
        fmt = f">{rows}{'I' if ver == 2 else 'H'}"
        p += rows * step + sum(struct.unpack(fmt, tbl))
    else:
        return None
    if p > maxs:
        return None
    return p, "psb" if ver == 2 else None


def size_rtf(w: Win, maxs: int) -> Result:
    depth = 0
    p = 0
    CH = MB
    while p < maxs:
        blk = w.get(p, CH)
        if not blk:
            return None
        i = 0
        L = len(blk)
        while i < L:
            c = blk[i]
            if c == 0x5C:
                i += 2
                continue
            if c == 0x7B:
                depth += 1
            elif c == 0x7D:
                depth -= 1
                if depth == 0:
                    return p + i + 1, None
            elif c == 0:
                return None
            i += 1
        p += CH
    return None


def size_midi(w: Win, maxs: int) -> Result:
    p = 14
    ntr = be16(w.get(10, 2), 0)
    for _ in range(ntr):
        c = w.get(p, 8)
        if c[:4] != b"MTrk":
            break
        p += 8 + be32(c, 4)
    return (p, None) if p > 14 else None


def size_sfnt(w: Win, maxs: int) -> Result:
    h = w.get(0, 12)
    n = be16(h, 4)
    if not 4 <= n <= 64:
        return None
    tabs = w.get(12, n * 16)
    end = 12 + n * 16
    tags = set()
    for i in range(n):
        t = tabs[i * 16:(i + 1) * 16]
        if len(t) < 16 or not all(32 <= c < 127 for c in t[:4]):
            return None
        tags.add(t[:4])
        end = max(end, be32(t, 8) + be32(t, 12))
    if b"head" not in tags or end > maxs:
        return None
    return end + ((-end) % 4), ("otf" if h[:4] == b"OTTO" else "ttf")


def size_woff(w: Win, maxs: int) -> Result:
    h = w.get(0, 12)
    ln = be32(h, 8)
    if ln < 44 or ln > maxs:
        return None
    return ln, "woff2" if h[:4] == b"wOF2" else "woff"


def size_pst(w: Win, maxs: int) -> Result:
    h = w.get(0, 0x200)
    if h[8:10] not in (b"SM", b"SO", b"SA"):
        return None
    ver = le16(h, 10)
    size = le64(h, 0xB8) if ver >= 23 else le32(h, 0xA8)
    if size < 512 or size > maxs:
        return None
    return size, "ost" if h[8:10] == b"SO" else None


def size_ico(w: Win, maxs: int) -> Result:
    h = w.get(0, 6 + 16 * 64)
    n = le16(h, 4)
    if not 1 <= n <= 64:
        return None
    end = 6 + 16 * n
    for i in range(n):
        e = h[6 + i * 16:22 + i * 16]
        if len(e) < 16 or e[3] != 0 or le16(e, 4) > 1 or le16(e, 6) not in (0, 1, 4, 8, 16, 24, 32):
            return None
        sz, off = le32(e, 8), le32(e, 12)
        if off < 6 + 16 * n or sz == 0:
            return None
        end = max(end, off + sz)
    if end > min(maxs, 4 * MB):
        return None
    first = w.get(6 + 16 * n, 8)
    if first[:4] not in (b"\x28\0\0\0", b"\x89PNG"):
        return None
    return end, None


def size_iso(w: Win, maxs: int) -> Result:
    pvd = w.get(0x8000, 2048)
    if pvd[1:6] != b"CD001" or pvd[0] != 1:
        return None
    blocks = le32(pvd, 80)
    bsz = le16(pvd, 128)
    total = blocks * bsz
    if total < 0x8800 or total > maxs:
        return None
    return total, None


def size_cab(w: Win, maxs: int) -> Result:
    h = w.get(0, 16)
    sz = le32(h, 8)
    if le32(h, 4) != 0 or sz < 36 or sz > maxs:
        return None
    return sz, None


def size_tar(w: Win, maxs: int) -> Result:
    p = 0
    n = 0
    while p < maxs:
        hd = w.get(p, 512)
        if len(hd) < 512:
            break
        if hd == b"\0" * 512:
            return p + 1024, "tar"
        if hd[257:262] != b"ustar":
            break
        try:
            size = int(hd[124:136].split(b"\0")[0].strip() or b"0", 8)
        except ValueError:
            break
        p += 512 + ((size + 511) // 512) * 512
        n += 1
    return (p, "tar") if n else None


def size_ar(w: Win, maxs: int) -> Result:
    p = 8
    ext = "deb" if w.get(8, 13) == b"debian-binary" else "a"
    n = 0
    while p < maxs:
        hd = w.get(p, 60)
        if len(hd) < 60 or hd[58:60] != b"`\n":
            break
        try:
            size = int(hd[48:58].strip())
        except ValueError:
            break
        p += 60 + size + (size & 1)
        n += 1
    return (p, ext) if n else None


def size_blend(w: Win, maxs: int) -> Result:
    h = w.get(0, 12)
    ptr = 8 if h[7:8] == b"-" else 4
    le = h[8:9] == b"v"
    U32 = le32 if le else be32
    p = 12
    bh = 16 + ptr
    while p < maxs:
        b = w.get(p, bh)
        if len(b) < bh:
            return None
        code = b[:4]
        size = U32(b, 4)
        p += bh + size
        if code == b"ENDB":
            return p, None
        if not all(32 <= c < 127 or c == 0 for c in code):
            return None
    return None


def size_swf(w: Win, maxs: int) -> Result:
    h = w.get(0, 8)
    ln = le32(h, 4)
    if not 21 <= ln <= maxs or h[3] > 50:
        return None
    return ln, None


def size_emf(w: Win, maxs: int) -> Result:
    h = w.get(0, 88)
    if le32(h, 0) != 1 or h[40:44] != b" EMF":
        return None
    n = le32(h, 48)
    if n < 88 or n > maxs:
        return None
    return n, None


def size_evtx(w: Win, maxs: int) -> Result:
    h = w.get(0, 128)
    chunks = le16(h, 0x2A)
    if not chunks:
        return None
    return 4096 + chunks * 65536, None


def size_regf(w: Win, maxs: int) -> Result:
    h = w.get(0, 64)
    n = le32(h, 0x28)
    if not n or n % 4096 or n + 4096 > maxs:
        return None
    return n + 4096, "reg"


def size_wav64(w: Win, maxs: int) -> Result:
    return None


def size_kdbx(w: Win, maxs: int) -> Result:
    return None


# --------------------------------------------------------------------------- #
# Catálogo de firmas
# --------------------------------------------------------------------------- #
@dataclass
class Signature:
    ext: str
    desc: str
    pattern: bytes               # expresión regular sobre bytes
    sizer: Optional[Callable] = None
    max_size: int = 64 * MB
    offset: int = 0              # posición de la cabecera dentro del archivo
    footer: Optional[bytes] = None
    custom: bool = False
    enabled: bool = True

    @property
    def category(self) -> str:
        return category_of("x." + self.ext)


def _sig(ext, desc, pattern, sizer=None, max_mb=64, offset=0, footer=None):
    return Signature(ext, desc, pattern, sizer, int(max_mb * MB), offset, footer)


SIGNATURES: List[Signature] = [
    _sig("jpg", "Imagen JPEG", rb"\xFF\xD8\xFF[\xC0-\xFE]", size_jpeg, 80),
    _sig("png", "Imagen PNG", rb"\x89PNG\r\n\x1a\n", size_png, 200),
    _sig("gif", "Imagen GIF", rb"GIF8[79]a", size_gif, 100),
    _sig("bmp", "Mapa de bits BMP", rb"BM....\x00\x00\x00\x00", size_bmp, 200),
    _sig("tif", "TIFF / RAW de cámara (CR2, NEF, ARW, DNG, PEF, ORF, RW2…)",
         rb"II\x2A\x00|MM\x00\x2A|IIRO|IIRS|IIU\x00", size_tiff, 300),
    _sig("psd", "Photoshop PSD/PSB", rb"8BPS\x00[\x01\x02]", size_psd, 4000),
    _sig("ico", "Icono de Windows", rb"\x00\x00\x01\x00[\x01-\x40]\x00", size_ico, 4),
    _sig("jp2", "JPEG 2000", rb"\x00\x00\x00\x0CjP  \r\n\x87\n", size_jp2, 200),
    _sig("raf", "RAW Fujifilm", rb"FUJIFILMCCD-RAW", None, 150),
    _sig("x3f", "RAW Sigma X3F", rb"FOVb", None, 120),
    _sig("xcf", "Imagen GIMP", rb"gimp xcf ", None, 500),
    _sig("pdf", "Documento PDF", rb"%PDF-[12]\.", size_pdf, 2000),
    _sig("zip", "ZIP / DOCX / XLSX / PPTX / ODT / EPUB / APK / JAR", rb"PK\x03\x04", size_zip, 4000),
    _sig("doc", "Documento OLE (DOC/XLS/PPT/MSG/VSD/MSI)", rb"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1", size_ole, 1000),
    _sig("rtf", "Texto enriquecido RTF", rb"\{\\rtf1", size_rtf, 200),
    _sig("ps", "PostScript / EPS", rb"%!PS-Adobe-", None, 200, footer=b"%%EOF"),
    _sig("html", "Página HTML", rb"<!DOCTYPE [Hh][Tt][Mm][Ll]|<[Hh][Tt][Mm][Ll][ >]", None, 20, footer=b"</html>"),
    _sig("xml", "Documento XML", rb"<\?xml version=", None, 20),
    _sig("djvu", "Documento DjVu", rb"AT&TFORM", size_djvu, 500),
    _sig("wpd", "WordPerfect", rb"\xFFWPC", None, 100),
    _sig("one", "Bloc de OneNote", rb"\xE4\x52\x5C\x7B\x8C\xD8\xA7\x4D\xAE\xB1\x53\x78\xD0\x29\x96\xD3", None, 500),
    _sig("indd", "Adobe InDesign", rb"\x06\x06\xED\xF5\xD8\x1D\x46\xE5\xBD\x31\xEF\xE7\xFE\x74\xB7\x1D", None, 2000),
    _sig("rar", "Archivo RAR 4/5", rb"Rar!\x1a\x07[\x00\x01]", size_rar, 8000),
    _sig("7z", "Archivo 7-Zip", rb"7z\xBC\xAF\x27\x1C", size_7z, 8000),
    _sig("gz", "Archivo GZIP / TGZ", rb"\x1F\x8B\x08[\x00-\x1F]", size_gz, 2000),
    _sig("bz2", "Archivo BZIP2", rb"BZh[1-9]1AY&SY", size_bz2, 1000),
    _sig("xz", "Archivo XZ", rb"\xFD7zXZ\x00", size_xz, 1000),
    _sig("cab", "Archivo CAB", rb"MSCF\x00\x00\x00\x00", size_cab, 2000),
    _sig("tar", "Archivo TAR", rb"ustar[\x00 ]", size_tar, 8000, offset=257),
    _sig("deb", "Paquete DEB / archivo AR", rb"!<arch>\n", size_ar, 2000),
    _sig("iso", "Imagen ISO 9660", rb"\x01CD001\x01", size_iso, 64000, offset=0x8000),
    _sig("zst", "Archivo Zstandard", rb"\x28\xB5\x2F\xFD", None, 2000),
    _sig("mp3", "Audio MP3 (ID3)", rb"ID3[\x02-\x04]\x00", size_mp3, 300),
    _sig("wav", "RIFF (WAV, AVI, WEBP, ANI, CDR)", rb"RIFF....(?:WAVE|AVI |WEBP|RMID|ACON|CDR.|AMV )", size_riff, 16000),
    _sig("aiff", "Audio AIFF", rb"FORM....AIF[FC]", size_aiff, 2000),
    _sig("flac", "Audio FLAC", rb"fLaC\x00\x00\x00\x22", None, 600),
    _sig("ogg", "Ogg (Vorbis, Opus, Theora)", rb"OggS\x00\x02", size_ogg, 1000),
    _sig("mid", "MIDI", rb"MThd\x00\x00\x00\x06", size_midi, 20),
    _sig("amr", "Audio AMR", rb"#!AMR", None, 50),
    _sig("ape", "Monkey's Audio", rb"MAC \x96\x0F|MAC [\x00-\xFF]\x0F", None, 1000),
    _sig("mp4", "MP4 / MOV / M4A / HEIC / AVIF / 3GP / CR3", rb"ftyp[a-zA-Z0-9 ]{4}", size_isobmff, 64000, offset=4),
    _sig("mkv", "Matroska / WebM", rb"\x1A\x45\xDF\xA3", size_ebml, 64000),
    _sig("flv", "Flash Video", rb"FLV\x01[\x01\x04\x05]\x00\x00\x00\x09", size_flv, 4000),
    _sig("wmv", "ASF (WMV/WMA)", rb"\x30\x26\xB2\x75\x8E\x66\xCF\x11\xA6\xD9\x00\xAA\x00\x62\xCE\x6C", size_asf, 16000),
    _sig("mpg", "Video MPEG-PS", rb"\x00\x00\x01\xBA[\x40-\x7F\x20-\x2F]", None, 4000),
    _sig("swf", "Flash SWF", rb"FWS[\x01-\x30]", size_swf, 100),
    _sig("sqlite", "Base de datos SQLite", rb"SQLite format 3\x00", size_sqlite, 8000),
    _sig("accdb", "Base de datos Access", rb"\x00\x01\x00\x00Standard (?:Jet|ACE) DB", None, 2000),
    _sig("kdbx", "Base de datos KeePass", rb"\x03\xD9\xA2\x9A[\x65\x67]\xFB\x4B\xB5", None, 100),
    _sig("pst", "Correo Outlook PST/OST", rb"!BDN....SM|!BDN....SO", size_pst, 50000),
    _sig("dbx", "Outlook Express DBX", rb"\xCF\xAD\x12\xFE", None, 2000),
    _sig("eml", "Mensaje de correo EML", rb"Return-Path: <|MIME-Version: 1\.0\r?\nContent-Type: multipart", None, 50),
    _sig("exe", "Ejecutable Windows (EXE/DLL/SYS)", rb"MZ[\x00-\xFF]{58}", size_pe, 1000),
    _sig("elf", "Ejecutable ELF", rb"\x7FELF[\x01\x02][\x01\x02]\x01", size_elf, 1000),
    _sig("macho", "Ejecutable Mach-O", rb"\xCF\xFA\xED\xFE|\xCE\xFA\xED\xFE", None, 500),
    _sig("evtx", "Registro de eventos de Windows", rb"ElfFile\x00", size_evtx, 1000),
    _sig("reg", "Colmena del registro", rb"regf", size_regf, 1000),
    _sig("lnk", "Acceso directo de Windows", rb"\x4C\x00\x00\x00\x01\x14\x02\x00", None, 0.25),
    _sig("plist", "Lista de propiedades binaria", rb"bplist00", None, 10),
    _sig("ttf", "Fuente TrueType/OpenType", rb"\x00\x01\x00\x00\x00[\x04-\x40]|OTTO\x00[\x04-\x40]", size_sfnt, 50),
    _sig("woff", "Fuente web WOFF/WOFF2", rb"wOFF|wOF2", size_woff, 20),
    _sig("dwg", "Dibujo AutoCAD DWG", rb"AC10[0-3][0-9]", None, 500),
    _sig("blend", "Escena de Blender", rb"BLENDER[-_][vV]\d\d\d", size_blend, 4000),
    _sig("fbx", "Modelo FBX", rb"Kaydara FBX Binary  \x00", None, 2000),
    _sig("skp", "Modelo SketchUp", rb"\xFF\xFE\xFF\x0ES\x00k\x00e\x00t\x00c\x00h\x00U\x00p\x00", None, 1000),
    _sig("emf", "Metarchivo EMF", rb"\x01\x00\x00\x00.{36} EMF", size_emf, 100),
    _sig("vmdk", "Disco virtual VMDK", rb"KDMV\x01\x00\x00\x00", None, 64000),
    _sig("vdi", "Disco virtual VirtualBox", rb"<<< (?:Oracle VM VirtualBox|Sun xVM VirtualBox|innotek VirtualBox) Disk Image >>>", None, 64000),
    _sig("qcow2", "Disco virtual QCOW2", rb"QFI\xFB\x00\x00\x00[\x02\x03]", None, 64000),
    _sig("torrent", "Archivo torrent", rb"d8:announce", None, 10),
]


_META = set(b"[(.|?*+{^$")


def _literal_prefixes(pattern: bytes) -> List[bytes]:
    """Prefijos literales de cada alternativa de nivel superior del patrón."""
    alts, depth, cls, cur, i = [], 0, False, bytearray(), 0
    while i < len(pattern):
        c = pattern[i]
        if c == 0x5C:
            cur += pattern[i:i + 2] if pattern[i + 1:i + 2] != b"x" else pattern[i:i + 4]
            i += 2 if pattern[i + 1:i + 2] != b"x" else 4
            continue
        if c == ord("[") and not cls:
            cls = True
        elif c == ord("]") and cls:
            cls = False
        elif not cls and c == ord("("):
            depth += 1
        elif not cls and c == ord(")"):
            depth -= 1
        elif not cls and depth == 0 and c == ord("|"):
            alts.append(bytes(cur))
            cur = bytearray()
            i += 1
            continue
        cur.append(c)
        i += 1
    alts.append(bytes(cur))
    out = []
    for a in alts:
        lit = bytearray()
        j = 0
        while j < len(a):
            c = a[j]
            if c == 0x5C:
                if a[j + 1:j + 2] == b"x":
                    nxt = a[j + 4:j + 5]
                    if nxt and nxt in b"?*{":
                        break
                    lit.append(int(a[j + 2:j + 4], 16))
                    j += 4
                    continue
                if a[j + 1:j + 2] and a[j + 1:j + 2] in b"dDsSwWbB":
                    break
                esc = a[j + 1:j + 2]
                lit += {b"r": b"\r", b"n": b"\n", b"t": b"\t", b"0": b"\0"}.get(esc, esc)
                j += 2
                continue
            if c in _META:
                break
            if a[j + 1:j + 2] and a[j + 1:j + 2] in b"?*{":
                break
            lit.append(c)
            j += 1
        out.append(bytes(lit))
    return out


def load_custom(path: str) -> List[Signature]:
    """Carga firmas personalizadas: [{"ext": "abc", "descripcion": "...", "cabecera": "414243",
    "pie": "FFD9", "desplazamiento": 0, "max_mb": 10}]"""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            items = json.load(fh)
    except (OSError, ValueError):
        return out
    for it in items:
        try:
            hdr = bytes.fromhex(it["cabecera"].replace(" ", ""))
            ftr = bytes.fromhex(it["pie"].replace(" ", "")) if it.get("pie") else None
            out.append(Signature(it["ext"].lower(), it.get("descripcion", it["ext"]), re.escape(hdr),
                                 None, int(float(it.get("max_mb", 20)) * MB), int(it.get("desplazamiento", 0)),
                                 ftr, custom=True))
        except (KeyError, ValueError):
            continue
    return out


def save_custom(path: str, sigs: List[Signature]) -> None:
    items = []
    for s in sigs:
        if not s.custom:
            continue
        items.append({"ext": s.ext, "descripcion": s.desc,
                      "cabecera": re.sub(rb"\\(.)", rb"\1", s.pattern).hex(),
                      "pie": s.footer.hex() if s.footer else "", "desplazamiento": s.offset,
                      "max_mb": s.max_size / MB})
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2, ensure_ascii=False)


def identify(head: bytes, src: Optional[BlockSource] = None) -> Optional[str]:
    """Adivina la extensión a partir de los primeros bytes; si se da ``src`` (el
    contenido completo) se valida con el medidor para distinguir DOCX/XLSX, CR2/NEF…"""
    for s in SIGNATURES:
        if re.match(s.pattern, head[s.offset:s.offset + 64], re.S):
            if src is not None and s.sizer:
                try:
                    r = s.sizer(Win(src, 0, src.size), src.size)
                    if r and r[1]:
                        return r[1]
                except Exception:
                    pass
            return s.ext
    return None


# --------------------------------------------------------------------------- #
# Motor
# --------------------------------------------------------------------------- #
class Carver:
    def __init__(self, signatures: Optional[List[Signature]] = None, aligned: bool = True,
                 align: int = 512, skip_inside: bool = True) -> None:
        self.sigs = [s for s in (signatures or SIGNATURES) if s.enabled]
        self.aligned = aligned
        self.align = align
        self.skip_inside = skip_inside
        parts = []
        for i, s in enumerate(self.sigs):
            parts.append(b"(?P<s%d>%s)" % (i, s.pattern))
        self.rx = re.compile(b"|".join(parts), re.S)
        # para el modo alineado: una expresión por desplazamiento de cabecera
        groups: Dict[int, List[bytes]] = {}
        for i, s in enumerate(self.sigs):
            groups.setdefault(s.offset, []).append(b"(?P<s%d>%s)" % (i, s.pattern))
        self.by_offset = [(o, re.compile(b"|".join(ps), re.S)) for o, ps in sorted(groups.items())]
        # para el modo no alineado: prefijos literales buscados con bytes.find (velocidad de C)
        self.prefixes = []
        self.slow = []
        for s in self.sigs:
            rx1 = re.compile(s.pattern, re.S)
            prefs = _literal_prefixes(s.pattern)
            if prefs and all(len(x) >= 2 for x in prefs):
                for x in prefs:
                    self.prefixes.append((x, rx1, s))
            else:
                self.slow.append((rx1, s))

    def _size_of(self, src: BlockSource, sig: Signature, start: int, end: int) -> Result:
        maxs = min(sig.max_size, end - start)
        w = Win(src, start, maxs)
        if sig.sizer:
            try:
                return sig.sizer(w, maxs)
            except (struct.error, IndexError, ValueError, OverflowError, MemoryError):
                return None
        if sig.footer:
            p = w.find(sig.footer, 1, maxs)
            if p >= 0:
                return p + len(sig.footer), None
        return -1, None  # tamaño abierto: se cierra en la siguiente cabecera

    def _matches(self, blk: bytes, pos: int, limit: int):
        """Genera (inicio_absoluto, firma) en orden creciente."""
        if not self.aligned:
            cands = []
            for pref, rx1, sig in self.prefixes:
                i = blk.find(pref)
                while i != -1 and i < limit:
                    if rx1.match(blk, i):
                        cands.append((pos + i - sig.offset, sig))
                    i = blk.find(pref, i + 1)
            for rx1, sig in self.slow:
                for m in rx1.finditer(blk):
                    if m.start() >= limit:
                        break
                    cands.append((pos + m.start() - sig.offset, sig))
            cands.sort(key=lambda c: c[0])
            yield from cands
            return
        a = self.align
        first = (-pos) % a
        L = len(blk)
        for p in range(first, limit, a):
            for o, rx in self.by_offset:
                if p + o >= L:
                    continue
                m = rx.match(blk, p + o)
                if m:
                    yield pos + p, self.sigs[int(m.lastgroup[1:])]
                    break

    def scan(self, src: BlockSource, start: int = 0, end: Optional[int] = None,
             progress=None, cancel=None, on_found: Optional[Callable] = None) -> List[FileEntry]:
        end = end or src.size
        out: List[FileEntry] = []
        counters: Dict[str, int] = {}
        open_entry: Optional[Tuple[FileEntry, Signature]] = None
        CH = 8 * MB
        OV = 0x8100
        pos = start
        skip_to = start

        def close_open(at: int):
            nonlocal open_entry
            if open_entry:
                e, s = open_entry
                e.size = max(0, min(s.max_size, at - e.extents[0][0]))
                e.extents = [(e.extents[0][0], e.size)]
                if e.size >= 64:
                    out.append(e)
                    if on_found:
                        on_found(e)
                open_entry = None

        while pos < end:
            if cancel and cancel():
                break
            blk = src.read(pos, min(CH + OV, end - pos))
            if not blk:
                break
            limit = min(CH, len(blk))
            for fstart, sig in self._matches(blk, pos, limit):
                if fstart < start or fstart < skip_to:
                    continue
                res = self._size_of(src, sig, fstart, end)
                if res is None:
                    continue
                size, ext = res
                ext = ext or sig.ext
                if size == 0:
                    continue
                close_open(fstart)
                n = counters.get(ext, 0) + 1
                counters[ext] = n
                cat = category_of("x." + ext)
                e = FileEntry(name=f"{ext.upper()}_{n:06d}_{fstart:X}.{ext}",
                              path=f"Encontrados por firma/{cat}/{ext.upper()}", fs="RAW", origin="raw",
                              ident=f"0x{fstart:X}", chance="Buena")
                if size < 0:
                    e.extents = [(fstart, 0)]
                    e.chance = "Regular"
                    e.note = "Tamaño estimado (formato sin tamaño interno)"
                    open_entry = (e, sig)
                    continue
                e.size = size
                e.extents = [(fstart, size)]
                out.append(e)
                if on_found:
                    on_found(e)
                if self.skip_inside:
                    skip_to = fstart + size
            pos += CH
            if progress:
                progress(min(pos, end) - start, end - start)
        close_open(min(end, src.size))
        return out
