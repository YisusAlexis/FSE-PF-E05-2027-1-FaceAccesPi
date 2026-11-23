# FaceAccessPi – Control de asistencia por reconocimiento facial (Raspberry Pi 4)

Proyecto integrador de Fundamentos de Sistemas Embebidos (FI-UNAM, 2027-1), equipo 5, grupo 6: Alexis, Jimena, Oswaldo y Carolina.

Una cámara USB captura el rostro. **YuNet** lo detecta y **SFace** lo compara con las personas registradas en SQLite, con sus datos **cifrados con AES-256-GCM**. Cada reconocimiento se convierte en una **entrada o salida** según las reglas de asistencia. El monitor muestra la cámara y un panel con la hora, el estado y el aviso de cada registro. Este panel se dibuja directo en el framebuffer, sin escritorio. Una tira **NeoPixel WS2812** conectada por **SPI0** indica el resultado:

| Color | Significado |
| --- | --- |
| Azul | Listo, esperando un rostro |
| Verde (2 s) | Entrada o salida registrada |
| Amarillo (1.5 s) | Rostro no reconocido con claridad: acérquese o quédese quieto. También: reloj sin sincronizar |
| Rojo (1 s) | Persona no reconocida |
| Rojo (2 s) | Registro bloqueado, enviado a revisión del supervisor |
| Amarillo fijo | Falla (por ejemplo, cámara desconectada) |

El sistema arranca solo al encender la Raspberry, sin teclado y sin intervención.

## Arquitectura

```
Cámara USB --V4L2--> [faceaccess.py]                                  [neopixel_spi (C)] --SPI0--> NeoPixel
                      hilo captura --cola(1)--> hilo reconocimiento --FIFO--> lee estado, codifica bits
                                                  YuNet + SFace         /tmp/faceaccess_led
                                                  SQLite cifrado (AES-256-GCM)
                      motor de asistencia (reglas) --> registros
                      hilo principal: panel en /dev/fb0 (o ventana) + métricas
                        ^ FIFO /tmp/faceaccess_ctl (completa / ventana / ocultar / mostrar)
Administrador --SSH--> scripts/registrar.sh  (alta / baja / lista / eventos)
                       scripts/asistencia.sh (modo / hoy / persona / pendientes / aprobar / exportar)
                       scripts/pantalla.sh   (control de la ventana)
```

| Bloque | Archivo |
| --- | --- |
| Captura, reconocimiento, decisión y eventos | `vision/faceaccess.py` |
| YuNet + SFace, cámara, log y métricas | `vision/comun.py` |
| Reglas de asistencia (perfiles PRODUCTIVO y DEMO) | `vision/asistencia.py` |
| Base de datos (personas, registros, eventos y auditoría) | `vision/db.py` |
| Cifrado AES-256-GCM e índice HMAC | `vision/cripto.py` |
| Panel en framebuffer o ventana | `vision/pantalla.py` |
| Cliente de la FIFO | `vision/fifo_led.py` |
| Alta y baja de personas | `vision/registrar.py`, `scripts/registrar.sh` |
| Consultas y revisión de asistencia | `vision/admin_asistencia.py`, `scripts/asistencia.sh` |
| Control de la ventana | `scripts/pantalla.sh` |
| Parámetros | `vision/config.py` |
| NeoPixel por SPI (C) | `led/neopixel_spi.c` |
| Arranque y modo kiosco o escritorio | `systemd/*.service.in`, `scripts/iniciar_vision.sh`, `scripts/modo.sh` |

## Hardware

| Elemento | Conexión en la Raspberry Pi 4 |
| --- | --- |
| NeoPixel WS2812 (8 LED), DIN | Pin 19 (GPIO10, SPI0 MOSI) con resistencia de 330 Ω |
| NeoPixel VCC / GND | Pin 1 (3.3 V) / pin 9 (GND); capacitor de 470 a 1000 µF entre VCC y GND. Con 3.3 V, los datos de la Pi quedan dentro del nivel lógico de la tira |
| Ventilador de 5 V | Pin 4 (5 V) / pin 6 (GND) |
| Cámara USB | Puerto USB; aparece como `/dev/video0` |
| Monitor | Salida micro-HDMI (con adaptador si el monitor es VGA) |
| Fuente | USB-C de 5 V / 3 A |
| Obligatorio | Ventilador (y disipador). Sin él, la Pi llegó a 82 °C, bajó el reloj del núcleo de 500 a 333 MHz y la tira se ponía blanca. Con ventilador funcionó más de 1 h sin fallas |

