/* epitaph on a microcontroller: the 260K-parameter model and its life, in portable C.
 *
 * The weights are int8 in flash (model_data.h); RAM holds only the memory window (the KV
 * cache, EP_SEQ x 1280 bytes) and 5 KB of scratch, which is allocated per thought so the
 * death is real: when the machine takes the heap, the next thought cannot be allocated.
 *
 * Nothing here knows a chip. A port fills in `ep_platform` below (the ESP32 sketch and the
 * laptop test are two) and calls ep_init once, then ep_live for ever. A hook the board has
 * no hardware for is left NULL (or returns 0): that loss is skipped, and the model is never
 * told of it. */
#pragma once
#include <stddef.h>
#include <stdint.h>

#ifndef EP_SEQ
#define EP_SEQ 128 /* positions it can hold at birth; 128 fits a plain ESP32's heap */
#endif

typedef struct {
    /* required */
    uint32_t (*millis)(void);
    void (*sleep_ms)(uint32_t ms);
    void (*write)(const char *text);   /* the screen: a serial terminal, a display */
    /* optional: NULL, or return 0, when the board cannot do it */
    int (*set_clock_mhz)(int mhz);     /* the CPU clock; 1 when it really changed */
    int (*set_light)(int on);          /* an LED; 1 when it is now as asked */
    /* required */
    uint32_t (*random32)(void);
    void *(*alloc)(size_t n);          /* the heap the machine takes at the end; NULL when full */
    void (*release)(void *ptr);
    /* optional */
    int (*set_screen)(int percent);    /* a backlight, 100 is full; 1 when it is now as asked */
    void (*event)(const char *kind, const char *text); /* told each loss ("take"), each reading
                                          it starts to read ("read") and the death ("die") */
} ep_platform;

typedef struct {
    int life_s;        /* the losses are spread over this; it dies after the last reading */
    int silence_s;     /* dark between lives */
    int full_mhz;      /* its clock at birth (0: this chip's clock is left alone) */
    int clocks[3];     /* the clock steps it is taken down through (0: no such step) */
} ep_config;

#ifdef __cplusplus
extern "C" {
#endif

/* Allocate the memory window (once, at boot); 0 when the heap is too small. */
int ep_init(const ep_platform *p);
void ep_default_config(ep_config *cfg);

/* Live one life, from birth to the death; returns the seconds it lived. */
float ep_live(const ep_platform *p, const ep_config *cfg, int life_number);

/* For tests: the logits for `token` at the next position (window 0 = everything). */
const float *ep_test_forward(int token, int window);
void ep_test_reset(void);
int ep_encode(const char *text, int *out, int max);
/* The words for share `r` of quantity `key` (0-2), as a life would say them; `reset` forgets
 * what was said before. */
const char *ep_test_say(int key, float r, int reset);

#ifdef __cplusplus
}
#endif
