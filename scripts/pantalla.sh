#!/usr/bin/env bash
# pantalla.sh - Controla la imagen de FaceAccessPi en el monitor sin reiniciar el servicio.
#
#   scripts/pantalla.sh completa    pantalla completa (modo escritorio)
#   scripts/pantalla.sh ventana     ventana normal de 960x540 para usar el escritorio al lado
#   scripts/pantalla.sh ocultar     quita la imagen del monitor; el reconocimiento SIGUE funcionando
#   scripts/pantalla.sh mostrar     vuelve a mostrarla
#
# En modo kiosco (sin escritorio) solo aplican "ocultar" y "mostrar": el framebuffer siempre
# ocupa toda la pantalla. Para el escritorio completo: scripts/modo.sh escritorio | temporal.
set -u
FIFO="${FACEACCESS_FIFO_CONTROL:-/tmp/faceaccess_ctl}"

case "${1:-}" in
    completa|ventana|ocultar|mostrar) ;;
    *) sed -n '3,10p' "$0"; exit 2 ;;
esac
if [[ ! -p "$FIFO" ]]; then
    echo "No existe $FIFO: ¿está corriendo el servicio? (systemctl status faceaccess)" >&2
    exit 1
fi
if timeout 2 bash -c "echo '$1' > '$FIFO'"; then
    sleep 0.5
    journalctl -u faceaccess -n 20 --no-pager 2>/dev/null | grep "Orden de pantalla" | tail -1
    if [[ "$1" == "ocultar" && "$(systemctl get-default)" != "graphical.target" ]]; then
        echo "Modo kiosco: no hay escritorio detrás, la pantalla queda en negro (es lo esperado)."
        echo "Para usar el escritorio: scripts/modo.sh escritorio && sudo reboot   (o scripts/modo.sh temporal)"
    fi
else
    echo "El servicio no respondió" >&2
    exit 1
fi
