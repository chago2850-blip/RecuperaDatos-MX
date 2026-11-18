"""Constructor de RAID virtual: JBOD, RAID 0, 1, 1E, 4, 5, 5E-like, 6 (paridad XOR),
10, con miembros faltantes, desplazamientos por disco y detección automática."""
from __future__ import annotations

import itertools
import json
from typing import Callable, List, Optional, Tuple

from .device import BlockSource, open_image

LEVELS = ["JBOD", "0", "1", "1E", "4", "5", "6", "10"]
LAYOUTS = ["left-asymmetric", "left-symmetric", "right-asymmetric", "right-symmetric"]
LAYOUT_NAMES = {"left-asymmetric": "Izquierda asimétrica", "left-symmetric": "Izquierda simétrica (Linux md)",
                "right-asymmetric": "Derecha asimétrica", "right-symmetric": "Derecha simétrica"}


def xor_bytes(blocks: List[bytes]) -> bytes:
    n = max(len(b) for b in blocks)
    acc = 0
    for b in blocks:
        acc ^= int.from_bytes(b.ljust(n, b"\0"), "little")
    return acc.to_bytes(n, "little")


class VirtualRAID(BlockSource):
    def __init__(self, members: List[Optional[BlockSource]], level: str = "0", stripe: int = 65536,
                 layout: str = "left-symmetric", offsets: Optional[List[int]] = None,
                 parity_delay: int = 1, member_size: Optional[int] = None, name: str = "RAID virtual") -> None:
        if level not in LEVELS:
            raise ValueError(f"Nivel RAID no soportado: {level}")
        self.members = members
        self.n = len(members)
        self.level = level
        self.stripe = stripe
        self.layout = layout
        self.offsets = offsets or [0] * self.n
        self.parity_delay = max(1, parity_delay)
        self.name = name
        self.sector_size = 512
        sizes = [m.size - o for m, o in zip(members, self.offsets) if m is not None]
        if not sizes:
            raise ValueError("Se necesita al menos un miembro presente")
        self.msize = member_size or min(sizes)
        self.msize -= self.msize % stripe if level not in ("JBOD", "1") else 0
        missing = sum(1 for m in members if m is None)
        n = self.n
        if level == "JBOD":
            if missing:
                raise ValueError("JBOD no admite miembros faltantes")
            self.size = sum(m.size - o for m, o in zip(members, self.offsets))
        elif level == "0":
            self.size = self.msize * n
        elif level == "1":
            self.size = self.msize
        elif level == "1E":
            self.size = self.msize * n // 2
        elif level in ("4", "5"):
            if missing > 1:
                raise ValueError("RAID 4/5 tolera un solo miembro faltante")
            self.size = self.msize * (n - 1)
        elif level == "6":
            if missing > 1:
                raise ValueError("Con paridad XOR solo se reconstruye un miembro faltante en RAID 6")
            self.size = self.msize * (n - 2)
        elif level == "10":
            if n % 2:
                raise ValueError("RAID 10 requiere un número par de discos")
            self.size = self.msize * (n // 2)

    # ------------------------------------------------------------------ #
    def _member_read(self, d: int, off: int, ln: int) -> Optional[bytes]:
        m = self.members[d]
        if m is None:
            return None
        return m.read(self.offsets[d] + off, ln).ljust(ln, b"\0")

    def _parity_disk(self, row: int, ndisks: int) -> int:
        r = row // self.parity_delay
        if self.layout.startswith("left"):
            return (ndisks - 1) - (r % ndisks)
        return r % ndisks

    def _locate(self, stripe_no: int) -> Tuple[int, int, List[int]]:
        """Devuelve (disco_datos, fila, discos_de_la_fila_para_reconstruir)."""
        n = self.n
        if self.level == "0":
            return stripe_no % n, stripe_no // n, []
        if self.level == "4":
            row, k = divmod(stripe_no, n - 1)
            return k, row, list(range(n))
        if self.level == "5":
            row, k = divmod(stripe_no, n - 1)
            p = self._parity_disk(row, n)
            if self.layout.endswith("asymmetric"):
                d = k if k < p else k + 1
            else:
                d = (p + 1 + k) % n
            return d, row, list(range(n))
        if self.level == "6":
            row, k = divmod(stripe_no, n - 2)
            p = self._parity_disk(row, n)
            q = (p + 1) % n
            if self.layout.endswith("asymmetric"):
                data = [x for x in range(n) if x not in (p, q)]
                d = data[k]
            else:
                d = (p + 2 + k) % n
            return d, row, [x for x in range(n) if x != q]
        raise ValueError

    def _read_stripe_unit(self, stripe_no: int, inner: int, ln: int) -> bytes:
        d, row, group = self._locate(stripe_no)
        off = row * self.stripe + inner
        data = self._member_read(d, off, ln)
        if data is not None:
            return data
        others = [self._member_read(x, off, ln) for x in group if x != d]
        if any(o is None for o in others):
            return b"\0" * ln
        return xor_bytes(others)

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        out = bytearray()
        if self.level == "JBOD":
            base = 0
            for d, m in enumerate(self.members):
                sz = m.size - self.offsets[d]
                if offset < base + sz and offset + length > base:
                    s = max(offset, base)
                    e = min(offset + length, base + sz)
                    out += m.read(self.offsets[d] + s - base, e - s)
                base += sz
            return bytes(out)
        if self.level == "1":
            for d in range(self.n):
                r = self._member_read(d, offset, length)
                if r is not None:
                    return r
            return b"\0" * length
        while length > 0:
            sn, inner = divmod(offset, self.stripe)
            n = min(length, self.stripe - inner)
            if self.level == "10":
                pairs = self.n // 2
                pair, row = sn % pairs, sn // pairs
                chunk = None
                for d in (pair * 2, pair * 2 + 1):
                    chunk = self._member_read(d, row * self.stripe + inner, n)
                    if chunk is not None:
                        break
                out += chunk if chunk is not None else b"\0" * n
            elif self.level == "1E":
                # cada franja se escribe en el disco k y su copia en el disco k+1
                k = (sn * 2) % self.n
                row = (sn * 2) // self.n
                chunk = self._member_read(k, row * self.stripe + inner, n)
                if chunk is None:
                    k2 = (sn * 2 + 1) % self.n
                    row2 = (sn * 2 + 1) // self.n
                    chunk = self._member_read(k2, row2 * self.stripe + inner, n)
                out += chunk if chunk is not None else b"\0" * n
            else:
                out += self._read_stripe_unit(sn, inner, n)
            offset += n
            length -= n
        return bytes(out)

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {"tipo": "raid", "nivel": self.level, "franja": self.stripe, "esquema": self.layout,
                "retardo_paridad": self.parity_delay, "desplazamientos": self.offsets,
                "miembros": [m.describe() if m is not None else None for m in self.members]}

    describe = to_dict

    @classmethod
    def from_dict(cls, d: dict) -> "VirtualRAID":
        from .session import reopen_source
        members = [reopen_source(m) if m else None for m in d["miembros"]]
        return cls(members, d["nivel"], d["franja"], d.get("esquema", "left-symmetric"),
                   d.get("desplazamientos"), d.get("retardo_paridad", 1))


def save_raid_definition(path: str, raid: VirtualRAID) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raid.to_dict(), fh, indent=2, ensure_ascii=False)


