"""
comun.py - Utilidades compartidas: registro (log), YuNet + SFace, cámara y métricas del proceso.
"""
import os
import sys
import time
import logging
import platform

import cv2
import numpy as np

import config


def configurar_log(nombre):
    os.makedirs(config.CARPETA_LOGS, exist_ok=True)
    marca = time.strftime('%Y%m%d_%H%M%S')
    ruta = os.path.join(config.CARPETA_LOGS, f'{nombre}_{marca}.log')
    log = logging.getLogger(nombre)
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter('%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(message)s',
                            '%H:%M:%S')
    consola = logging.StreamHandler(sys.stdout)
    consola.setLevel(logging.INFO)
    consola.setFormatter(fmt)
    archivo = logging.FileHandler(ruta, encoding='utf-8')
    archivo.setLevel(logging.DEBUG)
    archivo.setFormatter(fmt)
    log.addHandler(consola)
    log.addHandler(archivo)
    log.info(f'Python {platform.python_version()} | OpenCV {cv2.__version__} | '
             f'{platform.system()} {platform.release()} | log: {ruta}')
    return log


def segundos_desde_encendido():
    """Tiempo desde que arrancó el kernel (sirve para medir RNF-02)."""
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except OSError:
        return float('nan')


def memoria_residente_mib():
    """VmRSS del proceso en MiB (RNF-04: < 512 MiB)."""
    try:
        with open('/proc/self/status') as f:
            for linea in f:
                if linea.startswith('VmRSS:'):
                    return int(linea.split()[1]) / 1024
    except OSError:
        pass
    return float('nan')


def resumen(valores, unidad='ms'):
    if not valores:
        return 'sin datos'
    a = np.array(valores, dtype=float)
    return (f'n={len(a)} | prom {a.mean():.1f} {unidad} | p95 {np.percentile(a, 95):.1f} | '
            f'máx {a.max():.1f}')


def abrir_camara(fuente, log):
    """Abre /dev/videoN (V4L2 en Linux) o un archivo de video. Devuelve None si falla."""
    t0 = time.perf_counter()
    if fuente.isdigit():
        indice = int(fuente)
        if platform.system() == 'Linux':
            cap = cv2.VideoCapture(indice, cv2.CAP_V4L2)
        elif platform.system() == 'Windows':
            cap = cv2.VideoCapture(indice, cv2.CAP_DSHOW)
        else:
            cap = cv2.VideoCapture(indice)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.ANCHO)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.ALTO)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    else:
        cap = cv2.VideoCapture(fuente)
    ms = (time.perf_counter() - t0) * 1000
    if not cap.isOpened():
        log.error(f'No se pudo abrir la cámara {fuente} ({ms:.0f} ms)')
        return None
    log.info(f'Cámara {fuente} abierta en {ms:.0f} ms '
             f'({int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))})')
    return cap


class Biometria:
    """YuNet detecta el rostro y 5 puntos; SFace alinea y produce un vector de 128 valores."""

    def __init__(self, log):
        for ruta in (config.MODELO_YUNET, config.MODELO_SFACE):
            if not os.path.isfile(ruta):
                raise FileNotFoundError(f'Falta el modelo {ruta}; ejecuta scripts/descargar_modelos.sh')
        t0 = time.perf_counter()
        self.detector = cv2.FaceDetectorYN.create(
            config.MODELO_YUNET, '', (config.ANCHO, config.ALTO), config.UMBRAL_DETECCION, 0.3, 5000)
        self.reconocedor = cv2.FaceRecognizerSF.create(config.MODELO_SFACE, '')
        self.tam_entrada = (config.ANCHO, config.ALTO)
        log.info(f'Modelos YuNet + SFace cargados en {(time.perf_counter() - t0) * 1000:.0f} ms')

    def detectar(self, imagen):
        """Devuelve un arreglo N x 15: x, y, w, h, 5 puntos (x, y) y la confianza."""
        alto, ancho = imagen.shape[:2]
        if (ancho, alto) != self.tam_entrada:
            self.detector.setInputSize((ancho, alto))
            self.tam_entrada = (ancho, alto)
        _, caras = self.detector.detect(imagen)
        return np.empty((0, 15), np.float32) if caras is None else caras

    def vector(self, imagen, cara):
        """Vector SFace normalizado (norma 1): la similitud coseno es un producto punto."""
        alineada = self.reconocedor.alignCrop(imagen, cara)
        v = self.reconocedor.feature(alineada).flatten().astype(np.float32)
        return v / np.linalg.norm(v)


def dibujar(imagen, cara, texto, color):
    x, y, w, h = [int(v) for v in cara[:4]]
    cv2.rectangle(imagen, (x, y), (x + w, y + h), color, 2)
    cv2.putText(imagen, texto, (x, max(20, y - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)
