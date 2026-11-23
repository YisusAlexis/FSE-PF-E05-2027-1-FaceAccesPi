/*
 * neopixel_spi.c - Señalización del control de acceso con una tira WS2812 (NeoPixel)
 *                  conectada al bus SPI0 de la Raspberry Pi 4 (MOSI = GPIO10, pin 19).
 *
 * Lee mensajes de texto de una tubería con nombre (FIFO) y cambia el color de la tira:
 *
 *   LISTO        -> azul (permanente)
 *   REGISTRADO   -> verde durante 1 s, luego vuelve a LISTO
 *   DESCONOCIDO  -> rojo durante 1 s, luego vuelve a LISTO
 *   ENTRADA      -> verde durante 2 s (registro de entrada)
 *   SALIDA       -> verde durante 2 s (registro de salida)
 *   AVISO        -> amarillo durante 1.5 s (rostro poco claro: acercarse; reloj sin sincronizar)
 *   REVISION     -> rojo durante 2 s (bloqueado: límite diario, enviado a revisión)
 *   FALLO        -> amarillo (permanente)
 *   APAGADO      -> todos los LED apagados
 *
 * Decisión bitácora clase 18: Linux no garantiza los pulsos del WS2812, así que no se
 * generan cambiando un GPIO. El periférico SPI los genera por hardware:
 * a 3.2 MHz cada bit SPI dura 0.3125 us y cada bit del LED se codifica con 4 bits SPI
 *   bit 0 del LED -> 1000  (0.3125 us en alto, 0.9375 us en bajo)
 *   bit 1 del LED -> 1100  (0.625 us en alto,  0.625 us en bajo)
 * Cumple la hoja de datos del WS2812 (T0H 0.35+-0.15, T1H 0.7+-0.15 us) y la del WS2812B
 * actual (T0H 0.22-0.38 us, T1H 0.58-1.0 us). La versión del Avance 3 (3 bits a 2.4 MHz,
 * T0H = 0.417 us) quedaba fuera del rango del WS2812B y producía destellos en blanco.
 * 8 LED x 24 bits x 4 = 768 bits = 96 bytes, más bytes en cero para el reset (> 280 us).
 * El color se reenvía cada segundo para corregir solo cualquier error transitorio.
 *
 * Compensación térmica (Avance 4): el reloj del SPI se deriva del reloj del núcleo (VPU) y
 * el driver spi-bcm2835 calcula el divisor suponiendo 500 MHz. Cuando la Pi llega a ~80 °C
 * el firmware baja el núcleo a 333 MHz (throttled bit 3); el SPI bajaba a ~2.1 MHz, el pulso
 * de un 0 crecía a 0.47 us y la tira lo leía como 1 (destellos blancos a brillo máximo).
 * Antes de cada trama se lee la frecuencia REAL del núcleo por el mailbox del firmware
 * (/dev/vcio, la misma fuente que "vcgencmd measure_clock core") y se pide al driver una
 * velocidad escalada para que la señal en el cable quede en 3.2 MHz.
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
#define SPI_VELOCIDAD_HZ     3200000u   /* 4 bits SPI = 1.25 us = 800 kHz del WS2812 */
#define SPI_VELOCIDAD_MAX_HZ 8000000u   /* tope para la velocidad compensada */
#define CORE_NOMINAL_HZ      500000000u /* reloj del núcleo que supone el driver spi-bcm2835 */
#define MAILBOX_DISPOSITIVO  "/dev/vcio"
#define MBOX_TAG_RELOJ_MEDIDO 0x00030047u   /* GET_CLOCK_RATE_MEASURED */
#define MBOX_RELOJ_CORE      4u
#define BITS_SPI_POR_BIT     4
#define BITS_POR_LED         24         /* orden G, R, B; 8 bits cada uno */
#define NUM_LEDS_DEF         8
#define NUM_LEDS_MAX         64
#define BRILLO_DEF           20         /* %: limita la corriente a ~100 mA con 8 LED */
#define BYTES_INICIO         1          /* línea en bajo antes de los datos */
#define BYTES_RESET          120        /* 120 x 8 x 0.3125 us = 300 us en bajo (> 280 us) */
#define DURACION_TEMPORAL_MS 1000       /* tiempo de verde / rojo antes de volver a LISTO */
#define DURACION_REGISTRO_MS 2000       /* tiempo de ENTRADA / SALIDA / REVISION */
#define DURACION_AVISO_MS    1500       /* tiempo del amarillo de aviso */
#define REFRESCO_MS          1000       /* reenvío periódico del color actual */
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
    {"ENTRADA",     0,   255, 0,   DURACION_REGISTRO_MS, "LISTO"},
    {"SALIDA",      0,   255, 0,   DURACION_REGISTRO_MS, "LISTO"},
    {"AVISO",       255, 160, 0,   DURACION_AVISO_MS,    "LISTO"},
    {"REVISION",    255, 0,   0,   DURACION_REGISTRO_MS, "LISTO"},
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
            /* tercer y cuarto bit SPI: siempre en bajo (ya están en 0) */
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
            int s3 = (buf[(pos + 3) / 8] >> (7 - (pos + 3) % 8)) & 1;
            if (s0 != 1 || s2 != 0 || s3 != 0)
                return -1;                  /* patrón distinto de 1000 / 1100 */
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

    /* Byte 0x00 -> 1000 x 8 = 88 88 88 88; byte 0xFF -> 1100 x 8 = CC CC CC CC */
    uint8_t prueba[3] = {0x00, 0xFF, 0x00};
    codificar(prueba, 1, buf);
    const uint8_t *d = buf + BYTES_INICIO;
    int ok = d[0] == 0x88 && d[3] == 0x88 && d[4] == 0xCC && d[7] == 0xCC;
    registrar(ok ? "INFO" : "ERROR", "Byte 0x00 -> %02X %02X %02X %02X (esperado 88 88 88 88)",
              d[0], d[1], d[2], d[3]);
    registrar(ok ? "INFO" : "ERROR", "Byte 0xFF -> %02X %02X %02X %02X (esperado CC CC CC CC)",
              d[4], d[5], d[6], d[7]);
    fallas += !ok;

    double bit_spi = 1e6 / SPI_VELOCIDAD_HZ;
    registrar("INFO", "Tiempos: bit SPI %.4f us, bit WS2812 %.3f us, T0H %.3f us, T1H %.3f us, "
              "reset %.0f us", bit_spi, bit_spi * BITS_SPI_POR_BIT, bit_spi, 2 * bit_spi,
              bit_spi * BYTES_RESET * 8);
    registrar(fallas ? "ERROR" : "INFO", "Autoprueba %s (%d fallas)",
              fallas ? "FALLIDA" : "superada", fallas);
    return fallas ? 1 : 0;
}

