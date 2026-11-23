"""
faceaccess.py - Servicio principal del control de acceso (arranca con systemd).

Estructura (Avance 1, clases 8 y 9):
  - Hilo "captura":        lee la cámara y publica la imagen más reciente.
  - Cola de capacidad 1:   recurso compartido entre los dos hilos (queue.Queue = Lock + Condition).
  - Hilo "reconocimiento": YuNet + SFace, decisión, control de duplicados, reglas de asistencia
                           (asistencia.py), registro en SQLite y, solo después del commit,
                           mensaje al LED por la FIFO.
  - Hilo principal:        pantalla (framebuffer o ventana) y métricas; menor prioridad.
Proceso aparte (clase 11, cambio): neopixel_spi en C lee la FIFO y maneja el NeoPixel por SPI.

Métricas que escribe en el log (y en journalctl -u faceaccess):
  SISTEMA LISTO a X s del encendido  -> RNF-02 (<= 60 s)
  latencia de cada evento            -> RNF-01 (p95 <= 2 s)
  memoria residente máxima           -> RNF-04 (< 512 MiB)
  uso de CPU, temperatura y frecuencia del procesador
"""
import collections
import errno
import os
import subprocess
import sys
import time
import queue
import signal
import sqlite3
import threading

import cv2
import numpy as np

import config
from comun import (Biometria, abrir_camara, configurar_log, dibujar, memoria_residente_mib,
                   resumen, segundos_desde_encendido)
from cripto import Cripto, ErrorCripto
from db import Base
import asistencia
from fifo_led import LedFifo
from pantalla import Pantalla, componer, TONOS, ROJO as P_ROJO, AMARILLO as P_AMARILLO

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


def leer_archivo(ruta, divisor=1.0):
    try:
        with open(ruta) as f:
            return float(f.read().strip()) / divisor
    except (OSError, ValueError):
        return float('nan')


_reloj_cache = [0.0, False]


def reloj_sincronizado():
    """¿La hora está sincronizada por NTP? La Pi 4 no tiene RTC: al encender arranca con la
    última hora guardada hasta que NTP la corrige. Sin hora confiable no se registra asistencia."""
    if not config.REQUIERE_NTP:
        return True
    if os.path.exists('/run/systemd/timesync/synchronized'):       # systemd-timesyncd
        return True
    ahora = time.monotonic()
    if ahora - _reloj_cache[0] > 30:                                # timedatectl, cada 30 s
        _reloj_cache[0] = ahora
        try:
            salida = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                                    capture_output=True, text=True, timeout=3).stdout.strip()
            _reloj_cache[1] = salida == 'yes'
        except (OSError, subprocess.SubprocessError):
            _reloj_cache[1] = False
    return _reloj_cache[1]


