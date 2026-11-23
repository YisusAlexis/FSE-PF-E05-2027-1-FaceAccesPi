"""
asistencia.py - Motor de registro de entradas y salidas (reglas del control de asistencia).

El tipo de registro NUNCA se decide por la hora del reloj, sino por el historial del día:

1. Transición de estado (alternancia)
   - El primer registro válido del día es Entrada (IN).
   - Después de IN viene OUT y después de OUT viene IN.
   - Máximo MAX_REGISTROS registros válidos por día (2 entradas y 2 salidas). El siguiente
     intento se bloquea y queda PENDIENTE de revisión administrativa.
2. Doble marcaje (debounce)
   - Tras cualquier intento registrado, la persona queda bloqueada BLOQUEO_S segundos:
     los reconocimientos en esa ventana se ignoran por completo (sin aviso ni registro).
3. Calidad biométrica: la decide faceaccess.py con UMBRAL_IDENTIDAD (0.70) antes de llamar aquí.
4. Casos particulares
   - Turno huérfano: si el último registro válido de un día anterior es IN, se cierra con un
     OUT a la hora de cierre (18:00) marcado SALIDA_NO_REGISTRADA, y el escaneo actual es IN.
   - Salida corta: si la última fue OUT hace menos de REGRESO_S (15 min), el intento se toma
     como doble marcaje accidental y se descarta.
5. Puntualidad (solo la primera entrada del día)
   - Hasta entrada + 10 min: PUNTUAL; hasta + 15 min: RETARDO;
   - después: RETARDO_MAYOR, que queda PENDIENTE de autorización del supervisor.

Los parámetros vienen de un perfil: PRODUCTIVO o DEMO (tiempos cortos y hora de entrada
configurable para la presentación). El modo se guarda en datos/asistencia.conf y se cambia
con scripts/asistencia.sh; el servicio lo vuelve a leer sin reiniciarse.
"""
import datetime as dt
import os
from dataclasses import dataclass, replace

import config

RUTA_CONF = os.path.join(os.path.dirname(config.RUTA_DB), 'asistencia.conf')


@dataclass(frozen=True)
class Perfil:
    nombre: str
    hora_entrada: str = '08:00'
    puntual_min: int = 10          # hasta 08:10 -> puntual
    retardo_min: int = 15          # hasta 08:15 -> retardo; después retardo mayor
    bloqueo_s: int = 180           # 3 min
    regreso_s: int = 900           # 15 min
    max_registros: int = 4         # 2 entradas y 2 salidas
    hora_cierre: str = '18:00'     # cierre de turnos huérfanos
    umbral: float = 0.70           # similitud SFace para identificar

    def resumen(self):
        return (f'{self.nombre}: entrada {self.hora_entrada} (puntual +{self.puntual_min} min, '
                f'retardo +{self.retardo_min} min), bloqueo {self.bloqueo_s} s, regreso mínimo '
                f'{self.regreso_s} s, máx {self.max_registros} registros, cierre {self.hora_cierre}, '
                f'umbral {self.umbral:.2f}')


PRODUCTIVO = Perfil('PRODUCTIVO')
DEMO = Perfil('DEMO', bloqueo_s=30, regreso_s=60)


def leer_conf(ruta=RUTA_CONF):
    conf = {}
    try:
        with open(ruta, encoding='utf-8') as f:
            for linea in f:
                linea = linea.strip()
                if linea and not linea.startswith('#') and '=' in linea:
                    k, v = linea.split('=', 1)
                    conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


def guardar_conf(conf, ruta=RUTA_CONF):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = ruta + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('# Modo del control de asistencia (lo escribe scripts/asistencia.sh)\n')
        for k, v in conf.items():
            f.write(f'{k}={v}\n')
    os.replace(tmp, ruta)       # escritura atómica


def cargar_perfil(ruta=RUTA_CONF):
    conf = leer_conf(ruta)
    perfil = DEMO if conf.get('modo', 'productivo').lower() == 'demo' else PRODUCTIVO
    if perfil is DEMO and conf.get('hora_entrada'):
        perfil = replace(perfil, hora_entrada=conf['hora_entrada'])
    return perfil


def _a_minutos(hhmm):
    h, m = hhmm.split(':')[:2]
    return int(h) * 60 + int(m)


@dataclass
class Resultado:
    accion: str            # ENTRADA, SALIDA, REVISION, IGNORADO
    led: str               # estado para neopixel_spi ('' = no cambiar)
    titulo: str = ''
    lineas: tuple = ()
    tono: str = ''         # verde, cian, naranja, rojo (color del aviso en pantalla)
    registro_id: int = None
    detalle: str = ''


