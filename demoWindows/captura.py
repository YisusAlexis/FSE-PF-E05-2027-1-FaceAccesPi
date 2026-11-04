"""
captura.py - Captura de rostros para el registro de una persona.

Muestra en consola (y guarda en logs/) todo lo que ocurre:
apertura de cámara, progreso, cuadros sin rostro o con varios rostros,
errores de escritura y un resumen final con tiempos.
"""
import cv2
import os
import re
import sys
import time
import logging
import platform
import imutils
import numpy as np

# ----------------------------- Configuración -----------------------------
DATA_PATH = './Reconocimiento'
CASCADE_FILE = 'haarcascade_frontalface_default.xml'
CARPETA_LOGS = './logs'
INDICE_CAMARA = 0
MAX_IMAGENES = 300            # RF-01: 300 imágenes por persona
TAM_ROSTRO = (150, 150)       # RF-01: 150 x 150 px
ANCHO_FRAME = 640
SCALE_FACTOR = 1.3
MIN_NEIGHBORS = 5
TECLA_ESC = 27
INTERVALO_REPORTE = 25        # cada cuántas imágenes se informa el progreso
MAX_FALLOS_LECTURA = 30       # lecturas fallidas seguidas antes de abortar
EXTENSIONES = ('.jpg', '.jpeg', '.png')
NOMBRE_VALIDO = re.compile(r'^[A-Za-z0-9_]+$')   # el nombre no debe armar rutas raras


def configurar_log(nombre):
    os.makedirs(CARPETA_LOGS, exist_ok=True)
    marca = time.strftime('%Y%m%d_%H%M%S')
    ruta = os.path.join(CARPETA_LOGS, f'{nombre}_{marca}.log')
    log = logging.getLogger(nombre)
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter('%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s', '%H:%M:%S')
    consola = logging.StreamHandler(sys.stdout)
    consola.setLevel(logging.INFO)
    consola.setFormatter(fmt)
    archivo = logging.FileHandler(ruta, encoding='utf-8')
    archivo.setLevel(logging.DEBUG)
    archivo.setFormatter(fmt)
    log.addHandler(consola)
    log.addHandler(archivo)
    return log, ruta


def resumen_tiempos(valores):
    if not valores:
        return 'sin datos'
    arr = np.array(valores)
    return f'prom {arr.mean():.1f} ms | p95 {np.percentile(arr, 95):.1f} ms | máx {arr.max():.1f} ms'


