/*
 * neopixel_spi.c - Señalización del control de acceso con una tira WS2812 (NeoPixel)
 *                  conectada al bus SPI0 de la Raspberry Pi 4 (MOSI = GPIO10, pin 19).
 *
 * Lee mensajes de texto de una tubería con nombre (FIFO) y cambia el color de la tira:
 *
 *   LISTO        -> azul (permanente)
 *   REGISTRADO   -> verde durante 1 s, luego vuelve a LISTO
 *   DESCONOCIDO  -> rojo durante 1 s, luego vuelve a LISTO
 *   FALLO        -> amarillo (permanente)
 *   APAGADO      -> todos los LED apagados
 *
 * Decisión bitácora clase 18: Linux no garantiza los pulsos de 0.35 us / 0.7 us del WS2812,
 * así que no se generan cambiando un GPIO. El periférico SPI los genera por hardware:
 * a 2.4 MHz cada bit SPI dura 0.417 us y cada bit del LED se codifica con 3 bits SPI
 *   bit 0 del LED -> 100  (0.417 us en alto, 0.833 us en bajo)
 *   bit 1 del LED -> 110  (0.833 us en alto, 0.417 us en bajo)
 * 8 LED x 24 bits x 3 = 576 bits = 72 bytes, más bytes en cero para el reset (> 280 us).
 *
 * Decisión bitácora clase 11 (cambio): proceso separado del reconocimiento (Python),
 * comunicado por la FIFO /tmp/faceaccess_led.
 *
 * Compilar:  gcc -Wall -Wextra -O2 -o neopixel_spi neopixel_spi.c
 * Uso:       ./neopixel_spi [-d /dev/spidev0.0] [-f /tmp/faceaccess_led] [-n 8] [-b 20]
 *                           [-s]   modo simulación: no usa SPI, imprime lo que enviaría
 *                           [-T]   autoprueba de la codificación y termina
 */

#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifdef __linux__
#include <linux/spi/spidev.h>
#endif

/* ----------------------------- Configuración ----------------------------- */
#define SPI_DISPOSITIVO_DEF  "/dev/spidev0.0"
#define FIFO_DEF             "/tmp/faceaccess_led"
#define SPI_VELOCIDAD_HZ     2400000u   /* 3 bits SPI = 1.25 us = 800 kHz del WS2812 */
#define BITS_SPI_POR_BIT     3
#define BITS_POR_LED         24         /* orden G, R, B; 8 bits cada uno */
#define NUM_LEDS_DEF         8
#define NUM_LEDS_MAX         64
#define BRILLO_DEF           20         /* %: limita la corriente a ~100 mA con 8 LED */
#define BYTES_INICIO         1          /* línea en bajo antes de los datos */
#define BYTES_RESET          90         /* 90 x 8 x 0.417 us = 300 us en bajo (> 280 us) */
#define DURACION_TEMPORAL_MS 1000       /* tiempo de verde / rojo antes de volver a LISTO */
#define TAM_LINEA            64

/* Tamaño máximo del búfer SPI: inicio + datos + reset */
#define BYTES_DATOS(n)  (((n) * BITS_POR_LED * BITS_SPI_POR_BIT + 7) / 8)
#define TAM_BUFER_MAX   (BYTES_INICIO + BYTES_DATOS(NUM_LEDS_MAX) + BYTES_RESET)

typedef struct {
    const char *nombre;
    uint8_t r, g, b;
    int duracion_ms;          /* 0 = permanente */
    const char *siguiente;    /* estado al terminar la duración */
} Estado;

static const Estado ESTADOS[] = {
    {"LISTO",       0,   0,   255, 0,                    NULL},
    {"REGISTRADO",  0,   255, 0,   DURACION_TEMPORAL_MS, "LISTO"},
    {"DESCONOCIDO", 255, 0,   0,   DURACION_TEMPORAL_MS, "LISTO"},
    {"FALLO",       255, 160, 0,   0,                    NULL},
    {"APAGADO",     0,   0,   0,   0,                    NULL},
};
#define NUM_ESTADOS (sizeof(ESTADOS) / sizeof(ESTADOS[0]))

static volatile sig_atomic_t g_salir = 0;

