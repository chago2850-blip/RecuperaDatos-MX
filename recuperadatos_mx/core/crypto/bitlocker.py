"""BitLocker (Windows Vista a 11, BitLocker To Go): desbloqueo con contraseña,
clave de recuperación de 48 dígitos o archivo .BEK, y lectura descifrada.

Métodos: AES-XTS 128/256, AES-CBC 128/256 y AES-CBC con difusor Elephant (Windows 7)."""
from __future__ import annotations

import hashlib
import re
import struct
from typing import List, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from ..device import BlockSource

SIG = b"-FVE-FS-"
METHODS = {0x8000: "AES-128-CBC + difusor", 0x8001: "AES-256-CBC + difusor", 0x8002: "AES-128-CBC",
           0x8003: "AES-256-CBC", 0x8004: "AES-128-XTS", 0x8005: "AES-256-XTS"}


def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]


def is_bitlocker(src: BlockSource, base: int = 0) -> bool:
    return src.read(base + 3, 8) == SIG


def _entries(data: bytes, p: int, end: int) -> List[Tuple[int, int, bytes]]:
    out = []
    while p + 8 <= end:
        size, etype, vtype = u16(data, p), u16(data, p + 2), u16(data, p + 4)
        if size < 8 or p + size > end:
            break
        out.append((etype, vtype, data[p + 8:p + size]))
        p += size
    return out


def _stretch(initial: bytes, salt: bytes) -> bytes:
    last = b"\0" * 32
    for i in range(0x100000):
        last = hashlib.sha256(last + initial + salt + struct.pack("<Q", i)).digest()
    return last


def password_key(pw: str, salt: bytes) -> bytes:
    return _stretch(hashlib.sha256(hashlib.sha256(pw.encode("utf-16-le")).digest()).digest(), salt)


