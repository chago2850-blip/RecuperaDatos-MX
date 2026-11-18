"""Respaldos de iPhone/iPad (Finder, iTunes, libimobiledevice): listado, extracción
con rutas originales (fotos, WhatsApp, notas, mensajes…) y descifrado de respaldos
cifrados con la contraseña del respaldo.

También puede crear un respaldo nuevo del dispositivo conectado si está instalado
``idevicebackup2`` (libimobiledevice)."""
from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from typing import Dict, Iterator, List, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap

INTERESTING = {
    "Fotos y videos (Carrete)": ("CameraRollDomain", "Media/DCIM/"),
    "Mensajes (sms.db)": ("HomeDomain", "Library/SMS/sms.db"),
    "Adjuntos de Mensajes": ("MediaDomain", "Library/SMS/Attachments/"),
    "WhatsApp": ("AppDomainGroup-group.net.whatsapp.WhatsApp.shared", ""),
    "Notas": ("AppDomainGroup-group.com.apple.notes", ""),
    "Contactos": ("HomeDomain", "Library/AddressBook/"),
    "Historial de llamadas": ("HomeDomain", "Library/CallHistoryDB/"),
    "Notas de voz": ("MediaDomain", "Media/Recordings/"),
    "Safari": ("HomeDomain", "Library/Safari/"),
}


def backup_roots() -> List[str]:
    c = []
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        c.append(os.path.join(home, "Library", "Application Support", "MobileSync", "Backup"))
    elif sys.platform == "win32":
        c.append(os.path.join(os.environ.get("APPDATA", ""), "Apple Computer", "MobileSync", "Backup"))
        c.append(os.path.join(home, "Apple", "MobileSync", "Backup"))
        c.append(os.path.join(home, "Apple Computer", "MobileSync", "Backup"))
    c.append(os.path.join(home, ".local", "share", "RecuperaDatos-MX", "iphone"))
    return [p for p in c if os.path.isdir(p)]


def list_backups(extra: Optional[List[str]] = None) -> List[dict]:
    out = []
    for root in backup_roots() + list(extra or []):
        dirs = [root] if os.path.exists(os.path.join(root, "Manifest.plist")) else \
            [os.path.join(root, d) for d in os.listdir(root)]
        for d in dirs:
            mp = os.path.join(d, "Manifest.plist")
            if not os.path.exists(mp):
                continue
            try:
                m = plistlib.load(open(mp, "rb"))
            except Exception:
                continue
            info = {}
            ip = os.path.join(d, "Info.plist")
            if os.path.exists(ip):
                try:
                    info = plistlib.load(open(ip, "rb"))
                except Exception:
                    pass
            out.append({"ruta": d, "cifrado": bool(m.get("IsEncrypted")), "nombre": info.get("Device Name", ""),
                        "modelo": info.get("Product Type", ""), "ios": info.get("Product Version", ""),
                        "fecha": str(info.get("Last Backup Date", "")), "udid": os.path.basename(d)})
    return out


# --------------------------------------------------------------------------- #
class Keybag:
    def __init__(self, data: bytes) -> None:
        self.attrs: Dict[str, bytes] = {}
        self.classes: Dict[int, Dict[str, bytes]] = {}
        cur = None
        p = 0
        while p + 8 <= len(data):
            tag = data[p:p + 4].decode("ascii", "replace")
            ln = struct.unpack(">I", data[p + 4:p + 8])[0]
            val = data[p + 8:p + 8 + ln]
            p += 8 + ln
            if tag == "UUID" and cur is None and "UUID" in self.attrs:
                cur = {"UUID": val}
                continue
            if tag == "UUID" and cur is not None:
                if "CLAS" in cur:
                    self.classes[struct.unpack(">I", cur["CLAS"])[0]] = cur
                cur = {"UUID": val}
                continue
            if cur is None:
                self.attrs[tag] = val
            else:
                cur[tag] = val
        if cur and "CLAS" in cur:
            self.classes[struct.unpack(">I", cur["CLAS"])[0]] = cur
        self.keys: Dict[int, bytes] = {}

    def unlock(self, password: str) -> bool:
        pw = password.encode("utf-8")
        a = self.attrs
        if "DPSL" in a:
            pw = hashlib.pbkdf2_hmac("sha256", pw, a["DPSL"], struct.unpack(">I", a["DPIC"])[0], 32)
        key = hashlib.pbkdf2_hmac("sha1", pw, a["SALT"], struct.unpack(">I", a["ITER"])[0], 32)
        ok = False
        for cls, c in self.classes.items():
            if "WPKY" not in c:
                continue
            wrap = struct.unpack(">I", c.get("WRAP", b"\0\0\0\0"))[0]
            if wrap & 2:
                try:
                    self.keys[cls] = aes_key_unwrap(key, c["WPKY"])
                    ok = True
                except (InvalidUnwrap, ValueError):
                    return False
        return ok

    def unwrap(self, cls: int, wrapped: bytes) -> Optional[bytes]:
        k = self.keys.get(cls)
        if not k:
            return None
        try:
            return aes_key_unwrap(k, wrapped)
        except (InvalidUnwrap, ValueError):
            return None


