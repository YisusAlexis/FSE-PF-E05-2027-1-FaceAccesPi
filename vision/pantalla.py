"""
pantalla.py - Salida de video del sistema: framebuffer (/dev/fb0), ventana de OpenCV o nada.

Modo kiosco (sin escritorio): se escribe cada imagen directo en /dev/fb0, el dispositivo de
framebuffer que expone el driver vc4 (DRM/KMS) de la Raspberry Pi. No hace falta sesión
gráfica ni escritorio; la imagen aparece en cuanto el servicio procesa el primer cuadro.
El formato del framebuffer (tamaño, bits por píxel y bytes por línea) se lee de
/sys/class/graphics/fb0/. La conversión a RGB565 la hace OpenCV (cv2.COLOR_BGR2BGR565).

El panel muestra la cámara, la hora, el estado del sistema, el aviso del último registro
y el historial reciente. OpenCV no dibuja acentos: el texto se pasa sin acentos.
"""
import os
import time
import unicodedata

import cv2
import numpy as np

import config

DIAS = ['lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo']
FUENTE = cv2.FONT_HERSHEY_SIMPLEX
FUENTE_TITULO = cv2.FONT_HERSHEY_DUPLEX
BLANCO, GRIS, GRIS_OSCURO = (235, 235, 235), (150, 150, 150), (60, 60, 60)
VERDE, ROJO, AMARILLO, CIAN = (80, 200, 80), (60, 60, 230), (0, 200, 255), (230, 200, 0)
NARANJA = (0, 140, 255)
TONOS = {'verde': VERDE, 'cian': CIAN, 'naranja': NARANJA, 'rojo': ROJO, 'amarillo': AMARILLO}
FONDO = (25, 25, 25)


def sin_acentos(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', str(texto))
                   if unicodedata.category(c) != 'Mn').replace('ñ', 'n').replace('Ñ', 'N')


