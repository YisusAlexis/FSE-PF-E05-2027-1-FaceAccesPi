"""
db.py - Base de datos SQLite con los datos personales cifrados (esquema 2).

Tablas:
  personas  (id, indice, nombre_cif, vector_cif, muestras, creado)
            indice     = HMAC del nombre (único; permite buscar sin guardar el nombre)
            nombre_cif = nombre cifrado con AES-256-GCM
            vector_cif = vector SFace (128 float32) cifrado con AES-256-GCM
  eventos   (id, ts, persona_id, resultado, similitud, latencia_ms)
            sin nombres: solo el id numérico de la persona
  auditoria (id, ts, accion, detalle_cif)
  registros (id, persona_id, fecha, hora, ts, tipo, estado, revision, motivo, similitud, origen)
            asistencia: tipo IN/OUT; estado PUNTUAL, RETARDO, RETARDO_MAYOR, REGRESO, SALIDA,
            SALIDA_NO_REGISTRADA o BLOQUEADO; revision NULL, PENDIENTE, APROBADO o RECHAZADO.
            Sin nombres: solo el id numérico de la persona.
  meta      (clave, valor)  -> esquema = 3

Configuración: WAL + synchronous=FULL (un evento confirmado ya está en la microSD;
un corte de energía no corrompe la base) y secure_delete=ON (lo borrado se sobrescribe).
SQLite sincroniza al servicio con registrar.py, que escriben desde procesos distintos.
"""
import os
import sqlite3
import time

import numpy as np

ESQUEMA_VERSION = 3

ESQUEMA = """
CREATE TABLE IF NOT EXISTS meta (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS personas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    indice      TEXT NOT NULL UNIQUE,
    nombre_cif  BLOB NOT NULL,
    vector_cif  BLOB NOT NULL,
    muestras    INTEGER NOT NULL,
    creado      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    persona_id  INTEGER,
    resultado   TEXT NOT NULL,
    similitud   REAL,
    latencia_ms REAL
);
CREATE TABLE IF NOT EXISTS auditoria (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    accion      TEXT NOT NULL,
    detalle_cif BLOB
);
CREATE INDEX IF NOT EXISTS eventos_persona_ts ON eventos (persona_id, ts);
CREATE TABLE IF NOT EXISTS registros (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id  INTEGER NOT NULL,
    fecha       TEXT NOT NULL,          -- YYYY-MM-DD (hora local)
    hora        TEXT NOT NULL,          -- HH:MM:SS
    ts          REAL NOT NULL,          -- segundos desde 1970 (para los intervalos)
    tipo        TEXT NOT NULL,          -- IN / OUT
    estado      TEXT NOT NULL,
    revision    TEXT,                   -- NULL, PENDIENTE, APROBADO, RECHAZADO
    motivo      TEXT,
    similitud   REAL,
    origen      TEXT NOT NULL DEFAULT 'AUTO'   -- AUTO, SISTEMA, DEMO
);
CREATE INDEX IF NOT EXISTS registros_persona_fecha ON registros (persona_id, fecha, ts);
"""

# Un registro cuenta para la alternancia IN/OUT si no fue rechazado y no está bloqueado
# esperando revisión (un retardo mayor sí cuenta: la persona entró, falta autorizarlo).
VALIDO = ("(revision IS NULL OR revision = 'APROBADO' "
          "OR (revision = 'PENDIENTE' AND estado = 'RETARDO_MAYOR'))")
COLUMNAS_REG = 'id, persona_id, fecha, hora, ts, tipo, estado, revision, motivo, similitud, origen'


def ahora():
    return time.strftime('%Y-%m-%d %H:%M:%S')


