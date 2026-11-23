#!/usr/bin/env bash
# evidencia.sh - Junta en un archivo la evidencia de terminal para el PDF del Avance 3:
#   - Clase 21: archivo de dispositivo, biblioteca y driver de cada sensor/actuador
#   - Clase 20: tipos y volatile en el código que toca hardware
#   - Mediciones: RNF-02 (arranque), RNF-01 (latencia), RNF-04 (memoria)
#   - Estado de los servicios systemd
#   - Clase 19: ensamblador generado por gcc en codificar() y soporte NEON de OpenCV
#   - Control de asistencia: modo, registros del día, pendientes y prueba de reglas
# Uso:  sudo scripts/evidencia.sh      (sudo para leer dmesg)
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$DIR/logs"
SALIDA="$DIR/logs/evidencia_$(date +%Y%m%d_%H%M%S).txt"

seccion() { printf '\n========== %s ==========\n' "$*"; }
cmd() { printf '\n$ %s\n' "$*"; bash -c "$*" 2>&1; }

{
seccion "SISTEMA"
cmd "date"
cmd "cat /etc/os-release | head -4"
cmd "uname -a"
cmd "free -h"
cmd "python3 -c 'import cv2; print(\"OpenCV\", cv2.__version__)'"
cmd "vcgencmd measure_temp; vcgencmd get_throttled; vcgencmd measure_clock arm; vcgencmd measure_clock core"
cmd "grep -E '^(dtparam=spi|core_freq_min)' /boot/firmware/config.txt"

seccion "CLASE 21 - CÁMARA USB (sensor)"
cmd "ls -l /dev/video0"
cmd "v4l2-ctl --list-devices"
cmd "v4l2-ctl -d /dev/video0 --list-formats-ext | head -30"
cmd "readlink -f /sys/class/video4linux/video0/device/driver"
cmd "lsmod | grep -E '^(uvcvideo|videobuf2|videodev)'"
cmd "dmesg | grep -i -E 'uvc|usb.*camera|webcam' | tail -10"

seccion "CLASE 21 - NEOPIXEL WS2812 POR SPI0 (actuador)"
cmd "ls -l /dev/spidev0.0"
cmd "readlink -f /sys/bus/spi/devices/spi0.0/driver"
cmd "readlink -f /sys/class/spi_master/spi0/device/driver"
cmd "lsmod | grep -E '^(spidev|spi_bcm2835)'"
cmd "dmesg | grep -i spi | tail -10"

seccion "CLASE 21 - MONITOR (salida de video)"
cmd "ls -l /dev/dri/"
cmd "lsmod | grep -E '^(vc4|v3d|drm)' "
cmd "cat /sys/class/drm/card*-HDMI-A-*/status 2>/dev/null"

seccion "CLASE 21 - FRAMEBUFFER (pantalla en modo kiosco)"
cmd "ls -l /dev/fb0"
cmd "cat /sys/class/graphics/fb0/name /sys/class/graphics/fb0/virtual_size /sys/class/graphics/fb0/bits_per_pixel /sys/class/graphics/fb0/stride"
cmd "systemctl get-default"
cmd "journalctl -u faceaccess -b --no-pager | grep -E 'pantalla|Pantalla en' | tail -3"

seccion "CLASE 21 - ALMACENAMIENTO (microSD por SDIO)"
cmd "lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT /dev/mmcblk0"
cmd "readlink -f /sys/block/mmcblk0/device/driver"
cmd "ls -l $DIR/datos/"

seccion "CIFRADO DE DATOS BIOMÉTRICOS (AES-256-GCM)"
cmd "ls -ld /etc/faceaccess; ls -l /etc/faceaccess/clave.bin"
cmd "sqlite3 $DIR/datos/faceaccess.db 'SELECT valor FROM meta WHERE clave=\"esquema\"; SELECT id, substr(indice,1,16), length(nombre_cif), length(vector_cif), hex(substr(vector_cif,1,16)) FROM personas;'"
cmd "python3 - <<'EOF'
import sys, glob
sys.path.insert(0, '$DIR/vision')
import config
from cripto import Cripto
from db import Base
nombres = [n for _, n, _, _ in Base(config.RUTA_DB, Cripto(config.RUTA_CLAVE)).listar()]
crudo = b''.join(open(f, 'rb').read() for f in glob.glob(config.RUTA_DB + '*'))
for n in nombres:
    print(f'{n}: aparece en claro en la base? {n.encode() in crudo}')
EOF"

seccion "RELOJ"
cmd "timedatectl"

seccion "CONTROL DE ASISTENCIA"
cmd "$DIR/scripts/asistencia.sh modo"
cmd "$DIR/scripts/asistencia.sh hoy"
cmd "$DIR/scripts/asistencia.sh pendientes"
cmd "sqlite3 $DIR/datos/faceaccess.db 'SELECT tipo, estado, COALESCE(revision,\"-\"), count(*) FROM registros GROUP BY 1,2,3;'"
cmd "python3 $DIR/vision/test_asistencia.py | tail -1"

seccion "CLASE 20 - TIPOS Y VOLATILE (led/neopixel_spi.c)"
cmd "grep -n -E 'volatile|uint8_t|uint32_t|sig_atomic_t' $DIR/led/neopixel_spi.c | head -30"
cmd "printf '#include <stdio.h>\n#include <stdint.h>\n#include <signal.h>\nint main(void){printf(\"sizeof uint8_t=%%zu uint32_t=%%zu sig_atomic_t=%%zu long long=%%zu puntero=%%zu\\\\n\",sizeof(uint8_t),sizeof(uint32_t),sizeof(sig_atomic_t),sizeof(long long),sizeof(void*));return 0;}' > /tmp/tipos.c && gcc -o /tmp/tipos /tmp/tipos.c && /tmp/tipos"
cmd "$DIR/led/neopixel_spi -T"
cmd "journalctl -u faceaccess-led -b --no-pager | grep -E 'Terminando|Resumen|actualización' | tail -6"

seccion "CLASE 19 - ENSAMBLADOR (gcc -S y objdump de la parte crítica)"
cmd "make -C $DIR/led asm"
cmd "echo -n 'Llamadas a poner_bit dentro de codificar (0 = el compilador la integró): '; grep -cE 'call.*<poner_bit>|bl.*<poner_bit>' $DIR/led/codificar.objdump"
cmd "sed -n '1,60p' $DIR/led/codificar.objdump"
cmd "python3 -c 'import cv2; print(\"OpenCV usa NEON (SIMD de ARM):\", cv2.checkHardwareSupport(100)); print(\"Optimizaciones:\", cv2.getBuildInformation().split(\"CPU/HW features:\")[1].split(\"C/C++:\")[0].strip())'"
cmd "make -C $DIR/led clean >/dev/null; make -C $DIR/led >/dev/null && echo binario recompilado"

seccion "SERVICIOS SYSTEMD"
cmd "systemctl status faceaccess-led faceaccess --no-pager | head -30"
cmd "systemctl show faceaccess-led -p NRestarts -p Nice -p ActiveEnterTimestamp"
cmd "systemctl show faceaccess -p NRestarts -p ActiveEnterTimestamp"
cmd "ps -eLo pid,tid,ni,comm,args | grep -E 'neopixel_spi|faceaccess' | grep -v grep"

seccion "MEDICIÓN RNF-02 - ARRANQUE (<= 60 s desde la energización)"
cmd "systemd-analyze"
cmd "systemd-analyze critical-chain faceaccess.service"
cmd "journalctl -u faceaccess -b --no-pager | grep 'SISTEMA LISTO'"

seccion "MEDICIÓN RNF-04 - MEMORIA (< 512 MiB)"
cmd "for p in \$(pgrep -f 'faceaccess.py|neopixel_spi'); do grep -H -E 'Name|VmRSS|VmHWM' /proc/\$p/status; done"
cmd "journalctl -u faceaccess -b --no-pager | grep 'MÉTRICAS' | tail -6"

seccion "MEDICIÓN RNF-01 - LATENCIA DE EVENTOS (p95 <= 2000 ms)"
cmd "python3 - <<'EOF'
import sqlite3, statistics
con = sqlite3.connect('$DIR/datos/faceaccess.db')
lat = [r[0] for r in con.execute('SELECT latencia_ms FROM eventos WHERE latencia_ms IS NOT NULL')]
print('eventos con latencia:', len(lat))
if lat:
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
    print(f'prom {statistics.mean(lat):.0f} ms | p95 {p95:.0f} ms | max {max(lat):.0f} ms')
for r in con.execute('SELECT resultado, COUNT(*) FROM eventos GROUP BY resultado'):
    print(r)
EOF"
} > "$SALIDA"

echo "Evidencia guardada en: $SALIDA"
