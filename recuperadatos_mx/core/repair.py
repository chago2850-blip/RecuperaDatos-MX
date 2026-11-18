"""Reparación de archivos dañados: fotos JPEG, documentos ZIP/Office/OpenDocument
y PDF.  Nunca modifica el original: siempre escribe una copia reparada."""
from __future__ import annotations

import os
import re
import struct
import zipfile
import zlib
from typing import Optional, Tuple


def _out(path: str, suffix: str = "_reparado") -> str:
    b, e = os.path.splitext(path)
    return f"{b}{suffix}{e}"


# --------------------------------------------------------------------------- #
def repair_jpeg(path: str, reference: Optional[str] = None, dest: Optional[str] = None) -> Tuple[str, str]:
    """Repara JPEG truncados o con cabecera dañada.

    Si se indica una foto de referencia (misma cámara y resolución) se trasplanta
    su cabecera completa (tablas de cuantización/Huffman, SOF y SOS) sobre los
    datos de imagen del archivo dañado.
    """
    data = open(path, "rb").read()
    dest = dest or _out(path)
    notes = []
    sos = data.find(b"\xFF\xDA")
    if reference:
        ref = open(reference, "rb").read()
        rsos = ref.find(b"\xFF\xDA")
        if rsos < 0:
            raise ValueError("La foto de referencia no es un JPEG válido")
        rlen = struct.unpack(">H", ref[rsos + 2:rsos + 4])[0]
        header = ref[:rsos + 2 + rlen]
        if sos >= 0 and sos + 4 <= len(data):
            body = data[sos + 2 + struct.unpack(">H", data[sos + 2:sos + 4])[0]:]
        else:
            # cabecera destruida: buscar el comienzo probable de los datos comprimidos
            body = data[min(len(data), 0x400):]
        data = header + body
        notes.append("cabecera trasplantada desde la referencia")
    elif not data.startswith(b"\xFF\xD8"):
        i = data.find(b"\xFF\xD8\xFF")
        if i > 0:
            data = data[i:]
            notes.append("se eliminaron bytes basura antes del inicio")
        else:
            data = b"\xFF\xD8" + data[2:] if len(data) > 2 else data
            notes.append("se restauró el marcador SOI")
    end = data.rfind(b"\xFF\xD9")
    if end < 0 or end < len(data) - 4096:
        tail = data.rstrip(b"\x00\xFF")
        data = tail + b"\xFF\xD9"
        notes.append("se agregó el marcador de fin (EOI)")
    else:
        data = data[:end + 2]
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest, ", ".join(notes) or "sin cambios estructurales necesarios"


# --------------------------------------------------------------------------- #
def repair_zip(path: str, dest: Optional[str] = None) -> Tuple[str, str]:
    """Reconstruye el directorio central de ZIP/DOCX/XLSX/PPTX/ODT a partir de las
    cabeceras locales y vuelve a comprimir cada entrada legible."""
    data = open(path, "rb").read()
    dest = dest or _out(path)
    pos = 0
    recovered = failed = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        while True:
            i = data.find(b"PK\x03\x04", pos)
            if i < 0 or i + 30 > len(data):
                break
            (ver, flag, method, mt, md, crc, csize, usize, nlen, xlen) = struct.unpack_from("<HHHHHIIIHH", data, i + 4)
            name = data[i + 30:i + 30 + nlen].decode("utf-8" if flag & 0x800 else "cp437", "replace")
            start = i + 30 + nlen + xlen
            if flag & 0x08 or csize == 0 and usize:
                nxt = data.find(b"PK\x03\x04", start)
                cd = data.find(b"PK\x01\x02", start)
                cands = [x for x in (nxt, cd) if x >= 0]
                end = min(cands) if cands else len(data)
                if data[end - 16:end - 12] == b"PK\x07\x08":
                    end -= 16
                csize = end - start
            payload = data[start:start + csize]
            try:
                if method == 0:
                    content = payload
                elif method == 8:
                    d = zlib.decompressobj(-15)
                    content = d.decompress(payload)
                else:
                    raise ValueError("método no soportado")
                if name and not name.endswith("/"):
                    zout.writestr(name, content)
                recovered += 1
            except (zlib.error, ValueError):
                failed += 1
            pos = start + max(csize, 1)
    return dest, f"{recovered} entradas recuperadas, {failed} dañadas"


