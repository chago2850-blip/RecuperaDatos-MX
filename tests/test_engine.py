"""Pruebas de extremo a extremo sobre imágenes reales generadas con tests/build_images.py.

Ejecutar:  sudo python3 tests/build_images.py /tmp/rdt && RD_IMAGES=/tmp/rdt python3 -m pytest tests
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recuperadatos_mx.core import carving, repair, session  # noqa: E402
from recuperadatos_mx.core.device import FileSource, MemorySource  # noqa: E402
from recuperadatos_mx.core.model import iter_content  # noqa: E402
from recuperadatos_mx.core.partitions import read_table, search_lost  # noqa: E402
from recuperadatos_mx.core.raid import VirtualRAID, auto_detect  # noqa: E402
from recuperadatos_mx.core.scanner import ScanOptions, scan  # noqa: E402

IMG = os.environ.get("RD_IMAGES", "/tmp/rdt")
need = pytest.mark.skipif(not os.path.exists(os.path.join(IMG, "hashes.txt")), reason="sin imágenes de prueba")


def hashes():
    out = {}
    for line in open(os.path.join(IMG, "hashes.txt"), encoding="utf-8"):
        h, n = line.rstrip("\n").split("  ", 1)
        out[n] = h
    return out


DELETED = ["foto_1.jpg", "informe.pdf", "Carta muy larga para la abuela.docx", "grafico.png"]


def sha(src, e):
    h = hashlib.sha256()
    for c in iter_content(src, e):
        h.update(c)
    return h.hexdigest()


@need
@pytest.mark.parametrize("kind", ["fat", "exfat", "ntfs", "ext4"])
def test_quick_scan_recovers_deleted(kind):
    src = FileSource(os.path.join(IMG, f"{kind}.img"))
    res = scan(src, ScanOptions(deep=False))
    H = hashes()
    files = {e.name: e for e in res.entries if not e.is_dir}
    live = [n for n in H if n not in DELETED]
    for n in live:
        assert n in files, f"{kind}: falta {n}"
        assert sha(src, files[n]) == H[n], f"{kind}: contenido distinto en {n}"
    # en FAT 8.3 el primer carácter de un nombre borrado se pierde: comparar por contenido
    deleted_hashes = {sha(src, e) for e in res.entries if e.deleted and not e.is_dir}
    recovered = [n for n in DELETED if H[n] in deleted_hashes]
    assert len(recovered) == len(DELETED), f"{kind}: recuperados {recovered}"


@need
@pytest.mark.parametrize("kind", ["ext4", "fat", "ntfs"])
def test_deep_scan_carves_deleted(kind):
    src = FileSource(os.path.join(IMG, f"{kind}.img"))
    res = scan(src, ScanOptions(deep=True))
    H = hashes()
    got = {sha(src, e) for e in res.entries if not e.is_dir and e.origin == "raw" or e.deleted}
    for n in ("foto_1.jpg", "informe.pdf", "grafico.png", "Carta muy larga para la abuela.docx"):
        assert H[n] in got, f"{kind}: {n} no recuperado por firma"


@need
def test_partition_table_and_lost_search():
    src = FileSource(os.path.join(IMG, "disco_mbr.img"))
    parts = read_table(src)
    assert [p.fs for p in parts] == ["FAT", "NTFS"]
    lost = search_lost(src)
    assert {p.start for p in lost} >= {p.start for p in parts}


@need
def test_session_roundtrip(tmp_path):
    src = FileSource(os.path.join(IMG, "ntfs.img"))
    res = scan(src, ScanOptions())
    p = tmp_path / "s.rdsesion"
    session.save(str(p), src.describe(), res.entries, res.partitions)
    desc, ents, parts = session.load(str(p))
    assert len(ents) == len(res.entries)
    src2 = session.reopen_source(desc)
    e = next(x for x in ents if x.name == "notas.txt")
    assert sha(src2, e) == hashes()["notas.txt"]


def test_raid5_reconstruction_and_missing_member():
    import random
    rnd = random.Random(1)
    data = bytes(rnd.randrange(256) for _ in range(64 * 1024 * 3 * 4))
    stripe = 16 * 1024
    n = 4
    disks = [bytearray() for _ in range(n)]
    sn = 0
    row = 0
    while sn * stripe < len(data):
        p = (n - 1) - (row % n)
        chunks = []
        for k in range(n - 1):
            chunks.append(data[sn * stripe:(sn + 1) * stripe].ljust(stripe, b"\0"))
            sn += 1
        par = bytes(stripe)
        for c in chunks:
            par = bytes(a ^ b for a, b in zip(par, c))
        for k in range(n - 1):
            disks[(p + 1 + k) % n] += chunks[k]
        disks[p] += par
        row += 1
    members = [MemorySource(bytes(d)) for d in disks]
    v = VirtualRAID(members, "5", stripe, "left-symmetric")
    assert v.read(0, len(data)) == data
    v2 = VirtualRAID([members[0], None, members[2], members[3]], "5", stripe, "left-symmetric")
    assert v2.read(0, len(data)) == data


def test_carver_jpeg_png_zip_in_memory():
    from PIL import Image
    import io
    import zipfile
    b1 = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(b1, "JPEG")
    b2 = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(b2, "PNG")
    b3 = io.BytesIO()
    with zipfile.ZipFile(b3, "w") as z:
        z.writestr("[Content_Types].xml", "x")
        z.writestr("xl/workbook.xml", "y")
    blob = bytearray(os.urandom(4096))
    offs = []
    for b in (b1.getvalue(), b2.getvalue(), b3.getvalue()):
        blob += b"\0" * ((-len(blob)) % 512)
        offs.append(len(blob))
        blob += b
        blob += os.urandom(1000)
    src = MemorySource(bytes(blob))
    found = carving.Carver().scan(src)
    exts = {e.ext for e in found}
    assert {"jpg", "png", "xlsx"} <= exts
    jp = next(e for e in found if e.ext == "jpg")
    assert b"".join(iter_content(src, jp)) == b1.getvalue()


def test_repair_jpeg_truncated(tmp_path):
    from PIL import Image
    p = tmp_path / "a.jpg"
    Image.new("RGB", (200, 200), (200, 10, 10)).save(p, "JPEG")
    data = p.read_bytes()
    p.write_bytes(data[:-200])
    out, _ = repair.repair_jpeg(str(p))
    Image.open(out).load()


def _make_raid6(data: bytes, n: int, stripe: int):
    from recuperadatos_mx.core.raid import VirtualRAID, q_syndrome, xor_bytes
    tmp = VirtualRAID([MemorySource(b"\0" * stripe * 4)] * n, "6", stripe, "left-symmetric")
    disks = [bytearray() for _ in range(n)]
    sn = row = 0
    while sn * stripe < len(data):
        p, q, order = tmp._row6(row)
        chunks = []
        for k in range(n - 2):
            chunks.append(data[sn * stripe:(sn + 1) * stripe].ljust(stripe, b"\0"))
            sn += 1
        row_disks = [None] * n
        for k, d in enumerate(order):
            row_disks[d] = chunks[k]
        row_disks[p] = xor_bytes(chunks)
        row_disks[q] = q_syndrome(chunks)
        for d in range(n):
            disks[d] += row_disks[d]
        row += 1
    return [MemorySource(bytes(d)) for d in disks]


@pytest.mark.parametrize("missing", [(0,), (1, 3), (0, 4), (2, 5), (3, 4)])
def test_raid6_double_failure(missing):
    import random
    rnd = random.Random(3)
    stripe, n = 4096, 6
    data = bytes(rnd.randrange(256) for _ in range(stripe * 4 * 7))
    members = _make_raid6(data, n, stripe)
    mem = [None if i in missing else m for i, m in enumerate(members)]
    v = VirtualRAID(mem, "6", stripe, "left-symmetric")
    assert v.read(0, len(data)) == data
