"""
faceaccess.py - Servicio principal del control de acceso (arranca con systemd).

Estructura (Avance 1, clases 8 y 9):
  - Hilo "captura":        lee la cámara y publica la imagen más reciente.
  - Cola de capacidad 1:   recurso compartido entre los dos hilos (queue.Queue = Lock + Condition).
  - Hilo "reconocimiento": YuNet + SFace, decisión, control de duplicados, evento en SQLite
                           y, solo después del commit, mensaje al LED por la FIFO.
  - Hilo principal:        ventana en el monitor (opcional) y métricas; menor prioridad.
Proceso aparte (clase 11, cambio): neopixel_spi en C lee la FIFO y maneja el NeoPixel por SPI.

Métricas que escribe en el log (y en journalctl -u faceaccess):
  SISTEMA LISTO a X s del encendido  -> RNF-02 (<= 60 s)
  latencia de cada evento            -> RNF-01 (p95 <= 2 s)
  memoria residente máxima           -> RNF-04 (< 512 MiB)
"""
import os
import sys
import time
import queue
import signal
import sqlite3
import threading

import cv2
import numpy as np

import config
import db
from comun import (Biometria, abrir_camara, configurar_log, dibujar, memoria_residente_mib,
                   resumen, segundos_desde_encendido)
from fifo_led import LedFifo

VERDE, ROJO, NARANJA, AMARILLO = (0, 255, 0), (0, 0, 255), (0, 165, 255), (0, 255, 255)


def fijar_nice(valor, log):
    """Decisión bitácora clase 8: prioridad por hilo (en Linux, setpriority acepta el TID)."""
    tid = threading.get_native_id()
    try:
        os.setpriority(os.PRIO_PROCESS, tid, valor)
    except (OSError, AttributeError) as e:
        log.debug(f'No se pudo fijar nice {valor}: {e}')
    try:
        log.info(f'Hilo {threading.current_thread().name} (TID {tid}) con nice '
                 f'{os.getpriority(os.PRIO_PROCESS, tid)}')
    except (OSError, AttributeError):
        pass


