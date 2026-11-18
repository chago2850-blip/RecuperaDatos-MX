"""Construye imágenes de prueba reales (FAT32, exFAT, NTFS, ext4, disco MBR/GPT)
con archivos borrados.  Requiere Linux, root y mkfs.* (se usa en CI y en pruebas locales)."""
from __future__ import annotations

import hashlib
import io
import os
import random
import shutil
import subprocess
import sys
import zipfile

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/rdt"


def sh(*cmd, **kw):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def samples(dirp: str) -> dict:
    from PIL import Image
    os.makedirs(dirp, exist_ok=True)
    rnd = random.Random(7)
    files = {}
    for i in range(3):
        im = Image.new("RGB", (640 + i * 64, 480), (rnd.randrange(256), 90, 160))
        for k in range(300):
            im.putpixel((rnd.randrange(im.width), rnd.randrange(im.height)), (255, 255, 0))
        p = os.path.join(dirp, f"foto_{i}.jpg")
        im.save(p, "JPEG", quality=90)
        files[p] = None
    p = os.path.join(dirp, "grafico.png")
    Image.new("RGBA", (300, 200), (10, 200, 120, 255)).save(p)
    files[p] = None
    pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
           b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
           b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \n"
           b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n160\n%%EOF\n") + b"%" + os.urandom(9000).hex().encode() + b"\n%%EOF\n"
    p = os.path.join(dirp, "informe.pdf")
    open(p, "wb").write(pdf)
    files[p] = None
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document>" + "hola mundo " * 5000 + "</w:document>")
    p = os.path.join(dirp, "Carta muy larga para la abuela.docx")
    open(p, "wb").write(bio.getvalue())
    files[p] = None
    p = os.path.join(dirp, "notas.txt")
    open(p, "w").write("Recupera datos MX\n" * 400)
    files[p] = None
    p = os.path.join(dirp, "grande.bin")
    open(p, "wb").write(os.urandom(3 << 20))
    files[p] = None
    for k in list(files):
        files[k] = hashlib.sha256(open(k, "rb").read()).hexdigest()
    return files


def build_fs(kind: str, files: dict, size_mb: int = 64) -> str:
    img = os.path.join(OUT, f"{kind}.img")
    with open(img, "wb") as fh:
        fh.truncate(size_mb << 20)
    mk = {"fat": ["mkfs.fat", "-F", "32", img], "exfat": ["mkfs.exfat", img],
          "ntfs": ["mkntfs", "-F", "-f", "-q", img], "ext4": ["mkfs.ext4", "-q", "-F", img]}[kind]
    sh(*mk)
    mnt = os.path.join(OUT, "mnt_" + kind)
    os.makedirs(mnt, exist_ok=True)
    if kind == "fat":
        return build_fat_mtools(img, files)
    if kind == "ntfs":
        sh("ntfs-3g", img, mnt)
    elif kind == "exfat":
        loop = subprocess.run(["losetup", "-f", "--show", img], capture_output=True, text=True,
                              check=True).stdout.strip()
        sh("mount.exfat-fuse", loop, mnt)
    else:
        sh("mount", "-o", "loop", img, mnt)
    try:
        os.makedirs(os.path.join(mnt, "Documentos", "Sub"), exist_ok=True)
        for p in files:
            dst = os.path.join(mnt, "Documentos", "Sub" if p.endswith(".jpg") else "", os.path.basename(p))
            shutil.copy(p, dst)
        sh("sync")
        # borrar la mitad
        for p in files:
            base = os.path.basename(p)
            if base.startswith(("foto_1", "informe", "Carta", "grafico")):
                dst = os.path.join(mnt, "Documentos", "Sub" if p.endswith(".jpg") else "", base)
                os.remove(dst)
        sh("sync")
    finally:
        sh("umount", mnt)
        if kind == "exfat":
            sh("losetup", "-d", loop)
    return img


def build_fat_mtools(img: str, files: dict) -> str:
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
    sh("mmd", "-i", img, "::/Documentos", env=env)
    sh("mmd", "-i", img, "::/Documentos/Sub", env=env)
    for p in files:
        dst = "::/Documentos/" + ("Sub/" if p.endswith(".jpg") else "") + os.path.basename(p)
        sh("mcopy", "-i", img, p, dst, env=env)
    for p in files:
        base = os.path.basename(p)
        if base.startswith(("foto_1", "informe", "Carta", "grafico")):
            dst = "::/Documentos/" + ("Sub/" if p.endswith(".jpg") else "") + base
            sh("mdel", "-i", img, dst, env=env)
    return img


def build_disk(files: dict) -> str:
    """Disco MBR con dos particiones (FAT32 + NTFS)."""
    img = os.path.join(OUT, "disco_mbr.img")
    with open(img, "wb") as fh:
        fh.truncate(160 << 20)
    import struct
    mbr = bytearray(512)
    total = (160 << 20) // 512
    for i, (t, lba, cnt) in enumerate(((0x0C, 2048, 131072), (0x07, 133120, total - 133120))):
        struct.pack_into("<B3sB3sII", mbr, 446 + i * 16, 0, b"\xfe\xff\xff", t, b"\xfe\xff\xff", lba, cnt)
    mbr[510:512] = b"\x55\xaa"
    with open(img, "r+b") as fh:
        fh.write(mbr)
    fat = build_fs("fat", files, 64)
    ntfs = build_fs("ntfs", files, 64)
    with open(img, "r+b") as fh:
        fh.seek(2048 * 512)
        fh.write(open(fat, "rb").read())
        fh.seek(133120 * 512)
        fh.write(open(ntfs, "rb").read()[: (160 << 20) - 133120 * 512])
    return img


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    f = samples(os.path.join(OUT, "muestras"))
    for k in ("fat", "exfat", "ntfs", "ext4"):
        print(build_fs(k, f))
    print(build_disk(f))
    with open(os.path.join(OUT, "hashes.txt"), "w") as fh:
        for p, h in f.items():
            fh.write(f"{h}  {os.path.basename(p)}\n")