/* ------------------------------- Registro -------------------------------- */
static void registrar(const char *nivel, const char *fmt, ...)
{
    struct timespec ts;
    struct tm tm_local;
    clock_gettime(CLOCK_REALTIME, &ts);
    localtime_r(&ts.tv_sec, &tm_local);
    printf("%02d:%02d:%02d.%03ld [%s] ", tm_local.tm_hour, tm_local.tm_min,
           tm_local.tm_sec, ts.tv_nsec / 1000000L, nivel);
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    printf("\n");
    fflush(stdout);
}

static long long ahora_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000;
}

static void manejar_senal(int sig)
{
    (void)sig;
    g_salir = 1;
}

/* ------------------------------ Codificación ----------------------------- */
/* Escribe 'bit' en la posición 'pos' (MSB primero) del búfer. */
static void poner_bit(uint8_t *buf, size_t pos, int bit)
{
    if (bit)
        buf[pos / 8] |= (uint8_t)(0x80u >> (pos % 8));
}

/*
 * Convierte los colores (G,R,B por LED) en el tren de bits SPI.
 * Devuelve el número de bytes a transmitir (inicio + datos + reset).
 */
static size_t codificar(const uint8_t *grb, int num_leds, uint8_t *salida)
{
    size_t total = BYTES_INICIO + BYTES_DATOS(num_leds) + BYTES_RESET;
    memset(salida, 0, total);

    size_t pos = BYTES_INICIO * 8;
    for (int i = 0; i < num_leds * 3; i++) {
        for (int b = 7; b >= 0; b--) {
            int bit = (grb[i] >> b) & 1;
            poner_bit(salida, pos, 1);       /* primer bit SPI: siempre en alto */
            poner_bit(salida, pos + 1, bit); /* segundo: alto solo si el bit es 1 */
            /* tercer bit SPI: siempre en bajo (ya está en 0) */
            pos += BITS_SPI_POR_BIT;
        }
    }
    return total;
}

/* Proceso inverso; solo se usa en la autoprueba (-T). Devuelve 0 si es válido. */
static int decodificar(const uint8_t *buf, int num_leds, uint8_t *grb)
{
    size_t pos = BYTES_INICIO * 8;
    for (int i = 0; i < num_leds * 3; i++) {
        uint8_t valor = 0;
        for (int b = 0; b < 8; b++) {
            int s0 = (buf[pos / 8] >> (7 - pos % 8)) & 1;
            int s1 = (buf[(pos + 1) / 8] >> (7 - (pos + 1) % 8)) & 1;
            int s2 = (buf[(pos + 2) / 8] >> (7 - (pos + 2) % 8)) & 1;
            if (s0 != 1 || s2 != 0)
                return -1;                  /* patrón distinto de 100 / 110 */
            valor = (uint8_t)((valor << 1) | s1);
            pos += BITS_SPI_POR_BIT;
        }
        grb[i] = valor;
    }
    return 0;
}

static int autoprueba(void)
{
    uint8_t buf[TAM_BUFER_MAX];
    uint8_t grb[NUM_LEDS_MAX * 3], leido[NUM_LEDS_MAX * 3];
    int fallas = 0;

    /* Casos fijos: todo en 0, todo en 1, patrón alternado y valores aleatorios */
    for (int caso = 0; caso < 4; caso++) {
        for (int i = 0; i < NUM_LEDS_DEF * 3; i++) {
            switch (caso) {
            case 0:  grb[i] = 0x00; break;
            case 1:  grb[i] = 0xFF; break;
            case 2:  grb[i] = 0xA5; break;
            default: grb[i] = (uint8_t)rand(); break;
            }
        }
        size_t n = codificar(grb, NUM_LEDS_DEF, buf);
        int ok = decodificar(buf, NUM_LEDS_DEF, leido) == 0 &&
                 memcmp(grb, leido, NUM_LEDS_DEF * 3) == 0;
        registrar(ok ? "INFO" : "ERROR", "Autoprueba caso %d: %zu bytes, %s", caso, n,
                  ok ? "codificación correcta" : "codificación INCORRECTA");
        fallas += !ok;
    }

    /* Un byte 0x00 debe producir 100 100 100 100 100 100 100 100 = 0x92 0x49 0x24 */
    uint8_t cero[3] = {0, 0, 0};
    codificar(cero, 1, buf);
    int ok = buf[BYTES_INICIO] == 0x92 && buf[BYTES_INICIO + 1] == 0x49 &&
             buf[BYTES_INICIO + 2] == 0x24;
    registrar(ok ? "INFO" : "ERROR", "Byte 0x00 -> %02X %02X %02X (esperado 92 49 24)",
              buf[BYTES_INICIO], buf[BYTES_INICIO + 1], buf[BYTES_INICIO + 2]);
    fallas += !ok;

    registrar("INFO", "Tiempos: bit SPI %.3f us, bit WS2812 %.3f us, reset %.0f us",
              1e6 / SPI_VELOCIDAD_HZ, 1e6 * BITS_SPI_POR_BIT / SPI_VELOCIDAD_HZ,
              1e6 * BYTES_RESET * 8 / SPI_VELOCIDAD_HZ);
    registrar(fallas ? "ERROR" : "INFO", "Autoprueba %s (%d fallas)",
              fallas ? "FALLIDA" : "superada", fallas);
    return fallas ? 1 : 0;
}