class Motor:
    def __init__(self, base, perfil, log):
        self.base = base
        self.perfil = perfil
        self.log = log

    def cerrar_huerfano(self, persona_id, hoy):
        """Regla 4: cierra un IN de un día anterior que nunca tuvo salida."""
        ultimo = self.base.ultimo_registro(persona_id)
        if not ultimo or ultimo['tipo'] != 'IN' or ultimo['fecha'] >= hoy:
            return None
        hora = max(self.perfil.hora_cierre + ':00', ultimo['hora'])
        fecha = dt.datetime.strptime(f"{ultimo['fecha']} {hora}", '%Y-%m-%d %H:%M:%S')
        rid = self.base.insertar_registro(persona_id, ultimo['fecha'], hora, fecha.timestamp(), 'OUT',
                                          'SALIDA_NO_REGISTRADA', motivo='Turno huérfano cerrado por el sistema',
                                          origen='SISTEMA')
        self.log.warning(f'Turno huérfano: persona #{persona_id} sin salida el {ultimo["fecha"]}; '
                         f'cerrado a las {hora} (registro #{rid})')
        return ultimo['fecha']

    def procesar(self, persona_id, nombre, similitud, ahora=None):
        p = self.perfil
        ahora = ahora or dt.datetime.now()
        ts = ahora.timestamp()
        hoy, hora = ahora.strftime('%Y-%m-%d'), ahora.strftime('%H:%M:%S')

        # Regla 2: ventana de bloqueo tras cualquier intento registrado (válido o bloqueado)
        ultimo_intento = self.base.ultimo_registro(persona_id, solo_validos=False)
        if ultimo_intento and ultimo_intento['origen'] != 'SISTEMA' and ts - ultimo_intento['ts'] < p.bloqueo_s:
            return Resultado('IGNORADO', '', detalle=f'bloqueo activo ({ts - ultimo_intento["ts"]:.0f} s '
                                                      f'de {p.bloqueo_s} s)')

        huerfano = self.cerrar_huerfano(persona_id, hoy)          # Regla 4
        del_dia = self.base.registros_dia(persona_id, hoy)
        ultimo = del_dia[-1] if del_dia else None

        # Regla 4: salida corta -> doble marcaje accidental
        if ultimo and ultimo['tipo'] == 'OUT' and ts - ultimo['ts'] < p.regreso_s:
            return Resultado('IGNORADO', '', detalle=f'regreso antes de {p.regreso_s} s '
                                                      f'({ts - ultimo["ts"]:.0f} s desde la salida)')

        tipo = 'IN' if ultimo is None or ultimo['tipo'] == 'OUT' else 'OUT'   # Regla 1
        nota_huerfano = (f'Salida del {huerfano} no registrada',) if huerfano else ()

        # Regla 1: límite operativo diario -> revisión administrativa
        if len(del_dia) >= p.max_registros:
            # Solo el primer intento excedente va a revisión; los siguientes del mismo día se avisan
            # (LED rojo) pero no se guardan, para no llenar la lista de pendientes con duplicados.
            ya = [r for r in self.base.registros_dia(persona_id, hoy, solo_validos=False)
                  if r['estado'] == 'BLOQUEADO' and r['revision'] == 'PENDIENTE']
            if ya:
                return Resultado('REVISION', 'REVISION', 'LIMITE DIARIO ALCANZADO',
                                 (nombre, f'Hora: {hora}', f'Ya en revision (registro #{ya[0]["id"]})'),
                                 'rojo', None, f'BLOQUEADO: ya en revisión (#{ya[0]["id"]}), sin nuevo registro')
            rid = self.base.insertar_registro(persona_id, hoy, hora, ts, tipo, 'BLOQUEADO', revision='PENDIENTE',
                                              motivo=f'Excede {p.max_registros} registros diarios',
                                              similitud=similitud)
            return Resultado('REVISION', 'REVISION', 'LIMITE DIARIO ALCANZADO',
                             (nombre, f'Hora: {hora}', 'Enviado a revision'), 'rojo', rid,
                             f'BLOQUEADO: {len(del_dia)} registros válidos hoy')

        revision, motivo = None, None
        if tipo == 'OUT':
            estado = 'SALIDA'
            res = Resultado('SALIDA', 'SALIDA', 'SALIDA REGISTRADA',
                            (f'Hasta luego, {nombre}', f'Hora: {hora}') + nota_huerfano, 'cian')
        elif any(r['tipo'] == 'IN' for r in del_dia):
            estado = 'REGRESO'
            res = Resultado('ENTRADA', 'ENTRADA', 'REGRESO REGISTRADO',
                            (f'Bienvenido de nuevo, {nombre}', f'Hora: {hora}') + nota_huerfano, 'verde')
        else:
            # Regla 5: puntualidad de la primera entrada del día
            minutos = ahora.hour * 60 + ahora.minute - _a_minutos(p.hora_entrada)
            if minutos <= p.puntual_min:
                estado, texto, tono = 'PUNTUAL', 'Puntual', 'verde'
            elif minutos <= p.retardo_min:
                estado, texto, tono = 'RETARDO', f'Retardo ({minutos} min)', 'naranja'
            else:
                estado, texto, tono = 'RETARDO_MAYOR', f'Retardo mayor ({minutos} min)', 'naranja'
                revision, motivo = 'PENDIENTE', 'Requiere autorización del supervisor'
            lineas = (f'Bienvenido, {nombre}', f'Hora: {hora}  {texto}')
            if revision:
                lineas += ('Requiere autorizacion',)
            res = Resultado('ENTRADA', 'ENTRADA', 'ENTRADA REGISTRADA', lineas + nota_huerfano, tono)

        res.registro_id = self.base.insertar_registro(persona_id, hoy, hora, ts, tipo, estado, revision, motivo,
                                                      similitud)
        res.detalle = estado
        return res