# ------------------------------------------------------------------- salidas
class FrameBuffer:
    def __init__(self, ruta):
        nombre = os.path.basename(ruta)
        base = f'/sys/class/graphics/{nombre}'
        with open(f'{base}/virtual_size') as f:
            self.ancho, self.alto = [int(v) for v in f.read().strip().split(',')]
        with open(f'{base}/bits_per_pixel') as f:
            self.bpp = int(f.read())
        with open(f'{base}/stride') as f:
            self.stride = int(f.read())
        if self.bpp not in (16, 32):
            raise OSError(f'{ruta}: {self.bpp} bits por píxel no soportado')
        tipo = np.uint16 if self.bpp == 16 else np.uint32
        por_pixel = self.bpp // 8
        self.mem = np.memmap(ruta, dtype=tipo, mode='r+',
                             shape=(self.alto, self.stride // por_pixel))

    def escribir(self, img):
        if self.bpp == 16:
            px = cv2.cvtColor(img, cv2.COLOR_BGR2BGR565).view(np.uint16)
        else:
            px = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA).view(np.uint32)
        alto, ancho = img.shape[:2]
        self.mem[:alto, :ancho] = px.reshape(alto, ancho)

    def limpiar(self):
        self.mem[:] = 0


class Pantalla:
    """modo: 'fb', 'qt', 'no' o 'auto'."""

    def __init__(self, log, titulo='FaceAccessPi', modo=None):
        self.log = log
        self.titulo = titulo
        self.fb = None
        self.completa = False
        modo = (modo or config.PANTALLA).lower()
        if modo == 'auto':
            modo = 'fb' if os.access(config.DISPOSITIVO_FB, os.W_OK) else 'no'
        self.modo = modo
        try:
            if modo == 'fb':
                self.fb = FrameBuffer(config.DISPOSITIVO_FB)
                self.fb.limpiar()
                self.tam = (self.fb.ancho, self.fb.alto)
                log.info(f'Pantalla en framebuffer {config.DISPOSITIVO_FB}: {self.fb.ancho}x{self.fb.alto}, '
                         f'{self.fb.bpp} bpp, {self.fb.stride} bytes por línea')
            elif modo == 'qt':
                self.tam = config.TAM_VENTANA_QT
                self.completa = config.PANTALLA_COMPLETA
                self._crear_ventana()
                log.info(f'Pantalla en ventana de escritorio (Qt), {"completa" if self.completa else "normal"}')
            else:
                self.modo = 'no'
                self.tam = config.TAM_VENTANA_QT
                log.info('Sin pantalla')
        except (OSError, ValueError, cv2.error) as e:
            log.warning(f'No se pudo usar la pantalla en modo {modo} ({e}); se continúa sin pantalla')
            self.modo, self.fb, self.tam = 'no', None, config.TAM_VENTANA_QT

        self.oculta = False

    def _crear_ventana(self):
        cv2.namedWindow(self.titulo, cv2.WINDOW_NORMAL)
        if self.completa:
            cv2.setWindowProperty(self.titulo, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        else:
            cv2.setWindowProperty(self.titulo, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(self.titulo, 960, 540)

    @property
    def activa(self):
        return self.modo != 'no' and not self.oculta

    def comando(self, orden):
        """Control desde scripts/pantalla.sh: completa, ventana, ocultar, mostrar."""
        if self.modo == 'no':
            return 'sin pantalla'
        if orden == 'ocultar':
            self.oculta = True
            if self.modo == 'fb':
                self.fb.limpiar()
            else:
                cv2.destroyWindow(self.titulo)
                cv2.waitKey(1)
            return 'oculta'
        if orden == 'mostrar':
            if self.oculta and self.modo == 'qt':
                self._crear_ventana()
            self.oculta = False
            return 'visible'
        if orden in ('completa', 'ventana'):
            if self.modo == 'fb':
                return 'el framebuffer siempre ocupa toda la pantalla'
            self.completa = orden == 'completa'
            if not self.oculta:
                self._crear_ventana()
            return 'pantalla completa' if self.completa else 'ventana normal'
        return f'orden desconocida: {orden}'

    def mostrar(self, img):
        if self.oculta:
            return
        if self.modo == 'fb':
            self.fb.escribir(img)
        elif self.modo == 'qt':
            cv2.imshow(self.titulo, img)

    def esperar(self, ms):
        """Pausa; en modo qt procesa la ventana y devuelve la tecla (27 = ESC)."""
        if self.modo == 'qt':
            return cv2.waitKey(max(1, int(ms))) & 0xFF
        time.sleep(ms / 1000)
        return -1

    def cerrar(self):
        if self.modo == 'fb':
            self.fb.limpiar()
        elif self.modo == 'qt':
            cv2.destroyAllWindows()


# --------------------------------------------------------------- composición
def _texto(img, texto, x, y, escala, color, grosor=1, fuente=FUENTE, ancho_max=None):
    """Dibuja texto (sin acentos), reduciendo la escala si no cabe en ancho_max."""
    texto = sin_acentos(texto)
    if ancho_max:
        (w, _), _ = cv2.getTextSize(texto, fuente, escala, grosor)
        if w > ancho_max:
            escala *= ancho_max / w
    cv2.putText(img, texto, (int(x), int(y)), fuente, escala, color, grosor, cv2.LINE_AA)
    return escala


def componer(tam, camara, estado):
    """
    Arma el lienzo completo. 'estado' es un dict con:
      personas, fps, camara_ok, led_ok, aviso (texto bajo la cámara o ''),
      mensaje ({'titulo', 'lineas', 'color', 'expira'} o None),
      historial (lista de (hora, texto, color)), pie (texto)
    """
    ancho, alto = tam
    s = alto / 768                                  # escala relativa a 1360x768
    lienzo = np.full((alto, ancho, 3), FONDO, np.uint8)
    m = int(16 * s)

    # --- cámara (izquierda), conservando proporción
    area_w, area_h = int(ancho * 0.66), alto - 2 * m
    if camara is not None:
        h, w = camara.shape[:2]
        f = min(area_w / w, area_h / h)
        cw, ch = int(w * f), int(h * f)
        cx, cy = m, m + (area_h - ch) // 2
        lienzo[cy:cy + ch, cx:cx + cw] = cv2.resize(camara, (cw, ch), interpolation=cv2.INTER_LINEAR)
    else:
        cw, ch, cx, cy = area_w, area_h, m, m
        cv2.rectangle(lienzo, (cx, cy), (cx + cw, cy + ch), GRIS_OSCURO, 2)
        _texto(lienzo, 'Camara desconectada - reintentando', cx + 30 * s, cy + ch / 2, 1.0 * s, ROJO, 2)
    if not estado.get('camara_ok', True) and camara is not None:
        _texto(lienzo, 'Camara desconectada - reintentando', cx + 20 * s, cy + 40 * s, 0.9 * s, ROJO, 2)
    if estado.get('aviso'):
        cv2.rectangle(lienzo, (cx, cy + ch - int(56 * s)), (cx + cw, cy + ch), (0, 0, 0), -1)
        _texto(lienzo, estado['aviso'], cx + 16 * s, cy + ch - 18 * s, 0.9 * s, AMARILLO, 2, ancho_max=cw - 32 * s)

    # --- panel (derecha)
    px = cx + cw + int(24 * s)
    pw = ancho - px - m
    y = m + 34 * s
    _texto(lienzo, 'FaceAccessPi', px, y, 1.1 * s, BLANCO, 2, FUENTE_TITULO, pw)
    y += 26 * s
    _texto(lienzo, 'Control de asistencia', px, y, 0.6 * s, GRIS, 1, ancho_max=pw)
    if estado.get('modo'):
        demo = estado['modo'] == 'DEMO'
        etiqueta = f"MODO {estado['modo']}"
        (tw, th), _ = cv2.getTextSize(etiqueta, FUENTE, 0.5 * s, 1)
        bx = int(px + pw - tw - 16 * s)
        cv2.rectangle(lienzo, (bx, int(y - th - 8 * s)), (int(px + pw), int(y + 6 * s)),
                      NARANJA if demo else GRIS_OSCURO, -1)
        _texto(lienzo, etiqueta, bx + 8 * s, y, 0.5 * s, (0, 0, 0) if demo else BLANCO, 1)
    t = time.localtime()
    y += 72 * s
    _texto(lienzo, time.strftime('%H:%M:%S', t), px, y, 2.0 * s, BLANCO, 3, FUENTE_TITULO, pw)
    y += 34 * s
    _texto(lienzo, f'{DIAS[t.tm_wday]} {time.strftime("%d/%m/%Y", t)}', px, y, 0.7 * s, GRIS, 1, ancho_max=pw)

    # aviso del último registro
    y += 30 * s
    caja_h = int(170 * s)
    msg = estado.get('mensaje')
    if msg and msg['expira'] > time.monotonic():
        cv2.rectangle(lienzo, (px, int(y)), (px + pw, int(y) + caja_h), msg['color'], -1)
        _texto(lienzo, msg['titulo'], px + 14 * s, y + 44 * s, 1.0 * s, (0, 0, 0), 2, FUENTE_TITULO, pw - 28 * s)
        yy = y + 44 * s
        lineas = list(msg.get('lineas', []))[:4]
        paso = 32 if len(lineas) <= 3 else 26
        for i, linea in enumerate(lineas):
            yy += (40 if i == 0 else paso) * s
            _texto(lienzo, linea, px + 14 * s, yy, (0.95 if i == 0 else 0.62) * s, (0, 0, 0),
                   2 if i == 0 else 1, ancho_max=pw - 28 * s)
    else:
        cv2.rectangle(lienzo, (px, int(y)), (px + pw, int(y) + caja_h), GRIS_OSCURO, 2)
        _texto(lienzo, 'Mire a la camara', px + 14 * s, y + caja_h / 2, 0.9 * s, GRIS, 2, ancho_max=pw - 28 * s)
    y += caja_h + 40 * s

    # estado del sistema
    def linea_estado(etiqueta, valor, color):
        nonlocal y
        _texto(lienzo, etiqueta, px, y, 0.6 * s, GRIS, 1)
        _texto(lienzo, valor, px + pw * 0.5, y, 0.6 * s, color, 2, ancho_max=pw * 0.5)
        y += 28 * s
    linea_estado('Camara', 'OK' if estado.get('camara_ok', True) else 'FALLA',
                 VERDE if estado.get('camara_ok', True) else ROJO)
    linea_estado('Senal LED', 'OK' if estado.get('led_ok', True) else 'SIN SENAL',
                 VERDE if estado.get('led_ok', True) else AMARILLO)
    linea_estado('Reloj (NTP)', 'OK' if estado.get('reloj_ok', True) else 'SIN SINCRONIZAR',
                 VERDE if estado.get('reloj_ok', True) else ROJO)
    linea_estado('Empleados', str(estado.get('personas', 0)), BLANCO)
    linea_estado('Proceso', f"{estado.get('fps', 0):.1f} img/s", BLANCO)

    # historial
    y += 14 * s
    _texto(lienzo, 'Ultimos registros', px, y, 0.65 * s, BLANCO, 1)
    y += 8 * s
    cv2.line(lienzo, (px, int(y)), (px + pw, int(y)), GRIS_OSCURO, 1)
    for hora, texto, color in list(estado.get('historial', []))[-6:][::-1]:
        y += 28 * s
        if y > alto - 40 * s:
            break
        _texto(lienzo, hora, px, y, 0.55 * s, GRIS, 1)
        _texto(lienzo, texto, px + 90 * s, y, 0.55 * s, color, 1, ancho_max=pw - 90 * s)

    if estado.get('pie'):
        _texto(lienzo, estado['pie'], px, alto - m, 0.45 * s, GRIS, 1, ancho_max=pw)
    return lienzo
