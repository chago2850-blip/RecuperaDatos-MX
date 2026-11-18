"""Fuentes de bloques: discos físicos, particiones, imágenes y vistas parciales.

Toda lectura del motor pasa por :class:`BlockSource`.  Las fuentes se abren
siempre en modo de solo lectura; la escritura (restaurar imagen, clonar,
editar sectores) usa rutas explícitas y separadas.
"""
from __future__ import annotations

import io
import os
import re
import struct
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional

SECTOR = 512


class BlockSource:
    """Interfaz mínima de lectura aleatoria."""

    name: str = "fuente"
    size: int = 0
    sector_size: int = SECTOR

    def read(self, offset: int, length: int) -> bytes:  # pragma: no cover - interfaz
        raise NotImplementedError

    def close(self) -> None:
        pass

    def describe(self) -> dict:
        return {"tipo": type(self).__name__, "nombre": self.name, "tamano": self.size}

    # utilidades
    def read_sector(self, lba: int, count: int = 1) -> bytes:
        return self.read(lba * self.sector_size, count * self.sector_size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ErrorLog:
    """Registro de sectores ilegibles encontrados durante las lecturas."""

    def __init__(self) -> None:
        self.bad: List[tuple] = []
        self._lock = threading.Lock()

    def add(self, offset: int, length: int) -> None:
        with self._lock:
            self.bad.append((offset, length))

    @property
    def bad_bytes(self) -> int:
        return sum(l for _, l in self.bad)


def _windows_size(handle_path: str) -> int:
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 1
    FILE_SHARE_WRITE = 2
    OPEN_EXISTING = 3
    IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(handle_path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None, OPEN_EXISTING, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(f"No se pudo abrir {handle_path}")
    try:
        out = ctypes.c_longlong(0)
        ret = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                                 ctypes.byref(out), 8, ctypes.byref(ret), None)
        if not ok:
            raise OSError("DeviceIoControl falló")
        return out.value
    finally:
        k32.CloseHandle(h)


def _mac_size(fd: int) -> tuple:
    import fcntl
    DKIOCGETBLOCKSIZE = 0x40046418
    DKIOCGETBLOCKCOUNT = 0x40086419
    bs = struct.unpack("I", fcntl.ioctl(fd, DKIOCGETBLOCKSIZE, b"\0" * 4))[0]
    bc = struct.unpack("Q", fcntl.ioctl(fd, DKIOCGETBLOCKCOUNT, b"\0" * 8))[0]
    return bs * bc, bs


class FileSource(BlockSource):
    """Disco, partición o imagen RAW abierta como archivo.

    Las lecturas se alinean a ``align`` bytes (necesario en Windows y en
    ``/dev/rdisk*`` de macOS).  Si un bloque no se puede leer se reintenta
    sector a sector y los sectores dañados se devuelven como ceros y se
    anotan en :attr:`errors`.
    """

    def __init__(self, path: str, writable: bool = False, align: int = 4096) -> None:
        self.path = path
        self.name = os.path.basename(path) or path
        self.errors = ErrorLog()
        self._lock = threading.Lock()
        self.align = align
        mode = "r+b" if writable else "rb"
        self.f = open(path, mode, buffering=0)
        self.sector_size = SECTOR
        self.size = self._detect_size()

    def _detect_size(self) -> int:
        if sys.platform == "win32" and self.path.startswith("\\\\.\\"):
            try:
                return _windows_size(self.path)
            except OSError:
                pass
        if sys.platform == "darwin" and self.path.startswith("/dev/"):
            try:
                size, bs = _mac_size(self.f.fileno())
                self.sector_size = bs
                return size
            except OSError:
                pass
        try:
            self.f.seek(0, os.SEEK_END)
            size = self.f.tell()
            self.f.seek(0)
            if size:
                return size
        except OSError:
            pass
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def _raw(self, offset: int, length: int) -> bytes:
        self.f.seek(offset)
        out = bytearray()
        while len(out) < length:
            chunk = self.f.read(length - len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0 or offset >= self.size:
            return b""
        length = min(length, self.size - offset)
        a = self.align
        start = offset - (offset % a)
        end = offset + length
        end_al = end + ((-end) % a)
        end_al = min(end_al, self.size + ((-self.size) % a))
        with self._lock:
            try:
                data = self._raw(start, end_al - start)
            except OSError:
                data = self._read_tolerant(start, end_al - start)
        return data[offset - start: offset - start + length]

    def _read_tolerant(self, start: int, length: int) -> bytes:
        out = bytearray()
        ss = self.sector_size
        pos = start
        while pos < start + length:
            try:
                piece = self._raw(pos, ss)
                if not piece:
                    piece = b"\0" * ss
            except OSError:
                piece = b"\0" * ss
                self.errors.add(pos, ss)
            out += piece
            pos += ss
        return bytes(out)

    def write(self, offset: int, data: bytes) -> None:
        with self._lock:
            self.f.seek(offset)
            self.f.write(data)
            self.f.flush()

    def close(self) -> None:
        try:
            self.f.close()
        except Exception:
            pass

    def describe(self) -> dict:
        return {"tipo": "archivo", "ruta": self.path, "tamano": self.size}


class SliceSource(BlockSource):
    """Vista de una región de otra fuente (p. ej. una partición dentro de un disco)."""

    def __init__(self, parent: BlockSource, offset: int, size: int, name: str = "") -> None:
        self.parent = parent
        self.offset = offset
        self.size = max(0, min(size, parent.size - offset)) if parent.size else size
        self.sector_size = parent.sector_size
        self.name = name or f"{parent.name}@{offset}"

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        return self.parent.read(self.offset + offset, length)

    def describe(self) -> dict:
        return {"tipo": "region", "padre": self.parent.describe(), "desplazamiento": self.offset,
                "tamano": self.size}


class MemorySource(BlockSource):
    def __init__(self, data: bytes, name: str = "memoria") -> None:
        self.data = data
        self.size = len(data)
        self.name = name

    def read(self, offset: int, length: int) -> bytes:
        return self.data[offset: offset + length]


class SplitImageSource(BlockSource):
    """Imagen RAW dividida en partes (archivo.001, archivo.002 …)."""

    def __init__(self, first_path: str) -> None:
        base, ext = os.path.splitext(first_path)
        width = len(ext) - 1
        self.parts: List[FileSource] = []
        n = int(ext[1:])
        while True:
            p = f"{base}.{str(n).zfill(width)}"
            if not os.path.exists(p):
                break
            self.parts.append(FileSource(p))
            n += 1
        self.offsets = []
        acc = 0
        for p in self.parts:
            self.offsets.append(acc)
            acc += p.size
        self.size = acc
        self.name = os.path.basename(first_path)

    def read(self, offset: int, length: int) -> bytes:
        out = bytearray()
        for base, part in zip(self.offsets, self.parts):
            if offset + length <= base or offset >= base + part.size:
                continue
            s = max(offset, base)
            e = min(offset + length, base + part.size)
            out += part.read(s - base, e - s)
        return bytes(out)

    def close(self) -> None:
        for p in self.parts:
            p.close()


class VHDSource(BlockSource):
    """Discos virtuales VHD (fijos y dinámicos)."""

    def __init__(self, path: str) -> None:
        self.f = FileSource(path)
        self.name = os.path.basename(path)
        footer = self.f.read(self.f.size - 512, 512)
        if footer[:8] != b"conectix":
            footer = self.f.read(0, 512)
        if footer[:8] != b"conectix":
            raise ValueError("No es un VHD válido")
        self.size = struct.unpack(">Q", footer[48:56])[0]
        self.disk_type = struct.unpack(">I", footer[60:64])[0]
        if self.disk_type == 2:  # fijo
            self.dynamic = False
            return
        self.dynamic = True
        dyn_off = struct.unpack(">Q", footer[16:24])[0]
        hdr = self.f.read(dyn_off, 1024)
        if hdr[:8] != b"cxsparse":
            raise ValueError("Cabecera dinámica VHD inválida")
        bat_off = struct.unpack(">Q", hdr[16:24])[0]
        entries = struct.unpack(">I", hdr[28:32])[0]
        self.block_size = struct.unpack(">I", hdr[32:36])[0]
        raw = self.f.read(bat_off, entries * 4)
        self.bat = struct.unpack(f">{entries}I", raw)
        self.bitmap_size = ((self.block_size // 512 // 8) + 511) // 512 * 512

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self.size:
            return b""
        length = min(length, self.size - offset)
        if not self.dynamic:
            return self.f.read(offset, length)
        out = bytearray()
        while length > 0:
            blk = offset // self.block_size
            inner = offset % self.block_size
            n = min(length, self.block_size - inner)
            sec = self.bat[blk] if blk < len(self.bat) else 0xFFFFFFFF
            if sec == 0xFFFFFFFF:
                out += b"\0" * n
            else:
                out += self.f.read(sec * 512 + self.bitmap_size + inner, n).ljust(n, b"\0")
            offset += n
            length -= n
        return bytes(out)

    def close(self) -> None:
        self.f.close()


def open_image(path: str) -> BlockSource:
    """Abre una imagen según su extensión/contenido."""
    low = path.lower()
    from .ewf import EWFSource, is_ewf
    if is_ewf(path):
        return EWFSource(path)
    if re.search(r"\.(00)?1$", low) and os.path.exists(path):
        base = os.path.splitext(path)[0]
        if os.path.exists(base + ".002") or os.path.exists(base + ".02"):
            return SplitImageSource(path)
    if low.endswith(".vhd"):
        return VHDSource(path)
    if low.endswith(".rdmx"):
        from .raid import load_raid_definition
        return load_raid_definition(path)
    return FileSource(path)


# --------------------------------------------------------------------------- #
# Enumeración de dispositivos
# --------------------------------------------------------------------------- #
@dataclass
class DeviceInfo:
    path: str
    name: str
    size: int
    model: str = ""
    kind: str = "disco"          # disco | particion | imagen | raid | remoto
    removable: bool = False
    fs: str = ""
    mountpoint: str = ""
    children: List["DeviceInfo"] = field(default_factory=list)


def _read_text(p: str) -> str:
    try:
        with open(p) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _linux_mounts() -> dict:
    m = {}
    try:
        with open("/proc/self/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[0].startswith("/dev/"):
                    m[os.path.realpath(parts[0])] = (parts[1], parts[2])
    except OSError:
        pass
    return m


def list_linux() -> List[DeviceInfo]:
    out: List[DeviceInfo] = []
    mounts = _linux_mounts()
    base = "/sys/block"
    if not os.path.isdir(base):
        return out
    for dev in sorted(os.listdir(base)):
        if dev.startswith(("loop", "ram", "zram", "dm-", "sr", "fd")) and not dev.startswith("loop"):
            continue
        size = int(_read_text(f"{base}/{dev}/size") or 0) * 512
        if size == 0:
            continue
        vendor = _read_text(f"{base}/{dev}/device/vendor")
        model = _read_text(f"{base}/{dev}/device/model")
        if vendor.startswith("0x"):
            vendor = ""
        model = (vendor + " " + model).strip()
        info = DeviceInfo(path=f"/dev/{dev}", name=dev, size=size, model=model or dev,
                          removable=_read_text(f"{base}/{dev}/removable") == "1")
        for sub in sorted(os.listdir(f"{base}/{dev}")):
            if sub.startswith(dev) and os.path.exists(f"{base}/{dev}/{sub}/partition"):
                psize = int(_read_text(f"{base}/{dev}/{sub}/size") or 0) * 512
                mp, fs = mounts.get(f"/dev/{sub}", ("", ""))
                info.children.append(DeviceInfo(path=f"/dev/{sub}", name=sub, size=psize,
                                                kind="particion", fs=fs, mountpoint=mp))
        out.append(info)
    return out


def list_macos() -> List[DeviceInfo]:
    import plistlib
    import subprocess
    out: List[DeviceInfo] = []
    try:
        raw = subprocess.run(["diskutil", "list", "-plist"], capture_output=True, timeout=20).stdout
        pl = plistlib.loads(raw)
    except Exception:
        return out
    for d in pl.get("AllDisksAndPartitions", []):
        ident = d.get("DeviceIdentifier", "")
        info = DeviceInfo(path=f"/dev/r{ident}", name=ident, size=d.get("Size", 0),
                          model=d.get("Content", ""))
        for p in d.get("Partitions", []) + d.get("APFSVolumes", []):
            pid = p.get("DeviceIdentifier", "")
            info.children.append(DeviceInfo(path=f"/dev/r{pid}", name=pid, size=p.get("Size", 0),
                                            kind="particion", fs=p.get("Content", ""),
                                            mountpoint=p.get("MountPoint", "")))
        out.append(info)
    return out


def list_windows() -> List[DeviceInfo]:
    import json
    import subprocess
    out: List[DeviceInfo] = []
    ps = ("Get-Disk | Select Number,FriendlyName,Size,BusType | ConvertTo-Json -Compress;"
          "'@@';Get-Partition | Select DiskNumber,PartitionNumber,DriveLetter,Size,Offset |"
          " ConvertTo-Json -Compress")
    disks, parts = [], []
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, timeout=30, creationflags=0x08000000)
        a, _, b = res.stdout.partition("@@")
        disks = json.loads(a.strip() or "[]")
        parts = json.loads(b.strip() or "[]")
        disks = disks if isinstance(disks, list) else [disks]
        parts = parts if isinstance(parts, list) else [parts]
    except Exception:
        for n in range(16):
            p = f"\\\\.\\PhysicalDrive{n}"
            try:
                sz = _windows_size(p)
                out.append(DeviceInfo(path=p, name=f"PhysicalDrive{n}", size=sz))
            except OSError:
                continue
        return out
    for d in disks:
        n = d.get("Number")
        info = DeviceInfo(path=f"\\\\.\\PhysicalDrive{n}", name=f"Disco {n}",
                          size=int(d.get("Size") or 0), model=d.get("FriendlyName") or "",
                          removable=str(d.get("BusType")) in ("7", "USB", "SD"))
        for p in parts:
            if p.get("DiskNumber") != n:
                continue
            letter = (p.get("DriveLetter") or "").strip("\0 ")
            path = f"\\\\.\\{letter}:" if letter else info.path
            info.children.append(DeviceInfo(path=path, name=f"Partición {p.get('PartitionNumber')}"
                                            + (f" ({letter}:)" if letter else ""),
                                            size=int(p.get("Size") or 0), kind="particion",
                                            mountpoint=f"{letter}:\\" if letter else "",
                                            fs=str(p.get("Offset") or "")))
        out.append(info)
    return out


def list_devices() -> List[DeviceInfo]:
    if sys.platform.startswith("linux"):
        return list_linux()
    if sys.platform == "darwin":
        return list_macos()
    if sys.platform == "win32":
        return list_windows()
    return []


def is_admin() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return str(n)
