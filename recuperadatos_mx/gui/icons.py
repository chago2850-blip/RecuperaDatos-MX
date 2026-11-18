"""Iconos lineales propios (SVG) coloreados en tiempo de ejecución."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_P = {
    "home": '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    "disk": '<rect x="3" y="5" width="18" height="14" rx="3"/><circle cx="8" cy="12" r="2.5"/><path d="M14 10h4M14 14h4"/>',
    "scan": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l5 5"/>',
    "deep": '<circle cx="11" cy="11" r="6.5"/><circle cx="11" cy="11" r="2.5"/><path d="M16 16l5 5"/>',
    "results": '<path d="M4 6h16M4 12h16M4 18h10"/>',
    "partition": '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5V12l6 6"/>',
    "raid": '<rect x="3" y="4" width="7" height="7" rx="1.5"/><rect x="14" y="4" width="7" height="7" rx="1.5"/><rect x="3" y="13" width="7" height="7" rx="1.5"/><rect x="14" y="13" width="7" height="7" rx="1.5"/>',
    "image": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/><path d="M15 16l2 2 3-4"/>',
    "hex": '<path d="M8 4L3 12l5 8M16 4l5 8-5 8M13.5 6l-3 12"/>',
    "health": '<path d="M3 12h4l2-5 4 10 2-5h6"/>',
    "repair": '<path d="M14.5 5.5a4 4 0 0 0 4.9 4.9l-9.9 9.9a2 2 0 0 1-2.8-2.8l9.9-9.9a4 4 0 0 0-2.1-2.1z"/>',
    "vault": '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="12" cy="12" r="3.5"/><path d="M12 8.5V7M12 17v-1.5M15.5 12H17M7 12h1.5"/>',
    "network": '<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5L6.5 17M12 11.5l5.5 5.5"/>',
    "signature": '<path d="M4 7V4h16v3M9 20h6M12 4v16"/>',
    "about": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    "open": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "save": '<path d="M5 3h11l3 3v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M7 3v5h8V3M7 21v-7h10v7"/>',
    "recover": '<path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    "shield": '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "down": '<path d="M12 5v14M6 13l6 6 6-6"/>',
    "play": '<path d="M7 4l13 8-13 8z"/>',
    "find": '<circle cx="10" cy="10" r="6"/><path d="M14.5 14.5L20 20"/>',
    "left": '<path d="M15 5l-7 7 7 7"/>',
    "right": '<path d="M9 5l7 7-7 7"/>',
    "first": '<path d="M17 5l-7 7 7 7M7 5v14"/>',
    "last": '<path d="M7 5l7 7-7 7M17 5v14"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 7l4 4"/>',
    "theme": '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    "admin": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "export": '<path d="M12 15V3M7 8l5-5 5 5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
}


@lru_cache(maxsize=512)
def icon(name: str, color: str = "#CFE3F5", size: int = 22) -> QIcon:
    body = _P.get(name, _P["about"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    r = QSvgRenderer(QByteArray(svg.encode()))
    ic = QIcon()
    for s in (size, size * 2):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        r.render(p, QRectF(0, 0, s, s))
        p.end()
        ic.addPixmap(pm)
    return ic
