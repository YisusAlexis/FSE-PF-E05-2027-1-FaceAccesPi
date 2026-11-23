#!/usr/bin/env bash
# modo.sh - Cambia cómo arranca la Raspberry Pi (por SSH o en la consola).
#
#   scripts/modo.sh kiosco       Arranca en consola, sin escritorio. FaceAccessPi dibuja en el
#                                monitor (framebuffer) en cuanto procesa el primer cuadro.
#                                Es el modo del proyecto y de la presentación.
#   scripts/modo.sh escritorio   Arranca con el escritorio normal (para otras materias).
#                                FaceAccessPi sigue funcionando y abre su ventana en el escritorio.
#   scripts/modo.sh temporal     Abre el escritorio ahora, sin cambiar el arranque; al reiniciar
#                                vuelve a modo kiosco.
#   scripts/modo.sh estado       Muestra el modo actual.
#
# Los cambios de "kiosco" y "escritorio" se aplican al reiniciar (sudo reboot).
set -u
SERVICIO=faceaccess.service

case "${1:-estado}" in
    kiosco)
        sudo systemctl set-default multi-user.target
        sudo systemctl disable getty@tty1.service 2>/dev/null   # sin pantalla de login sobre la imagen
        echo "Modo kiosco configurado. Reinicia con: sudo reboot"
        echo "(Para entrar a una consola local: Ctrl+Alt+F2)"
        ;;
    escritorio)
        sudo systemctl set-default graphical.target
        sudo systemctl enable getty@tty1.service 2>/dev/null
        echo "Modo escritorio configurado. Reinicia con: sudo reboot"
        ;;
    temporal)
        echo "Abriendo el escritorio sin cambiar el arranque..."
        sudo systemctl stop "$SERVICIO"
        sudo systemctl start graphical.target
        echo "Escritorio iniciado. FaceAccessPi quedó detenido; para volver al modo kiosco: sudo reboot"
        ;;
    estado)
        objetivo="$(systemctl get-default)"
        if [[ "$objetivo" == "graphical.target" ]]; then
            echo "Modo actual: ESCRITORIO ($objetivo)"
        else
            echo "Modo actual: KIOSCO ($objetivo)"
        fi
        systemctl is-active --quiet "$SERVICIO" && echo "FaceAccessPi: activo" || echo "FaceAccessPi: detenido"
        journalctl -u "$SERVICIO" -b --no-pager 2>/dev/null | grep -E '\[pantalla\]|Pantalla en' | tail -2
        ;;
    *)
        sed -n '3,14p' "$0"; exit 2 ;;
esac