/* ------------------------- Mailbox del firmware -------------------------- */
#define IOCTL_MBOX_PROPERTY _IOWR(100, 0, char *)
static int g_mbox = -1;

static void abrir_mailbox(void)
{
    g_mbox = open(MAILBOX_DISPOSITIVO, O_RDWR);
    if (g_mbox < 0)
        registrar("WARNING", "No se pudo abrir %s (%s): sin compensación térmica del SPI "
                  "(¿usuario en el grupo video?)", MAILBOX_DISPOSITIVO, strerror(errno));
    else
        registrar("INFO", "Mailbox %s abierto: compensación térmica del SPI activa", MAILBOX_DISPOSITIVO);
}

/* Frecuencia medida del núcleo en Hz (0 si no se puede leer). */
static uint32_t leer_core_hz(void)
{
    if (g_mbox < 0)
        return 0;
    uint32_t msg[8] __attribute__((aligned(16))) = {
        8 * sizeof(uint32_t), 0,                       /* tamaño total, petición */
        MBOX_TAG_RELOJ_MEDIDO, 8, 0, MBOX_RELOJ_CORE, 0, /* etiqueta, búfer, id de reloj, valor */
        0                                              /* fin */
    };
    if (ioctl(g_mbox, IOCTL_MBOX_PROPERTY, msg) < 0 || msg[1] != 0x80000000u)
        return 0;
    return msg[6];
}

