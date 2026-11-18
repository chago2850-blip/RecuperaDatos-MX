"""Pruebas con imágenes reales generadas en Linux, macOS y Windows (flujo «Datos de prueba»):
XFS, Btrfs, LUKS1/2, APFS con FileVault, HFS+, ReFS, BitLocker y E01.

Descarga de los datos:  python tests/get_test_data.py /tmp/dp
Ejecución:              RD_DATOS=/tmp/dp python -m pytest tests/test_formatos.py
"""
import hashlib
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recuperadatos_mx.core.device import open_image  # noqa: E402
from recuperadatos_mx.core.model import iter_content  # noqa: E402
from recuperadatos_mx.core.scanner import ScanOptions, find_encrypted, scan  # noqa: E402

D = os.environ.get("RD_DATOS", "/tmp/dp")
BORRADOS = {"foto_1.jpg", "informe.pdf", "Carta muy larga para la abuela.docx", "grafico.png"}
VIVOS = {"foto_0.jpg", "foto_2.jpg", "notas.txt", "grande.bin"}


def hashes(folder):
    out = {}
    for line in open(os.path.join(D, folder, "hashes.txt"), encoding="utf-8"):
        h, n = line.rstrip("\r\n").split("  ", 1)
        out[n] = h
    return out


def check(img, folder, passwords=(), want_deleted=BORRADOS, deep=False):
    path = os.path.join(D, folder, img)
    if not os.path.exists(path):
        pytest.skip(f"falta {path}")
    src = open_image(path)
    res = scan(src, ScanOptions(deep=deep, carving=deep, passwords=list(passwords)))
    H = hashes(folder)
    ok_live, ok_del = set(), set()
    for e in res.entries:
        if e.is_dir or H.get(e.name) is None:
            continue
        h = hashlib.sha256(b"".join(iter_content(src, e))).hexdigest()
        if h == H[e.name]:
            (ok_del if e.deleted else ok_live).add(e.name)
    assert VIVOS <= ok_live, f"{img}: vivos {ok_live}"
    assert want_deleted <= ok_del, f"{img}: borrados {ok_del}"
    return res


def test_xfs():
    check("xfs.img", "lin")


def test_btrfs():
    check("btrfs.img", "lin")


@pytest.mark.parametrize("img", ["luks1.img", "luks2.img"])
def test_luks(img):
    check(img, "lin", ["equivocada", "clave123"])


def test_luks_sin_clave_queda_bloqueado():
    p = os.path.join(D, "lin", "luks1.img")
    if not os.path.exists(p):
        pytest.skip("sin datos")
    res = scan(open_image(p), ScanOptions())
    assert res.locked


def test_apfs_y_filevault():
    res = check("apfs.cdr", "mac", ["clave123"])
    assert any("Secreto" in e.path and e.deleted for e in res.entries)


def test_hfs_vivos():
    check("hfs.cdr", "mac", want_deleted=set())


def test_bitlocker_contrasena_y_clave_recuperacion():
    check("bitlocker.vhd", "win", ["Clave12345!"])
    claves = open(os.path.join(D, "win", "bitlocker_claves.txt"), encoding="utf-8", errors="ignore").read()
    import re
    rp = re.search(r"(\d{6}-){7}\d{6}", claves)
    if rp:
        check("bitlocker.vhd", "win", [rp.group(0)])


def test_refs():
    # Windows aplica TRIM a los clústeres liberados de ReFS: los borrados se identifican por nombre
    res = check("refs.vhd", "win", want_deleted=set())
    names = {e.name for e in res.entries if e.deleted}
    assert BORRADOS <= names


def test_encrypted_detection():
    for img, folder, kind in (("bitlocker.vhd", "win", "BitLocker"), ("luks2.img", "lin", "LUKS"),
                              ("apfs.cdr", "mac", "FileVault")):
        p = os.path.join(D, folder, img)
        if os.path.exists(p):
            assert any(k == kind for _, k in find_encrypted(open_image(p)))


@pytest.mark.skipif(not shutil.which("ewfacquire"), reason="sin ewf-tools")
def test_e01(tmp_path):
    raw = tmp_path / "disco.img"
    raw.write_bytes(os.urandom(3 << 20) + b"\0" * (5 << 20))
    subprocess.run(["ewfacquire", "-u", "-t", str(tmp_path / "disco"), "-f", "encase6", "-c", "deflate:fast",
                    "-S", "1MiB", str(raw)], check=True, capture_output=True)
    src = open_image(str(tmp_path / "disco.E01"))
    assert src.read(0, src.size) == raw.read_bytes()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="sin ffmpeg")
@pytest.mark.parametrize("codec", ["libx264", "libx265"])
def test_video_repair(tmp_path, codec):
    from recuperadatos_mx.core.repair import repair_video
    tag = ["-tag:v", "hvc1"] if codec == "libx265" else []
    for name, src, secs in (("clip", "testsrc", 4), ("ref", "testsrc2", 2)):
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}=size=320x240:rate=30",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(secs), "-c:v", codec, "-g", "30",
                            "-pix_fmt", "yuv420p", *tag, "-c:a", "aac", str(tmp_path / f"{name}.mp4")],
                           capture_output=True)
        if r.returncode:
            pytest.skip(f"ffmpeg sin {codec}")
    data = (tmp_path / "clip.mp4").read_bytes()
    cut = data.rfind(b"moov") - 4
    (tmp_path / "roto.mp4").write_bytes(data[:cut])
    out, note = repair_video(str(tmp_path / "roto.mp4"), str(tmp_path / "ref.mp4"))
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", out], capture_output=True, text=True)
    assert int(r.stdout.strip().split(",")[0]) >= 110
