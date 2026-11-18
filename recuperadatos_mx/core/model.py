"""Modelo de datos de los archivos encontrados y lectura de su contenido."""
from __future__ import annotations

import base64
import os
from dataclasses import asdict, dataclass, field
from typing import Iterator, List, Optional, Tuple

from .device import BlockSource

# Categorías para el filtro por tipo (como en EaseUS / Disk Drill)
CATEGORIES = {
    "Imágenes": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif", "psd",
                 "ico", "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "raf", "svg", "jp2", "avif",
                 "pef", "srw", "x3f", "tga", "xcf"},
    "Videos": {"mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv", "3gp", "mpg", "mpeg", "mts",
               "m2ts", "ts", "vob", "insv", "mxf", "asf", "rm", "rmvb", "nev", "braw", "r3d"},
    "Audio": {"mp3", "wav", "flac", "ogg", "m4a", "aac", "wma", "aiff", "aif", "amr", "mid", "midi",
              "opus", "ape", "au", "ra"},
    "Documentos": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf",
                   "txt", "csv", "html", "htm", "xml", "json", "md", "epub", "pages", "numbers",
                   "key", "vsd", "vsdx", "pub", "wpd", "djvu", "tex", "ps", "eps", "msg", "one"},
    "Archivos comprimidos": {"zip", "rar", "7z", "gz", "tgz", "bz2", "xz", "tar", "cab", "iso", "zst",
                             "lz4", "lzh", "arj", "dmg", "jar", "apk", "deb", "rpm", "xpi", "cpio"},
    "Correo": {"pst", "ost", "eml", "mbox", "dbx", "emlx", "nsf"},
    "Bases de datos": {"sqlite", "db", "mdb", "accdb", "dbf", "frm", "ibd", "sql", "kdbx", "realm"},
    "Programas y sistema": {"exe", "dll", "sys", "elf", "so", "dylib", "msi", "class", "pyc", "bin",
                            "lnk", "evtx", "reg", "dat", "plist", "torrent"},
    "Diseño y CAD": {"dwg", "dxf", "ai", "indd", "skp", "blend", "fbx", "obj", "stl", "3ds", "max",
                     "cdr", "sketch", "fig"},
    "Fuentes": {"ttf", "otf", "woff", "woff2"},
    "Máquinas virtuales": {"vmdk", "vhd", "vhdx", "vdi", "qcow2", "ova"},
}


def category_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Otros"


@dataclass
class FileEntry:
    name: str
    path: str = ""                       # carpeta contenedora, con "/" como separador
    size: int = 0
    extents: List[Tuple[int, int]] = field(default_factory=list)  # (desplazamiento, longitud); -1 = disperso
    deleted: bool = False
    is_dir: bool = False
    fs: str = ""
    origin: str = "fs"                   # fs | raw | boveda | ntfs-huerfano
    mtime: float = 0.0
    ctime: float = 0.0
    atime: float = 0.0
    chance: str = ""                     # Excelente | Buena | Regular | Baja
    ident: str = ""                      # MFT#, inodo, cluster …
    resident: Optional[bytes] = None     # datos residentes (NTFS / ext4 inline)
    codec: str = ""                      # "" | lznt1
    codec_args: dict = field(default_factory=dict)
    note: str = ""

    @property
    def full_path(self) -> str:
        return (self.path.rstrip("/") + "/" + self.name) if self.path else self.name

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower().lstrip(".")

    @property
    def category(self) -> str:
        return "Carpetas" if self.is_dir else category_of(self.name)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["resident"] = base64.b64encode(self.resident).decode() if self.resident is not None else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FileEntry":
        d = dict(d)
        if d.get("resident") is not None:
            d["resident"] = base64.b64decode(d["resident"])
        d["extents"] = [tuple(x) for x in d.get("extents", [])]
        return cls(**d)


CHUNK = 1 << 20


def iter_content(src: BlockSource, entry: FileEntry, limit: Optional[int] = None) -> Iterator[bytes]:
    """Genera el contenido del archivo en bloques."""
    src = getattr(entry, "_src", None) or src
    remaining = entry.size if limit is None else min(entry.size, limit)
    if entry.resident is not None:
        yield entry.resident[:remaining]
        return
    if entry.codec == "apfs-xts":
        from .crypto.common import iter_apfs_encrypted
        yield from iter_apfs_encrypted(src, entry, remaining)
        return
    if entry.codec == "lznt1":
        from .fs.ntfs import iter_compressed
        yield from iter_compressed(src, entry, remaining)
        return
    for off, length in entry.extents:
        if remaining <= 0:
            break
        n = min(length, remaining)
        pos = 0
        while pos < n:
            k = min(CHUNK, n - pos)
            if off < 0:
                data = b"\0" * k
            else:
                data = src.read(off + pos, k)
                if len(data) < k:
                    data = data.ljust(k, b"\0")
            yield data
            pos += k
        remaining -= n
    if remaining > 0 and entry.extents:
        # el tamaño declarado excede las extensiones conocidas
        while remaining > 0:
            k = min(CHUNK, remaining)
            yield b"\0" * k
            remaining -= k


def read_head(src: BlockSource, entry: FileEntry, n: int = 4096) -> bytes:
    out = bytearray()
    for chunk in iter_content(src, entry, limit=n):
        out += chunk
        if len(out) >= n:
            break
    return bytes(out[:n])


class EntrySource(BlockSource):
    """Expone el contenido de un FileEntry como una fuente de bloques (vista previa,
    identificación de tipo, imágenes dentro de imágenes)."""

    def __init__(self, src: BlockSource, entry: FileEntry) -> None:
        src = getattr(entry, "_src", None) or src
        self.src, self.entry = src, entry
        self.size = entry.size
        self.name = entry.name
        self._flat = None
        if entry.resident is not None or entry.codec:
            self._flat = b"".join(iter_content(src, entry, limit=256 << 20))

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        if self._flat is not None:
            return self._flat[offset:offset + length]
        out = bytearray()
        pos = 0
        for off, ln in self.entry.extents:
            if pos + ln > offset and pos < offset + length:
                s = max(offset, pos)
                e = min(offset + length, pos + ln)
                out += (b"\0" * (e - s)) if off < 0 else self.src.read(off + s - pos, e - s).ljust(e - s, b"\0")
            pos += ln
            if pos >= offset + length:
                break
        return bytes(out).ljust(length, b"\0")
