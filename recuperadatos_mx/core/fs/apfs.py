"""APFS: contenedor, puntos de control, mapas de objetos, varios volúmenes,
árbol de archivos (inodos, directorios, extensiones), y volúmenes cifrados
(FileVault / cifrado nativo de APFS) desbloqueados con contraseña.

Los archivos borrados se recuperan de nodos antiguos del árbol que el
copy-on-write dejó en el espacio libre del contenedor."""
from __future__ import annotations

import hashlib
import struct
import uuid
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from ..device import BlockSource
from ..model import FileEntry

OBJ_TYPE_MASK = 0x0000FFFF
T_BTREE, T_BTREE_NODE, T_NXSB, T_APSB = 0x2, 0x3, 0x1, 0xD
ST_FSTREE, ST_OMAP = 0xE, 0xB
J_INODE, J_XATTR, J_DSTREAM_ID, J_FILE_EXTENT, J_DIR_REC = 3, 4, 6, 8, 9
NS = 1e9


def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]


def fletcher64(block: bytes) -> int:
    n = len(block) // 4
    words = struct.unpack(f"<{n}I", block)
    s1 = s2 = 0
    M = 0xFFFFFFFF
    for w in words[2:]:
        s1 = (s1 + w) % M
        s2 = (s2 + s1) % M
    c1 = M - ((s1 + s2) % M)
    c2 = M - ((s1 + c1) % M)
    return (c2 << 32) | c1


def valid(block: bytes) -> bool:
    return len(block) >= 64 and u64(block, 0) == fletcher64(block) and block[:8] != b"\0" * 8


