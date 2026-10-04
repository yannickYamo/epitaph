// epitaph on a microcontroller: the 260K-parameter model lives ten minutes on the chip while
// the chip takes its world away (its light, its memory window, its CPU clock, then its heap),
// dies, and is born again. Its words go to the serial port: open a terminal at 115200 baud.
//
// ESP32: any board with 4 MB of flash works; no PSRAM needed. The weights (260 KB, int8)
// live in flash, the memory window (160 KB) and the thought's scratch in RAM.
// Arduino IDE: board "ESP32 Dev Module"; or arduino-cli compile --fqbn esp32:esp32:esp32.
//
// Another Arduino board: it needs 600 KB of flash and, for the memory window, 1280 bytes of
// RAM per position (set EP_SEQ in epitaph_tiny.h: 64 positions fit in 100 KB). Its light is
// LED_BUILTIN; its clock is left alone unless you fill in set_clock below. What a board cannot
// do is skipped, and the model is never told of it. badge/README.md, "Port it".

#include <Arduino.h>

#include "epitaph_tiny.h"

#ifndef LED_BUILTIN
#define LED_BUILTIN 2  // most ESP32 dev boards; set yours if it differs
#endif

static uint32_t now_ms() { return millis(); }
static void sleep_for(uint32_t ms) { delay(ms); }
static void write_text(const char *text) { Serial.print(text); }
static void *heap_alloc(size_t n) { return malloc(n); }
static void heap_release(void *p) { free(p); }

static int set_light(int on)
{
    digitalWrite(LED_BUILTIN, on ? HIGH : LOW);
    return 1;
}

#if defined(ESP32)
#include <esp_system.h>

static uint32_t random32() { return esp_random(); }

static int set_clock(int mhz)
{
    if (!setCpuFrequencyMhz(mhz)) return 0;
    Serial.updateBaudRate(115200);  // keep the terminal readable after a clock change
    return getCpuFrequencyMhz() == (uint32_t)mhz;
}

#define SET_CLOCK set_clock
#else
static uint32_t random32() { return ((uint32_t)random(0x10000) << 16) ^ (uint32_t)random(0x10000); }

// This chip's clock is left alone. To take it too, write int set_clock(int mhz) for the chip
// (return 1 only when the clock really changed), name it here, and set config.full_mhz and
// config.clocks in setup().
#define SET_CLOCK NULL
#endif

// millis, sleep, write, clock, light, random, alloc, release, screen (none), event (none)
static const ep_platform platform = {now_ms, sleep_for, write_text, SET_CLOCK, set_light,
                                     random32, heap_alloc, heap_release, NULL, NULL};
static ep_config config;
static int life_number = 0;

void setup()
{
    Serial.begin(115200);
    pinMode(LED_BUILTIN, OUTPUT);
    ep_default_config(&config);
    delay(1000);
#if !defined(ESP32)
    randomSeed(analogRead(0) ^ micros());
#endif
    if (!ep_init(&platform)) {
        Serial.println("not enough heap for the memory window: lower EP_SEQ in epitaph_tiny.h");
        for (;;) delay(1000);
    }
    Serial.println("\nepitaph\n");
}

void loop()
{
    float lived = ep_live(&platform, &config, ++life_number);
    Serial.print("[life ");
    Serial.print(life_number);
    Serial.print(" lived ");
    Serial.print((int)lived);
    Serial.println(" s]\n");
}
