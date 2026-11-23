"""
admin_asistencia.py - Consultas y administración del control de asistencia (por SSH).
Se usa a través de scripts/asistencia.sh:

  modo                          muestra el modo actual (PRODUCTIVO o DEMO) y sus parámetros
  modo productivo               reglas reales: entrada 08:00, bloqueo 3 min, regreso 15 min
  modo demo [HH:MM]             tiempos cortos (bloqueo 30 s, regreso 1 min) y hora de entrada
                                configurable (por defecto, la hora actual) para la presentación
  hoy [AAAA-MM-DD]              asistencia del día por persona
  persona NOMBRE [DIAS]         historial de una persona (por defecto 7 días) con horas trabajadas
  pendientes                    registros en revisión (límite diario, retardo mayor)
  aprobar ID [NOTA]             aprueba un registro pendiente
  rechazar ID [NOTA]            rechaza un registro pendiente
  exportar [DESDE] [HASTA]      CSV en logs/ (por defecto, los últimos 30 días)
  limpiar-bloqueados            rechaza los BLOQUEADO duplicados (deja el primero de cada persona
                                y día), creados por versiones anteriores a la del 17/11/2026
  demo-huerfano NOMBRE          (solo modo demo) crea una entrada de AYER sin salida para
                                mostrar la regla del turno huérfano en el siguiente escaneo
"""
import csv
import datetime as dt
import os
import sys

import config
import asistencia
from cripto import Cripto, ErrorCripto
from db import Base, VALIDO


def abrir_base():
    try:
        return Base(config.RUTA_DB, Cripto(config.RUTA_CLAVE))
    except ErrorCripto as e:
        sys.exit(f'Error: {e}')


def horas_trabajadas(registros):
    """Suma los pares IN -> OUT válidos del día (en horas)."""
    total, entrada = 0.0, None
    for r in registros:
        if r['revision'] == 'RECHAZADO' or (r['revision'] == 'PENDIENTE' and r['estado'] == 'BLOQUEADO'):
            continue
        if r['tipo'] == 'IN':
            entrada = r['ts']
        elif entrada is not None:
            total += (r['ts'] - entrada) / 3600
            entrada = None
    return total


def fmt(r):
    rev = f' [{r["revision"]}]' if r['revision'] else ''
    origen = '' if r['origen'] == 'AUTO' else f' ({r["origen"].lower()})'
    return f'#{r["id"]} {r["hora"][:5]} {r["tipo"]:<3} {r["estado"]}{rev}{origen}'


def cmd_modo(args):
    if not args:
        p = asistencia.cargar_perfil()
        print(f'Modo actual: {p.resumen()}')
        return 0
    conf = asistencia.leer_conf()
    if args[0] == 'productivo':
        conf = {'modo': 'productivo'}
    elif args[0] == 'demo':
        hora = args[1] if len(args) > 1 else dt.datetime.now().strftime('%H:%M')
        try:
            dt.datetime.strptime(hora, '%H:%M')
        except ValueError:
            sys.exit('Hora inválida; usa HH:MM, por ejemplo 16:30')
        conf = {'modo': 'demo', 'hora_entrada': hora}
    else:
        sys.exit('Uso: modo [productivo | demo [HH:MM]]')
    asistencia.guardar_conf(conf)
    base = abrir_base()
    base.auditar('MODO', f'asistencia en modo {conf}')
    print(f'Modo cambiado: {asistencia.cargar_perfil().resumen()}')
    print('El servicio lo aplica en unos 3 s, sin reiniciarse.')
    return 0


def cmd_hoy(args):
    fecha = args[0] if args else dt.date.today().isoformat()
    base = abrir_base()
    nombres = base.nombres_por_id()
    filas = base.registros_rango(fecha, fecha)
    print(f'Asistencia del {fecha} ({len(filas)} registros)')
    por_persona = {}
    for r in filas:
        por_persona.setdefault(r['persona_id'], []).append(r)
    for pid, nombre in sorted(nombres.items(), key=lambda x: x[1]):
        regs = por_persona.get(pid, [])
        if not regs:
            print(f'  {nombre:<15} sin registros')
            continue
        print(f'  {nombre:<15} {horas_trabajadas(regs):4.1f} h | ' + ' | '.join(fmt(r) for r in regs))
    return 0


def cmd_persona(args):
    if not args:
        sys.exit('Uso: persona NOMBRE [DIAS]')
    dias = int(args[1]) if len(args) > 1 else 7
    base = abrir_base()
    pid = base.persona_id(args[0])
    if pid is None:
        sys.exit(f'{args[0]} no está registrado')
    hasta = dt.date.today()
    desde = hasta - dt.timedelta(days=dias - 1)
    filas = base.registros_rango(desde.isoformat(), hasta.isoformat(), pid)
    print(f'{args[0]}: {desde} a {hasta}')
    por_dia = {}
    for r in filas:
        por_dia.setdefault(r['fecha'], []).append(r)
    for fecha in sorted(por_dia):
        regs = por_dia[fecha]
        print(f'  {fecha} {horas_trabajadas(regs):4.1f} h | ' + ' | '.join(fmt(r) for r in regs))
    if not por_dia:
        print('  sin registros')
    return 0


