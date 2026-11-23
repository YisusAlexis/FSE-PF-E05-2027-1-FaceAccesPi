"""
test_asistencia.py - Prueba de las reglas de asistencia con un reloj simulado.
No usa la cámara, ni la base real, ni la clave real: crea una base y una clave temporales.

  python3 vision/test_asistencia.py
"""
import sys, os, logging, tempfile, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
S = tempfile.mkdtemp(prefix='faceaccess_test_')
RUTA = os.path.join(S, 'prueba.db')
os.environ['FACEACCESS_DB'] = RUTA
import numpy as np, cripto, db, asistencia
logging.basicConfig(level=logging.WARNING, format='%(message)s'); log = logging.getLogger()
cripto.generar_clave(os.path.join(S, 'clave.bin'))
b = db.Base(RUTA, cripto.Cripto(os.path.join(S, 'clave.bin')), log)
b.guardar_persona('Alexis', np.ones(128, np.float32) / np.sqrt(128), 15)
pid = b.persona_id('Alexis')
m = asistencia.Motor(b, asistencia.PRODUCTIVO, log)
D = lambda s: dt.datetime.strptime('2026-11-18 ' + s, '%Y-%m-%d %H:%M:%S')
casos = [
 ('07:58:00', 'ENTRADA', 'PUNTUAL',  'primer registro del día = IN, antes de las 8'),
 ('07:59:30', 'IGNORADO', 'bloqueo', 'dentro de 3 min: ignorado'),
 ('12:00:00', 'SALIDA', 'SALIDA',    'alternancia: OUT'),
 ('12:05:00', 'IGNORADO', 'regreso', 'regreso a los 5 min (<15): doble marcaje'),
 ('12:20:00', 'ENTRADA', 'REGRESO',  'regreso tras 20 min: IN'),
 ('17:30:00', 'SALIDA', 'SALIDA',    'segunda salida'),
 ('17:40:00', 'IGNORADO', 'regreso', 'a 10 min de la salida: doble marcaje'),
 ('17:50:00', 'REVISION', 'BLOQUEADO','quinto intento (20 min después): revisión'),
 ('17:51:00', 'IGNORADO', 'bloqueo', 'tras bloqueado, ventana de 3 min'),
 ('17:55:00', 'REVISION', 'ya en revisión', 'otro intento excedente: aviso rojo sin nuevo registro'),
 ('18:10:00', 'REVISION', 'ya en revisión', 'y otro más: sigue sin duplicar pendientes'),
]
ok = True
for h, acc, det, desc in casos:
    r = m.procesar(pid, 'Alexis', 0.8, D(h))
    bien = r.accion == acc and det in r.detalle
    ok &= bien
    print(f"{'OK ' if bien else 'MAL'} {h} -> {r.accion:<9} {r.detalle:<45} | {desc}")
n_bloq = sum(1 for r in b.registros_dia(pid, '2026-11-18', solo_validos=False) if r['estado'] == 'BLOQUEADO')
bien = n_bloq == 1; ok &= bien
print('OK ' if bien else 'MAL', f'registros BLOQUEADO guardados: {n_bloq} (se espera 1)')
# puntualidad, otro día
b.guardar_persona('Caro', -np.ones(128, np.float32) / np.sqrt(128), 15); c = b.persona_id('Caro')
for h, est in [('08:10:00','PUNTUAL'), ]:
    r = m.procesar(c, 'Caro', 0.8, D(h)); print('OK ' if r.detalle==est else 'MAL', h, r.detalle); ok &= r.detalle==est
for dia, h, est in [('2026-11-19','08:11:00','RETARDO'), ('2026-11-20','08:15:59','RETARDO'), ('2026-11-21','08:16:00','RETARDO_MAYOR')]:
    # Caro no marcó salida el 18 -> el 19 se cierra huérfano
    r = m.procesar(c, 'Caro', 0.8, dt.datetime.strptime(dia+' '+h, '%Y-%m-%d %H:%M:%S'))
    bien = r.detalle == est and r.accion == 'ENTRADA'; ok &= bien
    print('OK ' if bien else 'MAL', dia, h, r.accion, r.detalle, r.lineas)
for f in b.registros_rango('2026-11-18', '2026-11-21', c):
    print('   ', f['fecha'], f['hora'], f['tipo'], f['estado'], f['revision'] or '', f['origen'])
print('pendientes:', [(p['id'], p['estado']) for p in b.pendientes()])
print('TODAS OK' if ok else 'HAY FALLAS')
import shutil; shutil.rmtree(S, ignore_errors=True)
sys.exit(0 if ok else 1)
