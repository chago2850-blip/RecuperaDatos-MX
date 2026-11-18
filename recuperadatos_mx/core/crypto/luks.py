"""LUKS1 y LUKS2 (Linux): desbloqueo con contraseña y lectura descifrada.

Soporta PBKDF2 y Argon2i/Argon2id, AES-XTS (plain/plain64) y AES-CBC
(plain/plain64/essiv:sha256), sectores de 512 a 4096 bytes."""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from typing import Optional

from ..device import BlockSource
from .common import DecryptedSource, sector_cipher

MAGIC = b"LUKS\xba\xbe"
MAGIC2 = b"SKUL\xba\xbe"


def is_luks(src: BlockSource, base: int = 0) -> bool:
    return src.read(base, 6) == MAGIC


def _af_merge(data: bytes, key_bytes: int, stripes: int, hname: str) -> bytes:
    def diffuse(buf: bytes) -> bytes:
        hl = hashlib.new(hname).digest_size
        out = bytearray()
        for i in range((len(buf) + hl - 1) // hl):
            blk = buf[i * hl:(i + 1) * hl]
            out += hashlib.new(hname, struct.pack(">I", i) + blk).digest()[:len(blk)]
        return bytes(out)

    d = bytes(key_bytes)
    for i in range(stripes - 1):
        blk = data[i * key_bytes:(i + 1) * key_bytes]
        d = diffuse(bytes(a ^ b for a, b in zip(d, blk)))
    last = data[(stripes - 1) * key_bytes: stripes * key_bytes]
    return bytes(a ^ b for a, b in zip(d, last))


def _decrypt_area(src: BlockSource, off: int, length: int, cipher: str, key: bytes) -> bytes:
    dec = sector_cipher(cipher, key, 512)
    raw = src.read(off, length)
    out = bytearray()
    for i in range(0, len(raw), 512):
        out += dec(i // 512, raw[i:i + 512])
    return bytes(out)


def _kdf(kdf: dict, password: bytes, length: int) -> bytes:
    t = kdf["type"]
    salt = base64.b64decode(kdf["salt"])
    if t == "pbkdf2":
        return hashlib.pbkdf2_hmac(kdf["hash"], password, salt, kdf["iterations"], length)
    from argon2.low_level import Type, hash_secret_raw
    return hash_secret_raw(password, salt, kdf["time"], kdf["memory"], kdf["cpus"], length,
                           Type.ID if t == "argon2id" else Type.I)


class LUKSVolume:
    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src, self.base = src, base
        h = src.read(base, 4096)
        if h[:6] != MAGIC:
            raise ValueError("No es un volumen LUKS")
        self.version = struct.unpack_from(">H", h, 6)[0]
        if self.version == 1:
            c = lambda o, n: h[o:o + n].split(b"\0")[0].decode()
            self.cipher = c(8, 32) + "-" + c(40, 32)
            self.hash = c(72, 32)
            self.payload = struct.unpack_from(">I", h, 104)[0] * 512
            self.key_bytes = struct.unpack_from(">I", h, 108)[0]
            self.digest = h[112:132]
            self.dsalt = h[132:164]
            self.diter = struct.unpack_from(">I", h, 164)[0]
            self.slots = []
            for i in range(8):
                o = 208 + i * 48
                active, it = struct.unpack_from(">II", h, o)
                if active == 0x00AC71F3:
                    km, stripes = struct.unpack_from(">II", h, o + 40)
                    self.slots.append({"iter": it, "salt": h[o + 8:o + 40], "off": km * 512, "stripes": stripes})
            self.sector = 512
            self.iv_shift = 0
            self.uuid = c(168, 40)
        else:
            hsize = struct.unpack_from(">Q", h, 8)[0]
            meta = src.read(base + 4096, hsize - 4096).split(b"\0")[0]
            self.j = json.loads(meta)
            seg = self.j["segments"]["0"]
            self.cipher = seg["encryption"]
            self.payload = int(seg["offset"])
            self.sector = int(seg.get("sector_size", 512))
            self.iv_tweak = int(seg.get("iv_tweak", 0))
            self.uuid = h[168:208].split(b"\0")[0].decode()

    def unlock(self, password: str) -> Optional[bytes]:
        pw = password.encode()
        if self.version == 1:
            for s in self.slots:
                k = hashlib.pbkdf2_hmac(self.hash, pw, s["salt"], s["iter"], self.key_bytes)
                mat = _decrypt_area(self.src, self.base + s["off"], self.key_bytes * s["stripes"], self.cipher, k)
                mk = _af_merge(mat, self.key_bytes, s["stripes"], self.hash)
                if hashlib.pbkdf2_hmac(self.hash, mk, self.dsalt, self.diter, 20) == self.digest:
                    return mk
            return None
        for sid, ks in self.j["keyslots"].items():
            if ks.get("type") != "luks2":
                continue
            area = ks["area"]
            kb = int(ks["key_size"])
            k = _kdf(ks["kdf"], pw, int(area["key_size"]))
            stripes = ks["af"]["stripes"]
            mat = _decrypt_area(self.src, self.base + int(area["offset"]), kb * stripes, area["encryption"], k)
            mk = _af_merge(mat, kb, stripes, ks["af"]["hash"])
            for d in self.j["digests"].values():
                if sid in d["keyslots"]:
                    want = base64.b64decode(d["digest"])
                    got = hashlib.pbkdf2_hmac(d["hash"], mk, base64.b64decode(d["salt"]), d["iterations"], len(want))
                    if got == want:
                        return mk
        return None

    def open(self, password: str) -> "DecryptedSource":
        mk = self.unlock(password)
        if mk is None:
            raise PermissionError("Contraseña incorrecta")
        return self.open_with_key(mk)

    def open_with_key(self, mk: bytes) -> "DecryptedSource":
        size = self.src.size - self.base - self.payload
        # dm-crypt cuenta el IV en unidades de 512 bytes (salvo la opción iv_large_sectors)
        large = "iv_large_sectors" in str(getattr(self, "j", {}).get("config", {}))
        iv_div = 1 if large else self.sector // 512
        dec = sector_cipher(self.cipher, mk, self.sector)
        tweak = getattr(self, "iv_tweak", 0)
        return DecryptedSource(self.src, self.base + self.payload, size, self.sector,
                               lambda n, data: dec(n * iv_div + tweak, data),
                               name=f"LUKS{self.version} descifrado ({self.src.name})")
