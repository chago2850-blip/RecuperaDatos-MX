"""NTFS: lectura de la MFT, archivos borrados, flujos alternos, compresión LZNT1
y reconstrucción a partir de registros MFT huérfanos."""
from __future__ import annotations

import struct
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from ..device import BlockSource
from ..model import FileEntry
from . import u16, u32, u64

EPOCH_DIFF = 116444736000000000


def filetime(v: int) -> float:
    if v <= EPOCH_DIFF:
        return 0.0
    return (v - EPOCH_DIFF) / 1e7


def apply_fixup(rec: bytearray, bps: int) -> bool:
    usa_off = u16(rec, 4)
    usa_cnt = u16(rec, 6)
    if usa_off + usa_cnt * 2 > len(rec) or usa_cnt == 0:
        return False
    usn = rec[usa_off:usa_off + 2]
    ok = True
    for i in range(1, usa_cnt):
        pos = i * bps - 2
        if pos + 2 > len(rec):
            break
        if rec[pos:pos + 2] != usn:
            ok = False
        rec[pos:pos + 2] = rec[usa_off + i * 2: usa_off + i * 2 + 2]
    return ok


def decode_runs(b: bytes, off: int) -> List[Tuple[int, int]]:
    """Devuelve [(lcn | -1, clusters)]."""
    runs = []
    lcn = 0
    while off < len(b):
        h = b[off]
        if h == 0:
            break
        ln = h & 0x0F
        lo = h >> 4
        off += 1
        if ln == 0 or off + ln + lo > len(b):
            break
        count = int.from_bytes(b[off:off + ln], "little")
        off += ln
        if lo == 0:
            runs.append((-1, count))
        else:
            delta = int.from_bytes(b[off:off + lo], "little", signed=True)
            lcn += delta
            runs.append((lcn, count))
        off += lo
    return runs


def lznt1_decompress(buf: bytes) -> bytes:
    out = bytearray()
    pos = 0
    while pos + 2 <= len(buf):
        hdr = u16(buf, pos)
        pos += 2
        if hdr == 0:
            break
        size = (hdr & 0x0FFF) + 1
        chunk = buf[pos:pos + size]
        pos += size
        if not hdr & 0x8000:
            out += chunk
            continue
        dst = bytearray()
        i = 0
        while i < len(chunk):
            flags = chunk[i]
            i += 1
            for bit in range(8):
                if i >= len(chunk):
                    break
                if not (flags >> bit) & 1:
                    dst.append(chunk[i])
                    i += 1
                else:
                    if i + 2 > len(chunk):
                        i = len(chunk)
                        break
                    tok = u16(chunk, i)
                    i += 2
                    p = len(dst) - 1
                    shift = 12
                    while p >= 0x10:
                        p >>= 1
                        shift -= 1
                    length = (tok & ((1 << shift) - 1)) + 3
                    disp = (tok >> shift) + 1
                    start = len(dst) - disp
                    if start < 0:
                        break
                    for k in range(length):
                        dst.append(dst[start + k])
        out += dst.ljust(4096, b"\0") if len(dst) < 4096 and pos < len(buf) else dst
    return bytes(out)


def iter_compressed(src: BlockSource, entry: FileEntry, remaining: int) -> Iterator[bytes]:
    a = entry.codec_args
    cluster, base, unit = a["cluster"], a["base"], a.get("unit", 16)
    # expandir a lista de clusters por VCN
    vcns: List[int] = []
    for lcn, cnt in a["runs"]:
        if lcn < 0:
            vcns.extend([-1] * cnt)
        else:
            vcns.extend(range(lcn, lcn + cnt))
    for u in range(0, len(vcns), unit):
        if remaining <= 0:
            break
        grp = vcns[u:u + unit]
        real = [c for c in grp if c >= 0]
        if not real:
            data = b"\0" * (unit * cluster)
        else:
            raw = b"".join(src.read(base + c * cluster, cluster) for c in real)
            if len(real) == unit:
                data = raw
            else:
                data = lznt1_decompress(raw)
                data = data.ljust(unit * cluster, b"\0")[:unit * cluster]
        n = min(len(data), remaining)
        yield data[:n]
        remaining -= n


