"""Construye el ejecutable con PyInstaller y los instaladores de la plataforma actual.

  python packaging/build.py            -> dist/ con el ejecutable + instaladores
  Linux:   .deb, .rpm y .tar.gz (con libpcre3 compilada desde el código fuente e integrada)
  Windows: instalador .exe (Inno Setup) y .zip portátil
  macOS:   .dmg con RecuperaDatos-MX.app
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from recuperadatos_mx import __version__  # noqa: E402

NAME = "RecuperaDatos-MX"
PKG = "recuperadatos-mx"
DIST = os.path.join(ROOT, "dist")
ICONS = os.path.join(ROOT, "packaging", "icons")
ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(),
                                                                                         platform.machine())


def run(*cmd, **kw):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, **kw)


def pyinstaller() -> str:
    sep = ";" if sys.platform == "win32" else ":"
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", NAME,
            "--add-data", f"{os.path.join(ROOT, 'recuperadatos_mx', 'assets')}{sep}recuperadatos_mx/assets",
            "--collect-submodules", "recuperadatos_mx",
            "--exclude-module", "tkinter", "--exclude-module", "PySide6.QtWebEngineCore",
            "--exclude-module", "PySide6.Qt3DCore", "--exclude-module", "PySide6.QtQuick3D",
            "--distpath", os.path.join(ROOT, "build", "pyi-dist"), "--workpath", os.path.join(ROOT, "build", "pyi-work"),
            "--specpath", os.path.join(ROOT, "build")]
    if sys.platform == "win32":
        args += ["--windowed", "--icon", os.path.join(ICONS, f"{PKG}.ico"), "--uac-admin"]
    elif sys.platform == "darwin":
        args += ["--windowed", "--icon", os.path.join(ICONS, f"{PKG}.icns"),
                 "--osx-bundle-identifier", "mx.recuperadatos.app"]
    args.append(os.path.join(ROOT, "packaging", "launcher.py"))
    run(*args)
    out = os.path.join(ROOT, "build", "pyi-dist", NAME + (".app" if sys.platform == "darwin" else ""))
    return out


# --------------------------------------------------------------------------- #
# libpcre3 propia: se compila desde packaging/linux/pcre3 (PCRE 8.39 + parches de
# seguridad de Ubuntu) y se mete dentro del ejecutable, así la app ya no depende del
# paquete libpcre3 del sistema (retirado en Debian 13, Ubuntu 25.04+ y derivadas).
PCRE_DIR = os.path.join(ROOT, "packaging", "linux", "pcre3")
PCRE_LIBS = ("libpcre.so.3", "libpcreposix.so.3")


def compile_pcre3() -> str:
    out = os.path.join(ROOT, "build", "pcre3")
    run("sh", os.path.join(PCRE_DIR, "compilar-pcre3.sh"), out)
    return os.path.join(out, "lib")


def bundle_pcre3(app_dir: str) -> None:
    """Copia la libpcre3 compilada junto a las demás bibliotecas del ejecutable.

    El cargador de PyInstaller pone esa carpeta (``_internal``) en LD_LIBRARY_PATH,
    así que Qt/GLib encuentran esta copia antes que (o en lugar de) la del sistema."""
    libdir = compile_pcre3()
    internal = os.path.join(app_dir, "_internal")
    dest = internal if os.path.isdir(internal) else app_dir
    for lib in PCRE_LIBS:
        target = os.path.join(dest, lib)
        if os.path.lexists(target):  # PyInstaller pudo haber copiado la del sistema
            os.remove(target)
        shutil.copy2(os.path.join(libdir, lib), target)
        print(f"  libpcre3 integrada: {target}")
    lic = os.path.join(dest, "licencias")
    os.makedirs(lic, exist_ok=True)
    shutil.copy(os.path.join(PCRE_DIR, "copyright"), os.path.join(lic, "PCRE-8.39.txt"))


# --------------------------------------------------------------------------- #
DESKTOP = f"""[Desktop Entry]
Type=Application
Name={NAME}
GenericName=Recuperación de datos
Comment=Recupere archivos borrados, particiones perdidas y discos formateados (GPL-3)
Exec=/usr/bin/{PKG} gui %F
Icon={PKG}
Terminal=false
Categories=System;Utility;Filesystem;
Keywords=recuperar;datos;undelete;particiones;raid;forense;
StartupWMClass={NAME}
"""

DESKTOP_ADMIN = f"""[Desktop Entry]
Type=Application
Name={NAME} (administrador)
Comment=Abrir RecuperaDatos-MX con acceso directo a los discos
Exec=pkexec /opt/{PKG}/{NAME} gui
Icon={PKG}
Terminal=false
Categories=System;Utility;Filesystem;
"""

POLKIT = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="mx.recuperadatos.pkexec">
    <description>Ejecutar RecuperaDatos-MX con acceso a los discos</description>
    <message>RecuperaDatos-MX necesita privilegios para leer los discos directamente</message>
    <defaults><allow_any>auth_admin</allow_any><allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin_keep</allow_active></defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/opt/{PKG}/{NAME}</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>
</policyconfig>
"""


