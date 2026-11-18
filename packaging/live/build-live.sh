#!/bin/sh
# Construye RecuperaDatos-MX Live (Debian 12 + XFCE + RecuperaDatos-MX), ISO híbrido BIOS/UEFI.
# Uso (como root en Debian):  sh packaging/live/build-live.sh ruta/al/recuperadatos-mx.deb VERSION
set -e
DEB="$1"; VER="${2:-dev}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-/tmp/rdmx-live}"
rm -rf "$WORK"; mkdir -p "$WORK"; cd "$WORK"
lb config \
  --distribution bookworm --archive-areas "main contrib non-free non-free-firmware" \
  --debian-installer none --binary-images iso-hybrid --bootloaders "grub-efi,syslinux" \
  --iso-application "RecuperaDatos-MX Live" --iso-volume "RECUPERADATOS" \
  --iso-publisher "Santiago Miguel" --memtest none \
  --bootappend-live "boot=live components quiet splash locales=es_MX.UTF-8 keyboard-layouts=latam timezone=America/Mexico_City"
cp -r "$HERE/config/." config/
mkdir -p config/packages.chroot
cp "$DEB" config/packages.chroot/
lb build
mv live-image-amd64.hybrid.iso "/tmp/RecuperaDatos-MX-$VER-live-amd64.iso"
ls -la /tmp/*.iso
