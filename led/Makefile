# Makefile del programa de señalización (NeoPixel por SPI)
CC      ?= gcc
CFLAGS  ?= -Wall -Wextra -O2
TARGET  := neopixel_spi

.PHONY: all clean prueba asm

all: $(TARGET)

$(TARGET): neopixel_spi.c
	$(CC) $(CFLAGS) -o $@ $<

# Autoprueba de la codificación SPI (no necesita la Raspberry Pi)
prueba: $(TARGET)
	./$(TARGET) -T

# Clase 19: revisar el ensamblador que genera el compilador en la parte crítica (codificar)
#   neopixel_spi.s        salida de gcc -S (mismas opciones que el binario)
#   codificar.objdump     desensamblado de codificar() con el código C intercalado
asm: neopixel_spi.c
	$(CC) $(CFLAGS) -S -fverbose-asm -o neopixel_spi.s $<
	$(CC) $(CFLAGS) -g -fno-inline-functions-called-once -o neopixel_spi_dbg $<
	objdump -d -S --no-show-raw-insn neopixel_spi_dbg | awk '/<codificar>:/,/^$$/' > codificar.objdump
	@echo "codificar(): $$(grep -cE '^ +[0-9a-f]+:' codificar.objdump) instrucciones"
	@echo "  saltos/ramas:        $$(grep -cE '\s(b\.|cb|tb|j[a-z]+ )' codificar.objdump)"
	@echo "  llamadas a funciones: $$(grep -cE '\s(bl|call)\s' codificar.objdump) (memset)"
	@echo "Ver: led/neopixel_spi.s y led/codificar.objdump"

clean:
	rm -f $(TARGET) neopixel_spi_dbg neopixel_spi.s codificar.objdump
