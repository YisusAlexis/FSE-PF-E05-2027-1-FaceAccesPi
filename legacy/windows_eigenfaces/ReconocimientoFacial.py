"""
ReconocimientoFacial.py - Reconocimiento en vivo con EigenFaces.

Muestra en consola (y guarda en logs/) lo que ocurre y mide:
  - tiempos por cuadro (detección, predicción, total) y FPS
  - distancia de cada predicción (sirve para calibrar UMBRAL_DISTANCIA)
  - "presentaciones": cada vez que un rostro aparece y luego se va

Para obtener métricas de las pruebas P-03 y P-04 se usa el MODO DE PRUEBA:
  tecla r -> frente a la cámara está una persona REGISTRADA (se espera verde)
  tecla n -> frente a la cámara está una persona NO registrada (se espera rojo)
  tecla l -> modo libre, sin resultado esperado
  ESC     -> salir y mostrar el resumen
Cada presentación se califica como correcta o incorrecta según el modo activo.
Todo queda también en un CSV por cuadro y otro por presentación en logs/.
"""
import cv2
import os
import sys
import csv
import json
import time
import logging
import platform
from collections import Counter
import numpy as np

# ----------------------------- Configuración -----------------------------
DATA_PATH = './Reconocimiento'
ARCHIVO_MODELO = 'modeloEigenFace.xml'
ARCHIVO_ETIQUETAS = 'etiquetas.json'
CASCADE_FILE = 'haarcascade_frontalface_default.xml'
CARPETA_LOGS = './logs'
INDICE_CAMARA = 0
TAM_ROSTRO = (150, 150)
UMBRAL_DISTANCIA = 4000       # distancia menor = persona registrada; calibrar con el resumen
SCALE_FACTOR = 1.3
MIN_NEIGHBORS = 5
TECLA_ESC = 27
FIN_PRESENTACION_S = 0.7      # segundos sin rostro para dar por terminada una presentación
MAX_FALLOS_LECTURA = 30
VERDE = (0, 255, 0)
ROJO = (0, 0, 255)

MODOS = {
    ord('r'): 'REGISTRADO',
    ord('n'): 'NO_REGISTRADO',
    ord('l'): 'LIBRE',
}


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
    return log, ruta, marca


def resumen(valores, unidad='ms'):
    if not valores:
        return 'sin datos'
    a = np.array(valores, dtype=float)
    return (f'n={len(a)} | prom {a.mean():.1f} {unidad} | mín {a.min():.1f} | '
            f'p95 {np.percentile(a, 95):.1f} | máx {a.max():.1f}')


def cargar_etiquetas(log):
    if os.path.isfile(ARCHIVO_ETIQUETAS):
        with open(ARCHIVO_ETIQUETAS, encoding='utf-8') as f:
            etiquetas = {int(k): v for k, v in json.load(f).items()}
        log.info(f'Etiquetas cargadas de {ARCHIVO_ETIQUETAS}: {etiquetas}')
        return etiquetas
    log.warning(f'No existe {ARCHIVO_ETIQUETAS}; se usa el orden alfabético de {DATA_PATH}. '
                f'Vuelve a ejecutar entrenador.py para generarlo')
    if not os.path.isdir(DATA_PATH):
        return {}
    personas = sorted(d for d in os.listdir(DATA_PATH) if os.path.isdir(os.path.join(DATA_PATH, d)))
    return dict(enumerate(personas))


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
    log.info(f'Cámara {indice} abierta en {ms:.0f} ms ({ancho}x{alto})')
    return cap


class Presentaciones:
    """Agrupa cuadros consecutivos con rostro en una 'presentación' y la califica."""

    def __init__(self, log, escritor):
        self.log = log
        self.escritor = escritor
        self.actual = None
        self.historial = []

    def cuadro_con_rostro(self, ahora, t_lectura, aceptado, nombre, distancia, modo):
        if self.actual is None:
            self.actual = {
                'id': len(self.historial) + 1,
                'modo': modo,
                'inicio': ahora,
                'latencia_ms': (ahora - t_lectura) * 1000,   # lectura del cuadro -> color en pantalla
                'primer_resultado': 'ACEPTADO' if aceptado else 'RECHAZADO',
                'resultados': [],
                'nombres': [],
                'distancias': [],
            }
            self.log.info(f'Presentación {self.actual["id"]} iniciada (modo {modo}): '
                          f'primer resultado {self.actual["primer_resultado"]} '
                          f'en {self.actual["latencia_ms"]:.1f} ms, distancia {distancia:.0f}')
        p = self.actual
        p['ultimo'] = ahora
        p['resultados'].append(aceptado)
        p['distancias'].append(distancia)
        if aceptado:
            p['nombres'].append(nombre)

    def revisar_fin(self, ahora, forzar=False):
        p = self.actual
        if p is None:
            return
        if not forzar and ahora - p['ultimo'] < FIN_PRESENTACION_S:
            return
        n = len(p['resultados'])
        aceptados = sum(p['resultados'])
        veredicto = 'ACEPTADO' if aceptados > n / 2 else 'RECHAZADO'
        nombre = Counter(p['nombres']).most_common(1)[0][0] if p['nombres'] else '-'
        if p['modo'] == 'REGISTRADO':
            correcto = veredicto == 'ACEPTADO'
        elif p['modo'] == 'NO_REGISTRADO':
            correcto = veredicto == 'RECHAZADO'
        else:
            correcto = None
        p.update({
            'duracion_s': p['ultimo'] - p['inicio'],
            'cuadros': n,
            'cuadros_aceptados': aceptados,
            'veredicto': veredicto,
            'nombre': nombre,
            'dist_min': min(p['distancias']),
            'dist_prom': float(np.mean(p['distancias'])),
            'correcto': correcto,
        })
        texto = {True: 'CORRECTA', False: 'INCORRECTA', None: 'sin calificar'}[correcto]
        self.log.info(f'Presentación {p["id"]} terminada: {veredicto} ({nombre}) | '
                      f'{aceptados}/{n} cuadros aceptados | {p["duracion_s"]:.1f} s | '
                      f'distancia mín {p["dist_min"]:.0f}, prom {p["dist_prom"]:.0f} | {texto}')
        self.escritor.writerow([p['id'], p['modo'], p['primer_resultado'], f'{p["latencia_ms"]:.1f}',
                                veredicto, nombre, n, aceptados, f'{p["duracion_s"]:.2f}',
                                f'{p["dist_min"]:.0f}', f'{p["dist_prom"]:.0f}', texto])
        self.historial.append(p)
        self.actual = None