class Sistema:
    def __init__(self, log, bio, cripto):
        self.log = log
        self.bio = bio
        self.cripto = cripto
        self.parar = threading.Event()
        # Decisión bitácora clase 9: la imagen es el recurso compartido; cola de capacidad 1.
        # El productor no vuelve a tocar una imagen después de publicarla.
        self.cola = queue.Queue(maxsize=1)
        # Segundo recurso compartido: lo que muestra la pantalla (imagen anotada, aviso,
        # mensaje del último registro e historial), protegido con un Lock.
        self.candado_vista = threading.Lock()
        self.vista = None
        self.aviso = ''
        self.mensaje = None
        self.historial = collections.deque(maxlen=8)
        self.fps = 0.0
        self.camara_ok = threading.Event()
        self.led = LedFifo(config.FIFO_LED, log)   # solo lo usa el hilo de reconocimiento
        # métricas (las escribe un solo hilo cada una)
        self.cuadros_leidos = 0
        self.cuadros_descartados = 0
        self.cuadros_procesados = 0
        self.reconexiones = 0
        self.t_deteccion = collections.deque(maxlen=5000)
        self.t_sface = collections.deque(maxlen=5000)
        self.t_cuadro = collections.deque(maxlen=5000)
        self.latencias = []
        self.eventos = collections.Counter()
        self.perfil = asistencia.cargar_perfil()
        self.reloj_ok = True
        self.rss_max = 0.0
        self.listo_uptime = None
        self.n_personas = 0
        self.t_inicio = time.monotonic()
        self.cpu_previo = (time.monotonic(), sum(os.times()[:2]))

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
    def avisar(self, titulo, lineas, tono, historial_txt=None):
        color = TONOS.get(tono, P_AMARILLO)
        with self.candado_vista:
            self.mensaje = {'titulo': titulo, 'lineas': list(lineas), 'color': color,
                            'expira': time.monotonic() + config.DURACION_MENSAJE_S}
            if historial_txt:
                self.historial.append((time.strftime('%H:%M:%S'), historial_txt, color))

    def emitir(self, base, motor, clase, persona_id, nombre, similitud, t_captura):
        """clase: 'persona' (>= umbral de identidad), 'dudoso' o 'desconocido'."""
        hora = time.strftime('%H:%M:%S')
        try:
            if clase == 'dudoso':
                resultado, led = 'AVISO', 'AVISO'
                self.avisar('ROSTRO NO CLARO', ['Acerquese o quedese quieto', f'Similitud {similitud:.2f}'],
                            'amarillo')
            elif clase == 'desconocido':
                resultado, led = 'DESCONOCIDO', 'DESCONOCIDO'
                self.avisar('NO RECONOCIDO', ['Persona no registrada', f'Hora: {hora}'], 'rojo', 'Desconocido')
            elif not reloj_sincronizado():
                resultado, led = 'RELOJ', 'AVISO'
                self.avisar('RELOJ SIN SINCRONIZAR', ['No se registro nada', 'Espere la conexion a internet'],
                            'amarillo')
            else:
                r = motor.procesar(persona_id, nombre, similitud)        # commit del registro
                resultado, led = r.accion, r.led
                if r.accion == 'IGNORADO':                               # regla 2: sin aviso ni LED
                    self.log.debug(f'{nombre}: ignorado ({r.detalle})')
                    self.eventos['IGNORADO'] += 1
                    return
                self.avisar(r.titulo, r.lineas, r.tono, f'{nombre}  {r.accion.lower()} {r.detalle.lower()}')
            id_evento = base.registrar_evento(persona_id, resultado, similitud)
        except sqlite3.Error as e:
            self.eventos['FALLO_DB'] += 1
            self.log.error(f'FALLA: no se pudo guardar el evento ({e}); se señala FALLO')
            self.led.enviar('FALLO')
            return
        # RF-06: el LED solo se activa después del commit
        llego = self.led.enviar(led) if led else True
        latencia = (time.monotonic() - t_captura) * 1000
        self.latencias.append(latencia)
        self.eventos[resultado] += 1
        try:
            base.actualizar_latencia(id_evento, latencia)
        except sqlite3.Error:
            pass
        self.log.info(f'Evento #{id_evento}: {resultado} {nombre or ""} | similitud {similitud:.3f} | '
                      f'latencia {latencia:.0f} ms | LED {"enviado" if llego else "SIN SEÑAL"}')

    def hilo_reconocimiento(self):
        fijar_nice(0, self.log)
        base = Base(config.RUTA_DB, self.cripto, self.log)   # conexión propia: dueño del escritor
        motor = asistencia.Motor(base, self.perfil, self.log)
        conf_mtime = self._mtime_conf()
        self.log.info(f'Asistencia en modo {self.perfil.resumen()}')
        ids, nombres, matriz = base.personas()
        self.n_personas = len(nombres)
        version = base.version()
        t_revision = time.monotonic()
        self.log.info(f'Personas registradas: {len(nombres)} {nombres}')
        if not nombres:
            self.log.warning('No hay personas registradas: todo rostro será DESCONOCIDO')

        self.led.enviar('LISTO')
        camara_antes = True
        presentacion = None      # {'ultimo': t, 'racha': [...], 't_emitido': t o None}
        t_fps, n_fps = time.monotonic(), 0

        while not self.parar.is_set():
            ahora = time.monotonic()
            camara = self.camara_ok.is_set()
            # amarillo si la cámara falla (o no abre en los primeros 5 s); azul al recuperarse
            if camara != camara_antes and (camara or ahora - self.t_inicio > 5):
                self.led.enviar('LISTO' if camara else 'FALLO')
                camara_antes = camara

            if ahora - t_revision >= config.RECARGA_DB_S:   # ¿el administrador cambió personas?
                t_revision = ahora
                v = base.version()
                if v != version:
                    version = v
                    ids, nombres, matriz = base.personas()
                    self.n_personas = len(nombres)
                    self.log.info(f'Personas actualizadas sin reiniciar: {len(nombres)} {nombres}')
                m = self._mtime_conf()
                if m != conf_mtime:                          # scripts/asistencia.sh cambió el modo
                    conf_mtime = m
                    self.perfil = asistencia.cargar_perfil()
                    motor.perfil = self.perfil
                    self.log.info(f'Modo de asistencia cambiado sin reiniciar: {self.perfil.resumen()}')
                self.reloj_ok = reloj_sincronizado()

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
            aviso = ''

            if len(caras) == 0:
                if presentacion and ahora - presentacion['ultimo'] >= config.REARME_S:
                    presentacion = None                 # RF-05: rearme tras 2 s sin rostro
            elif len(caras) > 1:
                for c in caras:
                    dibujar(imagen, c, '', ROJO)
                aviso = 'Una persona a la vez'
                if presentacion:
                    presentacion['ultimo'] = ahora
            elif caras[0][2] < config.TAM_MIN_ROSTRO:
                dibujar(imagen, caras[0], 'Acercate', NARANJA)
                aviso = 'Acerquese a la camara'
                if presentacion:
                    presentacion['ultimo'] = ahora
            else:
                cara = caras[0]
                t1 = time.perf_counter()
                vector = self.bio.vector(imagen, cara)
                self.t_sface.append((time.perf_counter() - t1) * 1000)
                persona_id, nombre, similitud, clase = None, None, 0.0, 'desconocido'
                if len(matriz):
                    sims = matriz @ vector
                    i = int(np.argmax(sims))
                    similitud = float(sims[i])
                    # Regla 3 (calidad biométrica): >= 0.70 identifica; entre 0.363 y 0.70 es dudoso
                    if similitud >= self.perfil.umbral:
                        persona_id, nombre, clase = ids[i], nombres[i], 'persona'
                    elif similitud >= config.UMBRAL_COSENO:
                        clase = 'dudoso'
                    self.log.debug(f'Cuadro: mejor {nombres[i]} similitud {similitud:.3f}')
                color = {'persona': VERDE, 'dudoso': AMARILLO, 'desconocido': ROJO}[clase]
                etiqueta = nombre or ('Acerquese' if clase == 'dudoso' else 'Desconocido')
                dibujar(imagen, cara, f'{etiqueta} {similitud:.2f}', color)
                if clase == 'dudoso':
                    aviso = 'Rostro no reconocido con claridad: acerquese o quedese quieto'

                if presentacion is None:
                    presentacion = {'racha': [], 't_emitido': None, 't_aviso': None}
                presentacion['ultimo'] = ahora
                racha = presentacion['racha']
                racha.append(nombre or clase)
                del racha[:-config.CUADROS_CONFIRMACION]
                confirmado = (len(racha) == config.CUADROS_CONFIRMACION and len(set(racha)) == 1)
                if confirmado and clase == 'dudoso':
                    # el aviso no cierra la presentación: si después se ve claro, sí se registra
                    t_aviso = presentacion['t_aviso']
                    if t_aviso is None or ahora - t_aviso >= 3.0:
                        self.emitir(base, motor, clase, None, None, similitud, t_captura)
                        presentacion['t_aviso'] = ahora
                elif confirmado:
                    t_emitido = presentacion['t_emitido']
                    # RF-05: un evento por presentación; otro solo tras 10 s de permanencia continua
                    if t_emitido is None or ahora - t_emitido >= config.PERMANENCIA_MAX_S:
                        self.emitir(base, motor, clase, persona_id, nombre, similitud, t_captura)
                        presentacion['t_emitido'] = ahora

            self.cuadros_procesados += 1
            n_fps += 1
            if ahora - t_fps >= 1.0:
                self.fps, n_fps, t_fps = n_fps / (ahora - t_fps), 0, ahora
            self.t_cuadro.append((time.monotonic() - t_captura) * 1000)
            if self.listo_uptime is None:
                self.listo_uptime = segundos_desde_encendido()
                self.log.info(f'SISTEMA LISTO: primer cuadro procesado a {self.listo_uptime:.1f} s '
                              f'del encendido (RNF-02: <= 60 s)')
            with self.candado_vista:
                self.vista = imagen
                self.aviso = aviso
        base.cerrar()

    # ---------------------------------------------------- métricas / pantalla
    def _mtime_conf(self):
        try:
            return os.stat(asistencia.RUTA_CONF).st_mtime
        except OSError:
            return None

    def uso_cpu(self):
        """% de CPU del proceso desde la última llamada (100 % = un núcleo; la Pi 4 tiene 4)."""
        t, cpu = time.monotonic(), sum(os.times()[:2])
        t0, cpu0 = self.cpu_previo
        self.cpu_previo = (t, cpu)
        return 100 * (cpu - cpu0) / (t - t0) if t > t0 else 0.0

    def metricas(self, final=False):
        rss = memoria_residente_mib()
        self.rss_max = max(self.rss_max, rss)
        dur = time.monotonic() - self.t_inicio
        temp = leer_archivo('/sys/class/thermal/thermal_zone0/temp', 1000)
        mhz = leer_archivo('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 1000)
        self.log.info(f'MÉTRICAS: {dur:.0f} s en ejecución | RSS {rss:.0f} MiB (máx {self.rss_max:.0f}, '
                      f'RNF-04 < 512) | CPU {self.uso_cpu():.0f} % (de 400 %) | {temp:.1f} °C | '
                      f'ARM {mhz:.0f} MHz | procesados {self.cuadros_procesados} '
                      f'({self.cuadros_procesados / dur if dur else 0:.1f} img/s) | eventos {dict(self.eventos)} | '
                      f'reconexiones de cámara {self.reconexiones}')
        if self.latencias:
            self.log.info(f'MÉTRICAS: latencia de eventos (RNF-01, p95 <= 2000 ms): {resumen(self.latencias)}')
        if final:
            self.log.info(f'MÉTRICAS: detección YuNet: {resumen(list(self.t_deteccion))}')
            self.log.info(f'MÉTRICAS: vector SFace: {resumen(list(self.t_sface))}')
            self.log.info(f'MÉTRICAS: captura -> fin de proceso por cuadro: {resumen(list(self.t_cuadro))}')
            self.log.info(f'MÉTRICAS: cuadros leídos {self.cuadros_leidos}, descartados por la cola '
                          f'{self.cuadros_descartados}, FIFO enviados {self.led.enviados} / '
                          f'fallidos {self.led.fallidos}')
            if self.listo_uptime is not None:
                self.log.info(f'MÉTRICAS: sistema listo a {self.listo_uptime:.1f} s del encendido')

    def abrir_control(self):
        """FIFO de órdenes para la pantalla (scripts/pantalla.sh). Lectura no bloqueante."""
        ruta = config.FIFO_CONTROL
        try:
            if not os.path.exists(ruta):
                os.mkfifo(ruta, 0o660)
            self.fd_ctl = os.open(ruta, os.O_RDONLY | os.O_NONBLOCK)
            self.fd_ctl_reserva = os.open(ruta, os.O_WRONLY | os.O_NONBLOCK)   # evita fin de archivo
            self.log.info(f'Control de pantalla en {ruta}')
        except OSError as e:
            self.fd_ctl = None
            self.log.warning(f'Sin control de pantalla ({ruta}): {e}')

    def leer_control(self, pantalla):
        if getattr(self, 'fd_ctl', None) is None:
            return
        try:
            datos = os.read(self.fd_ctl, 256)
        except OSError as e:
            if e.errno != errno.EAGAIN:
                self.log.warning(f'Control de pantalla: {e}')
            return
        for orden in datos.decode(errors='ignore').split():
            self.log.info(f'Orden de pantalla "{orden}": {pantalla.comando(orden.lower())}')

    def bucle_principal(self, pantalla):
        fijar_nice(5, self.log)                 # la pantalla tiene menor prioridad (clase 8)
        self.abrir_control()
        t_metricas = time.monotonic()
        periodo_ms = 1000 / config.FPS_PANTALLA
        while not self.parar.is_set():
            t0 = time.monotonic()
            self.leer_control(pantalla)
            if pantalla.activa:
                p = self.perfil
                pie = (f'Entrada {p.hora_entrada} | tolerancia {p.puntual_min} min | umbral {p.umbral:.2f} | '
                       f'datos cifrados AES-256-GCM')
                with self.candado_vista:
                    estado = {'personas': self.n_personas, 'fps': self.fps,
                              'camara_ok': self.camara_ok.is_set(),
                              'led_ok': self.led.conectado_antes is not False,
                              'reloj_ok': self.reloj_ok, 'modo': p.nombre,
                              'aviso': self.aviso, 'mensaje': self.mensaje,
                              'historial': list(self.historial), 'pie': pie}
                    vista = self.vista if self.camara_ok.is_set() else None
                pantalla.mostrar(componer(pantalla.tam, vista, estado))
                espera = periodo_ms - (time.monotonic() - t0) * 1000
                if pantalla.esperar(max(1, espera)) == 27:
                    self.log.info('ESC presionado: se detiene el servicio')
                    self.parar.set()
            else:
                pantalla.esperar(500)
            if time.monotonic() - t_metricas >= config.INTERVALO_METRICAS_S:
                t_metricas = time.monotonic()
                self.metricas()


def main():
    log = configurar_log('faceaccess')
    log.info(f'=== FaceAccessPi: inicio a {segundos_desde_encendido():.1f} s del encendido ===')
    log.info(f'Cámara {config.CAMARA} | {config.FPS_PROCESO:.0f} img/s | umbral coseno {config.UMBRAL_COSENO} | '
             f'pantalla {config.PANTALLA} | reloj NTP {"sincronizado" if reloj_sincronizado() else "SIN sincronizar"}')
    try:
        cripto = Cripto(config.RUTA_CLAVE, log)
        Base(config.RUTA_DB, cripto, log).cerrar()       # crea la base o la migra al esquema cifrado
        bio = Biometria(log)
    except (ErrorCripto, FileNotFoundError, sqlite3.Error, cv2.error) as e:
        log.error(f'No se pudo iniciar: {e}')
        LedFifo(config.FIFO_LED, log).enviar('FALLO')
        return 1

    # Calentamiento: el grupo de hilos de OpenCV se crea aquí, con nice 0, antes de que el
    # hilo principal baje su prioridad (corrige la diferencia encontrada en el Avance 3).
    t0 = time.perf_counter()
    bio.detectar(np.zeros((config.ALTO, config.ANCHO, 3), np.uint8))
    bio.reconocedor.feature(np.zeros((112, 112, 3), np.uint8))
    log.info(f'Calentamiento de YuNet + SFace en {(time.perf_counter() - t0) * 1000:.0f} ms')

    pantalla = Pantalla(log)
    sistema = Sistema(log, bio, cripto)

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
        sistema.bucle_principal(pantalla)
    finally:
        sistema.parar.set()
        for h in hilos:
            h.join(timeout=3)
        sistema.led.enviar('APAGADO')
        sistema.led.cerrar()
        pantalla.cerrar()
        log.info('=== Resumen final ===')
        sistema.metricas(final=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
