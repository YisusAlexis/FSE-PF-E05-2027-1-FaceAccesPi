"""
entrenador.py - Entrena el modelo EigenFaces con las carpetas de ./Reconocimiento.

Muestra en consola (y guarda en logs/) cada paso con tiempos:
lectura de imágenes por persona, archivos omitidos y por qué,
duración del entrenamiento, tamaño del modelo y del mapa de etiquetas.
"""
import cv2
import os
import sys
import json
import time
import logging
import platform
import numpy as np

# ----------------------------- Configuración -----------------------------
DATA_PATH = './Reconocimiento'
ARCHIVO_MODELO = 'modeloEigenFace.xml'
ARCHIVO_ETIQUETAS = 'etiquetas.json'     # mapa etiqueta -> nombre, lo usa el reconocedor
CARPETA_LOGS = './logs'
TAM_ROSTRO = (150, 150)
NUM_COMPONENTES = 80                     # Decisión bitácora clase 15: modelo más pequeño
MOSTRAR_IMAGENES = True                  # ventana con cada imagen que se procesa
ESPERA_VENTANA_MS = 10                   # tiempo que se muestra cada imagen
EXTENSIONES = ('.jpg', '.jpeg', '.png')


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


def main():
    log, ruta_log = configurar_log('entrenador')
    log.info('=== Entrenamiento EigenFaces ===')
    log.info(f'Python {platform.python_version()} | OpenCV {cv2.__version__} | '
             f'{platform.system()} {platform.release()}')
    log.info(f'Log de esta ejecución: {ruta_log}')

    if not hasattr(cv2, 'face'):
        log.error('cv2.face no existe: instala solo opencv-contrib-python '
                  '(desinstala opencv-python si están los dos)')
        return 1
    if not os.path.isdir(DATA_PATH):
        log.error(f'No existe la carpeta de datos {DATA_PATH}')
        return 1

    # sorted(): mismo orden siempre; el mapa se guarda para el reconocedor
    personas = sorted(d for d in os.listdir(DATA_PATH)
                      if os.path.isdir(os.path.join(DATA_PATH, d)))
    if not personas:
        log.error(f'No hay carpetas de personas en {DATA_PATH}')
        return 1
    log.info(f'Personas encontradas ({len(personas)}): {personas}')

    mostrar = MOSTRAR_IMAGENES
    if mostrar and platform.system() == 'Linux' and not os.environ.get('DISPLAY'):
        log.warning('No hay pantalla (p. ej. Raspberry Pi por SSH); se entrena sin ventana')
        mostrar = False
    t_ventana = 0.0      # tiempo dedicado a la ventana, se reporta aparte

    labels = []
    facesData = []
    etiquetas = {}
    omitidos = 0
    t_lectura_total = time.perf_counter()

    for label, nombre in enumerate(personas):
        persona_path = os.path.join(DATA_PATH, nombre)
        t0 = time.perf_counter()
        t_ventana_persona = 0.0
        leidas = 0
        for fileName in sorted(os.listdir(persona_path)):
            ruta = os.path.join(persona_path, fileName)
            if not fileName.lower().endswith(EXTENSIONES):
                omitidos += 1
                log.warning(f'Omitido (no es imagen): {ruta}')
                continue
            imagen = cv2.imread(ruta, cv2.IMREAD_GRAYSCALE)
            if imagen is None:
                omitidos += 1
                log.warning(f'Omitido (no se pudo leer o está dañado): {ruta}')
                continue
            if imagen.shape[::-1] != TAM_ROSTRO:
                log.debug(f'Redimensionada {ruta} de {imagen.shape[::-1]} a {TAM_ROSTRO}')
                imagen = cv2.resize(imagen, TAM_ROSTRO, interpolation=cv2.INTER_CUBIC)
            facesData.append(imagen)
            labels.append(label)
            leidas += 1
            if mostrar:
                tv = time.perf_counter()
                vista = cv2.cvtColor(imagen, cv2.COLOR_GRAY2BGR)
                cv2.putText(vista, f'{nombre} {leidas}', (5, 15), 1, 0.9, (0, 255, 0), 1, cv2.LINE_AA)
                cv2.imshow('imagen', vista)
                cv2.waitKey(ESPERA_VENTANA_MS)
                ms_v = (time.perf_counter() - tv) * 1000
                t_ventana += ms_v / 1000
                t_ventana_persona += ms_v
        ms = (time.perf_counter() - t0) * 1000 - t_ventana_persona
        etiquetas[label] = nombre
        if leidas == 0:
            log.warning(f'[{label}] {nombre}: 0 imágenes válidas')
        else:
            log.info(f'[{label}] {nombre}: {leidas} imágenes leídas en {ms:.0f} ms '
                     f'({ms / leidas:.2f} ms por imagen, sin contar la ventana)')

    if mostrar:
        cv2.destroyAllWindows()

    t_lectura = time.perf_counter() - t_lectura_total - t_ventana
    log.info(f'Lectura terminada: {len(facesData)} imágenes en {t_lectura:.2f} s, {omitidos} omitidas')
    if mostrar:
        log.info(f'Tiempo mostrando la ventana: {t_ventana:.2f} s (no se suma a la lectura)')

    if len(facesData) < 2:
        log.error('Se necesitan al menos 2 imágenes válidas para entrenar')
        return 1

    componentes = min(NUM_COMPONENTES, len(facesData) - 1)
    log.info(f'Entrenamiento en ejecución con {componentes} componentes...')
    t0 = time.perf_counter()
    try:
        reconocedor = cv2.face.EigenFaceRecognizer_create(num_components=componentes)
        reconocedor.train(facesData, np.array(labels))
    except cv2.error as e:
        log.error(f'Falló el entrenamiento: {e}')
        return 1
    t_entrenamiento = time.perf_counter() - t0
    log.info(f'Entrenamiento terminado en {t_entrenamiento:.2f} s')

    # Decisión bitácora clase 15: escribir en temporal y renombrar (operación atómica)
    t0 = time.perf_counter()
    tmp_modelo = ARCHIVO_MODELO + '.tmp.xml'
    tmp_etiquetas = ARCHIVO_ETIQUETAS + '.tmp'
    try:
        reconocedor.write(tmp_modelo)
        with open(tmp_etiquetas, 'w', encoding='utf-8') as f:
            json.dump({str(k): v for k, v in etiquetas.items()}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_modelo, ARCHIVO_MODELO)
        os.replace(tmp_etiquetas, ARCHIVO_ETIQUETAS)
    except (cv2.error, OSError) as e:
        log.error(f'No se pudo guardar el modelo: {e}')
        return 1
    t_guardado = time.perf_counter() - t0

    tam_modelo = os.path.getsize(ARCHIVO_MODELO) / (1024 * 1024)
    log.info(f'Modelo almacenado: {ARCHIVO_MODELO} ({tam_modelo:.2f} MB) en {t_guardado:.2f} s')
    log.info(f'Mapa de etiquetas: {ARCHIVO_ETIQUETAS} -> {etiquetas}')

    log.info('=== Resumen de entrenamiento ===')
    log.info(f'Personas: {len(personas)} | imágenes usadas: {len(facesData)} | omitidas: {omitidos}')
    for label, nombre in etiquetas.items():
        log.info(f'  [{label}] {nombre}: {labels.count(label)} imágenes')
    log.info(f'Componentes: {componentes}')
    log.info(f'Tiempos -> lectura: {t_lectura:.2f} s | entrenamiento: {t_entrenamiento:.2f} s | '
             f'guardado: {t_guardado:.2f} s | total: {t_lectura + t_entrenamiento + t_guardado:.2f} s')
    log.info(f'Tamaño del modelo: {tam_modelo:.2f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