def recovery_key(rp: str, salt: bytes) -> Optional[bytes]:
    groups = re.findall(r"\d{6}", rp)
    if len(groups) != 8:
        return None
    raw = b""
    for g in groups:
        v = int(g)
        if v % 11 or v // 11 > 0xFFFF:
            return None
        raw += struct.pack("<H", v // 11)
    return _stretch(hashlib.sha256(raw).digest(), salt)


def _ccm(key: bytes, payload: bytes) -> Optional[bytes]:
    nonce, mac, ct = payload[:12], payload[12:28], payload[28:]
    try:
        return AESCCM(key, tag_length=16).decrypt(nonce, ct + mac, None)
    except Exception:
        return None


def _diffuser_a_dec(d: List[int]) -> None:
    n = len(d)
    ra = (9, 0, 13, 0)
    for _ in range(5):
        for i in range(n - 1, -1, -1):
            d[i] = (d[i] + (d[(i - 2) % n] ^ _rotl(d[(i - 5) % n], ra[i % 4]))) & 0xFFFFFFFF


def _diffuser_b_dec(d: List[int]) -> None:
    n = len(d)
    rb = (0, 10, 0, 25)
    for _ in range(3):
        for i in range(n - 1, -1, -1):
            d[i] = (d[i] + (d[(i + 2) % n] ^ _rotl(d[(i + 5) % n], rb[i % 4]))) & 0xFFFFFFFF


def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & 0xFFFFFFFF if r else x


class BitLockerVolume:
    def __init__(self, src: BlockSource, base: int = 0, size: Optional[int] = None) -> None:
        self.src, self.base = src, base
        boot = src.read(base, 512)
        if boot[3:11] != SIG:
            raise ValueError("No es un volumen BitLocker")
        self.size = size or (src.size - base)
        offs = struct.unpack_from("<3Q", boot, 176)
        self.meta = None
        for o in offs:
            blk = src.read(base + o, 64)
            if blk[:8] == SIG:
                self.meta_off = o
                self._parse(o)
                break
        if self.meta is None:
            raise ValueError("No se encontró el bloque de metadatos FVE")

    def _parse(self, off: int) -> None:
        hdr = self.src.read(self.base + off, 64 + 48)
        self.version = u16(hdr, 10)
        self.encrypted_size = u64(hdr, 16)
        self.fve_offsets = struct.unpack_from("<3Q", hdr, 32)
        self.header_offset = u64(hdr, 56)
        msize = u32(hdr, 64)
        self.method = u16(hdr, 64 + 36)
        data = self.src.read(self.base + off + 64, msize)
        self.meta = data
        self.entries = _entries(data, 48, msize)
        self.header_sectors = u32(hdr, 28) if self.version == 2 else 0
        for et, vt, d in self.entries:
            if vt == 0x000F and et == 0x000F and len(d) >= 16:  # bloque de cabecera del volumen
                self.header_offset, self.header_size = u64(d, 0), u64(d, 8)
        if not hasattr(self, "header_size"):
            self.header_size = (self.header_sectors or 16) * 512
        self.description = next((d.decode("utf-16-le", "ignore").rstrip("\0") for et, vt, d in self.entries
                                 if vt == 0x0002), "")

    def protectors(self) -> List[str]:
        names = {0x0000: "Clave sin protección", 0x0100: "TPM", 0x0200: "Clave de inicio (.BEK)",
                 0x0500: "TPM + PIN", 0x0800: "Contraseña de recuperación", 0x2000: "Contraseña"}
        return [names.get(u16(d, 26), hex(u16(d, 26))) for et, vt, d in self.entries if vt == 0x0008]

    def _vmk(self, secret: str = "", bek: Optional[bytes] = None) -> Optional[bytes]:
        for et, vt, d in self.entries:
            if vt != 0x0008 or len(d) < 28:
                continue
            ptype = u16(d, 26)
            props = _entries(d, 28, len(d))
            stretch = next((p for e, v, p in props if v == 0x0003), None)
            ccm = next((p for e, v, p in props if v == 0x0005), None)
            if ccm is None:
                continue
            key = None
            if ptype == 0x0000:  # clave en claro (BitLocker suspendido)
                k = next((p for e, v, p in props if v == 0x0001), None)
                key = k[4:] if k else None
            elif ptype == 0x2000 and secret and stretch:
                key = password_key(secret, stretch[4:20])
            elif ptype == 0x0800 and secret and stretch:
                key = recovery_key(secret, stretch[4:20])
            elif ptype == 0x0200 and bek:
                key = self._bek_key(bek, d[:16])
            if key is None:
                continue
            plain = _ccm(key, ccm)
            if plain and len(plain) >= 44:
                return plain[12:44]
        return None

    @staticmethod
    def _bek_key(bek: bytes, guid: bytes) -> Optional[bytes]:
        hl = u32(bek, 8) if len(bek) > 12 else 48
        for et, vt, d in _entries(bek, hl, len(bek)):
            if vt == 0x0009 and d[:16] == guid:
                for e2, v2, k in _entries(d, 24, len(d)):
                    if v2 == 0x0001:
                        return k[4:36]
        return None

    def unlock(self, secret: str = "", bek: Optional[bytes] = None) -> Optional[bytes]:
        vmk = self._vmk(secret.strip(), bek)
        if vmk is None:
            return None
        for et, vt, d in self.entries:
            if et == 0x0003 and vt == 0x0005:
                plain = _ccm(vmk, d)
                if plain and len(plain) >= 28:
                    return plain[12:]
        return None

    def open(self, secret: str = "", bek: Optional[bytes] = None) -> "BitLockerSource":
        fvek = self.unlock(secret, bek)
        if fvek is None:
            raise PermissionError("Contraseña o clave de recuperación incorrecta")
        return BitLockerSource(self, fvek)


class BitLockerSource(BlockSource):
    sector_size = 512

    def __init__(self, vol: BitLockerVolume, fvek: bytes) -> None:
        self.v = vol
        self.src = vol.src
        self.size = vol.size
        self.name = f"BitLocker descifrado ({vol.src.name})"
        m = vol.method
        if m in (0x8004, 0x8005):
            self.kind = "xts"
            self.key = fvek[:32] if m == 0x8004 else fvek[:64]
        elif m in (0x8002, 0x8003):
            self.kind = "cbc"
            self.key = fvek[:16] if m == 0x8002 else fvek[:32]
        else:
            self.kind = "elephant"
            ks = 16 if m == 0x8000 else 32
            self.key, self.tweak_key = fvek[:ks], fvek[32:32 + ks]
        self.ecb = Cipher(algorithms.AES(self.key), modes.ECB()).encryptor()
        self.fve_ranges = [(o, 64 * 1024) for o in vol.fve_offsets if o]
        self._cache = (-1, b"")

    def _dec_sector(self, data: bytes, off: int) -> bytes:
        """``off`` = posición física del sector dentro del volumen."""
        if self.v.encrypted_size and off >= self.v.encrypted_size:
            return data
        n = off // 512
        if self.kind == "xts":
            d = Cipher(algorithms.AES(self.key), modes.XTS(struct.pack("<Q", n) + b"\0" * 8)).decryptor()
            return d.update(data) + d.finalize()
        iv = self.ecb.update(struct.pack("<QQ", off, 0))
        d = Cipher(algorithms.AES(self.key), modes.CBC(iv)).decryptor()
        plain = d.update(data) + d.finalize()
        if self.kind == "cbc":
            return plain
        e = Cipher(algorithms.AES(self.tweak_key), modes.ECB()).encryptor()
        sk = e.update(struct.pack("<QQ", off, 0)) + e.update(struct.pack("<QQ", off, 0x80))
        sk = sk * (512 // 32)
        w = list(struct.unpack("<128I", plain))
        _diffuser_b_dec(w)
        _diffuser_a_dec(w)
        mixed = struct.pack("<128I", *w)
        return bytes(a ^ b for a, b in zip(mixed, sk))

    def _phys(self, off: int) -> int:
        if off < self.v.header_size:
            return self.v.header_offset + off
        return off

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        start = offset - offset % 512
        end = offset + length
        co, cd = self._cache
        if co <= start and end <= co + len(cd):
            return cd[offset - co: offset - co + length]
        span = max(end - start, 1 << 20)
        span = min(span + ((-span) % 512), self.size - start)
        out = bytearray()
        pos = start
        while pos < start + span:
            phys = self._phys(pos)
            n = min(start + span - pos, (self.v.header_size - pos) if pos < self.v.header_size else 1 << 30)
            raw = self.src.read(self.v.base + phys, n)
            for i in range(0, len(raw), 512):
                p = pos + i
                if any(o <= p < o + l for o, l in self.fve_ranges):
                    out += b"\0" * 512
                else:
                    out += self._dec_sector(raw[i:i + 512], phys + i)
            pos += n
        data = bytes(out)
        self._cache = (start, data)
        return data[offset - start: offset - start + length]

    def describe(self) -> dict:
        return {"tipo": "cifrado", "nombre": self.name, "padre": self.src.describe(), "tamano": self.size}
