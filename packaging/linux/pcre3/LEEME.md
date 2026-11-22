# libpcre3 integrada

RecuperaDatos-MX ya no depende del paquete `libpcre3` del sistema, que fue retirado en
Debian 13 (trixie), Ubuntu 25.04+ y sus derivadas. La biblioteca se compila desde el código
fuente incluido aquí y se mete dentro del ejecutable (`/opt/recuperadatos-mx/_internal/`).

| Archivo | Qué es |
|---|---|
| `pcre3_8.39.orig.tar.bz2` | Código fuente original de PCRE 8.39 (SHA-256 `b858099f…49e8b`, el mismo del paquete de Ubuntu) |
| `parches/` | Parches de Ubuntu `8.39-13ubuntu0.22.04.1`, con las correcciones de CVE-2017-6004, CVE-2017-7186, CVE-2019-20838 y CVE-2020-14155 |
| `compilar-pcre3.sh` | Verifica el hash, aplica los parches y compila `libpcre.so.3` y `libpcreposix.so.3` |
| `copyright` | Licencia de PCRE (BSD), se copia también dentro de la app |

`packaging/build.py` lo ejecuta solo al compilar en Linux. Para compilarla a mano:

```sh
sh packaging/linux/pcre3/compilar-pcre3.sh build/pcre3   # deja build/pcre3/lib/libpcre.so.3
```

Se configura igual que el paquete de Debian (UTF-8, propiedades Unicode y JIT) y con el mismo
soname (`libpcre.so.3`), así que exporta exactamente los mismos símbolos que el `libpcre3` oficial.
Solo requiere un compilador de C, `make`, `patch` y `tar`; no descarga nada.