def _aes_cbc_decrypt(key: bytes, data: bytes) -> bytes:
    data = data[:len(data) - len(data) % 16]
    d = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
    return d.update(data) + d.finalize()


def _unpad(b: bytes) -> bytes:
    if b and 1 <= b[-1] <= 16 and b.endswith(bytes([b[-1]]) * b[-1]):
        return b[:-b[-1]]
    return b


def _archived(blob: bytes) -> dict:
    """Decodifica el plist NSKeyedArchiver de la columna ``file`` de Manifest.db."""
    try:
        pl = plistlib.loads(blob)
    except Exception:
        return {}
    objs = pl.get("$objects", [])
    top = pl.get("$top", {}).get("root")
    root = objs[top.data if hasattr(top, "data") else top] if objs else {}
    out = {}
    for k, v in (root.items() if isinstance(root, dict) else []):
        if hasattr(v, "data") and isinstance(v.data, int) and v.data < len(objs):
            v = objs[v.data]
        if isinstance(v, dict) and "NS.data" in v:
            v = v["NS.data"]
        out[k] = v
    return out


class Backup:
    def __init__(self, path: str, password: str = "") -> None:
        self.path = path
        self.manifest = plistlib.load(open(os.path.join(path, "Manifest.plist"), "rb"))
        self.encrypted = bool(self.manifest.get("IsEncrypted"))
        self.keybag = None
        db_path = os.path.join(path, "Manifest.db")
        if self.encrypted:
            if not password:
                raise PermissionError("El respaldo está cifrado: indique la contraseña del respaldo")
            self.keybag = Keybag(self.manifest["BackupKeyBag"])
            if not self.keybag.unlock(password):
                raise PermissionError("Contraseña del respaldo incorrecta")
            mk = self.manifest["ManifestKey"]
            cls = struct.unpack("<I", mk[:4])[0]
            key = self.keybag.unwrap(cls, mk[4:])
            if not key:
                raise PermissionError("No se pudo descifrar Manifest.db")
            plain = _aes_cbc_decrypt(key, open(db_path, "rb").read())
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.write(_unpad(plain))
            tmp.close()
            db_path = tmp.name
        self.db = sqlite3.connect(db_path)

    def files(self, domain_like: str = "", path_like: str = "") -> Iterator[Tuple[str, str, str, int, bytes]]:
        q = "SELECT fileID, domain, relativePath, flags, file FROM Files WHERE flags = 1"
        args = []
        if domain_like:
            q += " AND domain LIKE ?"
            args.append(domain_like + "%")
        if path_like:
            q += " AND relativePath LIKE ?"
            args.append(path_like + "%")
        yield from self.db.execute(q, args)

    def read(self, file_id: str, blob: bytes) -> bytes:
        p = os.path.join(self.path, file_id[:2], file_id)
        if not os.path.exists(p):
            p = os.path.join(self.path, file_id)
        data = open(p, "rb").read()
        if not self.encrypted:
            return data
        meta = _archived(blob)
        ek = meta.get("EncryptionKey")
        cls = meta.get("ProtectionClass")
        if not ek or cls is None:
            return data
        key = self.keybag.unwrap(int(cls), bytes(ek)[4:])
        if not key:
            return b""
        plain = _unpad(_aes_cbc_decrypt(key, data))
        size = meta.get("Size")
        return plain[:size] if isinstance(size, int) and size else plain

    def extract(self, dest: str, domain_like: str = "", path_like: str = "", progress=None) -> int:
        n = 0
        rows = list(self.files(domain_like, path_like))
        for i, (fid, dom, rel, flags, blob) in enumerate(rows):
            out = os.path.join(dest, dom.replace("/", "_"), *rel.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            try:
                with open(out, "wb") as fh:
                    fh.write(self.read(fid, blob))
                n += 1
            except OSError:
                continue
            if progress:
                progress(i + 1, len(rows), rel)
        return n


def make_backup(dest: str) -> Tuple[bool, str]:
    exe = shutil.which("idevicebackup2")
    if not exe:
        return False, ("No se encontró idevicebackup2. En Linux instale «libimobiledevice-utils»; en macOS "
                       "«brew install libimobiledevice»; o cree el respaldo con Finder/iTunes y ábralo aquí.")
    os.makedirs(dest, exist_ok=True)
    r = subprocess.run([exe, "backup", "--full", dest], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr)[-2000:]
