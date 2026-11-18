"""Pruebas de respaldos de iPhone (normales y cifrados) y recuperación SQLite."""
import hashlib
import os
import plistlib
import sqlite3
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.hazmat.primitives import padding  # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402
from cryptography.hazmat.primitives.keywrap import aes_key_wrap  # noqa: E402

from recuperadatos_mx.core.mobile.iphone import Backup, list_backups  # noqa: E402
from recuperadatos_mx.core.mobile.sqlite_recover import SQLiteRecovery  # noqa: E402


def _tlv(tag, val):
    return tag.encode() + struct.pack(">I", len(val)) + val


def _cbc(key, data):
    p = padding.PKCS7(128).padder()
    data = p.update(data) + p.finalize()
    e = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).encryptor()
    return e.update(data) + e.finalize()


def _make_backup(root, encrypted, password="secreto"):
    os.makedirs(root, exist_ok=True)
    files = {"Media/DCIM/100APPLE/IMG_0001.JPG": os.urandom(5000), "Library/SMS/sms.db": b"SQLite format 3\0" + os.urandom(900)}
    db = sqlite3.connect(os.path.join(root, "Manifest.db.plain"))
    db.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")
    manifest = {"IsEncrypted": encrypted, "Version": "10.0"}
    class_key = os.urandom(32)
    if encrypted:
        salt, dpsl = os.urandom(20), os.urandom(20)
        pk = hashlib.pbkdf2_hmac("sha256", password.encode(), dpsl, 1000, 32)
        key = hashlib.pbkdf2_hmac("sha1", pk, salt, 100, 32)
        kb = (_tlv("VERS", struct.pack(">I", 4)) + _tlv("TYPE", struct.pack(">I", 1)) + _tlv("UUID", os.urandom(16))
              + _tlv("HMCK", os.urandom(40)) + _tlv("WRAP", struct.pack(">I", 1)) + _tlv("SALT", salt)
              + _tlv("ITER", struct.pack(">I", 100)) + _tlv("DPWT", struct.pack(">I", 1))
              + _tlv("DPIC", struct.pack(">I", 1000)) + _tlv("DPSL", dpsl)
              + _tlv("UUID", os.urandom(16)) + _tlv("CLAS", struct.pack(">I", 3)) + _tlv("WRAP", struct.pack(">I", 2))
              + _tlv("KTYP", struct.pack(">I", 0)) + _tlv("WPKY", aes_key_wrap(key, class_key)))
        manifest["BackupKeyBag"] = kb
        dbkey = os.urandom(32)
        manifest["ManifestKey"] = struct.pack("<I", 3) + aes_key_wrap(class_key, dbkey)
    for rel, data in files.items():
        fid = hashlib.sha1(("CameraRollDomain-" + rel).encode()).hexdigest()
        blob = b""
        payload = data
        if encrypted:
            fkey = os.urandom(32)
            payload = _cbc(fkey, data)
            arch = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": plistlib.UID(1)},
                    "$objects": ["$null", {"EncryptionKey": plistlib.UID(2), "ProtectionClass": 3, "Size": len(data)},
                                 {"NS.data": struct.pack("<I", 3) + aes_key_wrap(class_key, fkey)}]}
            blob = plistlib.dumps(arch, fmt=plistlib.FMT_BINARY)
        os.makedirs(os.path.join(root, fid[:2]), exist_ok=True)
        open(os.path.join(root, fid[:2], fid), "wb").write(payload)
        db.execute("INSERT INTO Files VALUES (?,?,?,?,?)", (fid, "CameraRollDomain", rel, 1, blob))
    db.commit()
    db.close()
    plain = open(os.path.join(root, "Manifest.db.plain"), "rb").read()
    os.remove(os.path.join(root, "Manifest.db.plain"))
    open(os.path.join(root, "Manifest.db"), "wb").write(_cbc(dbkey, plain) if encrypted else plain)
    plistlib.dump(manifest, open(os.path.join(root, "Manifest.plist"), "wb"))
    plistlib.dump({"Device Name": "iPhone de prueba", "Product Version": "18.1"}, open(os.path.join(root, "Info.plist"), "wb"))
    return files


def test_iphone_backup_plain_and_encrypted(tmp_path):
    for enc in (False, True):
        root = str(tmp_path / f"b{enc}")
        files = _make_backup(root, enc)
        assert list_backups([root])[0]["cifrado"] == enc
        b = Backup(root, "secreto" if enc else "")
        out = str(tmp_path / f"out{enc}")
        assert b.extract(out) == 2
        for rel, data in files.items():
            assert open(os.path.join(out, "CameraRollDomain", *rel.split("/")), "rb").read() == data
    import pytest
    with pytest.raises(PermissionError):
        Backup(str(tmp_path / "bTrue"), "otra")


def test_sqlite_deleted_messages(tmp_path):
    p = str(tmp_path / "sms.db")
    c = sqlite3.connect(p)
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("create table message (ROWID integer primary key, text text, handle text, date integer)")
    for i in range(300):
        c.execute("insert into message(text,handle,date) values (?,?,?)", (f"Mensaje {i} desde Acapulco", f"+52744{i:07d}", i))
    c.commit()
    c.execute("delete from message where ROWID % 3 = 0")
    c.commit()
    c.close()
    texts = {v[1] for t, w, v in SQLiteRecovery(p).recover() if len(v) > 1}
    want = {f"Mensaje {i} desde Acapulco" for i in range(300) if (i + 1) % 3 == 0}
    assert len(want & texts) >= 95
