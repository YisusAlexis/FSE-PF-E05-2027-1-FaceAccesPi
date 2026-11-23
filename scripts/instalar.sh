#!/usr/bin/env bash
# instalar.sh - Deja la Raspberry Pi 4 lista para FaceAccessPi (Raspberry Pi OS 64 bits, Trixie).
# Uso (desde la raíz del repositorio):
#     sudo bash scripts/instalar.sh              primera vez: modo kiosco; al actualizar: conserva el modo actual
#     sudo bash scripts/instalar.sh --escritorio deja el escritorio como arranque
#     sudo bash scripts/instalar.sh --kiosco     fuerza el modo kiosco
# Es idempotente: puede ejecutarse varias veces. El modo se cambia después con scripts/modo.sh.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Ejecuta con sudo: sudo $0" >&2
    exit 1
fi
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USUARIO="${SUDO_USER:-$(logname)}"
CONFIG_TXT=/boot/firmware/config.txt
CMDLINE_TXT=/boot/firmware/cmdline.txt
DIR_CLAVE=/etc/faceaccess
CLAVE="$DIR_CLAVE/clave.bin"
# Primera instalación: kiosco. Reinstalación (actualizar): conserva el modo que ya tenía la Pi.
MODO=kiosco
if [[ -f /etc/systemd/system/faceaccess.service && "$(systemctl get-default)" == "graphical.target" ]]; then
    MODO=escritorio
fi
case "${1:-}" in
    --escritorio) MODO=escritorio ;;
    --kiosco)     MODO=kiosco ;;
esac
paso() { echo; echo "==> $*"; }

# Si el proyecto se copió por Windows o USB, los scripts pierden el permiso de ejecución
chmod +x "$DIR"/scripts/*.sh

paso "1/9 Paquetes del sistema (OpenCV de Debian, compilador, utilidades)"
apt-get update
apt-get install -y python3-opencv python3-numpy python3-cryptography build-essential sqlite3 v4l-utils curl

paso "2/9 Verificando OpenCV (YuNet necesita >= 4.8)"
python3 - <<'EOF'
import cv2, sys
v = tuple(int(x) for x in cv2.__version__.split('.')[:2])
ok = v >= (4, 8) and hasattr(cv2, 'FaceDetectorYN') and hasattr(cv2, 'FaceRecognizerSF')
print(f'OpenCV {cv2.__version__}: {"OK" if ok else "NO COMPATIBLE"}')
sys.exit(0 if ok else 1)
EOF

paso "3/9 SPI activado y reloj del núcleo fijo (NeoPixel)"
raspi-config nonint do_spi 0
if ! grep -q '^core_freq_min=500' "$CONFIG_TXT"; then
    echo 'core_freq_min=500' >> "$CONFIG_TXT"
    echo "Agregado core_freq_min=500 a $CONFIG_TXT"
fi

paso "4/9 Modo de arranque: $MODO"
raspi-config nonint do_boot_behaviour B4     # deja listo el escritorio con autologin (modo escritorio)
raspi-config nonint do_blanking 1 || true    # sin apagado de pantalla
# consola: sin apagado de pantalla ni cursor parpadeando sobre la imagen del framebuffer
for opcion in consoleblank=0 vt.global_cursor_default=0; do
    grep -q "$opcion" "$CMDLINE_TXT" || sed -i "1 s/\$/ $opcion/" "$CMDLINE_TXT"
done
if [[ "$MODO" == "kiosco" ]]; then
    systemctl set-default multi-user.target
    systemctl disable getty@tty1.service 2>/dev/null || true
else
    systemctl set-default graphical.target
    systemctl enable getty@tty1.service 2>/dev/null || true
fi

paso "5/9 Permisos del usuario $USUARIO (cámara, framebuffer y SPI)"
usermod -aG video,spi,gpio "$USUARIO"

paso "6/9 Clave de cifrado AES-256-GCM"
install -d -m 700 -o "$USUARIO" -g "$USUARIO" "$DIR_CLAVE"
if [[ -f "$CLAVE" ]]; then
    echo "La clave ya existe; se conserva (sin ella no se pueden leer los datos cifrados)"
else
    sudo -u "$USUARIO" python3 -c "import sys; sys.path.insert(0, '$DIR/vision'); import cripto; cripto.generar_clave('$CLAVE')"
    echo "Clave creada en $CLAVE (32 bytes, permisos 0400). Respáldala fuera de la Raspberry."
fi
chown "$USUARIO:$USUARIO" "$CLAVE"
chmod 400 "$CLAVE"
ls -l "$CLAVE"

paso "7/9 Modelos YuNet + SFace"
sudo -u "$USUARIO" bash "$DIR/scripts/descargar_modelos.sh"

paso "8/9 Compilando el programa en C del NeoPixel"
sudo -u "$USUARIO" make -C "$DIR/led" clean
sudo -u "$USUARIO" make -C "$DIR/led"
sudo -u "$USUARIO" "$DIR/led/neopixel_spi" -T
sudo -u "$USUARIO" mkdir -p "$DIR/datos" "$DIR/logs"
chmod +x "$DIR"/scripts/*.sh

paso "9/9 Servicios systemd (arranque automático)"
for unidad in faceaccess-led faceaccess; do
    sed -e "s|@USUARIO@|$USUARIO|g" -e "s|@DIR@|$DIR|g" \
        "$DIR/systemd/$unidad.service.in" > "/etc/systemd/system/$unidad.service"
done
systemctl daemon-reload
systemctl disable faceaccess-led.service faceaccess.service 2>/dev/null || true   # rehace los enlaces
systemctl enable faceaccess-led.service faceaccess.service

echo
echo "Instalación terminada (modo $MODO). Reinicia para aplicar los cambios:"
echo "    sudo reboot"
echo "Registrar personas por SSH:   scripts/registrar.sh alta NOMBRE"
echo "Cambiar de modo:              scripts/modo.sh kiosco | escritorio | temporal | estado"
