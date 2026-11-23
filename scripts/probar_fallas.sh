#!/usr/bin/env bash
# probar_fallas.sh - Las 2 fallas inducidas del Avance 3, con tiempos de recuperación.
#
#   scripts/probar_fallas.sh led      Falla 2: mata el programa del LED (kill -9) y mide
#                                     cuánto tarda systemd en reiniciarlo y Python en reconectarse.
#   scripts/probar_fallas.sh camara   Falla 1: pide desconectar la cámara USB, espera y
#                                     muestra la detección de la falla y la reconexión.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$DIR/logs"
LOG="$DIR/logs/fallas_$(date +%Y%m%d_%H%M%S).log"
log() { echo "$(date +%H:%M:%S.%3N) $*" | tee -a "$LOG"; }

falla_led() {
    log "=== Falla inducida: programa del NeoPixel terminado con kill -9 ==="
    local pid_antes reinicios_antes t0 pid_nuevo t1
    pid_antes=$(systemctl show -p MainPID --value faceaccess-led)
    reinicios_antes=$(systemctl show -p NRestarts --value faceaccess-led)
    if [[ "$pid_antes" == "0" ]]; then
        log "ERROR: faceaccess-led no está corriendo"; exit 1
    fi
    log "PID antes: $pid_antes | reinicios antes: $reinicios_antes"
    t0=$(date +%s%N)
    sudo kill -9 "$pid_antes"
    log "kill -9 enviado"
    for _ in $(seq 1 100); do
        pid_nuevo=$(systemctl show -p MainPID --value faceaccess-led)
        if [[ "$pid_nuevo" != "0" && "$pid_nuevo" != "$pid_antes" ]]; then
            t1=$(date +%s%N)
            log "RECUPERADO: systemd lo reinició (PID $pid_nuevo) en $(( (t1 - t0) / 1000000 )) ms"
            log "Reinicios ahora: $(systemctl show -p NRestarts --value faceaccess-led)"
            break
        fi
        sleep 0.1
    done
    log "Presenta un rostro para confirmar que el LED vuelve a responder; mensajes del reconocimiento:"
    sleep 8
    journalctl -u faceaccess --since "-20s" --no-pager | grep -E "FIFO|Señalización|Evento" | tee -a "$LOG"
}

falla_camara() {
    log "=== Falla inducida: cámara USB desconectada ==="
    local inicio
    inicio=$(date '+%Y-%m-%d %H:%M:%S')
    read -r -p "Desconecta la cámara USB y presiona Enter... " _
    log "Cámara desconectada por el operador"
    sleep 15
    read -r -p "Vuelve a conectar la cámara y presiona Enter... " _
    log "Cámara reconectada por el operador; esperando recuperación"
    sleep 10
    journalctl -u faceaccess --since "$inicio" --no-pager | grep -E "FALLA|RECUPERACIÓN|Cámara|cámara" | tee -a "$LOG"
    journalctl -u faceaccess-led --since "$inicio" --no-pager | grep -E "FALLO|LISTO" | tee -a "$LOG"
}

case "${1:-}" in
    led)    falla_led ;;
    camara) falla_camara ;;
    *)      sed -n '3,8p' "$0"; exit 2 ;;
esac
log "Registro de la prueba: $LOG"
