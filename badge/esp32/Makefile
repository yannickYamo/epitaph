# The ESP32 port, built and run on a laptop.
CFLAGS ?= -O2 -std=c11 -Wall -Wextra -Wno-unused-parameter
SRC = epitaph_esp32/epitaph_tiny.c host/test_host.c

host/test_host: $(SRC) epitaph_esp32/epitaph_tiny.h epitaph_esp32/model_data.h
	$(CC) $(CFLAGS) -D_POSIX_C_SOURCE=200809L -o $@ $(SRC) -lm

.PHONY: life clean
life: host/test_host
	./host/test_host life

clean:
	rm -f host/test_host
