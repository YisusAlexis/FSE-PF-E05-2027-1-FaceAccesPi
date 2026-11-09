#!/usr/bin/env bash
# registrar.sh - Administración de personas por SSH (solo el administrador tiene acceso SSH).
#
#   scripts/registrar.sh alta NOMBRE        registra con la cámara (pausa el servicio)
#   scripts/registrar.sh reemplazar NOMBRE  vuelve a registrar a alguien
#   scripts/registrar.sh baja NOMBRE        da de baja (sin pausar el servicio)
#   scripts/registrar.sh lista              personas registradas
#   scripts/registrar.sh eventos [N]        últimos N eventos del historial
#
# Para el alta, la cámara la usa el servicio: se detiene faceaccess, se registra y se
# vuelve a iniciar (el modelo se actualiza con la operación pausada, bitácora clase 9).
# La ventana del registro aparece en el monitor junto a la cámara.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
SERVICIO="faceaccess.service"

uso() { sed -n '3,9p' "$0"; exit 2; }
[[ $# -lt 1 ]] && uso

accion="$1"; nombre="${2:-}"
case "$accion" in
    lista)    exec python3 vision/registrar.py --listar ;;
    eventos)  exec python3 vision/registrar.py --eventos "${2:-20}" ;;
    baja)     [[ -z "$nombre" ]] && uso; exec python3 vision/registrar.py --borrar "$nombre" ;;
    alta|reemplazar)
        [[ -z "$nombre" ]] && uso
        extra=()
        [[ "$accion" == "reemplazar" ]] && extra=(--reemplazar)
        estaba_activo=0
        if systemctl is-active --quiet "$SERVICIO"; then
            estaba_activo=1
            echo "[registro] Pausando $SERVICIO para liberar la cámara..."
            sudo systemctl stop "$SERVICIO"
        fi
        reanudar() {
            if [[ $estaba_activo -eq 1 ]]; then
                echo "[registro] Reanudando $SERVICIO"
                sudo systemctl start "$SERVICIO"
            fi
        }
        trap reanudar EXIT
        export ESPERA_PANTALLA=5
        # shellcheck source=entorno_pantalla.sh
        source "$DIR/scripts/entorno_pantalla.sh"
        python3 vision/registrar.py --nombre "$nombre" "${extra[@]}"
        exit $?
        ;;
    *) uso ;;
esac