class NTFS:
    name = "NTFS"

    def __init__(self, src: BlockSource, base: int = 0) -> None:
        self.src = src
        self.base = base
        b = src.read(base, 512)
        if b[3:11] != b"NTFS    ":
            raise ValueError("No es NTFS")
        self.bps = u16(b, 0x0B) or 512
        spc = b[0x0D]
        if spc > 0x80:
            spc = 1 << (256 - spc)
        self.cluster = self.bps * spc
        self.total_sectors = u64(b, 0x28)
        self.size = self.total_sectors * self.bps
        self.mft_lcn = u64(b, 0x30)
        self.mftmirr_lcn = u64(b, 0x38)
        v = struct.unpack_from("<b", b, 0x40)[0]
        self.rec_size = (1 << -v) if v < 0 else v * self.cluster
        self.label = ""
        self._bitmap: Optional[bytes] = None

    # ------------------------------------------------------------------ #
    def parse_record(self, raw: bytes, recno: Optional[int] = None) -> Optional[dict]:
        if raw[:4] != b"FILE":
            return None
        rec = bytearray(raw[:self.rec_size])
        apply_fixup(rec, self.bps)
        flags = u16(rec, 0x16)
        r = {"flags": flags, "in_use": bool(flags & 1), "dir": bool(flags & 2),
             "seq": u16(rec, 0x10), "base": u64(rec, 0x20) & 0xFFFFFFFFFFFF,
             "recno": u32(rec, 0x2C) if recno is None else recno,
             "names": [], "data": {}, "si": None, "attrlist": False}
        off = u16(rec, 0x14)
        guard = 0
        while off + 16 <= len(rec) and guard < 64:
            guard += 1
            atype = u32(rec, off)
            if atype == 0xFFFFFFFF:
                break
            alen = u32(rec, off + 4)
            if alen < 16 or off + alen > len(rec):
                break
            nonres = rec[off + 8]
            nlen = rec[off + 9]
            noff = u16(rec, off + 10)
            aflags = u16(rec, off + 12)
            aname = rec[off + noff: off + noff + nlen * 2].decode("utf-16-le", "replace") if nlen else ""
            if not nonres:
                clen = u32(rec, off + 0x10)
                coff = u16(rec, off + 0x14)
                content = bytes(rec[off + coff: off + coff + clen])
                if atype == 0x10 and len(content) >= 32:
                    r["si"] = (filetime(u64(content, 0)), filetime(u64(content, 8)),
                               filetime(u64(content, 24)))
                elif atype == 0x30 and len(content) >= 66:
                    parent = u64(content, 0)
                    fnl = content[64]
                    ns = content[65]
                    nm = content[66:66 + fnl * 2].decode("utf-16-le", "replace")
                    r["names"].append({"parent": parent & 0xFFFFFFFFFFFF, "pseq": parent >> 48,
                                       "name": nm, "ns": ns, "size": u64(content, 48),
                                       "ctime": filetime(u64(content, 8)),
                                       "mtime": filetime(u64(content, 16))})
                elif atype == 0x80:
                    r["data"].setdefault(aname, {"resident": content, "size": len(content),
                                                  "runs": [], "flags": aflags})
                elif atype == 0x60:
                    self.label = content.decode("utf-16-le", "replace")
                elif atype == 0x20:
                    r["attrlist"] = True
            else:
                start_vcn = u64(rec, off + 0x10)
                roff = u16(rec, off + 0x20)
                runs = decode_runs(bytes(rec[off: off + alen]), roff)
                if atype == 0x80:
                    d = r["data"].setdefault(aname, {"resident": None, "size": 0, "runs": [],
                                                     "flags": aflags, "frags": []})
                    d.setdefault("frags", []).append((start_vcn, runs))
                    if start_vcn == 0:
                        d["size"] = u64(rec, off + 0x30)
                        d["flags"] = aflags
            off += alen
        for d in r["data"].values():
            frags = sorted(d.get("frags", []))
            d["runs"] = [run for _, rs in frags for run in rs]
        return r

    def _mft_runs(self) -> Tuple[List[Tuple[int, int]], int]:
        raw = self.src.read(self.base + self.mft_lcn * self.cluster, self.rec_size)
        rec = self.parse_record(raw, 0)
        if not rec or "" not in rec["data"]:
            raw = self.src.read(self.base + self.mftmirr_lcn * self.cluster, self.rec_size)
            rec = self.parse_record(raw, 0)
        if not rec or "" not in rec["data"]:
            raise ValueError("MFT ilegible")
        d = rec["data"][""]
        return d["runs"], d["size"]

    def iter_records(self, progress=None, cancel=None) -> Iterator[dict]:
        runs, size = self._mft_runs()
        total = size // self.rec_size
        recno = 0
        CH = 256 * self.rec_size
        for lcn, cnt in runs:
            if lcn < 0:
                recno += cnt * self.cluster // self.rec_size
                continue
            run_off = self.base + lcn * self.cluster
            run_len = cnt * self.cluster
            pos = 0
            while pos < run_len and recno < total:
                if cancel and cancel():
                    return
                block = self.src.read(run_off + pos, min(CH, run_len - pos))
                for i in range(0, len(block) - self.rec_size + 1, self.rec_size):
                    if recno >= total:
                        break
                    r = self.parse_record(block[i:i + self.rec_size], recno)
                    if r:
                        yield r
                    recno += 1
                pos += CH
                if progress:
                    progress(recno, total)

    def bitmap(self) -> Optional[bytes]:
        if self._bitmap is None:
            try:
                runs, size = self._mft_runs()
                # registro 6 = $Bitmap
                lcn0 = runs[0][0]
                raw = self.src.read(self.base + lcn0 * self.cluster + 6 * self.rec_size, self.rec_size)
                r = self.parse_record(raw, 6)
                d = r["data"][""]
                if d["resident"] is not None:
                    self._bitmap = d["resident"]
                else:
                    e = FileEntry(name="$Bitmap", size=d["size"], extents=self._extents(d["runs"]))
                    from ..model import iter_content
                    self._bitmap = b"".join(iter_content(self.src, e))
            except Exception:
                self._bitmap = b""
        return self._bitmap

    def _extents(self, runs) -> List[Tuple[int, int]]:
        out = []
        for lcn, cnt in runs:
            if lcn < 0:
                out.append((-1, cnt * self.cluster))
            else:
                out.append((self.base + lcn * self.cluster, cnt * self.cluster))
        return out

    def _chance(self, runs) -> str:
        bm = self.bitmap()
        if not bm or not runs:
            return "Buena" if runs else "Regular"
        used = total = 0
        for lcn, cnt in runs:
            if lcn < 0:
                continue
            for c in range(lcn, lcn + min(cnt, 4096)):
                total += 1
                byte = c >> 3
                if byte < len(bm) and bm[byte] >> (c & 7) & 1:
                    used += 1
        if total == 0:
            return "Regular"
        ratio = used / total
        if ratio == 0:
            return "Buena"
        if ratio < 0.5:
            return "Regular"
        return "Baja"

    # ------------------------------------------------------------------ #
    def entries(self, progress=None, cancel=None, records: Optional[List[dict]] = None) -> List[FileEntry]:
        recs: Dict[int, dict] = {}
        ext_data: Dict[int, List[dict]] = {}
        src_iter = records if records is not None else self.iter_records(progress, cancel)
        for r in src_iter:
            if r["base"]:
                ext_data.setdefault(r["base"], []).append(r)
                continue
            if r["names"]:
                recs[r["recno"]] = r
        # fusionar registros de extensión (lista de atributos)
        for b, exts in ext_data.items():
            if b in recs:
                for e in exts:
                    for k, d in e["data"].items():
                        tgt = recs[b]["data"].setdefault(k, {"resident": None, "size": 0, "runs": [],
                                                             "flags": d["flags"], "frags": []})
                        tgt.setdefault("frags", []).extend(d.get("frags", []))
                for d in recs[b]["data"].values():
                    if d.get("frags"):
                        d["runs"] = [run for _, rs in sorted(d["frags"]) for run in rs]
                        if not d["size"]:
                            d["size"] = sum(c for _, c in d["runs"]) * self.cluster

        def best_name(r):
            names = sorted(r["names"], key=lambda n: {1: 0, 3: 1, 0: 2, 2: 3}.get(n["ns"], 4))
            return names[0]

        cache: Dict[int, str] = {5: ""}

        def path_of(recno: int, depth=0) -> str:
            if recno in cache:
                return cache[recno]
            r = recs.get(recno)
            if r is None or depth > 64:
                return "$Huérfanos"
            n = best_name(r)
            if n["parent"] == recno:
                return ""
            parent_path = path_of(n["parent"], depth + 1)
            p = (parent_path + "/" + n["name"]) if parent_path else n["name"]
            cache[recno] = p
            return p

        out: List[FileEntry] = []
        for recno, r in recs.items():
            if recno < 16 or recno == 5:
                continue
            n = best_name(r)
            if n["name"].startswith("$") and recno < 32:
                continue
            parent_path = path_of(n["parent"]) if n["parent"] != recno else ""
            if r["in_use"] is False:
                parent = recs.get(n["parent"])
                if parent is None or (parent and not parent["dir"]):
                    parent_path = "$Huérfanos"
            si = r["si"] or (n["ctime"], n["mtime"], 0.0)
            if r["dir"]:
                out.append(FileEntry(name=n["name"], path=parent_path, is_dir=True,
                                     deleted=not r["in_use"], fs="NTFS", ident=f"MFT#{recno}",
                                     ctime=si[0], mtime=si[1], atime=si[2]))
                continue
            for stream, d in (r["data"].items() or [("", {"resident": b"", "size": 0, "runs": [], "flags": 0})]):
                name = n["name"] if not stream else f"{n['name']}:{stream}"
                if stream in ("$I30",):
                    continue
                e = FileEntry(name=name, path=parent_path, size=d["size"], deleted=not r["in_use"],
                              fs="NTFS", ident=f"MFT#{recno}", ctime=si[0], mtime=si[1], atime=si[2])
                if d["resident"] is not None:
                    e.resident = d["resident"]
                    e.chance = "Excelente"
                else:
                    e.extents = self._extents(d["runs"])
                    if d["flags"] & 0x0001:
                        e.codec = "lznt1"
                        e.codec_args = {"runs": d["runs"], "cluster": self.cluster, "base": self.base,
                                        "unit": 16}
                    if d["flags"] & 0x4000:
                        e.note = "Cifrado EFS: se recupera el contenido cifrado"
                    e.chance = "Excelente" if r["in_use"] else self._chance(d["runs"])
                out.append(e)
        return out


def scan_orphan_records(src: BlockSource, start: int, end: int, rec_size: int = 1024,
                        bps: int = 512, progress=None, cancel=None) -> List[dict]:
    """Busca registros MFT ('FILE') sueltos en una región (MFT dañada o disco formateado)."""
    found = []
    CH = 8 << 20
    pos = start
    dummy = NTFS.__new__(NTFS)
    dummy.rec_size = rec_size
    dummy.bps = bps
    dummy.label = ""
    while pos < end:
        if cancel and cancel():
            break
        blk = src.read(pos, min(CH, end - pos))
        if not blk:
            break
        i = blk.find(b"FILE0")
        while i != -1:
            if i % bps == 0 and i + rec_size <= len(blk):
                r = dummy.parse_record(blk[i:i + rec_size])
                if r and r["names"] and not r["base"]:
                    r["abs"] = pos + i
                    found.append(r)
            i = blk.find(b"FILE0", i + 1)
        pos += CH
        if progress:
            progress(pos - start, end - start)
    return found
