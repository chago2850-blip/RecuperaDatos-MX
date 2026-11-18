"""Vista hexadecimal virtual capaz de recorrer discos completos sin cargarlos en memoria."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter
from PySide6.QtWidgets import QAbstractScrollArea

from ..core.device import BlockSource
from .common import colors

BPR = 16  # bytes por fila


class HexView(QAbstractScrollArea):
    offsetChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        f.setPointSize(10)
        self.setFont(f)
        self.src: Optional[BlockSource] = None
        self.top_row = 0
        self.sel = -1
        self.highlight = (0, 0)
        self.cache = (-1, b"")
        self.setFocusPolicy(Qt.StrongFocus)

    def set_source(self, src: Optional[BlockSource]) -> None:
        self.src = src
        self.cache = (-1, b"")
        self.top_row = 0
        self._update_scroll()
        self.viewport().update()

    def _rows_total(self) -> int:
        return (self.src.size + BPR - 1) // BPR if self.src else 0

    def _visible_rows(self) -> int:
        return max(1, self.viewport().height() // self.fontMetrics().height())

    def _update_scroll(self) -> None:
        total = self._rows_total()
        # la barra trabaja en "páginas" para soportar discos de varios TB
        self.scale = max(1, total // 1_000_000)
        sb = self.verticalScrollBar()
        sb.setRange(0, max(0, (total - self._visible_rows()) // self.scale))
        sb.setPageStep(max(1, self._visible_rows() // self.scale))

    def scrollContentsBy(self, dx, dy) -> None:
        self.top_row = self.verticalScrollBar().value() * self.scale
        self.viewport().update()
        self.offsetChanged.emit(self.top_row * BPR)

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._update_scroll()

    def goto(self, offset: int, length: int = 0) -> None:
        if not self.src:
            return
        offset = max(0, min(offset, max(0, self.src.size - 1)))
        self.top_row = offset // BPR
        if length:
            self.highlight = (offset, length)
        self.verticalScrollBar().blockSignals(True)
        self.verticalScrollBar().setValue(self.top_row // self.scale)
        self.verticalScrollBar().blockSignals(False)
        self.viewport().update()
        self.offsetChanged.emit(self.top_row * BPR)

    def _data(self, off: int, n: int) -> bytes:
        co, cd = self.cache
        if co <= off and off + n <= co + len(cd):
            return cd[off - co: off - co + n]
        start = max(0, off - 64 * 1024)
        cd = self.src.read(start, 256 * 1024)
        self.cache = (start, cd)
        return cd[off - start: off - start + n]

    def paintEvent(self, ev) -> None:
        p = QPainter(self.viewport())
        c = colors()
        p.fillRect(self.viewport().rect(), QColor(c["panel"]))
        if not self.src:
            p.setPen(QColor(c["muted"]))
            p.drawText(self.viewport().rect(), Qt.AlignCenter, "Seleccione un origen y pulse «Abrir»")
            return
        fm = self.fontMetrics()
        lh = fm.height()
        cw = fm.horizontalAdvance("0")
        rows = self._visible_rows() + 1
        base = self.top_row * BPR
        data = self._data(base, rows * BPR)
        x_hex = cw * 18
        x_asc = x_hex + cw * (BPR * 3 + 2)
        hs, hl = self.highlight
        for r in range(rows):
            off = base + r * BPR
            if off >= self.src.size:
                break
            y = r * lh + fm.ascent()
            chunk = data[r * BPR:(r + 1) * BPR]
            sector_start = off % self.src.sector_size == 0
            p.setPen(QColor(c["accent"] if sector_start else c["muted"]))
            p.drawText(4, y, f"{off:016X}")
            for i, b in enumerate(chunk):
                o = off + i
                xx = x_hex + i * cw * 3 + (cw if i >= 8 else 0)
                if hl and hs <= o < hs + hl:
                    p.fillRect(QRect(xx - 2, r * lh, cw * 2 + 4, lh), QColor(c["sel"]))
                    p.fillRect(QRect(x_asc + i * cw, r * lh, cw, lh), QColor(c["sel"]))
                p.setPen(QColor(c["muted"] if b == 0 else c["text"]))
                p.drawText(xx, y, f"{b:02X}")
                p.drawText(x_asc + i * cw, y, chr(b) if 32 <= b < 127 else "·")

    def keyPressEvent(self, ev) -> None:
        sb = self.verticalScrollBar()
        k = ev.key()
        if k == Qt.Key_PageDown:
            self.goto((self.top_row + self._visible_rows()) * BPR)
        elif k == Qt.Key_PageUp:
            self.goto(max(0, self.top_row - self._visible_rows()) * BPR)
        elif k == Qt.Key_Down:
            self.goto((self.top_row + 1) * BPR)
        elif k == Qt.Key_Up:
            self.goto(max(0, self.top_row - 1) * BPR)
        else:
            super().keyPressEvent(ev)
            return
        sb.setValue(self.top_row // self.scale)

    def wheelEvent(self, ev) -> None:
        steps = -ev.angleDelta().y() // 40
        self.goto(max(0, (self.top_row + steps) * BPR))