def stage_linux(app_dir: str) -> str:
    root = os.path.join(ROOT, "build", "linux-root")
    shutil.rmtree(root, ignore_errors=True)
    opt = os.path.join(root, "opt", PKG)
    shutil.copytree(app_dir, opt, symlinks=True)
    os.makedirs(os.path.join(root, "usr", "bin"))
    with open(os.path.join(root, "usr", "bin", PKG), "w") as fh:
        fh.write(f'#!/bin/sh\nexec /opt/{PKG}/{NAME} "$@"\n')
    os.chmod(os.path.join(root, "usr", "bin", PKG), 0o755)
    apps = os.path.join(root, "usr", "share", "applications")
    os.makedirs(apps)
    open(os.path.join(apps, f"{PKG}.desktop"), "w").write(DESKTOP)
    open(os.path.join(apps, f"{PKG}-admin.desktop"), "w").write(DESKTOP_ADMIN)
    for s in (16, 24, 32, 48, 64, 128, 256, 512):
        d = os.path.join(root, "usr", "share", "icons", "hicolor", f"{s}x{s}", "apps")
        os.makedirs(d)
        shutil.copy(os.path.join(ICONS, f"{PKG}-{s}.png"), os.path.join(d, f"{PKG}.png"))
    pk = os.path.join(root, "usr", "share", "polkit-1", "actions")
    os.makedirs(pk)
    open(os.path.join(pk, "mx.recuperadatos.policy"), "w").write(POLKIT)
    doc = os.path.join(root, "usr", "share", "doc", PKG)
    os.makedirs(doc)
    shutil.copy(os.path.join(ROOT, "LICENSE"), os.path.join(doc, "copyright"))
    shutil.copy(os.path.join(ROOT, "README.md"), os.path.join(doc, "README.md"))
    return root


def du_kb(path: str) -> int:
    total = 0
    for dp, _, fns in os.walk(path):
        for f in fns:
            p = os.path.join(dp, f)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return total // 1024


def build_deb(root: str) -> str:
    arch = {"x86_64": "amd64", "arm64": "arm64"}.get(ARCH, ARCH)
    deb_root = os.path.join(ROOT, "build", "deb-root")
    shutil.rmtree(deb_root, ignore_errors=True)
    shutil.copytree(root, deb_root, symlinks=True)
    os.makedirs(os.path.join(deb_root, "DEBIAN"))
    ctrl = (f"Package: {PKG}\nVersion: {__version__}\nSection: utils\nPriority: optional\nArchitecture: {arch}\n"
            f"Maintainer: Santiago Miguel <chago2850@gmail.com>\nInstalled-Size: {du_kb(deb_root)}\n"
            "Depends: libc6, libgl1, libegl1, libxkbcommon0, libxkbcommon-x11-0, libfontconfig1, libdbus-1-3, "
            "libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-image0, libxcb-render-util0, libxcb-xkb1, "
            "libxcb-randr0, libxcb-shape0, libxcb-xinerama0\n"
            "Recommends: smartmontools, policykit-1 | pkexec\n"
            "Homepage: https://github.com/chago2850-blip/RecuperaDatos-MX\n"
            "Description: Recuperación de datos libre y multiplataforma\n"
            " Recupera archivos borrados, discos formateados, particiones perdidas y RAID dañados.\n"
            " Escaneo rápido y profundo (NTFS, FAT, exFAT, ext2/3/4, HFS+ y más de 70 familias de\n"
            " formatos por firma), imágenes byte a byte, editor de disco, S.M.A.R.T., reparación de\n"
            " archivos, bóveda de protección y recuperación por red. Licencia GPL-3.0 o posterior.\n")
    open(os.path.join(deb_root, "DEBIAN", "control"), "w").write(ctrl)
    post = "#!/bin/sh\nset -e\ncommand -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true\n" \
           "command -v update-desktop-database >/dev/null && update-desktop-database -q || true\n"
    open(os.path.join(deb_root, "DEBIAN", "postinst"), "w").write(post)
    os.chmod(os.path.join(deb_root, "DEBIAN", "postinst"), 0o755)
    out = os.path.join(DIST, f"{PKG}_{__version__}_{arch}.deb")
    run("dpkg-deb", "--root-owner-group", "-Zxz", "--build", deb_root, out)
    return out


