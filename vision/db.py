"""
db.py - Base de datos SQLite: personas registradas (vectores SFace) e historial de eventos.

WAL + synchronous=FULL: un corte de energía no deja la base corrupta y cada evento
confirmado ya está en disco cuando commit() regresa (RNF-05).
SQLite también sincroniza al proceso de reconocimiento con registrar.py, que puede
escribir mientras el servicio lee (bloqueo de la propia base).
"""
import sqlite3
import time
import os

import numpy as np

ESQUEMA = """
CREATE TABLE IF NOT EXISTS personas (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre    TEXT NOT NULL UNIQUE,
    vector    BLOB NOT NULL,          -- 128 float32 normalizados (512 bytes)
    muestras  INTEGER NOT NULL,
    creado    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    nombre      TEXT,
    resultado   TEXT NOT NULL,        -- REGISTRADO / DESCONOCIDO
    similitud   REAL,
    latencia_ms REAL                  -- imagen que confirma -> mensaje al LED (RNF-01)
);
CREATE TABLE IF NOT EXISTS auditoria (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    accion  TEXT NOT NULL,
    detalle TEXT
);
"""


def ahora():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def abrir(ruta):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    con = sqlite3.connect(ruta, timeout=10)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=FULL')
    con.executescript(ESQUEMA)
    return con


def version(con):
    """Cambia cuando OTRA conexión hace commit; sirve para recargar personas."""
    return con.execute('PRAGMA data_version').fetchone()[0]


def cargar_personas(con):
    filas = con.execute('SELECT nombre, vector FROM personas ORDER BY nombre').fetchall()
    nombres = [f[0] for f in filas]
    if not filas:
        return nombres, np.empty((0, 128), np.float32)
    matriz = np.vstack([np.frombuffer(f[1], dtype=np.float32) for f in filas])
    return nombres, matriz


def guardar_persona(con, nombre, vector, muestras, reemplazar=False):
    with con:   # transacción
        if reemplazar:
            con.execute('DELETE FROM personas WHERE nombre = ?', (nombre,))
        con.execute('INSERT INTO personas (nombre, vector, muestras, creado) VALUES (?, ?, ?, ?)',
                    (nombre, vector.astype(np.float32).tobytes(), muestras, ahora()))
        con.execute('INSERT INTO auditoria (ts, accion, detalle) VALUES (?, ?, ?)',
                    (ahora(), 'ALTA' if not reemplazar else 'REEMPLAZO', f'{nombre} ({muestras} muestras)'))


def borrar_persona(con, nombre):
    with con:
        n = con.execute('DELETE FROM personas WHERE nombre = ?', (nombre,)).rowcount
        if n:
            con.execute('INSERT INTO auditoria (ts, accion, detalle) VALUES (?, ?, ?)',
                        (ahora(), 'BAJA', nombre))
    return n


def registrar_evento(con, nombre, resultado, similitud, latencia_ms=None):
    """RF-06: el LED se enciende solo después de que esto regresa sin error."""
    with con:
        cur = con.execute(
            'INSERT INTO eventos (ts, nombre, resultado, similitud, latencia_ms) VALUES (?, ?, ?, ?, ?)',
            (ahora(), nombre, resultado, similitud, latencia_ms))
    return cur.lastrowid


def actualizar_latencia(con, id_evento, latencia_ms):
    with con:
        con.execute('UPDATE eventos SET latencia_ms = ? WHERE id = ?', (latencia_ms, id_evento))


def auditar(con, accion, detalle):
    with con:
        con.execute('INSERT INTO auditoria (ts, accion, detalle) VALUES (?, ?, ?)',
                    (ahora(), accion, detalle))