# --------------------------------------------------------------------------- #
def repair_pdf(path: str, dest: Optional[str] = None) -> Tuple[str, str]:
    """Reconstruye la tabla xref y el trailer escaneando los objetos del archivo."""
    data = open(path, "rb").read()
    dest = dest or _out(path)
    start = data.find(b"%PDF-")
    if start < 0:
        raise ValueError("No se encontró la cabecera %PDF")
    data = data[start:]
    objs = {}
    for m in re.finditer(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b", data):
        objs[int(m.group(1))] = (m.start(), int(m.group(2)))
    if not objs:
        raise ValueError("No se encontraron objetos PDF")
    root = None
    for num, (off, gen) in sorted(objs.items()):
        chunk = data[off:off + 2048]
        if re.search(rb"/Type\s*/Catalog", chunk):
            root = (num, gen)
    # recortar basura tras el último endobj / %%EOF
    last = max(data.rfind(b"endobj"), data.rfind(b"%%EOF"))
    body = data[:last + 6] if last > 0 else data
    size = max(objs) + 1
    xref_off = len(body) + 1
    lines = [b"xref", b"0 %d" % size, b"0000000000 65535 f\r"]
    for i in range(1, size):
        if i in objs:
            lines.append(b"%010d %05d n\r" % (objs[i][0], objs[i][1]))
        else:
            lines.append(b"0000000000 65535 f\r")
    trailer = b"trailer\n<< /Size %d" % size
    if root:
        trailer += b" /Root %d %d R" % root
    trailer += b" >>\nstartxref\n%d\n%%%%EOF\n" % xref_off
    with open(dest, "wb") as fh:
        fh.write(body + b"\n" + b"\n".join(lines) + b"\n" + trailer)
    return dest, f"{len(objs)} objetos indexados" + ("" if root else " (sin catálogo raíz: puede requerir otro visor)")


def repair(path: str, reference: Optional[str] = None) -> Tuple[str, str]:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".jpe", ".jfif"):
        return repair_jpeg(path, reference)
    if ext in (".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub", ".jar", ".apk", ".vsdx"):
        return repair_zip(path)
    if ext == ".pdf":
        return repair_pdf(path)
    if ext in (".mp4", ".mov", ".m4v", ".3gp", ".insv"):
        if not reference:
            raise ValueError("Para reparar video indique un video de referencia sano de la misma cámara")
        return repair_video(path, reference)
    raise ValueError("Formato sin reparador: JPEG, PDF, ZIP, DOCX/XLSX/PPTX, ODT/ODS/ODP, EPUB, MP4/MOV")


# --------------------------------------------------------------------------- #
# Video MP4 / MOV (H.264 / H.265): reconstrucción del índice «moov» perdido
# --------------------------------------------------------------------------- #
def _atoms(data: bytes, start: int = 0, end: Optional[int] = None):
    end = len(data) if end is None else end
    p = start
    while p + 8 <= end:
        size, typ = struct.unpack_from(">I4s", data, p)
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, p + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - p
        if size < hdr:
            break
        yield typ, p, hdr, min(size, end - p)
        p += size


def _find(data: bytes, path: list, start: int = 0, end: Optional[int] = None):
    for typ, p, hdr, size in _atoms(data, start, end):
        if typ == path[0]:
            if len(path) == 1:
                return p, hdr, size
            skip = {b"stsd": 8, b"meta": 4}.get(typ, 0)
            r = _find(data, path[1:], p + hdr + skip, p + size)
            if r:
                return r
    return None