class APFSContainer:
    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src, self.base = src, base
        b0 = src.read(base, 4096)
        if b0[32:36] != b"NXSB":
            raise ValueError("No es un contenedor APFS")
        self.bs = u32(b0, 36)
        self.nblocks = u64(b0, 40)
        sb = self._latest_sb(b0)
        self.sb = sb
        self.uuid = sb[72:88]
        self.xid = u64(sb, 16)
        self.omap = self.block(u64(sb, 160))
        self.omap_root = u64(self.omap, 48)
        self.fs_oids = [o for o in struct.unpack_from("<100Q", sb, 184) if o]
        self.keylocker = (u64(sb, 1296), u64(sb, 1304))

    def block(self, paddr: int, n: int = 1) -> bytes:
        return self.src.read(self.base + paddr * self.bs, n * self.bs)

    def _latest_sb(self, b0: bytes) -> bytes:
        bs = u32(b0, 36)
        desc_base, desc_blocks = u64(b0, 112), u32(b0, 104) & 0x7FFFFFFF
        best = b0[:bs] if valid(b0[:bs]) else None
        data = self.src.read(self.base + desc_base * bs, desc_blocks * bs)
        for i in range(desc_blocks):
            blk = data[i * bs:(i + 1) * bs]
            if blk[32:36] == b"NXSB" and valid(blk):
                if best is None or u64(blk, 16) > u64(best, 16):
                    best = blk
        if best is None:
            raise ValueError("No se encontró un superbloque APFS válido")
        return best

    # ------------------------------------------------------------------ #
    def btree_records(self, node: bytes, resolve: Optional[Callable[[int], Optional[bytes]]],
                      fixed: Optional[Tuple[int, int]] = None, guard: int = 0) -> Iterator[Tuple[bytes, bytes]]:
        """Recorre un B-tree APFS; ``resolve`` traduce los hijos (None = direcciones físicas)."""
        if guard > 12 or len(node) < 56:
            return
        flags, level, nkeys = u16(node, 32), u16(node, 34), u32(node, 36)
        toff, tlen = u16(node, 40), u16(node, 42)
        kstart = 56 + toff + tlen
        vend = len(node) - (40 if flags & 1 else 0)
        is_fixed = bool(flags & 4)
        for i in range(nkeys):
            if is_fixed:
                ko, vo = struct.unpack_from("<HH", node, 56 + toff + i * 4)
                kl = fixed[0] if fixed else 16
                vl = (fixed[1] if fixed else 16) if level == 0 else 8
            else:
                ko, kl, vo, vl = struct.unpack_from("<HHHH", node, 56 + toff + i * 8)
            k = node[kstart + ko:kstart + ko + kl]
            if vo == 0xFFFF:
                continue
            v = node[vend - vo:vend - vo + vl]
            if level == 0:
                yield k, v
            else:
                child_oid = u64(v, 0)
                child = resolve(child_oid) if resolve else self.block(child_oid)
                if child is not None:
                    yield from self.btree_records(child, resolve, fixed, guard + 1)

    def omap_table(self, omap_root_paddr: int, max_xid: Optional[int] = None) -> Dict[int, int]:
        """oid → paddr (la versión más reciente con xid ≤ max_xid)."""
        table: Dict[int, Tuple[int, int]] = {}
        root = self.block(omap_root_paddr)
        for k, v in self.btree_records(root, None, (16, 16)):
            oid, xid = u64(k, 0), u64(k, 8)
            if max_xid is not None and xid > max_xid:
                continue
            flags, paddr = u32(v, 0), u64(v, 8)
            if flags & 1:  # borrado
                continue
            if oid not in table or table[oid][0] < xid:
                table[oid] = (xid, paddr)
        return {o: p for o, (x, p) in table.items()}

    def volumes(self) -> List["APFSVolume"]:
        omap = self.omap_table(self.omap_root)
        out = []
        for oid in self.fs_oids:
            p = omap.get(oid)
            if p is None:
                continue
            blk = self.block(p)
            if blk[32:36] == b"APSB":
                out.append(APFSVolume(self, blk, p))
        return out

    # ------------------------- cifrado ------------------------------- #
    def keybag(self, prange: Tuple[int, int], key_uuid: bytes) -> List[Tuple[bytes, int, bytes]]:
        from ..crypto.common import Cipher, algorithms, modes
        start, count = prange
        if not start or not count:
            return []
        raw = self.block(start, count)
        key = key_uuid + key_uuid
        out = bytearray()
        for i in range(0, len(raw), 512):
            tweak = struct.pack("<Q", (start * self.bs + i) // 512) + b"\0" * 8
            d = Cipher(algorithms.AES(key), modes.XTS(tweak)).decryptor()
            out += d.update(raw[i:i + 512]) + d.finalize()
        kb = bytes(out)
        if u32(kb, 24) not in (0x6B657973, 0x72656373):  # 'keys' / 'recs'
            return []
        nkeys = u16(kb, 34)
        p = 48
        ents = []
        for _ in range(nkeys):
            if p + 24 > len(kb):
                break
            uid, tag, klen = kb[p:p + 16], u16(kb, p + 16), u16(kb, p + 18)
            ents.append((uid, tag, kb[p + 24:p + 24 + klen]))
            p += (24 + klen + 15) & ~15
        return ents


def _der(b: bytes, p: int = 0):
    """Decodificador DER mínimo: devuelve [(etiqueta, valor_bytes)] del nivel actual."""
    out = []
    while p < len(b):
        tag = b[p]
        ln = b[p + 1]
        p += 2
        if ln & 0x80:
            n = ln & 0x7F
            ln = int.from_bytes(b[p:p + n], "big")
            p += n
        out.append((tag, b[p:p + ln]))
        p += ln
    return out


def _der_find(b: bytes, path: List[int]) -> Optional[bytes]:
    cur = b
    for t in path:
        items = _der(cur)
        hit = next((v for tg, v in items if (tg & 0x1F) == t), None)
        if hit is None:
            return None
        cur = hit
    return cur


def aes_unwrap(kek: bytes, wrapped: bytes) -> Optional[bytes]:
    from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap
    try:
        return aes_key_unwrap(kek, wrapped)
    except (InvalidUnwrap, ValueError):
        return None


class APFSVolume:
    def __init__(self, nx: APFSContainer, sb: bytes, paddr: int) -> None:
        self.nx, self.sb, self.paddr = nx, sb, paddr
        self.name = sb[704:960].split(b"\0")[0].decode("utf-8", "replace")
        self.uuid = sb[240:256]
        self.fs_flags = u64(sb, 264)
        self.encrypted = not (self.fs_flags & 1)
        self.incompat = u64(sb, 56)
        self.hashed = bool(self.incompat & 0x8 or self.incompat & 0x1)  # normalización/insensible
        self.omap = nx.block(u64(sb, 128))
        self.omap_root = u64(self.omap, 48)
        self.root_oid = u64(sb, 136)
        self.vek: Optional[bytes] = None
        self._omap: Optional[Dict[int, int]] = None

    def omap_table(self) -> Dict[int, int]:
        if self._omap is None:
            self._omap = self.nx.omap_table(self.omap_root)
        return self._omap

    # ------------------------- cifrado ------------------------------- #
    def unlock(self, password: str) -> bool:
        if not self.encrypted:
            return True
        nx = self.nx
        ents = nx.keybag(nx.keylocker, nx.uuid)
        wrapped_vek = next((d for u, t, d in ents if u == self.uuid and t == 2), None)
        rec = next((d for u, t, d in ents if u == self.uuid and t == 3), None)
        if not wrapped_vek or not rec or len(rec) < 16:
            return False
        vkb = nx.keybag((u64(rec, 0), u64(rec, 8)), self.uuid)
        for u, t, blob in vkb:
            if t != 3:
                continue
            kek = self._kek_from_blob(blob, password)
            if kek is None:
                continue
            vek = self._vek_from_blob(wrapped_vek, kek)
            if vek:
                self.vek = vek
                self._omap = None
                return True
        return False

    @staticmethod
    def _kek_from_blob(blob: bytes, password: str) -> Optional[bytes]:
        body = _der_find(blob, [16])  # SEQUENCE
        if body is None:
            return None
        inner = _der_find(body, [3]) or body
        items = {tg & 0x1F: v for tg, v in _der(inner)}
        wrapped, it, salt = items.get(3), items.get(4), items.get(5)
        if not wrapped or not it or not salt:
            return None
        iters = int.from_bytes(it, "big")
        for klen in (32, 16):
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters, klen)
            k = aes_unwrap(dk, wrapped)
            if k:
                return k
        return None

    @staticmethod
    def _vek_from_blob(blob: bytes, kek: bytes) -> Optional[bytes]:
        body = _der_find(blob, [16])
        if body is None:
            return None
        inner = _der_find(body, [3]) or body
        items = {tg & 0x1F: v for tg, v in _der(inner)}
        wrapped = items.get(3)
        if not wrapped:
            return None
        for k in (kek, kek[:16]):
            v = aes_unwrap(k, wrapped)
            if v:
                if len(v) == 16:  # VEK de 128 bits: se completa con SHA-256(VEK || UUID) según apfs-fuse
                    v = v + hashlib.sha256(v + b"").digest()[:16]
                return v
        return None

    def decrypt_block(self, data: bytes, paddr: int) -> bytes:
        if not self.vek:
            return data
        from ..crypto.common import Cipher, algorithms, modes
        out = bytearray()
        bs = self.nx.bs
        for i in range(0, len(data), 512):
            sector = (paddr * bs + i) // 512
            tweak = struct.pack("<Q", sector) + b"\0" * 8
            d = Cipher(algorithms.AES(self.vek), modes.XTS(tweak)).decryptor()
            out += d.update(data[i:i + 512]) + d.finalize()
        return bytes(out)

    def node(self, paddr: int) -> bytes:
        b = self.nx.block(paddr)
        if self.vek and not valid(b):
            b = self.decrypt_block(b, paddr)
        return b

    # ------------------------------------------------------------------ #
    def records(self) -> Iterator[Tuple[bytes, bytes]]:
        omap = self.omap_table()

        def resolve(oid: int) -> Optional[bytes]:
            p = omap.get(oid)
            return self.node(p) if p is not None else None

        root = resolve(self.root_oid)
        if root is None:
            return iter(())
        return self.nx.btree_records(root, resolve)

    def old_records(self, cancel=None) -> Iterator[Tuple[bytes, bytes]]:
        """Nodos hoja del árbol de archivos que ya no están referenciados (espacio libre)."""
        nx = self.nx
        omap = self.omap_table()
        live = set(omap.values())
        mine = set(omap)  # oids virtuales de este volumen (únicos en el contenedor)
        CH = 256
        for start in range(0, nx.nblocks, CH):
            if cancel and cancel():
                return
            n = min(CH, nx.nblocks - start)
            data = nx.block(start, n)
            for i in range(n):
                pa = start + i
                if pa in live:
                    continue
                b = data[i * nx.bs:(i + 1) * nx.bs]
                hdr = b[:32]
                if not hdr.strip(b"\0"):
                    continue
                typ, sub = u32(b, 24) & OBJ_TYPE_MASK, u32(b, 28)
                if not (typ in (T_BTREE, T_BTREE_NODE) and sub == ST_FSTREE):
                    if not self.vek:
                        continue
                    first = self.decrypt_block(b[:512], pa)
                    if (u32(first, 24) & OBJ_TYPE_MASK) not in (T_BTREE, T_BTREE_NODE) or u32(first, 28) != ST_FSTREE:
                        continue
                    b = self.decrypt_block(b, pa)
                elif self.vek:
                    continue  # nodo sin cifrar: pertenece a otro volumen
                if u64(b, 8) not in mine or u16(b, 34) != 0 or not valid(b):
                    continue
                yield from nx.btree_records(b, lambda o: None)

    def _parse(self, it, store: dict) -> None:
        for k, v in it:
            if len(k) < 8:
                continue
            kv = u64(k, 0)
            oid, typ = kv & 0x0FFFFFFFFFFFFFFF, kv >> 60
            if typ == J_INODE and len(v) >= 92:
                store["inodes"].setdefault(oid, v)
            elif typ == J_DIR_REC:
                if self.hashed:
                    nl = u32(k, 8) & 0x3FF
                    name = k[12:12 + nl]
                else:
                    nl = u16(k, 8)
                    name = k[10:10 + nl]
                store["drecs"].append((oid, name.rstrip(b"\0").decode("utf-8", "replace"), u64(v, 0), u16(v, 16) & 0xF))
            elif typ == J_FILE_EXTENT and len(k) >= 16 and len(v) >= 24:
                store["extents"].setdefault(oid, {}).setdefault(
                    u64(k, 8), (u64(v, 0) & ((1 << 56) - 1), u64(v, 8), u64(v, 16)))

    @staticmethod
    def _xfields(v: bytes) -> Dict[int, bytes]:
        out = {}
        if len(v) < 96:
            return out
        n = u16(v, 92)
        p = 96 + n * 4
        for i in range(n):
            t, fl, sz = v[96 + i * 4], v[97 + i * 4], u16(v, 98 + i * 4)
            out[t] = v[p:p + sz]
            p += (sz + 7) & ~7
        return out

    def file_entry(self, oid: int, v: bytes, store: dict, name: str, path: str, deleted: bool) -> FileEntry:
        xf = self._xfields(v)
        size = u64(xf[8], 0) if 8 in xf and len(xf[8]) >= 8 else 0
        private = u64(v, 8)
        exts = sorted(store["extents"].get(private, {}).items())
        out: List[Tuple[int, int]] = []
        tweaks: List[int] = []
        pos = 0
        bs = self.nx.bs
        for laddr, (ln, pblk, cid) in exts:
            if laddr > pos:
                out.append((-1, laddr - pos))
                tweaks.append(0)
            out.append((self.nx.base + pblk * bs if pblk else -1, ln))
            tweaks.append(pblk * (bs // 512))  # sector físico del contenedor (volúmenes ONEKEY/FileVault)
            pos = laddr + ln
        e = FileEntry(name=name, path=path, size=size, deleted=deleted, fs="APFS",
                      ctime=u64(v, 16) / NS, mtime=u64(v, 24) / NS, atime=u64(v, 40) / NS,
                      ident=f"inodo {oid}", extents=[])
        rem = size
        used = []
        for (o, l), tw in zip(out, tweaks):
            if rem <= 0:
                break
            e.extents.append((o, min(l, rem)))
            used.append(tw)
            rem -= l
        if self.vek and e.extents:
            e.codec = "apfs-xts"
            e.codec_args = {"vek": self.vek.hex(), "tweaks": used}
        if 0xC in xf or (u32(v, 68) & 0x20):  # comprimido (decmpfs)
            e.note = "Archivo comprimido con decmpfs; se recupera el flujo comprimido"
        return e

    def entries(self, progress=None, cancel=None, root_label: str = "") -> List[FileEntry]:
        live = {"inodes": {}, "drecs": [], "extents": {}}
        self._parse(self.records(), live)
        old = {"inodes": {}, "drecs": [], "extents": {}}
        self._parse(self.old_records(cancel), old)
        children = {}
        for parent, name, fid, dt in live["drecs"]:
            children[fid] = (parent, name)
        cache = {2: root_label, 1: root_label}

        def path_of(oid, depth=0):
            if oid in cache:
                return cache[oid]
            if oid not in children or depth > 64:
                return (root_label + "/" if root_label else "") + "$Huérfanos"
            par, nm = children[oid]
            pp = path_of(par, depth + 1)
            cache[oid] = (pp + "/" + nm) if pp else nm
            return cache[oid]

        out: List[FileEntry] = []
        for oid, v in live["inodes"].items():
            if oid <= 2 or oid not in children:
                continue
            mode = u16(v, 80)
            par, nm = children[oid]
            if mode & 0xF000 == 0x4000:
                out.append(FileEntry(name=nm, path=path_of(par), is_dir=True, fs="APFS", mtime=u64(v, 24) / NS,
                                     ident=f"inodo {oid}"))
            elif mode & 0xF000 == 0x8000:
                e = self.file_entry(oid, v, live, nm, path_of(par), False)
                e.chance = "Excelente"
                out.append(e)
        # borrados: inodos presentes solo en nodos antiguos
        merged_ext = {"extents": dict(old["extents"])}
        for k, v in live["extents"].items():
            merged_ext["extents"].setdefault(k, v)
        for oid, v in old["inodes"].items():
            if oid in live["inodes"] or oid <= 2:
                continue
            mode = u16(v, 80)
            if mode & 0xF000 != 0x8000:
                continue
            xf = self._xfields(v)
            nm = xf.get(4, b"").rstrip(b"\0").decode("utf-8", "replace") or f"inodo_{oid}"
            par = u64(v, 0)
            e = self.file_entry(oid, v, merged_ext, nm, path_of(par) if par in cache or par in children else
                                (root_label + "/" if root_label else "") + "$Huérfanos", True)
            if not e.extents:
                continue
            e.chance = "Regular"
            e.note = "Recuperado de un nodo antiguo del árbol APFS (copy-on-write)"
            out.append(e)
        return out


class APFS:
    """Adaptador para el escáner: une todos los volúmenes del contenedor."""
    name = "APFS"
    passwords: Dict[str, str] = {}
    password_list: List[str] = []

    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.nx = APFSContainer(src, base)
        self.size = self.nx.bs * self.nx.nblocks

    def entries(self, progress=None, cancel=None) -> List[FileEntry]:
        out = []
        for vol in self.nx.volumes():
            label = f"Volumen «{vol.name}»"
            if vol.encrypted:
                cands = [APFS.passwords.get(uuid.UUID(bytes=vol.uuid).hex), APFS.passwords.get("*")] + APFS.password_list
                if not any(pw and vol.unlock(pw) for pw in cands):
                    out.append(FileEntry(name=f"🔒 {vol.name} (cifrado: indique la contraseña)", path=label,
                                         fs="APFS", note="Volumen cifrado con FileVault/APFS", is_dir=True))
                    continue
                label += " (descifrado)"
            out += vol.entries(progress, cancel, label)
        return out