/* ---------------------------------- SPI ---------------------------------- */
static int abrir_spi(const char *dispositivo)
{
#ifdef __linux__
    int fd = open(dispositivo, O_WRONLY);
    if (fd < 0) {
        registrar("ERROR", "No se pudo abrir %s: %s (¿SPI activado en raspi-config? "
                  "¿usuario en el grupo spi?)", dispositivo, strerror(errno));
        return -1;
    }
    uint8_t modo = SPI_MODE_0;
    uint8_t bits = 8;
    uint32_t velocidad = SPI_VELOCIDAD_HZ;
    if (ioctl(fd, SPI_IOC_WR_MODE, &modo) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &velocidad) < 0) {
        registrar("ERROR", "No se pudo configurar %s: %s", dispositivo, strerror(errno));
        close(fd);
        return -1;
    }
    registrar("INFO", "SPI abierto: %s, modo 0, 8 bits, %u Hz", dispositivo, velocidad);
    return fd;
#else
    (void)dispositivo;
    registrar("ERROR", "SPI solo disponible en Linux; usa -s para simular");
    return -1;
#endif
}

static int enviar_spi(int fd, const uint8_t *buf, size_t n)
{
#ifdef __linux__
    struct spi_ioc_transfer tr;
    memset(&tr, 0, sizeof(tr));
    tr.tx_buf = (unsigned long)buf;
    tr.len = (uint32_t)n;
    tr.speed_hz = SPI_VELOCIDAD_HZ;
    tr.bits_per_word = 8;
    if (ioctl(fd, SPI_IOC_MESSAGE(1), &tr) < 0) {
        registrar("ERROR", "Falló la transferencia SPI: %s", strerror(errno));
        return -1;
    }
    return 0;
#else
    (void)fd; (void)buf; (void)n;
    return -1;
#endif
}

/* ------------------------------- Estados --------------------------------- */
static const Estado *buscar_estado(const char *nombre)
{
    for (size_t i = 0; i < NUM_ESTADOS; i++)
        if (strcmp(ESTADOS[i].nombre, nombre) == 0)
            return &ESTADOS[i];
    return NULL;
}

typedef struct {
    int fd_spi;            /* -1 en simulación */
    int simular;
    int num_leds;
    int brillo;
    unsigned long cambios;
    unsigned long errores;
    long long t_spi_max_us;
    long long t_spi_total_us;
} Tira;

