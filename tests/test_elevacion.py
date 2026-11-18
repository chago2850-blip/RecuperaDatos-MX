"""«Reiniciar como administrador» en Linux: la ventana espera a pkexec (que se niega
a funcionar si su proceso padre terminó), reaparece si se cancela y recurre a sudo
con diálogo propio si no hay agente de polkit."""
import os
import stat
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="solo Linux")

FAKE_PKEXEC = """#!/bin/sh
sleep 1
if [ "$(ps -o ppid= -p $$ | tr -d ' ')" = "1" ]; then exit 127; fi
echo ok > "$FAKE_DIR/pkexec.txt"
case "$FAKE_MODE" in cancel) exit 126;; noagent) echo "No authentication agent found." >&2; exit 127;; ok) sleep 3; exit 0;; esac
"""
FAKE_SUDO = """#!/bin/sh
echo "$SUDO_ASKPASS" > "$FAKE_DIR/sudo.txt"; sleep 2; exit 0
"""


@pytest.mark.parametrize("mode,quits,used_sudo", [("cancel", False, False), ("ok", True, False), ("noagent", True, True)])
def test_restart_as_admin(tmp_path, monkeypatch, mode, quits, used_sudo):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    for name, body in (("pkexec", FAKE_PKEXEC), ("sudo", FAKE_SUDO)):
        p = tmp_path / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.setenv("FAKE_MODE", mode)
    from recuperadatos_mx.gui import common
    from recuperadatos_mx.gui.theme import DARK
    common.STATE["colors"] = DARK
    app = QApplication.instance() or QApplication([])
    from PySide6.QtWidgets import QWidget
    w = QWidget()
    w.show()
    state = {"quit": False, "visible": []}
    monkeypatch.setattr(app, "quit", lambda: state.__setitem__("quit", True))
    assert common.restart_as_admin(w)
    t0 = time.time()
    while time.time() - t0 < 12 and not state["quit"]:
        app.processEvents()
        state["visible"].append(w.isVisible())
        time.sleep(0.05)
    assert (tmp_path / "pkexec.txt").exists(), "pkexec se negó: el proceso padre no siguió vivo"
    assert state["quit"] == quits
    assert False in state["visible"]  # la ventana se ocultó mientras se pedía la contraseña
    if not quits:
        assert w.isVisible()  # al cancelar, la ventana vuelve
    assert (tmp_path / "sudo.txt").exists() == used_sudo