def abrir_camara(log, indice):
    t0 = time.perf_counter()
    if platform.system() == 'Windows':
        cap = cv2.VideoCapture(indice, cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(indice)
    ms = (time.perf_counter() - t0) * 1000
    if not cap.isOpened():
        log.error(f'No se pudo abrir la cámara {indice} (intento de {ms:.0f} ms)')
        return None
    ancho = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    alto = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    log.info(f'Cámara {indice} abierta en {ms:.0f} ms ({ancho}x{alto}, {fps:.0f} fps reportados)')
    return cap


def main():
    log, ruta_log = configurar_log('captura')
    log.info('=== Captura de rostros ===')
    log.info(f'Python {platform.python_version()} | OpenCV {cv2.__version__} | '
             f'{platform.system()} {platform.release()}')
    log.info(f'Log de esta ejecución: {ruta_log}')

    nombre = input('Nombre: ').strip()
    if not NOMBRE_VALIDO.match(nombre):
        log.error(f'Nombre inválido "{nombre}": usa solo letras, números y guion bajo')
        return 1

    persona_path = os.path.join(DATA_PATH, nombre)
    if os.path.isdir(persona_path):
        existentes = len([f for f in os.listdir(persona_path) if f.lower().endswith(EXTENSIONES)])
        log.warning(f'La carpeta {persona_path} ya existe con {existentes} imágenes; '
                    f'las de igual nombre se sobrescribirán')
    else:
        os.makedirs(persona_path)
        log.info(f'Carpeta creada: {persona_path}')

    t0 = time.perf_counter()
    clasificador = cv2.CascadeClassifier(CASCADE_FILE)
    if clasificador.empty():
        log.error(f'No se pudo cargar el clasificador {CASCADE_FILE}')
        return 1
    log.info(f'Clasificador Haar cargado en {(time.perf_counter() - t0) * 1000:.0f} ms')

    cap = abrir_camara(log, INDICE_CAMARA)
    if cap is None:
        return 1

    count = 0
    cuadros = 0
    sin_rostro = 0
    multiples = 0
    errores_escritura = 0
    fallos_seguidos = 0
    t_deteccion = []
    t_primera = None
    detenido_esc = False
    t_inicio = time.perf_counter()
    log.info(f'Iniciando captura de {MAX_IMAGENES} imágenes (ESC para detener)')

    try:
        while count < MAX_IMAGENES:
            ret, frame = cap.read()
            if not ret:
                fallos_seguidos += 1
                log.warning(f'No se pudo leer un cuadro ({fallos_seguidos} seguidos)')
                if fallos_seguidos >= MAX_FALLOS_LECTURA:
                    log.error('Demasiadas lecturas fallidas; se detiene la captura')
                    break
                continue
            fallos_seguidos = 0
            cuadros += 1

            frame = imutils.resize(frame, width=ANCHO_FRAME)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            auxFrame = frame.copy()

            t0 = time.perf_counter()
            caras = clasificador.detectMultiScale(gray, SCALE_FACTOR, MIN_NEIGHBORS)
            t_deteccion.append((time.perf_counter() - t0) * 1000)
            n = len(caras)

            if n == 0:
                sin_rostro += 1
            elif n > 1:
                multiples += 1
                log.debug(f'Cuadro {cuadros}: {n} rostros, no se guarda (RF-01 pide uno solo)')
            else:
                x, y, w, h = caras[0]
                rostro = auxFrame[y:y + h, x:x + w]
                rostro = cv2.resize(rostro, TAM_ROSTRO, interpolation=cv2.INTER_CUBIC)
                ruta = os.path.join(persona_path, f'rostro_{count}.jpg')
                if cv2.imwrite(ruta, rostro):
                    count += 1
                    if t_primera is None:
                        t_primera = time.perf_counter() - t_inicio
                        log.info(f'Primera imagen guardada a los {t_primera:.2f} s')
                    if count % INTERVALO_REPORTE == 0:
                        transcurrido = time.perf_counter() - t_inicio
                        log.info(f'Progreso: {count}/{MAX_IMAGENES} imágenes en {transcurrido:.1f} s')
                else:
                    errores_escritura += 1
                    log.error(f'No se pudo escribir {ruta}')

            color = (0, 255, 0) if n == 1 else (0, 0, 255)
            for (x, y, w, h) in caras:
                cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            cv2.putText(frame, f'{count}/{MAX_IMAGENES}', (10, 25), 2, 0.8, (255, 255, 0), 1, cv2.LINE_AA)
            cv2.imshow('frame', frame)

            if cv2.waitKey(1) == TECLA_ESC:
                detenido_esc = True
                log.warning('Captura detenida con ESC')
                break
    except KeyboardInterrupt:
        log.warning('Captura interrumpida con Ctrl+C')
    finally:
        cap.release()
        cv2.destroyAllWindows()

    total = time.perf_counter() - t_inicio
    log.info('=== Resumen de captura ===')
    log.info(f'Persona: {nombre} | carpeta: {persona_path}')
    log.info(f'Imágenes guardadas: {count}/{MAX_IMAGENES} ({TAM_ROSTRO[0]}x{TAM_ROSTRO[1]} px)')
    log.info(f'Tiempo total: {total:.2f} s | tasa: {count / total if total > 0 else 0:.1f} imágenes/s')
    log.info(f'Cuadros procesados: {cuadros} | FPS efectivos: {cuadros / total if total > 0 else 0:.1f}')
    log.info(f'Cuadros sin rostro: {sin_rostro} | con varios rostros (descartados): {multiples}')
    log.info(f'Errores de escritura: {errores_escritura}')
    log.info(f'Detección Haar por cuadro: {resumen_tiempos(t_deteccion)}')
    if detenido_esc:
        log.info('Terminó por ESC antes de completar las imágenes')

    return 0 if count >= MAX_IMAGENES else 2


if __name__ == '__main__':
    sys.exit(main())