static int aplicar_estado(Tira *t, const Estado *e)
{
    uint8_t grb[NUM_LEDS_MAX * 3];
    uint8_t buf[TAM_BUFER_MAX];
    uint8_t r = (uint8_t)(e->r * t->brillo / 100);
    uint8_t g = (uint8_t)(e->g * t->brillo / 100);
    uint8_t b = (uint8_t)(e->b * t->brillo / 100);

    for (int i = 0; i < t->num_leds; i++) {
        grb[i * 3] = g;
        grb[i * 3 + 1] = r;
        grb[i * 3 + 2] = b;
    }

    long long t0 = ahora_us();
    size_t n = codificar(grb, t->num_leds, buf);
    int res = 0;
    if (t->simular) {
        printf("            [SIM] %zu bytes: %02X %02X %02X ... (R=%u G=%u B=%u)\n",
               n, buf[BYTES_INICIO], buf[BYTES_INICIO + 1], buf[BYTES_INICIO + 2], r, g, b);
    } else {
        res = enviar_spi(t->fd_spi, buf, n);
    }
    long long dt = ahora_us() - t0;

    if (res < 0) {
        t->errores++;
        return -1;
    }
    t->cambios++;
    t->t_spi_total_us += dt;
    if (dt > t->t_spi_max_us)
        t->t_spi_max_us = dt;
    registrar("INFO", "Estado %s aplicado (R=%u G=%u B=%u, brillo %d %%) en %lld us",
              e->nombre, r, g, b, t->brillo, dt);
    return 0;
}

/* Quita espacios y fin de línea, y pasa a mayúsculas. */
static void normalizar(char *s)
{
    char *ini = s;
    while (*ini && isspace((unsigned char)*ini))
        ini++;
    memmove(s, ini, strlen(ini) + 1);
    size_t n = strlen(s);
    while (n > 0 && isspace((unsigned char)s[n - 1]))
        s[--n] = '\0';
    for (char *p = s; *p; p++)
        *p = (char)toupper((unsigned char)*p);
}

/* ---------------------------------- FIFO --------------------------------- */
static int preparar_fifo(const char *ruta, int *fd_lectura, int *fd_reserva)
{
    struct stat st;
    if (stat(ruta, &st) == 0) {
        if (!S_ISFIFO(st.st_mode)) {
            registrar("ERROR", "%s existe y no es una FIFO", ruta);
            return -1;
        }
    } else if (mkfifo(ruta, 0660) < 0) {
        registrar("ERROR", "No se pudo crear la FIFO %s: %s", ruta, strerror(errno));
        return -1;
    } else {
        registrar("INFO", "FIFO creada: %s", ruta);
    }

    *fd_lectura = open(ruta, O_RDONLY | O_NONBLOCK);
    if (*fd_lectura < 0) {
        registrar("ERROR", "No se pudo abrir la FIFO: %s", strerror(errno));
        return -1;
    }
    /* Extremo de escritura propio: evita que poll() marque fin de archivo
       cada vez que el proceso de Python cierra su extremo. */
    *fd_reserva = open(ruta, O_WRONLY | O_NONBLOCK);
    if (*fd_reserva < 0) {
        registrar("ERROR", "No se pudo abrir la FIFO para reserva: %s", strerror(errno));
        close(*fd_lectura);
        return -1;
    }
    registrar("INFO", "Esperando mensajes en %s", ruta);
    return 0;
}

/* --------------------------------- main ---------------------------------- */
static void uso(const char *prog)
{
    fprintf(stderr,
            "Uso: %s [-d dispositivo] [-f fifo] [-n leds] [-b brillo%%] [-s] [-T]\n"
            "  -d  dispositivo SPI (por defecto %s)\n"
            "  -f  ruta de la FIFO (por defecto %s)\n"
            "  -n  número de LED, 1 a %d (por defecto %d)\n"
            "  -b  brillo en %%, 1 a 100 (por defecto %d)\n"
            "  -s  simulación: no usa SPI\n"
            "  -T  autoprueba de la codificación\n",
            prog, SPI_DISPOSITIVO_DEF, FIFO_DEF, NUM_LEDS_MAX, NUM_LEDS_DEF, BRILLO_DEF);
}