def _video_track(ref: bytes) -> dict:
    moov = _find(ref, [b"moov"])
    if not moov:
        raise ValueError("El video de referencia no tiene índice (moov)")
    for typ, p, hdr, size in _atoms(ref, moov[0] + moov[1], moov[0] + moov[2]):
        if typ != b"trak":
            continue
        h = _find(ref, [b"mdia", b"hdlr"], p + hdr, p + size)
        if not h or ref[h[0] + h[1] + 8:h[0] + h[1] + 12] != b"vide":
            continue
        md = _find(ref, [b"mdia", b"mdhd"], p + hdr, p + size)
        v = ref[md[0] + md[1]]
        ts = struct.unpack_from(">I", ref, md[0] + md[1] + (20 if v == 1 else 12))[0]
        sd = _find(ref, [b"mdia", b"minf", b"stbl", b"stsd"], p + hdr, p + size)
        stsd = ref[sd[0]:sd[0] + sd[2]]
        st = _find(ref, [b"mdia", b"minf", b"stbl", b"stts"], p + hdr, p + size)
        delta = struct.unpack_from(">I", ref, st[0] + st[1] + 12)[0] if st else ts // 30
        tk = _find(ref, [b"tkhd"], p + hdr, p + size)
        tkv = ref[tk[0] + tk[1]]
        wpos = tk[0] + tk[1] + (88 if tkv == 1 else 76)
        width, height = struct.unpack_from(">II", ref, wpos)
        codec = stsd[16 + 4:16 + 8]
        nal_len = 4
        cfg = stsd.find(b"avcC") if codec in (b"avc1", b"avc3") else stsd.find(b"hvcC")
        if cfg > 0 and codec in (b"avc1", b"avc3"):
            nal_len = (stsd[cfg + 4 + 4] & 3) + 1
        elif cfg > 0:
            nal_len = (stsd[cfg + 4 + 21] & 3) + 1
        t = {"timescale": ts, "delta": delta, "stsd": stsd, "codec": codec, "nal": nal_len,
             "width": width, "height": height, "hevc": codec in (b"hvc1", b"hev1")}
        t["learned"], t["maxlen"] = _learn(ref, p + hdr, p + size, nal_len)
        return t
    raise ValueError("El video de referencia no tiene pista de video")


def _learn(ref: bytes, tstart: int, tend: int, n: int):
    """Aprende de la referencia los dos primeros bytes de cada NAL y su tamaño máximo."""
    def table(name):
        r = _find(ref, [b"mdia", b"minf", b"stbl", name], tstart, tend)
        return r
    sz, co, c64, sc = table(b"stsz"), table(b"stco"), table(b"co64"), table(b"stsc")
    learned, maxlen = set(), 0
    if not sz or not (co or c64) or not sc:
        return learned, 1 << 22
    b = sz[0] + sz[1]
    fixed, count = struct.unpack_from(">II", ref, b + 4)
    sizes = [fixed] * count if fixed else list(struct.unpack_from(f">{count}I", ref, b + 12))
    if co:
        cb = co[0] + co[1]
        nchunk = struct.unpack_from(">I", ref, cb + 4)[0]
        offs = list(struct.unpack_from(f">{nchunk}I", ref, cb + 8))
    else:
        cb = c64[0] + c64[1]
        nchunk = struct.unpack_from(">I", ref, cb + 4)[0]
        offs = list(struct.unpack_from(f">{nchunk}Q", ref, cb + 8))
    sb = sc[0] + sc[1]
    ne = struct.unpack_from(">I", ref, sb + 4)[0]
    runs = [struct.unpack_from(">III", ref, sb + 8 + 12 * i) for i in range(ne)]
    si = 0
    for ci, off in enumerate(offs):
        per = next((r[1] for r in reversed(runs) if r[0] <= ci + 1), 1)
        p = off
        for _ in range(per):
            if si >= len(sizes):
                break
            q, e = p, p + sizes[si]
            while q + n + 2 <= e:
                ln = int.from_bytes(ref[q:q + n], "big")
                learned.add((ref[q + n], ref[q + n + 1]))
                maxlen = max(maxlen, ln)
                q += n + ln
            p = e
            si += 1
    return learned, max(maxlen * 4, 1 << 16)


