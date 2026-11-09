# Makefile del programa de señalización (NeoPixel por SPI)
CC      ?= gcc
CFLAGS  ?= -Wall -Wextra -O2
TARGET  := neopixel_spi

.PHONY: all clean prueba

all: $(TARGET)

$(TARGET): neopixel_spi.c
	$(CC) $(CFLAGS) -o $@ $<

# Autoprueba de la codificación SPI (no necesita la Raspberry Pi)
prueba: $(TARGET)
	./$(TARGET) -T

clean:
	rm -f $(TARGET)
