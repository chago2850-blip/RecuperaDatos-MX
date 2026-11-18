# Investigación: EaseUS Data Recovery Wizard, R-Studio, DMDE y Disk Drill Enterprise

Este documento resume cómo funcionan las cuatro aplicaciones de referencia y cómo
RecuperaDatos-MX implementa cada capacidad. Fuentes al final.

## 1. Cómo funcionan (principios comunes)

Las cuatro aplicaciones combinan las mismas técnicas, con distinta profundidad:

1. **Escaneo rápido (lectura de metadatos).** Leen las estructuras del sistema de
   archivos (MFT de NTFS, entradas de directorio FAT/exFAT, inodos de ext, catálogo de
   HFS+/APFS). Un archivo borrado suele seguir registrado ahí, marcado como libre, con su
   nombre, carpeta, fechas y ubicación de sus datos. Es rápido y conserva nombres.
2. **Escaneo profundo / RAW (búsqueda por firmas).** Recorren el disco sector por sector
   buscando cabeceras conocidas (JPEG `FFD8FF`, PDF `%PDF`, ZIP `PK..`). Funciona
   aunque el sistema de archivos esté formateado o destruido, pero pierde nombres y
   carpetas. Disk Drill anuncia unas 400 firmas y EaseUS más de 1000 tipos.
3. **Reconstrucción del sistema de archivos.** DMDE y R-Studio buscan fragmentos de
   estructuras (registros MFT sueltos, superbloques, directorios) para reconstruir el
   árbol de carpetas de volúmenes dañados o reformateados.
4. **Particiones perdidas.** Buscan sectores de arranque y superbloques (y sus copias de
   respaldo) para encontrar volúmenes borrados y restaurarlos en la tabla de particiones.
5. **Herramientas auxiliares.** Imágenes byte a byte con manejo de sectores dañados,
   RAID virtual, editor hexadecimal, S.M.A.R.T., recuperación por red, reparación de
   archivos y protección preventiva (Recovery Vault de Disk Drill).

## 2. Matriz de funciones

| Función | EaseUS | R-Studio | DMDE | Disk Drill Ent. | RecuperaDatos-MX |
|---|:-:|:-:|:-:|:-:|---|
| Escaneo rápido de archivos borrados | ✔ | ✔ | ✔ | ✔ | ✔ NTFS, FAT12/16/32, exFAT, ext2/3/4, HFS+, **APFS, ReFS, Btrfs, XFS** |
| Escaneo profundo por firmas | ✔ | ✔ | ✔ | ✔ | ✔ 70 familias (más de 110 extensiones) con validación estructural |
| Firmas personalizadas | — | ✔ | ✔ | — | ✔ cabecera/pie en hex, desplazamiento, tamaño máx. |
| Reconstrucción NTFS con MFT dañada / formateo | parcial | ✔ | ✔ | parcial | ✔ registros `FILE` huérfanos con nombres y rutas |
| Recuperación ext4 / XFS tras borrado | — | ✔ | ✔ | parcial | ✔ copias de inodos y directorios en el journal (JBD2 y log de XFS) |
| APFS y Btrfs: borrados por copy-on-write | ✔ | ✔ | ✔ | ✔ | ✔ nodos antiguos del árbol, con nombre y carpeta |
| ReFS 3.x | ✔ | ✔ | ✔ | — | ✔ tabla de contenedores, registros incrustados y filas de directorio no indexadas |
| Volúmenes cifrados | BitLocker | BitLocker, FileVault | BitLocker | BitLocker | ✔ **BitLocker** (contraseña, clave de recuperación de 48 dígitos, .BEK; XTS, CBC y difusor de Windows 7), **FileVault/APFS cifrado**, **LUKS1/LUKS2** (PBKDF2 y Argon2) |
| NTFS comprimido, flujos alternos (ADS) | ✔ | ✔ | ✔ | ✔ | ✔ LZNT1 y ADS |
| Estimación de probabilidad de recuperación | — | ✔ | — | ✔ | ✔ bitmap de clústeres + verificación del contenido (detecta TRIM y clústeres reutilizados) |
| Vista previa antes de recuperar | ✔ | ✔ | imágenes | ✔ | ✔ imágenes, PDF, texto, hex y apertura externa |
| Filtros por tipo, nombre, estado, tamaño | ✔ | ✔ | ✔ | ✔ | ✔ y árbol de carpetas reconstruido |
| Guardar y reanudar sesiones de escaneo | — | ✔ | ✔ | ✔ | ✔ `.rdsesion` |
| Búsqueda y restauración de particiones perdidas | ✔ | ✔ | ✔ | ✔ | ✔ NTFS (incl. respaldo), FAT, exFAT, ext, HFS+, APFS, XFS; escritura MBR/GPT |
| Reparación del sector de arranque NTFS | — | — | ✔ | — | ✔ desde la copia de respaldo |
| RAID virtual | — | ✔ 0,1,4,5,6,10,1E… | ✔ 0,1,4,5,6, JBOD | ✔ | ✔ JBOD, 0, 1, 1E, 4, 5, **6 con dos discos faltantes (Reed-Solomon P+Q)**, 10; retardo de paridad |
| Detección automática de parámetros RAID | — | ✔ | ✔ | — | ✔ orden, franja, nivel y esquema |
| Imagen byte a byte con sectores dañados | — | ✔ | ✔ | ✔ | ✔ reintento por sector, salto tras error, copia inversa, reanudación, MD5/SHA-256, partes |
| Clonar disco / restaurar imagen | ✔ (otro producto) | ✔ | ✔ | — | ✔ |
| Imágenes RAW, divididas, VHD, **E01** | ✔ | ✔ | ✔ | ✔ | ✔ `.img/.dd/.raw/.001…/.vhd/.cdr/.E01` (EnCase 1–7, varios segmentos, comprimidos) |
| Editor hexadecimal de disco con plantillas | — | ✔ | ✔ | hex | ✔ MBR, GPT, NTFS, FAT, ext, registro MFT; búsqueda y edición |
| S.M.A.R.T. | — | ✔ | — | ✔ | ✔ ATA y NVMe vía smartctl, alertas y vigilancia |
| Recuperación por red | — | ✔ | — | — | ✔ agente de solo lectura con autenticación HMAC |
| Reparación de fotos, documentos y **video** | ✔ | — | — | — | ✔ JPEG, ZIP/Office/ODF, PDF y **MP4/MOV H.264/H.265** (índice reconstruido con video de referencia) |
| **Medio de arranque** | ✔ | ✔ | ✔ (DOS) | ✔ (macOS) | ✔ ISO «RecuperaDatos-MX Live» (Debian, BIOS/UEFI) y grabador USB con verificación |
| **iPhone / Android** | ✔ (otro producto) | — | — | ✔ | ✔ respaldos de iPhone (también cifrados), papelera y carpetas de Android por ADB, imagen de particiones con root, **mensajes borrados en SQLite** |
| Protección preventiva (Recovery Vault) | — | — | — | ✔ | ✔ Bóveda con ubicación física (FIEMAP / FSCTL / F_LOG2PHYS) |
| Windows, macOS y Linux | Win/Mac | ✔ | ✔ | Win/Mac | ✔ |
| Línea de comandos | — | ✔ (técnico) | ✔ | — | ✔ `recuperadatos-mx` |
| Límite de recuperación | 2 GB gratis | 256 KB gratis | 4000 archivos/dir | 500 MB gratis | **sin límites, GPL-3** |