def cmd_pendientes(_args):
    base = abrir_base()
    nombres = base.nombres_por_id()
    pend = base.pendientes()
    print(f'Registros en revisión: {len(pend)}')
    for r in pend:
        print(f'  #{r["id"]} {r["fecha"]} {r["hora"]} {nombres.get(r["persona_id"], "?"):<15} '
              f'{r["tipo"]} {r["estado"]} - {r["motivo"] or ""}')
    if pend:
        print('Usa: scripts/asistencia.sh aprobar ID  |  scripts/asistencia.sh rechazar ID')
    return 0


def cmd_resolver(args, aprobado):
    if not args or not args[0].isdigit():
        sys.exit(f'Uso: {"aprobar" if aprobado else "rechazar"} ID [NOTA]')
    base = abrir_base()
    if base.resolver(int(args[0]), aprobado, ' '.join(args[1:])):
        print(f'Registro #{args[0]} {"APROBADO" if aprobado else "RECHAZADO"}')
        return 0
    sys.exit(f'No hay un registro pendiente con id {args[0]}')


def cmd_exportar(args):
    hasta = dt.date.fromisoformat(args[1]) if len(args) > 1 else dt.date.today()
    desde = dt.date.fromisoformat(args[0]) if args else hasta - dt.timedelta(days=29)
    base = abrir_base()
    nombres = base.nombres_por_id()
    filas = base.registros_rango(desde.isoformat(), hasta.isoformat())
    os.makedirs(config.CARPETA_LOGS, exist_ok=True)
    ruta = os.path.join(config.CARPETA_LOGS, f'asistencia_{desde}_{hasta}.csv')
    fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # contiene nombres en claro
    with os.fdopen(fd, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['id', 'fecha', 'hora', 'nombre', 'tipo', 'estado', 'revision', 'motivo', 'similitud', 'origen'])
        for r in filas:
            w.writerow([r['id'], r['fecha'], r['hora'], nombres.get(r['persona_id'], f'#{r["persona_id"]}'),
                        r['tipo'], r['estado'], r['revision'] or '', r['motivo'] or '',
                        f'{r["similitud"]:.3f}' if r['similitud'] is not None else '', r['origen']])
    base.auditar('EXPORTAR', f'{len(filas)} registros {desde} a {hasta}')
    print(f'{len(filas)} registros exportados a {ruta} (permisos 0600: contiene nombres)')
    return 0


def cmd_limpiar_bloqueados(_args):
    base = abrir_base()
    vistos, n = set(), 0
    for r in base.pendientes():
        if r['estado'] != 'BLOQUEADO':
            continue
        clave = (r['persona_id'], r['fecha'])
        if clave in vistos:
            base.resolver(r['id'], False, f'duplicado del límite diario')
            n += 1
        else:
            vistos.add(clave)
    print(f'{n} registros BLOQUEADO duplicados marcados como RECHAZADO (quedan en el historial y la auditoría).')
    return 0


def cmd_demo_huerfano(args):
    if asistencia.cargar_perfil().nombre != 'DEMO':
        sys.exit('Solo disponible en modo demo (scripts/asistencia.sh modo demo)')
    if not args:
        sys.exit('Uso: demo-huerfano NOMBRE')
    base = abrir_base()
    pid = base.persona_id(args[0])
    if pid is None:
        sys.exit(f'{args[0]} no está registrado')
    ayer = dt.datetime.now() - dt.timedelta(days=1)
    entrada = ayer.replace(hour=8, minute=2, second=0, microsecond=0)
    rid = base.insertar_registro(pid, entrada.strftime('%Y-%m-%d'), entrada.strftime('%H:%M:%S'),
                                 entrada.timestamp(), 'IN', 'PUNTUAL', motivo='Entrada de demostración sin salida',
                                 origen='DEMO')
    base.auditar('DEMO', f'entrada de ayer sin salida para {args[0]} (registro #{rid})')
    print(f'Creada la entrada de ayer {entrada:%Y-%m-%d %H:%M} para {args[0]} (#{rid}), sin salida.')
    print('En su siguiente escaneo, el sistema cerrará ese turno a las 18:00 como "Salida no registrada".')
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__.split('\n', 2)[2])
        return 2
    orden, args = sys.argv[1], sys.argv[2:]
    acciones = {
        'modo': cmd_modo, 'hoy': cmd_hoy, 'persona': cmd_persona, 'pendientes': cmd_pendientes,
        'aprobar': lambda a: cmd_resolver(a, True), 'rechazar': lambda a: cmd_resolver(a, False),
        'exportar': cmd_exportar, 'limpiar-bloqueados': cmd_limpiar_bloqueados, 'demo-huerfano': cmd_demo_huerfano,
    }
    if orden not in acciones:
        print(__doc__.split('\n', 2)[2])
        return 2
    return acciones[orden](args)


if __name__ == '__main__':
    sys.exit(main())
