"""Salud de discos S.M.A.R.T. a través de smartctl (smartmontools)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Optional

CRITICAL = {5: "Sectores reasignados", 187: "Errores no corregibles", 188: "Tiempo de espera de comandos",
            197: "Sectores pendientes", 198: "Sectores incorregibles sin conexión", 10: "Reintentos de giro",
            196: "Eventos de reasignación", 184: "Errores de extremo a extremo"}


def smartctl_path() -> Optional[str]:
    p = shutil.which("smartctl")
    if p:
        return p
    for c in (r"C:\Program Files\smartmontools\bin\smartctl.exe", "/usr/local/sbin/smartctl",
              "/opt/homebrew/sbin/smartctl", "/usr/sbin/smartctl",
              os.path.join(os.path.dirname(sys.executable), "smartctl.exe")):
        if os.path.exists(c):
            return c
    return None


def device_arg(path: str) -> str:
    m = re.search(r"PhysicalDrive(\d+)", path)
    if m:
        return f"/dev/pd{m.group(1)}"
    return path.replace("/dev/rdisk", "/dev/disk")


def read(path: str) -> dict:
    exe = smartctl_path()
    if not exe:
        return {"error": "No se encontró smartctl. Instale smartmontools (Linux: paquete smartmontools; "
                         "macOS: brew install smartmontools; Windows: smartmontools.org)."}
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        out = subprocess.run([exe, "-j", "-a", device_arg(path)], capture_output=True, text=True,
                             timeout=60, **kw).stdout
        data = json.loads(out or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"error": f"smartctl falló: {exc}"}
    return summarize(data)


def summarize(d: dict) -> dict:
    res = {"modelo": d.get("model_name") or d.get("model_family", ""), "serie": d.get("serial_number", ""),
           "firmware": d.get("firmware_version", ""), "capacidad": d.get("user_capacity", {}).get("bytes", 0),
           "temperatura": d.get("temperature", {}).get("current"),
           "horas": d.get("power_on_time", {}).get("hours"),
           "ciclos": d.get("power_cycle_count"),
           "aprobado": d.get("smart_status", {}).get("passed"),
           "atributos": [], "alertas": [], "salud": 100}
    msgs = d.get("smartctl", {}).get("messages", [])
    if msgs and not d.get("smart_status"):
        res["error"] = "; ".join(m.get("string", "") for m in msgs)
    for a in d.get("ata_smart_attributes", {}).get("table", []):
        raw = a.get("raw", {}).get("value", 0)
        res["atributos"].append({"id": a.get("id"), "nombre": a.get("name"), "valor": a.get("value"),
                                 "peor": a.get("worst"), "umbral": a.get("thresh"), "bruto": raw,
                                 "falla": a.get("when_failed", "")})
        if a.get("id") in CRITICAL and raw:
            res["alertas"].append(f"{CRITICAL[a['id']]}: {raw}")
            res["salud"] -= min(40, 5 + int(raw) // 2)
        if a.get("when_failed"):
            res["alertas"].append(f"{a.get('name')}: falló ({a['when_failed']})")
            res["salud"] -= 30
        if a.get("id") in (177, 231, 233) and a.get("value") is not None:
            res["salud"] = min(res["salud"], int(a["value"]))
    nv = d.get("nvme_smart_health_information_log")
    if nv:
        used = nv.get("percentage_used", 0)
        res["salud"] = min(res["salud"], 100 - used)
        for k, n in (("available_spare", "Reserva disponible %"), ("percentage_used", "Desgaste %"),
                     ("data_units_written", "Unidades escritas"), ("media_errors", "Errores de medio"),
                     ("unsafe_shutdowns", "Apagados inseguros"), ("critical_warning", "Aviso crítico")):
            if k in nv:
                res["atributos"].append({"id": "", "nombre": n, "valor": nv[k], "peor": "", "umbral": "",
                                         "bruto": nv[k], "falla": ""})
        if nv.get("media_errors"):
            res["alertas"].append(f"Errores de medio: {nv['media_errors']}")
            res["salud"] -= 20
        if nv.get("critical_warning"):
            res["alertas"].append("El controlador NVMe reporta un aviso crítico")
            res["salud"] -= 40
    if res["aprobado"] is False:
        res["alertas"].insert(0, "La autoevaluación S.M.A.R.T. FALLÓ: copie sus datos de inmediato")
        res["salud"] = min(res["salud"], 10)
    t = res.get("temperatura")
    if t and t >= 60:
        res["alertas"].append(f"Temperatura alta: {t} °C")
    res["salud"] = max(0, min(100, res["salud"]))
    return res