class Base:
    """Una instancia por hilo o proceso (sqlite3 no comparte conexiones entre hilos)."""

    def __init__(self, ruta, cripto, log=None):
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        self.cripto = cripto
        self.log = log
        self.con = sqlite3.connect(ruta, timeout=10)
        self.con.execute('PRAGMA journal_mode=WAL')
        self.con.execute('PRAGMA synchronous=FULL')
        self.con.execute('PRAGMA secure_delete=ON')
        if self._es_esquema_1():
            self._migrar_desde_1()
        self.con.executescript(ESQUEMA)
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO meta VALUES ('esquema', ?)", (str(ESQUEMA_VERSION),))

    # ------------------------------------------------------------ utilidades
    def cerrar(self):
        self.con.close()

    def version(self):
        """Cambia cuando OTRA conexión hace commit; sirve para recargar personas."""
        return self.con.execute('PRAGMA data_version').fetchone()[0]

    def _ctx_nombre(self, indice):
        return f'personas.nombre:{indice}'

    def _ctx_vector(self, indice):
        return f'personas.vector:{indice}'

    # --------------------------------------------------------------- personas
    def personas(self):
        """Devuelve (ids, nombres, matriz N x 128) ya descifrados, en memoria."""
        filas = self.con.execute(
            'SELECT id, indice, nombre_cif, vector_cif FROM personas ORDER BY id').fetchall()
        ids, nombres, vectores = [], [], []
        for id_, indice, nombre_cif, vector_cif in filas:
            ids.append(id_)
            nombres.append(self.cripto.descifrar_texto(nombre_cif, self._ctx_nombre(indice)))
            vectores.append(np.frombuffer(self.cripto.descifrar(vector_cif, self._ctx_vector(indice)),
                                          dtype=np.float32))
        matriz = np.vstack(vectores) if vectores else np.empty((0, 128), np.float32)
        return ids, nombres, matriz

    def existe(self, nombre):
        indice = self.cripto.indice(nombre)
        return self.con.execute('SELECT 1 FROM personas WHERE indice = ?', (indice,)).fetchone() is not None

    def guardar_persona(self, nombre, vector, muestras, reemplazar=False):
        indice = self.cripto.indice(nombre)
        nombre_cif = self.cripto.cifrar_texto(nombre, self._ctx_nombre(indice))
        vector_cif = self.cripto.cifrar(vector.astype(np.float32).tobytes(), self._ctx_vector(indice))
        with self.con:
            if reemplazar and self.con.execute('SELECT 1 FROM personas WHERE indice = ?',
                                               (indice,)).fetchone():
                # se conserva el id para no romper el historial de eventos
                self.con.execute('UPDATE personas SET nombre_cif = ?, vector_cif = ?, muestras = ?, '
                                 'creado = ? WHERE indice = ?',
                                 (nombre_cif, vector_cif, muestras, ahora(), indice))
                accion = 'REEMPLAZO'
            else:
                self.con.execute('INSERT INTO personas (indice, nombre_cif, vector_cif, muestras, creado) '
                                 'VALUES (?, ?, ?, ?, ?)',
                                 (indice, nombre_cif, vector_cif, muestras, ahora()))
                accion = 'ALTA'
            self._auditar(accion, f'{nombre} ({muestras} muestras)')

    def borrar_persona(self, nombre):
        indice = self.cripto.indice(nombre)
        with self.con:
            n = self.con.execute('DELETE FROM personas WHERE indice = ?', (indice,)).rowcount
            if n:
                self._auditar('BAJA', nombre)
        return n

    def listar(self):
        filas = self.con.execute(
            'SELECT id, indice, nombre_cif, muestras, creado FROM personas ORDER BY id').fetchall()
        return [(id_, self.cripto.descifrar_texto(nc, self._ctx_nombre(ind)), m, c)
                for id_, ind, nc, m, c in filas]

    def nombres_por_id(self):
        return {id_: nombre for id_, nombre, _, _ in self.listar()}

    # ---------------------------------------------------------------- eventos
    def registrar_evento(self, persona_id, resultado, similitud):
        """RF-06: el LED se enciende solo después de que esto regresa sin error."""
        with self.con:
            cur = self.con.execute(
                'INSERT INTO eventos (ts, persona_id, resultado, similitud) VALUES (?, ?, ?, ?)',
                (ahora(), persona_id, resultado, similitud))
        return cur.lastrowid

    def actualizar_latencia(self, id_evento, latencia_ms):
        with self.con:
            self.con.execute('UPDATE eventos SET latencia_ms = ? WHERE id = ?', (latencia_ms, id_evento))

    def eventos(self, n):
        nombres = self.nombres_por_id()
        filas = self.con.execute('SELECT id, ts, persona_id, resultado, similitud, latencia_ms '
                                 'FROM eventos ORDER BY id DESC LIMIT ?', (n,)).fetchall()
        return [(i, ts, nombres.get(p, '-' if p is None else f'(baja #{p})'), r, s, l)
                for i, ts, p, r, s, l in reversed(filas)]

    def latencias(self):
        return [r[0] for r in self.con.execute(
            'SELECT latencia_ms FROM eventos WHERE latencia_ms IS NOT NULL')]

    # ------------------------------------------------------------- asistencia
    def _filas_reg(self, sql, args=()):
        nombres = [c.strip() for c in COLUMNAS_REG.split(',')]
        return [dict(zip(nombres, f)) for f in self.con.execute(sql, args).fetchall()]

    def registros_dia(self, persona_id, fecha, solo_validos=True):
        filtro = f'AND {VALIDO}' if solo_validos else ''
        return self._filas_reg(f'SELECT {COLUMNAS_REG} FROM registros WHERE persona_id = ? AND fecha = ? '
                               f'{filtro} ORDER BY ts', (persona_id, fecha))

    def ultimo_registro(self, persona_id, solo_validos=True):
        filtro = f'AND {VALIDO}' if solo_validos else ''
        filas = self._filas_reg(f'SELECT {COLUMNAS_REG} FROM registros WHERE persona_id = ? {filtro} '
                                f'ORDER BY ts DESC LIMIT 1', (persona_id,))
        return filas[0] if filas else None

    def insertar_registro(self, persona_id, fecha, hora, ts, tipo, estado, revision=None,
                          motivo=None, similitud=None, origen='AUTO'):
        with self.con:
            cur = self.con.execute(
                'INSERT INTO registros (persona_id, fecha, hora, ts, tipo, estado, revision, motivo, '
                'similitud, origen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (persona_id, fecha, hora, ts, tipo, estado, revision, motivo, similitud, origen))
        return cur.lastrowid

    def registros_rango(self, desde, hasta, persona_id=None):
        sql = f'SELECT {COLUMNAS_REG} FROM registros WHERE fecha BETWEEN ? AND ?'
        args = [desde, hasta]
        if persona_id is not None:
            sql += ' AND persona_id = ?'
            args.append(persona_id)
        return self._filas_reg(sql + ' ORDER BY fecha, ts', args)

    def pendientes(self):
        return self._filas_reg(f"SELECT {COLUMNAS_REG} FROM registros WHERE revision = 'PENDIENTE' "
                               f"ORDER BY ts")

    def resolver(self, id_registro, aprobado, nota=''):
        decision = 'APROBADO' if aprobado else 'RECHAZADO'
        with self.con:
            n = self.con.execute("UPDATE registros SET revision = ? WHERE id = ? AND revision = 'PENDIENTE'",
                                 (decision, id_registro)).rowcount
            if n:
                self._auditar(decision, f'registro #{id_registro} {nota}'.strip())
        return n

    def persona_id(self, nombre):
        fila = self.con.execute('SELECT id FROM personas WHERE indice = ?',
                                (self.cripto.indice(nombre),)).fetchone()
        return fila[0] if fila else None

    # -------------------------------------------------------------- auditoría
    def _auditar(self, accion, detalle):
        ts = ahora()
        self.con.execute('INSERT INTO auditoria (ts, accion, detalle_cif) VALUES (?, ?, ?)',
                         (ts, accion, self.cripto.cifrar_texto(detalle, f'auditoria:{ts}:{accion}')))

    def auditar(self, accion, detalle):
        with self.con:
            self._auditar(accion, detalle)

    def auditoria(self, n):
        filas = self.con.execute('SELECT id, ts, accion, detalle_cif FROM auditoria '
                                 'ORDER BY id DESC LIMIT ?', (n,)).fetchall()
        return [(i, ts, a, self.cripto.descifrar_texto(d, f'auditoria:{ts}:{a}') if d else '')
                for i, ts, a, d in reversed(filas)]

    # -------------------------------------------------------------- migración
    def _es_esquema_1(self):
        cols = [r[1] for r in self.con.execute('PRAGMA table_info(personas)')]
        return 'vector' in cols and 'indice' not in cols

    def _migrar_desde_1(self):
        """Avance 3 guardaba nombres y vectores en claro: se cifran y se borra lo anterior."""
        c = self.con
        personas = c.execute('SELECT nombre, vector, muestras, creado FROM personas ORDER BY id').fetchall()
        eventos = c.execute('SELECT ts, nombre, resultado, similitud, latencia_ms FROM eventos ORDER BY id').fetchall()
        auditoria = c.execute('SELECT ts, accion, detalle FROM auditoria ORDER BY id').fetchall()
        with c:
            c.execute('DROP TABLE personas')
            c.execute('DROP TABLE eventos')
            c.execute('DROP TABLE auditoria')
            c.executescript(ESQUEMA)
            ids = {}
            for nombre, vector, muestras, creado in personas:
                indice = self.cripto.indice(nombre)
                cur = c.execute('INSERT INTO personas (indice, nombre_cif, vector_cif, muestras, creado) '
                                'VALUES (?, ?, ?, ?, ?)',
                                (indice, self.cripto.cifrar_texto(nombre, self._ctx_nombre(indice)),
                                 self.cripto.cifrar(vector, self._ctx_vector(indice)), muestras, creado))
                ids[nombre] = cur.lastrowid
            for ts, nombre, resultado, similitud, latencia in eventos:
                c.execute('INSERT INTO eventos (ts, persona_id, resultado, similitud, latencia_ms) '
                          'VALUES (?, ?, ?, ?, ?)', (ts, ids.get(nombre), resultado, similitud, latencia))
            for ts, accion, detalle in auditoria:
                c.execute('INSERT INTO auditoria (ts, accion, detalle_cif) VALUES (?, ?, ?)',
                          (ts, accion, self.cripto.cifrar_texto(detalle or '', f'auditoria:{ts}:{accion}')))
            self._auditar('MIGRACION', f'esquema 1 -> 2: {len(personas)} personas, {len(eventos)} eventos cifrados')
        # borrar físicamente las páginas con datos en claro
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        c.execute('VACUUM')
        if self.log:
            self.log.warning(f'Base migrada al esquema 2 (cifrado): {len(personas)} personas y '
                             f'{len(eventos)} eventos; los datos en claro se borraron con VACUUM')