def build_rpm(root: str) -> str:
    top = os.path.join(ROOT, "build", "rpmbuild")
    shutil.rmtree(top, ignore_errors=True)
    for d in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS", "BUILDROOT"):
        os.makedirs(os.path.join(top, d))
    files = []
    for dp, dns, fns in os.walk(root):
        for f in fns:
            files.append("/" + os.path.relpath(os.path.join(dp, f), root))
    spec = f"""%global debug_package %{{nil}}
%global __os_install_post %{{nil}}
%define _build_id_links none
AutoReqProv: no
Name: {PKG}
Version: {__version__}
Release: 1
Summary: Recuperación de datos libre y multiplataforma
License: GPL-3.0-or-later
URL: https://github.com/chago2850-blip/RecuperaDatos-MX
Recommends: smartmontools
Requires: libglvnd-egl, libxkbcommon, libxkbcommon-x11, fontconfig, dbus-libs, xcb-util-wm, xcb-util-keysyms, xcb-util-image, xcb-util-renderutil, xcb-util-cursor

%description
Recupera archivos borrados, discos formateados, particiones perdidas y RAID dañados.
Escaneo rápido y profundo, imágenes byte a byte, editor de disco, S.M.A.R.T.,
reparación de archivos, bóveda de protección y recuperación por red.

%install
mkdir -p %{{buildroot}}
cp -a {root}/. %{{buildroot}}/

%post
gtk-update-icon-cache -q /usr/share/icons/hicolor >/dev/null 2>&1 || true

%files
/opt/{PKG}
/usr/bin/{PKG}
/usr/share/applications/{PKG}.desktop
/usr/share/applications/{PKG}-admin.desktop
/usr/share/icons/hicolor/*/apps/{PKG}.png
/usr/share/polkit-1/actions/mx.recuperadatos.policy
/usr/share/doc/{PKG}
"""
    sp = os.path.join(top, "SPECS", f"{PKG}.spec")
    open(sp, "w").write(spec)
    run("rpmbuild", "-bb", "--define", f"_topdir {top}", "--target", ARCH if ARCH != "arm64" else "aarch64", sp)
    for dp, _, fns in os.walk(os.path.join(top, "RPMS")):
        for f in fns:
            if f.endswith(".rpm"):
                dst = os.path.join(DIST, f)
                shutil.copy(os.path.join(dp, f), dst)
                return dst
    raise RuntimeError("rpmbuild no generó paquete")


def build_tar(app_dir: str) -> str:
    out = os.path.join(DIST, f"{NAME}-{__version__}-linux-{ARCH}.tar.gz")
    with tarfile.open(out, "w:gz") as t:
        t.add(app_dir, arcname=f"{NAME}-{__version__}")
    return out


def build_windows(app_dir: str) -> list:
    outs = []
    z = os.path.join(DIST, f"{NAME}-{__version__}-windows-portable.zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for dp, _, fns in os.walk(app_dir):
            for f in fns:
                p = os.path.join(dp, f)
                zf.write(p, os.path.join(NAME, os.path.relpath(p, app_dir)))
    outs.append(z)
    iscc = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if os.path.exists(iscc):
        run(iscc, f"/DAppVersion={__version__}", f"/DSourceDir={app_dir}", f"/DOutDir={DIST}",
            f"/DIconFile={os.path.join(ICONS, PKG + '.ico')}", f"/DLicenseFile={os.path.join(ROOT, 'LICENSE')}",
            os.path.join(ROOT, "packaging", "windows", "recuperadatos-mx.iss"))
        outs.append(os.path.join(DIST, f"{NAME}-{__version__}-windows-setup.exe"))
    return outs


def build_macos(app: str) -> str:
    out = os.path.join(DIST, f"{NAME}-{__version__}-macos-{ARCH}.dmg")
    stage = os.path.join(ROOT, "build", "dmg")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    run("cp", "-R", app, stage)
    os.symlink("/Applications", os.path.join(stage, "Aplicaciones"))
    run("hdiutil", "create", "-volname", NAME, "-srcfolder", stage, "-ov", "-format", "UDZO", out)
    return out


def main() -> None:
    os.makedirs(DIST, exist_ok=True)
    app = pyinstaller()
    outs = []
    if sys.platform.startswith("linux"):
        bundle_pcre3(app)
        root = stage_linux(app)
        outs += [build_deb(root), build_rpm(root), build_tar(app)]
    elif sys.platform == "win32":
        outs += build_windows(app)
    elif sys.platform == "darwin":
        outs.append(build_macos(app))
    print("\nGenerados:")
    for o in outs:
        print("  ", o)


if __name__ == "__main__":
    main()
