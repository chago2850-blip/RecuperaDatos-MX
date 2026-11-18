"""Utilidades criptográficas comunes: cifrado por sector y fuente descifrada."""
from __future__ import annotations

import hashlib
import struct
import threading
from typing import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..device import BlockSource


def sector_cipher(spec: str, key: bytes, sector_size: int) -> Callable[[int, bytes], bytes]:
    """Devuelve ``decrypt(numero_de_sector, datos)`` para una especificación estilo dm-crypt
    (``aes-xts-plain64``, ``aes-cbc-essiv:sha256``, ``aes-cbc-plain64``…)."""
    parts = spec.lower().split("-", 2)
    algo, mode = parts[0], parts[1] if len(parts) > 1 else "xts"
    ivgen = parts[2] if len(parts) > 2 else "plain64"
    if algo != "aes":
        raise NotImplementedError(f"Cifrado {algo} no soportado (solo AES)")
    if mode == "xts":
        def dec(n: int, data: bytes) -> bytes:
            iv = struct.pack("<Q", n & (0xFFFFFFFF if ivgen == "plain" else 0xFFFFFFFFFFFFFFFF)) + b"\0" * 8
            d = Cipher(algorithms.AES(key), modes.XTS(iv)).decryptor()
            return d.update(data) + d.finalize()
        return dec
    if mode == "cbc":
        essiv = None
        if ivgen.startswith("essiv"):
            h = ivgen.split(":")[1] if ":" in ivgen else "sha256"
            salt = hashlib.new(h, key).digest()
            essiv = Cipher(algorithms.AES(salt), modes.ECB()).encryptor()

        def dec(n: int, data: bytes) -> bytes:
            iv = struct.pack("<Q", n) + b"\0" * 8
            if ivgen == "plain":
                iv = struct.pack("<I", n & 0xFFFFFFFF) + b"\0" * 12
            if essiv is not None:
                iv = essiv.update(iv)
            d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            return d.update(data) + d.finalize()
        return dec
    raise NotImplementedError(f"Modo {mode} no soportado")


class DecryptedSource(BlockSource):
    """Vista descifrada de un volumen cifrado por sectores."""

    def __init__(self, parent: BlockSource, offset: int, size: int, sector: int,
                 decrypt: Callable[[int, bytes], bytes], name: str = "volumen descifrado") -> None:
        self.parent, self.offset, self.size = parent, offset, max(0, size - size % sector)
        self.sector_size = 512
        self.unit = sector
        self.decrypt = decrypt
        self.name = name
        self._lock = threading.Lock()
        self._cache = (-1, b"")

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        u = self.unit
        first = offset // u
        last = (offset + length - 1) // u
        co, cd = self._cache
        if co >= 0 and co <= first and last < co + len(cd) // u:
            s = offset - co * u
            return cd[s:s + length]
        n_units = max(last - first + 1, (1 << 20) // u)
        n_units = min(n_units, (self.size - first * u) // u)
        raw = self.parent.read(self.offset + first * u, n_units * u)
        out = bytearray()
        for i in range(0, len(raw) // u):
            out += self.decrypt(first + i, raw[i * u:(i + 1) * u])
        data = bytes(out)
        with self._lock:
            self._cache = (first, data)
        s = offset - first * u
        return data[s:s + length]

    def describe(self) -> dict:
        return {"tipo": "cifrado", "nombre": self.name, "padre": self.parent.describe(), "tamano": self.size}


def iter_apfs_encrypted(src: BlockSource, entry, remaining: int):
    """Contenido de un archivo de un volumen APFS cifrado (AES-XTS, unidades de 512 bytes)."""
    vek = bytes.fromhex(entry.codec_args["vek"])
    tweaks = entry.codec_args.get("tweaks", [])
    for (off, ln), tw in zip(entry.extents, tweaks):
        if remaining <= 0:
            break
        n = min(ln, remaining)
        if off < 0:
            yield b"\0" * n
            remaining -= n
            continue
        pos = 0
        while pos < n:
            k = min(1 << 20, n - pos)
            k_al = (k + 511) // 512 * 512
            raw = src.read(off + pos, k_al).ljust(k_al, b"\0")
            out = bytearray()
            for i in range(0, k_al, 512):
                t = struct.pack("<Q", tw + (pos + i) // 512) + b"\0" * 8
                d = Cipher(algorithms.AES(vek), modes.XTS(t)).decryptor()
                out += d.update(raw[i:i + 512]) + d.finalize()
            yield bytes(out[:k])
            pos += k
        remaining -= n
