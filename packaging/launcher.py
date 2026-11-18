"""Punto de entrada para los ejecutables empaquetados.

Sin argumentos abre la interfaz gráfica; con argumentos funciona como CLI
(p. ej. ``RecuperaDatos-MX escanear disco.img --profundo``)."""
import multiprocessing
import sys

from recuperadatos_mx.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main(sys.argv[1:] if len(sys.argv) > 1 else None)
