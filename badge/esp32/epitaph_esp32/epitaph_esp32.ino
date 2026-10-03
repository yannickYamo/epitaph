// epitaph on an ESP32: the 260K-parameter model lives ten minutes on the chip while the chip
// takes its world away (its light, its memory window, its CPU clock, then its heap), dies,
// and is born again. Its words go to the serial port: open a terminal at 115200 baud.
//
// Any ESP32 board with 4 MB of flash works; no PSRAM needed. The weights (260 KB, int8)
// live in flash, the memory window (160 KB) and the thought's scratch in RAM.
// Arduino IDE: board "ESP32 Dev Module"; or arduino-cli compile --fqbn esp32:esp32:esp32.

#include <Arduino.h>
#include <esp_system.h>

#include "epitaph_tiny.h"

#ifndef LED_BUILTIN
#define LED_BUILTIN 2  // most ESP32 dev boards; set yours if it differs
#endif

static uint32_t now_ms() { return millis(); }
static void sleep_for(uint32_t ms) { delay(ms); }
static void write_text(const char *text) { Serial.print(text); }
static uint32_t random32() { return esp_random(); }
static void *heap_alloc(size_t n) { return malloc(n); }
static void heap_release(void *p) { free(p); }

static int set_clock(int mhz)
{
    if (!setCpuFrequencyMhz(mhz)) return 0;
    Serial.updateBaudRate(115200);  // keep the terminal readable after a clock change
    return 1;
}

static int set_light(int on)
{
    digitalWrite(LED_BUILTIN, on ? HIGH : LOW);
    return 1;
}

static const ep_platform platform = {now_ms, sleep_for, write_text, set_clock, set_light,
                                     random32, heap_alloc, heap_release};
static ep_config config;
static int life_number = 0;

void setup()
{
    Serial.begin(115200);
    pinMode(LED_BUILTIN, OUTPUT);
    ep_default_config(&config);
    delay(1000);
    if (!ep_init(&platform)) {
        Serial.println("not enough heap for the memory window: lower EP_SEQ in epitaph_tiny.h");
        for (;;) delay(1000);
    }
    Serial.printf("\nepitaph: %u bytes of heap free beside its memory\n\n", ESP.getFreeHeap());
}

void loop()
{
    float lived = ep_live(&platform, &config, ++life_number);
    Serial.printf("[life %d lived %.0f s]\n\n", life_number, lived);
}
