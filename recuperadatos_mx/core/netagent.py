"""Recuperación por red: un agente de solo lectura expone los discos de una
computadora remota y el cliente los abre como si fueran locales.

Protocolo: mensajes con prefijo de longitud (4 bytes) y cabecera JSON;
autenticación por desafío HMAC-SHA256 con una clave compartida.  Para redes no
confiables use una VPN o un túnel SSH (``ssh -L 7878:localhost:7878``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import socketserver
import struct
import threading
from typing import Optional

from .device import BlockSource, FileSource, list_devices, open_image

DEFAULT_PORT = 7878
MAX_READ = 16 << 20


def _send(sock, header: dict, payload: bytes = b"") -> None:
    h = json.dumps(header).encode()
    sock.sendall(struct.pack(">II", len(h), len(payload)) + h + payload)


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        c = sock.recv(min(n - len(buf), 1 << 20))
        if not c:
            raise ConnectionError("Conexión cerrada")
        buf += c
    return bytes(buf)


def _recv(sock):
    hl, pl = struct.unpack(">II", _recv_exact(sock, 8))
    if hl > 1 << 20 or pl > MAX_READ + 1024:
        raise ConnectionError("Mensaje demasiado grande")
    h = json.loads(_recv_exact(sock, hl))
    p = _recv_exact(sock, pl) if pl else b""
    return h, p


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        token = self.server.token.encode()
        nonce = os.urandom(16)
        _send(self.request, {"op": "desafio", "nonce": nonce.hex(), "app": "RecuperaDatos-MX"})
        try:
            h, _ = _recv(self.request)
            expect = hmac.new(token, nonce, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(h.get("respuesta", ""), expect):
                _send(self.request, {"ok": False, "error": "Autenticación fallida"})
                return
            _send(self.request, {"ok": True})
            handles = {}
            while True:
                h, _ = _recv(self.request)
                op = h.get("op")
                if op == "listar":
                    devs = [d.__dict__ | {"children": [c.__dict__ for c in d.children]} for d in list_devices()]
                    _send(self.request, {"ok": True, "dispositivos": json.loads(json.dumps(devs, default=str))})
                elif op == "abrir":
                    path = h["ruta"]
                    src = open_image(path) if not path.startswith(("/dev/", "\\\\.\\")) else FileSource(path)
                    hid = len(handles) + 1
                    handles[hid] = src
                    _send(self.request, {"ok": True, "id": hid, "tamano": src.size,
                                         "sector": src.sector_size, "nombre": src.name})
                elif op == "leer":
                    src = handles[h["id"]]
                    n = min(int(h["n"]), MAX_READ)
                    _send(self.request, {"ok": True}, src.read(int(h["off"]), n))
                elif op == "cerrar":
                    src = handles.pop(h["id"], None)
                    if src:
                        src.close()
                    _send(self.request, {"ok": True})
                else:
                    _send(self.request, {"ok": False, "error": "Operación desconocida"})
        except (ConnectionError, OSError, KeyError, ValueError) as exc:
            try:
                _send(self.request, {"ok": False, "error": str(exc)})
            except OSError:
                pass


class Agent(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int, token: str) -> None:
        if not token:
            raise ValueError("Defina una clave de acceso para el agente")
        self.token = token
        super().__init__((host, port), _Handler)


def run_agent(host: str = "0.0.0.0", port: int = DEFAULT_PORT, token: str = "",
              background: bool = False) -> Agent:
    srv = Agent(host, port, token)
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    else:
        srv.serve_forever()
    return srv


class RemoteClient:
    def __init__(self, host: str, port: int, token: str, timeout: float = 30) -> None:
        self.host, self.port = host, port
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.lock = threading.Lock()
        h, _ = _recv(self.sock)
        nonce = bytes.fromhex(h["nonce"])
        _send(self.sock, {"respuesta": hmac.new(token.encode(), nonce, hashlib.sha256).hexdigest()})
        h, _ = _recv(self.sock)
        if not h.get("ok"):
            raise PermissionError(h.get("error", "Acceso denegado"))

    def call(self, header: dict):
        with self.lock:
            _send(self.sock, header)
            h, p = _recv(self.sock)
        if not h.get("ok"):
            raise OSError(h.get("error", "Error remoto"))
        return h, p

    def devices(self):
        return self.call({"op": "listar"})[0]["dispositivos"]


class RemoteSource(BlockSource):
    def __init__(self, host: str, port: int, token: str, path: str,
                 client: Optional[RemoteClient] = None) -> None:
        self.client = client or RemoteClient(host, port, token)
        h, _ = self.client.call({"op": "abrir", "ruta": path})
        self.id = h["id"]
        self.size = h["tamano"]
        self.sector_size = h["sector"]
        self.path = path
        self.name = f"{host}:{h['nombre']}"
        self.host, self.port = host, port
        self._cache = (-1, b"")

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self.size or length <= 0:
            return b""
        co, cd = self._cache
        if co <= offset and offset + length <= co + len(cd):
            return cd[offset - co: offset - co + length]
        out = bytearray()
        want = max(length, 1 << 20)
        pos = offset
        while len(out) < want and pos < self.size:
            n = min(MAX_READ, want - len(out))
            _, p = self.client.call({"op": "leer", "id": self.id, "off": pos, "n": n})
            if not p:
                break
            out += p
            pos += len(p)
        self._cache = (offset, bytes(out))
        return bytes(out[:length])

    def describe(self) -> dict:
        return {"tipo": "remoto", "host": self.host, "puerto": self.port, "ruta": self.path, "tamano": self.size}

    def close(self) -> None:
        try:
            self.client.call({"op": "cerrar", "id": self.id})
        except OSError:
            pass