/* Velocidad a pedir al driver para que en el cable salgan SPI_VELOCIDAD_HZ. */
static uint32_t velocidad_compensada(uint32_t core_hz)
{
    if (core_hz < CORE_NOMINAL_HZ / 4 || core_hz > CORE_NOMINAL_HZ * 2)
        return SPI_VELOCIDAD_HZ;                       /* lectura inválida: valor nominal */
    uint64_t v = (uint64_t)SPI_VELOCIDAD_HZ * CORE_NOMINAL_HZ / core_hz;
    return v > SPI_VELOCIDAD_MAX_HZ ? SPI_VELOCIDAD_MAX_HZ : (uint32_t)v;
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
    uint32_t velocidad = SPI_VELOCIDAD_MAX_HZ;   /* tope; cada trama pide su velocidad */
    if (ioctl(fd, SPI_IOC_WR_MODE, &modo) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &velocidad) < 0) {
        registrar("ERROR", "No se pudo configurar %s: %s", dispositivo, strerror(errno));
        close(fd);
        return -1;
    }
    registrar("INFO", "SPI abierto: %s, modo 0, 8 bits, %u Hz nominales (tope %u Hz)",
              dispositivo, SPI_VELOCIDAD_HZ, velocidad);
    return fd;
#else
    (void)dispositivo;
    registrar("ERROR", "SPI solo disponible en Linux; usa -s para simular");
    return -1;
#endif
}

static int enviar_spi(int fd, const uint8_t *buf, size_t n, uint32_t velocidad)
{
#ifdef __linux__
    struct spi_ioc_transfer tr;
    memset(&tr, 0, sizeof(tr));
    tr.tx_buf = (unsigned long)buf;
    tr.len = (uint32_t)n;
    tr.speed_hz = velocidad;
    tr.bits_per_word = 8;
    if (ioctl(fd, SPI_IOC_MESSAGE(1), &tr) < 0) {
        registrar("ERROR", "Falló la transferencia SPI: %s", strerror(errno));
        return -1;
    }
    return 0;
#else
    (void)fd; (void)buf; (void)n; (void)velocidad;
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
    unsigned long refrescos;
    unsigned long errores;
    long long t_spi_max_us;
    long long t_spi_total_us;
    unsigned long compensadas;   /* tramas enviadas con el núcleo por debajo de 500 MHz */
    uint32_t core_min_hz;
    uint32_t core_anterior_hz;
    long long t_aviso_core_us;
} Tira;

static int aplicar_estado(Tira *t, const Estado *e, int es_refresco)
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
        if (!es_refresco)
            printf("            [SIM] %zu bytes: %02X %02X %02X ... (R=%u G=%u B=%u)\n",
                   n, buf[BYTES_INICIO], buf[BYTES_INICIO + 1], buf[BYTES_INICIO + 2], r, g, b);
    } else {
        uint32_t core = leer_core_hz();
        uint32_t velocidad = velocidad_compensada(core);
        if (core && core < CORE_NOMINAL_HZ * 99ULL / 100) {
            t->compensadas++;
            if (!t->core_min_hz || core < t->core_min_hz)
                t->core_min_hz = core;
        }
        /* aviso en el log cuando cambia la frecuencia (máximo uno cada 10 s) */
        int cambio = core && t->core_anterior_hz &&
                     (core > t->core_anterior_hz * 1.05 || core < t->core_anterior_hz * 0.95);
        if (cambio && t0 - t->t_aviso_core_us > 10000000LL) {
            registrar("WARNING", "Reloj del núcleo %u MHz (antes %u MHz): SPI compensado a %u Hz",
                      core / 1000000, t->core_anterior_hz / 1000000, velocidad);
            t->t_aviso_core_us = t0;
        }
        if (core)
            t->core_anterior_hz = core;
        res = enviar_spi(t->fd_spi, buf, n, velocidad);
    }
    long long dt = ahora_us() - t0;

    if (res < 0) {
        t->errores++;
        return -1;
    }
    t->t_spi_total_us += dt;
    if (dt > t->t_spi_max_us)
        t->t_spi_max_us = dt;
    if (es_refresco) {          /* reenvío periódico: no se escribe en el log */
        t->refrescos++;
        return 0;
    }
    t->cambios++;
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