**Señal del NeoPixel:** 4 bits SPI por bit del LED, a 3.2 MHz. Los pulsos quedan en T0H = 0.31 µs y T1H = 0.63 µs, dentro de las hojas de datos del WS2812 y del WS2812B. El color se reenvía cada segundo. Además, el programa lee la frecuencia real del núcleo por el mailbox del firmware (`/dev/vcio`) y ajusta la velocidad SPI si el núcleo baja de 500 MHz.

## Instalación (reproducible)

Requisito: Raspberry Pi OS de 64 bits (Debian 13 Trixie).

```bash
git clone https://github.com/YisusAlexis/FSE-PF-E05-2027-1-FaceAccesPi.git
cd FSE-PF-E05-2027-1-FaceAccesPi
sudo bash scripts/instalar.sh            # primera vez: modo kiosco; al actualizar conserva el modo (--escritorio / --kiosco para forzar)
sudo reboot
```

`instalar.sh` hace lo siguiente:

- instala OpenCV (4.8 o superior), `python3-cryptography`, el compilador y utilidades;
- activa SPI y agrega `core_freq_min=500`;
- configura el **modo kiosco**: arranque en consola, sin pantalla de inicio de sesión, sin cursor y sin apagado de pantalla;
- crea la **clave de cifrado** `/etc/faceaccess/clave.bin` (32 bytes, permisos 0400, dueño el usuario del servicio);
- da al usuario los permisos `video` (cámara y framebuffer) y `spi`;
- descarga y verifica (SHA-256) los modelos YuNet y SFace;
- compila el programa en C y corre su autoprueba;
- instala y habilita los dos servicios de systemd.

**Respalda la clave.** Sin `/etc/faceaccess/clave.bin` no se pueden leer las personas registradas.

## Modo kiosco y modo escritorio

```bash
scripts/modo.sh estado        # modo actual
scripts/modo.sh kiosco        # arranque en consola; el panel se dibuja en /dev/fb0 (modo del proyecto)
scripts/modo.sh escritorio    # arranque con escritorio; FaceAccessPi abre su ventana ahí
scripts/modo.sh temporal      # abre el escritorio ahora; al reiniciar vuelve al modo kiosco
```

Los cambios de `kiosco` y `escritorio` se aplican al reiniciar. En modo kiosco, Ctrl+Alt+F2 abre una consola local.

## Uso

Al encender, el LED se pone azul y el panel aparece en el monitor en cuanto se procesa el primer cuadro.

La administración se hace por SSH. Solo el administrador tiene la contraseña:

```bash
scripts/registrar.sh alta Alexis        # 15 muestras; pausa el servicio unos segundos; vista previa en el monitor
scripts/registrar.sh lista
scripts/registrar.sh baja Alexis        # sin reiniciar el servicio
scripts/registrar.sh eventos 20         # historial
python3 vision/registrar.py --auditoria 20   # altas, bajas y reemplazos
```

Registrar a una persona **no reentrena nada**: su vector SFace de 128 valores se guarda cifrado en SQLite y el servicio lo carga en unos 3 s.

## Control de asistencia

Cada persona solo se presenta frente a la cámara. El sistema decide si es entrada o salida (alternan) y aplica estas reglas:

| Regla | Modo PRODUCTIVO | Modo DEMO |
| --- | --- | --- |
| Hora de entrada | 08:00 | Configurable (por defecto, la hora en que se activa) |
| Puntual / retardo / retardo mayor | Hasta +10 min / +11 a +15 min / después de +15 min (va a revisión) | Igual |
| Bloqueo tras cualquier registro (sin aviso, sin registro) | 3 min | 30 s |
| Regreso mínimo después de una salida (antes se descarta como doble marcaje) | 15 min | 1 min |
| Máximo diario | 2 entradas y 2 salidas; el 5.º intento queda BLOQUEADO y va a revisión. Los intentos siguientes del día solo encienden el LED rojo, sin crear más pendientes | Igual |
| Turno sin salida | Se cierra a las 18:00 como "Salida no registrada" al siguiente escaneo | Igual |
| Similitud para registrar | 0.70 (entre 0.363 y 0.70: "acérquese o quédese quieto") | Igual |
| Reloj sin sincronizar (NTP) | No registra; el panel muestra "Reloj sin sincronizar" | Igual |

La Raspberry no tiene reloj con batería: necesita internet al arrancar para sincronizar la hora. Mientras no lo haga, no se registra nada.