def _nal_ok(t: dict, b0: int, b1: int) -> bool:
    if b0 & 0x80:
        return False
    if t["hevc"]:
        typ = (b0 >> 1) & 0x3F
        return typ <= 40 and (b1 & 0x07) >= 1 and (b0 & 1) == 0
    typ = b0 & 0x1F
    return 1 <= typ <= 21 and typ not in (13, 14, 15, 16, 17, 18)


def _is_slice(t: dict, b0: int) -> bool:
    return ((b0 >> 1) & 0x3F) <= 31 if t["hevc"] else (b0 & 0x1F) in (1, 5)


def _is_key(t: dict, b0: int) -> bool:
    return 16 <= ((b0 >> 1) & 0x3F) <= 21 if t["hevc"] else (b0 & 0x1F) == 5


def _first_slice(t: dict, data: bytes, p: int) -> bool:
    # primer bit del encabezado de slice: first_mb_in_slice == 0 (H.264) / first_slice_segment_in_pic (H.265)
    off = p + (2 if t["hevc"] else 1)
    return bool(data[off] & 0x80) if off < len(data) else True


def _scan_samples(data: bytes, start: int, end: int, t: dict):
    n = t["nal"]
    samples = []  # (offset, size, key)
    p = start
    cur = None
    skipped = 0

    def valid_at(q: int, strict: bool) -> bool:
        if q + n + 2 > end:
            return False
        ln = int.from_bytes(data[q:q + n], "big")
        if ln < 2 or q + n + ln > end:
            return False
        if not _nal_ok(t, data[q + n], data[q + n + 1]):
            return False
        if strict and t.get("learned"):
            # al resincronizar solo se aceptan cabeceras vistas en la referencia
            return (data[q + n], data[q + n + 1]) in t["learned"] and ln <= t["maxlen"]
        q2 = q + n + ln
        if strict and q2 + n + 2 <= end:
            ln2 = int.from_bytes(data[q2:q2 + n], "big")
            if ln2 < 2 or q2 + n + ln2 > end + 1 or not _nal_ok(t, data[q2 + n], data[q2 + n + 1]):
                return len(samples) > 0 and q2 >= end - 64
        return True

    while p < end:
        if not valid_at(p, cur is None):
            if cur:
                samples.append(tuple(cur))
                cur = None
            p += 1
            skipped += 1
            continue
        ln = int.from_bytes(data[p:p + n], "big")
        b0 = data[p + n]
        new_au = False
        if cur is None:
            new_au = True
        elif _is_slice(t, b0) and _first_slice(t, data, p + n) and cur[3]:
            new_au = True
        elif not _is_slice(t, b0) and cur[3]:
            new_au = True
        if new_au:
            if cur:
                samples.append(tuple(cur))
            cur = [p, 0, False, False]
        cur[1] = p + n + ln - cur[0]
        if _is_key(t, b0):
            cur[2] = True
        if _is_slice(t, b0):
            cur[3] = True
        p += n + ln
    if cur:
        samples.append(tuple(cur))
    return [(o, s, k) for o, s, k, has_slice in samples if has_slice], skipped


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _full(typ: bytes, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    return _box(typ, struct.pack(">I", (version << 24) | flags) + payload)


def _build_moov(t: dict, samples) -> bytes:
    n = len(samples)
    dur = n * t["delta"]
    ts = t["timescale"]
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = _full(b"mvhd", struct.pack(">IIII", 0, 0, ts, dur) + struct.pack(">IH", 0x10000, 0x100) + b"\0" * 10
                 + matrix + b"\0" * 24 + struct.pack(">I", 2))
    tkhd = _full(b"tkhd", struct.pack(">IIIII", 0, 0, 1, 0, dur) + b"\0" * 8 + struct.pack(">HHH", 0, 0, 0)
                 + b"\0\0" + matrix + struct.pack(">II", t["width"], t["height"]), flags=3)
    mdhd = _full(b"mdhd", struct.pack(">IIII", 0, 0, ts, dur) + struct.pack(">HH", 0x55C4, 0))
    hdlr = _full(b"hdlr", b"\0" * 4 + b"vide" + b"\0" * 12 + b"VideoHandler\0")
    vmhd = _full(b"vmhd", b"\0" * 8, flags=1)
    dinf = _box(b"dinf", _full(b"dref", struct.pack(">I", 1) + _full(b"url ", b"", flags=1)))
    stts = _full(b"stts", struct.pack(">III", 1, n, t["delta"]))
    keys = [i + 1 for i, (_, _, k) in enumerate(samples) if k] or [1]
    stss = _full(b"stss", struct.pack(">I", len(keys)) + b"".join(struct.pack(">I", k) for k in keys))
    stsz = _full(b"stsz", struct.pack(">II", 0, n) + b"".join(struct.pack(">I", s) for _, s, _ in samples))
    stsc = _full(b"stsc", struct.pack(">IIII", 1, 1, 1, 1))
    if samples and samples[-1][0] > 0xFFFFFFFF:
        stco = _full(b"co64", struct.pack(">I", n) + b"".join(struct.pack(">Q", o) for o, _, _ in samples))
    else:
        stco = _full(b"stco", struct.pack(">I", n) + b"".join(struct.pack(">I", o) for o, _, _ in samples))
    stbl = _box(b"stbl", t["stsd"] + stts + stss + stsz + stsc + stco)
    minf = _box(b"minf", vmhd + dinf + stbl)
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    trak = _box(b"trak", tkhd + mdia)
    return _box(b"moov", mvhd + trak)


def repair_video(path: str, reference: str, dest: Optional[str] = None) -> Tuple[str, str]:
    """Reconstruye el índice de un MP4/MOV cortado (grabación interrumpida, batería agotada,
    archivo recuperado sin moov) usando un video de referencia de la misma cámara y ajustes."""
    data = open(path, "rb").read()
    ref = open(reference, "rb").read()
    t = _video_track(ref)
    dest = dest or _out(path)
    md = None
    for typ, p, hdr, size in _atoms(data):
        if typ == b"mdat":
            md = (p, hdr)
    if md is None:
        i = data.find(b"mdat")
        if i < 4:
            raise ValueError("No se encontraron datos de video (mdat)")
        md = (i - 4, 8)
    start = md[0] + md[1]
    samples, skipped = _scan_samples(data, start, len(data), t)
    if not samples:
        raise ValueError("No se reconocieron cuadros de video compatibles con la referencia")
    ftyp = _find(data, [b"ftyp"]) or _find(ref, [b"ftyp"])
    src_ftyp = (data if _find(data, [b"ftyp"]) else ref)[ftyp[0]:ftyp[0] + ftyp[2]]
    body = data[start:]
    out = bytearray(src_ftyp)
    mdat_hdr = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + len(body))
    shift = len(out) + 16 - start
    out += mdat_hdr + body
    fixed = [(o + shift, s, k) for o, s, k in samples]
    out += _build_moov(t, fixed)
    with open(dest, "wb") as fh:
        fh.write(out)
    secs = len(samples) * t["delta"] / t["timescale"]
    note = f"{len(samples)} cuadros de video reconstruidos ({secs:.1f} s, {t['codec'].decode()})"
    if skipped > 1024:
        note += "; el audio se omite porque su índice no se puede reconstruir sin el original"
    return dest, note
