"""Interfaz de línea de comandos de RecuperaDatos-MX.

Ejemplos:
  recuperadatos-mx listar
  recuperadatos-mx escanear /dev/sdb --profundo --sesion usb.rdsesion
  recuperadatos-mx recuperar usb.rdsesion /mnt/destino --borrados --tipo Imágenes
  recuperadatos-mx imagen /dev/sdb disco.img --inverso
  recuperadatos-mx particiones /dev/sdb --buscar
  recuperadatos-mx smart /dev/sda
  recuperadatos-mx agente --clave secreta
  recuperadatos-mx reparar foto.jpg --referencia buena.jpg
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import time

from . import __app_name__, __version__


def _bar(stage, done, total):
    pct = 100 * done / total if total else 0
    sys.stderr.write(f"\r{stage[:48]:<48} {pct:6.2f}%")
    sys.stderr.flush()


def _open(path: str):
    from .core.device import FileSource, open_image
    if path.startswith(("/dev/", "\\\\.\\")):
        return FileSource(path)
    return open_image(path)


def cmd_listar(a):
    from .core.device import human, list_devices
    for d in list_devices():
        print(f"{d.path:<28} {human(d.size):>12}  {d.model}")
        for c in d.children:
            print(f"  └ {c.path:<24} {human(c.size):>12}  {c.fs} {c.mountpoint}")


def cmd_escanear(a):
    from .core import session
    from .core.scanner import ScanOptions, scan
    src = _open(a.origen)
    opts = ScanOptions(deep=a.profundo, carving=not a.sin_firmas, aligned=not a.no_alineado)
    res = scan(src, opts, progress=_bar)
    sys.stderr.write("\n")
    for line in res.log:
        print(line)
    files = [e for e in res.entries if not e.is_dir]
    print(f"Archivos encontrados: {len(files)} · borrados: {sum(e.deleted for e in files)} · "
          f"por firma: {sum(e.origin == 'raw' for e in files)}")
    if a.sesion:
        session.save(a.sesion, src.describe(), res.entries, res.partitions)
        print(f"Sesión guardada en {a.sesion}")
    if a.listar:
        for e in files:
            print(f"{'B' if e.deleted else ' '} {e.size:>12} {e.chance:<9} {e.full_path}")


def cmd_recuperar(a):
    from .core import session
    from .core.recover import recover
    desc, entries, _ = session.load(a.sesion)
    src = _open(a.origen) if a.origen else session.reopen_source(desc)
    sel = [e for e in entries if not e.is_dir]
    if a.borrados:
        sel = [e for e in sel if e.deleted or e.origin != "fs"]
    if a.tipo:
        sel = [e for e in sel if e.category == a.tipo]
    if a.patron:
        sel = [e for e in sel if fnmatch.fnmatch(e.name.lower(), a.patron.lower())]
    res = recover(src, sel, a.destino, keep_structure=not a.plano,
                  progress=lambda d, t, n: _bar(n, d, t))
    sys.stderr.write("\n")
    print(res["resumen"])


def cmd_imagen(a):
    from .core.imaging import create_image
    src = _open(a.origen)
    res = create_image(src, a.destino, split_size=int(a.dividir * (1 << 30)) if a.dividir else 0,
                       reverse=a.inverso, block=a.bloque << 10,
                       progress=lambda d, t, i: _bar(f"Copiando ({i['malos']} malos)", d, t))
    sys.stderr.write("\n")
    print(json.dumps(res, indent=2, ensure_ascii=False))


def cmd_particiones(a):
    from .core.device import human
    from .core.partitions import read_table, search_lost
    src = _open(a.origen)
    parts = search_lost(src, progress=lambda d, t: _bar("Buscando particiones", d, t), quick=not a.completo) if a.buscar else read_table(src)
    if a.buscar:
        sys.stderr.write("\n")
    for p in parts:
        print(f"#{p.index} {p.scheme:<10} inicio={p.start:>14} tamaño={human(p.size):>10} {p.fs or p.type_name} "
              f"{p.label} {p.confidence}")


def cmd_smart(a):
    from .core.smart import read
    print(json.dumps(read(a.dispositivo), indent=2, ensure_ascii=False))


def cmd_agente(a):
    from .core.netagent import run_agent
    print(f"Agente de recuperación escuchando en {a.host}:{a.puerto} (Ctrl+C para salir)")
    run_agent(a.host, a.puerto, a.clave)


def cmd_reparar(a):
    from .core.repair import repair
    out, note = repair(a.archivo, a.referencia)
    print(f"{out}: {note}")


def cmd_boveda(a):
    from .core import vault
    v = vault.Vault()
    if a.agregar:
        v.add_folder(a.agregar)
    if a.vigilar:
        vault.watch(a.intervalo)
        return
    print(json.dumps(v.index(), ensure_ascii=False))
    for dev, e in v.deleted_entries()[:50]:
        print(f"{e.full_path}  ({e.size} bytes, {e.note})")


def cmd_gui(a):
    from .gui.app import main
    main()


def main(argv=None):
    p = argparse.ArgumentParser(prog="recuperadatos-mx", description=f"{__app_name__} {__version__} — "
                                "recuperación de datos libre (GPL-3.0-o-posterior)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("listar", help="Listar discos y particiones").set_defaults(fn=cmd_listar)
    s = sub.add_parser("escanear", help="Escanear un disco, partición o imagen")
    s.add_argument("origen")
    s.add_argument("--profundo", action="store_true", help="Particiones perdidas, MFT huérfana y firmas")
    s.add_argument("--sin-firmas", action="store_true")
    s.add_argument("--no-alineado", action="store_true", help="Buscar firmas en cualquier byte (más lento)")
    s.add_argument("--sesion", help="Guardar la sesión de escaneo (.rdsesion)")
    s.add_argument("--listar", action="store_true", help="Imprimir todos los archivos")
    s.set_defaults(fn=cmd_escanear)
    s = sub.add_parser("recuperar", help="Recuperar archivos de una sesión")
    s.add_argument("sesion")
    s.add_argument("destino")
    s.add_argument("--origen", help="Ruta del origen si cambió")
    s.add_argument("--borrados", action="store_true", help="Solo borrados / perdidos")
    s.add_argument("--tipo", help="Categoría (Imágenes, Videos, Documentos, …)")
    s.add_argument("--patron", help="Patrón de nombre, p. ej. *.docx")
    s.add_argument("--plano", action="store_true", help="No conservar la estructura de carpetas")
    s.set_defaults(fn=cmd_recuperar)
    s = sub.add_parser("imagen", help="Copia byte a byte a un archivo de imagen")
    s.add_argument("origen")
    s.add_argument("destino")
    s.add_argument("--inverso", action="store_true")
    s.add_argument("--bloque", type=int, default=1024, help="KiB por lectura")
    s.add_argument("--dividir", type=float, default=0, help="Dividir en partes de N GiB")
    s.set_defaults(fn=cmd_imagen)
    s = sub.add_parser("particiones", help="Mostrar tabla o buscar particiones perdidas")
    s.add_argument("origen")
    s.add_argument("--buscar", action="store_true")
    s.add_argument("--completo", action="store_true", help="Revisar cada sector")
    s.set_defaults(fn=cmd_particiones)
    s = sub.add_parser("smart", help="Salud S.M.A.R.T.")
    s.add_argument("dispositivo")
    s.set_defaults(fn=cmd_smart)
    s = sub.add_parser("agente", help="Agente para recuperación por red")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--puerto", type=int, default=7878)
    s.add_argument("--clave", required=True)
    s.set_defaults(fn=cmd_agente)
    s = sub.add_parser("reparar", help="Reparar JPEG, PDF, ZIP/Office")
    s.add_argument("archivo")
    s.add_argument("--referencia")
    s.set_defaults(fn=cmd_reparar)
    s = sub.add_parser("boveda", help="Bóveda de recuperación (protección)")
    s.add_argument("--agregar")
    s.add_argument("--vigilar", action="store_true")
    s.add_argument("--intervalo", type=int, default=600)
    s.set_defaults(fn=cmd_boveda)
    sub.add_parser("gui", help="Abrir la interfaz gráfica").set_defaults(fn=cmd_gui)
    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        return cmd_gui(a)
    return a.fn(a)


if __name__ == "__main__":
    main()