class Sistema:
    def __init__(self, log, bio):
        self.log = log
        self.bio = bio
        self.parar = threading.Event()
        # Decisión bitácora clase 9: la imagen es el recurso compartido; cola de capacidad 1.
        # El productor no vuelve a tocar una imagen después de publicarla.
        self.cola = queue.Queue(maxsize=1)
        # Segundo recurso compartido: la última imagen anotada para la pantalla.
        self.candado_vista = threading.Lock()
        self.vista = None
        self.camara_ok = threading.Event()
        self.led = LedFifo(config.FIFO_LED, log)   # solo lo usa el hilo de reconocimiento
        # métricas (las escribe un solo hilo cada una)
        self.cuadros_leidos = 0
        self.cuadros_descartados = 0
        self.cuadros_procesados = 0
        self.reconexiones = 0
        self.t_deteccion = []
        self.t_sface = []
        self.t_cuadro = []
        self.latencias = []
        self.eventos = {'REGISTRADO': 0, 'DESCONOCIDO': 0, 'FALLO_DB': 0}
        self.rss_max = 0.0
        self.listo_uptime = None
        self.n_personas = 0
        self.t_inicio = time.monotonic()

    # ------------------------------------------------------------ captura
    def publicar(self, elemento):
        try:
            self.cola.put_nowait(elemento)
        except queue.Full:
            try:
                self.cola.get_nowait()          # se descarta la imagen vieja: nunca se acumula
                self.cuadros_descartados += 1
            except queue.Empty:
                pass
            try:
                self.cola.put_nowait(elemento)
            except queue.Full:
                pass

    def hilo_captura(self):
        fijar_nice(0, self.log)
        es_archivo = not config.CAMARA.isdigit()
        intervalo = 1.0 / config.FPS_PROCESO
        cap = None
        fallos = 0
        t_publicado = 0.0
        t_perdida = None
        while not self.parar.is_set():
            if cap is None:
                cap = abrir_camara(config.CAMARA, self.log)
                if cap is None:
                    self.camara_ok.clear()
                    self.parar.wait(config.REINTENTO_CAMARA_S)
                    continue
                if t_perdida is not None:
                    self.log.warning(f'RECUPERACIÓN: cámara reconectada tras '
                                     f'{time.monotonic() - t_perdida:.1f} s')
                    t_perdida = None
                self.camara_ok.set()
                fallos = 0

            ok, imagen = cap.read()
            t = time.monotonic()
            if not ok:
                if es_archivo:                       # video de prueba: se repite
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                fallos += 1
                if fallos >= config.FALLOS_PARA_DESCONEXION:
                    self.log.error(f'FALLA: cámara desconectada ({fallos} lecturas fallidas); '
                                   f'reintento cada {config.REINTENTO_CAMARA_S:.0f} s')
                    cap.release()
                    cap = None
                    self.camara_ok.clear()
                    self.reconexiones += 1
                    t_perdida = time.monotonic()
                else:
                    self.parar.wait(0.05)
                continue
            fallos = 0
            self.cuadros_leidos += 1
            if es_archivo:
                self.parar.wait(1 / 30)
            if t - t_publicado < intervalo:
                continue
            t_publicado = t
            self.publicar((t, imagen))
        if cap is not None:
            cap.release()

    # ----------------------------------------------------- reconocimiento
    def emitir(self, con, nombre, similitud, t_captura):
        resultado = 'REGISTRADO' if nombre else 'DESCONOCIDO'
        try:
            id_evento = db.registrar_evento(con, nombre, resultado, similitud)
        except sqlite3.Error as e:
            self.eventos['FALLO_DB'] += 1
            self.log.error(f'FALLA: no se pudo guardar el evento ({e}); se señala FALLO')
            self.led.enviar('FALLO')
            return
        # RF-06: el LED solo se activa después del commit
        llego = self.led.enviar(resultado)
        latencia = (time.monotonic() - t_captura) * 1000
        self.latencias.append(latencia)
        self.eventos[resultado] += 1
        try:
            db.actualizar_latencia(con, id_evento, latencia)
        except sqlite3.Error:
            pass
        self.log.info(f'Evento #{id_evento}: {resultado} {nombre or ""} | similitud {similitud:.3f} | '
                      f'latencia {latencia:.0f} ms | LED {"enviado" if llego else "SIN SEÑAL"}')

    def hilo_reconocimiento(self):
        fijar_nice(0, self.log)
        con = db.abrir(config.RUTA_DB)          # conexión propia: este hilo es dueño del escritor
        nombres, matriz = db.cargar_personas(con)
        self.n_personas = len(nombres)
        version = db.version(con)
        t_revision = time.monotonic()
        self.log.info(f'Personas registradas: {len(nombres)} {nombres}')
        if not nombres:
            self.log.warning('No hay personas registradas: todo rostro será DESCONOCIDO')

        self.led.enviar('LISTO')
        camara_antes = True
        presentacion = None      # {'ultimo': t, 'racha': [...], 't_emitido': t o None}
        t_fps, n_fps, fps = time.monotonic(), 0, 0.0

        while not self.parar.is_set():
            ahora = time.monotonic()
            camara = self.camara_ok.is_set()
            # amarillo si la cámara falla (o no abre en los primeros 5 s); azul al recuperarse
            if camara != camara_antes and (camara or ahora - self.t_inicio > 5):
                self.led.enviar('LISTO' if camara else 'FALLO')
                camara_antes = camara

            if ahora - t_revision >= config.RECARGA_DB_S:   # ¿el administrador cambió personas?
                t_revision = ahora
                v = db.version(con)
                if v != version:
                    version = v
                    nombres, matriz = db.cargar_personas(con)
                    self.n_personas = len(nombres)
                    self.log.info(f'Personas actualizadas sin reiniciar: {len(nombres)} {nombres}')

            try:
                t_captura, imagen = self.cola.get(timeout=0.5)
            except queue.Empty:
                if presentacion and ahora - presentacion['ultimo'] >= config.REARME_S:
                    presentacion = None
                continue

            t0 = time.perf_counter()
            caras = self.bio.detectar(imagen)
            self.t_deteccion.append((time.perf_counter() - t0) * 1000)
            ahora = time.monotonic()
            texto = ''

            if len(caras) == 0:
                if presentacion and ahora - presentacion['ultimo'] >= config.REARME_S:
                    presentacion = None                 # RF-05: rearme tras 2 s sin rostro
                texto = 'Esperando rostro'
            elif len(caras) > 1:
                for c in caras:
                    dibujar(imagen, c, '', ROJO)
                texto = 'Una persona a la vez'
                if presentacion:
                    presentacion['ultimo'] = ahora
            elif caras[0][2] < config.TAM_MIN_ROSTRO:
                dibujar(imagen, caras[0], 'Acercate', NARANJA)
                if presentacion:
                    presentacion['ultimo'] = ahora
            else:
                cara = caras[0]
                t1 = time.perf_counter()
                vector = self.bio.vector(imagen, cara)
                self.t_sface.append((time.perf_counter() - t1) * 1000)
                if len(matriz):
                    sims = matriz @ vector
                    i = int(np.argmax(sims))
                    similitud = float(sims[i])
                    nombre = nombres[i] if similitud >= config.UMBRAL_COSENO else None
                else:
                    similitud, nombre = 0.0, None
                self.log.debug(f'Cuadro: mejor {nombres[i] if len(matriz) else "-"} similitud {similitud:.3f}')
                dibujar(imagen, cara, f'{nombre or "Desconocido"} {similitud:.2f}', VERDE if nombre else ROJO)

                if presentacion is None:
                    presentacion = {'racha': [], 't_emitido': None}
                presentacion['ultimo'] = ahora
                racha = presentacion['racha']
                racha.append(nombre or '?')
                del racha[:-config.CUADROS_CONFIRMACION]
                confirmado = (len(racha) == config.CUADROS_CONFIRMACION and len(set(racha)) == 1)
                t_emitido = presentacion['t_emitido']
                # RF-05: un evento por presentación; otro solo tras 10 s de permanencia continua
                if confirmado and (t_emitido is None or ahora - t_emitido >= config.PERMANENCIA_MAX_S):
                    self.emitir(con, nombre, similitud, t_captura)
                    presentacion['t_emitido'] = ahora

            self.cuadros_procesados += 1
            n_fps += 1
            if ahora - t_fps >= 1.0:
                fps, n_fps, t_fps = n_fps / (ahora - t_fps), 0, ahora
            self.t_cuadro.append((time.monotonic() - t_captura) * 1000)
            if self.listo_uptime is None:
                self.listo_uptime = segundos_desde_encendido()
                self.log.info(f'SISTEMA LISTO: primer cuadro procesado a {self.listo_uptime:.1f} s '
                              f'del encendido (RNF-02: <= 60 s)')

            cv2.putText(imagen, f'Personas: {self.n_personas} | {fps:.1f} img/s | {texto}', (10, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, AMARILLO, 2, cv2.LINE_AA)
            with self.candado_vista:
                self.vista = imagen
        con.close()

    # ---------------------------------------------------- métricas / pantalla
    def metricas(self, final=False):
        rss = memoria_residente_mib()
        self.rss_max = max(self.rss_max, rss)
        dur = time.monotonic() - self.t_inicio
        self.log.info(f'MÉTRICAS: {dur:.0f} s en ejecución | RSS {rss:.0f} MiB (máx {self.rss_max:.0f}, '
                      f'RNF-04 < 512) | procesados {self.cuadros_procesados} '
                      f'({self.cuadros_procesados / dur if dur else 0:.1f} img/s) | eventos {self.eventos} | '
                      f'reconexiones de cámara {self.reconexiones}')
        if self.latencias:
            self.log.info(f'MÉTRICAS: latencia de eventos (RNF-01, p95 <= 2000 ms): {resumen(self.latencias)}')
        if final:
            self.log.info(f'MÉTRICAS: detección YuNet: {resumen(self.t_deteccion)}')
            self.log.info(f'MÉTRICAS: vector SFace: {resumen(self.t_sface)}')
            self.log.info(f'MÉTRICAS: captura -> fin de proceso por cuadro: {resumen(self.t_cuadro)}')
            self.log.info(f'MÉTRICAS: cuadros leídos {self.cuadros_leidos}, descartados por la cola '
                          f'{self.cuadros_descartados}, FIFO enviados {self.led.enviados} / '
                          f'fallidos {self.led.fallidos}')
            if self.listo_uptime is not None:
                self.log.info(f'MÉTRICAS: sistema listo a {self.listo_uptime:.1f} s del encendido')

    def bucle_principal(self):
        ventana = config.VENTANA
        if ventana:
            try:
                cv2.namedWindow('FaceAccessPi', cv2.WINDOW_NORMAL)
                if config.PANTALLA_COMPLETA:
                    cv2.setWindowProperty('FaceAccessPi', cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
                self.log.info('Ventana abierta en el monitor')
            except cv2.error as e:
                self.log.warning(f'Sin ventana, se continúa sin pantalla: {e}')
                ventana = False
        fijar_nice(5, self.log)                 # la pantalla tiene menor prioridad (clase 8)
        t_metricas = time.monotonic()
        negro = np.zeros((config.ALTO, config.ANCHO, 3), np.uint8)
        while not self.parar.is_set():
            if ventana:
                with self.candado_vista:
                    v = self.vista
                if not self.camara_ok.is_set():
                    v = negro.copy()
                    cv2.putText(v, 'Camara desconectada - reintentando', (40, config.ALTO // 2),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, ROJO, 2, cv2.LINE_AA)
                if v is not None:
                    cv2.imshow('FaceAccessPi', v)
                if cv2.waitKey(40) == 27:
                    self.log.info('ESC presionado: se detiene el servicio')
                    self.parar.set()
            else:
                self.parar.wait(0.5)
            if time.monotonic() - t_metricas >= config.INTERVALO_METRICAS_S:
                t_metricas = time.monotonic()
                self.metricas()
        if ventana:
            cv2.destroyAllWindows()


def main():
    log = configurar_log('faceaccess')
    log.info(f'=== FaceAccessPi: inicio a {segundos_desde_encendido():.1f} s del encendido ===')
    log.info(f'Cámara {config.CAMARA} | {config.FPS_PROCESO:.0f} img/s | umbral coseno {config.UMBRAL_COSENO} | '
             f'ventana {"sí" if config.VENTANA else "no"}')
    try:
        bio = Biometria(log)
    except (FileNotFoundError, cv2.error) as e:
        log.error(f'No se pudieron cargar los modelos: {e}')
        LedFifo(config.FIFO_LED, log).enviar('FALLO')
        return 1

    sistema = Sistema(log, bio)

    def detener(signum, _frame):
        log.info(f'Señal {signal.Signals(signum).name}: deteniendo')
        sistema.parar.set()
    signal.signal(signal.SIGTERM, detener)
    signal.signal(signal.SIGINT, detener)

    hilos = [threading.Thread(target=sistema.hilo_captura, name='captura', daemon=True),
             threading.Thread(target=sistema.hilo_reconocimiento, name='reconocimiento', daemon=True)]
    for h in hilos:
        h.start()
    try:
        sistema.bucle_principal()
    finally:
        sistema.parar.set()
        for h in hilos:
            h.join(timeout=3)
        sistema.led.enviar('APAGADO')
        sistema.led.cerrar()
        log.info('=== Resumen final ===')
        sistema.metricas(final=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
