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
| Escaneo rápido de archivos borrados | ✔ | ✔ | ✔ | ✔ | ✔ NTFS, FAT12/16/32, exFAT, ext2/3/4, HFS+ |
| Escaneo profundo por firmas | ✔ | ✔ | ✔ | ✔ | ✔ 70 familias (más de 110 extensiones) con validación estructural |
| Firmas personalizadas | — | ✔ | ✔ | — | ✔ cabecera/pie en hex, desplazamiento, tamaño máx. |
| Reconstrucción NTFS con MFT dañada / formateo | parcial | ✔ | ✔ | parcial | ✔ búsqueda de registros `FILE` huérfanos con nombres y rutas |
| Recuperación ext4 tras borrado | — | ✔ | ✔ | parcial | ✔ copias de inodos y directorios en el journal JBD2 |
| NTFS comprimido, flujos alternos (ADS) | ✔ | ✔ | ✔ | ✔ | ✔ LZNT1 y ADS |
| Estimación de probabilidad de recuperación | — | ✔ | — | ✔ | ✔ por bitmap de clústeres / asignación |
| Vista previa antes de recuperar | ✔ | ✔ | imágenes | ✔ | ✔ imágenes, PDF, texto, hex y apertura externa |
| Filtros por tipo, nombre, estado, tamaño | ✔ | ✔ | ✔ | ✔ | ✔ y árbol de carpetas reconstruido |
| Guardar y reanudar sesiones de escaneo | — | ✔ | ✔ | ✔ | ✔ `.rdsesion` |
| Búsqueda y restauración de particiones perdidas | ✔ | ✔ | ✔ | ✔ | ✔ NTFS (incl. respaldo), FAT, exFAT, ext, HFS+, APFS, XFS; escritura MBR/GPT |
| Reparación del sector de arranque NTFS | — | — | ✔ | — | ✔ desde la copia de respaldo |
| RAID virtual | — | ✔ 0,1,4,5,6,10,1E,5E… | ✔ 0,1,4,5,6, JBOD | ✔ | ✔ JBOD, 0, 1, 1E, 4, 5, 6 (XOR), 10; miembros faltantes; retardo de paridad |
| Detección automática de parámetros RAID | — | ✔ | ✔ | — | ✔ orden, franja, nivel y esquema |
| Imagen byte a byte con sectores dañados | — | ✔ | ✔ | ✔ | ✔ reintento por sector, salto tras error, copia inversa, reanudación, MD5/SHA-256, partes |
| Clonar disco / restaurar imagen | ✔ (otro producto) | ✔ | ✔ | — | ✔ |
| Abrir imágenes RAW, divididas, VHD | ✔ | ✔ | ✔ | ✔ | ✔ `.img/.dd/.raw/.001…/.vhd` |
| Editor hexadecimal de disco con plantillas | — | ✔ | ✔ | hex | ✔ MBR, GPT, NTFS, FAT, ext, registro MFT; búsqueda y edición |
| S.M.A.R.T. | — | ✔ | — | ✔ | ✔ ATA y NVMe vía smartctl, alertas y vigilancia |
| Recuperación por red | — | ✔ | — | — | ✔ agente de solo lectura con autenticación HMAC |
| Reparación de fotos y documentos | ✔ | — | — | — | ✔ JPEG (con referencia), ZIP/DOCX/XLSX/PPTX/ODT, PDF |
| Protección preventiva (Recovery Vault) | — | — | — | ✔ | ✔ Bóveda con ubicación física (FIEMAP / FSCTL / F_LOG2PHYS) |
| Windows, macOS y Linux | Win/Mac | ✔ | ✔ | Win/Mac | ✔ |
| Línea de comandos | — | ✔ (técnico) | ✔ | — | ✔ `recuperadatos-mx` |
| Límite de recuperación | 2 GB gratis | 256 KB gratis | 4000 archivos/dir | 500 MB gratis | **sin límites, GPL-3** |

## 3. Lo que todavía no cubre RecuperaDatos-MX 1.0

Para ser transparentes, estas capacidades de los productos comerciales no están
implementadas a nivel de archivos en esta versión (los volúmenes se detectan y sus
archivos se recuperan por firmas):

* Lectura del árbol de archivos de **APFS, ReFS, Btrfs, XFS y UFS** (se detectan y se
  buscan como particiones; su contenido se recupera con el escaneo profundo).
* Volúmenes cifrados (BitLocker, FileVault, LUKS) y archivos cifrados con EFS
  (se recupera el contenido cifrado).
* Imágenes **E01/EWF**, VMDK y VHDX (sí: RAW, divididas y VHD).
* Creación de medios de arranque, recuperación de iPhone/Android y de copias de Time Machine.
* Paridad Reed-Solomon de RAID 6 (dos discos faltantes) y RAID 5E/5EE/6E.
* Hardware especializado (DeepSpar, PC-3000) y reparación de video MP4/MOV.

## 4. Fuentes

* R-Studio: <https://www.r-tt.com/RStudio.shtml>, <https://www.r-tt.com/data_recovery_linux/>,
  <https://www.cleverfiles.com/howto/?p=36494>, <https://datarecoverydigest.com/datarecoverysoftwarereview/advanced-file-recovery-features>
* DMDE: <https://www.dmde.com>, <https://dmde.com/manual/about.html>, <https://dmde.com/docs/dmde-4-4-0-manual.pdf>,
  <https://dmde.com/changelog.html>, <https://www.neowin.net/software/dmde-444842/>
* Disk Drill: <https://www.cleverfiles.com/lp/disk-drill-windows/>, <https://www.cleverfiles.com/?p=20190>,
  <https://blog.7datarecovery.com/?p=2788>, <https://data-recovery.wiki/?p=3353>, <https://www.cleverfiles.com/help/news/page/2>
* EaseUS: <https://easeus.com/data-recovery-wizard/>, <https://www.easeus.com/datarecoverysoftware/>,
  <https://www.cleverfiles.com/howto/easeus-data-recovery-review.html>, <https://techjury.net/reviews/easeus-review/>
