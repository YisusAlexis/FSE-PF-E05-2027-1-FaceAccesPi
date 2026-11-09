"""
registrar.py - Administración de personas (se usa por SSH; ver scripts/registrar.sh).

  python3 registrar.py --nombre Alexis        registra con la cámara (15 muestras)
  python3 registrar.py --nombre Alexis --reemplazar
  python3 registrar.py --borrar Alexis
  python3 registrar.py --listar
  python3 registrar.py --eventos 20           últimos eventos del historial

No hay entrenamiento: se calcula el vector SFace promedio de la persona y se guarda
en SQLite. El servicio lo detecta en unos segundos sin reiniciarse ni reentrenar.
"""
import argparse
import re
import sys
import time

import cv2
import numpy as np

import config
import db
from comun import Biometria, abrir_camara, configurar_log, dibujar

NOMBRE_VALIDO = re.compile(r'^[A-Za-z0-9_]{1,40}$')


def intentar_ventana(log):
    if not config.VENTANA:
        return False
    try:
        cv2.namedWindow('registro', cv2.WINDOW_NORMAL)
        return True
    except cv2.error as e:
        log.warning(f'Sin ventana (no hay pantalla disponible): {e}')
        return False


def capturar_muestras(bio, cap, nombre, n_muestras, log):
    ventana = intentar_ventana(log)
    vectores = []
    rechazos = {'sin rostro': 0, 'varios rostros': 0, 'muy lejos': 0}
    t_inicio = time.perf_counter()
    t_ultima = 0.0
    fallos = 0
    log.info(f'Mira a la cámara; mueve un poco la cabeza. Se tomarán {n_muestras} muestras')

    while len(vectores) < n_muestras:
        if time.perf_counter() - t_inicio > config.TIEMPO_MAX_REGISTRO_S:
            log.error(f'Tiempo agotado ({config.TIEMPO_MAX_REGISTRO_S} s) con {len(vectores)} muestras')
            break
        ok, imagen = cap.read()
        if not ok:
            fallos += 1
            if fallos > config.FALLOS_PARA_DESCONEXION:
                log.error('La cámara dejó de entregar imágenes')
                break
            continue
        fallos = 0
        caras = bio.detectar(imagen)
        vista = imagen.copy()
        estado = ''
        if len(caras) == 0:
            rechazos['sin rostro'] += 1
            estado = 'Sin rostro'
        elif len(caras) > 1:
            rechazos['varios rostros'] += 1
            estado = 'Solo una persona frente a la camara'
            for c in caras:
                dibujar(vista, c, '', (0, 0, 255))
        elif caras[0][2] < config.TAM_MIN_ROSTRO:
            rechazos['muy lejos'] += 1
            estado = 'Acercate a la camara'
            dibujar(vista, caras[0], 'lejos', (0, 165, 255))
        else:
            dibujar(vista, caras[0], f'{len(vectores)}/{n_muestras}', (0, 255, 0))
            if time.perf_counter() - t_ultima >= config.INTERVALO_MUESTRA_S:
                t0 = time.perf_counter()
                vectores.append(bio.vector(imagen, caras[0]))
                t_ultima = time.perf_counter()
                log.info(f'Muestra {len(vectores)}/{n_muestras} (confianza {caras[0][-1]:.2f}, '
                         f'ancho {int(caras[0][2])} px, SFace {(t_ultima - t0) * 1000:.0f} ms)')
        if ventana:
            cv2.putText(vista, f'Registro: {nombre}  {estado}', (10, 25), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (255, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow('registro', vista)
            if cv2.waitKey(1) == 27:
                log.warning('Registro cancelado con ESC')
                break
    if ventana:
        cv2.destroyAllWindows()
    return vectores, rechazos, time.perf_counter() - t_inicio


def registrar(args, log):
    if not NOMBRE_VALIDO.match(args.nombre):
        log.error('Nombre inválido: usa letras, números o guion bajo (máx. 40)')
        return 1
    con = db.abrir(config.RUTA_DB)
    nombres, matriz = db.cargar_personas(con)
    if args.nombre in nombres and not args.reemplazar:
        log.error(f'{args.nombre} ya está registrado; usa --reemplazar')
        return 1

    bio = Biometria(log)
    cap = abrir_camara(args.fuente, log)
    if cap is None:
        log.error('¿El servicio está usando la cámara? Usa scripts/registrar.sh, que lo detiene')
        return 1
    try:
        vectores, rechazos, segundos = capturar_muestras(bio, cap, args.nombre, args.muestras, log)
    finally:
        cap.release()

    if len(vectores) < args.muestras:
        log.error(f'Registro incompleto: {len(vectores)}/{args.muestras} muestras; no se guardó nada')
        return 1

    m = np.vstack(vectores)
    promedio = m.mean(axis=0)
    promedio /= np.linalg.norm(promedio)
    consistencia = m @ promedio
    log.info(f'Consistencia de las muestras con el promedio: mín {consistencia.min():.3f}, '
             f'prom {consistencia.mean():.3f} (umbral de aceptación {config.UMBRAL_COSENO})')

    if len(matriz):
        sims = matriz @ promedio
        i = int(np.argmax(sims))
        if nombres[i] != args.nombre and sims[i] >= config.UMBRAL_COSENO:
            log.warning(f'Se parece a {nombres[i]} (similitud {sims[i]:.3f}); '
                        f'¿es la misma persona? Usa --forzar para guardarlo de todos modos')
            if not args.forzar:
                return 1

    t0 = time.perf_counter()
    db.guardar_persona(con, args.nombre, promedio, len(vectores), reemplazar=args.reemplazar)
    ms_guardar = (time.perf_counter() - t0) * 1000
    log.info('=== Resumen de registro ===')
    log.info(f'Persona: {args.nombre} | muestras: {len(vectores)} | rechazos: {rechazos}')
    log.info(f'Tiempo de captura: {segundos:.1f} s | guardado en SQLite: {ms_guardar:.0f} ms')
    log.info(f'Personas registradas ahora: {len(nombres) + (0 if args.nombre in nombres else 1)}. '
             f'No se reentrenó nada; el servicio la cargará en ~{config.RECARGA_DB_S:.0f} s')
    return 0


def main():
    p = argparse.ArgumentParser(description='Registro de personas FaceAccessPi')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--nombre', help='registrar a una persona con la cámara')
    g.add_argument('--borrar', metavar='NOMBRE', help='dar de baja a una persona')
    g.add_argument('--listar', action='store_true', help='listar personas registradas')
    g.add_argument('--eventos', type=int, metavar='N', help='mostrar los últimos N eventos')
    p.add_argument('--reemplazar', action='store_true', help='reemplazar si ya existe')
    p.add_argument('--forzar', action='store_true', help='guardar aunque se parezca a otra persona')
    p.add_argument('--fuente', default=config.CAMARA, help='índice de cámara o ruta de video')
    p.add_argument('--muestras', type=int, default=config.MUESTRAS_REGISTRO)
    args = p.parse_args()

    log = configurar_log('registrar')
    if args.nombre:
        return registrar(args, log)

    con = db.abrir(config.RUTA_DB)
    if args.borrar:
        n = db.borrar_persona(con, args.borrar)
        if n:
            log.info(f'{args.borrar} dado de baja; el servicio lo dejará de reconocer en ~{config.RECARGA_DB_S:.0f} s')
            return 0
        log.error(f'{args.borrar} no está registrado')
        return 1
    if args.listar:
        filas = con.execute('SELECT nombre, muestras, creado FROM personas ORDER BY nombre').fetchall()
        log.info(f'Personas registradas: {len(filas)}')
        for nombre, muestras, creado in filas:
            log.info(f'  {nombre:<20} {muestras} muestras  registrado {creado}')
        return 0
    filas = con.execute('SELECT id, ts, resultado, nombre, similitud, latencia_ms FROM eventos '
                        'ORDER BY id DESC LIMIT ?', (args.eventos,)).fetchall()
    for f in reversed(filas):
        sim = f'{f[4]:.3f}' if f[4] is not None else '-'
        lat = f'{f[5]:.0f} ms' if f[5] is not None else '-'
        log.info(f'#{f[0]} {f[1]} {f[2]:<11} {f[3] or "-":<15} similitud {sim} latencia {lat}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
