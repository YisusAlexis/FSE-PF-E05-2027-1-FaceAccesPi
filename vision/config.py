"""
config.py - Parámetros del sistema FaceAccessPi en un solo lugar.

Cualquier valor puede cambiarse con una variable de entorno FACEACCESS_<NOMBRE>,
por ejemplo FACEACCESS_CAMARA=1 o FACEACCESS_UMBRAL_COSENO=0.40.
"""
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(nombre, defecto, tipo=str):
    valor = os.environ.get('FACEACCESS_' + nombre)
    return defecto if valor is None else tipo(valor)


# ------------------------------ Archivos ---------------------------------
MODELO_YUNET = _env('MODELO_YUNET', os.path.join(BASE, 'modelos', 'face_detection_yunet_2023mar.onnx'))
MODELO_SFACE = _env('MODELO_SFACE', os.path.join(BASE, 'modelos', 'face_recognition_sface_2021dec.onnx'))
RUTA_DB = _env('DB', os.path.join(BASE, 'datos', 'faceaccess.db'))
CARPETA_LOGS = _env('LOGS', os.path.join(BASE, 'logs'))
FIFO_LED = _env('FIFO_LED', '/tmp/faceaccess_led')     # Decisión bitácora clase 11 (cambio)

# ------------------------------- Cámara ----------------------------------
CAMARA = _env('CAMARA', '0')            # índice de /dev/videoN o ruta de un video de prueba
ANCHO = _env('ANCHO', 640, int)
ALTO = _env('ALTO', 480, int)
FPS_PROCESO = _env('FPS_PROCESO', 10, float)   # Decisión bitácora clase 8: hasta 10 imágenes/s
FALLOS_PARA_DESCONEXION = 10                   # lecturas fallidas seguidas = cámara perdida
REINTENTO_CAMARA_S = 2.0

# ----------------------------- Biometría ---------------------------------
UMBRAL_DETECCION = _env('UMBRAL_DETECCION', 0.90, float)   # confianza mínima de YuNet
UMBRAL_COSENO = _env('UMBRAL_COSENO', 0.363, float)        # SFace: >= umbral = misma persona
TAM_MIN_ROSTRO = _env('TAM_MIN_ROSTRO', 80, int)           # px de ancho; más chico = muy lejos

# --------------------------- Decisión (RF-05) -----------------------------
CUADROS_CONFIRMACION = _env('CUADROS_CONFIRMACION', 3, int)  # cuadros seguidos con el mismo resultado
REARME_S = 2.0              # RF-05: rearmar tras 2 s sin rostro
PERMANENCIA_MAX_S = 10.0    # RF-05: máximo un evento por 10 s de permanencia continua
RECARGA_DB_S = 3.0          # cada cuánto se revisa si el administrador cambió personas

# ------------------------------ Registro ---------------------------------
MUESTRAS_REGISTRO = _env('MUESTRAS', 15, int)
INTERVALO_MUESTRA_S = 0.25
TIEMPO_MAX_REGISTRO_S = 60

# ---------------------------- Visualización ------------------------------
VENTANA = _env('VENTANA', '1') == '1'     # iniciar_vision.sh la apaga si no hay pantalla
PANTALLA_COMPLETA = _env('PANTALLA_COMPLETA', '0') == '1'
INTERVALO_METRICAS_S = 60
