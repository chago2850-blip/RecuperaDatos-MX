"""Plantillas del editor de disco: interpretan estructuras conocidas en un sector."""
from __future__ import annotations

import struct
import uuid
from typing import List, Tuple

Field = Tuple[int, int, str, str]  # desplazamiento, longitud, nombre, valor


def _u(b, o, n):
    return int.from_bytes(b[o:o + n], "little")


def mbr(b: bytes) -> List[Field]:
    out = [(440, 4, "Firma del disco", f"0x{_u(b, 440, 4):08X}")]
    for i in range(4):
        o = 446 + i * 16
        out += [(o, 1, f"P{i + 1} activa", "sí" if b[o] == 0x80 else "no"),
                (o + 4, 1, f"P{i + 1} tipo", f"0x{b[o + 4]:02X}"),
                (o + 8, 4, f"P{i + 1} LBA inicial", str(_u(b, o + 8, 4))),
                (o + 12, 4, f"P{i + 1} sectores", str(_u(b, o + 12, 4)))]
    out.append((510, 2, "Firma", "55AA ✓" if b[510:512] == b"\x55\xAA" else b[510:512].hex()))
    return out


def gpt_header(b: bytes) -> List[Field]:
    return [(0, 8, "Firma", b[:8].decode("ascii", "replace")),
            (8, 4, "Revisión", f"0x{_u(b, 8, 4):08X}"), (16, 4, "CRC32 cabecera", f"0x{_u(b, 16, 4):08X}"),
            (24, 8, "LBA actual", str(_u(b, 24, 8))), (32, 8, "LBA de respaldo", str(_u(b, 32, 8))),
            (40, 8, "Primer LBA utilizable", str(_u(b, 40, 8))), (48, 8, "Último LBA utilizable", str(_u(b, 48, 8))),
            (56, 16, "GUID del disco", str(uuid.UUID(bytes_le=bytes(b[56:72])))),
            (72, 8, "LBA de entradas", str(_u(b, 72, 8))), (80, 4, "Número de entradas", str(_u(b, 80, 4))),
            (84, 4, "Tamaño de entrada", str(_u(b, 84, 4)))]


def ntfs_boot(b: bytes) -> List[Field]:
    return [(3, 8, "OEM", b[3:11].decode("ascii", "replace")), (11, 2, "Bytes por sector", str(_u(b, 11, 2))),
            (13, 1, "Sectores por clúster", str(b[13])), (40, 8, "Sectores totales", str(_u(b, 40, 8))),
            (48, 8, "Clúster de $MFT", str(_u(b, 48, 8))), (56, 8, "Clúster de $MFTMirr", str(_u(b, 56, 8))),
            (64, 1, "Tamaño de registro MFT", str(struct.unpack_from("<b", b, 64)[0])),
            (72, 8, "Número de serie", f"{_u(b, 72, 8):016X}"),
            (510, 2, "Firma", "55AA ✓" if b[510:512] == b"\x55\xAA" else b[510:512].hex())]


def fat_boot(b: bytes) -> List[Field]:
    fat32 = b[82:87] == b"FAT32"
    out = [(3, 8, "OEM", b[3:11].decode("ascii", "replace")), (11, 2, "Bytes por sector", str(_u(b, 11, 2))),
           (13, 1, "Sectores por clúster", str(b[13])), (14, 2, "Sectores reservados", str(_u(b, 14, 2))),
           (16, 1, "Número de FAT", str(b[16])), (19, 2, "Sectores totales (16)", str(_u(b, 19, 2))),
           (32, 4, "Sectores totales (32)", str(_u(b, 32, 4)))]
    if fat32:
        out += [(36, 4, "Sectores por FAT", str(_u(b, 36, 4))), (44, 4, "Clúster raíz", str(_u(b, 44, 4))),
                (71, 11, "Etiqueta", b[71:82].decode("ascii", "replace"))]
    else:
        out += [(22, 2, "Sectores por FAT", str(_u(b, 22, 2))), (17, 2, "Entradas raíz", str(_u(b, 17, 2))),
                (43, 11, "Etiqueta", b[43:54].decode("ascii", "replace"))]
    return out


def ext_super(b: bytes) -> List[Field]:
    o = 1024 if len(b) >= 2048 and _u(b, 1024 + 56, 2) == 0xEF53 else 0
    return [(o + 0, 4, "Inodos", str(_u(b, o, 4))), (o + 4, 4, "Bloques", str(_u(b, o + 4, 4))),
            (o + 24, 4, "Tamaño de bloque", str(1024 << _u(b, o + 24, 4))),
            (o + 32, 4, "Bloques por grupo", str(_u(b, o + 32, 4))),
            (o + 40, 4, "Inodos por grupo", str(_u(b, o + 40, 4))),
            (o + 56, 2, "Firma", f"0x{_u(b, o + 56, 2):04X}"), (o + 88, 2, "Tamaño de inodo", str(_u(b, o + 88, 2))),
            (o + 120, 16, "Etiqueta", b[o + 120:o + 136].split(b"\0")[0].decode("utf-8", "replace"))]


def mft_record(b: bytes) -> List[Field]:
    return [(0, 4, "Firma", b[:4].decode("ascii", "replace")), (16, 2, "Secuencia", str(_u(b, 16, 2))),
            (18, 2, "Enlaces", str(_u(b, 18, 2))), (20, 2, "Primer atributo", str(_u(b, 20, 2))),
            (22, 2, "Indicadores", {0: "borrado", 1: "en uso", 2: "dir. borrado", 3: "dir. en uso"}.get(_u(b, 22, 2), "?")),
            (24, 4, "Tamaño usado", str(_u(b, 24, 4))), (32, 8, "Registro base", str(_u(b, 32, 6))),
            (44, 4, "Número de registro", str(_u(b, 44, 4)))]


TEMPLATES = {"Automática": None, "MBR": mbr, "Cabecera GPT": gpt_header, "Arranque NTFS": ntfs_boot,
             "Arranque FAT": fat_boot, "Superbloque ext": ext_super, "Registro MFT": mft_record}


def auto(b: bytes, lba: int) -> Tuple[str, List[Field]]:
    if b[:4] == b"FILE":
        return "Registro MFT", mft_record(b)
    if b[:8] == b"EFI PART":
        return "Cabecera GPT", gpt_header(b)
    if b[3:11] == b"NTFS    ":
        return "Arranque NTFS", ntfs_boot(b)
    if b[510:512] == b"\x55\xAA" and (b[82:87] == b"FAT32" or b[54:57] == b"FAT"):
        return "Arranque FAT", fat_boot(b)
    if len(b) >= 1024 + 58 and _u(b, 1024 + 56, 2) == 0xEF53:
        return "Superbloque ext", ext_super(b)
    if b[510:512] == b"\x55\xAA" and lba == 0:
        return "MBR", mbr(b)
    return "", []