## 3. Límites que siguen existiendo (también en los programas comerciales)

Estas situaciones no dependen del programa sino de cómo funciona el hardware o el sistema:

* **TRIM en SSD, memorias y discos virtuales:** al borrar, el sistema le indica a la unidad que
  descarte los bloques y quedan en ceros. RecuperaDatos-MX encuentra el nombre (por ejemplo en
  ReFS o NTFS) pero marca el archivo con probabilidad «Baja» porque su contenido ya no existe.
* **Cifrado por hardware de teléfonos:** en iPhone y en Android moderno no es posible leer el
  espacio libre de la memoria interna sin el teléfono desbloqueado y con acceso total; se recupera
  lo que hay en respaldos, en la papelera del sistema, en miniaturas y en registros borrados de las
  bases de datos.
* **Audio de videos sin índice:** en un MP4/MOV cortado se reconstruye la pista de video; el audio
  necesitaría el índice original, que es justamente lo que se perdió.
* **macOS moderno y HFS+:** al borrar, macOS limpia el registro del catálogo; los datos se
  recuperan por firma, sin el nombre original.
* **ReFS 1.x/2.x** (Windows Server 2012) se recupera por firmas; ReFS 3.x se lee completo.
* **BitLocker con TPM únicamente:** la clave está sellada en el chip de esa computadora; se necesita
  la clave de recuperación de 48 dígitos (disponible en la cuenta Microsoft del usuario).
* **Procesador:** la interfaz (Qt 6) requiere un procesador x86-64-v2 (prácticamente cualquier PC de 2009 en adelante).
* **Secure Boot:** el ISO de arranque está basado en Debian; si la computadora tiene Arranque
  seguro estricto puede ser necesario desactivarlo temporalmente.

## 4. Fuentes

* R-Studio: <https://www.r-tt.com/RStudio.shtml>, <https://www.r-tt.com/data_recovery_linux/>,
  <https://www.cleverfiles.com/howto/?p=36494>, <https://datarecoverydigest.com/datarecoverysoftwarereview/advanced-file-recovery-features>
* DMDE: <https://www.dmde.com>, <https://dmde.com/manual/about.html>, <https://dmde.com/docs/dmde-4-4-0-manual.pdf>,
  <https://dmde.com/changelog.html>, <https://www.neowin.net/software/dmde-444842/>
* Disk Drill: <https://www.cleverfiles.com/lp/disk-drill-windows/>, <https://www.cleverfiles.com/?p=20190>,
  <https://blog.7datarecovery.com/?p=2788>, <https://data-recovery.wiki/?p=3353>, <https://www.cleverfiles.com/help/news/page/2>
* EaseUS: <https://easeus.com/data-recovery-wizard/>, <https://www.easeus.com/datarecoverysoftware/>,
  <https://www.cleverfiles.com/howto/easeus-data-recovery-review.html>, <https://techjury.net/reviews/easeus-review/>
