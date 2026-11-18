"""Descarga las imágenes de prueba publicadas por el flujo «Datos de prueba»."""
import os
import sys
import urllib.request
import zipfile

dest = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dp"
base = "https://github.com/chago2850-blip/RecuperaDatos-MX/releases/download"
for so, carpeta in (("linux", "lin"), ("macos", "mac"), ("windows", "win")):
    z = os.path.join(dest, f"{so}.zip")
    os.makedirs(os.path.join(dest, carpeta), exist_ok=True)
    urllib.request.urlretrieve(f"{base}/datos-prueba-{so}/datos-{so}.zip", z)
    zipfile.ZipFile(z).extractall(os.path.join(dest, carpeta))
    os.remove(z)
    print(so, "listo")
