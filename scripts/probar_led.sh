#!/usr/bin/env bash
# probar_led.sh - Prueba P-05: envía los cuatro estados al programa neopixel_spi por la FIFO
#                 y registra el resultado. El programa en C debe estar corriendo.
#
# Uso:  ./probar_led.sh            envía la secuencia y muestra los tiempos
#       ./probar_led.sh -i         modo interactivo: pregunta si cada color se vio bien
#       ./probar_led.sh -f RUTA    usa otra FIFO (por defecto /tmp/faceaccess_led)

set -u

FIFO="/tmp/faceaccess_led"
INTERACTIVO=0
TIEMPO_MAX_ESCRITURA=2      # segundos; si nadie lee la FIFO, no se queda colgado
CARPETA_LOGS="./logs"

while getopts "if:h" opt; do
    case "$opt" in
        i) INTERACTIVO=1 ;;
        f) FIFO="$OPTARG" ;;
        *) echo "Uso: $0 [-i] [-f fifo]"; exit 2 ;;
    esac
done

mkdir -p "$CARPETA_LOGS"
LOG="$CARPETA_LOGS/probar_led_$(date +%Y%m%d_%H%M%S).log"

log() {
    local linea
    linea="$(date +%H:%M:%S.%3N) [$1] $2"
    echo "$linea" | tee -a "$LOG"
}

# Secuencia: mensaje | color esperado | segundos de espera antes del siguiente
SECUENCIA=(
    "LISTO|azul|2"
    "ENTRADA|verde 2 s y luego azul|3"
    "SALIDA|verde 2 s y luego azul|3"
    "AVISO|amarillo 1.5 s y luego azul|3"
    "REVISION|rojo 2 s y luego azul|3"
    "DESCONOCIDO|rojo 1 s y luego azul|2"
    "FALLO|amarillo|2"
    "LISTO|azul|1"
)

log INFO "=== Prueba P-05: NeoPixel por sí solo ==="
log INFO "Log de esta ejecución: $LOG"

if [[ ! -p "$FIFO" ]]; then
    log ERROR "No existe la FIFO $FIFO. Inicia primero: ./neopixel_spi (o ./neopixel_spi -s para simular)"
    exit 1
fi

correctos=0
total=0
fallas_envio=0

for paso in "${SECUENCIA[@]}"; do
    IFS='|' read -r mensaje esperado espera <<< "$paso"
    total=$((total + 1))

    t0=$(date +%s%N)
    if timeout "$TIEMPO_MAX_ESCRITURA" bash -c "echo '$mensaje' > '$FIFO'"; then
        t_ms=$(( ($(date +%s%N) - t0) / 1000000 ))
        log INFO "Enviado $mensaje en $t_ms ms; se espera: $esperado"
    else
        fallas_envio=$((fallas_envio + 1))
        log ERROR "No se pudo enviar $mensaje en ${TIEMPO_MAX_ESCRITURA} s (¿neopixel_spi está corriendo?)"
        continue
    fi

    if [[ $INTERACTIVO -eq 1 ]]; then
        read -r -p "    ¿La tira mostró $esperado? [s/n]: " resp
        if [[ "$resp" =~ ^[sS]$ ]]; then
            correctos=$((correctos + 1))
            log INFO "Observado: correcto"
        else
            log WARNING "Observado: INCORRECTO"
        fi
    else
        sleep "$espera"
    fi
done

log INFO "=== Resumen P-05 ==="
log INFO "Mensajes enviados: $((total - fallas_envio))/$total"
if [[ $INTERACTIVO -eq 1 ]]; then
    log INFO "Colores correctos (observados): $correctos/$total"
fi

if [[ $fallas_envio -gt 0 ]]; then
    exit 1
fi
if [[ $INTERACTIVO -eq 1 && $correctos -ne $total ]]; then
    exit 1
fi
exit 0
