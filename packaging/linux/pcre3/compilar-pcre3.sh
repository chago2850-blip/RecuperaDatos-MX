#!/bin/sh
# Compila libpcre3 (PCRE 8.39 + parches de seguridad de Ubuntu 22.04, 8.39-13ubuntu0.22.04.1)
# desde el código fuente incluido en este directorio. No descarga nada.
#
#   sh packaging/linux/pcre3/compilar-pcre3.sh DESTINO
#
# Deja en DESTINO/lib:
#   libpcre.so.3       (mismo soname y ABI que el paquete libpcre3 de Debian/Ubuntu)
#   libpcreposix.so.3
#
# Solo necesita un compilador de C, make, patch y tar con soporte bzip2.
set -eu

AQUI=$(cd "$(dirname "$0")" && pwd)
DESTINO=$(mkdir -p "${1:?uso: compilar-pcre3.sh DESTINO}" && cd "$1" && pwd)
FUENTE="$AQUI/pcre3_8.39.orig.tar.bz2"
SHA256="b858099f82483031ee02092711689e7245586ada49e534a06e678b8ea9549e8b"

# 1. Verificar el código fuente incluido (mismo hash que el .dsc firmado por Ubuntu)
if command -v sha256sum >/dev/null 2>&1; then
    echo "$SHA256  $FUENTE" | sha256sum -c -
else
    [ "$(shasum -a 256 "$FUENTE" | cut -d' ' -f1)" = "$SHA256" ] || { echo "hash incorrecto" >&2; exit 1; }
fi

TRABAJO="$DESTINO/fuente"
rm -rf "$TRABAJO" "$DESTINO/instalado" "$DESTINO/lib"
mkdir -p "$TRABAJO"
tar -xjf "$FUENTE" -C "$TRABAJO" --strip-components=1

# 2. Aplicar los parches de Debian/Ubuntu (correcciones de CVE incluidas)
while read -r p; do
    case "$p" in ""|\#*) continue ;; esac
    echo "parche: $p"
    patch -d "$TRABAJO" -p1 -s -N < "$AQUI/parches/$p"
done < "$AQUI/parches/series"

# 3. Configurar igual que debian/rules (utf8 + propiedades unicode + JIT), con endurecimiento
cd "$TRABAJO"
CFLAGS="-O2 -g0 -fstack-protector-strong -D_FORTIFY_SOURCE=2 -Wformat -Werror=format-security -fPIC" \
LDFLAGS="-Wl,-z,relro -Wl,-z,now" \
./configure --prefix="$DESTINO/instalado" \
    --enable-utf8 --enable-unicode-properties --enable-jit \
    --enable-shared --disable-static --disable-cpp \
    --disable-pcre16 --disable-pcre32 --disable-pcregrep-libz --disable-pcregrep-libbz2 \
    --disable-silent-rules >/dev/null

# Debian usa la versión de interfaz 16:3:13 -> soname libpcre.so.3 (el original sería libpcre.so.1)
NUCLEOS=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)
make -j"$NUCLEOS" libpcre.la libpcreposix.la \
    EXTRA_LIBPCRE_LDFLAGS="-no-undefined -version-info 16:3:13" \
    EXTRA_LIBPCREPOSIX_LDFLAGS="-no-undefined -version-info 16:3:13" >/dev/null

# 4. Copiar solo las bibliotecas, sin símbolos de depuración
mkdir -p "$DESTINO/lib"
for b in libpcre libpcreposix; do
    real=$(readlink -f ".libs/$b.so.3")
    cp "$real" "$DESTINO/lib/$b.so.3"
    strip --strip-unneeded "$DESTINO/lib/$b.so.3" 2>/dev/null || true
done
# libpcreposix busca libpcre junto a ella
command -v patchelf >/dev/null 2>&1 && patchelf --set-rpath '$ORIGIN' "$DESTINO/lib/libpcreposix.so.3" || true

for b in "$DESTINO"/lib/*.so.3; do
    son=$(readelf -d "$b" | sed -n 's/.*SONAME.*\[\(.*\)\]/\1/p')
    [ "$son" = "$(basename "$b")" ] || { echo "soname inesperado en $b: $son" >&2; exit 1; }
    echo "listo: $b (soname $son)"
done
