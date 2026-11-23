#!/usr/bin/env bash
# asistencia.sh - Administración del control de asistencia por SSH (solo el administrador).
#
#   scripts/asistencia.sh modo                     modo actual
#   scripts/asistencia.sh modo productivo          reglas reales (entrada 08:00, bloqueo 3 min, regreso 15 min)
#   scripts/asistencia.sh modo demo [HH:MM]        presentación: bloqueo 30 s, regreso 1 min, entrada HH:MM
#   scripts/asistencia.sh hoy [AAAA-MM-DD]         asistencia del día
#   scripts/asistencia.sh persona NOMBRE [DIAS]    historial de una persona con horas trabajadas
#   scripts/asistencia.sh pendientes               registros en revisión
#   scripts/asistencia.sh aprobar ID [NOTA]        aprobar un registro en revisión
#   scripts/asistencia.sh rechazar ID [NOTA]       rechazar un registro en revisión
#   scripts/asistencia.sh exportar [DESDE] [HASTA] CSV en logs/ (últimos 30 días por defecto)
#   scripts/asistencia.sh limpiar-bloqueados       rechaza BLOQUEADO duplicados (versiones anteriores)
#   scripts/asistencia.sh demo-huerfano NOMBRE     (modo demo) entrada de ayer sin salida
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -lt 1 ]]; then
    sed -n '3,15p' "$0"
    exit 2
fi
exec python3 "$DIR/vision/admin_asistencia.py" "$@"
