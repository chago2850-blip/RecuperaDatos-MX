"""Genera los archivos de muestra y sus huellas (usado por el flujo datos-prueba)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from build_images import samples  # noqa: E402

out = sys.argv[1]
files = samples(os.path.join(out, "muestras"))
with open(os.path.join(out, "hashes.txt"), "w", encoding="utf-8") as fh:
    for p, h in files.items():
        fh.write(f"{h}  {os.path.basename(p)}\n")
print("\n".join(files))
