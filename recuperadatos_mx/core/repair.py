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
    raise ValueError("Formato sin reparador: JPEG, PDF, ZIP, DOCX/XLSX/PPTX, ODT/ODS/ODP, EPUB")