def main():
    log, ruta_log, marca = configurar_log('reconocimiento')
    log.info('=== Reconocimiento facial ===')
    log.info(f'Python {platform.python_version()} | OpenCV {cv2.__version__} | '
             f'{platform.system()} {platform.release()}')
    log.info(f'Log de esta ejecución: {ruta_log}')
    log.info(f'Umbral de distancia: {UMBRAL_DISTANCIA}')

    if not hasattr(cv2, 'face'):
        log.error('cv2.face no existe: instala solo opencv-contrib-python')
        return 1
    if not os.path.isfile(ARCHIVO_MODELO):          # Prueba F-02
        log.error(f'No se encontró el modelo {ARCHIVO_MODELO}; ejecuta entrenador.py primero')
        return 1

    etiquetas = cargar_etiquetas(log)
    if not etiquetas:
        log.error('No hay etiquetas de personas; ejecuta entrenador.py')
        return 1

    t0 = time.perf_counter()
    try:
        reconocedor = cv2.face.EigenFaceRecognizer_create()
        reconocedor.read(ARCHIVO_MODELO)
    except cv2.error as e:
        log.error(f'No se pudo cargar el modelo: {e}')
        return 1
    log.info(f'Modelo cargado en {(time.perf_counter() - t0) * 1000:.0f} ms '
             f'({os.path.getsize(ARCHIVO_MODELO) / (1024 * 1024):.2f} MB)')

    clasificador = cv2.CascadeClassifier(CASCADE_FILE)
    if clasificador.empty():
        log.error(f'No se pudo cargar el clasificador {CASCADE_FILE}')
        return 1

    cap = abrir_camara(log, INDICE_CAMARA)          # Prueba F-01
    if cap is None:
        return 1

    f_cuadros = open(os.path.join(CARPETA_LOGS, f'reconocimiento_cuadros_{marca}.csv'),
                     'w', newline='', encoding='utf-8')
    f_pres = open(os.path.join(CARPETA_LOGS, f'reconocimiento_presentaciones_{marca}.csv'),
                  'w', newline='', encoding='utf-8')
    csv_cuadros = csv.writer(f_cuadros)
    csv_cuadros.writerow(['t_s', 'cuadro', 'modo', 'n_rostros', 'etiqueta', 'nombre', 'distancia',
                          'resultado', 't_deteccion_ms', 't_prediccion_ms', 't_cuadro_ms'])
    csv_pres = csv.writer(f_pres)
    csv_pres.writerow(['presentacion', 'modo', 'primer_resultado', 'latencia_ms', 'veredicto',
                       'nombre', 'cuadros', 'cuadros_aceptados', 'duracion_s',
                       'distancia_min', 'distancia_prom', 'calificacion'])
    presentaciones = Presentaciones(log, csv_pres)

    modo = 'LIBRE'
    cuadros = 0
    cuadros_con_rostro = 0
    fallos_seguidos = 0
    t_det, t_pred, t_cuadro = [], [], []
    dist_aceptadas, dist_rechazadas = [], []
    t_inicio = time.perf_counter()
    log.info('Teclas: r = persona registrada | n = persona no registrada | l = libre | ESC = salir')

    try:
        while True:
            ret, frame = cap.read()
            t_lectura = time.perf_counter()
            if not ret:
                fallos_seguidos += 1
                log.warning(f'No se pudo leer un cuadro ({fallos_seguidos} seguidos)')
                if fallos_seguidos >= MAX_FALLOS_LECTURA:
                    log.error('Demasiadas lecturas fallidas; ¿se desconectó la cámara?')
                    break
                continue
            fallos_seguidos = 0
            cuadros += 1

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            t0 = time.perf_counter()
            caras = clasificador.detectMultiScale(gray, SCALE_FACTOR, MIN_NEIGHBORS)
            ms_det = (time.perf_counter() - t0) * 1000
            t_det.append(ms_det)

            if len(caras) > 1:
                log.debug(f'Cuadro {cuadros}: {len(caras)} rostros; la presentación usa el más grande')
            principal = max(caras, key=lambda c: c[2] * c[3]) if len(caras) else None

            for (x, y, w, h) in caras:
                rostro = gray[y:y + h, x:x + w]
                rostro = cv2.resize(rostro, TAM_ROSTRO, interpolation=cv2.INTER_CUBIC)
                t0 = time.perf_counter()
                etiqueta, distancia = reconocedor.predict(rostro)
                ms_pred = (time.perf_counter() - t0) * 1000
                t_pred.append(ms_pred)

                aceptado = distancia < UMBRAL_DISTANCIA
                nombre = etiquetas.get(etiqueta, f'etiqueta_{etiqueta}')
                if aceptado:
                    dist_aceptadas.append(distancia)
                    cv2.putText(frame, nombre, (x, y - 25), 2, 1.1, VERDE, 1, cv2.LINE_AA)
                    cv2.rectangle(frame, (x, y), (x + w, y + h), VERDE, 2)
                else:
                    dist_rechazadas.append(distancia)
                    cv2.putText(frame, 'Desconocido', (x, y - 20), 2, 0.8, ROJO, 1, cv2.LINE_AA)
                    cv2.rectangle(frame, (x, y), (x + w, y + h), ROJO, 2)

                ms_cuadro = (time.perf_counter() - t_lectura) * 1000
                csv_cuadros.writerow([f'{t_lectura - t_inicio:.3f}', cuadros, modo, len(caras), etiqueta,
                                      nombre, f'{distancia:.0f}', 'ACEPTADO' if aceptado else 'RECHAZADO',
                                      f'{ms_det:.1f}', f'{ms_pred:.2f}', f'{ms_cuadro:.1f}'])
                if principal is not None and (x, y, w, h) == tuple(principal):
                    presentaciones.cuadro_con_rostro(time.perf_counter(), t_lectura,
                                                     aceptado, nombre, distancia, modo)

            if len(caras):
                cuadros_con_rostro += 1
            presentaciones.revisar_fin(time.perf_counter())

            cv2.putText(frame, f'Modo: {modo}  (r/n/l, ESC)', (10, 25), 2, 0.6,
                        (255, 255, 0), 1, cv2.LINE_AA)
            cv2.imshow('frame', frame)
            t_cuadro.append((time.perf_counter() - t_lectura) * 1000)

            k = cv2.waitKey(1) & 0xFF
            if k == TECLA_ESC:
                break
            if k in MODOS and MODOS[k] != modo:
                presentaciones.revisar_fin(time.perf_counter(), forzar=True)
                modo = MODOS[k]
                log.info(f'Modo cambiado a {modo}')
    except KeyboardInterrupt:
        log.warning('Interrumpido con Ctrl+C')
    finally:
        presentaciones.revisar_fin(time.perf_counter(), forzar=True)
        cap.release()
        cv2.destroyAllWindows()
        f_cuadros.close()
        f_pres.close()

    total = time.perf_counter() - t_inicio
    log.info('=== Resumen de reconocimiento ===')
    log.info(f'Duración: {total:.1f} s | cuadros: {cuadros} | FPS efectivos: '
             f'{cuadros / total if total > 0 else 0:.1f}')
    log.info(f'Cuadros con rostro: {cuadros_con_rostro}')
    log.info(f'Detección Haar: {resumen(t_det)}')
    log.info(f'Predicción EigenFaces: {resumen(t_pred)}')
    log.info(f'Cuadro completo (lectura -> pantalla): {resumen(t_cuadro)}')
    log.info(f'Distancias ACEPTADAS (< {UMBRAL_DISTANCIA}): {resumen(dist_aceptadas, "")}')
    log.info(f'Distancias RECHAZADAS (>= {UMBRAL_DISTANCIA}): {resumen(dist_rechazadas, "")}')

    hist = presentaciones.historial
    log.info(f'Presentaciones totales: {len(hist)}')
    for m in ('REGISTRADO', 'NO_REGISTRADO'):
        grupo = [p for p in hist if p['modo'] == m]
        if not grupo:
            continue
        ok = sum(1 for p in grupo if p['correcto'])
        esperado = 'aceptado (verde)' if m == 'REGISTRADO' else 'rechazado (rojo)'
        log.info(f'  Modo {m}: {ok}/{len(grupo)} correctas ({100 * ok / len(grupo):.1f} %), '
                 f'se esperaba {esperado}')
        log.info(f'    Latencia al primer resultado: {resumen([p["latencia_ms"] for p in grupo])}')
        log.info(f'    Distancia mínima por presentación: {resumen([p["dist_min"] for p in grupo], "")}')
    log.info(f'CSV por cuadro y por presentación guardados en {CARPETA_LOGS}/')
    return 0


if __name__ == '__main__':
    sys.exit(main())