/* Convierte texto a entero validando todo el texto (en lugar de atoi). */
static int leer_entero(const char *texto, int *valor)
{
    char *fin;
    errno = 0;
    long v = strtol(texto, &fin, 10);
    if (errno != 0 || fin == texto || *fin != '\0' || v < -100000 || v > 100000)
        return -1;
    *valor = (int)v;
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
        case 'n':
            if (leer_entero(optarg, &tira.num_leds) < 0) { uso(argv[0]); return 2; }
            break;
        case 'b':
            if (leer_entero(optarg, &tira.brillo) < 0) { uso(argv[0]); return 2; }
            break;
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
        abrir_mailbox();
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
    aplicar_estado(&tira, actual, 0);
    long long fin_temporal_us = 0;   /* 0 = el estado actual es permanente */
    long long ultimo_envio_us = ahora_us();

    char pendiente[TAM_LINEA * 4];
    size_t len_pendiente = 0;
    unsigned long mensajes = 0, desconocidos = 0;
    long long t_inicio = ahora_us();

    while (!g_salir) {
        /* espera hasta lo primero que ocurra: fin del color temporal o refresco */
        long long ahora = ahora_us();
        long long limite = ultimo_envio_us + REFRESCO_MS * 1000LL;
        if (fin_temporal_us && fin_temporal_us < limite)
            limite = fin_temporal_us;
        long long resta = (limite - ahora) / 1000;
        int espera = resta > 0 ? (int)resta : 0;

        struct pollfd pfd = {.fd = fd_fifo, .events = POLLIN};
        int r = poll(&pfd, 1, espera);
        if (r < 0) {
            if (errno == EINTR)
                continue;
            registrar("ERROR", "poll falló: %s", strerror(errno));
            break;
        }

        if (r == 0) {
            if (fin_temporal_us && ahora_us() >= fin_temporal_us) {   /* terminó el color temporal */
                actual = buscar_estado(actual->siguiente);
                fin_temporal_us = 0;
                aplicar_estado(&tira, actual, 0);
            } else {                                                   /* refresco periódico */
                aplicar_estado(&tira, actual, 1);
            }
            ultimo_envio_us = ahora_us();
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
            aplicar_estado(&tira, actual, 0);
            ultimo_envio_us = ahora_us();
        }
    }

    registrar("INFO", "Terminando: se apagan los LED");
    aplicar_estado(&tira, buscar_estado("APAGADO"), 0);

    double segundos = (ahora_us() - t_inicio) / 1e6;
    registrar("INFO", "=== Resumen ===");
    registrar("INFO", "Tiempo en ejecución: %.1f s", segundos);
    registrar("INFO", "Mensajes: %lu (desconocidos: %lu)", mensajes, desconocidos);
    registrar("INFO", "Cambios de color: %lu | refrescos: %lu | errores SPI: %lu",
              tira.cambios, tira.refrescos, tira.errores);
    if (tira.cambios + tira.refrescos)
        registrar("INFO", "Tramas con el núcleo bajo 500 MHz (compensadas): %lu | mínimo visto: %u MHz",
                  tira.compensadas, tira.core_min_hz / 1000000);
    if (tira.cambios + tira.refrescos)
        registrar("INFO", "Tiempo por actualización: prom %lld us, máx %lld us",
                  tira.t_spi_total_us / (long long)(tira.cambios + tira.refrescos), tira.t_spi_max_us);

    close(fd_fifo);
    close(fd_reserva);
    if (tira.fd_spi >= 0)
        close(tira.fd_spi);
    if (g_mbox >= 0)
        close(g_mbox);
    return tira.errores ? 1 : 0;
}
