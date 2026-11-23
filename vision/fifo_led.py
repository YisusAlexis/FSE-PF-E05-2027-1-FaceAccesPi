"""
fifo_led.py - Cliente de la FIFO hacia el programa en C del NeoPixel.

Decisión bitácora clase 11 (cambio): dos procesos (Python y C) comunicados por una FIFO.
La FIFO se abre en modo NO bloqueante: si el programa en C no está corriendo, open()
falla con ENXIO y el reconocimiento continúa (prueba F-04 / falla inducida 2).
"""
import os
import errno
import time


class LedFifo:
    REINTENTO_S = 1.0

    def __init__(self, ruta, log):
        self.ruta = ruta
        self.log = log
        self.fd = None
        self.ultimo_intento = 0.0
        self.conectado_antes = None
        self.enviados = 0
        self.fallidos = 0

    def _abrir(self):
        if time.monotonic() - self.ultimo_intento < self.REINTENTO_S:
            return False
        self.ultimo_intento = time.monotonic()
        try:
            self.fd = os.open(self.ruta, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            self.fd = None
            if self.conectado_antes is not False:
                motivo = 'el programa del LED no está corriendo' if e.errno in (errno.ENXIO, errno.ENOENT) else e
                self.log.warning(f'Sin señalización: no se pudo abrir {self.ruta} ({motivo}); se reintentará')
            self.conectado_antes = False
            return False
        if self.conectado_antes is False:
            self.log.info('Señalización recuperada: conectado de nuevo a la FIFO del LED')
        else:
            self.log.info(f'Conectado a la FIFO del LED: {self.ruta}')
        self.conectado_antes = True
        return True

    def enviar(self, estado):
        """Escribe el estado; devuelve True si llegó al programa en C."""
        if self.fd is None and not self._abrir():
            self.fallidos += 1
            return False
        try:
            os.write(self.fd, (estado + '\n').encode())
            self.enviados += 1
            return True
        except OSError as e:          # EPIPE: el lector se cerró (programa en C detenido)
            self.log.warning(f'Se perdió la FIFO del LED al enviar {estado}: {e.strerror}')
            self.cerrar()
            self.conectado_antes = False
            self.fallidos += 1
            self.ultimo_intento = 0.0
            return False

    def cerrar(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
