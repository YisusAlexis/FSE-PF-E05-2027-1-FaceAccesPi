#!/usr/bin/env bash
# entorno_pantalla.sh - Se carga con "source". Decide dónde se dibuja la imagen:
#   - Modo kiosco (arranque en consola, multi-user.target): directo en /dev/fb0.
#     No depende del escritorio; la imagen sale en cuanto el servicio procesa el primer cuadro.
#   - Modo escritorio (graphical.target): ventana de OpenCV (Qt5 por XWayland).
# Deja el resultado en FACEACCESS_PANTALLA (fb, qt o no). Ver scripts/modo.sh.

if [[ -n "${FACEACCESS_PANTALLA:-}" && "${FACEACCESS_PANTALLA}" != "auto" ]]; then
    echo "[pantalla] Modo fijado por variable: $FACEACCESS_PANTALLA"
    return 0 2>/dev/null || exit 0
fi

objetivo="$(systemctl get-default 2>/dev/null || echo multi-user.target)"

if [[ "$objetivo" != "graphical.target" ]]; then
    if [[ -w /dev/fb0 ]]; then
        export FACEACCESS_PANTALLA=fb
        echo "[pantalla] Modo kiosco: framebuffer /dev/fb0"
    else
        export FACEACCESS_PANTALLA=no
        echo "[pantalla] Modo kiosco sin acceso a /dev/fb0 (¿usuario en el grupo video?): sin pantalla"
    fi
    return 0 2>/dev/null || exit 0
fi

# Modo escritorio: OpenCV de Debian usa Qt5, que se conecta por XWayland
ESPERA="${ESPERA_PANTALLA:-60}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DISPLAY="${DISPLAY:-:0}"
export QT_QPA_PLATFORM=xcb
[[ -f "$HOME/.Xauthority" ]] && export XAUTHORITY="$HOME/.Xauthority"
PRUEBA="import cv2, numpy as np; cv2.imshow('t', np.zeros((8, 8, 3), np.uint8)); cv2.waitKey(1); cv2.destroyAllWindows()"

inicio=$SECONDS
export FACEACCESS_PANTALLA=no
while (( SECONDS - inicio < ESPERA )); do
    # prueba en un proceso aparte: si Qt no encuentra pantalla aborta, y no debe tumbar el servicio
    if timeout 20 python3 -c "$PRUEBA" >/dev/null 2>&1; then
        export FACEACCESS_PANTALLA=qt
        break
    fi
    sleep 3
done
echo "[pantalla] Modo escritorio: $FACEACCESS_PANTALLA (tras $(( SECONDS - inicio )) s)"