def load_raid_definition(path: str) -> VirtualRAID:
    with open(path, encoding="utf-8") as fh:
        return VirtualRAID.from_dict(json.load(fh))


# --------------------------------------------------------------------------- #
# Detección automática
# --------------------------------------------------------------------------- #
def parity_check(members: List[BlockSource], probe: int = 4 << 20, offset: int = 0) -> float:
    """Proporción de bloques donde el XOR de todos los miembros es cero (indica RAID 5)."""
    step = 64 << 10
    ok = total = 0
    for off in range(offset, offset + probe, step):
        blocks = [m.read(off, 4096) for m in members]
        if not any(any(b) for b in blocks):
            continue
        total += 1
        if not any(xor_bytes(blocks)):
            ok += 1
    return ok / total if total else 0.0


def _score(v: VirtualRAID) -> float:
    from .fs import detect
    from .partitions import read_table
    s = 0.0
    try:
        parts = read_table(v)
    except Exception:
        parts = []
    starts = [p.start for p in parts if p.fs] or ([0] if detect(v, 0) else [])
    if parts:
        s += 1
    for st in starts[:4]:
        fs = detect(v, st)
        if not fs:
            continue
        s += 2
        if fs == "NTFS":
            from .fs.ntfs import NTFS
            try:
                nt = NTFS(v, st)
                base = st + nt.mft_lcn * nt.cluster
                span = max(8, (v.stripe * 4) // nt.rec_size)
                good = 0
                for i in range(0, span, max(1, span // 64)):
                    raw = v.read(base + i * nt.rec_size, 48)
                    if raw[:4] == b"FILE" and int.from_bytes(raw[0x2C:0x30], "little") == i:
                        good += 1
                s += 10 * good / len(range(0, span, max(1, span // 64)))
            except Exception:
                pass
        elif fs.startswith("ext"):
            from .fs.ext import ExtFS
            try:
                ex = ExtFS(v, st)
                root = ex.parse_inode(ex.inode_raw(2))
                data = ex.read_all(root, limit=4096)
                live, _ = ex._parse_dir(data)
                names = [n for _, n, _ in live]
                if "." in names and ".." in names:
                    s += 6
                if "lost+found" in names:
                    s += 4
            except Exception:
                pass
        else:
            s += 1
    return s


def auto_detect(members: List[BlockSource], levels=("0", "5"), stripes=None,
                progress: Optional[Callable[[int, int], None]] = None,
                cancel: Optional[Callable[[], bool]] = None, max_results: int = 5) -> List[dict]:
    """Prueba combinaciones de orden, tamaño de franja, nivel y esquema; devuelve las mejores."""
    stripes = stripes or [s << 10 for s in (4, 8, 16, 32, 64, 128, 256, 512, 1024)]
    n = len(members)
    if n < 2:
        return []
    levels = list(levels)
    if "5" in levels and parity_check(members) < 0.9:
        levels = [lv for lv in levels if lv != "5"] or levels
    combos = []
    perms = list(itertools.permutations(range(n)))
    if n > 6:
        perms = perms[:720]
    for lv in levels:
        for st in stripes:
            for order in perms:
                for lay in (LAYOUTS if lv in ("5", "6") else ["left-symmetric"]):
                    combos.append((lv, st, order, lay))
    results = []
    for i, (lv, st, order, lay) in enumerate(combos):
        if cancel and cancel():
            break
        try:
            v = VirtualRAID([members[k] for k in order], lv, st, lay)
            sc = _score(v)
        except Exception:
            continue
        if sc > 0:
            results.append({"nivel": lv, "franja": st, "orden": list(order), "esquema": lay, "puntos": sc})
        if progress and i % 16 == 0:
            progress(i, len(combos))
    results.sort(key=lambda r: -r["puntos"])
    return results[:max_results]


def open_member(path: str) -> BlockSource:
    return open_image(path)