int main(int argc, char **argv)
{
    const char *dispositivo = SPI_DISPOSITIVO_DEF;
    const char *ruta_fifo = FIFO_DEF;
    Tira tira = {.fd_spi = -1, .num_leds = NUM_LEDS_DEF, .brillo = BRILLO_DEF};
    int opt;

    while ((opt = getopt(argc, argv, "d:f:n:b:sTh")) != -1) {
        switch (opt) {
        case 'd': dispositivo = optarg; break;
        case 'f': ruta_fifo = optarg; break;
        case 'n': tira.num_leds = atoi(optarg); break;
        case 'b': tira.brillo = atoi(optarg); break;
        case 's': tira.simular = 1; break;
        case 'T': return autoprueba();
        default:  uso(argv[0]); return opt == 'h' ? 0 : 2;
        }
    }
    if (tira.num_leds < 1 || tira.num_leds > NUM_LEDS_MAX ||
        tira.brillo < 1 || tira.brillo > 100) {
        uso(argv[0]);
        return 2;
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = manejar_senal;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    registrar("INFO", "=== neopixel_spi: %d LED, brillo %d %%, %s ===", tira.num_leds,
              tira.brillo, tira.simular ? "SIMULACIÓN" : dispositivo);

    if (!tira.simular) {
        tira.fd_spi = abrir_spi(dispositivo);
        if (tira.fd_spi < 0)
            return 1;
    }

    int fd_fifo = -1, fd_reserva = -1;
    if (preparar_fifo(ruta_fifo, &fd_fifo, &fd_reserva) < 0) {
        if (tira.fd_spi >= 0)
            close(tira.fd_spi);
        return 1;
    }

    const Estado *actual = buscar_estado("LISTO");
    aplicar_estado(&tira, actual);
    long long fin_temporal_us = 0;   /* 0 = el estado actual es permanente */

    char pendiente[TAM_LINEA * 4];
    size_t len_pendiente = 0;
    unsigned long mensajes = 0, desconocidos = 0;
    long long t_inicio = ahora_us();

    while (!g_salir) {
        int espera = -1;
        if (fin_temporal_us) {
            long long resta = (fin_temporal_us - ahora_us()) / 1000;
            espera = resta > 0 ? (int)resta : 0;
        }

        struct pollfd pfd = {.fd = fd_fifo, .events = POLLIN};
        int r = poll(&pfd, 1, espera);
        if (r < 0) {
            if (errno == EINTR)
                continue;
            registrar("ERROR", "poll falló: %s", strerror(errno));
            break;
        }

        if (r == 0) {   /* terminó el verde / rojo */
            actual = buscar_estado(actual->siguiente);
            fin_temporal_us = 0;
            aplicar_estado(&tira, actual);
            continue;
        }

        char tmp[TAM_LINEA];
        ssize_t n = read(fd_fifo, tmp, sizeof(tmp));
        if (n <= 0)
            continue;

        for (ssize_t i = 0; i < n; i++) {
            if (tmp[i] != '\n' && len_pendiente < sizeof(pendiente) - 1) {
                pendiente[len_pendiente++] = tmp[i];
                continue;
            }
            if (tmp[i] != '\n') {          /* línea demasiado larga: se descarta */
                registrar("WARNING", "Mensaje demasiado largo, descartado");
                len_pendiente = 0;
                continue;
            }
            pendiente[len_pendiente] = '\0';
            len_pendiente = 0;
            normalizar(pendiente);
            if (pendiente[0] == '\0')
                continue;

            mensajes++;
            const Estado *e = buscar_estado(pendiente);
            if (!e) {
                desconocidos++;
                registrar("WARNING", "Mensaje desconocido: \"%s\"", pendiente);
                continue;
            }
            registrar("INFO", "Mensaje recibido: %s", e->nombre);
            actual = e;
            fin_temporal_us = e->duracion_ms ? ahora_us() + e->duracion_ms * 1000LL : 0;
            aplicar_estado(&tira, actual);
        }
    }

    registrar("INFO", "Terminando: se apagan los LED");
    aplicar_estado(&tira, buscar_estado("APAGADO"));

    double segundos = (ahora_us() - t_inicio) / 1e6;
    registrar("INFO", "=== Resumen ===");
    registrar("INFO", "Tiempo en ejecución: %.1f s", segundos);
    registrar("INFO", "Mensajes: %lu (desconocidos: %lu)", mensajes, desconocidos);
    registrar("INFO", "Cambios de color: %lu | errores SPI: %lu", tira.cambios, tira.errores);
    if (tira.cambios)
        registrar("INFO", "Tiempo por actualización: prom %lld us, máx %lld us",
                  tira.t_spi_total_us / (long long)tira.cambios, tira.t_spi_max_us);

    close(fd_fifo);
    close(fd_reserva);
    if (tira.fd_spi >= 0)
        close(tira.fd_spi);
    return tira.errores ? 1 : 0;
}