```bash
scripts/asistencia.sh modo                    # modo actual y sus parámetros
scripts/asistencia.sh modo demo 08:00         # presentación (el servicio lo aplica en ~3 s, sin reiniciar)
scripts/asistencia.sh modo productivo         # reglas reales
scripts/asistencia.sh hoy                     # asistencia del día con horas trabajadas
scripts/asistencia.sh persona Alexis 7        # historial de una persona
scripts/asistencia.sh pendientes              # registros en revisión
scripts/asistencia.sh aprobar 12 "con permiso"
scripts/asistencia.sh rechazar 13
scripts/asistencia.sh exportar 2026-11-01 2026-11-30   # CSV en logs/ (permisos 0600)
scripts/asistencia.sh limpiar-bloqueados    # rechaza BLOQUEADO duplicados de versiones anteriores
scripts/asistencia.sh demo-huerfano Alexis    # solo en DEMO: entrada de ayer sin salida, para mostrar el cierre
```

Cada cambio de modo, aprobación, rechazo y exportación queda en la auditoría.

## Control de la ventana

```bash
scripts/pantalla.sh completa     # pantalla completa (modo escritorio)
scripts/pantalla.sh ventana      # ventana de 960x540 para usar el escritorio al lado
scripts/pantalla.sh ocultar      # quita la imagen; el reconocimiento sigue funcionando
scripts/pantalla.sh mostrar      # la vuelve a mostrar
```

En modo kiosco solo aplican `ocultar` y `mostrar`, y al ocultar la pantalla queda en negro porque no hay escritorio detrás. Para ver el escritorio, cambia a modo escritorio con `scripts/modo.sh escritorio` y reinicia.

## Servicios y diagnóstico

```bash
systemctl status faceaccess faceaccess-led
journalctl -u faceaccess -f              # eventos, latencia, CPU, memoria y temperatura en vivo
sudo systemctl restart faceaccess
```

## Pruebas y evidencia

```bash
make -C led prueba                       # autoprueba de la codificación SPI
make -C led asm                          # ensamblador generado (gcc -S y objdump de codificar)
led/neopixel_spi -s                      # programa del LED en simulación (sin SPI)
scripts/probar_led.sh -i                 # todos los colores (con faceaccess-led corriendo)
python3 vision/test_asistencia.py        # 13 casos de las reglas de asistencia, con reloj simulado
scripts/probar_fallas.sh led             # falla inducida: kill -9 al programa del LED
scripts/probar_fallas.sh camara          # falla inducida: desconectar la cámara
sudo scripts/evidencia.sh                # drivers, framebuffer, cifrado, servicios y mediciones a logs/
```

## Parámetros

Están en `vision/config.py`. Cada uno se puede cambiar con una variable de entorno `FACEACCESS_<NOMBRE>`:

| Parámetro | Valor | Para qué sirve |
| --- | --- | --- |
| `UMBRAL_COSENO` | 0.363 | Similitud mínima para considerar a una persona (valor de referencia de OpenCV); el registro exige 0.70 |
| `FPS_PROCESO` | 6 | Imágenes procesadas por segundo como máximo (bajado de 10 por temperatura) |
| `ESCALA_DETECCION` | 0.5 | YuNet trabaja sobre 320x240 |
| `CUADROS_CONFIRMACION` | 3 | Cuadros seguidos con el mismo resultado antes de decidir |
| `REARME_S` | 2 | Segundos sin rostro para volver a decidir (RF-05) |
| `PERMANENCIA_MAX_S` | 10 | Máximo un evento cada 10 s de permanencia continua (RF-05) |
| `PANTALLA` | auto | `fb`, `qt`, `no` o `auto` |
| `FPS_PANTALLA` | 6 | Redibujado del panel por segundo |
| `REQUIERE_NTP` | 1 | Exige hora sincronizada para registrar |
| `CLAVE` | /etc/faceaccess/clave.bin | Archivo de la clave de cifrado |

## Datos personales

- Los nombres y los vectores faciales se guardan **cifrados con AES-256-GCM**, ligados a su fila con datos asociados. La búsqueda por nombre usa un índice HMAC.
- Los eventos y los registros de asistencia guardan solo el id numérico de la persona. El CSV exportado sí lleva nombres, por eso se crea con permisos 0600.
- `secure_delete` está activado, y la migración desde la base del Avance 3 borra con `VACUUM` los datos que estaban en claro.
- `datos/`, `logs/` y `modelos/` no se suben al repositorio (ver `.gitignore`).

## Versión anterior

`legacy/windows_eigenfaces/` contiene la validación del Avance 2 (Haar + EigenFaces en Windows 11).
